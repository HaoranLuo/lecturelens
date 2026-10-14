# -*- coding: utf-8 -*-
"""tests/test_note_keyboard.py — 波次二 B7：笔记库键盘整理流（离线秒级）。

交付面（docs/notes-batch.md B7 行「键盘整理流」+ 本条的 ask）：
- 在**既有** keydown 处理器（同一处理器还管 A1 的 Ctrl+A 与 Escape）里加一段：
  J/K（或 ↓/↑）在列表里移动选中（焦到对应行 button + scrollIntoView block:nearest）、
  Enter 打开选中行（openNote）、Delete 在选中行上走 delNote（带既有确认与可恢复文案）；
- 沿用**三道闸**（view-notes / 无弹窗 / 焦点不在输入控件）——与 Ctrl+A 同一口径；
- **不得破坏** tests/test_note_select_all.py 对 Ctrl+A 段与 Escape 尾链的夹取断言
  （本文件不复述那两条断言，只保证新分支加在它们**之前**且不改既有行）。

本文件只有 index.html 的静态结构断言：本仓没有 JS 测试运行器，repo 既有纪律是
「浏览器行为不进自动化；结构用 split 夹取 pin 住 + 真机手测」（见
test_note_course_move.py / test_note_select_all.py 抬头）。真浏览器里的按键行为
（J/K 移焦、Enter 开篇、Delete 弹确认）在实施报告里记录实测步骤与结果。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

LISTENER = "document.addEventListener('keydown', (e) => {"
ESCAPE_LINE = "if (e.key !== 'Escape') return;"


def block(marker: str, stop: str) -> str:
    """取 [marker, stop) 之间的片段（沿用 test_note_select_all.py 的夹取手法）。"""
    assert marker in HTML, f"index.html 缺少 {marker}"
    return HTML.split(marker, 1)[1].split(stop, 1)[0]


def listener() -> str:
    """整个 keydown 处理器（含新加的 B7 分支与既有 Escape 尾链）。"""
    return block(LISTENER, "\n});")


# ---------------- 1) 新分支：加在既有处理器里、Escape 之前 ----------------

def test_b7_branch_lives_in_existing_keydown_before_escape():
    """B7 只**加**分支：既不新开一个 keydown（否则和既有链抢键），也不能排到
    Escape 之后（Escape 那条 `if (e.key !== 'Escape') return;` 会先把键吃掉）。"""
    body = listener()
    assert "focusNoteRow(" in body and "selNoteName()" in body
    assert "delNote([name])" in body and "openNote(NB.sel)" in body
    assert HTML.index("B7：键盘整理流") < HTML.index(ESCAPE_LINE)
    # 既有两条链的字面量原样还在（test_note_select_all.py 另有更细的夹取断言）
    assert "pickAllVisible()" in body
    assert "wizardFinish()" in body and "closeModal(mask)" in body


def test_move_keys_cover_jk_and_arrows_only_without_modifiers():
    """J/K 与 ↓/↑ 都认（大写 J/K = 按住 Shift，照收）；带 Ctrl/Cmd/Alt 的按下不抢
    （那是浏览器/系统的快捷键）。"""
    body = listener()
    for k in ("'j'", "'J'", "'k'", "'K'", "'ArrowDown'", "'ArrowUp'"):
        assert k in body, k
    assert "const plainKey = !e.ctrlKey && !e.metaKey && !e.altKey;" in body
    assert "const down = (e.key === 'j' || e.key === 'J' || e.key === 'ArrowDown');" in body


def test_move_only_swallows_the_key_when_a_row_was_focused():
    """列表空 → focusNoteRow 返回 false → **不** preventDefault，方向键照常滚页。"""
    assert "if (focusNoteRow(down ? 1 : -1)) {" in listener()
    fn = block("function focusNoteRow(delta) {", "\n}")
    assert "if (!list.length) return false;" in fn
    assert "return true;" in fn
    # 到端点停住（不绕回）：夹在 [0, len-1]
    assert "Math.min(list.length - 1, Math.max(0, from + delta))" in fn


def test_move_focuses_row_button_and_scrolls_nearest():
    """ask 的机制口径：焦到对应行 button（阻止自带滚动）+ scrollIntoView block:nearest。"""
    fn = block("function focusNoteRow(delta) {", "\n}")
    assert "$('nbList').querySelectorAll('button.nbRow')" in fn
    assert "b.dataset.name === NB.sel" in fn                 # 还没聚焦过 → 从选中那篇起算
    assert ".focus({ preventScroll: true })" in fn
    assert "scrollIntoView({ block: 'nearest' })" in fn


def test_row_buttons_carry_data_name():
    """行按钮带 data-name：键盘流"焦点所在的行"靠它对回笔记名（逐项比对，不拼选择器）。"""
    row = block("function noteRow(n) {", "\n}")
    assert "b.dataset.name = n.name;" in row


def test_enter_opens_selected_row_without_hijacking_native_activation():
    """Enter：焦点不在按钮/链接上时代为打开 NB.sel；已在行按钮上 → 交给原生激活
    （那本来就是这一行的 onclick），否则会开两次。没选中则什么都不做。"""
    body = listener()
    assert "if (e.key === 'Enter' && plainKey && notesKeyAllowed()) {" in body
    assert "['BUTTON', 'A'].includes(act.tagName)" in body
    assert "&& NB.sel) {" in body


def test_delete_uses_existing_delnote_with_confirm_and_recoverable_copy():
    """Delete 不在处理器里自己发请求，走既有 delNote（确认框 + 「可恢复」文案）。"""
    body = listener()
    assert "const name = selNoteName();" in body
    assert "delNote([name]);" in body
    dn = block("async function delNote(files) {", "\n}")
    assert "confirm(`把 ${what} 移入回收站？" in dn
    assert "可随时从「回收站」恢复；彻底删除才不可逆。" in dn


def test_selected_row_definition_prefers_focused_row_then_nb_sel():
    fn = block("function selNoteName() {", "\n}")
    assert "el.classList.contains('nbRow')" in fn and "el.dataset.name" in fn
    assert "return NB.sel;" in fn


# ---------------- 2) 三道闸：与 Ctrl+A 同一口径 ----------------

def test_three_gates_shared_by_all_b7_keys():
    gate = block("function notesKeyAllowed() {", "\n}")
    assert "classList.contains('view-notes')" in gate        # ① 只在笔记库视图
    assert "activeMask()" in gate                             # ② 弹窗开着不抢键
    assert "isContentEditable" in gate                        # ③ 焦点在输入控件里不抢键
    assert "'INPUT'" in gate and "'TEXTAREA'" in gate and "'SELECT'" in gate
    body = listener()
    assert body.count("notesKeyAllowed()") >= 3               # 移动 / Delete / Enter 各一道


def test_ctrl_a_branch_keeps_its_own_inline_gates():
    """**给后来者**：Ctrl+A 段的三道闸是内联原文，被 test_note_select_all.py 夹住钉死——
    给 B7 抽公共闸门时**不能**顺手把它改成调用 notesKeyAllowed()（那会让断言找不到
    原文）。这条只钉"两处都在"这一事实。"""
    head = block(LISTENER, "\n  " + ESCAPE_LINE)
    assert "pickAllVisible()" in head                         # Ctrl+A 分支仍在
    assert "notesKeyAllowed()" in listener()                  # B7 的闸门也在


def test_list_rerender_keeps_the_focused_row():
    """评审1② 修复：Ctrl+A → pickAllVisible → rerenderPicked → renderNoteList 会**重建整张
    列表 DOM**，正聚焦的行按钮被销毁 → activeElement 掉回 body：焦点环消失、紧随其后的
    J 从第一行重来（"从当前行继续走"断掉，评审实测 focusAfter=''）。
    renderNoteList 因此必须先记住焦点行、画完还回去；**只在焦点原本就在行上时**还
    （焦点在检索框/批量条时不碰——否则每次后台刷新都把用户从输入框里拽出来）。"""
    rn = block("function renderNoteList() {", "\n}")
    assert "document.activeElement" in rn
    assert "classList.contains('nbRow')" in rn              # 只认"焦点在行上"这一种
    assert "act.dataset.name" in rn                         # 认的是那行的笔记名
    assert "box.innerHTML = ''" in rn                       # 仍旧是整表重建（选择集不受影响）
    assert ".find((b) => b.dataset.name === keepRow)" in rn
    assert "again.focus({ preventScroll: true })" in rn
