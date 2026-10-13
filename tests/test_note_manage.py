# -*- coding: utf-8 -*-
"""tests/test_note_manage.py — §4 笔记管理：软删除/回收站/编辑/重命名（离线秒级）。

交付面（docs/notes-batch.md §4）：
- 删除 = **软删除**：连派生文件移进 transcripts/.trash/（同盘 rename）；回收站
  列表 / 恢复 / 彻底删除 / 清空；索引与列表默认排除 .trash；回收站内容不可再被当
  普通笔记打开/编辑。
- 编辑：PUT /api/notes/{name} 保存 md 原文（保存后"重索引"= 列表按文件现读现算）。
- 重命名：新名 sanitize（补 .md、补 lecture_ 前缀、禁路径分隔/系统保留名/长度限），
  内容零改动，派生文件跟着改。
- 批量：多选 → 批量删除（批量改课程见 test_note_course_move.py）。

三层：notes.py 纯函数 / 端点行为 / index.html 结构断言。路径全部经 conftest 重定向
到 tmp_path（TRANSCRIPTS）；repo_*_guard 仍守着仓库真实文件。
"""
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


def write_note(name: str, text: str = NOTE) -> Path:
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def global_hub():
    """用全局 hub（= 端点实际操作的那个）跑"正在记录"场景，用完还原。

    同时把 status['course'] 一起存取——接力新笔记时服务端就是读它决定归属
    （与"开一堂新课"同一口径）。"""
    hub = main.hub
    saved = (hub.transcript_path, hub.stamp, hub.t0_audio, list(hub.times))
    saved_course = main.status.get("course")
    yield hub
    hub.transcript_path, hub.stamp = saved[0], saved[1]
    hub.t0_audio = saved[2]
    hub.times = saved[3]
    main.status["course"] = saved_course


# ---------------- 1) 纯函数：名字规范化 / 笔记名形态 ----------------

def test_normalize_adds_prefix_and_suffix():
    assert notes_mod.normalize_note_name("神经网络入门") == ("lecture_神经网络入门.md", None)
    assert notes_mod.normalize_note_name("lecture_第3讲.md") == ("lecture_第3讲.md", None)
    assert notes_mod.normalize_note_name("  第4讲  ") == ("lecture_第4讲.md", None)
    # 扩展名大小写统一；尾部的点/空格按 Windows 语义裁掉（否则名字对不上）
    assert notes_mod.normalize_note_name("x.MD") == ("lecture_x.md", None)
    assert notes_mod.normalize_note_name("trail. ") == ("lecture_trail.md", None)


def test_normalize_rejects_unsafe_names():
    """§4 口径：禁路径分隔、禁系统保留名、长度限——逐条都要拒绝且文案明确。"""
    cases = {
        "../evil.md": "非法字符",
        "a/b.md": "非法字符",
        "a\\b.md": "非法字符",
        "bad:name.md": "非法字符",
        'quo"te.md': "非法字符",
        "pipe|.md": "非法字符",
        "lecture_x.summary.md": "派生物后缀",
        "x" * 200: "太长",
        "": "不能为空",
        "   ": "不能为空",
        "lecture_": "不合法",
        "x.summary.md": "派生物后缀",
        "bad<name>.md": "非法字符",
    }
    for raw, want in cases.items():
        got, err = notes_mod.normalize_note_name(raw)
        assert got is None and want in (err or ""), (raw, got, err)
    for raw in (None, 7, ["x"]):
        assert notes_mod.normalize_note_name(raw)[1] == "新文件名必须是字符串"
    # 补上前缀后就不再是系统保留名（保留名判定看的是**完整名字**的第一段）
    assert notes_mod.normalize_note_name("NUL") == ("lecture_NUL.md", None)
    assert notes_mod.normalize_note_name("com1") == ("lecture_com1.md", None)
    assert notes_mod.name_problem("NUL.md") and notes_mod.name_problem("con")


def test_name_problem_control_chars_and_reserved():
    assert notes_mod.name_problem("lecture_a\u0007.md")      # 控制字符
    assert notes_mod.name_problem("lecture_nul.md") is None  # 前缀让它不再是保留名
    assert notes_mod.name_problem("lecture_ok.md") is None


def test_safe_note_path_allows_semantic_names_but_not_trash():
    """§4：改名后的语义名必须能读写；回收站里的笔记**必须打不开**（名字含 / 即被拒）。"""
    assert notes_mod.safe_note_path(main.TRANSCRIPTS, "lecture_第3讲-神经网络.md") is not None
    for bad in (".trash/lecture_x.md", ".trash\\lecture_x.md", ".trash",
                "lecture_x.summary.md", "lecture_x.bak.md", "lecture_x.md.tmp",
                "config.json"):
        assert notes_mod.safe_note_path(main.TRANSCRIPTS, bad) is None, bad


def test_is_note_file_accepts_renamed_excludes_derived():
    assert notes_mod.is_note_file(Path("lecture_第3讲.md"))
    assert notes_mod.is_note_file(Path("lecture_20261013_0146.md"))
    for bad in ("lecture_x.summary.md", "lecture_x.bak.md", "lecture_x.apkg",
                "lecture_x.times.json", "config.json", "notes.txt"):
        assert not notes_mod.is_note_file(Path(bad)), bad


def test_move_note_with_sidecars_moves_whole_set(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    p = d / "lecture_20261013_0146.md"
    p.write_text(NOTE, encoding="utf-8")
    for extra in notes_mod.sidecar_paths(p)[:3]:
        (d / extra).write_text("x", encoding="utf-8")
    trash = d / ".trash"
    moved = notes_mod.move_note_with_sidecars(p, trash)
    assert moved[0] == p.name and len(moved) == 4          # 正文 + 3 个已存在的派生文件
    assert not p.exists() and (trash / p.name).is_file()
    for extra in moved[1:]:
        assert (trash / extra).is_file()
    assert not list(d.glob("*.summary.md"))                # 原处不留孤儿


def test_index_excludes_trash_and_sorts_by_stamp():
    """索引看不到 .trash（非递归 glob）；改名后的笔记按**头部时间戳**排序，
    不会因为换了名字就跳到别的年份去。"""
    d = main.TRANSCRIPTS
    write_note("lecture_20260101_0000.md", NOTE.replace("20261013_0146", "20260101_0000"))
    write_note("lecture_20261013_0146.md")
    renamed = write_note("lecture_第3讲-神经网络.md",
                         NOTE.replace("20261013_0146", "20260901_0900"))
    trash = d / ".trash"
    trash.mkdir(exist_ok=True)
    (trash / "lecture_20260701_1200.md").write_text(NOTE, encoding="utf-8")
    out = notes_mod.index(d)
    names = [n["name"] for n in out["notes"]]
    assert renamed.name in names and "lecture_20260701_1200.md" not in names
    assert names == ["lecture_20261013_0146.md", renamed.name, "lecture_20260101_0000.md"]


# ---------------- 2) 端点：读原文 / 编辑 ----------------

async def test_raw_returns_text_and_mtime():
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        r = await c.get("/api/notes/raw", params={"name": p.name})
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == NOTE and body["name"] == p.name
        assert body["mtime_us"] == p.stat().st_mtime_ns // 1000
        assert (await c.get("/api/notes/raw",
                            params={"name": "lecture_19990101_0000.md"})).status_code == 404
        assert (await c.get("/api/notes/raw",
                            params={"name": "../config.json"})).status_code == 404


async def test_put_saves_body_and_index_reflects_it():
    p = write_note("lecture_20261013_0146.md")
    new_text = NOTE.replace("hello world", "linear algebra").replace("你好世界", "线性代数")
    async with asgi_client() as c:
        base = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        r = await c.put(f"/api/notes/{p.name}", json={"text": new_text, "base_sha": base})
        assert r.status_code == 200
        # size 报的是**磁盘字节**：Windows 文本模式把 LF 落成 CRLF（与 Hub.append 落盘
        # 一致），故比 len(text.encode()) 大；读回来仍是同一段文本
        assert r.json()["ok"] and r.json()["size"] == p.stat().st_size > 0
        # 保存后"重索引"= 列表现读现算：新内容立刻可被检索到，旧内容检索不到
        hit = (await c.get("/api/notes/list", params={"q": "linear algebra"})).json()
        assert [n["name"] for n in hit["notes"]] == [p.name]
        miss = (await c.get("/api/notes/list", params={"q": "hello world"})).json()
        assert miss["notes"] == []
    assert p.read_text(encoding="utf-8") == new_text
    assert not list(main.TRANSCRIPTS.glob("*.tmp"))


async def test_put_rejects_stale_sha_and_missing_base():
    """编辑的乐观并发：页面打开后又有新字幕写进来 → 409 明确文案（防覆盖新内容）。

    凭据是**正文指纹**而不是 mtime：Windows 的 mtime 只有约 15ms 粒度，毫秒内的
    两次写入会撞成同一戳；指纹对这里要防的场景（页面开着好几分钟）既准确又确定。"""
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        base = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        # 模拟「编辑面板开着的时候，字幕又写了一段进来」
        p.write_text(NOTE + "- `10:00:09` new segment\n", encoding="utf-8")
        r = await c.put(f"/api/notes/{p.name}", json={"text": "x", "base_sha": base})
        assert r.status_code == 409 and "新内容" in r.json()["error"]
        assert "new segment" in p.read_text(encoding="utf-8")     # 被拒那次没动文件
        assert (await c.put(f"/api/notes/{p.name}", json={"text": "x"})).status_code == 400
        assert (await c.put(f"/api/notes/{p.name}",
                            json={"text": "x", "base_sha": "  "})).status_code == 400
        # 指纹对齐（同一篇、没人动过）→ 保存成功
        ok = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        assert (await c.put(f"/api/notes/{p.name}",
                            json={"text": NOTE, "base_sha": ok})).status_code == 200
    assert p.read_text(encoding="utf-8") == NOTE


async def test_put_validation_and_trash_unreachable():
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        base = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        assert (await c.put(f"/api/notes/{p.name}",
                            json={"text": 7, "base_sha": base})).status_code == 400
        assert (await c.put(f"/api/notes/{p.name}",
                            json={"text": "x", "base_sha": base,
                                  "bogus": 1})).status_code == 400
        assert (await c.put("/api/notes/" + "x" * 200,
                            json={"text": "x", "base_sha": base})).status_code == 404
        assert (await c.put("/api/notes/lecture_x.summary.md",
                            json={"text": "x", "base_sha": base})).status_code == 404
        # 回收站里的笔记：普通编辑入口够不到（名字含 / 就被 safe_note_path 拒）
        trash = main.TRANSCRIPTS / notes_mod.TRASH_DIR
        trash.mkdir(exist_ok=True)
        (trash / "lecture_20261013_0146.md").write_text(NOTE, encoding="utf-8")
        for path in ("/api/notes/raw", "/api/notes/preview", "/api/notes/file"):
            r = await c.get(path, params={"name": f".trash/{p.name}"})
            assert r.status_code == 404, (path, r.status_code)
        assert (await c.put(f"/api/notes/.trash/{p.name}",
                            json={"text": "x", "base_sha": base})).status_code == 404
    assert (trash / "lecture_20261013_0146.md").read_text(encoding="utf-8") == NOTE


async def test_put_size_cap():
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        base = (await c.get("/api/notes/raw", params={"name": p.name})).json()["sha"]
        big = "x" * (main.NOTE_EDIT_MAX + 1)
        r = await c.put(f"/api/notes/{p.name}", json={"text": big, "base_sha": base})
        assert r.status_code == 400 and "过大" in r.json()["error"]
    assert p.read_text(encoding="utf-8") == NOTE


# ---------------- 3) 端点：重命名 ----------------

async def test_rename_moves_file_and_sidecars():
    p = write_note("lecture_20261013_0146.md")
    (main.TRANSCRIPTS / "lecture_20261013_0146.summary.md").write_text("摘要", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.post("/api/notes/rename",
                         json={"file": p.name, "to": "第3讲-神经网络"})
        assert r.status_code == 200
        body = r.json()
        assert body["to"] == "lecture_第3讲-神经网络.md" and body["sidecars"] == 1
        assert body["live"] is False
        lst = (await c.get("/api/notes/list")).json()
        assert [n["name"] for n in lst["notes"]] == [body["to"]]
        assert lst["notes"][0]["has_summary"] is True     # 摘要跟着改名（徽章不丢）
    d = main.TRANSCRIPTS
    assert (d / body["to"]).read_text(encoding="utf-8") == NOTE   # 内容零改动
    assert (d / "lecture_第3讲-神经网络.summary.md").is_file()
    assert not p.exists()


async def test_rename_errors_are_400():
    p = write_note("lecture_20261013_0146.md")
    write_note("lecture_taken.md")
    async with asgi_client() as c:
        cases = [
            ({"file": p.name, "to": p.name}, "相同"),
            ({"file": p.name, "to": "taken"}, "已存在同名笔记"),
            ({"file": p.name, "to": "../evil"}, "非法字符"),
            ({"file": p.name, "to": "x.summary.md"}, "派生物后缀"),
            ({"file": "lecture_19990101_0000.md", "to": "x"}, "笔记不存在"),
            ({"file": p.name, "to": "x", "bogus": 1}, "未知字段"),
        ]
        for body, want in cases:
            r = await c.post("/api/notes/rename", json=body)
            assert r.status_code == 400, (body, r.status_code)
            assert want in r.json()["error"], (body, r.json())
    assert p.read_text(encoding="utf-8") == NOTE


async def test_rename_live_note_follows_transcript_pointer(global_hub):
    """正在记录的那篇改名后，hub 指针要跟着走——否则下一条字幕会在旧名下另起文件。"""
    global_hub.start_transcript("微积分")
    old = global_hub.transcript_path
    async with asgi_client() as c:
        r = await c.post("/api/notes/rename",
                         json={"file": old.name, "to": "微积分-第1讲"})
        assert r.status_code == 200 and r.json()["live"] is True
    assert global_hub.transcript_path == main.TRANSCRIPTS / r.json()["to"]
    global_hub.append("10:00:01", "derivative", "导数", 1)      # 字幕继续写进新文件
    assert "derivative" in global_hub.transcript_path.read_text(encoding="utf-8")
    assert not old.exists()


# ---------------- 4) 端点：软删除 / 回收站 ----------------

async def test_delete_is_soft_and_hidden_from_list():
    p = write_note("lecture_20261013_0146.md")
    (main.TRANSCRIPTS / "lecture_20261013_0146.times.json").write_text("[]", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.post("/api/notes/delete", json={"files": [p.name]})
        assert r.status_code == 200
        assert r.json()["trashed"] == 1 and r.json()["failed"] == []
        assert r.json()["restarted"] is False
        assert (await c.get("/api/notes/list")).json()["notes"] == []
        tr = (await c.get("/api/notes/trash")).json()
        assert tr["total"] == 1 and tr["notes"][0]["name"] == p.name
        assert tr["notes"][0]["first_en"] == "hello world"
        assert tr["dir"] == ".trash"
    trash = main.TRANSCRIPTS / ".trash"
    assert (trash / p.name).is_file() and (trash / "lecture_20261013_0146.times.json").is_file()
    assert not p.exists() and not (main.TRANSCRIPTS / "lecture_20261013_0146.times.json").exists()


async def test_delete_batch_all_or_nothing_and_cap():
    a = write_note("lecture_20261013_0100.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/delete",
                         json={"files": [a.name, "lecture_19990101_0000.md"]})
        assert r.status_code == 400 and "笔记不存在" in r.json()["error"]
        assert a.is_file()                                    # 一篇不存在 → 整单不动
        assert (await c.post("/api/notes/delete", json={"files": []})).status_code == 400
        many = [f"lecture_20260101_{i:04d}.md" for i in range(main.NOTE_MOVE_MAX + 1)]
        assert (await c.post("/api/notes/delete", json={"files": many})).status_code == 400
        assert (await c.post("/api/notes/delete", json={"file": a.name})).status_code == 400


async def test_delete_live_note_restarts_transcript(global_hub):
    """删"正在记录的那篇"→ 立刻接力新笔记（沿用当前课程），字幕不会在旧名下重新长出来。"""
    global_hub.start_transcript("微积分")
    main.status["course"] = "微积分"      # 与课上选课后的服务端状态一致
    old = global_hub.transcript_path
    async with asgi_client() as c:
        r = await c.post("/api/notes/delete", json={"files": [old.name]})
        assert r.status_code == 200 and r.json()["restarted"] is True
    assert global_hub.transcript_path != old          # 避开刚进回收站的名字（-2 后缀）
    assert global_hub.transcript_path.is_file()
    assert (main.TRANSCRIPTS / ".trash" / old.name).is_file()   # 原文完好在回收站
    assert courses_mod.note_course(
        global_hub.transcript_path.read_text(encoding="utf-8")) == "微积分"
    global_hub.append("10:00:02", "still recording", "", 1)
    assert "still recording" in global_hub.transcript_path.read_text(encoding="utf-8")
    assert not old.exists()                                   # 旧文件没被重新创建


async def test_trash_restore_and_conflict():
    p = write_note("lecture_20261013_0146.md")
    async with asgi_client() as c:
        await c.post("/api/notes/delete", json={"files": [p.name]})
        # 恢复前先占住同名：恢复必须拒绝而不是覆盖
        write_note(p.name, NOTE + "占用\n")
        r = await c.post("/api/notes/trash/restore", json={"files": [p.name]})
        assert r.status_code == 200
        assert r.json()["restored"] == 0 and "未覆盖" in r.json()["failed"][0]["error"]
        assert (main.TRANSCRIPTS / p.name).read_text(encoding="utf-8").endswith("占用\n")
        # 腾开同名再恢复：这次成功，内容 = 回收站里的那份
        (main.TRANSCRIPTS / p.name).unlink()
        r2 = await c.post("/api/notes/trash/restore", json={"files": [p.name]})
        assert r2.json()["restored"] == 1
        assert (main.TRANSCRIPTS / p.name).read_text(encoding="utf-8") == NOTE
        assert (await c.get("/api/notes/trash")).json()["total"] == 0


async def test_trash_restore_rejects_unknown_and_bad_names():
    async with asgi_client() as c:
        r = await c.post("/api/notes/trash/restore",
                         json={"files": ["lecture_19990101_0000.md", "../x.md"]})
        assert r.status_code == 200 and r.json()["restored"] == 0
        assert len(r.json()["failed"]) == 2
        assert (await c.post("/api/notes/trash/restore", json={"files": []})).status_code == 400
        assert (await c.post("/api/notes/trash/restore",
                             json={"files": ["x.md"], "bogus": 1})).status_code == 400


async def test_trash_purge_and_empty():
    a = write_note("lecture_20261013_0100.md")
    b = write_note("lecture_20261013_0200.md")
    async with asgi_client() as c:
        await c.post("/api/notes/delete", json={"files": [a.name, b.name]})
        r = await c.post("/api/notes/trash/purge", json={"files": [a.name]})
        assert r.json()["purged"] == 1
        trash = main.TRANSCRIPTS / ".trash"
        assert not (trash / a.name).exists() and (trash / b.name).is_file()
        # 彻底删除后不可恢复（不在回收站里了）
        assert (await c.post("/api/notes/trash/restore",
                             json={"files": [a.name]})).json()["restored"] == 0
        r2 = await c.post("/api/notes/trash/empty", json={})
        assert r2.status_code == 200 and r2.json()["removed"] >= 1
        assert (await c.get("/api/notes/trash")).json()["total"] == 0
        assert not any(p.is_file() for p in trash.iterdir()) if trash.is_dir() else True


async def test_deleted_note_content_survives_in_trash():
    """回收站是"搬家"不是"压缩"：正文逐字节保留（可恢复的凭据）。"""
    body = NOTE + "- `10:00:09` keep me\n"
    p = write_note("lecture_20261013_0146.md", body)
    async with asgi_client() as c:
        await c.post("/api/notes/delete", json={"files": [p.name]})
    assert (main.TRANSCRIPTS / ".trash" / p.name).read_text(encoding="utf-8") == body


async def test_broadcasts_notes_event_on_delete_and_rename(monkeypatch):
    p = write_note("lecture_20261013_0146.md")
    sent: list = []
    real = main.hub.broadcast

    async def spy(payload):
        sent.append(payload)

    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as c:
            await c.post("/api/notes/rename", json={"file": p.name, "to": "改了名"})
            await c.post("/api/notes/delete", json={"files": ["lecture_改了名.md"]})
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)
    frames = [m for m in sent if m.get("type") == "notes"]
    assert len(frames) == 2
    assert frames[0]["files"] == ["lecture_改了名.md", p.name]


# ---------------- 5) index.html 结构 ----------------

def test_row_and_preview_entries_exist():
    row = HTML.split("function noteRow(n) {", 1)[1].split("\n}", 1)[0]
    assert "删除" in row and "delNote(" in row
    assert "移动" in row and "openMoveDialog([n.name])" in row
    head = HTML.split("// 顶部操作（§七-2）", 1)[1].split("ph.append(", 1)[0]
    for label in ("编辑", "重命名", "删除", "移动到…"):
        assert label in head, label
    assert "openEdit(d.name)" in HTML and "openRename(d.name)" in HTML


def test_batch_bar_has_delete():
    assert 'id="nbBatchDelete"' in HTML
    assert "$('nbBatchDelete').onclick" in HTML
    assert "'/api/notes/delete'" in HTML


def test_trash_ui_wired():
    assert 'id="trashMask"' in HTML and 'id="trashList"' in HTML
    assert "$('nbTrashBtn').onclick" in HTML                      # 笔记库入口
    for path in ("'/api/notes/trash'", "'/api/notes/trash/restore'",
                 "'/api/notes/trash/purge'", "'/api/notes/trash/empty'"):
        assert path in HTML, path


def test_edit_and_rename_use_optimistic_concurrency():
    assert "/api/notes/raw" in HTML                                # 打开编辑先取原文+指纹
    assert "base_sha" in HTML                                      # 保存必须带回正文指纹
    save = HTML.split("async function saveEdit() {", 1)[1].split(
        "async function openRename", 1)[0]
    assert "base_sha: editTarget.sha" in save
    assert "method: 'PUT'" in save
    assert "'/api/notes/' + encodeURIComponent(editTarget.name)" in save


def test_manage_dialogs_use_shared_modal_infra():
    """三个新弹窗必须进共用白名单（inert 与 Escape 都读 MASKS）：漏一个就会出现
    「背景 inert 把弹窗关在门外」或「Escape 关不掉」。"""
    masks = HTML.split("const MASKS = [", 1)[1].split("];", 1)[0]
    for mid in ("editMask", "renameMask", "trashMask"):
        assert f"'{mid}'" in masks, mid
    assert "MASKS.map((id) => $(id))" in HTML                      # Escape 分支
    for mid in ("editMask", "renameMask", "trashMask"):
        assert f"openModal($('{mid}'))" in HTML, mid


# ---------------- 6) 评审 M1：接力新笔记后中文配对不能断 ----------------

def test_note_seg_is_note_local_and_resets(global_hub):
    """笔记级段号（ZH 锚点/时间轴 seg 口径）随新笔记归零。

    评审 M1：ZH 锚点与会话级 status['segments'] 混用 → 删掉正在记录的那篇后接力
    的新笔记 EN 行从 1 起、锚点却接着上一场的号，notes.parse 按 EN 序号取 zh 全部
    取不到，整场剩下的中文列在预览/列表/导出里静默消失。"""
    global_hub.start_transcript("甲")
    assert [global_hub.next_note_seg() for _ in range(3)] == [1, 2, 3]
    global_hub.start_transcript("甲", avoid={global_hub.transcript_path.name})
    assert global_hub.next_note_seg() == 1          # 新笔记从 1 起（与会话号无关）


def test_record_time_writes_note_local_segs(global_hub):
    """时间轴 sidecar 的 seg 也是笔记级——SRT 查表（by_seg）才命中，导出的
    「precise」来源标记才对得上。"""
    global_hub.start_transcript("甲")
    for i in range(2):
        global_hub.record_time(global_hub.next_note_seg(), 100.0 + i, None, 1.0)
    assert [t["seg"] for t in global_hub.times] == [1, 2]
    global_hub.start_transcript("甲", avoid={global_hub.transcript_path.name})
    global_hub.record_time(global_hub.next_note_seg(), 200.0, None, 1.0)
    assert [t["seg"] for t in global_hub.times] == [1]


async def test_restart_after_delete_keeps_zh_pairing(global_hub):
    """评审 M1 回归（端到端）：删掉正在记录的那篇 → 接力新笔记 → 后续段的中文
    必须仍能与英文配对（parse.paired == segments、索引 translated 同步）。"""
    global_hub.start_transcript("微积分")
    main.status["course"] = "微积分"
    for i in range(3):                                  # 先记 3 段（笔记级 1..3）
        seg = global_hub.next_note_seg()
        global_hub.append(f"10:00:0{i}", f"en{i}", "", seg=seg)
        global_hub.append(f"10:00:0{i}", "", f"zh{i}", seg=seg)
    main.status["segments"] = 56                        # 会话级已说过 56 段
    old = global_hub.transcript_path
    async with asgi_client() as c:
        assert (await c.post("/api/notes/delete",
                             json={"files": [old.name]})).json()["restarted"] is True
    for i in range(2):                                  # 接力后再来两段
        seg = global_hub.next_note_seg()
        main.status["segments"] += 1
        global_hub.record_time(seg, 500.0 + i, None, 1.0)
        global_hub.append(f"10:01:0{i}", f"new{i}", "", seg=seg)
        global_hub.append(f"10:01:0{i}", "", f"新{i}", seg=seg)
    new = global_hub.transcript_path
    parsed = notes_mod.parse(new.read_text(encoding="utf-8"))
    assert parsed["segments"] == 2, "接力后 EN 行数不对"
    assert parsed["paired"] == 2, "中文列断了（评审 M1 复现）"
    assert [p["zh"] for p in parsed["pairs"]] == ["新0", "新1"]
    idx = notes_mod.index(main.TRANSCRIPTS)
    mine = [n for n in idx["notes"] if n["name"] == new.name]
    assert mine and mine[0]["translated"] == 2          # 列表/预览口径同源
    assert [t["seg"] for t in global_hub.times] == [1, 2]   # sidecar 也是笔记级


def test_append_drops_late_zh_from_previous_note(global_hub):
    """晚到的译文若属于**已换掉的那篇**笔记，直接丢弃——不往新笔记里写孤儿中文行。"""
    old = global_hub.start_transcript("甲")
    global_hub.append("10:00:00", "old en", "", seg=1)
    new = global_hub.start_transcript("甲", avoid={old.name})
    global_hub.append("10:00:01", "", "迟到译文", seg=1, note=old)
    assert "迟到译文" not in new.read_text(encoding="utf-8")
    global_hub.append("10:00:02", "", "本笔记译文", seg=1, note=new)
    assert "本笔记译文" in new.read_text(encoding="utf-8")


# ---------------- 7) 评审 M2：搬运绝不覆盖（含派生文件与 .trash 侧） ----------------

async def test_delete_refuses_silent_trash_overwrite():
    """评审 M2-①：回收站里已有同名笔记（连摘要）时再删同名的一篇 → 报失败，
    回收站里那份原文与摘要都不许被覆盖。"""
    trash = main.TRANSCRIPTS / notes_mod.TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "lecture_20261013_0100.md").write_text("OLD-BODY", encoding="utf-8")
    (trash / "lecture_20261013_0100.summary.md").write_text("OLD-SUMMARY", encoding="utf-8")
    p = write_note("lecture_20261013_0100.md", NOTE + "- dummy\n")
    (main.TRANSCRIPTS / "lecture_20261013_0100.summary.md").write_text(
        "NEW-SUMMARY", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.post("/api/notes/delete", json={"files": [p.name]})
        assert r.status_code == 200
        body = r.json()
        assert body["trashed"] == 0 and len(body["failed"]) == 1
        assert "未删除也未覆盖" in body["failed"][0]["error"]
        assert p.is_file()                                   # 源头没动
    assert (trash / "lecture_20261013_0100.md").read_text(encoding="utf-8") == "OLD-BODY"
    assert (trash / "lecture_20261013_0100.summary.md").read_text(encoding="utf-8") \
        == "OLD-SUMMARY"


async def test_restore_refuses_when_sidecar_conflicts():
    """评审 M2-②：目标只有同名**派生物**时恢复也必须拒绝——旧实现只查正文，
    于是那份摘要被静默盖掉（与 docstring 的「绝不覆盖」自相矛盾）。"""
    p = write_note("lecture_20261013_0146.md")
    (main.TRANSCRIPTS / "lecture_20261013_0146.summary.md").write_text(
        "SUMMARY", encoding="utf-8")
    async with asgi_client() as c:
        await c.post("/api/notes/delete", json={"files": [p.name]})
        # 目标目录现在没有正文，但有一份同名摘要占位
        (main.TRANSCRIPTS / "lecture_20261013_0146.summary.md").write_text(
            "NEW-LIVE-SUMMARY", encoding="utf-8")
        r = await c.post("/api/notes/trash/restore", json={"files": [p.name]})
        body = r.json()
        assert body["restored"] == 0 and "未覆盖" in body["failed"][0]["error"]
        assert "summary" in body["failed"][0]["error"]       # 点名冲突文件
    assert (main.TRANSCRIPTS / "lecture_20261013_0146.summary.md").read_text(
        encoding="utf-8") == "NEW-LIVE-SUMMARY"
    assert (main.TRANSCRIPTS / ".trash" / p.name).is_file()  # 回收站那份还在


def test_move_note_with_sidecars_prechecks_all_names(tmp_path):
    """纯函数层：任一目标名（正文或派生）已存在 → 抛 MoveConflict 且一个都不搬。"""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    p = src / "lecture_a.md"
    p.write_text(NOTE, encoding="utf-8")
    (src / "lecture_a.times.json").write_text("[]", encoding="utf-8")
    (dest / "lecture_a.times.json").write_text("OLD", encoding="utf-8")
    with pytest.raises(notes_mod.MoveConflict) as ei:
        notes_mod.move_note_with_sidecars(p, dest)
    assert ei.value.names == ["lecture_a.times.json"]
    assert p.is_file() and not (dest / "lecture_a.md").exists()
    (dest / "lecture_a.times.json").unlink()
    moved = notes_mod.move_note_with_sidecars(p, dest)
    assert moved == ["lecture_a.md", "lecture_a.times.json"]
