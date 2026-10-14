# -*- coding: utf-8 -*-
"""tests/test_note_select_all.py — 波次一 A1：全选套装（三态全选 + 反选 + Ctrl+A）。

交付面（docs/notes-batch.md「A 组：选择与批量」A1 行）：
- 笔记库**工具条**加三态全选复选（全选 / 半选 / 空）+ 反选按钮；
- Ctrl+A 挂在**既有** keydown 处理器上（同一处理器还管 Escape），且**仅笔记库视图**生效；
- 「已选 N 篇」沿用 renderBatchBar 既有口径；
- 筛选变化（检索 / 课程 / 标签三条手臂）**硬清空**选择（旧实现只把选择集收敛到可见项）；
- 删除确认框保持「数量 + 进回收站可恢复」文案。

三层拆分（与 test_note_manage / test_note_course_move 同款）：本文件**只有第三层**——
index.html 的静态结构断言。理由如实写明：A1 是纯前端交互（勾选/半选/Ctrl+A 都在浏览器
里发生），本仓没有 JS 测试运行器，repo 既有纪律也是「浏览器行为不进自动化；结构用
split 夹取 pin 住 + 真机手测」（见 test_note_course_move.py 抬头）。故：
  · 结构 / 接线 / 文案 / 令牌：本文件（pytest，秒级）；
  · 「服务端真正发出去的页面含这套控件」：scripts/smoke_test.py [5] 的页面断言；
  · 真浏览器里的三态与 Ctrl+A 行为：真机手测（已在本批实施报告中记录实测步骤）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


def block(marker: str, stop: str) -> str:
    """取 [marker, stop) 之间的片段（本仓既有的 split 夹取手法，先断言 marker 存在）。"""
    assert marker in HTML, f"index.html 缺少 {marker}"
    return HTML.split(marker, 1)[1].split(stop, 1)[0]


# ---------------- 1) 工具条：三态全选 + 反选 ----------------

def test_select_all_lives_in_toolbar_not_batch_bar():
    """全选框必须在**工具条**里：批量条「已选 ≥1 篇才显示」，而全选框要在 0 篇选中时
    就能用（否则无从开始多选）。同时 pin 住 nbBatch 本体 markup 字面量没被动过。"""
    bar = block('<div class="nbBar">', "</div>")
    assert 'id="nbSelAll"' in bar and 'id="nbBatchInvert"' in bar
    batch = block('<div class="nbBatch" id="nbBatch">', "</div>")
    assert "nbSelAll" not in batch and "nbBatchInvert" not in batch
    # A1 原文：回收站已有「清空」故**不设**全选 → 全选框只允许存在这一处
    assert HTML.count('id="nbSelAll"') == 1
    assert 'class="nbBatch" id="nbBatch"' in HTML        # A1 只加子元素，不重写本体
    # 复选框本体 + 无障碍名（键盘/读屏用户也要能用）
    box = block('<input type="checkbox" id="nbSelAll"', ">")
    assert 'aria-label="全选当前列表的笔记"' in box


def test_master_checkbox_is_wired_tristate():
    """三态：全选（checked）/ 半选（原生 indeterminate）/ 空；同步点只有 renderBatchBar。"""
    sync = block("function syncSelAll() {", "\n}")
    assert ".indeterminate" in sync                       # 半选态
    assert ".checked" in sync and ".disabled" in sync     # 全选态 / 列表空时禁用
    bar = block("function renderBatchBar() {", "\n}")
    assert "已选 ${nbPicked.size} 篇" in bar              # 「已选 N 篇」沿用既有口径
    assert "syncSelAll()" in bar                          # 选择集变 → 三态跟着走
    # 点击语义写死在接线里：已全选 → 清空；空 / 半选 → 补齐
    pick = block("$('nbSelAll').onchange = () => {", "\n};")
    assert "nbPicked.clear()" in pick and "nbPicked.add(n)" in pick
    assert "rerenderPicked()" in pick
    inv = block("$('nbBatchInvert').onclick = () => {", "\n};")
    assert "nbPicked.delete(n)" in inv and "nbPicked.add(n)" in inv


def test_selection_scope_is_visible_list_only():
    """A1 的硬约束：选择作用域 = **当前列表可见项**（看不见的笔记勾不上，也就不会被
    批量改归属 / 删除带走）。三个入口（全选框 / 反选 / Ctrl+A）都只遍历 visibleNames()。"""
    vn = block("function visibleNames() {", "\n}")
    assert "NB.notes" in vn and "nbPicked" not in vn
    for marker in ("$('nbSelAll').onchange = () => {",
                   "$('nbBatchInvert').onclick = () => {",
                   "function pickAllVisible() {"):
        body = block(marker, "\n}")
        assert "visibleNames()" in body, marker
        assert "NB.notes" not in body, f"{marker} 应经 visibleNames() 取列表"


# ---------------- 2) Ctrl+A：挂在既有 keydown 上，且仅笔记库视图 ----------------

def test_ctrl_a_hangs_on_existing_keydown_handler():
    head = block("document.addEventListener('keydown', (e) => {",
                 "\n  if (e.key !== 'Escape') return;")
    assert "(e.ctrlKey || e.metaKey)" in head and "(e.key === 'a' || e.key === 'A')" in head
    assert "pickAllVisible()" in head
    assert "e.preventDefault()" in head
    # 道闸一：只在笔记库视图生效
    assert "classList.contains('view-notes')" in head
    # 道闸二：弹窗开着时不抢（背景正 inert）
    assert "activeMask()" in head
    # 道闸三：焦点在输入控件里 → 归浏览器（检索框里的 Ctrl+A 必须是全选文字）
    assert "isContentEditable" in head and "'INPUT'" in head and "'TEXTAREA'" in head
    # 同一个处理器仍是既有 Escape 链（A1 只**加**分支，不改既有语义）
    tail = block("document.addEventListener('keydown', (e) => {", "\n});")
    assert "e.key !== 'Escape'" in tail
    assert "wizardFinish()" in tail and "closeModal(mask)" in tail
    assert "MASKS.map((id) => $(id))" in HTML             # 遮罩白名单判据仍只有一处写法


# ---------------- 3) 筛选变化 = 硬清空（不是收敛） ----------------

def test_filter_change_hard_clears_selection():
    """A1：切换检索 / 课程 / 标签时**清空**选择，防「看不见的选中」被下一次批量删除
    带走。清空点只有一处（refreshNotes），三条手臂都走它 → 漏改一条的缝不存在。"""
    rn = block("async function refreshNotes() {", "\n}")
    assert "clearNotePick();" in rn
    assert "nbPicked = new Set([...nbPicked]" not in HTML          # 旧的「收敛」已删
    cl = block("function clearNotePick() {", "\n}")
    assert "nbPicked.clear()" in cl and "renderBatchBar()" in cl   # 清空 + 三态复位
    for marker, stop in (("$('nbSearch').oninput = () => {", "\n};"),
                         ("$('nbSearch').onkeydown = (e) => {", "\n};"),
                         ("$('nbCourseFilter').onchange = () => {", "\n};"),
                         ("$('nbTagFilter').onchange = () => {", "\n};")):
        b = block(marker, stop)
        assert "refreshNotes()" in b, marker                       # 三者都收敛到同一处
        assert "nbPicked" not in b, f"{marker} 不该自己碰选择集"


def test_batch_bar_copy_explains_the_clear():
    """A1：清空这条规矩要在**批量条文案**上说明（用户看得见，不必猜）。"""
    assert 'id="nbBatchHint"' in HTML
    hint = block('id="nbBatchHint"', "</span>")
    assert "切换筛选" in hint and "清空选择" in hint
    assert "title=" in hint                                       # 悬停给出完整理由


def test_clears_use_the_single_helper():
    """移动/删除完成后与「清空选择」按钮一律走 clearNotePick()（口径唯一，防漏复位）。

    评审1 ① 追加：clearNotePick 必须**连行内勾选框一起重画**（rerenderPicked），否则
    清空后行上还显示着旧勾——用户点那一行时 onchange 从 checked 翻到 unchecked、走
    「取消勾选」分支，那次点击被静默吞掉（真浏览器实测复现）。"""
    assert "$('nbBatchClear').onclick = () => clearNotePick();" in HTML
    cl = block("function clearNotePick() {", "\n}")
    assert "rerenderPicked();" in cl and "renderNoteList()" in block(
        "function rerenderPicked() {", "\n}")
    apply_move = block("async function applyMove(courseId) {", "\n}")
    assert "clearNotePick()" in apply_move
    del_note = block("async function delNote(files) {", "\nfunction fmtDeleted(ts) {")
    assert "clearNotePick()" in del_note


# ---------------- 4) 删除确认（A1 要求保持） ----------------

def test_delete_confirm_keeps_count_and_trash_copy():
    """既有确认框的两件事实必须留着：**几篇**（数量）+ **可恢复**（软删除，不是销毁）。"""
    d = block("async function delNote(files) {", "\nfunction fmtDeleted(ts) {")
    assert "`${files.length} 篇笔记`" in d                        # 多篇时写明数量
    assert "files.length === 1 ? files[0]" in d                   # 单篇时写明文件名
    assert "移入回收站" in d and "恢复" in d and "不可逆" in d


# ---------------- 5) 视觉：语义令牌，无新色值 / 无新动效 ----------------

def test_a1_styles_use_tokens_no_new_animation():
    blk = block("/* —— 波次一 A1：工具条三态全选 + 反选 ——", "/* —— §8")
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}", blk), "A1 样式不得写死色值（§一-4）"
    assert "animation" not in blk and "transition" not in blk, "§5：不加动效"
    assert ".nbBar .nbPickAll" in blk
    # 全选框复用行内勾选的 .nbPick（16px + accent-color=gold ⇒ §六 选中态口径）
    assert ".nbPick input { width:16px; height:16px; accent-color:var(--gold);" in HTML
