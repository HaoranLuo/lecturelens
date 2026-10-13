# -*- coding: utf-8 -*-
"""tests/test_summary_edit.py — §6 可编辑摘要（notes-batch）。

验收口径（docs/notes-batch.md §6）：
- 摘要（`lecture_*.summary.md`）可**人工编辑**：GET /api/notes/summary 读原文、
  PUT /api/notes/summary 就地写回（走 notes.safe_note_path 同款名字校验 + 路径由
  笔记名**派生**，不接受直接指定某个 .summary.md）；保存后重索引（本项目的索引每次
  现读现算 + WS 'notes' 广播让别的窗口刷新）；
- 再次「生成摘要」前检测已有文件：POST /api/notes/summarize 回 overwrite/backup，
  覆盖前自动备份为 `*.summary.bak.md`（summarize.backup_existing），
  **备份不进笔记列表**（notes.is_note_file/DERIVED_SUFFIXES 口径）。

浏览器交互（确认框/弹窗）不进自动化：本文件覆盖纯逻辑与端点契约，前端布线由
结构断言 + smoke 的真服务往返 + 真机手测承担。
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import notes as notes_mod
import summarize

NOTE = "lecture_20260101_0900.md"
SUMMARY = "lecture_20260101_0900.summary.md"
BACKUP = "lecture_20260101_0900.summary.bak.md"
ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


def write_note(name=NOTE, body="- `10:00:00` hello world\n"):
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    p.write_text("# 双语课堂笔记 20260101_0900\n\n"
                 "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                 "> 课程：未分类\n\n---\n\n" + body, encoding="utf-8")
    return p


def write_summary(text="# 课堂笔记摘要\n\n## 概览\nAI 版\n", name=SUMMARY):
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    p = main.TRANSCRIPTS / name
    p.write_text(text, encoding="utf-8")
    return p


def summary_of(name=NOTE):
    """笔记名 → 摘要路径（与 main.summary_path 同口径，测试里用局部实现防耦合）。"""
    return main.TRANSCRIPTS / (Path(name).stem + ".summary.md")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def sum_env():
    """复位摘要相关的模块级状态（hub 指针 / runtime 守卫 / status 快照）。"""
    saved = (main.status["summary"], main.runtime["summarizing"],
             main.runtime["summary_task"], main.hub.transcript_path,
             main.runtime["translator"])
    main.status["summary"] = {"state": "idle", "done": 0, "total": 0,
                              "failed": 0, "path": None, "error": None}
    main.runtime["summarizing"] = False
    main.runtime["summary_task"] = None
    main.hub.transcript_path = None
    main.runtime["translator"] = None
    yield main
    (main.status["summary"], main.runtime["summarizing"],
     main.runtime["summary_task"], main.hub.transcript_path,
     main.runtime["translator"]) = saved


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class FakeTranslator:
    """只要 chat_with_retry 与 model（与 tests/test_summarize.FakeTranslator 同形）。"""

    def __init__(self):
        self.model = "deepseek-chat"
        self.calls = 0

    async def chat_with_retry(self, payload: dict) -> str:
        self.calls += 1
        system = payload["messages"][0]["content"]
        if system == summarize.MAP_SYSTEM:
            return "### 要点\n- 一块要点"
        if system == summarize.REDUCE_SYSTEM:
            return "## 概览\nAI 新版\n\n## 要点\n- 合并后的要点"
        raise AssertionError("未知 system prompt")


async def _wait(pred, timeout: float = 3.0) -> bool:
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


# ---------------- 备份：命名 / 内容 / 不覆盖静默 ----------------

def test_backup_path_naming():
    assert summarize.backup_path(Path(SUMMARY)).name == BACKUP
    assert summarize.backup_path(Path("lecture_第3讲.summary.md")).name == \
        "lecture_第3讲.summary.bak.md"


def test_backup_existing_none_when_absent(tmp_path):
    assert summarize.backup_existing(tmp_path / SUMMARY) is None


def test_backup_existing_copies_content(tmp_path):
    src = tmp_path / SUMMARY
    src.write_text("第一版（人工改过）\n", encoding="utf-8")
    got = summarize.backup_existing(src)
    assert got is not None and got.name == BACKUP
    assert got.read_text(encoding="utf-8") == "第一版（人工改过）\n"
    assert src.read_text(encoding="utf-8") == "第一版（人工改过）\n"   # 源文件不动
    # 第二次覆盖：备份刷新为**最近一版**（旧内容被下一次覆盖前的版本取代）
    src.write_text("第二版\n", encoding="utf-8")
    summarize.backup_existing(src)
    assert got.read_text(encoding="utf-8") == "第二版\n"


def test_atomic_write_leaves_no_tmp(tmp_path):
    out = tmp_path / SUMMARY
    summarize.atomic_write_text(out, "内容\n")
    assert out.read_text(encoding="utf-8") == "内容\n"
    assert [p.name for p in tmp_path.iterdir()] == [SUMMARY]


async def test_summarize_makes_backup_before_overwrite(tmp_path):
    src = tmp_path / "lecture_x.md"
    src.write_text("# 双语课堂笔记\n\n- `12:00:01` chunk content\n", encoding="utf-8")
    out = tmp_path / "lecture_x.summary.md"
    out.write_text("人工修订版：这里是我的笔记\n", encoding="utf-8")
    res = await summarize.TranscriptSummarizer(FakeTranslator()).summarize(src, out)
    assert res["backup"] is not None
    bak = tmp_path / "lecture_x.summary.bak.md"
    assert bak.read_text(encoding="utf-8") == "人工修订版：这里是我的笔记\n"
    assert "## 概览" in out.read_text(encoding="utf-8")        # 新摘要已覆盖
    assert res["text"] == out.read_text(encoding="utf-8")


async def test_summarize_no_backup_on_first_run(tmp_path):
    src = tmp_path / "lecture_x.md"
    src.write_text("# 双语课堂笔记\n\n- `12:00:01` chunk content\n", encoding="utf-8")
    out = tmp_path / "lecture_x.summary.md"
    res = await summarize.TranscriptSummarizer(FakeTranslator()).summarize(src, out)
    assert res["backup"] is None
    assert not (tmp_path / "lecture_x.summary.bak.md").exists()


def test_backup_is_not_a_note_file():
    """备份文件不进笔记列表/索引（派生物后缀口径）。"""
    assert not notes_mod.is_note_file(Path(BACKUP))
    assert not notes_mod.is_note_file(Path(SUMMARY))
    assert notes_mod.SUMMARY_BACKUP_SUFFIX in notes_mod.SIDECAR_SUFFIXES
    assert notes_mod.SUMMARY_SUFFIX in notes_mod.DERIVED_SUFFIXES


def test_index_excludes_summary_and_backup():
    write_note()
    write_summary()
    write_summary("上一版\n", name=BACKUP)
    idx = notes_mod.index(main.TRANSCRIPTS)
    assert [n["name"] for n in idx["notes"]] == [NOTE]
    assert idx["notes"][0]["has_summary"] is True


# ---------------- GET /api/notes/summary ----------------

async def test_summary_get_when_absent(sum_env):
    write_note()
    async with asgi_client() as c:
        r = await c.get("/api/notes/summary", params={"name": NOTE})
    assert r.status_code == 200                     # 「还没生成」不是错误
    j = r.json()
    assert j["ok"] is True and j["exists"] is False
    assert j["summary"] == SUMMARY and j["backup"] == BACKUP
    assert j["text"] == "" and j["sha"] is None and j["has_backup"] is False


async def test_summary_get_returns_text_sha_and_backup_flag(sum_env):
    write_note()
    write_summary()
    write_summary("上一版\n", name=BACKUP)
    async with asgi_client() as c:
        r = await c.get("/api/notes/summary", params={"name": NOTE})
    j = r.json()
    assert j["exists"] is True and "AI 版" in j["text"]
    assert j["sha"] == main.text_sha(j["text"])
    assert j["has_backup"] is True and j["size"] > 0 and j["mtime_us"] > 0


@pytest.mark.parametrize("bad", ["../../config.json", "config.json",
                                 SUMMARY, BACKUP, "lecture_不存在.md"])
async def test_summary_get_rejects_bad_names(sum_env, bad):
    """名字白名单 + **派生物不可直接寻址**（.summary.md/.summary.bak.md 传进来一律 404）。"""
    write_note()
    write_summary()
    async with asgi_client() as c:
        r = await c.get("/api/notes/summary", params={"name": bad})
    assert r.status_code == 404, bad
    assert r.json()["ok"] is False


# ---------------- PUT /api/notes/summary ----------------

async def test_summary_put_saves_in_place(sum_env):
    write_note()
    write_summary()
    ws = FakeWS()
    main.hub.clients.add(ws)
    try:
        async with asgi_client() as c:
            j = (await c.get("/api/notes/summary", params={"name": NOTE})).json()
            r = await c.put("/api/notes/summary",
                            json={"name": NOTE, "text": "# 我的修订\n\n- 手写要点\n",
                                  "base_sha": j["sha"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["summary"] == SUMMARY
        assert body["sha"] == main.text_sha("# 我的修订\n\n- 手写要点\n")
        on_disk = (main.TRANSCRIPTS / SUMMARY).read_text(encoding="utf-8")
        assert "我的修订" in on_disk and "AI 版" not in on_disk
        # 重索引：广播 notes（别的窗口刷新预览）
        msgs = [json.loads(m) for m in ws.sent]
        assert any(m.get("type") == "notes" and NOTE in (m.get("files") or [])
                   for m in msgs)
        # 再读：指纹随之更新（编辑器可连续编辑）
        async with asgi_client() as c:
            again = (await c.get("/api/notes/summary", params={"name": NOTE})).json()
        assert again["sha"] == body["sha"]
    finally:
        main.hub.clients.discard(ws)


async def test_summary_put_stale_sha_409_and_file_untouched(sum_env):
    write_note()
    write_summary()
    async with asgi_client() as c:
        j = (await c.get("/api/notes/summary", params={"name": NOTE})).json()
        write_summary("# 概览\n被重新生成过\n")          # 模拟期间又生成了一次
        r = await c.put("/api/notes/summary",
                        json={"name": NOTE, "text": "旧编辑器提交\n",
                              "base_sha": j["sha"]})
    assert r.status_code == 409 and "已被更新" in r.json()["error"]
    assert "被重新生成过" in (main.TRANSCRIPTS / SUMMARY).read_text(encoding="utf-8")


async def test_summary_put_requires_base_sha(sum_env):
    write_note()
    write_summary()
    async with asgi_client() as c:
        r = await c.put("/api/notes/summary", json={"name": NOTE, "text": "x"})
    assert r.status_code == 400 and "base_sha" in r.json()["error"]


@pytest.mark.parametrize("body,why", [
    ({"name": NOTE, "text": 123}, "text 类型"),
    ({"name": NOTE, "text": "x", "base_sha": "abc", "extra": 1}, "未知字段"),
])
async def test_summary_put_rejects_bad_body(sum_env, body, why):
    write_note()
    write_summary()
    async with asgi_client() as c:
        r = await c.put("/api/notes/summary", json=body)
    assert r.status_code == 400, why


async def test_summary_put_404_when_no_summary_yet(sum_env):
    """没有摘要 → 404（**不接受凭空创建**：那会让用户以为写的是 AI 摘要）。"""
    write_note()
    async with asgi_client() as c:
        r = await c.put("/api/notes/summary",
                        json={"name": NOTE, "text": "凭空写的", "base_sha": "x"})
    assert r.status_code == 404 and "还没有摘要" in r.json()["error"]
    assert not (main.TRANSCRIPTS / SUMMARY).exists()


async def test_summary_put_404_on_bad_or_missing_note(sum_env):
    write_note()
    write_summary()
    async with asgi_client() as c:
        for bad in ("../config.json", SUMMARY, "lecture_不存在.md"):
            r = await c.put("/api/notes/summary",
                            json={"name": bad, "text": "x", "base_sha": "y"})
            assert r.status_code == 404, bad


async def test_summary_put_too_large_400(sum_env, monkeypatch):
    write_note()
    write_summary()
    monkeypatch.setattr(main, "NOTE_EDIT_MAX", 8)
    async with asgi_client() as c:
        r = await c.put("/api/notes/summary",
                        json={"name": NOTE, "text": "很长的内容" * 4, "base_sha": "x"})
    assert r.status_code == 400 and "过大" in r.json()["error"]


# ---------------- 预览 / 生成 端点里的 §6 字段 ----------------

async def test_preview_reports_summary_flags(sum_env):
    write_note()
    async with asgi_client() as c:
        j = (await c.get("/api/notes/preview", params={"name": NOTE})).json()
    assert j["has_summary"] is False and j["has_summary_backup"] is False
    write_summary()
    write_summary("上一版\n", name=BACKUP)
    async with asgi_client() as c:
        j2 = (await c.get("/api/notes/preview", params={"name": NOTE})).json()
    assert j2["has_summary"] is True and j2["has_summary_backup"] is True


async def test_summarize_endpoint_reports_overwrite_and_backup(sum_env):
    """已有摘要 → 响应回 overwrite/backup（前端确认框/提示用它）；
    跑完后 status['summary']['backup'] 记下备份文件名，且备份内容=旧版。"""
    hub = main.Hub()
    tpath = hub.start_transcript()
    hub.append("12:00:01", "alpha beta", "", None)
    main.hub.transcript_path = tpath
    main.runtime["translator"] = FakeTranslator()
    old = write_summary("人工修订版内容\n", name=tpath.stem + ".summary.md")
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    j = r.json()
    assert j["ok"] is True and j["overwrite"] is True
    assert j["backup"] == tpath.stem + ".summary.bak.md"
    task = main.runtime["summary_task"]
    assert await _wait(lambda: main.status["summary"]["state"] == "done")
    await task
    assert main.status["summary"]["backup"] == tpath.stem + ".summary.bak.md"
    assert (main.TRANSCRIPTS / (tpath.stem + ".summary.bak.md")).read_text(
        encoding="utf-8") == "人工修订版内容\n"
    assert old.read_text(encoding="utf-8") != "人工修订版内容\n"


async def test_summarize_endpoint_first_run_has_no_backup(sum_env):
    hub = main.Hub()
    tpath = hub.start_transcript()
    hub.append("12:00:01", "alpha beta", "", None)
    main.hub.transcript_path = tpath
    main.runtime["translator"] = FakeTranslator()
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    j = r.json()
    assert j["overwrite"] is False and j["backup"] is None
    task = main.runtime["summary_task"]
    assert await _wait(lambda: main.status["summary"]["state"] == "done")
    await task
    assert main.status["summary"]["backup"] is None
    assert not (main.TRANSCRIPTS / (tpath.stem + ".summary.bak.md")).exists()


async def test_summary_put_rejects_while_generating(sum_env):
    """§6 评审③：生成中不许改摘要。

    summarize 的收尾会把「当时盘上的那份」当旧版备份再用 AI 版覆盖正文——用户在生成
    期间保存的修订会立刻缩进 .bak.md，而界面刚显示过「保存成功」（看得见的现象是
    「我一保存正文就变了」）。守卫与 POST 入口同一道：409 + 明确文案，文件不动。"""
    write_note()
    write_summary("## 概览\n人工版（不该被生成撞掉的）\n")
    main.runtime["summarizing"] = True          # 模拟「摘要正在生成中」
    try:
        async with asgi_client() as c:
            j = (await c.get("/api/notes/summary", params={"name": NOTE})).json()
            r = await c.put("/api/notes/summary",
                            json={"name": NOTE, "text": "生成期间提交\n",
                                  "base_sha": j["sha"]})
        assert r.status_code == 409, r.text
        assert "生成中" in r.json()["error"]
        assert "人工版" in (main.TRANSCRIPTS / SUMMARY).read_text(encoding="utf-8")
    finally:
        main.runtime["summarizing"] = False
    # 生成结束（守卫释放）后同样能存——守卫只挡生成期间
    async with asgi_client() as c:
        j = (await c.get("/api/notes/summary", params={"name": NOTE})).json()
        r2 = await c.put("/api/notes/summary",
                         json={"name": NOTE, "text": "生成结束后提交\n",
                               "base_sha": j["sha"]})
    assert r2.status_code == 200 and "生成结束后提交" in (
        main.TRANSCRIPTS / SUMMARY).read_text(encoding="utf-8")


async def test_atomic_tmp_names_are_unique(tmp_path, monkeypatch):
    """§6 评审④低：摘要正文被两条路径写（summarize 生成 / main 人工保存），临时名都叫
    `<名>.tmp` 时会共用一个临时文件——任一侧挪进线程池就会互踩（第②条那类混合产物的
    复现路径）。两边都改成唯一名后，两次写入用的临时文件必须不同、且各自落盘完整。

    这里**确定性**验证：记录每次 replace 的源文件名，而不是并发跑（Windows 上两个线程
    同时 rename 同一目标会被杀软/索引器的瞬时占用打成 PermissionError，那是文件系统
    语义不是本条的缺陷；并发不混合的性质由 test_export_dir 的 8 线程用例覆盖）。"""
    target = tmp_path / "lecture_x.summary.md"
    target.write_text("初始\n", encoding="utf-8")
    srcs: list[str] = []
    real_replace = Path.replace

    def spy(self, dst):
        srcs.append(self.name)
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", spy)
    body_a, body_b = "# A\n" * 200, "# B\n" * 200
    summarize.atomic_write_text(target, body_a)
    assert target.read_text(encoding="utf-8") == body_a
    main.note_atomic_write(target, body_b, newline=None)
    assert len(srcs) == 2 and srcs[0] != srcs[1], f"两次写入共用了临时文件: {srcs}"
    assert all(s.endswith(".tmp") for s in srcs)
    assert "# B" in target.read_text(encoding="utf-8")     # 后写者完整胜出
    assert [p.name for p in tmp_path.iterdir()] == [target.name], \
        f"不得留临时文件: {[p.name for p in tmp_path.iterdir()]}"


# ---------------- 前端布线（结构断言） ----------------

def test_index_html_has_summary_edit_ui():
    for anchor in ("sumMask", "sumText", "sumSave", "openSummaryEdit",
                   "saveSummaryEdit", "renderSummaryPanel", "startSummary",
                   "/api/notes/summary", "summary.bak.md"):
        assert anchor in HTML, f"index.html 缺少 {anchor}"
    assert "'sumMask'" in HTML and "sumMask" in HTML.split("const MASKS")[1][:200]


def test_index_summary_confirm_mentions_backup():
    """确认框文案必须点名备份去处（§6：先备份为 *.summary.bak.md）。"""
    seg = HTML.split("async function startSummary", 1)[1].split("async function", 1)[0]
    assert "confirm(" in seg and ".summary.bak.md" in seg
