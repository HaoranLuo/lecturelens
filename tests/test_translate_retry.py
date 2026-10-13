# -*- coding: utf-8 -*-
"""tests/test_translate_retry.py — DeepSeek 翻译重试逻辑离线单测（升级计划条目 2/6）。

不改生产代码：构造后把 tr.client 换成 httpx.MockTransport 后端（条目 6 的
transport=None 注入口落地前的替代 seam，行为等价）。重试间隔置 0 保证秒级。
真实 API 实调仍由 smoke_test [4]（--translate）覆盖。"""
import json

import httpx
import pytest

from translate import DeepSeekTranslator

KEY = "test-key"
ATTEMPTS = 4  # retry_delays=(0,0,0) → 首次 + 3 次重试


def make_translator() -> DeepSeekTranslator:
    return DeepSeekTranslator(KEY, retry_delays=(0.0, 0.0, 0.0))


def install_transport(tr: DeepSeekTranslator, handler) -> list[httpx.Request]:
    """替换底层传输并逐请求记录；返回请求列表供断言次数/形状。

    生产代码把 Authorization 头挂在构造时的 AsyncClient 上（translate.py:63-66），
    替换 client 必须带上同样的头，否则鉴权头不会出现在请求里。"""
    seen: list[httpx.Request] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    tr.client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_handler),
        headers={"Authorization": f"Bearer {KEY}"},
    )
    return seen


def ok_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_429_then_200_retries_and_succeeds():
    tr = make_translator()
    seen = install_transport(
        tr, lambda req, n: httpx.Response(429) if n == 1 else ok_response(" 注意力 "))
    assert await tr.translate("attention") == "注意力"  # 前后空白被 strip
    assert len(seen) == 2


async def test_429_exhausted_raises():
    tr = make_translator()
    seen = install_transport(tr, lambda req, n: httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        await tr.translate("attention")
    assert len(seen) == ATTEMPTS  # 退避耗尽才向上抛


async def test_401_raises_immediately():
    tr = make_translator()
    seen = install_transport(tr, lambda req, n: httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await tr.translate("attention")
    assert len(seen) == 1  # 鉴权/参数类 4xx 重试无意义，立即抛


async def test_timeout_retries_then_raises():
    tr = make_translator()

    def handler(req: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=req)

    seen = install_transport(tr, handler)
    with pytest.raises(httpx.TimeoutException):
        await tr.translate("attention")
    assert len(seen) == ATTEMPTS


async def test_request_shape_and_auth_header():
    tr = make_translator()
    captured: dict = {}

    def handler(req: httpx.Request, n: int) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        captured["json"] = json.loads(req.content)
        return ok_response("hi")

    install_transport(tr, handler)
    await tr.translate("gradient descent")
    assert captured["headers"].get("authorization") == f"Bearer {KEY}"
    payload = captured["json"]
    assert payload["model"] == "deepseek-chat"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "gradient descent"
    assert "translator" in payload["messages"][0]["content"].lower()


def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        DeepSeekTranslator("")


def test_system_prompt_embeds_glossary(tmp_path):
    g = tmp_path / "CS.md"
    g.write_text("attention = 注意力\n", encoding="utf-8")
    tr = DeepSeekTranslator(KEY, glossary_files=[str(g)])
    assert "Glossary (use these translations):" in tr.system
    assert "attention = 注意力" in tr.system
