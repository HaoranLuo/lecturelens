# -*- coding: utf-8 -*-
"""tests/test_tag_manage.py — B9 标签管理页：全库重命名（合并）/删除标签。

交付面（docs/notes-batch.md B9 行）：「标签变多后的必需件」——把 `#考试` 并成
`#重点`、把错打的标签从全库摘掉、给一个标签改个好名字。

本文件锁四层：
1) **范围**：`tag_manage_targets` 只扫 transcripts/ 直属的 `lecture_*.md`
   ——`.trash/`（非递归 glob 天然不收）、`.summary.md`/`.bak.md` 派生物、以及任何
   非笔记文件都不在名单里；
2) **语义**：rename = 改名 + **合并**（某篇本来就有新名时去重，绝不写出
   `#重点 #重点`）；delete = 走 merge_tags 的 remove 语义；没命中旧名的笔记**不写盘**
   （mtime 不变——这点只有按篇断言才看得出）；
3) **纪律**：非法名/未知 op/未知字段 → 400 且**一篇都不动**（整单先校验后动手）、
   只动笔记头的标签行（正文逐字节不变）、全库完成**只广播一帧**；
4) 端点形状 `{ok, scanned, changed, failed}` + 前端接线（按钮 / 弹窗 / 内联重命名 / 二次确认）。

全程 ROOT 指向 tmp_path（conftest 已隔离 TRANSCRIPTS），文件是唯一事实来源。
"""
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import notes as notes_mod

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

BODY = "\n---\n\n- `10:00:01` hello world\n  - `#1` 你好世界\n"


def note_text(tags_line: str = "") -> str:
    """一篇最小笔记；tags_line 空 = 没有标签行。"""
    head = ("# 双语课堂笔记 20261013_0146\n\n"
            "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
            "> 课程：未分类\n")
    if tags_line:
        head += f"{tags_line}\n"
    return head + BODY


def write_note(name: str, tags_line: str = "", text: str | None = None,
               subdir: str = "") -> Path:
    d = Path(main.TRANSCRIPTS)
    if subdir:
        d = d / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text if text is not None else note_text(tags_line), encoding="utf-8")
    return p


def tags_of(p: Path) -> list[str]:
    """从**盘上文件**解析标签（不看响应体自报）。"""
    return notes_mod.note_tags(p.read_text(encoding="utf-8"))


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def tag_root(tmp_path, monkeypatch):
    """TRANSCRIPTS → tmp_path/transcripts（conftest 已隔离，这里只确保目录存在）。"""
    d = Path(main.TRANSCRIPTS)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _manage(c, op, old=None, new=None, extra=None):
    body = {"op": op}
    if old is not None:
        body["old"] = old
    if new is not None:
        body["new"] = new
    if extra:
        body.update(extra)
    return await c.post("/api/notes/tags/manage", json=body)


# ---------------- 1) 纯函数：范围与合并 ----------------

def test_targets_only_direct_lecture_notes_and_never_trash(tag_root):
    """范围纪律：直属 lecture_*.md；派生物 / .trash / 非笔记文件一律不在名单。"""
    a = write_note("lecture_20261013_0146.md", "> 标签：#考试")
    b = write_note("lecture_20261013_0200.md")
    write_note("lecture_20261013_0146.summary.md", "> 标签：#考试",
               subdir="")                       # 派生物（摘要）
    write_note("lecture_20261013_0146.bak.md", "> 标签：#考试")   # 派生物（备份）
    write_note("lecture_20261013_0300.md", "> 标签：#考试", subdir=notes_mod.TRASH_DIR)
    (Path(main.TRANSCRIPTS) / "notes.md").write_text(
        note_text("> 标签：#考试"), encoding="utf-8")              # 非笔记名
    got = main.tag_manage_targets()
    assert got == sorted([a, b])
    assert all(p.parent == Path(main.TRANSCRIPTS) for p in got)


def test_targets_empty_when_dir_missing(tag_root, monkeypatch):
    monkeypatch.setattr(main, "TRANSCRIPTS", Path(main.TRANSCRIPTS) / "nope")
    assert main.tag_manage_targets() == []


def test_merge_is_rename_with_dedupe():
    """rename 即合并：命中项换名，且**去重保序**（不是逐项替换——那会写出重复标签）。"""
    assert main.tag_manage_merge(["考试"], "rename", "考试", "重点") == ["重点"]
    assert main.tag_manage_merge(["重点", "考试"], "rename", "考试", "重点") == ["重点"]
    assert main.tag_manage_merge(["a", "考试", "b"], "rename", "考试", "a") == ["a", "b"]
    assert main.tag_manage_merge(["考试", "考试"], "rename", "考试", "重点") == ["重点"]
    # 没这篇的份 → None（调用方据此跳过：不写盘、不计 changed）
    assert main.tag_manage_merge(["其它"], "rename", "考试", "重点") is None
    assert main.tag_manage_merge([], "delete", "考试", None) is None
    # delete = merge_tags 的 remove 语义（同一函数，不另写一套）
    assert main.tag_manage_merge(["重点", "考试"], "delete", "考试", None) == ["重点"]
    assert main.tag_manage_merge(["考试"], "delete", "考试", None) == []


# ---------------- 2) rename：往返以文件为事实来源 ----------------

async def test_rename_roundtrip_and_file_is_truth(tag_root):
    """改名 → 读盘核对 → 再改回来 → 文件逐字节等于最初（只动标签行）。

    往返用**单标签**的笔记：换名的合并语义是**有损**的（`#考试 #重点` 合并成 `#重点`
    后，再改回来只剩 `#考试`）——那条另有用例，这里要钉的是「只动标签行、其余逐字节不动」。"""
    a = write_note("lecture_20261013_0146.md", "> 标签：#考试")
    b = write_note("lecture_20261013_0200.md", "> 标签：#考试 #笔记")
    c = write_note("lecture_20261013_0300.md", "> 标签：#其它")      # 无关笔记：不许被动
    before_a, before_c = a.read_bytes(), c.read_bytes()
    before_b = b.read_bytes()          # 按**盘上字节**留底：Path.write_text 在 Windows 上
    mtime_c = c.stat().st_mtime_ns     # 把 \n 翻成 \r\n，重造字符串会失真（见 glossary.py 抬头）
    async with asgi_client() as client:
        r = await _manage(client, "rename", "考试", "重点")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scanned"] == 3 and body["changed"] == 2 and body["failed"] == []
        assert sorted(body["files"]) == sorted([a.name, b.name])
        # 文件是事实来源（响应体只作交叉核对）
        assert tags_of(a) == ["重点"] and tags_of(b) == ["重点", "笔记"]
        assert a.read_text(encoding="utf-8").count("> 标签：") == 1
        # 改回来（宽松写法 `#重点` = 同一个标签）
        r2 = await _manage(client, "rename", "#重点", "#考试")
        assert r2.status_code == 200 and r2.json()["changed"] == 2
    assert a.read_bytes() == before_a, "往返后必须逐字节回到最初（只动标签行）"
    assert b.read_bytes() == before_b, "多标签笔记往返后同样逐字节不变"
    assert c.read_bytes() == before_c
    assert c.stat().st_mtime_ns == mtime_c, "没命中旧名的笔记**不写盘**（mtime 不变）"


async def test_rename_merges_without_duplicating_in_file(tag_root):
    """合并落盘形状：`#考试 #重点` 改名后只剩一条 `#重点`（绝不是 `#重点 #重点`）。"""
    p = write_note("lecture_20261013_0146.md", "> 标签：#考试 #重点")
    async with asgi_client() as client:
        r = await _manage(client, "rename", "考试", "重点")
        assert r.status_code == 200 and r.json()["changed"] == 1
    text = p.read_text(encoding="utf-8")
    assert "> 标签：#重点\r\n" in text or "> 标签：#重点\n" in text
    assert "#重点 #重点" not in text
    assert text.count("#重点") == 1
    assert tags_of(p) == ["重点"]


async def test_rename_updates_only_tag_line_body_intact(tag_root):
    """正文/头部其余行逐字不动（只替换标签行）。"""
    p = write_note("lecture_20261013_0146.md", "> 标签：#错名")
    before = p.read_text(encoding="utf-8").splitlines()
    async with asgi_client() as client:
        assert (await _manage(client, "rename", "错名", "正确")).status_code == 200
    after = p.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before)
    diffs = [(x, y) for x, y in zip(before, after) if x != y]
    assert diffs == [("> 标签：#错名", "> 标签：#正确")]


async def test_rename_to_same_name_is_noop(tag_root):
    """新旧同名：不是错误，但也不逐篇写盘（回执里明说没改动）。"""
    p = write_note("lecture_20261013_0146.md", "> 标签：#重点")
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    async with asgi_client() as client:
        r = await _manage(client, "rename", "重点", "#重点")     # 宽松写法 → 同一个标签
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == 0 and body["scanned"] == 1 and body["files"] == []
    assert "相同" in body["note"]
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime


# ---------------- 3) delete ----------------

async def test_delete_strips_tag_from_all_notes(tag_root):
    a = write_note("lecture_20261013_0146.md", "> 标签：#考试 #重点")
    b = write_note("lecture_20261013_0200.md", "> 标签：#考试")
    c = write_note("lecture_20261013_0300.md", "> 标签：#其它")
    async with asgi_client() as client:
        r = await _manage(client, "delete", "考试")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["op"] == "delete" and body["new"] is None
    assert body["scanned"] == 3 and body["changed"] == 2
    assert tags_of(a) == ["重点"] and tags_of(b) == []
    assert tags_of(c) == ["其它"]
    # 删掉最后一个标签 → 整个标签行消失（不是留一行空标签）
    assert "> 标签" not in b.read_text(encoding="utf-8")
    assert "hello world" in b.read_text(encoding="utf-8")     # 正文在


async def test_delete_missing_tag_changes_nothing(tag_root):
    a = write_note("lecture_20261013_0146.md", "> 标签：#重点")
    before, mtime = a.read_bytes(), a.stat().st_mtime_ns
    async with asgi_client() as client:
        r = await _manage(client, "delete", "不存在的标签")
    assert r.status_code == 200
    assert r.json()["changed"] == 0 and r.json()["scanned"] == 1
    assert a.read_bytes() == before and a.stat().st_mtime_ns == mtime


# ---------------- 4) 校验：整单先校验后动手 ----------------

@pytest.mark.parametrize("body,needle", [
    ({"op": "rename", "old": "有 空格", "new": "新名"}, "非法标签"),
    ({"op": "rename", "old": "考试", "new": "有 空格"}, "非法新标签"),
    ({"op": "rename", "old": "考试", "new": "x" * 17}, "非法新标签"),
    ({"op": "rename", "old": "考试"}, "非法新标签"),            # 缺 new
    ({"op": "rename", "old": "考试", "new": None}, "非法新标签"),
    ({"op": "rename", "old": "a#b", "new": "b"}, "非法标签"),   # 内部 #
    ({"op": "rename", "old": "a\u2028b", "new": "b"}, "非法标签"),
    ({"op": "rename", "old": 5, "new": "b"}, "非法标签"),
    ({"op": "delete", "old": ""}, "非法标签"),
    ({"op": "delete", "old": None}, "非法标签"),
    ({"op": "wipe", "old": "考试"}, "未知操作"),                # 不认识的操作
    ({"old": "考试"}, "未知操作"),                              # 缺 op
    ({"op": "delete", "old": "考试", "mode": "add"}, "未知字段"),
])
async def test_bad_requests_400_and_notes_untouched(tag_root, monkeypatch, body, needle):
    """任一非法 → 400（文案点名），且**一篇都动不了**（整单先校验后动手）。"""
    a = write_note("lecture_20261013_0146.md", "> 标签：#考试")
    b = write_note("lecture_20261013_0200.md", "> 标签：#考试")
    before = {p.name: p.read_bytes() for p in (a, b)}
    sent: list = []

    async def spy(payload):
        sent.append(payload)

    real = main.hub.broadcast
    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as client:
            r = await client.post("/api/notes/tags/manage", json=body)
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    assert r.status_code == 400, (body, r.status_code, r.text)
    assert needle in r.json()["error"], (body, r.json())
    assert {p.name: p.read_bytes() for p in (a, b)} == before
    assert sent == [], "被拒的请求不该广播任何事件"


# ---------------- 5) 广播：全库只一帧 ----------------

async def test_single_broadcast_frame_with_all_changed_files(tag_root, monkeypatch):
    """全库操作**只广播一帧**（逐篇广播会把别的窗口打爆），帧里带齐改动篇目。"""
    names = [f"lecture_20261013_14{i:02d}.md" for i in range(5)]
    for n in names:
        write_note(n, "> 标签：#考试")
    write_note("lecture_20261013_1500.md", "> 标签：#其它")     # 不改的
    sent: list = []

    async def spy(payload):
        sent.append(payload)

    real = main.hub.broadcast
    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as client:
            r = await _manage(client, "delete", "考试")
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    assert r.status_code == 200 and r.json()["changed"] == 5
    frames = [m for m in sent if m.get("type") == "notes"]
    assert len(frames) == 1, f"应只广播一帧，实际 {len(frames)} 帧"
    assert sorted(frames[0]["files"]) == sorted(names)


async def test_no_broadcast_when_nothing_changed(tag_root, monkeypatch):
    write_note("lecture_20261013_0146.md", "> 标签：#重点")
    sent: list = []

    async def spy(payload):
        sent.append(payload)

    real = main.hub.broadcast
    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as client:
            r = await _manage(client, "delete", "没人用的标签")
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    assert r.status_code == 200 and r.json()["changed"] == 0
    assert sent == []


async def test_failed_notes_are_reported_but_others_continue(tag_root, monkeypatch):
    """逐篇失败不中断：坏的跳过并记 failed，好的照改（返回 200 + 明细）。"""
    ok = write_note("lecture_20261013_0146.md", "> 标签：#考试")
    bad = write_note("lecture_20261013_0200.md", "> 标签：#考试")
    real_write = main.note_tags_write

    def flaky(p, tags):
        if p.name == bad.name:
            raise OSError("boom")
        return real_write(p, tags)

    monkeypatch.setattr(main, "note_tags_write", flaky)
    async with asgi_client() as client:
        r = await _manage(client, "rename", "考试", "重点")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == 1 and [f["file"] for f in body["failed"]] == [bad.name]
    assert "boom" in body["failed"][0]["error"]
    assert tags_of(ok) == ["重点"] and tags_of(bad) == ["考试"]      # 坏的那篇原样


# ---------------- 6) 端点规模：不受 batch 的 200 篇上限约束 ----------------

async def test_scan_is_not_capped_at_batch_limit(tag_root):
    """B9 的核心价值之一：**全库**（tags/batch 有 200 篇上限，全库改名会直接撞墙）。"""
    for i in range(210):
        write_note(f"lecture_20261013_{i:04d}.md", "> 标签：#考试")
    async with asgi_client() as client:
        r = await _manage(client, "rename", "考试", "重点")
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 210 and r.json()["changed"] == 210


# ---------------- 7) 前端接线（结构断言，真机点选归人工） ----------------

def test_manage_button_and_modal_wired():
    bar = HTML.split('<div class="nbBar">', 1)[1].split("</div>", 1)[0]
    assert 'id="nbTagManage"' in bar and "标签管理" in bar
    modal = HTML.split('<div id="tagMask" class="dlgMask">', 1)[1].split("</div>\n</div>", 1)[0]
    for anchor in ('id="tagList"', 'id="tagSub"', 'id="tagDone"', 'id="tagClose"'):
        assert anchor in modal, anchor
    for binding in ("$('nbTagManage').onclick", "$('tagDone').onclick",
                    "$('tagClose').onclick"):
        assert binding in HTML, binding
    assert "'tagMask'," in HTML                       # 登记进 MASKS（焦点/inert/Escape）
    assert "openModal($('tagMask'))" in HTML
    assert "closeModal($('tagMask'))" in HTML


def test_manage_modal_uses_server_counts_and_endpoint():
    """列表数据 = **全库**计数（不带筛选地现拉 /api/notes/list 的 tag_counts）；写操作
    **不带文件名**（全库由服务端枚举）。

    为什么不用侧栏那份 NB.tagCounts：它是「当前课程/检索筛选下」的 facet 口径，而这一页
    做的是全库操作——混用会让确认框少报改动的篇数（安全提示少报比不报更糟）。"""
    render = HTML.split("function renderTagManage() {", 1)[1].split("\n}", 1)[0]
    assert "tagManageRows()" in render
    load = HTML.split("async function loadTagManageCounts() {", 1)[1].split("\n}", 1)[0]
    assert "jfetch('/api/notes/list')" in load          # 不带筛选 = 全库
    assert "r.tag_counts" in load
    rows = HTML.split("function tagManageRows() {", 1)[1].split("\n}", 1)[0]
    assert "tagManageCounts || []" in rows
    assert "textContent = '#' + tag" in HTML          # 标签名一律 textContent（用户输入）
    run = HTML.split("async function runTagManage(op, old, next) {", 1)[1].split(
        "\n}", 1)[0]
    assert "'/api/notes/tags/manage'" in run
    assert "JSON.stringify(payload)" in run
    assert "const payload = { op, old };" in run and "payload.new = next;" in run
    assert "scanned" in run and "failed" in run
    assert "await afterNotesChanged();" in run         # 筛选栏 / tagCounts 跟随
    assert "NB.tag === r.old" in run                   # 筛选值跟着改名/删除走（不留空列表）


def test_manage_modal_inline_rename_and_confirm_texts():
    """重命名 = 内联输入 + 确认（提示 N 篇将被更新）；删除 = 二次确认（N 篇将移除）。"""
    row = HTML.split("function tagManageRow(tag, count) {", 1)[1].split("\n}", 1)[0]
    assert "tagRenameOld === tag" in row
    assert "'tagRenameInput'" in row and "input.select()" in row
    assert "go.textContent = '确认'" in row
    assert "askTagDelete(tag, count)" in row
    rename = HTML.split("async function confirmTagRename(oldTag, rawNew) {", 1)[1].split(
        "\n}", 1)[0]
    assert "tagProblem(next)" in rename                # 与服务端 normalize_tag 同规则
    assert "篇笔记将被更新" in rename
    dele = HTML.split("async function askTagDelete(tag, count) {", 1)[1].split("\n}", 1)[0]
    assert "confirm(" in dele and "篇笔记会移除这个标签" in dele
    esc = row.split("e.key === 'Escape'", 1)[1][:200]
    assert "stopPropagation()" in esc                  # Escape = 退出内联编辑，不关弹窗
