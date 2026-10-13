# -*- coding: utf-8 -*-
"""tests/test_auth.py — LAN token 鉴权 + 全键白名单（升级计划条目 19）。

验收口径（§2.2-19）：
- 无 token 非环回 → 403；环回豁免 → 200（豁免只认 socket 对端地址，
  X-Forwarded-For 等请求头不参与豁免判定——防伪造，技术评审审查项）；
- 全键白名单：ALLOWED_CONFIG_KEYS 每键喂非法值 → 400 且 config.json 未改写、
  runtime['config'] 零污染（先验后存沿用条目 4 框架）；
- auth_token 是服务端管理键：不可经 /api/config 写入（否则持 token 的 LAN
  客户端可改写 token 形成后门）；
- 纯函数 is_loopback_peer / token_ok / auth_allows 全矩阵；
- provision_token：首启生成随机 token 写 config.json 并 print（值不落日志）；
  已有 token 幂等不重写；
- 凭据（DeepSeek key）不进 config.json / DEFAULT_CONFIG（条目19④）。

HTTP 用 httpx.ASGITransport（可注入对端地址 client=...，127.0.0.1=环回豁免分支）；
WS 拒绝用 TestClient（对端 "testclient" 非环回；accept 前被拒 → WebSocketDisconnect，
实机 uvicorn 为 HTTP 403 握手拒绝，由 smoke_test [5] 裸 socket 断言非 101）。
CONFIG_PATH 由 conftest 重定向 tmp_path；测试 token 全部运行期 secrets 生成——
源码无任何可用凭据字面量（Mimosa 约束）。"""
import json
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import WebSocketDisconnect
from starlette.testclient import TestClient

import main
from main import (ALLOWED_CONFIG_KEYS, DEFAULT_CONFIG, auth_allows,
                  is_loopback_peer, provision_token, token_ok)

TEST_TOKEN = secrets.token_urlsafe(16)   # 运行期生成，非源码字面量


@pytest.fixture
def app_state(monkeypatch):
    """打桩重型副作用 + 复位模块级可变状态 + 注入测试 token；teardown 还原
    runtime（防 TEST_TOKEN 泄漏进其它测试模块的 runtime['config']）。"""
    monkeypatch.setattr(main, "apply_segmenter", lambda: None)
    monkeypatch.delenv("LECTURELENS_AUTH_NO_LOCAL_EXEMPT", raising=False)
    saved = dict(main.runtime)
    main.runtime["config"].clear()
    main.runtime["config"].update(DEFAULT_CONFIG)
    main.runtime["config"]["auth_token"] = TEST_TOKEN
    main.runtime["reloading"] = False
    main.runtime["asr"] = None
    main.runtime["segmenter"] = None
    main.runtime["seg_q"] = None
    yield main
    main.runtime.clear()
    main.runtime.update(saved)


@asynccontextmanager
async def asgi_client(peer=("127.0.0.1", 123)):
    transport = httpx.ASGITransport(app=main.app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------- 纯函数：环回判定 / token 比较 / 鉴权判定 ----------------

@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("192.168.1.5", False), ("10.0.0.2", False), ("::ffff:192.168.1.5", False),
    ("", False), (None, False), ("not-an-ip", False), ("999.1.1.1", False),
])
def test_is_loopback_peer(host, expected):
    assert is_loopback_peer(host) is expected


@pytest.mark.parametrize("provided,expected,ok", [
    (TEST_TOKEN, TEST_TOKEN, True),
    (TEST_TOKEN, "other-token", False),
    (None, TEST_TOKEN, False),
    ("", TEST_TOKEN, False),
    (TEST_TOKEN, None, False),
    (TEST_TOKEN, "", False),
])
def test_token_ok(provided, expected, ok):
    assert token_ok(provided, expected) is ok


@pytest.mark.parametrize("kwargs,peer,allowed", [
    # 非环回：无 token 拒 / 正确 token 放 / 错 token 拒
    (dict(), "192.168.1.5", False),
    (dict(provided=TEST_TOKEN), "192.168.1.5", True),
    (dict(provided="wrong-token"), "192.168.1.5", False),
    # 环回豁免（默认开）：无 token 放行
    (dict(), "127.0.0.1", True),
    (dict(), "::1", True),
    # 豁免可禁用（smoke_test [5] 覆盖 403 分支的纯函数口径）
    (dict(exempt_loopback=False), "127.0.0.1", False),
    (dict(provided=TEST_TOKEN, exempt_loopback=False), "127.0.0.1", True),
    # auth_enabled=false 回退开关：全放行
    (dict(enabled=False), "192.168.1.5", True),
    (dict(enabled=False), "127.0.0.1", True),
    # 服务端无 token（expected 空）：非环回 fail closed，环回仍豁免
    (dict(expected=None), "192.168.1.5", False),
    (dict(expected=None), "127.0.0.1", True),
])
def test_auth_allows_matrix(kwargs, peer, allowed):
    kw = dict(expected=TEST_TOKEN, provided=None)
    kw.update(kwargs)
    assert auth_allows(kw["expected"], kw["provided"], peer,
                      enabled=kw.get("enabled", True),
                      exempt_loopback=kw.get("exempt_loopback", True)) is allowed


# ---------------- 中间件：HTTP 403 / 环回豁免 / 防伪造头 ----------------

async def test_no_token_non_loopback_403(app_state):
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/api/status")
    assert r.status_code == 403
    assert "token" in r.json()["error"]


async def test_correct_token_header_and_query_200(app_state):
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/api/status", headers={"X-LectureLens-Token": TEST_TOKEN})
        assert r.status_code == 200
        r2 = await c.get(f"/api/status?token={TEST_TOKEN}")   # ?token= 同源放行
    assert r2.status_code == 200


async def test_wrong_token_403(app_state):
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/api/status", headers={"X-LectureLens-Token": "wrong"})
    assert r.status_code == 403


@pytest.mark.parametrize("spoof_hdr", ["X-Forwarded-For", "X-Real-IP", "X-Client-IP"])
async def test_forwarded_headers_do_not_grant_exempt(app_state, spoof_hdr):
    """防伪造核心：伪造环回请求头绝不触发豁免（豁免只认 socket 对端地址）。"""
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/api/status", headers={spoof_hdr: "127.0.0.1"})
    assert r.status_code == 403


async def test_loopback_exempt_200(app_state):
    """§2.2-19「环回豁免 200」：对端 127.0.0.1 无 token 直接过。"""
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200


async def test_auth_disabled_allows_any(app_state):
    """auth_enabled=false（回退开关）：非环回无 token 也放行（即时键，热生效）。"""
    main.runtime["config"]["auth_enabled"] = False
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200


async def test_index_page_not_protected(app_state):
    """/ 不在 /api/* 与 /ws 范围：非环回无 token 可打开页面（粘贴 token 的入口）。"""
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.get("/")
    assert r.status_code == 200


async def test_env_disables_loopback_exempt(app_state, monkeypatch):
    """LECTURELENS_AUTH_NO_LOCAL_EXEMPT=1（smoke_test [5] 子进程口径）：
    本机对端也不再豁免，带 token 才放行。"""
    monkeypatch.setenv("LECTURELENS_AUTH_NO_LOCAL_EXEMPT", "1")
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.get("/api/status")
        assert r.status_code == 403
        r2 = await c.get("/api/status", headers={"X-LectureLens-Token": TEST_TOKEN})
    assert r2.status_code == 200


# ---------------- 中间件：WS query 参数放行 / 无 token 握手被拒 ----------------

def test_ws_with_token_accepted(app_state):
    """TestClient 对端（testclient）非环回：带 token 的 WS 握手正常建立。"""
    client = TestClient(main.app)
    with client.websocket_connect(f"/ws?token={TEST_TOKEN}"):
        pass          # 进入即握手成功；退出即正常断开（服务端走 Disconnect 分支）


def test_ws_without_token_rejected(app_state):
    """无 token WS 握手被拒：middleware accept 前发 websocket.close →
    TestClient 抛 WebSocketDisconnect（uvicorn 实机为 HTTP 403，smoke_test 断言非 101）。"""
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


# ---------------- 全键白名单 parametrize：非法值 400 且零污染 ----------------

INVALID_VALUE_BY_KEY = {
    "model": "gpt-5",
    "device": "tpu",
    "compute_type": "fp16",
    "vad": "webrtc",
    "lang": "xx",
    "streaming": "yes",
    "source": "spdif",
    "input_device": "mic",       # 类型非法（须非负 int 或 null）
    "translate_model": "gpt-4o",
    "auth_enabled": "yes",
    "first_run_done": "yes",     # p1-5：类型非法（须布尔）
    "floating_bar": "yes",       # p1-7：类型非法（须布尔）
    # p2-5（B4）：数值键用**越界值**（比类型错更有信息量——区间校验才是新增的那层）
    "silence_ms": 10,            # 低于下界 200
    "max_seg_s": 999,            # 高于上界 30
    "hard_cut_s": 1,             # 低于下界 5
    "backlog_drop": 1,           # 低于下界 2
    "backlog_keep": 99,          # 高于上界 20
    "fade_lines": 3,             # p2-7：枚举只有 0/1/2
    # p3-1：热键键名白名单（hotkeys.combo_problem 专项校验，不在 CONFIG_VALUE_CHOICES）
    "hotkeys_enabled": "yes",    # 类型非法（须布尔）
    "hotkey_pause": "ctrl+",     # 缺主键
    "hotkey_float": 123,         # 类型非法（须字符串）
    # p3-5：窗口版式记忆（整数像素；用越界/类型错两种更典型的错法）
    "window_w": 100,             # 低于下界 320
    "window_h": 999999,          # 高于上界 20000
    "window_x": 12.5,            # 非整数
    "window_y": "0",             # 类型错（字符串）
    "float_x": True,             # bool 是 int 子类，必须显式排除
    "float_y": -999999,          # 低于负向下界
}


@pytest.mark.parametrize("key", sorted(ALLOWED_CONFIG_KEYS))
async def test_every_key_rejects_invalid_value_400(app_state, key):
    """§2.2-19「全键白名单非法值 400 且 config.json 未改写」：逐键 parametrize，
    整体校验先行（条目4②）——runtime 与文件零污染。"""
    snapshot = dict(main.runtime["config"])
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.post("/api/config", json={key: INVALID_VALUE_BY_KEY[key]})
    assert r.status_code == 400, f"{key} 非法值未被拒绝: HTTP {r.status_code}"
    assert key in r.json()["error"]
    assert main.runtime["config"] == snapshot
    assert not main.CONFIG_PATH.exists()


@pytest.mark.parametrize("key", sorted(ALLOWED_CONFIG_KEYS))
async def test_every_key_accepts_current_value(app_state, key):
    """反向锁：每个白名单键喂 DEFAULT 当前值必须是合法 no-op（防白名单与
    默认值漂移——新增键漏进 CONFIG_VALUE_CHOICES 会在这里现形）。"""
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.post("/api/config", json={key: DEFAULT_CONFIG[key]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": {}}
    assert not main.CONFIG_PATH.exists()   # no-op 不落盘


async def test_auth_token_not_settable_via_api(app_state):
    """auth_token 是服务端管理键（不在 ALLOWED_CONFIG_KEYS）：POST 必须以
    未知配置键 400 拒绝，runtime 里的 token 原样。（尝试值运行期生成——源码
    不写 token 字面量。）"""
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.post("/api/config",
                         json={"auth_token": secrets.token_urlsafe(8)})
    assert r.status_code == 400
    assert "未知配置键" in r.json()["error"]
    assert main.runtime["config"]["auth_token"] == TEST_TOKEN
    assert not main.CONFIG_PATH.exists()


async def test_auth_enabled_hot_toggle(app_state):
    """POST auth_enabled=false（带 token）→ 下一请求即时免鉴权 + 持久化；
    再开回 true 后恢复拦截（回退开关全链路）。"""
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r0 = await c.get("/api/status")
        assert r0.status_code == 403
        r = await c.post("/api/config", json={"auth_enabled": False},
                         headers={"X-LectureLens-Token": TEST_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"auth_enabled": False}}
        r1 = await c.get("/api/status")            # 无 token：即时放行
        assert r1.status_code == 200
        saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["auth_enabled"] is False
        r2 = await c.post("/api/config", json={"auth_enabled": True})  # 关闭期间免鉴权
        assert r2.status_code == 200
        r3 = await c.get("/api/status")
    assert r3.status_code == 403                    # 重开后恢复拦截
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["auth_enabled"] is True


# ---------------- provision_token：首启生成 / 幂等 / 打印不落日志 ----------------

def test_provision_token_first_run_generates_prints_and_persists(caplog, capsys):
    with caplog.at_level(logging.INFO, logger="main"):
        token = provision_token()
    assert token and len(token) >= 20
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["auth_token"] == token
    assert token in capsys.readouterr().out         # 首启 print 给用户（计划口径）
    assert token not in caplog.text                 # 值不落日志（logging 出口）


def test_provision_token_random_not_constant():
    t1 = provision_token()
    main.CONFIG_PATH.unlink()
    t2 = provision_token()
    assert t1 and t2 and t1 != t2                   # 每次生成都随机


def test_provision_token_idempotent_keeps_file(capsys):
    tok = secrets.token_urlsafe(12)
    main.CONFIG_PATH.write_text(json.dumps({"model": "tiny", "auth_token": tok}),
                               encoding="utf-8")
    before = main.CONFIG_PATH.read_bytes()
    assert provision_token() == tok
    assert main.CONFIG_PATH.read_bytes() == before  # 已有 token：不重写文件
    assert tok not in capsys.readouterr().out      # 非 first-run 不打印值


def test_provision_token_preserves_other_keys():
    main.CONFIG_PATH.write_text(json.dumps({"model": "tiny", "vad": "energy"}),
                               encoding="utf-8")
    token = provision_token()
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved == {"model": "tiny", "vad": "energy", "auth_token": token}  # 只增补


def test_provision_token_corrupt_file_recovers():
    main.CONFIG_PATH.write_text("not json {{{", encoding="utf-8")
    token = provision_token()
    assert token
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["auth_token"] == token


def test_no_credentials_in_config_defaults():
    """条目19④：凭据（DeepSeek key）只从 env/.env 读——默认配置与白名单不得
    出现 key 类键（auth_token 是本服务自生成的访问凭据，属例外且不入白名单）。"""
    blob = (json.dumps(DEFAULT_CONFIG) + json.dumps(list(ALLOWED_CONFIG_KEYS))
            ).lower()
    assert "api_key" not in blob and "apikey" not in blob


# ---------------- 评审修复回归：① uvicorn proxy_headers ② GET 不出 token ----------------

async def test_config_get_strips_auth_token(app_state):
    """评审修复②：GET /api/config 与 /api/status 不得出现 auth_token（GET 侧曾
    原样返回 runtime['config']，生产态 token 明文可达环回豁免对端与持 token 的
    LAN 客户端；POST 侧封禁见 test_auth_token_not_settable_via_api）。"""
    async with asgi_client(peer=("127.0.0.1", 123)) as c:
        r = await c.get("/api/config")
        assert r.status_code == 200
        j = r.json()
        assert "auth_token" not in j
        assert "model" in j                       # 其余配置键照常可用
        s = await c.get("/api/status")
    assert "auth_token" not in s.json()
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r2 = await c.get("/api/config", headers={"X-LectureLens-Token": TEST_TOKEN})
    assert r2.status_code == 200
    assert "auth_token" not in r2.json()


def test_uvicorn_run_disables_proxy_headers(app_state, monkeypatch):
    """评审修复①回归锁：uvicorn.run 必须显式 proxy_headers=False。uvicorn 默认
    True 安装 ProxyHeadersMiddleware（config.py:225/543），把受信对端
    （FORWARDED_ALLOW_IPS，缺省 "127.0.0.1,::1"，config.py:363）的
    X-Forwarded-For 改写进 scope['client']——环回带 XFF 被误拒（403）、
    FORWARDED_ALLOW_IPS=* 下 LAN 对端伪造 XFF:127.0.0.1 绕过豁免（均实测复现）。
    真实 uvicorn 链路由本机裸 socket harness 四场景验证（见 change 记录）。"""
    from types import SimpleNamespace
    captured: dict = {}
    saved_ctx = main.app.router.lifespan_context

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main, "parse_args", lambda: SimpleNamespace(
        list_devices=False, port=8799, gpu=False, lang=None, open_browser=False))
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(main, "uvicorn", SimpleNamespace(run=fake_run))
    try:
        main.main()
    finally:
        # main() 会把真 lifespan 挂到 app——还原，防影响后续测试
        main.app.router.lifespan_context = saved_ctx
    assert captured.get("proxy_headers") is False
    assert captured.get("host") == "0.0.0.0"
    assert captured.get("port") == 8799
