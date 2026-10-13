# -*- coding: utf-8 -*-
"""p1-5 首启向导测试：/api/first-run/apikey 热接入 + first_run_done 标记闭环。

.env 隔离纪律：monkeypatch main.ROOT → tmp_path——端点写 (tmp)/.env，绝不触碰
仓库真实 .env 的真 key；端点还会写 os.environ（比 .env 优先级高），测试以
monkeypatch.delenv 注册原值，teardown 自动还原。
"""
import json
from contextlib import asynccontextmanager

import httpx
import pytest

import main


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    """向导测试环境：ROOT → tmp（.env 隔离）+ translator/status 复位。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # 端点会写 env，teardown 还原
    saved_runtime = dict(main.runtime)
    saved_translate = main.status["translate"]
    main.runtime["translator"] = None
    main.runtime["prompt"] = None
    main.status["translate"] = "disabled (no DEEPSEEK_API_KEY)"
    yield tmp_path
    main.runtime.clear()
    main.runtime.update(saved_runtime)
    main.status["translate"] = saved_translate


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_first_run_apikey_happy_path(wizard_env):
    """key 热接入：写 .env + 重建 runtime['translator'] + translate=ready；
    响应与 .env 之外不泄漏 key（响应体不含 key 原文）。"""
    key = "sk-test-1234567890abcdef"
    async with asgi_client() as c:
        r = await c.post("/api/first-run/apikey", json={"api_key": key})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "translate": "ready"}
    assert key not in r.text, "响应体泄漏 key 原文"
    env_lines = (wizard_env / ".env").read_text(encoding="utf-8").splitlines()
    assert f"DEEPSEEK_API_KEY={key}" in env_lines
    assert main.runtime["translator"] is not None
    assert main.status["translate"] == "ready"


async def test_first_run_apikey_replaces_line_keeps_others(wizard_env):
    """.env 已有旧 DEEPSEEK 行 → 替换不重复；无关行保留。"""
    (wizard_env / ".env").write_text(
        "OTHER_VAR=1\nDEEPSEEK_API_KEY=sk-old\n# comment\n", encoding="utf-8")
    key = "sk-new-9876543210"
    async with asgi_client() as c:
        r = await c.post("/api/first-run/apikey", json={"api_key": key})
    assert r.status_code == 200
    lines = (wizard_env / ".env").read_text(encoding="utf-8").splitlines()
    assert lines == ["OTHER_VAR=1", "# comment", f"DEEPSEEK_API_KEY={key}"]


async def test_first_run_apikey_empty_key_400(wizard_env):
    async with asgi_client() as c:
        r = await c.post("/api/first-run/apikey", json={"api_key": "  "})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert not (wizard_env / ".env").exists(), "空 key 不应落盘"


async def test_first_run_apikey_bad_body_400(wizard_env):
    async with asgi_client() as c:
        r = await c.post("/api/first-run/apikey", json=["not-a-dict"])
    assert r.status_code == 400


async def test_first_run_done_config_roundtrip(wizard_env):
    """first_run_done 即时键：true（完成/跳过）落盘 → false（设置页/关于页重开）落盘。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"first_run_done": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": {"first_run_done": True}}
    on_disk = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["first_run_done"] is True

    async with asgi_client() as c:
        r = await c.post("/api/config", json={"first_run_done": False})
    assert r.status_code == 200
    on_disk = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["first_run_done"] is False

    async with asgi_client() as c:
        r = await c.get("/api/config")
    assert r.status_code == 200
    assert r.json()["first_run_done"] is False  # GET 侧可见（向导弹出的依据）


async def test_first_run_apikey_rejects_unauthenticated(wizard_env):
    """非环回对端无 token → 403（/api/ 前缀自动纳入条目19 鉴权，key 不外泄）。"""
    main.runtime["config"]["auth_token"] = "wizard-test-token"
    main.runtime["config"]["auth_enabled"] = True
    transport = httpx.ASGITransport(app=main.app, client=("192.168.1.5", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/first-run/apikey", json={"api_key": "sk-x"})
    assert r.status_code == 403
    assert main.runtime["translator"] is None  # 未被接入
