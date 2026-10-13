# -*- coding: utf-8 -*-
"""tests/test_lan_qr.py — §7 手机扫码同屏（LAN 绑定决策 / 主网卡探测 / 配对二维码）。

验收口径（docs/notes-batch.md §7 + 「共同验收」）：
- config 新键 lan_enabled：布尔枚举（DEFAULT false），写盘即存；**改它不换绑**——
  监听地址要重启才换，端点必须如实回 restart_required（UI 不许写「立即生效」）；
- 绑定决策纯逻辑：true → 0.0.0.0，false → 127.0.0.1（端口透传给调用方）；
- pick_port「优先 8787，占用则实际端口」：真占一个端口再调，必须换一个；
- 主网卡探测：UDP connect（不发包）——探测失败/探测到环回或链路本地地址都要
  如实回落，绝不把 127.0.0.1 / 169.254.x.x 写进配对 URL；
- /api/lan-qr：**仅 loopback 对端**（非本机 403，且判据是 socket 对端而非中间件的
  豁免开关）；返回 {enabled, active, restart_required, url, svg, ...}，url 形如
  http://<LAN-IP>:<port>/#t=<token>；token 为空时先 provision 再编二维码；
- status.ws_clients = len(Hub.clients)。

HTTP 走 httpx.ASGITransport（可注入对端地址）；CONFIG_PATH 由 conftest 重定向 tmp_path，
token 全部运行期 secrets 生成——源码不写任何可用凭据字面量。"""
import contextlib
import json
import socket
import secrets
from contextlib import asynccontextmanager

import asyncio
import time
import types

import httpx
import pytest
from fastapi import WebSocketDisconnect

import lan
import main
from main import DEFAULT_CONFIG

TEST_TOKEN = secrets.token_urlsafe(16)   # 运行期生成，非源码字面量


class _FakeWS:
    """最小 WebSocket 替身：驱动真实 ws_endpoint（登记/分类/清理/被踢），不起网络。

    只实现端点真正用到的那几面（accept / receive_text / close / client.host）——
    于是「对端是环回还是局域网」这条分类规则能被精确断言，而不用真开一条手机连接。"""

    def __init__(self, host: str):
        self.client = types.SimpleNamespace(host=host)
        self.closed: list[int] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        item = await self._q.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)

    def drop(self) -> None:
        """模拟对端断开（ws_endpoint 捕获 WebSocketDisconnect 后走 finally 清理）。"""
        self._q.put_nowait(WebSocketDisconnect(1000))


async def _until(pred, timeout: float = 2.0) -> bool:
    """等一个集合状态收敛（WS 端点是任务，登记/清理是异步的）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def _cancel_all(tasks: list) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


@pytest.fixture
def app_state(monkeypatch):
    """复位 runtime/status/hub 的模块级可变状态 + 注入测试 token；teardown 逐项还原
    （config 内容单独深拷贝还原——浅拷 runtime 会把测试值留在同一个 dict 里）。"""
    monkeypatch.delenv("LECTURELENS_AUTH_NO_LOCAL_EXEMPT", raising=False)
    saved_runtime = dict(main.runtime)
    saved_cfg = dict(main.runtime["config"])
    saved_clients = set(main.hub.clients)
    main.runtime["config"].clear()
    main.runtime["config"].update(DEFAULT_CONFIG)
    main.runtime["config"]["auth_token"] = TEST_TOKEN
    main.runtime["reloading"] = False
    main.runtime["bind"] = None
    main.hub.clients.clear()
    yield main
    main.runtime.clear()
    main.runtime.update(saved_runtime)
    main.runtime["config"].clear()
    main.runtime["config"].update(saved_cfg)
    main.hub.clients.clear()
    main.hub.clients.update(saved_clients)


@asynccontextmanager
async def asgi_client(peer=("127.0.0.1", 123), base="http://test"):
    transport = httpx.ASGITransport(app=main.app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url=base) as c:
        yield c


def with_token():
    return {"X-LectureLens-Token": TEST_TOKEN}


# ---------------- 1. 绑定决策（纯逻辑） ----------------

def test_lan_bind_off_is_loopback_only():
    """默认（false/缺键）→ 只听 127.0.0.1：服务不会因为"在跑"就暴露在同网段里。
    关闭态端口**原样透传**（0 = 内核挑空闲端口，桌面模式就这么用的）。"""
    assert lan.lan_bind({"lan_enabled": False}, 8787) == ("127.0.0.1", 8787)
    assert lan.lan_bind({}, 8787) == ("127.0.0.1", 8787)          # 缺键 = 关闭
    assert lan.lan_bind(None, 8799) == ("127.0.0.1", 8799)        # 空配置不炸
    assert lan.lan_bind({"lan_enabled": False}, 0) == ("127.0.0.1", 0)
    assert lan.lan_bind({}, None) == ("127.0.0.1", 8787)          # 端口缺省 → 首选 8787


def test_lan_bind_on_is_all_interfaces():
    """true → 0.0.0.0（所有网卡），端口原样透传（main 传 --port，desktop 传 8787）。"""
    assert lan.lan_bind({"lan_enabled": True}, 8787) == ("0.0.0.0", 8787)
    assert lan.lan_bind({"lan_enabled": True}, 8799) == ("0.0.0.0", 8799)
    # 端口给 0/缺省 → 用首选 8787（不是真绑 0——8787 才是文档里的对外地址）
    assert lan.lan_bind({"lan_enabled": True}, 0) == ("0.0.0.0", 8787)


def test_is_lan_host():
    """「是否对外」的判据：0.0.0.0/具体网卡 = 对外；环回/解析不了 = 不算（fail closed）。"""
    assert lan.is_lan_host("0.0.0.0") is True
    assert lan.is_lan_host("192.168.1.5") is True
    assert lan.is_lan_host("127.0.0.1") is False
    assert lan.is_lan_host("::1") is False
    assert lan.is_lan_host("localhost") is False   # 域名不猜
    assert lan.is_lan_host(None) is False
    assert lan.is_lan_host("") is False


# ---------------- 2. port 选择（真 bind 探测） ----------------

def test_pick_port_prefers_requested_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()          # 关掉后立即空闲（没 listen 没连接 → 无 TIME_WAIT）
    assert lan.pick_port("127.0.0.1", free) == free


def test_pick_port_falls_back_when_occupied():
    """§7「优先 8787，占用则实际端口」：占着不放 → 必须换一个能用端口，不是报错。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        busy = holder.getsockname()[1]
        assert lan.can_bind("127.0.0.1", busy) is False   # 独占语义（不设 SO_REUSEADDR）
        got = lan.pick_port("127.0.0.1", busy)
        assert got != busy and got > 0
        # 换来的端口真的能用（不是随手编一个号）
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", got))
        finally:
            probe.close()
    finally:
        holder.close()


def test_pick_port_zero_means_kernel_choice():
    """0 = 交给内核（desktop 关闭态就是 port 0 随机），不要自作主张解析成具体端口。"""
    assert lan.pick_port("127.0.0.1", 0) == 0


# ---------------- 3. 主网卡地址（UDP 探测，不发包） ----------------

def test_ip_usable_rejects_unreachable_candidates():
    for bad in ("127.0.0.1", "127.1.2.3", "0.0.0.0", "169.254.3.4", "224.0.0.1",
                "255.255.255.255", "not-an-ip", "", None, "fe80::1"):
        assert lan.ip_usable(bad) is False, bad


def test_ip_usable_accepts_real_lan_and_public():
    # 私有段（宿舍/校园 WiFi 常见）与公网段（eduroam 那类直接给公网地址）都放行——
    # 那正是手机要连的地址；本模块不发任何请求，「拒绝私有地址」那套 SSRF 纪律不在此。
    for good in ("192.168.1.23", "10.0.0.7", "172.16.9.9", "8.8.8.8"):
        assert lan.ip_usable(good) is True, good


class _FakeSock:
    """假 UDP socket：getsockname 返回预设地址；connect 可预设为抛 OSError。"""

    def __init__(self, addr, boom=False):
        self._addr, self._boom = addr, boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, _t):
        pass

    def connect(self, _target):
        if self._boom:
            raise OSError("network unreachable")

    def getsockname(self):
        return (self._addr, 12345)


def test_primary_lan_ip_skips_bad_candidates(monkeypatch):
    """探测到环回/链路本地不能直接用——继续试下一个目标（否则二维码里会是 127.0.0.1）。"""
    seq = [_FakeSock("127.0.0.1"), _FakeSock("169.254.9.9"), _FakeSock("192.168.5.6")]
    monkeypatch.setattr(lan.socket, "socket", lambda *a, **kw: seq.pop(0))
    assert lan.primary_lan_ip((("1.1.1.1", 53), ("2.2.2.2", 53), ("3.3.3.3", 53))) \
        == "192.168.5.6"


def test_primary_lan_ip_returns_none_when_unroutable(monkeypatch):
    monkeypatch.setattr(lan.socket, "socket",
                        lambda *a, **kw: _FakeSock("0.0.0.0", boom=True))
    assert lan.primary_lan_ip((("8.8.8.8", 53),)) is None
    assert lan.primary_lan_ip(()) is None      # 空目标表（纯逻辑兜底）


def test_primary_lan_ip_real_call_is_never_loopback():
    """真机调用：结果要么 None（没有默认路由），要么是个能写进 URL 的地址。"""
    ip = lan.primary_lan_ip()
    assert ip is None or lan.ip_usable(ip), ip


# ---------------- 4. 配对 URL 与二维码 ----------------

def test_lan_url_shape():
    """§7 明文：url=http://<LAN-IP>:<port>/#t=<token>（token 走 hash 段）。"""
    url = lan.lan_url("192.168.1.23", 8787, "AbC-123_xyz")
    assert url == "http://192.168.1.23:8787/#t=AbC-123_xyz"
    # token 为空：不编造 #t=（空凭据的二维码等于让用户白扫一次）
    assert lan.lan_url("192.168.1.23", 8787, None) == "http://192.168.1.23:8787/"
    assert lan.lan_url("10.0.0.7", 8799, "") == "http://10.0.0.7:8799/"


def test_qr_svg_is_real_svg_with_viewbox():
    svg = lan.qr_svg("http://192.168.1.23:8787/#t=abc")
    assert svg.startswith("<?xml") and "<svg" in svg and "<path" in svg
    assert "viewBox" in svg          # 有 viewBox 才能随 CSS 缩放（面板里自适应宽度）
    assert svg.endswith("</svg>")
    assert "192.168" not in svg      # 内容编进了图形，不会以明文出现在 SVG 里
    assert lan.qr_svg("http://192.168.1.23:8787/#t=abc") == svg   # 同输入同输出


# ---------------- 5. 绑定事实 & 状态行（UI 文案的事实来源） ----------------

def test_bind_state_unbound_is_off(app_state):
    st = main.lan_bind_state()
    assert st == {"enabled": False, "active": False, "restart_required": False,
                  "exposed_while_disabled": False, "host": None, "port": None}


def test_bind_state_on_and_active(app_state):
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    st = main.lan_bind_state()
    assert st["enabled"] is True and st["active"] is True
    assert st["restart_required"] is False and st["host"] == "0.0.0.0"
    assert main.lan_message(st, "192.168.1.23", 8787, True).startswith("手机与电脑连同一 WiFi")


def test_bind_state_on_but_not_restarted(app_state):
    """刚打开开关、还没重启：必须说「重启后生效」，绝不能说「已开启、扫这个码」。"""
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("127.0.0.1", 8799)
    st = main.lan_bind_state()
    assert st["restart_required"] is True and st["active"] is False
    assert "重启" in main.lan_message(st, None, 8799, True)


def test_bind_state_off_but_still_exposed(app_state):
    """配置说关、服务还开着（关掉开关但没重启）：这是**风险态**，必须显眼说明。"""
    app_state.runtime["config"]["lan_enabled"] = False
    main.set_bind("0.0.0.0", 8787)
    st = main.lan_bind_state()
    assert st["exposed_while_disabled"] is True and st["active"] is True
    assert st["restart_required"] is True
    msg = main.lan_message(st, "192.168.1.23", 8787, True)
    assert "仍在监听 LAN" in msg and "重启" in msg


def test_message_flags_missing_lan_ip_and_open_auth(app_state):
    st = {"enabled": True, "active": True, "restart_required": False,
          "exposed_while_disabled": False, "host": "0.0.0.0", "port": 8787}
    assert "没探测到局域网地址" in main.lan_message(st, None, 8787, True)
    assert "任何人都能看字幕" in main.lan_message(st, "192.168.1.23", 8787, False)


# ---------------- 6. /api/lan-qr 端点 ----------------

async def test_lan_qr_rejects_non_loopback(app_state):
    """非本机对端 → 403（判据是 socket 对端；带合法 token 也不放行——防 LAN 侧取走凭据）。"""
    main.set_bind("0.0.0.0", 8787)
    async with asgi_client(peer=("192.168.1.50", 5555)) as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False and "仅本机" in body["error"]
    assert "token" in body["error"]      # 拒绝理由说清是「内含 token」
    # 403 里绝不许回带 url/svg（不然等于把 token 递出去）
    assert "url" not in body and "svg" not in body


async def test_lan_qr_loopback_ok_with_qr(app_state, monkeypatch):
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["active"] is True
    assert body["restart_required"] is False and body["exposed_while_disabled"] is False
    assert body["url"] == f"http://192.168.1.23:8787/#t={TEST_TOKEN}"
    assert body["svg"].startswith("<?xml") and "<svg" in body["svg"]
    assert body["port"] == 8787 and body["ip"] == "192.168.1.23"
    assert body["auth_enabled"] is True


async def test_lan_qr_port_comes_from_live_scope(app_state, monkeypatch):
    """URL 端口取**这条连接的实际监听端口**（scope['server']）——8787 被占换端口后
    二维码里不能还写 8787。"""
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8790)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "10.0.0.7")
    async with asgi_client(base="http://127.0.0.1:9123") as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    assert r.json()["port"] == 9123
    assert r.json()["url"] == f"http://10.0.0.7:9123/#t={TEST_TOKEN}"


async def test_lan_qr_disabled_gives_no_qr(app_state, monkeypatch):
    """关着的时候不给二维码/URL，但把「还差什么」讲清楚（不是空对象式的沉默）。"""
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["active"] is False
    assert body["url"] is None and body["svg"] is None
    assert "未开启" in body["message"]


async def test_lan_qr_not_restarted_yet(app_state, monkeypatch):
    """开了但没重启：restart_required=true 且**不给二维码**（扫了也连不上）。"""
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("127.0.0.1", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    body = r.json()
    assert body["enabled"] is True and body["active"] is False
    assert body["restart_required"] is True
    assert body["url"] is None and body["svg"] is None
    assert "重启" in body["message"]


async def test_lan_qr_no_lan_ip(app_state, monkeypatch):
    """开了、也对外了，但探测不到局域网地址：给 URL 也没意义，如实说。"""
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: None)
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr", headers=with_token())
    body = r.json()
    assert body["url"] is None and body["svg"] is None
    assert "没探测到局域网地址" in body["message"]


async def test_lan_qr_check_ignores_auth_exempt_switch(app_state, monkeypatch):
    """「仅本机」判据是 socket 对端，不是中间件的豁免开关：smoke 里
    LECTURELENS_AUTH_NO_LOCAL_EXEMPT=1（环回也要 token）时，本机请求仍必须 200
    ——否则本机面板自己就取不到二维码了。"""
    monkeypatch.setenv("LECTURELENS_AUTH_NO_LOCAL_EXEMPT", "1")
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    async with asgi_client() as c:
        assert (await c.get("/api/lan-qr")).status_code == 403        # 无 token 被中间件拦
        r = await c.get("/api/lan-qr", headers=with_token())          # 带 token → 端点放行
    assert r.status_code == 200 and r.json()["svg"]


async def test_lan_qr_provisions_token_when_missing(app_state, monkeypatch):
    """§7⑤：auth_token 为空 → 先 provision 再编二维码（不然手机扫到的 URL 没有凭据）。"""
    app_state.runtime["config"]["auth_token"] = None
    app_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr")
    assert r.status_code == 200
    body = r.json()
    token = app_state.runtime["config"]["auth_token"]
    assert token and body["url"] == f"http://192.168.1.23:8787/#t={token}"
    # provision 的写盘落在 tmp config.json（conftest 重定向），不是仓库真文件
    on_disk = json.loads(app_state.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["auth_token"] == token


# ---------------- 7. config 键 + status.ws_clients ----------------

async def test_lan_enabled_flag_round_trip(app_state):
    """POST → runtime + config.json 落地；**且不改 bind**（重启才换绑）。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lan_enabled": True}, headers=with_token())
    assert r.status_code == 200 and r.json() == {"ok": True, "changed": {"lan_enabled": True}}
    assert app_state.runtime["config"]["lan_enabled"] is True
    saved = json.loads(app_state.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["lan_enabled"] is True
    # 写配置不等于换绑：监听地址没动 → 如实回 restart_required
    assert main.lan_bind_state()["restart_required"] is True


async def test_ws_clients_counts_only_lan_peers(app_state):
    """§7 + 评审2①：status.ws_clients = **非环回**对端的 WS 数（手机/局域网设备）。

    数 Hub.clients（含本机自己的窗口）会让面板的「已连接 N 台设备」恒 ≥1——桌面窗口、
    浏览器标签、悬浮条都连 WS，于是永远区分不出「手机连上了」和「只有我自己的窗口」。
    这里用假 WS 直接驱动 ws_endpoint（分类依据是真实的 ws.client.host），两条分支都钉死：
    非环回计入 / 环回不计入；断开后两边都清空（不留幽灵设备数）。
    类型同时钉死是 **int**（不是 bool——`isinstance(True, int)` 为真，面板要做加减）。"""
    lan_ws, lo_ws = _FakeWS("192.168.1.50"), _FakeWS("127.0.0.1")
    tasks = [asyncio.create_task(main.ws_endpoint(ws)) for ws in (lan_ws, lo_ws)]
    try:
        assert await _until(lambda: len(main.hub.clients) == 2), "端点没登记连接"
        assert len(main.hub.lan_clients) == 1, "分类错了：只有非环回对端才算设备"
        async with asgi_client() as c:
            v = (await c.get("/api/status")).json()["ws_clients"]
            assert isinstance(v, int) and not isinstance(v, bool) and v == 1
        lan_ws.drop()          # 手机走人 → 设备数回落
        assert await _until(lambda: len(main.hub.lan_clients) == 0)
        async with asgi_client() as c:
            assert (await c.get("/api/status")).json()["ws_clients"] == 0
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


async def test_kick_ws_clients_closes_and_clears(app_state):
    """评审2④b 的机制层：kick_ws_clients（token 轮换时调用）关掉所有在线连接并清空集合
    ——否则轮换只挡住**新**连接，「旧设备立刻失效」不成立（评审实测：轮换后旧 socket
    仍在收字幕）。"""
    a, b = _FakeWS("192.168.1.50"), _FakeWS("127.0.0.1")
    tasks = [asyncio.create_task(main.ws_endpoint(ws)) for ws in (a, b)]
    try:
        assert await _until(lambda: len(main.hub.clients) == 2)
        n = await main.kick_ws_clients()
        assert n == 2, f"应断开 2 条，实际 {n}"
        assert a.closed and b.closed, "连接没被 close()"
        assert not main.hub.clients and not main.hub.lan_clients
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


# ---------------- 8. hash 里的 token（§7② 的 Python 侧同款口径） ----------------

@pytest.mark.parametrize("raw,expected", [
    # 整条配对 URL（服务端刚给出的那个形状）
    ("http://192.168.1.23:8787/#t=AbC-123_xyz", "AbC-123_xyz"),
    # 手机端拿到的 location.hash（带 #）与裸片段
    ("#t=AbC-123_xyz", "AbC-123_xyz"),
    ("t=AbC-123_xyz", "AbC-123_xyz"),
    # hash 里 t= 不是第一个参数（前端正则 /(?:^#|&)t=/ 同款宽松）
    ("#a=1&t=xyz", "xyz"),
    ("a=1&t=xyz&b=2", "xyz"),
    # 百分号编码（token 恒为 URL 安全字符，这里是防御性兜底，与前端 decodeURIComponent 对齐）
    ("#t=a%2Db", "a-b"),
    # 没有 token 的各种形态 → 空串（绝不瞎编一个出来）
    ("http://192.168.1.23:8787/", ""),
    ("#x=1", ""),
    ("#t=", ""),
    ("", ""),
    (None, ""),
])
def test_token_from_hash_matches_frontend_contract(raw, expected):
    """§7②：从 URL/片段里取 `#t=` 的 token —— Python 侧实现（前端那份在
    index.html，tests/test_lan_ui.py 钉住它的正则与「先读后清」顺序）。"""
    assert lan.token_from_hash(raw) == expected


def test_token_from_hash_round_trips_with_lan_url():
    """自己构造的 URL，自己必须能解回来（构造与解析是同一口径的两端）。"""
    url = lan.lan_url("10.0.0.7", 8799, "Kcwe-w816_ulOEt3EX86SMYffzrp2u3Hpm")
    assert url == "http://10.0.0.7:8799/#t=Kcwe-w816_ulOEt3EX86SMYffzrp2u3Hpm"
    assert lan.token_from_hash(url) == "Kcwe-w816_ulOEt3EX86SMYffzrp2u3Hpm"
    # token 为空时不编造 #t=（空凭据的二维码等于让用户白扫一次）
    assert lan.token_from_hash(lan.lan_url("10.0.0.7", 8799, None)) == ""


@pytest.mark.parametrize("peer,is_local", [
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("192.168.1.50", False), ("10.131.37.5", False), ("::ffff:192.168.1.50", False),
    ("0.0.0.0", False), ("", False), (None, False), ("not-an-ip", False),
])
def test_loopback_gate_matrix_for_qr(peer, is_local):
    """§7③ 的判据本体：只有环回对端能读配对二维码（码 = 凭据）。

    这里的矩阵是**端点行为的判据**（main.is_loopback_peer），端点层的行为断言在
    上面 test_lan_qr_rejects_non_loopback / smoke 的「LAN IP + 合法 token 仍 403」。
    注意 0.0.0.0（未指定地址）不是环回对端——真实 socket 不会给出它，但判据若把它
    当成本机就是一处 fail-open，故钉住。"""
    assert main.is_loopback_peer(peer) is is_local


@pytest.mark.parametrize("value,stored,effective", [
    (True, True, True),
    (False, False, False),
    # 枚举表 (False, True) 的等价类：Python 里 1 == True / 0 == False，故 1/0 会被
    # 当成同一个合法取值。落盘的是原值（与 auth_enabled 等既有布尔键同款行为），
    # 而**生效判据恒走 bool()**（DEFAULT_CONFIG 注释 + lan_bind/lan_bind_state），
    # 所以语义正确；这里把「等价类」与「生效值」两件事都钉住。
    (1, 1, True),
    (0, 0, False),
])
async def test_lan_enabled_value_equivalence(app_state, value, stored, effective):
    """lan_enabled 取值校验：真布尔与 1/0 等价类都收，落盘原值、生效走 bool()。"""
    # 先置成反值：否则「与现值相同」会走 no-op 分支（不落盘），测不到写盘路径
    app_state.runtime["config"]["lan_enabled"] = not effective
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lan_enabled": value}, headers=with_token())
    assert r.status_code == 200 and r.json()["ok"] is True
    saved = json.loads(app_state.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["lan_enabled"] == stored
    assert bool(saved["lan_enabled"]) is effective
    # 生效判据：绑定决策与端点状态都按 bool() 读
    host, _port = lan.lan_bind({"lan_enabled": stored}, 8787)
    assert (host == "0.0.0.0") is effective
    assert main.lan_bind_state()["enabled"] is effective


async def test_lan_enabled_null_is_noop(app_state):
    """null 是 no-op 占位（与其它布尔键同款：前端下拉/未填的占位语义），
    不能被当成「关闭」写进配置——否则一次误发的 null 会悄悄改掉开关。"""
    app_state.runtime["config"]["lan_enabled"] = True
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lan_enabled": None}, headers=with_token())
    assert r.status_code == 200 and r.json() == {"ok": True, "changed": {}}
    assert app_state.runtime["config"]["lan_enabled"] is True
    assert not app_state.CONFIG_PATH.exists()   # no-op 不落盘
