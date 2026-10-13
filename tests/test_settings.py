# -*- coding: utf-8 -*-
"""p1-6 设置页测试：/api/security 只回布尔/枚举（凭据零回传）+ token 重新生成语义。

凭据纪律：测试 key 一律为明显不可用的假字面量（sk-test-…）；ROOT/CONFIG_PATH
经 monkeypatch 指向 tmp_path（conftest 已重定向 CONFIG_PATH，本文件再定 ROOT），
不触碰仓库真实 .env / config.json。
"""
import json
from contextlib import asynccontextmanager

import httpx
import pytest

import main

FAKE_KEY = "sk-test-not-a-real-key"  # 明显假值：仅用于存在性判定，不可用


@pytest.fixture
def sec_env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    saved = dict(main.runtime)
    main.runtime["config"].clear()
    main.runtime["config"].update(main.DEFAULT_CONFIG)
    main.runtime["config"]["auth_token"] = "test-token-aaaaaaaaaaaaaaaa"
    yield tmp_path
    main.runtime.clear()
    main.runtime.update(saved)


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_security_shape_and_no_credential_leak(sec_env):
    """形状：4 个键；响应文本不含现存 token 原文与任何 key 字符。"""
    main.runtime["config"]["auth_token"] = "secret-token-should-not-leak"
    async with asgi_client() as c:
        r = await c.get("/api/security")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"auth_enabled", "auth_token_set", "key_set", "key_source"}
    assert body["auth_token_set"] is True
    assert body["auth_enabled"] is True
    assert "secret-token-should-not-leak" not in r.text, "token 原文泄漏进响应"


async def test_security_key_source_tristate(sec_env, monkeypatch):
    """key 来源三态：none / file / env（env 优先于 file）。"""
    async with asgi_client() as c:
        r = await c.get("/api/security")
    assert r.json()["key_set"] is False and r.json()["key_source"] == "none"

    (sec_env / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.get("/api/security")
    assert r.json()["key_set"] is True and r.json()["key_source"] == "file"

    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    async with asgi_client() as c:
        r = await c.get("/api/security")
    assert r.json()["key_source"] == "env"  # env 优先
    assert FAKE_KEY not in r.text


async def test_security_empty_env_line_counts_as_unset(sec_env):
    """`DEEPSEEK_API_KEY=`（空值行）不算已配置（load_api_key 同口径：空串回落）。"""
    (sec_env / ".env").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.get("/api/security")
    assert r.json()["key_set"] is False
    assert r.json()["key_source"] == "none"


async def test_regen_token_rotates_and_persists_single_key(sec_env):
    """regen：返回新 token（≥32 字符）；runtime 更新；写盘只增补 auth_token
    （其他磁盘键保留）；旧值失效。"""
    main.CONFIG_PATH.write_text(
        '{"model": "tiny", "lang": "zh", "auth_token": "old-token-aaaaaaaaaaaa"}',
        encoding="utf-8")
    main.runtime["config"]["auth_token"] = "old-token-aaaaaaaaaaaa"
    async with asgi_client() as c:
        r = await c.post("/api/security/token/regenerate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    new_token = body["token"]
    assert isinstance(new_token, str) and len(new_token) >= 32
    assert new_token != "old-token-aaaaaaaaaaaa"
    assert main.runtime["config"]["auth_token"] == new_token  # 中间件即时生效
    on_disk = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["auth_token"] == new_token
    assert on_disk["model"] == "tiny" and on_disk["lang"] == "zh"  # 其他键保留


async def test_regen_when_config_missing_creates_file(sec_env):
    """config.json 不存在（首启场景）：regen 也能建文件并写入 token。"""
    assert not main.CONFIG_PATH.exists()
    async with asgi_client() as c:
        r = await c.post("/api/security/token/regenerate")
    assert r.status_code == 200
    on_disk = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk == {"auth_token": r.json()["token"]}


async def test_get_config_still_hides_auth_token(sec_env):
    """回归护栏：/api/config 永不回传 auth_token（设置页沿用既有口径，
    从未放宽评审修复②）。"""
    main.runtime["config"]["auth_token"] = "keep-hidden-token"
    async with asgi_client() as c:
        r = await c.get("/api/config")
    assert r.status_code == 200
    assert "auth_token" not in r.json()
    assert "keep-hidden-token" not in r.text
