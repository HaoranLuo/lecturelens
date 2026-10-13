# -*- coding: utf-8 -*-
"""tests/test_note_course_move.py — §2 课后改归属（离线秒级）。

交付面（docs/notes-batch.md §2）：`POST /api/notes/course` {file, course} **正式改写
笔记头的课程行**（定稿口径，与课中切课"追加切换标记"两套语义）；`POST /api/notes/course/batch`
{files[], course}；路径走 notes.safe_note_path；文件不存在/课程不存在 → 400 明确文案；
改写失败不改动原文件（先读后整体写回）。

三层：
1) notes.set_course 纯函数（替换/插入/行尾/窗口/切换标记不动/幂等）；
2) courses.resolve_target（id / 课程名 / null）；
3) 端点行为（单篇/批量/400 文案/先校验后落盘/写入失败不落盘/广播）；
4) index.html 的结构断言（多选、移动到弹窗、模态基建白名单、刷新收尾）。
真机行为（Chrome 点选移动、另一窗口同步）在实施报告里；回归靠本文件 + smoke [5]。
"""
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import courses as courses_mod
import main
import notes as notes_mod

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

NOTE = ("# 双语课堂笔记 20261013_0146\n\n"
        "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
        "> 课程：未分类\n\n---\n\n"
        "- `10:00:01` hello world\n  - `#1` 你好世界\n")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def cpath(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    return main.COURSES_PATH


def write_note(name: str, text: str = NOTE) -> Path:
    """在（conftest 重定向过的）transcripts 里落一篇笔记。"""
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------- 1) 纯函数：notes.set_course ----------------

def test_set_course_replaces_head_line_and_keeps_body():
    out, action = notes_mod.set_course(NOTE, "数学")
    assert action == "replaced"
    assert notes_mod.note_course(out) == "数学"
    assert "hello world" in out and "#1" in out          # 正文零改动
    assert out.count(courses_mod.COURSE_HEADER_PREFIX) == 1


def test_set_course_preserves_crlf():
    crlf = NOTE.replace("\n", "\r\n")
    out, action = notes_mod.set_course(crlf, "数学")
    assert action == "replaced"
    assert "\r\n" in out and out.count("\r\n") == crlf.count("\r\n")
    assert "\n" not in out.replace("\r\n", "")           # 不混排


def test_set_course_inserts_when_head_line_missing():
    """p2-1 之前的老笔记（有自动生成行、无课程行）→ 插在自动生成行之后。"""
    old = NOTE.replace("> 课程：未分类\n", "")
    out, action = notes_mod.set_course(old, "信号与系统")
    assert action == "inserted"
    assert notes_mod.note_course(out) == "信号与系统"
    lines = out.splitlines()
    assert lines[2].startswith(notes_mod.AUTOGEN_PREFIX)
    assert lines[3] == f"{courses_mod.COURSE_HEADER_PREFIX}信号与系统"
    assert "hello world" in out


def test_set_course_inserts_after_title_without_autogen_line():
    manual = "# 双语课堂笔记 20261013_0146\n\n正文第一行\n"
    out, action = notes_mod.set_course(manual, "数学")
    assert action == "inserted"
    assert notes_mod.note_course(out) == "数学"
    assert out.splitlines()[0].startswith("# 双语课堂笔记")   # 标题仍居首
    assert "正文第一行" in out


def test_set_course_keeps_body_switch_markers():
    """正文里的历史切换标记是"当时确实切过课"的事实，定稿改写不得抹掉它。"""
    text = NOTE + "\n> 课程：线性代数\n\n- `10:05:00` later\n"
    out, _ = notes_mod.set_course(text, "微积分")
    assert notes_mod.note_course(out) == "微积分"          # 头部（主归属）改了
    assert f"{courses_mod.COURSE_HEADER_PREFIX}线性代数" in out   # 正文标记保留
    assert "later" in out


def test_set_course_only_touches_head_window():
    """课程行只认头部区块（前 HEAD_SCAN 行，与 courses.note_course 同窗口）。"""
    far = "".join(["x\n"] * 30) + f"{courses_mod.COURSE_HEADER_PREFIX}深处\n"
    out, action = notes_mod.set_course(far, "数学")
    assert action == "inserted"                            # 没去改第 31 行那行
    assert f"{courses_mod.COURSE_HEADER_PREFIX}深处" in out
    assert notes_mod.note_course(out) == "数学"            # 但解析必得命中新写的行
    assert out.splitlines()[0] == f"{courses_mod.COURSE_HEADER_PREFIX}数学"


def test_set_course_is_idempotent():
    once, _ = notes_mod.set_course(NOTE, "数学")
    twice, action = notes_mod.set_course(once, "数学")
    assert twice == once and action == "replaced"          # 文本零变化 → 端点据此返回 none


def test_set_course_handles_empty_and_junk():
    out, action = notes_mod.set_course("", "数学")
    assert action == "inserted" and notes_mod.note_course(out) == "数学"
    out2, _ = notes_mod.set_course("杂乱内容\n", "数学")
    assert notes_mod.note_course(out2) == "数学"


def test_set_course_blank_falls_back_to_uncategorized():
    """空/None 归属兜底成「未分类」——纯函数自证安全，绝不把字面量 None 写进笔记头。"""
    for blank in (None, ""):
        out, _ = notes_mod.set_course(NOTE, blank)
        assert notes_mod.note_course(out) == courses_mod.UNCATEGORIZED
        assert "None" not in out


# ---------------- 2) 纯函数：courses.resolve_target ----------------

def _data(*courses):
    return {"active": None, "courses": [
        {"id": i, "name": n, "color": "#93a1b0", "glossary": None} for i, n in courses]}


def test_resolve_target_id_name_and_null():
    data = _data(("cs101", "计算机科学导论"), ("math", "数学"))
    assert courses_mod.resolve_target(data, "cs101") == ("计算机科学导论", None)
    assert courses_mod.resolve_target(data, "数学") == ("数学", None)
    assert courses_mod.resolve_target(data, "  数学  ") == ("数学", None)   # 去空白
    assert courses_mod.resolve_target(data, None) == (courses_mod.UNCATEGORIZED, None)
    assert courses_mod.resolve_target(data, "") == (courses_mod.UNCATEGORIZED, None)
    assert courses_mod.resolve_target(data, courses_mod.UNCATEGORIZED) \
        == (courses_mod.UNCATEGORIZED, None)


def test_resolve_target_rejects_unknown_and_wrong_type():
    data = _data(("cs101", "计算机科学导论"))
    name, err = courses_mod.resolve_target(data, "不存在课")
    assert name is None and "课程不存在" in err and "不存在课" in err
    name2, err2 = courses_mod.resolve_target(data, 7)
    assert name2 is None and "course 须为" in err2


def test_resolve_target_id_wins_over_same_name():
    """有人把课程名取成另一门课的 id：落到 id 那门（可预期、可核对，不做模糊匹配）。"""
    data = _data(("cs101", "计算机科学导论"), ("数学", "cs101"))
    assert courses_mod.resolve_target(data, "cs101") == ("计算机科学导论", None)


# ---------------- 3) 端点：单篇 / 批量 ----------------

async def test_single_move_replaces_header_and_index_follows(cpath):
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "数学"})).json()["course"]["id"]
        r = await c.post("/api/notes/course", json={"file": p.name, "course": cid})
        assert r.status_code == 200
        body = r.json()
        assert body == {"ok": True, "file": p.name, "course": "数学", "action": "replaced"}
        # 笔记头改了，索引（列表接口）跟着反映 → 前端列表徽章/侧栏计数据此刷新
        lst = (await c.get("/api/notes/list")).json()
        assert lst["notes"][0]["course"] == "数学"
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) == "数学"
    assert "hello world" in p.read_text(encoding="utf-8")


async def test_move_round_trip_to_uncategorized(cpath):
    """改回「未分类」同样是一等操作（归错能改，含改回来）。"""
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "数学"})).json()["course"]["id"]
        await c.post("/api/notes/course", json={"file": p.name, "course": cid})
        r = await c.post("/api/notes/course", json={"file": p.name, "course": None})
        assert r.status_code == 200 and r.json()["course"] == courses_mod.UNCATEGORIZED
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) \
        == courses_mod.UNCATEGORIZED


async def test_move_accepts_course_name(cpath):
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "线性代数"})
        r = await c.post("/api/notes/course", json={"file": p.name, "course": "线性代数"})
        assert r.status_code == 200 and r.json()["course"] == "线性代数"


async def test_move_old_note_inserts_line(cpath):
    old_text = NOTE.replace("> 课程：未分类\n", "")
    p = write_note("lecture_20261013_0146.md", old_text)
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        assert (await c.post("/api/notes/course",
                             json={"file": p.name, "course": "数学"})).json()["action"] \
            == "inserted"
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) == "数学"


async def test_move_errors_are_400_with_clear_text(cpath):
    """文件不存在 / 课程不存在 / 非法名 / 未知键 / 非 JSON —— 全部 400 + 明确文案。"""
    write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        cases = [
            ({"file": "lecture_20260101_0000.md", "course": "数学"}, "笔记不存在"),
            ({"file": "../../.env", "course": "数学"}, "非法笔记名"),
            ({"file": "config.json", "course": "数学"}, "非法笔记名"),
            ({"file": "lecture_20261013_0146.md", "course": "幽灵课"}, "课程不存在"),
            ({"file": "lecture_20261013_0146.md", "course": 7}, "course 须为"),
            ({"file": "lecture_20261013_0146.md", "file2": "x", "course": "数学"}, "未知字段"),
            ({"course": "数学"}, "缺少 file"),
        ]
        for body, want in cases:
            r = await c.post("/api/notes/course", json=body)
            assert r.status_code == 400, (body, r.status_code)
            assert want in r.json()["error"], (body, r.json())
        bad = await c.post("/api/notes/course", content=b"not json",
                           headers={"Content-Type": "application/json"})
        assert bad.status_code == 400
        assert (await c.post("/api/notes/course", json=[1, 2])).status_code == 400


async def test_move_refuses_corrupt_courses_table(cpath):
    """课程表损坏时绝不猜归属（写盘会把空表覆盖真表，与 /api/courses* 同款守卫：
    409 + 同一句提示——这里刻意与「请求本身的问题」的 400 区分开）。"""
    p = write_note("lecture_20261013_0146.md")
    cpath.write_text("{ broken", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.post("/api/notes/course", json={"file": p.name, "course": "数学"})
        assert r.status_code == 409 and "损坏" in r.json()["error"]
        r2 = await c.post("/api/notes/course/batch",
                          json={"files": [p.name], "course": "数学"})
        assert r2.status_code == 409
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) \
        == courses_mod.UNCATEGORIZED          # 原文件未被碰


async def test_batch_move_and_back(cpath):
    a = write_note("lecture_20261013_0100.md")
    b = write_note("lecture_20261013_0200.md")
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "数学"})).json()["course"]["id"]
        r = await c.post("/api/notes/course/batch",
                         json={"files": [a.name, b.name], "course": cid})
        assert r.status_code == 200
        body = r.json()
        assert body["moved"] == 2 and body["failed"] == []
        assert {x["file"] for x in body["results"]} == {a.name, b.name}
        back = await c.post("/api/notes/course/batch",
                            json={"files": [a.name, b.name], "course": None})
        assert back.json()["moved"] == 2
    for p in (a, b):
        assert courses_mod.note_course(p.read_text(encoding="utf-8")) \
            == courses_mod.UNCATEGORIZED


async def test_batch_is_all_or_nothing(cpath):
    """先校验后落盘：一篇不存在 → 整单 400，**已存在的那篇也不许动**。"""
    a = write_note("lecture_20261013_0100.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        r = await c.post("/api/notes/course/batch",
                         json={"files": [a.name, "lecture_20260101_0000.md"], "course": "数学"})
        assert r.status_code == 400 and "笔记不存在" in r.json()["error"]
    assert courses_mod.note_course(a.read_text(encoding="utf-8")) \
        == courses_mod.UNCATEGORIZED
    assert not list(main.TRANSCRIPTS.glob("*.tmp"))       # 半个字也没落盘


async def test_batch_caps_size_and_dedupes(cpath):
    a = write_note("lecture_20261013_0100.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        too_many = [f"lecture_20260101_{i:04d}.md" for i in range(main.NOTE_MOVE_MAX + 1)]
        r = await c.post("/api/notes/course/batch", json={"files": too_many, "course": "数学"})
        assert r.status_code == 400 and str(main.NOTE_MOVE_MAX) in r.json()["error"]
        # 重复列同一篇：去重（幂等），不报错也不算两次
        r2 = await c.post("/api/notes/course/batch",
                          json={"files": [a.name, a.name], "course": "数学"})
        assert r2.status_code == 200 and r2.json()["moved"] == 1


async def test_batch_empty_or_bad_shape(cpath):
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        assert (await c.post("/api/notes/course/batch",
                             json={"files": [], "course": "数学"})).status_code == 400
        assert (await c.post("/api/notes/course/batch",
                             json={"course": "数学"})).status_code == 400


async def test_write_failure_keeps_original_and_no_tmp(cpath, monkeypatch):
    """写盘失败（模拟磁盘故障）→ 500 + 原文零改动 + 不留 .tmp 残骸。"""
    p = write_note("lecture_20261013_0146.md")
    before = p.read_text(encoding="utf-8")
    real_open = Path.open

    def boom_open(self, *a, **k):
        # 注入点在**临时文件**的写打开上（M4 后 note_atomic_write 用 open(newline='')
        # 而非 write_text 落盘，模拟故障要跟着实现的落盘方式走）。
        # 判据写成「.tmp 结尾」而不是钉死旧的 `<名>.md.tmp`：临时名已唯一化
        # （`.<笔记名>.<pid>.<随机段>.tmp`，见 main.note_atomic_write 注释），
        # 钉死旧名会让这个用例静默失去覆盖（注入不中 → 请求反而 200）。
        if self.name.endswith(".tmp") and (a and "w" in str(a[0])):
            raise OSError("disk full (simulated)")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", boom_open)
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        r = await c.post("/api/notes/course",
                         json={"file": p.name, "course": "数学"})
    assert r.status_code == 500 and "原文件未改动" in r.json()["error"]
    assert p.read_text(encoding="utf-8") == before
    assert not list(main.TRANSCRIPTS.glob("*.tmp"))


async def test_successful_move_leaves_no_tmp(cpath):
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        await c.post("/api/notes/course", json={"file": p.name, "course": "数学"})
    assert not list(main.TRANSCRIPTS.glob("*.tmp"))


async def test_move_broadcasts_notes_event(cpath, monkeypatch):
    """§2：改完必须广播（另一窗口的笔记库据此刷新）——载荷形状在此钉住。"""
    p = write_note("lecture_20261013_0146.md")
    sent: list = []
    real = main.hub.broadcast

    async def spy(payload):
        sent.append(payload)

    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as c:
            await c.post("/api/courses", json={"name": "数学"})
            await c.post("/api/notes/course", json={"file": p.name, "course": "数学"})
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    notes_frames = [m for m in sent if m.get("type") == "notes"]
    assert notes_frames and notes_frames[0]["files"] == [p.name]


async def test_move_is_noop_when_already_same_course(cpath):
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        await c.post("/api/notes/course", json={"file": p.name, "course": "数学"})
        mtime = p.stat().st_mtime_ns
        r = await c.post("/api/notes/course", json={"file": p.name, "course": "数学"})
        assert r.json()["action"] == "none"
        assert p.stat().st_mtime_ns == mtime      # 没白写一遍盘


async def test_move_current_recording_note_keeps_appended_body(cpath):
    """正在记录的笔记也能改归属（下课后最常见的场景）：已落盘的正文段不会丢。"""
    hub = main.hub
    saved = (hub.transcript_path, hub.stamp, hub.t0_audio, list(hub.times))
    try:
        hub.start_transcript("微积分")
        hub.append("10:00:01", "derivative", "导数", 1)
        async with asgi_client() as c:
            await c.post("/api/courses", json={"name": "数学"})
            r = await c.post("/api/notes/course",
                             json={"file": hub.transcript_path.name, "course": "数学"})
            assert r.status_code == 200 and r.json()["action"] == "replaced"
        text = hub.transcript_path.read_text(encoding="utf-8")
        assert courses_mod.note_course(text) == "数学"
        assert "derivative" in text and "导数" in text
    finally:
        hub.transcript_path, hub.stamp = saved[0], saved[1]
        hub.t0_audio = saved[2]
        hub.times = saved[3]


# ---------------- 4) index.html 结构 ----------------

def test_note_row_has_pick_and_move():
    body = HTML.split("function noteRow(n) {", 1)[1].split("\n}", 1)[0]
    assert "nbPicked" in body and "cb.type = 'checkbox'" in body
    assert "移动到…" in body and "openMoveDialog([n.name])" in body
    assert "nbMove danger" in body and "delNote([n.name])" in body      # §4 起行内也有删除
    assert "wrap.append(pick, b, mv, del)" in body


def test_batch_bar_and_modal_wired():
    assert 'class="nbBatch" id="nbBatch"' in HTML
    assert "$('nbBatchMove').onclick" in HTML and "$('nbBatchClear').onclick" in HTML
    assert 'id="moveMask"' in HTML and 'id="moveList"' in HTML
    assert 'id="moveNewName"' in HTML and "$('moveNewGo').onclick" in HTML
    # 交付面：预览页头部入口
    assert "bMove.onclick = () => openMoveDialog([d.name]);" in HTML


def test_move_posts_right_endpoint_per_size():
    body = HTML.split("async function applyMove(courseId) {", 1)[1].split("\n}", 1)[0]
    assert "'/api/notes/course'" in body and "'/api/notes/course/batch'" in body
    assert "files.length === 1" in body
    assert "JSON.stringify({ file: files[0], course: courseId })" in body
    assert "JSON.stringify({ files, course: courseId })" in body


def test_move_dialog_uses_shared_modal_infra():
    """弹窗必须走统一入口（焦点/aria/inert/Escape）——遮罩白名单现在是共用一份
    MASKS（inert 与 Escape 都读它），六个遮罩一个不能少；漏一个的后果是「背景
    inert 把弹窗自己也关在门外」（点了没反应）。"""
    masks = HTML.split("const MASKS = [", 1)[1].split("];", 1)[0]
    for mid in ("aboutMask", "wizardMask", "moveMask", "editMask", "renameMask", "trashMask"):
        assert f"'{mid}'" in masks, mid
    assert "MASKS.includes(el.id)" in HTML
    assert "openModal($('moveMask'))" in HTML and "closeModal($('moveMask'))" in HTML


def test_notes_ws_event_refreshes_library():
    at = HTML.index("if (m.type === 'notes') {")
    seg = HTML[at:at + 320]
    assert "refreshCourses()" in seg
    assert "refreshNotes()" in seg


def test_move_refreshes_list_and_sidebar():
    body = HTML.split("async function afterNotesChanged() {", 1)[1].split("\n}", 1)[0]
    assert "await refreshNotes();" in body          # 列表徽章
    assert "await refreshCourses();" in body        # 侧栏课程计数
    assert "await openNote(sel);" in body           # 预览头部跟着改


def test_move_render_is_textcontent_only():
    """课程名/文件名都是用户输入：弹窗与列表一律 textContent，不拼 innerHTML。
    （只允许 `innerHTML = ''` 清空容器——那不是注入面。）"""
    body = HTML.split("function renderMoveList() {", 1)[1].split("\n}", 1)[0]
    assert "n.textContent = label;" in body
    assert body.count("innerHTML") == body.count("innerHTML = ''")
    assert "d.style.background = color;" in body    # 课程色只做 8px 圆点（同侧栏口径）


def test_move_styles_use_tokens_no_new_animation():
    block = HTML.split(".nbRowWrap { display:flex;", 1)[1].split(".nbRowTop {", 1)[0]
    assert "animation" not in block and "transition" not in block   # §5：不加动效
    # §一-4 语义令牌：只允许 §六 明文规定的「主要按钮 gold 底 + #1A1408 字」这一个字面量
    assert not re.search(r":#[0-9a-fA-F]{3,6}", block.replace("#1A1408", "")), \
        "行内多选样式不得写死色值（§一-4 语义令牌）"
    assert "var(--gold-dim)" in block               # 选中态走金色语义令牌（§六）
    assert "var(--surface-2)" in block and "var(--radius)" in block
