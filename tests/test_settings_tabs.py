# -*- coding: utf-8 -*-
"""tests/test_settings_tabs.py — B12：设置页五分区标签页（离线秒级，结构层）。

交付面（docs/notes-batch.md B12 行）：
- 设置页分五区：🎙上课（识别模型/音源/识别语言/高级·分句调优/流式字幕/DeepSeek key）、
  📝笔记（课程管理/下课归档/导出）、📶连接（手机同屏/安全）、🎨外观（字幕显示/悬浮字幕条）、
  ⚙系统（全局快捷键/开机自启/数据去向，含重开首启向导）；
- tab 条复用 .tagChip/.tagChip.on（选中态 gold-dim/gold，design-spec §六 令牌）；
- **记住上次分区**（localStorage 键 `ll_settings_tab`）；
- **深链** `openWorkbench('settings', {tab})`（手机同屏面板「去设置」直达 📶 连接）。

本文件只做结构/接线断言（真浏览器里的点选与记忆行为在实施报告与真机手测；本仓没有
JS 测试运行器，与其余前端条目同一口径）。同时**反向守卫**几条被既有测试钉死的约束：
LAN_JS 段标记之间不得出现本条的 JS、.tagChip CSS 块里不得混进新规则。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

#: 分区 → 该分区必须包含的卡片标题（以 <h2> 的**首个**片段匹配）
PANES = {
    "lesson": ("识别模型", "音源", "识别语言", "高级 · 分句调优", "流式字幕", "DeepSeek API key"),
    "notes": ("课程管理", "下课归档", "导出"),
    "link": ("手机同屏", "安全"),
    "look": ("字幕显示", "悬浮字幕条"),
    "system": ("全局快捷键", "开机自启", "数据去向"),
}
TABS = ("lesson", "notes", "link", "look", "system")


def settings_section() -> str:
    assert '<section id="viewSettings">' in HTML
    return HTML.split('<section id="viewSettings">', 1)[1].split("</section>", 1)[0]


def card_tabs(section: str) -> list[tuple[str, str]]:
    """→ [(卡片标题首段, data-set-tab)]，按 markdown 顺序。"""
    out = []
    for m in re.finditer(r'<div class="card"([^>]*)>\s*\n\s*<h2>(.*?)</h2>', section):
        t = re.search(r'data-set-tab="([a-z]+)"', m.group(1))
        out.append((m.group(2).split("<")[0].strip(), t.group(1) if t else ""))
    return out


# ---------------- 1) 结构：五分区 + 卡片归属 ----------------

def test_five_tabs_exist_with_labels():
    for t, label in (("lesson", "上课"), ("notes", "笔记"), ("link", "连接"),
                     ("look", "外观"), ("system", "系统")):
        assert f'id="setTab-{t}"' in HTML, t
        assert re.search(rf'id="setTab-{t}"[^>]*>.*?{label}', HTML), t
    bar = HTML.split('<div class="setTabs" id="setTabs"', 1)[1].split("</div>", 1)[0]
    assert 'role="tablist"' in HTML.split('<div class="setTabs"', 1)[1].split(">", 1)[0]
    assert bar.count('role="tab"') == 5


def test_every_settings_card_belongs_to_exactly_one_pane():
    cards = card_tabs(settings_section())
    assert len(cards) == sum(len(v) for v in PANES.values()) == 16
    assert all(t in TABS for _, t in cards), [c for c in cards if c[1] not in TABS]
    for pane, titles in PANES.items():
        got = [name for name, t in cards if t == pane]
        for want in titles:
            assert any(name.startswith(want) for name in got), (pane, want, got)
        assert len(got) == len(titles), (pane, got)


def test_panes_are_disjoint_and_cover_all_cards():
    """反向守卫：不允许有卡片漏挂分区（漏了就永远不显示，用户找不到）。"""
    cards = card_tabs(settings_section())
    assert not [name for name, t in cards if not t]


# ---------------- 2) 视觉：复用 .tagChip 令牌，且不污染被钉死的 CSS 块 ----------------

def test_tabs_reuse_tagchip_and_own_css_is_elsewhere():
    """tab 复用 .tagChip（选中态 gold-dim/gold 由既有规则给），面板显隐只有两条规则；
    且这段 CSS **不在** test_note_tags 钉住的 `.tagChip → tagQuickWrap` 区间内
    （区间里的内容被断言「无 animation/transition + 有 gold-dim」，混进来就有风险）。"""
    pinned = HTML.split("  .tagChip { display:inline-flex;", 1)[1].split(
        "body:has(#tagQuickWrap.on)", 1)[0]
    assert "setTabs" not in pinned and "data-set-tab" not in pinned
    assert 'id="setTab-lesson" data-tab="lesson" role="tab"' in HTML
    assert HTML.count('class="tagChip" id="setTab-') == 5      # 复用同一个 chip 类
    block = HTML.split("/* —— B12：设置页五分区标签页 ——", 1)[1].split("/*", 1)[0]
    assert ".setTabs { display:flex;" in block and ".setTabs .tagChip" in block
    assert "#viewSettings .card[data-set-tab].setOn { display:block; }" in block
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}", block), "分区样式不得写死色值（§一-4）"
    assert "animation" not in block and "transition" not in block, "§5：不加动效"


# ---------------- 3) 记住上次分区（localStorage） ----------------

def test_remembers_last_tab_in_localstorage():
    assert "const SET_TAB_KEY = 'll_settings_tab';" in HTML
    assert "localStorage.getItem(SET_TAB_KEY)" in HTML
    assert "localStorage.setItem(SET_TAB_KEY, want)" in HTML
    fn = HTML.split("function setActiveTab(tab, save = true) {", 1)[1].split("\n}", 1)[0]
    assert "SET_TABS.includes(tab) ? tab : 'lesson'" in fn      # 非法值回落上课
    assert "card.dataset.setTab === want" in fn                 # 卡片按分区显隐
    assert "aria-selected" in fn                                # 读屏用户能知道选中哪格
    stored = HTML.split("function storedSetTab() {", 1)[1].split("\n}", 1)[0]
    assert "catch" in stored, "隐私模式（localStorage 抛异常）不得让设置页崩掉"


# ---------------- 4) 深链：openWorkbench('settings', {tab}) ----------------

def test_openworkbench_accepts_tab_deeplink():
    fn = HTML.split("function openWorkbench(view, opts) {", 1)[1].split("\n}", 1)[0]
    assert "opts && opts.tab" in fn
    assert "setActiveTab(want || storedSetTab(), !!want);" in fn   # 深链会记成"上次分区"
    assert "refreshSettings();" in fn
    # 至少一个真实入口：手机同屏面板的「去设置」直达 📶 连接
    assert 'id="lanGoSet"' in HTML
    boot = HTML.split("// B12 深链入口：手机同屏面板「去设置」", 1)[1]
    assert "closeModal($('lanMask'));" in boot[:400]
    assert "openWorkbench('settings', { tab: 'link' });" in boot[:400]


def test_tab_wiring_lives_in_boot_block_not_after_connect():
    """新初始化必须放在启动引导区：connect(); 之后首个非空白仍是注释行
    （tests/test_toolbar_course.py 钉着这条——本项不得绕过它）。"""
    after_connect = HTML.split("connect();", 1)[1].lstrip()
    assert after_connect.startswith("//")
    boot = HTML.split("// —— 启动引导（**必须放在全部 let/const 声明之后**）——", 1)[1]
    assert "$('setTab-' + _t)" in boot
    assert "setActiveTab(storedSetTab(), false);" in boot


# ---------------- 5) 反向守卫：不得污染既有钉死区 ----------------

def test_lan_js_block_untouched_by_tabs():
    """§7 面板 JS 段（两个段标记之间）是 tests/test_lan_ui.py 的断言区：
    本条的 JS 一律不得进去（进去就会让那一整段的结构断言口径漂移）。"""
    lan_js = HTML.split("// —— §7 手机同屏（notes-batch）", 1)[1].split(
        "// —— p2-4 模型管理器", 1)[0]
    for banned in ("setActiveTab", "SET_TABS", "setTab-", "ll_settings_tab"):
        assert banned not in lan_js, banned


def test_settings_card_ids_and_literals_preserved():
    """设置页既有控件 id/字面量（§3/§5/§7 与 §1 的消费方）一个都不能少。"""
    for anchor in ('id="setLanChk"', 'id="setLanState"', 'id="setEndConfirmChk"',
                   'id="setExportState"', 'id="setExportInput"', 'id="btnExportPick"',
                   'id="btnExportOpen"', 'id="btnExportClear"', 'id="setStreamChk"',
                   'id="setFloatChk"', 'id="setHotkeysChk"', 'id="setAutostartChk"',
                   'id="setAuthChk"', 'id="selCourse"', 'id="courseRows"'):
        assert anchor in HTML, anchor
    assert "$('setLanChk').checked = !!SETCFG.lan_enabled;" in HTML
    assert "syncLanClients(status_.ws_clients)" in HTML
