# -*- coding: utf-8 -*-
"""tests/test_note_tags.py — §8 标签（离线秒级）。

交付面（docs/notes-batch.md §8）：标签存**笔记文件头块**的一行 `> 标签：#A #B`
（存文件不存 sidecar）；`notes.note_tags()` 容错解析；`POST /api/notes/tags`
{file, tags[]} **全量替换**（幂等），行不存在则插到课程行下方；规则 ≤16 字符、
禁空格/全角空格/#/换行、每篇 ≤20、同名去重；`/api/notes` 索引每条返回 tags +
全局标签清单与计数；UI 三入口（编辑面板 chips / 笔记库筛选栏 / 字幕页 🏷）。

三层：notes.py 纯函数 / 端点行为 / index.html 结构断言。
"""
import json
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
        assert body["presets"] == main.tag_presets()
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
        assert raw["tags"] == ["待复习"] and raw["presets"] == main.tag_presets()


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


# ---------------- 7) A2：批量打标签（端点） ----------------

def test_merge_tags_pure():
    """合并口径（纯函数）：add 保序幂等（已有不重复加），remove 只筛命中项。"""
    assert main.merge_tags([], ["重点"], "add") == ["重点"]
    assert main.merge_tags(["重点"], ["重点", "考试"], "add") == ["重点", "考试"]
    assert main.merge_tags(["重点", "考试"], ["重点"], "add") == ["重点", "考试"]
    assert main.merge_tags(["重点", "考试"], ["重点"], "remove") == ["考试"]
    assert main.merge_tags(["重点"], ["没听懂"], "remove") == ["重点"]
    assert main.merge_tags([], ["重点"], "remove") == []


def note_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


async def test_tags_batch_add_then_remove_roundtrip():
    """A2 主链：一次给多篇加一组标签 → 读回（**以文件为准**）→ 再去掉其中一条。

    读回走 /api/notes/file 而不是只看响应体：标签是写进笔记头那行的，只有盘上的
    文件才是事实来源。"""
    a = write_note("lecture_20261013_0100.md")
    b = write_note("lecture_20261013_0200.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags/batch",
                         json={"files": [a.name, b.name], "tags": ["重点", "考试"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["mode"] == "add" and body["changed"] == 2
        assert body["failed"] == [] and body["tags"] == ["重点", "考试"]
        assert {x["file"] for x in body["results"]} == {a.name, b.name}
        assert all(x["action"] == "inserted" for x in body["results"])
        assert body["presets"] == main.tag_presets()
        back = [notes_mod.note_tags((await c.get("/api/notes/file",
                                                params={"name": n})).text)
                for n in (a.name, b.name)]
        assert back == [["重点", "考试"], ["重点", "考试"]]
        # 再删掉其中一条：只剩「重点」
        r2 = await c.post("/api/notes/tags/batch",
                          json={"files": [a.name, b.name], "tags": ["考试"],
                                "mode": "remove"})
        assert r2.status_code == 200 and r2.json()["changed"] == 2
        assert notes_mod.note_tags(note_text(a)) == ["重点"]
        assert notes_mod.note_tags(note_text(b)) == ["重点"]
        # 幂等：再删一次同一条（已经没有了）→ action=none（文件没动），changed 仍如实计数
        r3 = await c.post("/api/notes/tags/batch",
                          json={"files": [a.name, b.name], "tags": ["考试"],
                                "mode": "remove"})
        assert r3.json()["changed"] == 2
        assert all(x["action"] == "none" for x in r3.json()["results"])
        # 删到空：整行移除（不是留一行空标签）
        r4 = await c.post("/api/notes/tags/batch",
                          json={"files": [a.name], "tags": ["重点"], "mode": "remove"})
        assert r4.json()["results"][0]["action"] == "removed"
        assert "> 标签" not in note_text(a)
    assert "hello world" in note_text(a)          # 正文一字不动


async def test_tags_batch_keeps_body_and_crlf():
    """行级改写纪律（与 §2/M4 同款）：只动标签那一行，正文与行尾原样保留。"""
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / "lecture_20261013_0300.md"
    p.write_bytes(NOTE.replace("\n", "\r\n").encode("utf-8"))
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags/batch",
                         json={"files": [p.name], "tags": ["重点"]})
        assert r.status_code == 200, r.text
    raw = p.read_bytes()
    assert b"\r\r\n" not in raw and raw.count(b"\n") == raw.count(b"\r\n")
    assert "hello world" in raw.decode("utf-8")


async def test_tags_batch_is_all_or_nothing_on_bad_name():
    """先全部校验、后动手：任一篇不存在 → 整单 400，**已存在的那篇也不许动**。"""
    a = write_note("lecture_20261013_0100.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags/batch",
                         json={"files": [a.name, "lecture_19990101_0000.md"],
                               "tags": ["重点"]})
        assert r.status_code == 400 and "笔记不存在" in r.json()["error"]
    assert notes_mod.note_tags(note_text(a)) == []
    assert not list(main.TRANSCRIPTS.glob("*.tmp"))       # 半个字也没落盘


async def test_tags_batch_rejects_traversal_and_trash():
    """写路径同样只认库内 lecture_*.md（与读/导出/删除同一个入口）。"""
    async with asgi_client() as c:
        for bad in ("../config.json", ".trash/lecture_x.md", "lecture_x.summary.md",
                    "config.json", 7):
            r = await c.post("/api/notes/tags/batch",
                             json={"files": [bad], "tags": ["重点"]})
            assert r.status_code == 400, (bad, r.status_code)
            assert ("非法笔记名" in r.json()["error"]
                    or "笔记不存在" in r.json()["error"]), (bad, r.json())


async def test_tags_batch_validation_400s():
    a = write_note("lecture_20261013_0100.md")
    many = [f"lecture_20260101_{i:04d}.md" for i in range(main.NOTE_MOVE_MAX + 1)]
    async with asgi_client() as c:
        cases = [
            ({"files": [a.name], "tags": ["重点"], "bogus": 1}, "未知字段"),
            ({"files": [a.name], "tags": ["重点"], "mode": "反向"}, "未知方向"),
            ({"files": [a.name], "tags": []}, "至少要给一个标签"),
            ({"files": [a.name]}, "tags 必须是数组"),
            ({"files": [a.name], "tags": ["a b"]}, "非法标签"),
            ({"files": [a.name], "tags": ["x" * 40]}, "非法标签"),
            ({"files": [], "tags": ["重点"]}, "不能为空"),
            ({"files": a.name, "tags": ["重点"]}, "不能为空"),      # 非数组
            ({"tags": ["重点"]}, "缺少 files"),
            ({"files": many, "tags": ["重点"]}, str(main.NOTE_MOVE_MAX)),
        ]
        for body, want in cases:
            r = await c.post("/api/notes/tags/batch", json=body)
            assert r.status_code == 400, (body, r.status_code)
            assert want in r.json()["error"], (body, r.json())
        bad = await c.post("/api/notes/tags/batch", content=b"not json",
                           headers={"Content-Type": "application/json"})
        assert bad.status_code == 400
        assert (await c.post("/api/notes/tags/batch", json=[1, 2])).status_code == 400
        # 同一篇重复列：去重（幂等，不报错也不算两次）
        r = await c.post("/api/notes/tags/batch",
                         json={"files": [a.name, a.name], "tags": ["重点"]})
        assert r.status_code == 200 and r.json()["changed"] == 1
    assert notes_mod.note_tags(note_text(a)) == ["重点"]


async def test_tags_batch_cap_is_per_file_not_all_or_nothing():
    """满 20 个标签的那篇 → 单篇失败（点名原因、不静默截断）；**其余篇照常打上**。"""
    full = write_note("lecture_20261013_0400.md",
                      NOTE.replace("> 课程：未分类\n",
                                   "> 课程：未分类\n> 标签："
                                   + " ".join(f"#t{i}" for i in range(20)) + "\n"))
    ok = write_note("lecture_20261013_0500.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/tags/batch",
                         json={"files": [full.name, ok.name], "tags": ["新标签"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["changed"] == 1 and len(body["failed"]) == 1
        assert body["failed"][0]["file"] == full.name
        assert "20 个标签上限" in body["failed"][0]["error"]
        assert body["results"][0]["file"] == ok.name
    assert len(notes_mod.note_tags(note_text(full))) == 20      # 没被截断也没被改写
    assert notes_mod.note_tags(note_text(ok)) == ["新标签"]


async def test_tags_batch_broadcasts_notes_once(monkeypatch):
    """一次批量 = **一帧** notes 广播（N 篇逐条广播会把别的窗口打爆）。"""
    a = write_note("lecture_20261013_0100.md")
    b = write_note("lecture_20261013_0200.md")
    sent: list = []
    real = main.hub.broadcast

    async def spy(payload):
        sent.append(payload)

    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as c:
            await c.post("/api/notes/tags/batch",
                         json={"files": [a.name, b.name], "tags": ["重点"]})
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    frames = [m for m in sent if m.get("type") == "notes"]
    assert len(frames) == 1
    assert set(frames[0]["files"]) == {a.name, b.name}


async def test_list_endpoint_carries_presets():
    """A2：预设标签随列表下发（面板 chips 的服务端来源）。

    B3 起「唯一来源」从模块常量变成 **config.tags_presets**（取值函数 main.tag_presets()）
    ——默认仍是出厂四标签，用户自定义后同一条下发路径带来新值（四个下发点的跟随
    见本文件 §9）。"""
    async with asgi_client() as c:
        body = (await c.get("/api/notes/list")).json()
    assert body["presets"] == main.tag_presets()
    assert main.DEFAULT_TAG_PRESETS == ("重点", "考试", "没听懂", "待复习")
    assert main.DEFAULT_CONFIG["tags_presets"] == list(main.DEFAULT_TAG_PRESETS)
    assert main.tag_presets() == list(main.DEFAULT_TAG_PRESETS)   # 默认 = config 的四标签


# ---------------- 8) A2：批量打标签（index.html 结构断言） ----------------

def test_batch_tag_button_and_panel_wired():
    bar = HTML.split('<div class="nbBatch" id="nbBatch">', 1)[1].split("</div>", 1)[0]
    assert 'id="nbBatchTag"' in bar and "打标签" in bar          # 与「移动到…」同级
    panel = HTML.split('<div class="nbTagBar" id="nbTagBar">', 1)[1].split("</div>", 1)[0]
    for anchor in ('id="nbTagModeSel"', 'id="nbTagChips"', 'id="nbTagNew"',
                   'id="nbTagApply"', 'id="nbTagCancel"'):
        assert anchor in panel, anchor
    assert '<option value="add">添加</option>' in panel
    assert '<option value="remove">移除</option>' in panel
    for binding in ("$('nbBatchTag').onclick", "$('nbTagApply').onclick",
                    "$('nbTagCancel').onclick", "$('nbTagModeSel').onchange",
                    "$('nbTagNew').onkeydown"):
        assert binding in HTML, binding


def test_batch_tag_panel_uses_server_presets_and_batch_endpoint():
    """预设 chips 来自服务端（TAG_PRESETS 由列表/标签响应刷新）；提交走 batch 端点，
    参数形状 {files, tags, mode}；部分失败要如实汇总。"""
    apply_fn = HTML.split("async function applyBatchTags() {", 1)[1].split("\n}", 1)[0]
    assert "'/api/notes/tags/batch'" in apply_fn
    assert "JSON.stringify({ files, tags: nbTagPick, mode })" in apply_fn
    assert "r.failed" in apply_fn and "篇失败" in apply_fn       # 部分失败如实汇总
    assert "clearNotePick();" in apply_fn and "await afterNotesChanged();" in apply_fn
    chips = HTML.split("function renderNbTagChips() {", 1)[1].split("\n}", 1)[0]
    assert "for (const t of TAG_PRESETS) add(t);" in chips
    assert "tagChipEl(t, on," in chips                            # chips 走 §8 同一套样式
    assert "TAG_PRESETS = r.presets || TAG_PRESETS;" in HTML      # 服务端为准
    assert "NB.tagCounts = r.tag_counts" in HTML


def test_custom_tag_input_mirrors_server_rules():
    """评审1 ② 回归：自定义标签输入必须按**服务端同一套规则**先拦。

    旧实现只查长度：`机器 学习` 会被收成合法样子的 chip，点「应用」才由服务端整单
    400（A2 先校验后动手 → 一篇都不动），用户白跑一趟还得回面板删掉那条。这里逐条
    对齐 notes.normalize_tag 的禁令（空白 / ord<32 / 内部 # / 零宽族）+ 长度上限，
    并把 JS 那张零宽表与服务端 TAG_INVISIBLE 常量机械对齐（两边同名同值）。"""
    fn = HTML.split("function tagProblem(t) {", 1)[1].split("\n}", 1)[0]
    assert "t.length > 16" in fn
    assert r"/\s/.test(ch)" in fn                 # 空白（JS \s 含全角空格/NBSP/\u2028\u2029）
    assert "codePointAt(0) < 32" in fn            # 控制字符
    assert "ch === '#'" in fn                     # 内部 #（开头的 # 由调用方先剥掉）
    assert "TAG_BAD_INVISIBLE.includes(ch)" in fn  # 零宽族（isspace 抓不到）
    assert notes_mod.TAG_INVISIBLE == "\u200b\u200c\u200d\u2060\ufeff"
    assert "'\\u200b\\u200c\\u200d\\u2060\\ufeff'" in HTML
    add = HTML.split("function nbTagAddCustom() {", 1)[1].split("\n}", 1)[0]
    assert "const bad = tagProblem(val);" in add and "showToast(bad, 'err')" in add
    assert "nbTagPick.push(val);" in add           # 校验不过就 return，push 在其后


def test_batch_tag_panel_dies_with_selection():
    """面板与选择同生共死：选择一空就收起（renderBatchBar 是唯一同步点）。"""
    bar = HTML.split("function renderBatchBar() {", 1)[1].split("\n}", 1)[0]
    assert "if (!nbPicked.size) closeNbTagPanel();" in bar
    close_fn = HTML.split("function closeNbTagPanel() {", 1)[1].split("\n}", 1)[0]
    assert "classList.remove('on')" in close_fn and "nbTagPick = [];" in close_fn


def test_batch_tag_styles_use_tokens_and_scope_buttons():
    """令牌纪律 + 作用域纪律：不复用 .nbBatch 类名（它的 `.nbBatch button` 会盖掉 chips
    的 999 圆角/24px 高度，面板会长出一排方 chip），按钮规则一律 `> button`。"""
    blk = HTML.split("/* —— A2：批量打标签的小面板 ——", 1)[1].split(".nbTagBar select", 1)[0]
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}", blk), "面板样式不得写死色值（§一-4）"
    assert "animation" not in blk and "transition" not in blk, "§5：不加动效"
    assert "var(--surface-2)" in blk
    whole = HTML.split("/* —— A2：批量打标签的小面板 ——", 1)[1].split("/* ——", 1)[0]
    assert ".nbTagBar > button" in whole and ".nbTagBar button {" not in whole
    assert ".nbTagBar.on { display:flex; }" in whole
    assert '<div class="nbTagBar" id="nbTagBar">' in HTML         # 不带 nbBatch 类


# ---------------- 9) B3：常用标签自定义（config.tags_presets） ----------------
# 交付面（docs/notes-batch.md B3 行）：设置页维护预设 chips（chips 增删 + 保存），
# 默认仍是 重点/考试/没听懂/待复习。落地要点（评审口径）：
#  ① config 白名单新键 tags_presets（list[str]）——**必须走专项校验**：取值是列表，
#     塞不进 CONFIG_VALUE_CHOICES，未特判键会在 set_config 里 KeyError（500 而非 400）；
#  ② main.tag_presets() 是唯一取值口径（读 runtime config，坏值回落出厂四值）；
#  ③ 四个下发点（list / raw / tags_view / batch）跟随自定义值。

@pytest.fixture
def tags_env(monkeypatch):
    """隔离 runtime['config']（POST /api/config 会就地改它）——与 test_fade_lines 同款。"""
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.runtime, "reloading", False)
    return main.runtime


def test_config_key_whitelisted_and_not_an_enum():
    """B3：白名单 + 默认值 + 「不能进枚举表」这三件事一起锁。

    最后一条是**根因防复现**：tags_presets 一旦被当成枚举键，set_config 会走到
    `CONFIG_VALUE_CHOICES[k]` 直接 KeyError——那是 500 而不是 400，且运行时与文件
    都还没动，最难查的一类故障。"""
    assert "tags_presets" in main.ALLOWED_CONFIG_KEYS
    assert main.DEFAULT_CONFIG["tags_presets"] == ["重点", "考试", "没听懂", "待复习"]
    assert "tags_presets" not in main.CONFIG_VALUE_CHOICES
    assert "tags_presets" not in main.CONFIG_NUM_RANGES
    assert main.TAG_PRESETS_MAX == 12


def test_tags_presets_norm_rules():
    """专项校验的规则表：必须 list / 1–12 条 / 逐项过 notes.normalize_tag / 去重保序。"""
    norm, err = main.tags_presets_norm(["重点", "考试"])
    assert (norm, err) == (["重点", "考试"], None)
    # 规范化：去首尾空白、剥开头的 #（宽松接受用户最自然的写法）
    assert main.tags_presets_norm(["  #重点  ", "考试"])[0] == ["重点", "考试"]
    # 去重保序（同一标签的两种写法算一条）
    assert main.tags_presets_norm(["重点", "#重点", "考试"])[0] == ["重点", "考试"]
    # 类型/长度闸
    for bad in ("重点", 5, None, {"a": 1}, ("重点",)):
        norm, err = main.tags_presets_norm(bad)
        assert norm is None and err and "tags_presets" in err, bad
    assert main.tags_presets_norm([])[1] is not None                  # 空表非法
    assert main.tags_presets_norm(["x"] * main.TAG_PRESETS_MAX)[1] is None
    assert main.tags_presets_norm(["x"] * (main.TAG_PRESETS_MAX + 1))[1] is not None
    # 逐项校验用服务端同一套规则（空白 / 控制字符 / 内部 # / 超长）
    for bad_item in ("机器 学习", "a#b", "x" * 17, "a\u2028b", "\u200b"):
        norm, err = main.tags_presets_norm(["重点", bad_item])
        assert norm is None and "非法标签" in err, bad_item
    assert main._tags_presets_problem(["重点"]) is None               # 错误文案视图同源
    assert main._tags_presets_problem("重点") is not None


def test_tag_presets_reads_config_and_falls_back_on_broken_values(tags_env):
    """取值口径：读 runtime config；**手改坏的 config.json** 整体回落出厂四值。

    config.json 是用户可手改的文件：半截坏值（字符串/混入非法项/超长表）流到前端
    只会渲染出一排看不出问题的怪 chip，故读侧宁可回到已知可用的一套。"""
    assert main.tag_presets() == ["重点", "考试", "没听懂", "待复习"]      # 默认
    tags_env["config"]["tags_presets"] = ["论文", "考试周"]
    assert main.tag_presets() == ["论文", "考试周"]                       # 自定义生效
    for broken in ("重点", ["重点", "机器 学习"], ["x"] * 99, [], None, 5):
        tags_env["config"]["tags_presets"] = broken
        assert main.tag_presets() == list(main.DEFAULT_TAG_PRESETS), broken
    # 返回的是**副本**：调用方改动不得污染 runtime config（否则下一处下发就串味）
    tags_env["config"]["tags_presets"] = ["论文"]
    got = main.tag_presets()
    got.append("污染")
    assert main.tag_presets() == ["论文"]


async def test_post_config_roundtrips_tags_presets(tags_env):
    """POST /api/config 写列表：规范化后落盘，GET /api/config 回读同一份。"""
    async with asgi_client() as c:
        r = await c.post("/api/config",
                         json={"tags_presets": ["  #论文 ", "考试周", "论文"]})
        assert r.status_code == 200, r.text
        assert r.json()["changed"]["tags_presets"] == ["论文", "考试周"]   # 规范形
        body = (await c.get("/api/config")).json()
    assert body["tags_presets"] == ["论文", "考试周"]
    assert main.runtime["config"]["tags_presets"] == ["论文", "考试周"]
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["tags_presets"] == ["论文", "考试周"]


async def test_post_config_rejects_bad_tags_presets_without_pollution(tags_env):
    """非法值一律 400（**不是 500**——未特判键的 KeyError 就是那条故障），且零污染。"""
    snapshot = dict(tags_env["config"])
    for bad in ("重点", [], ["x"] * (main.TAG_PRESETS_MAX + 1), ["机器 学习"], [5], None):
        async with asgi_client() as c:
            r = await c.post("/api/config", json={"tags_presets": bad})
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "tags_presets" in r.json()["error"] or "非法标签" in r.json()["error"]
        assert tags_env["config"] == snapshot, bad        # 整体校验先行：运行时零污染
    assert not main.CONFIG_PATH.exists()                  # 也未落盘


async def test_all_four_dispatch_points_follow_custom_presets(tags_env):
    """B3 的核心：自定义之后，**四个下发点**同时跟随（同一取值函数，零分支）。"""
    custom = ["论文", "考试周", "周会"]
    async with asgi_client() as c:
        assert (await c.post("/api/config", json={"tags_presets": custom})).status_code == 200
        p = write_note("lecture_20261013_0300.md")
        got_list = (await c.get("/api/notes/list")).json()["presets"]
        got_raw = (await c.get("/api/notes/raw", params={"name": p.name})).json()["presets"]
        got_tags = (await c.get("/api/notes/tags", params={"name": p.name})).json()["presets"]
        got_batch = (await c.post("/api/notes/tags/batch",
                                  json={"files": [p.name], "tags": ["论文"]})).json()["presets"]
    assert got_list == got_raw == got_tags == got_batch == custom
    # 落盘确认（重启后仍是自定义值——config.json 是唯一持久化）
    assert json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))["tags_presets"] == custom


async def test_presets_noop_when_unchanged(tags_env):
    """喂**同一个值**（含顺序）→ no-op：不落盘、不改 changed（与其余键同一条语义）。"""
    async with asgi_client() as c:
        r = await c.post("/api/config",
                         json={"tags_presets": list(main.DEFAULT_TAG_PRESETS)})
    assert r.status_code == 200 and r.json() == {"ok": True, "changed": {}}
    assert not main.CONFIG_PATH.exists()


def test_settings_card_tolerates_non_array_config_value():
    """评审修复⑤：手改 config.json 把 tags_presets 写成字符串等非数组时，设置页不许
    半渲染（旧实现 `[...'重点']` 静默变 ['重','点']、`'重点'.map` 抛 TypeError 把
    renderSettings() 后半段整段掀掉，用户还能把单字保存进配置）。

    口径必须与服务端读侧（main.tag_presets：非数组/含非法项 → **整体**回落出厂四值）
    一致——同一份数据的前后端两半不能各说各话。"""
    fn = HTML.split("function cfgTagPresets() {", 1)[1].split("\n}", 1)[0]
    assert "Array.isArray(raw)" in fn and "raw.slice()" in fn      # 只认数组，且给的是副本
    assert "Array.isArray(fb)" in fn                               # 前端兜底那份同样验
    card = HTML.split("function renderSetTagsCard() {", 1)[1].split("\n}", 1)[0]
    assert "SET_TAGS = cfgTagPresets();" in card                   # 播种不再裸展开
    render = HTML.split("function renderSetTags() {", 1)[1].split("\n}", 1)[0]
    assert "cfgTagPresets().map((t) => '#' + t)" in render         # 「当前生效」也不裸 map
    assert "SETCFG.tags_presets || TAG_PRESETS" not in HTML        # 旧写法：字符串会抛
    assert "[...src]" not in HTML                                  # 旧播种：字符串被拆成单字


def test_settings_card_wired_in_notes_tab():
    """设置页 📝笔记 分区有「常用标签」卡：chips 工作副本 + 增删 + 保存 + 恢复默认。"""
    section = HTML.split('<section id="viewSettings">', 1)[1].split("</section>", 1)[0]
    card = section.split('<div class="card" data-set-tab="notes">', 4)[4].split(
        "</div>\n\n    <div", 1)[0]
    assert "常用标签" in card and 'id="setTagBar"' in card
    for anchor in ('id="setTagRow"', 'id="setTagNew"', 'id="btnSetTagAdd"',
                   'id="btnSetTagSave"', 'id="btnSetTagReset"', 'id="setTagState"'):
        assert anchor in card, anchor
    for binding in ("$('btnSetTagAdd').onclick", "$('btnSetTagSave').onclick",
                    "$('btnSetTagReset').onclick", "$('setTagNew').onkeydown"):
        assert binding in HTML, binding
    render_fn = HTML.split("function renderSettings() {", 1)[1].split("\n}", 1)[0]
    assert "renderSetTagsCard();" in render_fn             # 设置页渲染时回填编辑条


def test_settings_card_reuses_chips_rules_and_posts_config():
    """复用既有 chips 交互与**服务端同一套**预校验；保存走 POST /api/config（全量替换）。

    另锁两条约束：① 工作副本不被刷新覆盖（未保存的编辑不能被别的卡片的刷新吃掉）；
    ② chips 样式用 #setTagBar 提权——`.card button`(0,1,1) 比 `.tagChip`(0,1,0) 高，
    不提权就会长出一排方 chip（A2 面板抬头记过同一条教训）。"""
    add_fn = HTML.split("function addSetTag() {", 1)[1].split("\n}", 1)[0]
    assert "tagProblem(val)" in add_fn                     # 与服务端 normalize_tag 同规则
    assert "SET_TAGS.length >= SET_TAGS_MAX" in add_fn
    assert "SET_TAGS_DIRTY = true;" in add_fn
    save_fn = HTML.split("async function saveSetTags(list, label) {", 1)[1].split(
        "\n}", 1)[0]
    assert "'/api/config'" in save_fn
    assert "JSON.stringify({ tags_presets: list })" in save_fn
    assert "TAG_PRESETS = saved;" in save_fn               # 各面板 chips 立即跟随
    assert "SET_TAGS_DIRTY = false;" in save_fn
    assert "jfetch('/api/config')" in save_fn              # 失败回滚到服务端真值
    card_fn = HTML.split("function renderSetTagsCard() {", 1)[1].split("\n}", 1)[0]
    assert "if (!SET_TAGS_DIRTY)" in card_fn               # 不覆盖未保存的工作副本
    assert "const SET_TAGS_MAX = 12;" in HTML              # 与服务端 TAG_PRESETS_MAX 同值
    css = HTML.split("/* —— B3：设置页「常用标签」编辑条 ——", 1)[1].split("/*", 1)[0]
    assert "#setTagBar .tagChip" in css and "border-radius:999px" in css
    assert "#setTagBar > button" in css                    # 只吃本条自己的按钮
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}", css), "不得写死色值（§一-4）"
    assert "animation" not in css and "transition" not in css, "§5：不加动效"
    assert "var(--gold-dim)" in css                        # 选中态仍走 §六 口径
