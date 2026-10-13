# -*- coding: utf-8 -*-
"""tests/test_toolbar_course.py — §1 录制页工具条「课程」下拉（离线秒级）。

交付面（docs/notes-batch.md §1）：字幕页工具条列出全部课程 + 「未分类」+「新建课程…」
（内联输入调既有 POST /api/courses）→ 选中 POST /api/courses/active → 笔记归属复用
服务端既有 Hub.retag_course 语义（空笔记改头 / 记录中追加切换标记）；下拉显示当前课程名
并与 status.course、WS 'courses' 事件保持同步。

如实说明（§2.1 纪律）：浏览器交互不进自动化。本文件做两件事——
① 对 index.html 的**结构断言**（下拉/内联输入/三条同步路径的布线，含一条 TDZ 回归）；
② 对服务端侧契约的**行为断言**（下拉消费的响应形状与 retag 语义，走 httpx ASGITransport）。
真机行为在 Chrome 里逐项跑过（新建 → 设为当前 → 另一窗口切课即时同步 → 选项文本
textContent 无注入），记录见实施报告；后续回归以本文件 + scripts/smoke_test 的 [5] 段承担。
"""
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import courses as courses_mod
import main

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

#: 工具条区块（结构断言的作用域：只认 #toolbar 里的控件，别处同名不算数）
TOOLBAR = HTML.split('<div id="toolbar">', 1)[1].split("</div>", 1)[0]


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def cpath(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    return main.COURSES_PATH


@pytest.fixture
def global_hub(tmp_path):
    """用**全局 hub**（= /api/courses/active 实际操作的那个）起笔记，用完还原。

    踩过的坑：端点走 `main.hub`，测试里 `main.Hub()` 新建的实例与它无关——两份笔记
    各写各的，断言 note 永远得到 'none'（看似服务端没做事，其实是测试看错了对象）。
    笔记目录已由 conftest 重定向到 tmp_path，不碰仓库真文件。"""
    hub = main.hub
    saved = (hub.transcript_path, hub.stamp, hub.t0_audio, list(hub.times))

    def start(course=None):
        return hub.start_transcript(course)

    yield hub, start
    hub.transcript_path, hub.stamp = saved[0], saved[1]
    hub.t0_audio = saved[2]
    hub.times = saved[3]


# ---------------- ① 结构：控件与选项来源 ----------------

def test_toolbar_has_course_select():
    """工具条里确有「课程」下拉 + 内联新建三件套（id 与 §1 实现一一对应）。"""
    assert 'id="selCourse"' in TOOLBAR
    assert "课程：未分类" in TOOLBAR          # 首屏骨架：未选课 = 未分类（不等 JS 拉数据）
    for el in ('id="courseNewWrap"', 'id="courseNewInline"',
               'id="btnCourseNewGo"', 'id="btnCourseNewCancel"'):
        assert el in TOOLBAR, f"工具条内联新建缺少 {el}"


def test_options_cover_courses_uncategorized_and_new():
    """选项 = 未分类 + 全部课程 +「＋ 新建课程…」，且全部走 textContent（课程名是用户输入）。"""
    body = HTML.split("function renderToolbarCourse(fresh) {", 1)[1].split("\n}", 1)[0]
    assert "COURSES.uncategorized" in body            # 「未分类」
    assert "for (const c of (COURSES.courses || []))" in body   # 全部课程（单一数据源）
    assert "NEW_COURSE" in body                       # 「＋ 新建课程…」哨兵
    assert "o.textContent = label;" in HTML           # 渲染纪律：textContent，不拼 innerHTML
    assert "o.dataset.name = name;" in HTML           # 与 status.course 对齐的键（课程名）


def test_inline_new_course_uses_existing_endpoint():
    """内联输入走**既有** POST /api/courses（createCourse），建完设为当前课程。"""
    body = HTML.split("async function submitCourseNew() {", 1)[1].split("\n}", 1)[0]
    assert "await createCourse(name, '')" in body
    assert "await setActiveCourse(course.id)" in body
    # 回车提交 / Esc 取消都要在（上课态少点鼠标）
    assert "e.key === 'Enter'" in HTML and "submitCourseNew()" in HTML
    assert "e.key === 'Escape'" in HTML
    # createCourse 必须回传 course（拿 id 才能接着设为当前课程）
    assert "return r.course;" in HTML


def test_selection_posts_active_endpoint():
    """选中 → 复用 setActiveCourse → POST /api/courses/active（未分类 = active:null）。"""
    onchange = HTML.split("selCourse.onchange = async () => {", 1)[1].split("\n};", 1)[0]
    assert "openCourseNewInline()" in onchange         # 哨兵值只展开输入，不切课
    assert "setActiveCourse(val === '' ? null : val)" in onchange
    assert "await jfetch('/api/courses/active'" in HTML
    assert 'JSON.stringify({ active: id })' in HTML


def test_three_sync_paths_wired():
    """三条同步路径缺一不可：① 本窗口重建 ② WS 'courses' 广播 ③ status.course 轮询兜底。"""
    assert "renderToolbarCourse(r.active_name);" in HTML        # ① refreshCourses
    at = HTML.index("if (m.type === 'courses') {")
    assert "renderToolbarCourse(m.active_name)" in HTML[at:at + 400]   # ② WS（另一窗口改课即达）
    poll = HTML.index("async function pollStatus() {")
    seg = HTML[poll:HTML.index("trackNoteTasks();", poll)]
    # ③ 跃迁式：只在 status.course **变化**时动作（每 2s 无条件覆盖会把刚到的真值顶回旧值）
    assert "status_.course !== lastCourseName" in seg
    assert "lastCourseName = status_.course" in seg
    assert "syncToolbarCourse();" in seg


def test_startup_course_pull_runs_after_declarations():
    """TDZ 回归（实测修复）：启动拉课程表必须发生在 `let COURSES` / `const LOADED` 之后。

    修前 `refreshCourses()` 写在脚本中段，函数体一碰 LOADED 就抛 TDZ ReferenceError；
    async 把它变成 rejected promise——界面无提示、但启动那次 /api/courses 从未发出
    （真 Chrome 控制台：Uncaught (in promise) at refreshCourses (index.html:2002) ← index.html:1410）。
    断言口径：启动引导块在全部声明之后，且它内部确有拉取调用；connect() 之后紧跟的是
    说明注释（旧调用点已搬走）。"""
    boot = HTML.index("启动引导（**必须放在全部 let/const 声明之后**）")
    assert boot > HTML.index("let COURSES = {")
    assert boot > HTML.index("const LOADED = {")
    assert "refreshCourses();" in HTML[boot:], "启动引导里必须真的拉一次课程表"
    after_connect = HTML.split("connect();", 1)[1].lstrip()
    assert after_connect.startswith("//"), "connect() 之后应先是说明注释（旧调用点已搬走）"


def test_inline_row_follows_design_spec():
    """视觉口径（docs/design-spec.md）：内联输入沿用 §6 输入规范（语义令牌，无硬编码色值），
    且不新增动效（§5 只有三个动效，工具条浮现是其一）。"""
    # 输入框并入工具条控件同一条规则（surface-2 底 + 1px border + 高 28px）
    assert "#toolbar button, #toolbar select, #toolbar input[type=text] {" in HTML
    assert "#toolbar select:focus, #toolbar input[type=text]:focus {" in HTML
    assert "var(--gold) 50%, transparent" in HTML              # §6 聚焦口径
    wrap = HTML.split("#courseNewWrap {", 1)[1].split("}", 1)[0]
    assert "display:none" in wrap and "animation" not in wrap and "transition" not in wrap
    # 展开期间工具条保活：否则键盘打字 3s 后界面消失（同 #errBanner 的 body:has 口径）
    assert "body:has(#courseNewWrap.on) header" in HTML
    assert not re.search(r"#courseNewWrap[^{]*\{[^}]*#[0-9a-fA-F]{3,6}", HTML), \
        "内联新建样式不得写死色值（§一-4 语义令牌）"


# ---------------- ② 行为：下拉消费的服务端契约 ----------------

async def test_create_then_activate_rewrites_empty_note_header(cpath, global_hub):
    """内联新建的完整链路：建课 → 设为当前 → 空笔记**改写头**（retag 的 empty 语义）。"""
    hub, start = global_hub
    p = start(None)
    async with asgi_client() as c:
        r = await c.post("/api/courses", json={"name": "信号与系统"})
        assert r.status_code == 200
        course = r.json()["course"]
        assert course["id"] and course["name"] == "信号与系统"     # 下拉需要 id 去切课
        r2 = await c.post("/api/courses/active", json={"active": course["id"]})
        assert r2.status_code == 200
        body = r2.json()
        assert body["active_name"] == "信号与系统"
        assert body["note"] == "empty"          # 空笔记 = 改头（前端提示文案分支据此）
        assert main.status["course"] == "信号与系统"
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) == "信号与系统"


async def test_switch_mid_lecture_marks_instead_of_rewriting(cpath, global_hub):
    """已开记再切课：**追加切换标记**、原归属不改（前端只显示 note 字段，不自己实现分流）。"""
    hub, start = global_hub
    p = start("微积分")
    hub.append("10:00:01", "derivative", "导数", 1)
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "线性代数"})).json()["course"]["id"]
        assert (await c.post("/api/courses/active", json={"active": cid})).json()["note"] == "marked"
    text = p.read_text(encoding="utf-8")
    assert courses_mod.note_course(text) == "微积分"        # 主归属 = 开课课程，不说谎
    assert f"{courses_mod.COURSE_HEADER_PREFIX}线性代数" in text
    assert "derivative" in text                            # 正文零丢失


async def test_uncategorized_option_maps_to_null(cpath, global_hub):
    """「未分类」项 = active:null（与侧栏首项同一口径），并把空笔记头改回未分类。"""
    hub, start = global_hub
    p = start("微积分")
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "微积分"})).json()["course"]["id"]
        await c.post("/api/courses/active", json={"active": cid})
        body = (await c.post("/api/courses/active", json={"active": None})).json()
        assert body["active"] is None
        assert body["active_name"] == courses_mod.UNCATEGORIZED
        assert body["note"] == "empty"
        assert main.status["course"] == courses_mod.UNCATEGORIZED
        assert (await c.get("/api/courses")).json()["active"] is None
    assert courses_mod.note_course(
        p.read_text(encoding="utf-8")) == courses_mod.UNCATEGORIZED


async def test_active_response_shape_is_what_dropdown_consumes(cpath):
    """下拉只消费这几个键：ok/active/active_name/note（+ glossary_reloaded 供日志）——
    多一个少一个都算契约变更，前端要同步改，故在此钉住。"""
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "编译原理"})).json()["course"]["id"]
        body = (await c.post("/api/courses/active", json={"active": cid})).json()
        assert set(body) == {"ok", "active", "active_name", "note", "glossary_reloaded"}
        assert body["ok"] is True and body["note"] in ("empty", "marked", "none")


async def test_corrupt_courses_table_read_by_dropdown_is_visible(cpath):
    """课程表损坏时下拉不该显示成「0 门课」的假空状态：GET 必须带回 error 字段。"""
    cpath.write_text("{ broken", encoding="utf-8")
    async with asgi_client() as c:
        body = (await c.get("/api/courses")).json()
    assert body["courses"] == [] and body["error"] and body["detail"]


def test_repo_courses_json_untouched(repo_courses_guard):
    """本文件跑完，仓库真实 courses.json 未被创建/改写（conftest 会话级守卫）。"""
    repo_courses_guard()
