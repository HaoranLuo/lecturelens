# -*- coding: utf-8 -*-
"""tests/test_mobile_guard.py — B11 手机同屏防误触（离线秒级）。

交付面（docs/notes-batch.md B11 行「手机同屏防误触：手机端隐藏"暂停/下课"等控制按钮」
+ 本条的 ask）：
- 服务端 `/ws` 在 accept 之后**补发一帧递手** `{"type": "hello", "lan": <bool>}`，
  lan 的判据是与 ws_clients 同源的**对端地址**（is_loopback_peer）——客户端自报不算；
- 前端收到 `hello` 且 `lan=true` → `body.mobile` → CSS 收起"控制这台电脑"的按钮
  （暂停/下课/标签/切课/换模型），保留字号与「↓ 回到最新」；lan=false（本机窗口）
  摘掉该类，界面零变化；
- **不得误触既有分支**：`hello` 帧在 onmessage 最前段处理并 return（另经核查：即便
  落到后面，字幕分支要 m.en/m.zh、向导分支要 m.en + 数值 audio_ts，未知帧本来也撞不动
  ——这条"安全前提"在下面用静态断言钉住）。

三层：① 服务端帧契约（假 WS 驱动真 ws_endpoint，环回/LAN 各断言一次）；② 前端静态
断言（hello 处理、mobile 规则、被藏/被留的按钮）；③ 真浏览器里的行为在实施报告中
以实测记录（本仓无 JS 测试运行器，与其余前端条目同口径）。
"""
import asyncio
import contextlib
import time
import types
from pathlib import Path

from starlette.websockets import WebSocketDisconnect

import main

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


class _FakeWS:
    """最小 WebSocket 替身（同 test_lan_qr 口径）+ **记录发出的帧**。

    只实现端点用到的那几面：accept / send_json / receive_text / close / client.host——
    于是"递手帧发没发、内容是什么"能精确断言，而不用真开一条手机连接。"""

    def __init__(self, host: str):
        self.client = types.SimpleNamespace(host=host)
        self.sent: list[dict] = []
        self.closed: list[int] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def send_json(self, payload) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        item = await self._q.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)

    def drop(self) -> None:
        self._q.put_nowait(WebSocketDisconnect(1000))


async def _until(pred, timeout: float = 2.0) -> bool:
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


# ---------------- 1) 服务端：递手帧按 lan 真假各一次 ----------------

async def test_hello_frame_says_lan_true_for_lan_peer():
    """局域网对端（手机扫码进来的）：`hello` 帧的 lan 必须是 True。"""
    ws = _FakeWS("192.168.1.50")
    tasks = [asyncio.create_task(main.ws_endpoint(ws))]
    try:
        assert await _until(lambda: bool(ws.sent)), "端点没有发递手帧"
        assert ws.sent[0] == {"type": "hello", "lan": True}
        assert len(main.hub.lan_clients) == 1        # 与设备计数同源判据
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


async def test_hello_frame_says_lan_false_for_loopback():
    """本机窗口（桌面壳/浏览器标签/悬浮条都是环回）：lan=False → 前端摘掉 mobile，
    界面零变化（Windows 桌面回归的判据就在这一条上）。"""
    ws = _FakeWS("127.0.0.1")
    tasks = [asyncio.create_task(main.ws_endpoint(ws))]
    try:
        assert await _until(lambda: bool(ws.sent))
        assert ws.sent[0] == {"type": "hello", "lan": False}
        assert len(main.hub.lan_clients) == 0
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


async def test_hello_frame_is_the_only_thing_sent_on_connect():
    """递手只发**一帧**：别在连接建立期往通道里塞第二样东西（前端把每一帧都当消息解析）。"""
    ws = _FakeWS("10.0.0.7")
    tasks = [asyncio.create_task(main.ws_endpoint(ws))]
    try:
        assert await _until(lambda: len(ws.sent) == 1)
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 1
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


async def test_send_failure_does_not_break_the_connection():
    """递手帧发失败（对端刚断开/管道坏）不得让端点异常退出——登记照做、清理照走。

    生产里等价的场景：手机在 accept 与 send 之间掉线。假 WS 的 send_json 直接抛，
    这里断言端点仍完成登记（clients 里有它），断开后也照常清理。"""

    class _BrokenWS(_FakeWS):
        async def send_json(self, payload):
            raise OSError("管道已断")

    ws = _BrokenWS("192.168.1.51")
    tasks = [asyncio.create_task(main.ws_endpoint(ws))]
    try:
        assert await _until(lambda: len(main.hub.clients) == 1), "登记没做"
        ws.drop()
        assert await _until(lambda: len(main.hub.clients) == 0 and
                            len(main.hub.lan_clients) == 0), "清理没做"
    finally:
        await _cancel_all(tasks)
        main.hub.clients.clear()
        main.hub.lan_clients.clear()


def test_lan_judgement_comes_from_peer_address_only():
    """服务端判据只认 socket 对端（与环回豁免同源），不看任何客户端自报字段。"""
    assert main.is_loopback_peer("127.0.0.1") and main.is_loopback_peer("::1")
    assert not main.is_loopback_peer("192.168.1.50")
    assert not main.is_loopback_peer("10.131.37.5")


# ---------------- 2) index.html：hello 接线与 mobile 规则（静态断言） ----------------

def _onmessage() -> str:
    return HTML.split("ws.onmessage = (ev) => {", 1)[1].split("\n  };", 1)[0]


def test_hello_is_handled_first_and_toggles_the_mobile_class():
    body = _onmessage()
    hello = body.split("if (m.type === 'hello') {", 1)[1].split("}", 1)[0]
    assert "classList.toggle('mobile', !!m.lan)" in hello
    assert "return;" in hello                      # 处理完立刻收口，不落到字幕/向导分支
    # 位置在**最前**（在 partial / courses / notes 之前）
    assert body.index("m.type === 'hello'") < body.index("m.type === 'partial'")
    assert body.index("m.type === 'hello'") < body.index("m.type === 'courses'")


def test_unknown_frames_cannot_touch_existing_branches():
    """ask 要求的安全前提（先核对再动手，这里把结论钉住）：一个只有 type/lan 的帧
    落到既有分支也不会产生副作用——字幕分支要 m.en/m.zh，向导分支还要数值 audio_ts。"""
    body = _onmessage()
    assert "if (m.en && m.seg !== lastSeg)" in body
    assert "else if (m.zh)" in body
    wiz = HTML.split("function wizAccepts(m) {", 1)[1].split("\n}", 1)[0]
    assert "m.en" in wiz and "typeof m.audio_ts === 'number'" in wiz


def test_mobile_css_hides_capture_controls_and_keeps_reading_controls():
    css = HTML.split("body.mobile #btnPause", 1)[1].split("}", 1)[0]
    for sel in ("#btnEndClass", "#btnTag", "#tagQuickWrap", "#selCourse",
                "#courseNewWrap", "#selModel", "#selLang", "#selDevice"):
        assert sel in css, f"应藏：{sel}"
    assert "display:none" in css
    assert "!important" not in css                  # 靠特异性，不靠 !important
    # 保留阅读/导航类控件（一个字都不许动）
    for keep in ("#btnFontMinus", "#btnFontPlus", "#btnLatest", "#btnFull",
                 "#btnAbout", "#btnNotes", "#btnSettings", "#btnExport"):
        assert f"body.mobile {keep}" not in HTML, f"不该藏：{keep}"


def test_mobile_chip_explains_why_buttons_are_gone():
    """藏了按钮就得解释（否则用户以为坏了）：只读标识只在手机形态出现。"""
    assert 'id="chipMobile"' in HTML and "手机只读模式" in HTML
    assert "#chipMobile { display:none; }" in HTML
    assert "body.mobile #chipMobile { display:inline-block; }" in HTML


def test_mobile_rule_beats_the_touch_hover_none_rule():
    """ask 点名的优先级问题：`@media (hover:none)` 让工具条常显（opacity/pointer-events），
    它管不到 display——按 display 藏按钮在触屏上照样生效；而任何可能给这些控件
    重新 display 的规则（如 #tagQuickWrap.on / #courseNewWrap.on）特异性都低于
    body.mobile #id（1,1,1）。"""
    hover_none = HTML.split("@media (hover: none)", 1)[1].split("}", 1)[0]
    assert "display" not in hover_none               # 那条只碰 opacity/transform/pointer-events
    assert "#tagQuickWrap.on { display:flex; }" in HTML
    assert "#courseNewWrap.on { display:flex; }" in HTML
    # body.mobile #tagQuickWrap（1,1,1）> #tagQuickWrap.on（1,1,0）：CSS 特异性写在这里
    assert "body.mobile #btnPause, body.mobile #btnEndClass, body.mobile #btnTag," in HTML


# ---------------- 3) 评审1① / ④ 修复 ----------------

def test_mobile_css_also_hides_the_settings_capture_controls():
    """评审1①：⚙设置 → 🎙上课 里是**同一批控件的等效入口**——工具条藏了 #selLang、
    设置页却留着 #setLangSel，手机点两下照样重建 ASR/采集器（评审实测两个都在同一屏
    可点）。故按同一条理由一并收起：会改"这台电脑怎么录 / 怎么认"的控件。"""
    rule = HTML.split("body.mobile #tierRows", 1)[1].split("}", 1)[0]
    for sel in ("#setModelSel", "#modelRows", "#setSourceSel", "#setLangSel",
                ".segGrid", "#btnSegReset", "#setStreamChk"):
        assert sel in rule, f"设置页应一并藏：{sel}"
    assert "display:none" in rule
    # 边界（刻意不扩大）：笔记管理 / 连接 / 外观 / 系统 不藏
    for keep in ("#setExportRow", "#setTagBar", "#setLanChk", "#themeRow", "#setAutostartChk"):
        assert f"body.mobile {keep}" not in HTML, f"不该藏：{keep}"


def test_mobile_note_explains_the_hidden_settings():
    """藏了控件就得说明（否则设置页看起来像缺了半页）：说明行只在该形态出现，
    且必须写清"只在这台电脑上改"+ 列出收起了什么。"""
    assert 'id="setMobileNote"' in HTML
    note = HTML.split('id="setMobileNote"', 1)[1].split("</p>", 1)[0]
    assert "mobOnly" in HTML.split('id="setMobileNote"', 1)[0][-120:]
    assert "手机只读模式" in note and "电脑" in note
    for what in ("档位", "高级模型", "音源", "识别语言", "分句调优"):
        assert what in note, what
    assert ".mobOnly { display:none; }" in HTML
    assert "body.mobile .mobOnly { display:block; }" in HTML


def test_paused_banner_has_a_mobile_copy():
    """评审1④：横幅原先是固定串「点击工具条『▶ 继续』」——而手机形态那颗按钮正是被
    藏掉的，等于给手机用户一条点不到的死指令。两段文案按形态二选一（桌面文案逐字保留）。"""
    banner = HTML.split('<div id="pausedBanner">', 1)[1].split("</div>", 1)[0]
    assert 'class="pcOnly"' in banner and 'class="mobOnly"' in banner
    assert "点击工具条「▶ 继续」恢复字幕" in banner           # 桌面文案原样
    mob = banner.split('class="mobOnly"', 1)[1]
    assert "在电脑上点" in mob and "只读" in mob              # 手机文案指向电脑
    assert "body.mobile .pcOnly { display:none; }" in HTML
    assert "body.mobile #pausedBanner .mobOnly { display:inline; }" in HTML
