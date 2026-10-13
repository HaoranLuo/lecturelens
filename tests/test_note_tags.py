# -*- coding: utf-8 -*-
"""tests/test_note_tags.py — §8 标签（离线秒级）。

交付面（docs/notes-batch.md §8）：标签存**笔记文件头块**的一行 `> 标签：#A #B`
（存文件不存 sidecar）；`notes.note_tags()` 容错解析；`POST /api/notes/tags`
{file, tags[]} **全量替换**（幂等），行不存在则插到课程行下方；规则 ≤16 字符、
禁空格/全角空格/#/换行、每篇 ≤20、同名去重；`/api/notes` 索引每条返回 tags +
全局标签清单与计数；UI 三入口（编辑面板 chips / 笔记库筛选栏 / 字幕页 🏷）。

三层：notes.py 纯函数 / 端点行为 / index.html 结构断言。
"""
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

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


def write_note(name: str, text: str = NOTE) -> Path:
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def global_hub():
    hub = main.hub
    saved = (hub.transcript_path, hub.stamp, hub.t0_audio, list(hub.times))
    yield hub
    hub.transcript_path, hub.stamp = saved[0], saved[1]
    hub.t0_audio = saved[2]
    hub.times = saved[3]


# ---------------- 1) 纯函数：解析 ----------------

def test_note_tags_missing_line_is_empty():
    assert notes_mod.note_tags(NOTE) == []
    assert notes_mod.note_tags("") == []
    assert notes_mod.note_tags("随便什么正文\n") == []


def test_note_tags_parses_hash_tokens_only():
    """`#` 起始、空白分隔；未知格式（没 # 的词 / 没有冒号的行）忽略；同名去重。"""
    assert notes_mod.note_tags("> 标签：#重点 #考试\n") == ["重点", "考试"]
    assert notes_mod.note_tags("> 标签: #a #b\n") == ["a", "b"]           # 半角冒号也认
    assert notes_mod.note_tags("> 标签：#a b #c\n") == ["a", "c"]         # 无 # 的忽略
    assert notes_mod.note_tags("> 标签：#a #a #b\n") == ["a", "b"]        # 去重
    assert notes_mod.note_tags("> 标签：#" + "x" * 20 + "\n") == []       # 超长丢弃
    assert notes_mod.note_tags("> 标签 没有冒号 #a\n") == []              # 未知格式
    assert notes_mod.note_tags("> 标签：\n") == []


def test_note_tags_only_reads_head_window():
    """只认头部区块（与课程行同窗口）——正文里同形的引用行不算标签。"""
    text = "".join(f"x{i}\n" for i in range(20)) + "> 标签：#深处\n"
    assert notes_mod.note_tags(text) == []


def test_normalize_tag_rules():
    assert notes_mod.normalize_tag("重点") == "重点"
    assert notes_mod.normalize_tag("#重点") == "重点"       # 宽容：带 # 也收
    assert notes_mod.normalize_tag("  ##考试 ") == "考试"
    assert notes_mod.normalize_tag("x" * 16) == "x" * 16    # 边界长刚好合法
    for bad in ("x" * 17, "a b", "全角\u3000空格", "带#号", "换\n行", "tab\there", "", "   ",
                None, 7, ["x"]):
        assert notes_mod.normalize_tag(bad) is None, bad


def test_normalize_tags_dedupe_cap_and_errors():
    assert notes_mod.normalize_tags(["#a", "a", "b"]) == (["a", "b"], None)   # 保序去重
    assert notes_mod.normalize_tags([]) == ([], None)
    tags, err = notes_mod.normalize_tags(["ok", "bad tag"])
    assert tags is None and "bad tag" in err
    tags2, err2 = notes_mod.normalize_tags(["x"] * (notes_mod.TAG_MAX_COUNT + 1))
    assert tags2 is None and str(notes_mod.TAG_MAX_COUNT) in err2
    assert notes_mod.normalize_tags("不是数组")[1] == "tags 必须是数组"


# ---------------- 2) 纯函数：写回 ----------------

def test_set_tags_inserts_below_course_line():
    out, action = notes_mod.set_tags(NOTE, ["重点", "考试"])
    assert action == "inserted"
    lines = out.splitlines()
    i = lines.index("> 课程：未分类")
    assert lines[i + 1] == "> 标签：#重点 #考试"
    assert notes_mod.note_tags(out) == ["重点", "考试"]
    assert "hello world" in out                        # 正文零改动
    assert notes_mod.note_course(out) == "未分类"      # 课程行不受影响


def test_set_tags_falls_back_when_no_course_line():
    """老笔记没有课程行：依次退到自动生成行之后 → 标题行之后 → 文首。"""
    old = NOTE.replace("> 课程：未分类\n", "")
    out, _ = notes_mod.set_tags(old, ["没听懂"])
    assert notes_mod.note_tags(out) == ["没听懂"]
    assert out.splitlines()[2].startswith(notes_mod.AUTOGEN_PREFIX)   # 挂在自动行之后
    bare = "# 双语课堂笔记 20261013_0146\n\n正文\n"
    out2, _ = notes_mod.set_tags(bare, ["a"])
    assert out2.splitlines()[1] == "> 标签：#a"
    assert notes_mod.note_tags(out2) == ["a"]


def test_set_tags_replace_remove_idempotent_and_crlf():
    once, act1 = notes_mod.set_tags(NOTE, ["a", "b"])
    assert act1 == "inserted"
    twice, act2 = notes_mod.set_tags(once, ["a", "c"])
    assert act2 == "replaced" and notes_mod.note_tags(twice) == ["a", "c"]
    assert twice.count("> 标签：") == 1                 # 不重复插行
    same, act3 = notes_mod.set_tags(twice, ["a", "c"])
    assert act3 == "none" and same == twice             # 幂等：文本零变化
    cleared, act4 = notes_mod.set_tags(twice, [])
    assert act4 == "removed" and notes_mod.note_tags(cleared) == []
    assert cleared == NOTE                              # 清空后回到原样
    crlf, _ = notes_mod.set_tags(NOTE.replace("\n", "\r\n"), ["x"])
    assert crlf.count("\r\n") == NOTE.count("\n") + 1
    assert "\n" not in crlf.replace("\r\n", "")


def test_set_tags_keeps_body_reference_lines():
    """正文里恰好写成 `> 标签：#x` 的引用行不动（只改头部那一块；读写同窗口=前 12 行）。"""
    body = NOTE + "".join(f"填充行 {i}\n" for i in range(5)) + "\n> 标签：#正文里的引用\n"
    assert body.splitlines().index("> 标签：#正文里的引用") >= notes_mod.HEAD_SCAN
    out, _ = notes_mod.set_tags(body, ["头部"])
    assert notes_mod.note_tags(out) == ["头部"]
    assert "> 标签：#正文里的引用" in out


def test_tag_counts_sorted_by_count_then_name():
    got = notes_mod.tag_counts([
        {"tags": ["b", "a"]}, {"tags": ["b"]}, {"tags": ["c"]}, {"tags": []}])
    assert got == [{"tag": "b", "count": 2}, {"tag": "a", "count": 1},
                   {"tag": "c", "count": 1}]


# ---------------- 3) 索引：tags / 计数 / 筛选 / 检索命中 ----------------

def test_index_returns_tags_and_counts():
    write_note("lecture_20261013_0100.md",
               NOTE.replace("20261013_0146", "20261013_0100").replace(
                   "> 课程：未分类", "> 课程：未分类\n> 标签：#重点 #考试"))
    write_note("lecture_20261013_0200.md",
               NOTE.replace("20261013_0146", "20261013_0200").replace(
                   "> 课程：未分类", "> 课程：未分类\n> 标签：#重点"))
    out = notes_mod.index(main.TRANSCRIPTS)
    assert [n["tags"] for n in out["notes"]] == [["重点"], ["重点", "考试"]]
    assert out["tag_counts"] == [{"tag": "重点", "count": 2}, {"tag": "考试", "count": 1}]


def test_index_tag_filter_combines_with_course_and_query():
    a = write_note("lecture_20261013_0100.md",
                   NOTE.replace("20261013_0146", "20261013_0100").replace(
                       "> 课程：未分类", "> 课程：数学\n> 标签：#重点"))
    write_note("lecture_20261013_0200.md",
               NOTE.replace("20261013_0146", "20261013_0200").replace(
                   "> 课程：未分类", "> 课程：物理\n> 标签：#重点"))
    assert [n["name"] for n in notes_mod.index(main.TRANSCRIPTS, tag="重点")["notes"]] \
        == ["lecture_20261013_0200.md", a.name]
    only = notes_mod.index(main.TRANSCRIPTS, tag="重点", course="数学")
    assert [n["name"] for n in only["notes"]] == [a.name]
    # 计数是**级联**口径（facet）：只受 course/query 影响、不受 tag 自身影响——
    # 即"在当前这个视图里，选它能看到几篇"，数字与筛选结果自洽。
    assert only["tag_counts"] == [{"tag": "重点", "count": 1}]
    assert notes_mod.index(main.TRANSCRIPTS, tag="重点")["tag_counts"] \
        == [{"tag": "重点", "count": 2}]
    assert notes_mod.index(main.TRANSCRIPTS, tag="不存在")["notes"] == []


def test_index_search_hits_tag_name():
    write_note("lecture_20261013_0100.md",
               NOTE.replace("> 课程：未分类", "> 课程：未分类\n> 标签：#没听懂"))
    write_note("lecture_20261013_0200.md",
               NOTE.replace("20261013_0146", "20261013_0200"))
    hit = notes_mod.index(main.TRANSCRIPTS, query="没听懂")
    assert [n["name"] for n in hit["notes"]] == ["lecture_20261013_0100.md"]


# ---------------- 4) 端点 ----------------

async def test_get_tags_defaults_to_current_note(global_hub):
    global_hub.start_transcript("微积分")
    global_hub.transcript_path.write_text(
        notes_mod.set_tags(global_hub.transcript_path.read_text(encoding="utf-8"),
                           ["没听懂"])[0], encoding="utf-8")
    async with asgi_client() as c:
        body = (await c.get("/api/notes/tags")).json()      # 不给 name = 当前笔记
        assert body["ok"] and body["file"] == global_hub.transcript_path.name
        assert body["tags"] == ["没听懂"]
        assert body["presets"] == list(main.TAG_PRESETS)
        assert body["sha"] and body["mtime_us"]


async def test_get_tags_without_transcript_is_404(global_hub):
    global_hub.transcript_path = None
    async with asgi_client() as c:
        r = await c.get("/api/notes/tags")
        assert r.status_code == 404 and "笔记" in r.json()["error"]


async def test_post_tags_full_replace_roundtrip():
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags", json={"file": p.name,
                                                  "tags": ["#重点", "考试", "考试"]})
        assert r.status_code == 200
        body = r.json()
        assert body["tags"] == ["重点", "考试"] and body["action"] == "inserted"
        assert "> 标签：#重点 #考试" in p.read_text(encoding="utf-8")
        assert body["sha"] == (await c.get("/api/notes/tags",
                                           params={"name": p.name})).json()["sha"]
        # 全量替换：第二次只给一个 → 表里只剩一个（幂等：再来一次 action=none）
        r2 = await c.post("/api/notes/tags", json={"file": p.name, "tags": ["考试"]})
        assert r2.json()["tags"] == ["考试"] and r2.json()["action"] == "replaced"
        r3 = await c.post("/api/notes/tags", json={"file": p.name, "tags": ["考试"]})
        assert r3.json()["action"] == "none"
        # 清空 = 删掉那一行
        r4 = await c.post("/api/notes/tags", json={"file": p.name, "tags": []})
        assert r4.json()["action"] == "removed"
        assert "> 标签" not in p.read_text(encoding="utf-8")


async def test_post_tags_validation_and_unreachable_trash():
    p = write_note("lecture_20261013_0146.md")
    trash = main.TRANSCRIPTS / notes_mod.TRASH_DIR
    trash.mkdir(exist_ok=True)
    (trash / "lecture_20261013_0146.md").write_text(NOTE, encoding="utf-8")
    async with asgi_client() as c:
        cases = [
            ({"file": p.name, "tags": ["a b"]}, 400, "非法标签"),
            ({"file": p.name, "tags": ["x"] * (notes_mod.TAG_MAX_COUNT + 1)}, 400, "最多"),
            ({"file": p.name, "tags": "不是数组"}, 400, "必须是数组"),
            ({"file": p.name, "tags": ["ok"], "bogus": 1}, 400, "未知字段"),
            ({"file": "lecture_19990101_0000.md", "tags": ["a"]}, 400, "笔记不存在"),
            ({"file": "../config.json", "tags": ["a"]}, 400, "非法笔记名"),
            ({"file": f".trash/{p.name}", "tags": ["a"]}, 400, "非法笔记名"),
            ({"tags": ["a"]}, 400, "缺少"),
        ]
        for body, code, want in cases:
            r = await c.post("/api/notes/tags", json=body)
            assert r.status_code == code, (body, r.status_code)
            assert want in r.json()["error"], (body, r.json())
        bad = await c.post("/api/notes/tags", content=b"not json",
                           headers={"Content-Type": "application/json"})
        assert bad.status_code == 400
    assert "> 标签" not in p.read_text(encoding="utf-8")       # 全被拒 → 文件没被碰
    assert "> 标签" not in (trash / "lecture_20261013_0146.md").read_text(encoding="utf-8")


async def test_post_tags_on_live_note_keeps_body(global_hub):
    """正在记录的那篇也能打标签（上课场景）：正文段一条不丢。"""
    global_hub.start_transcript("微积分")
    p = global_hub.transcript_path
    global_hub.append("10:00:01", "derivative", "导数", 1)
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags", json={"file": p.name, "tags": ["没听懂"]})
        assert r.status_code == 200 and r.json()["tags"] == ["没听懂"]
    text = p.read_text(encoding="utf-8")
    assert "derivative" in text and "导数" in text
    assert notes_mod.note_tags(text) == ["没听懂"]


async def test_list_endpoint_exposes_tags_counts_and_filter():
    write_note("lecture_20261013_0100.md",
               NOTE.replace("20261013_0146", "20261013_0100").replace(
                   "> 课程：未分类", "> 课程：未分类\n> 标签：#重点"))
    write_note("lecture_20261013_0200.md",
               NOTE.replace("20261013_0146", "20261013_0200"))
    async with asgi_client() as c:
        body = (await c.get("/api/notes/list")).json()
        assert body["tag_counts"] == [{"tag": "重点", "count": 1}]
        assert [n["tags"] for n in body["notes"]] == [[], ["重点"]]
        filtered = (await c.get("/api/notes/list", params={"tag": "重点"})).json()
        assert [n["name"] for n in filtered["notes"]] == ["lecture_20261013_0100.md"]
        assert filtered["matched"] == 1 and filtered["tag"] == "重点"
        # 检索也命中标签名
        q = (await c.get("/api/notes/list", params={"q": "重点"})).json()
        assert [n["name"] for n in q["notes"]] == ["lecture_20261013_0100.md"]


async def test_preview_and_raw_expose_tags():
    p = write_note("lecture_20261013_0146.md",
                   NOTE.replace("> 课程：未分类", "> 课程：未分类\n> 标签：#待复习"))
    async with asgi_client() as c:
        assert (await c.get("/api/notes/preview",
                            params={"name": p.name})).json()["tags"] == ["待复习"]
        raw = (await c.get("/api/notes/raw", params={"name": p.name})).json()
        assert raw["tags"] == ["待复习"] and raw["presets"] == list(main.TAG_PRESETS)


async def test_tags_write_broadcasts_notes_event(monkeypatch):
    p = write_note("lecture_20261013_0146.md")
    sent: list = []
    real = main.hub.broadcast

    async def spy(payload):
        sent.append(payload)

    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as c:
            await c.post("/api/notes/tags", json={"file": p.name, "tags": ["重点"]})
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    assert [m for m in sent if m.get("type") == "notes"][0]["files"] == [p.name]


async def test_tags_survive_rename_and_course_move():
    """§8 与 §2/§4 互不冲突：移动不改标签；重命名不丢标签。"""
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/notes/tags", json={"file": p.name, "tags": ["重点"]})
        await c.post("/api/notes/rename", json={"file": p.name, "to": "改了名"})
        moved = "lecture_改了名.md"
        await c.post("/api/notes/course", json={"file": moved, "course": None})
        await c.post("/api/notes/course", json={"file": moved, "course": None})
        body = (await c.get("/api/notes/tags", params={"name": moved})).json()
        assert body["tags"] == ["重点"]
        listed = (await c.get("/api/notes/list")).json()
        assert listed["notes"][0]["tags"] == ["重点"]


# ---------------- 5) index.html 结构 ----------------

def test_toolbar_tag_entry():
    assert 'id="btnTag"' in HTML and "🏷" in HTML
    assert 'id="tagQuickWrap"' in HTML and "$('btnTag').onclick" in HTML
    seg = HTML.split("async function toggleTagQuick() {", 1)[1].split("function renderQuickTags", 1)[0]
    assert "jfetch('/api/notes/tags')" in seg            # 缺省 name = 当前笔记
    assert "r.presets" in seg                            # 预设由服务端给（唯一来源）


def test_quick_panel_shows_custom_tags():
    """实测踩过：面板只画预设 → 自定义标签看不见也取消不掉。锁住"自定义也要画出来"。"""
    seg = HTML.split("function renderQuickTags(name, tags) {", 1)[1].split(
        "async function quickToggleTag", 1)[0]
    assert "for (const t of tagQuickTags)" in seg
    assert "if (!seen.has(t))" in seg
    assert "tagQuickTags = (tags || []).slice();" in seg     # 现状存在变量里，不从 DOM 反解


def test_library_tag_filter_wired():
    assert 'id="nbTagFilter"' in HTML
    assert "if (NB.tag) p.set('tag', NB.tag);" in HTML
    assert "renderTagFilter();" in HTML
    assert "NB.tagCounts = r.tag_counts" in HTML
    assert "$('nbTagFilter').onchange" in HTML


def test_edit_panel_chips_and_custom_input():
    assert 'id="editTags"' in HTML and 'id="editTagNew"' in HTML
    open_seg = HTML.split("async function openEdit(name) {", 1)[1].split("async function saveEdit", 1)[0]
    assert "editTags = r.tags || []" in open_seg and "renderEditTags();" in open_seg
    assert "const TAG_PRESETS = " not in HTML            # 预设不写死在前端（服务端给）
    assert "let TAG_PRESETS = [" in HTML                 # 只有兜底一份


def test_edit_tag_change_keeps_editor_line_in_sync():
    """改标签后必须同步编辑框里那一行，否则接着「保存」会把标签写回去。"""
    assert "function syncTagLine(text, tags) {" in HTML
    body = HTML.split("async function editToggleTag(tag) {", 1)[1].split(
        "async function editAddTag", 1)[0]
    assert "editTarget.sha = r.sha" in body
    assert "$('editText').value = syncTagLine($('editText').value, r.tags);" in body


def test_preview_and_row_show_tags():
    assert "nbTagBadge" in HTML and "n.tags.length" in HTML
    seg = HTML.split("const tagLine = document.createElement('div');", 1)[1][:600]
    assert "tagChipEl(t, true" in seg and "toggleNoteTag(d.name" in seg


def test_tag_styles_use_tokens():
    block = HTML.split("  .tagChip { display:inline-flex;", 1)[1].split("body:has(#tagQuickWrap.on)", 1)[0]
    assert "animation" not in block and "transition" not in block
    assert "var(--gold-dim)" in block and "var(--gold) 50%, transparent" in block


# ---------------- 6) 评审修复回归（M3 字符集 / M4 行尾 / R2 前端接线） ----------------

def test_normalize_tag_blocks_all_whitespace_and_control():
    """评审 M3：只拦 '\\n\\r\\t' 时 \\u2028 等能过关——标签被当**裸行**写进笔记头，
    文件格式当场被用户输入破坏（POST 回显与随后 GET 自相矛盾）。现在按 isspace()
    与 ord<32 一票否决，零宽字符另列黑名单。"""
    for bad in ("a\u2028b", "a\u2029b", "a\x0bb", "a\x0cb", "a\x85b", "a\x00b",
                "a\u200bb", "a\u3000b", "a\xa0b", "a b", "a\tb", "a\nb"):
        assert notes_mod.normalize_tag(bad) is None, repr(bad)
    assert notes_mod.normalize_tag("重点") == "重点"
    assert notes_mod.normalize_tag("  #重点 ") == "重点"


async def test_post_tags_echo_matches_get_after_write():
    """回显必须是**从文件解析出来的**标签表：POST 的 tags 与随后 GET 的 tags 相等
    （评审 M3 的第二半：旧实现回显提交值，一旦写盘与提交值有出入就自相矛盾）。"""
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags",
                         json={"file": p.name, "tags": ["#重点", "考试", "考试"]})
        assert r.status_code == 200
        got = (await c.get("/api/notes/tags", params={"name": p.name})).json()
        assert r.json()["tags"] == got["tags"] == ["重点", "考试"]
        assert r.json()["sha"] == got["sha"]


async def test_post_tags_rejects_weird_whitespace():
    """端到端：\\u2028 这类输入必须 400（不再能写坏笔记头）。"""
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags",
                         json={"file": p.name, "tags": ["a\u2028b", "c"]})
        assert r.status_code == 400 and "非法标签" in r.json()["error"]
    assert "> 标签" not in p.read_text(encoding="utf-8")


def _write_raw(name: str, text: str, newline: str) -> Path:
    """按指定行尾原样落盘（write_text 会按 os.linesep 展开，测行尾保真不能用它）。"""
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    with p.open("w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", "\r\n") if newline == "\r\n" else text)
    return p


async def test_surgical_edits_preserve_lf_endings():
    """评审 M4：LF 笔记改归属/打标签后仍是 LF（不被 os.linesep 翻成 CRLF）。"""
    lf = NOTE.replace("\r\n", "\n")
    p = _write_raw("lecture_20261013_0146.md", lf, "\n")
    assert b"\r" not in p.read_bytes()
    async with asgi_client() as c:
        await c.post("/api/notes/course", json={"file": p.name, "course": None})
        assert (await c.post("/api/notes/course",
                             json={"file": p.name, "course": None})).json()["action"] \
            == "none"                                    # 幂等（没被行尾差异骗到）
        assert (await c.post("/api/notes/tags",
                             json={"file": p.name, "tags": ["重点"]})).json()["action"] \
            == "inserted"
    raw = p.read_bytes()
    assert b"\r" not in raw                              # 一个 CR 都没有
    assert b"> \xe6\xa0\x87\xe7\xad\xbe\xef\xbc\x9a#\xe9\x87\x8d\xe7\x82\xb9" in raw
    assert notes_mod.note_tags(p.read_text(encoding="utf-8")) == ["重点"]


async def test_surgical_edits_preserve_crlf_endings():
    """CRLF 笔记：改归属/打标签后仍是纯 CRLF（不出现 \\r\\r\\n，也不混排 LF）。"""
    p = _write_raw("lecture_20261013_0146.md", NOTE, "\r\n")
    before = p.read_bytes().count(b"\r\n")
    async with asgi_client() as c:
        await c.post("/api/notes/course", json={"file": p.name, "course": None})
        await c.post("/api/notes/tags", json={"file": p.name, "tags": ["重点"]})
    raw = p.read_bytes()
    assert raw.count(b"\r\n") == before + 1              # 只多了标签那一行
    assert b"\r\r\n" not in raw
    assert raw.count(b"\n") - raw.count(b"\r\n") == 0    # 无裸 LF


async def test_put_save_normalizes_to_platform_endings():
    """整篇保存（编辑面板）口径：按平台行尾落盘（Windows=CRLF）——浏览器 textarea
    的 value 由 DOM 规范成 \\n，逐字节回放本就不可能；这条口径在 docstring 写明。"""
    lf = NOTE.replace("\r\n", "\n")
    p = _write_raw("lecture_20261013_0146.md", lf, "\n")
    async with asgi_client() as c:
        sha = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        assert (await c.put(f"/api/notes/{p.name}",
                            json={"text": lf, "base_sha": sha})).status_code == 200
    raw = p.read_bytes()
    assert raw.count(b"\n") - raw.count(b"\r\n") == 0    # 归一成平台行尾
    assert p.read_text(encoding="utf-8") == lf           # 文本内容不变


def test_no_python_only_string_methods_in_js():
    """评审2 硬故障：`val.lstrip('#'` 是 Python 方法，JS 里没有 → 自定义标签入口
    静默死掉（onclick 不接 promise，只在控制台留 unhandled rejection）。"""
    offenders = [ln for ln in HTML.splitlines()
                 if ".lstrip(" in ln and not ln.strip().startswith("//")]
    assert not offenders, offenders                    # 注释里可以提，代码里不许有
    assert "replace(/^#+/, '')" in HTML                  # 改成 JS 口径的归一化


def test_trash_loading_and_error_states():
    """评审2：回收站取不到不说成"确实是空的"，且失败也要把弹窗打开。"""
    seg = HTML.split("async function refreshTrash() {", 1)[1].split(
        "async function openTrash", 1)[0]
    assert "trashLoaded" in seg                          # 载入态判据
    assert "回收站读取失败" in seg                        # 错误态（不是"是空的"）
    assert "try {" in seg and "catch" in seg             # fetch 失败不抛穿
    openseg = HTML.split("async function openTrash() {", 1)[1].split("async function trashRestore", 1)[0]
    assert "try {" in openseg and "openModal($('trashMask'))" in openseg


def test_toast_above_dialogs():
    """评审2：toast 必须在遮罩之上——本批新增弹窗遮罩 z-index 100/150/200，
    原 99 会让弹窗内的错误提示压在 55% 黑幕下（只剩 ~45% 亮度）。"""
    seg = HTML.split("#toast {", 1)[1].split("}", 1)[0]
    z = int(re.search(r"z-index:(\d+)", seg).group(1))
    assert z >= 300, f"toast z-index={z} 仍在遮罩之下"
    for mask_css in ("#aboutMask {", ".dlgMask {", "#wizardMask {"):
        if mask_css in HTML:
            mz = int(re.search(r"z-index:(\d+)",
                               HTML.split(mask_css, 1)[1].split("}", 1)[0]).group(1))
            assert z > mz, (mask_css, mz)


def test_tag_chain_reports_network_failure():
    """评审2：标签写入链路（含打开编辑面板/回收站）不能静默无反应。"""
    for fn in ("async function postTags(name, tags) {",
               "async function toggleTagQuick() {",
               "async function openEdit(name) {"):
        seg = HTML.split(fn, 1)[1].split("\n}", 1)[0]
        assert "catch" in seg, fn
    assert "标签保存失败：服务无响应" in HTML


def test_empty_state_covers_tag_filter():
    """评审2：只有标签筛选时不许说"还没有笔记。上一节课后会自动出现"。"""
    seg = HTML.split("emptySlot(box, 'nbEmpty', 'notes',", 1)[1].split(");", 1)[0]
    assert "NB.tag" in seg
    assert "课程/标签筛选" in seg
