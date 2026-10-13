# -*- coding: utf-8 -*-
"""tests/test_lan_ui.py — §7 手机同屏的**前端契约**（离线秒级：结构 + 服务端发出的页面）。

交付面（docs/notes-batch.md §7）：字幕页工具条「📱 手机同屏」→ 面板（二维码 + URL +
「已连接 N 台设备」+ LAN 开关 + 防火墙三步排查 + 仅同一 WiFi 说明）；手机端读
location.hash 的 #t= 存 localStorage（键 = 既有 TOKEN_KEY）并清 hash；设置页同键开关；
隐私透明页补「扫码配对」。

如实说明（§2.1 纪律）：浏览器交互不进自动化。本文件做两件事——
① 对 index.html 的**结构/文案/接线断言**（含「不许写立即生效」这类口径）；
② 对**服务端实际发出的页面**的断言（防「源码里有、送出去的没有」），以及
   前端读的字段与 /api/lan-qr 响应键的**子集核对**（防两端字段名漂移）。
真机/真浏览器行为已在真 Chrome 里逐项跑过（记录见实施报告）：面板三态渲染（未开启 /
重启后生效 / 可用）、开关 → toast + 重启提示 + config 落盘、设置页卡状态行、
`#t=` 存 token 并清 hash、经 LAN IP 打开面板得到「仅本机」说明、控制台零 error。
"""
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
PRIVACY = (WEB / "privacy.html").read_text(encoding="utf-8")

#: 运行期生成的假 token（源码不写字面量，Mimosa 约束 / 仓库既有纪律）
TEST_TOKEN = secrets.token_urlsafe(16)

#: 工具条区块（只认 #toolbar 里的控件；工具条内含多个 <div>，故切到下一个顶层区块）
TOOLBAR = HTML.split('<div id="toolbar">', 1)[1].split('<div id="loadwrap">', 1)[0]
#: §7 面板区块（`<div id="lanMask">` → 下一个弹窗）：面板自己的文案断言只看这一块
LAN_PANEL = HTML.split('<div id="lanMask"', 1)[1].split('<div id="trashMask"', 1)[0]
#: §7 面板 JS 区块（常量到下一个功能段）：数据接线断言只看这一块
LAN_JS = HTML.split("// —— §7 手机同屏（notes-batch）", 1)[1].split(
    "// —— p2-4 模型管理器", 1)[0]
#: 前端**自己造**的字段（不是服务端字段）：fetchLanQr 的失败态错误对象
LAN_FRONTEND_LOCAL_FIELDS = {"error"}
#: 面板里公开的必须有的元素（结构断言用；缺一个都算面板不完整）
PANEL_IDS = ("lanMask", "lanQr", "lanUrl", "lanCopy", "lanChk", "lanClients",
             "lanRestart", "lanHelpWrap", "lanRefresh")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------- 1. 工具条按钮与面板结构 ----------------

def test_toolbar_has_lan_button():
    """工具条必须有「📱 手机同屏」入口（面板的唯一打开方式）。"""
    assert 'id="btnLan"' in TOOLBAR, "工具条里没有手机同屏按钮"
    line = [l for l in TOOLBAR.splitlines() if 'id="btnLan"' in l][0]
    assert "📱 手机同屏" in line and "title=" in line, "按钮文案/提示缺失"


def test_panel_elements_present():
    """面板 = 二维码 + URL + 已连接设备数 + 开关 + 排查指引（§7 逐项交付）。"""
    for el in PANEL_IDS:
        assert f'id="{el}"' in HTML, f"面板缺元素 #{el}"
    assert 'id="setLanChk"' in HTML, "设置页没有同键开关"
    assert 'id="setLanState"' in HTML, "设置页没有状态行"


def test_panel_registered_in_modal_whitelist():
    """面板走统一弹窗（焦点/inert/Escape）——必须进 MASKS 白名单，否则背景 inert 会
    把面板自己也关在门外（p3-5 的既有纪律）。"""
    masks = HTML.split("const MASKS = [", 1)[1].split("];", 1)[0]
    assert "'lanMask'" in masks, "lanMask 不在 MASKS 白名单里"


def test_panel_copy_has_three_firewall_steps_and_wifi_scope():
    """③ 防火墙三步排查 + 「仅同一 WiFi」说明（文案要点逐条在）。"""
    assert LAN_PANEL.count("<li>") == 3, "排查步骤不是三步"
    for kw in ("防火墙", "专用网络", "同一个 WiFi"):
        assert kw in LAN_PANEL, f"排查指引缺关键词：{kw}"
    assert "仅同一 WiFi 可用" in LAN_PANEL, "缺「仅同一 WiFi」的范围说明"
    assert "别把截图外传" in LAN_PANEL, "缺「二维码含 token、勿外传」的风险提示"


def test_panel_copy_never_promises_immediate_effect():
    """**硬口径**：手机同屏改键要重启才换绑——面板文案不许出现「立即生效」，
    且必须明说重启（面板 HTML 与它的 JS 两处都算）。"""
    for block, name in ((LAN_PANEL, "面板 HTML"), (LAN_JS, "面板 JS")):
        assert "立即生效" not in block, f"{name} 里还在承诺立即生效（事实是重启才换绑）"
    # 静态 HTML 里「重启」只出现一次（.lanNote 的口径说明）；其余三处是 JS 动态文案：
    # 开启 toast / 关闭 toast / 重启提示行 / 无码占位语——所以 JS 侧要求更多次。
    assert LAN_PANEL.count("重启") >= 1, "面板静态文案没提「要重启」"
    assert LAN_JS.count("重启") >= 3, "面板的动态文案（toast/提示行）没把「要重启」说透"


# ---------------- 2. 数据接线（前端只呈现事实，不自己编状态） ----------------

def test_panel_reads_lan_qr_without_token_prompt():
    """面板取数走 /api/lan-qr，且**不用 jfetch**：jfetch 见 403 会弹「粘贴 token」框，
    而本端点对非本机对端的 403 是设计如此——弹框只会把人绕进死循环。"""
    assert "fetch(apiURL('/api/lan-qr'))" in LAN_JS
    fn = LAN_JS.split("async function fetchLanQr()", 1)[1].split("\n}", 1)[0]
    assert "jfetch" not in fn, "fetchLanQr 走了 jfetch（403 会弹 token 输入框）"
    assert "403" in fn and "only-local" in fn, "403 没有单独处理"


def test_clients_line_driven_by_status_ws_clients():
    """「已连接 N 台设备」的数据源 = status.ws_clients（2s 轮询刷新）。"""
    assert "ws_clients" in LAN_JS, "面板没有读 ws_clients"
    assert "syncLanClients(status_.ws_clients)" in HTML, "pollStatus 没接上设备数刷新"


def test_toggle_writes_same_config_key_and_says_restart():
    """开关写的是 config.lan_enabled（与设置页同键），且 toast 如实说「重启后生效」。"""
    fn = LAN_JS.split("async function lanSetEnabled(want)", 1)[1].split("\n}", 1)[0]
    assert "lan_enabled: !!want" in fn, "开关没写 lan_enabled 键"
    assert "/api/config" in fn
    assert fn.count("重启") >= 2, "开启/关闭两句话都没说清要重启"
    assert "立即生效" not in fn


def test_settings_card_uses_server_facts():
    """设置页状态行取自 /api/lan-qr 的 message（服务端事实），不回填本地猜测值；
    回填控件走 SETCFG.lan_enabled（同键）。"""
    assert "$('setLanChk').checked = !!SETCFG.lan_enabled;" in HTML, "设置页开关没回填"
    assert "$('setLanChk').onchange" in HTML and "lanSetEnabled(" in HTML
    fn = LAN_JS.split("async function refreshSetLanCard()", 1)[1].split("\n}", 1)[0]
    assert "st.textContent = (d.message || '')" in fn, "状态行没走服务端 message"
    assert "'only-local'" in fn, "手机侧打开设置页时没有降级文案"


@pytest.mark.asyncio
async def test_frontend_fields_are_subset_of_api_payload(monkeypatch):
    """前端读的每个 `d.<字段>` 都必须真的在 /api/lan-qr 的响应里（防两端字段名漂移）。

    本段 JS 里的 `d` 一律指响应体（lanSetQr 内的临时元素刻意不叫 d，注释里写了原因）。"""
    saved_cfg = dict(main.runtime["config"])
    saved_bind = main.runtime.get("bind")
    main.runtime["config"].update({"lan_enabled": True, "auth_token": TEST_TOKEN})
    main.set_bind("0.0.0.0", 8787)
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    try:
        async with asgi_client() as c:
            payload = (await c.get("/api/lan-qr")).json()   # 环回对端 → 200
    finally:
        main.runtime["config"].clear()
        main.runtime["config"].update(saved_cfg)
        main.runtime["bind"] = saved_bind
    assert payload.get("url"), "前置条件不成立：本机应拿到可用的二维码"
    used = set(re.findall(r"\bd\.([a-z_]+)\b", LAN_JS))
    assert used, "没抓到前端读取的字段（切分标记失效？）"
    missing = used - set(payload) - LAN_FRONTEND_LOCAL_FIELDS
    assert not missing, f"前端读了服务端没给的字段: {sorted(missing)}"


# ---------------- 3. 手机端：hash 里的 token ----------------

def test_mobile_hash_token_flow():
    """§7②：读 location.hash 的 #t= → 存既有 TOKEN_KEY → replaceState 清 hash。

    顺序也要对：先读/存再清（反了就等于没读到）。"""
    assert r"/(?:^#|&)t=([^&]+)/" in HTML, "没有解析 #t= 的正则"
    assert "localStorage.setItem(TOKEN_KEY, hashToken)" in HTML, "没存进既有 token 键"
    assert "history.replaceState(null, '', location.pathname + location.search)" in HTML, \
        "没清 hash（地址栏会留着 token）"
    i_read = HTML.index("hashToken || qsToken || localStorage.getItem(TOKEN_KEY)")
    i_clear = HTML.index("if (hashToken) history.replaceState")
    assert i_read < i_clear, "先清后读（token 会丢）"
    # 优先级：hash 里的（刚扫码来的）比 localStorage 里的旧值优先
    assert "LL_TOKEN = hashToken || qsToken" in HTML


# ---------------- 4. 服务端真正发出的页面 ----------------

@pytest.mark.asyncio
async def test_served_page_contains_lan_panel():
    """防「源码里有、送出去的没有」：真实 GET / 的响应体里必须带面板与接线标记。"""
    async with asgi_client() as c:
        r = await c.get("/")
    assert r.status_code == 200
    page = r.text
    for marker in ("btnLan", "lanMask", "lanQr", "setLanChk", "/api/lan-qr",
                   "ws_clients", "#t=", "手机同屏"):
        assert marker in page, f"服务端发出的页面缺标记：{marker}"


@pytest.mark.asyncio
async def test_served_privacy_page_documents_pairing():
    """§7④：隐私透明页要写清「扫码配对」与 lan_enabled 开关（服务端发出的版本为准）。"""
    async with asgi_client() as c:
        r = await c.get("/privacy")
    assert r.status_code == 200
    page = r.text
    for marker in ("扫码配对", "lan_enabled", "默认关闭", "明文", "token", "#t="):
        assert marker in page, f"隐私页缺说明：{marker}"
    assert "别外传截图" in page, "隐私页没提醒二维码/地址含 token、勿外传"
