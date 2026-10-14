# -*- coding: utf-8 -*-
"""tests/test_notes.py — p2-2 笔记库：解析/索引/检索/端点（离线秒级）。

三层：
1) notes.py 纯函数（解析 EN↔ZH 配对、首句摘要、日期、反越界）；
2) index() 的排序/全文检索（含中文子串）/课程过滤/摘要卡组标记；
3) /api/notes/list|preview|file 端点 + summarize/deck 的 name 参数化向后兼容。

路径全部经 conftest 重定向到 tmp_path（TRANSCRIPTS）。其中 real_transcripts
用例显式读**仓库真实** transcripts/（只读，不写）。
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import notes as notes_mod
import summarize as summarize_mod

REAL_TRANSCRIPTS = Path(__file__).resolve().parent.parent / "transcripts"


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def write_note(name: str, body: str) -> Path:
    """在（被重定向的）TRANSCRIPTS 下落一篇笔记。"""
    d = main.TRANSCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def sample_note(stamp="20261013_0146", course="计算机科学导论", pairs=(("01:46:42", "All right.", "好的。"),)):
    lines = [f"# 双语课堂笔记 {stamp}", "",
             "> LectureLens 自动生成（EN 原文 / ZH 翻译）"]
    if course:
        lines.append(f"> 课程：{course}")
    lines += ["", "---", ""]
    for i, (ts, en, zh) in enumerate(pairs, start=1):
        lines.append(f"- `{ts}` {en}")
        if zh:
            lines.append(f"  - `#{i}` {zh}")
    return "\n".join(lines) + "\n"


# —— 1) 反越界：safe_note_path ——

def test_safe_note_path_accepts_wellformed(tmp_path):
    got = notes_mod.safe_note_path(tmp_path, "lecture_20261013_0146.md")
    assert got == (tmp_path / "lecture_20261013_0146.md").resolve()


def test_safe_note_path_rejects_traversal_and_foreign_files(tmp_path):
    """核心安全断言：name 来自 HTTP 查询参数，必须挡住越界与任意文件读取。"""
    for bad in ("../config.json", "..\\config.json", "transcripts/../../.env",
                "C:/Windows/win.ini", "/etc/passwd", "sub/lecture_20261013_0146.md",
                "config.json", ".env", "lecture_20261013_0146.summary.md",
                "lecture_20261013_0146.apkg", "notes.txt",
                "lecture_20261013_0146.md.bak", "", None, 123, "x" * 200):
        assert notes_mod.safe_note_path(tmp_path, bad) is None, f"应拒绝: {bad!r}"


def test_safe_note_path_rejects_nested_subdir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "lecture_20261013_0146.md").write_text("x", encoding="utf-8")
    assert notes_mod.safe_note_path(tmp_path, "lecture_20261013_0146.md") is not None
    assert notes_mod.safe_note_path(sub, "lecture_20261013_0146.md") is not None


def test_is_note_file_excludes_derived(tmp_path):
    assert notes_mod.is_note_file(Path("lecture_20261013_0146.md"))
    assert not notes_mod.is_note_file(Path("lecture_20261013_0146.summary.md"))
    assert not notes_mod.is_note_file(Path("lecture_20261013_0146.apkg"))


# —— 2) 解析 ——

def test_parse_header_and_pairs():
    text = sample_note(pairs=[("01:46:42", "All right.", "好的。"),
                              ("01:46:46", "The rest go.", "其余的继续。")])
    got = notes_mod.parse(text)
    assert got["stamp"] == "20261013_0146"
    assert got["course"] == "计算机科学导论"
    assert got["segments"] == 2 and got["paired"] == 2
    # B4 体量：words = EN+ZH 字符数（列表体量徽章的 M；明细见 test_note_size.py）
    assert got["words"] == (len("All right.") + len("好的。")
                            + len("The rest go.") + len("其余的继续。"))
    assert [p["seg"] for p in got["pairs"]] == [1, 2]
    assert got["pairs"][0] == {"seg": 1, "ts": "01:46:42",
                               "en": "All right.", "zh": "好的。"}
    assert got["pairs"][1]["en"] == "The rest go."


def test_parse_no_course_header_is_uncategorized():
    got = notes_mod.parse(sample_note(course=None))
    assert got["course"] is None            # 归属归一在端点/索引层做


def test_parse_english_only_note_has_none_zh():
    """实测存在纯英文场次（未配 API key）：ZH 为 None，不得报错。"""
    got = notes_mod.parse(sample_note(pairs=[("01:46:42", "All right.", None),
                                             ("01:46:46", "Second.", None)]))
    assert got["segments"] == 2 and got["paired"] == 0
    assert got["has_zh"] is False
    assert [p["zh"] for p in got["pairs"]] == [None, None]


def test_parse_zh_out_of_order_still_pairs_by_seg():
    """ZH 迟到导致「缩进落在别的 EN 之下」时，靠 #seg 锚点仍正确配对。"""
    text = ("# 双语课堂笔记 20261013_0146\n\n> LectureLens 自动生成（EN 原文 / ZH 翻译）\n\n---\n\n"
            "- `t1` FIRST EN\n"
            "  - `#2` 第二句的中文\n"
            "  - `#1` 第一句的中文\n"
            "- `t2` SECOND EN\n")
    got = notes_mod.parse(text)
    assert got["pairs"][0]["en"] == "FIRST EN"
    assert got["pairs"][0]["zh"] == "第一句的中文"     # 按 seg 而非缩进
    assert got["pairs"][1]["en"] == "SECOND EN"
    assert got["pairs"][1]["zh"] == "第二句的中文"


def test_parse_legacy_untagged_zh_falls_back_positionally():
    """老格式 ZH 行无 #seg：按顺序补给还没有 ZH 的 EN，不破坏有标号的配对。"""
    text = ("- `t1` EN ONE\n  - 中文一\n- `t2` EN TWO\n  - 中文二\n")
    got = notes_mod.parse(text)
    assert [p["zh"] for p in got["pairs"]] == ["中文一", "中文二"]


def test_parse_empty_text():
    got = notes_mod.parse("")
    assert got["segments"] == 0 and got["pairs"] == [] and got["stamp"] is None


def test_first_sentence_prefers_en_and_truncates():
    pairs = [{"seg": 1, "ts": "t", "en": "", "zh": "只有中文"},
             {"seg": 2, "ts": "t", "en": "x" * 300, "zh": "y" * 300}]
    en, zh = notes_mod.first_sentence(pairs)
    assert en.endswith("…") and len(en) == notes_mod.FIRST_SENT_MAX + 1
    assert len(zh) == notes_mod.FIRST_SENT_MAX + 1
    assert notes_mod.first_sentence([]) == ("", "")


def test_first_sentence_zh_only_note():
    en, zh = notes_mod.first_sentence(
        [{"seg": 1, "ts": "t", "en": "  ", "zh": "只有中文"}])
    assert en == "" and zh == "只有中文"


def test_display_date():
    assert notes_mod.display_date(None, "lecture_20261013_0146.md") == "2026-10-13 01:46"
    assert notes_mod.display_date("20260101_0900", "weird.md") == "2026-01-01 09:00"


# —— 3) 索引 / 检索 ——

def test_index_lists_and_sorts_desc(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note(stamp="20260101_0900", course="甲"))
    write_note("lecture_20260202_1000.md", sample_note(stamp="20260202_1000", course="乙"))
    got = notes_mod.index(main.TRANSCRIPTS)
    assert [n["name"] for n in got["notes"]] == [
        "lecture_20260202_1000.md", "lecture_20260101_0900.md"]   # 时间倒序
    assert got["total"] == 2


def test_index_reports_summary_and_deck_flags(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note())
    assert notes_mod.index(main.TRANSCRIPTS)["notes"][0]["has_summary"] is False
    (main.TRANSCRIPTS / "lecture_20260101_0900.summary.md").write_text("s", encoding="utf-8")
    (main.TRANSCRIPTS / "lecture_20260101_0900.apkg").write_bytes(b"PK")
    n = notes_mod.index(main.TRANSCRIPTS)["notes"][0]
    assert n["has_summary"] is True and n["has_deck"] is True


def test_index_uncategorized_when_no_course_header(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note(course=None))
    assert notes_mod.index(main.TRANSCRIPTS)["notes"][0]["course"] == "未分类"


def test_index_search_chinese_substring(tmp_path):
    """中文检索靠子串（无分词器）：整词与片段都能命中。"""
    write_note("lecture_20260101_0900.md", sample_note(pairs=[
        ("t1", "The rest of the networks go.", "其余的神经网络继续运行。"),
        ("t2", "Attention matters.", "注意力很重要。")]))
    write_note("lecture_20260202_1000.md", sample_note(pairs=[
        ("t1", "Linear algebra today.", "今天讲线性代数。")]))
    for q in ("神经网络", "网络", "注意力", "线性代数"):
        got = notes_mod.index(main.TRANSCRIPTS, query=q)
        assert got["total"] == 1, f"{q} 应命中 1 篇"
    assert notes_mod.index(main.TRANSCRIPTS, query="神经网络")["notes"][0]["name"] \
        == "lecture_20260101_0900.md"
    assert notes_mod.index(main.TRANSCRIPTS, query="不存在的词")["total"] == 0


def test_index_search_english_case_insensitive(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note(pairs=[("t1", "Attention matters.", "注意力。")]))
    assert notes_mod.index(main.TRANSCRIPTS, query="attention")["total"] == 1
    assert notes_mod.index(main.TRANSCRIPTS, query="ATTENTION")["total"] == 1


def test_index_search_matches_course_and_filename(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note(course="线性代数"))
    assert notes_mod.index(main.TRANSCRIPTS, query="线性代数")["total"] == 1
    assert notes_mod.index(main.TRANSCRIPTS, query="20260101")["total"] == 1


def test_index_course_filter(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note(course="甲"))
    write_note("lecture_20260202_1000.md", sample_note(course="乙"))
    got = notes_mod.index(main.TRANSCRIPTS, course="乙")
    assert [n["name"] for n in got["notes"]] == ["lecture_20260202_1000.md"]


def test_index_missing_dir_and_corrupt_file(tmp_path):
    assert notes_mod.index(tmp_path / "nope")["total"] == 0
    # 坏文件（非法 UTF-8）只跳过该篇，不整体失败
    d = main.TRANSCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    (d / "lecture_20260101_0900.md").write_bytes(b"\xff\xfe not utf-8")
    write_note("lecture_20260202_1000.md", sample_note())
    got = notes_mod.index(d)
    assert got["total"] == 1


def test_index_excludes_summary_files(tmp_path):
    write_note("lecture_20260101_0900.md", sample_note())
    (main.TRANSCRIPTS / "lecture_20260101_0900.summary.md").write_text(
        "# 摘要\n", encoding="utf-8")
    got = notes_mod.index(main.TRANSCRIPTS)
    assert [n["name"] for n in got["notes"]] == ["lecture_20260101_0900.md"]


def test_real_transcripts_parse_cleanly():
    """对仓库真实 transcripts/ 全量跑解析（只读）——防止格式漂移让解析器静默失配。"""
    if not REAL_TRANSCRIPTS.is_dir():
        pytest.skip("no real transcripts")
    got = notes_mod.index(REAL_TRANSCRIPTS)
    assert got["total"] > 0
    bad = []
    for n in got["notes"]:
        if n["segments"] == 0 and n["size"] > 200:
            bad.append(n["name"])          # 有实质内容却解析不出任何段
        # B4：有段落就必然有体量（words 漏接/写错在真实数据上立刻现形）
        if n["segments"] > 0 and not n["words"] > 0:
            bad.append(n["name"] + "（体量为 0）")
    assert not bad, f"这些笔记解析不出段落: {bad}"


# —— 4) 端点 ——

async def test_list_endpoint_shape_and_search():
    write_note("lecture_20260101_0900.md", sample_note(
        course="计算机科学导论",
        pairs=[("t1", "Neural networks.", "神经网络。")]))
    async with asgi_client() as c:
        r = await c.get("/api/notes/list")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["total"] == 1
        n = body["notes"][0]
        assert n["date"] == "2026-01-01 09:00"
        assert n["course"] == "计算机科学导论"
        assert n["first_en"] == "Neural networks."
        assert n["has_summary"] is False and n["has_deck"] is False
        assert n["words"] == len("Neural networks.") + len("神经网络。")   # B4 体量
        # 中文检索
        assert (await c.get("/api/notes/list", params={"q": "神经网络"})).json()["total"] == 1
        assert (await c.get("/api/notes/list", params={"q": "无此词"})).json()["total"] == 0
        # 课程过滤
        assert (await c.get("/api/notes/list",
                            params={"course": "计算机科学导论"})).json()["total"] == 1
        assert (await c.get("/api/notes/list", params={"course": "别的课"})).json()["total"] == 0


async def test_list_without_transcripts_dir_is_empty_not_500():
    async with asgi_client() as c:
        r = await c.get("/api/notes/list")
        assert r.status_code == 200 and r.json()["notes"] == []


async def test_preview_endpoint_pairs():
    write_note("lecture_20260101_0900.md", sample_note(pairs=[
        ("01:46:42", "All right.", "好的。"),
        ("01:46:46", "Second.", "第二。")]))
    async with asgi_client() as c:
        r = await c.get("/api/notes/preview", params={"name": "lecture_20260101_0900.md"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["segments"] == 2
        assert body["pairs"][0]["en"] == "All right."
        assert body["pairs"][0]["zh"] == "好的。"
        assert body["course"] == "计算机科学导论"


async def test_preview_rejects_traversal():
    async with asgi_client() as c:
        for bad in ("../config.json", "C:/Windows/win.ini", "config.json"):
            r = await c.get("/api/notes/preview", params={"name": bad})
            assert r.status_code == 404, f"应拒绝 {bad}"
            assert "非法笔记名" in r.json()["error"]


async def test_preview_unknown_note_404():
    async with asgi_client() as c:
        r = await c.get("/api/notes/preview",
                        params={"name": "lecture_20990101_0000.md"})
        assert r.status_code == 404
        assert "不存在" in r.json()["error"]


async def test_file_endpoint_downloads_named_note():
    write_note("lecture_20260101_0900.md", sample_note())
    async with asgi_client() as c:
        r = await c.get("/api/notes/file", params={"name": "lecture_20260101_0900.md"})
        assert r.status_code == 200
        assert "双语课堂笔记" in r.text
        assert (await c.get("/api/notes/file",
                            params={"name": "../config.json"})).status_code == 404


@pytest.fixture
def current_note(monkeypatch):
    """把全局 main.hub 换成一个已开课、已记一段的临时 Hub（monkeypatch 自动还原）。

    端点读的是模块级 main.hub，测试里自建 Hub() 不会被端点看到——故必须换掉全局，
    而不是造个局部实例。"""
    hub = main.Hub()
    monkeypatch.setattr(main, "hub", hub)
    p = hub.start_transcript("某课")
    hub.append("10:00:01", "hello world", "", 1)
    return hub, p


async def test_legacy_get_api_notes_still_serves_current_note(current_note):
    """向后兼容：GET /api/notes（无参数）仍是当前笔记的 md 下载。"""
    async with asgi_client() as c:
        r = await c.get("/api/notes")
        assert r.status_code == 200 and "hello world" in r.text
        # 新端点缺省也指向当前笔记
        assert (await c.get("/api/notes/file")).status_code == 200
        assert (await c.get("/api/notes/preview")).json()["is_current"] is True


async def test_summarize_and_deck_still_accept_bodyless_calls():
    """向后兼容：两个 POST 历史上不带请求体（smoke_test/既有用例），不得 500。"""
    async with asgi_client() as c:
        r1 = await c.post("/api/notes/summarize")          # 无体 + 无笔记 → 404 而非 500
        assert r1.status_code == 404
        r2 = await c.post("/api/notes/deck")
        assert r2.status_code == 404


async def test_summarize_and_deck_reject_bad_name(current_note):
    """指定了非法 name → 明确 404，绝不静默回退到当前笔记。"""
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize", json={"name": "../../config.json"})
        assert r.status_code == 404 and "非法笔记名" in r.json()["error"]
        r2 = await c.post("/api/notes/deck", json={"name": "nope.md"})
        assert r2.status_code == 404 and "非法笔记名" in r2.json()["error"]


class FakeMapReduce:
    """summarize 依赖面最小替身：chat_with_retry(payload)->str + model（无真实调用）。"""

    model = "deepseek-chat"

    async def chat_with_retry(self, payload: dict) -> str:
        if payload["messages"][0]["content"] == summarize_mod.MAP_SYSTEM:
            return "### 要点\n- 要点一"
        return ("## 概览\n概。\n\n## 要点\n- 一\n\n"
                "## 术语对照\n无\n\n## 存疑处\n无")


@pytest.fixture
def notes_env(monkeypatch):
    """复位 hub/摘要相关的模块级单例状态，测试互不污染（test_summarize 同款口径）。"""
    hub = main.Hub()
    monkeypatch.setattr(main, "hub", hub)
    saved = (main.status["summary"], main.runtime["summarizing"],
             main.runtime["summary_task"], main.runtime["translator"])
    main.status["summary"] = {"state": "idle", "done": 0, "total": 0,
                              "failed": 0, "path": None, "error": None}
    main.runtime["summarizing"] = False
    main.runtime["summary_task"] = None
    main.runtime["translator"] = None
    yield hub
    (main.status["summary"], main.runtime["summarizing"],
     main.runtime["summary_task"], main.runtime["translator"]) = saved


async def test_summarize_targets_named_historical_note(notes_env):
    """p2-2 核心：带 name 时摘要必须落到**那篇历史笔记**上，而非当前笔记。

    这是「从列表发起摘要」的要害——指错文件会把摘要写到用户没选的那篇上。"""
    cur = write_note("lecture_20260101_0900.md",
                     sample_note(pairs=[("t1", "current note", "当前笔记。")]))
    hist = write_note("lecture_20260202_1000.md",
                      sample_note(pairs=[("t1", "history one", "历史一。"),
                                         ("t2", "history two", "历史二。")]))
    notes_env.transcript_path = cur            # 当前笔记 = cur
    main.runtime["translator"] = FakeMapReduce()
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize", json={"name": hist.name})
        assert r.status_code == 200
        j = r.json()
        assert j["transcript"] == hist.name                    # 目标是历史那篇
        assert j["output"] == hist.stem + ".summary.md"
        for _ in range(200):                                   # 等后台任务落盘
            if (main.TRANSCRIPTS / (hist.stem + ".summary.md")).is_file():
                break
            await asyncio.sleep(0.02)
    assert (main.TRANSCRIPTS / (hist.stem + ".summary.md")).is_file()
    assert not (main.TRANSCRIPTS / (cur.stem + ".summary.md")).exists(), \
        "摘要写到了当前笔记旁边——name 参数没生效"


async def test_deck_named_note_requires_its_own_summary(notes_env):
    """卡组的「先有摘要」校验必须按**目标那篇**判断，不能误用当前笔记的摘要。"""
    cur = write_note("lecture_20260101_0900.md", sample_note())
    hist = write_note("lecture_20260202_1000.md", sample_note())
    (main.TRANSCRIPTS / (cur.stem + ".summary.md")).write_text("s", encoding="utf-8")
    notes_env.transcript_path = cur
    main.runtime["translator"] = FakeMapReduce()
    async with asgi_client() as c:
        r = await c.post("/api/notes/deck", json={"name": hist.name})
        assert r.status_code == 409
        assert "尚无摘要文件" in r.json()["error"]
        assert main.runtime["deck_running"] is False    # 未起任务


async def test_deck_download_named_note():
    write_note("lecture_20260101_0900.md", sample_note())
    async with asgi_client() as c:
        assert (await c.get("/api/notes/deck",
                            params={"name": "lecture_20260101_0900.md"})).status_code == 404
        (main.TRANSCRIPTS / "lecture_20260101_0900.apkg").write_bytes(b"PK\x03\x04")
        r = await c.get("/api/notes/deck", params={"name": "lecture_20260101_0900.md"})
        assert r.status_code == 200 and r.content.startswith(b"PK")
