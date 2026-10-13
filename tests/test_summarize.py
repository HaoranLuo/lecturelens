# -*- coding: utf-8 -*-
"""tests/test_summarize.py — 课后 AI 笔记摘要离线单测（升级计划条目 17）。

覆盖：
- 分块纯函数：EN 行抽取（写侧 main.Hub.append 格式 vs 读侧解析不漂移）、
  预算内整行进出（绝不切断句子）、单行超预算独占一块；
- map-reduce 编排（fake translator 注入 chat_with_retry——条目 6 重试封装的
  依赖面）：happy path、单块失败标记不整体失败、全部块失败跳过 reduce、
  reduce 失败降级本地拼接、semaphore=4 并发上限、进度回调序列、空转写拒绝；
- POST /api/notes/summarize 端点布线（ASGI 直调，不进 lifespan）：404/409
  前置校验、受理即返回、状态机 idle→running→done、WS type=summary 广播。
LLM 真实调用由 smoke_test / 真机自调覆盖；本文件全部离线秒级。"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

import main
import summarize
from summarize import (TranscriptSummarizer, chunk_lines, chunk_transcript,
                       extract_en_lines)

FINAL_MD = ("## 概览\n本课讲排序算法。\n\n## 要点\n- 快排平均 O(n log n)\n\n"
            "## 术语对照\n- quicksort = 快速排序\n\n## 存疑处\n无")


class FakeWS:
    """test_hub 同款假客户端（本地副本避免跨测试文件 import）。"""

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


# ---------------- 分块纯函数 ----------------

def test_extract_en_lines_parses_hub_format():
    """EN 行 = 顶格 `- `ts` 文本`；ZH 缩进续行/标题/---/引导行全部跳过。"""
    text = "\n".join([
        "# 双语课堂笔记 20261012_1530",
        "",
        "> LectureLens 自动生成（EN 原文 / ZH 翻译）",
        "",
        "---",
        "",
        "- `12:00:01` Today we discuss gradient descent.",
        "  - `#1` 今天我们讨论梯度下降。",
        "- `12:00:20` The learning rate controls the step size.",
        "  - 学习率控制步长。",   # 无 seg 锚的 ZH 行（main.append tag=None 形状）
    ])
    assert extract_en_lines(text) == [
        "Today we discuss gradient descent.",
        "The learning rate controls the step size.",
    ]


def test_extract_en_lines_zh_must_not_leak_via_strip():
    """回归锁定：ZH 行判定必须在 strip 之前——`  - `#3` 中文` strip 后形似
    EN 行，解析器若先 strip 会把译文当原文混进摘要。"""
    text = "- `12:00:01` real english\n  - `#1` 中文翻译不该出现"
    out = extract_en_lines(text)
    assert out == ["real english"]
    assert all("中文" not in x for x in out)


def test_extract_en_lines_empty_and_malformed():
    assert extract_en_lines("") == []
    assert extract_en_lines("# 只有标题\n---\n  - `#1` 只有中文") == []
    # 起始反引号无闭合（残缺行）跳过不炸；空内容 EN 行跳过
    assert extract_en_lines("- `oops no closing\n- `12:00:02` ok") == ["ok"]
    assert extract_en_lines("- `12:00:01` \n- `12:00:02` ok") == ["ok"]


def test_chunk_lines_whole_lines_within_budget():
    lines = [f"line {i:02d} " + "x" * 30 for i in range(20)]  # 每行 ~37 字符
    chunks = chunk_lines(lines, max_chars=100)
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 1
    # 整行进出 + 顺序保持 + 无增删：按行重拼必须还原输入
    assert "\n".join(chunks).split("\n") == lines


def test_chunk_lines_single_oversized_line_own_chunk():
    big = "y" * 500
    chunks = chunk_lines(["short", big, "short2"], max_chars=100)
    assert chunks == ["short", big, "short2"]  # 超长行独占一块，内容完整不切断


def test_chunk_lines_empty():
    assert chunk_lines([]) == []


def test_chunk_transcript_matches_hub_writer_format():
    """漂移守卫：用真实 main.Hub 写一份 transcript，再解析分块——写侧格式
    变化必须在此暴露。"""
    hub = main.Hub()  # conftest 已把 TRANSCRIPTS 重定向 tmp_path
    p = hub.start_transcript()
    hub.append("12:00:01", "alpha beta gamma", "", None)
    hub.append("12:00:02", "", "阿尔法不该出现", 1)
    hub.append("12:00:03", "delta epsilon zeta", "", 2)
    chunks = chunk_transcript(p.read_text(encoding="utf-8"), max_chars=1000)
    assert len(chunks) == 1
    assert "alpha beta gamma" in chunks[0] and "delta epsilon zeta" in chunks[0]
    assert "阿尔法" not in chunks[0] and "双语课堂笔记" not in chunks[0]


# ---------------- map-reduce 编排（fake translator） ----------------

class FakeTranslator:
    """summarize 的依赖面最小化：chat_with_retry(payload)->str + model。
    按 system prompt 区分 map/reduce 调用；并发计数供 semaphore 上限断言。"""

    def __init__(self, map_fn=None, reduce_fn=None, delay: float = 0.0):
        self.model = "deepseek-chat"
        self.map_fn = map_fn or (lambda user: "### 要点\n- 通用要点")
        self.reduce_fn = reduce_fn or (lambda user: FINAL_MD)
        self.delay = delay
        self.calls: list[dict] = []
        self._concurrent = 0
        self.max_concurrent = 0

    async def chat_with_retry(self, payload: dict) -> str:
        self.calls.append(payload)
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            system = payload["messages"][0]["content"]
            user = payload["messages"][1]["content"]
            if system == summarize.MAP_SYSTEM:
                return self.map_fn(user)
            if system == summarize.REDUCE_SYSTEM:
                return self.reduce_fn(user)
            raise AssertionError(f"未知 system prompt: {system[:40]}")
        finally:
            self._concurrent -= 1


def _write_transcript(path, n_lines: int = 3) -> None:
    """n 行 ~51 字符的 EN 行（max_chunk_chars=50 时每行独占一块）。"""
    body = "\n".join(f"- `12:0{i:02d}:00` chunk content line {i} " + "z" * 30
                     for i in range(n_lines))
    path.write_text("# 双语课堂笔记\n\n" + body + "\n", encoding="utf-8")


async def test_summarize_happy_path(tmp_path):
    src = tmp_path / "lecture_x.md"
    _write_transcript(src)
    out = tmp_path / "lecture_x.summary.md"
    fake = FakeTranslator()

    result = await TranscriptSummarizer(fake, max_chunk_chars=50).summarize(src, out)

    assert result["total"] == 3 and result["failed"] == []
    assert result["reduce_ok"] is True and result["path"] == out
    assert out.is_file()
    assert "## 概览" in out.read_text(encoding="utf-8")
    maps = [c for c in fake.calls
            if c["messages"][0]["content"] == summarize.MAP_SYSTEM]
    reduces = [c for c in fake.calls
               if c["messages"][0]["content"] == summarize.REDUCE_SYSTEM]
    assert len(maps) == 3 and len(reduces) == 1
    # map payload 形状：自带 model/temperature/max_tokens（chat_with_retry 只管重试）
    assert maps[0]["model"] == "deepseek-chat" and maps[0]["stream"] is False


async def test_chunk_failure_marked_not_fatal(tmp_path):
    """条目17核心语义：单块重试耗尽只标记该块，其余照常，文件照写。"""
    def map_fn(user: str) -> str:
        if "第 2/3 块" in user:
            raise RuntimeError("boom")
        return "### 要点\n- ok"

    src = tmp_path / "lecture_x.md"
    _write_transcript(src)
    out = tmp_path / "lecture_x.summary.md"
    fake = FakeTranslator(map_fn=map_fn)

    result = await TranscriptSummarizer(fake, max_chunk_chars=50).summarize(src, out)

    assert result["failed"] == [1] and result["reduce_ok"] is True
    text = out.read_text(encoding="utf-8")
    assert "## 概览" in text                       # reduce 正常产物在前
    assert "1/3 块失败" in text and "第 2 块（RuntimeError: boom）" in text
    # 失败清单含块号与原因，固定落正文（不依赖 LLM 遵嘱）
    reduce_user = [c for c in fake.calls
                   if c["messages"][0]["content"] == summarize.REDUCE_SYSTEM
                   ][0]["messages"][1]["content"]
    assert "【第 2/3 块——摘要失败" in reduce_user   # reduce 素材含失败标注
    assert reduce_user.count("【第") == 3           # 成功块 + 失败块都进素材


async def test_all_chunks_fail_skips_reduce(tmp_path):
    """全部块失败：跳过 reduce（省一次必败调用），写失败清单文件，不抛。"""
    def map_fn(user: str) -> str:
        raise RuntimeError("always down")

    src = tmp_path / "lecture_x.md"
    _write_transcript(src)
    out = tmp_path / "lecture_x.summary.md"
    fake = FakeTranslator(map_fn=map_fn)

    result = await TranscriptSummarizer(fake, max_chunk_chars=50).summarize(src, out)

    assert result["failed"] == [0, 1, 2] and result["reduce_ok"] is False
    assert not [c for c in fake.calls
                if c["messages"][0]["content"] == summarize.REDUCE_SYSTEM]
    text = out.read_text(encoding="utf-8")
    assert "所有分块摘要均失败" in text and "3/3 块失败" in text


async def test_reduce_failure_falls_back_to_local_assembly(tmp_path):
    """reduce LLM 失败 → 降级为逐块摘要本地拼接，文件照写。"""
    def reduce_fn(user: str) -> str:
        raise RuntimeError("reduce boom")

    src = tmp_path / "lecture_x.md"
    _write_transcript(src)
    out = tmp_path / "lecture_x.summary.md"
    fake = FakeTranslator(reduce_fn=reduce_fn)

    result = await TranscriptSummarizer(fake, max_chunk_chars=50).summarize(src, out)

    assert result["failed"] == [] and result["reduce_ok"] is False
    text = out.read_text(encoding="utf-8")
    assert "降级拼接" in text
    assert "## 第 1 块" in text and "## 第 3 块" in text  # 块摘要原样保留


async def test_concurrency_capped_at_four(tmp_path):
    """条目17口径：semaphore=4——10 块并行 map 时同时在飞不得超过 4。"""
    src = tmp_path / "lecture_x.md"
    _write_transcript(src, n_lines=10)
    out = tmp_path / "lecture_x.summary.md"
    fake = FakeTranslator(delay=0.02)

    result = await TranscriptSummarizer(fake, max_chunk_chars=50,
                                        concurrency=4).summarize(src, out)

    assert result["total"] == 10 and result["failed"] == []
    assert fake.max_concurrent == 4


async def test_progress_callback_sequence(tmp_path):
    src = tmp_path / "lecture_x.md"
    _write_transcript(src)
    out = tmp_path / "lecture_x.summary.md"
    seen: list[tuple[int, int]] = []
    fake = FakeTranslator()
    await TranscriptSummarizer(fake, max_chunk_chars=50).summarize(
        src, out, on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]  # 分块后先报 (0,n) 再逐块


async def test_empty_transcript_raises_and_writes_nothing(tmp_path):
    src = tmp_path / "lecture_zh_only.md"
    src.write_text("# 双语课堂笔记\n\n  - `#1` 只有中文\n", encoding="utf-8")
    out = tmp_path / "lecture_zh_only.summary.md"
    with pytest.raises(ValueError):
        await TranscriptSummarizer(FakeTranslator()).summarize(src, out)
    assert not out.exists()


# ---------------- POST /api/notes/summarize 端点布线 ----------------

@asynccontextmanager
async def asgi_client():
    import httpx
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def summary_env():
    """复位模块级单例状态（hub/status/runtime 摘要相关键），测试互不污染。"""
    saved = (main.status["summary"], main.runtime["summarizing"],
             main.runtime["summary_task"], main.hub.transcript_path,
             main.runtime["translator"])
    main.status["summary"] = {"state": "idle", "done": 0, "total": 0,
                              "failed": 0, "path": None, "error": None}
    main.runtime["summarizing"] = False
    main.runtime["summary_task"] = None
    main.hub.transcript_path = None
    main.runtime["translator"] = None
    yield
    (main.status["summary"], main.runtime["summarizing"],
     main.runtime["summary_task"], main.hub.transcript_path,
     main.runtime["translator"]) = saved


async def _wait(pred, timeout: float = 3.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_summarize_endpoint_no_transcript_404(summary_env):
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    assert r.status_code == 404
    assert main.status["summary"]["state"] == "idle"  # 未起任务


async def test_summarize_endpoint_no_translator_409(summary_env):
    hub = main.Hub()
    main.hub.transcript_path = hub.start_transcript()  # conftest 重定向 tmp_path
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    assert r.status_code == 409
    assert "翻译未启用" in r.json()["error"]
    assert main.runtime["summarizing"] is False


async def test_summarize_endpoint_busy_409(summary_env):
    main.runtime["summarizing"] = True  # 直接置守卫（比计时竞争更确定）
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    assert r.status_code == 409
    assert "进行中" in r.json()["error"]


async def test_summarize_endpoint_end_to_end(summary_env):
    """受理即返回 → 后台任务跑完 → status 状态机 done + 文件落盘 + WS 广播。"""
    hub = main.Hub()
    tpath = hub.start_transcript()
    hub.append("12:00:01", "alpha beta gamma", "", None)
    hub.append("12:00:02", "delta epsilon zeta", "", 2)
    main.hub.transcript_path = tpath
    main.runtime["translator"] = FakeTranslator()

    ws = FakeWS()
    main.hub.clients.add(ws)
    try:
        async with asgi_client() as c:
            r = await c.post("/api/notes/summarize")
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["started"] is True
        assert j["transcript"] == tpath.name
        assert j["output"] == tpath.stem + ".summary.md"
        assert main.runtime["summarizing"] is True  # 受理即置守卫
        task = main.runtime["summary_task"]  # 先捕获引用（finally 会置 None）

        assert await _wait(
            lambda: main.status["summary"]["state"] == "done")
        await task  # 任务收尾（异常已被 _run 兜住）
        s = main.status["summary"]
        assert s["state"] == "done" and s["done"] == s["total"] == 1
        assert s["failed"] == 0 and s["path"].endswith(".summary.md")
        assert main.runtime["summarizing"] is False
        assert main.runtime["summary_task"] is None
        out = tpath.with_name(tpath.stem + ".summary.md")
        assert out.is_file() and "## 概览" in out.read_text(encoding="utf-8")
        # WS 收到 type=summary 的进度/完成广播
        msgs = [json.loads(m) for m in ws.sent]
        assert any(m.get("type") == "summary" and m.get("state") == "done"
                   for m in msgs)
    finally:
        main.hub.clients.discard(ws)


async def test_summarize_endpoint_empty_en_marks_error(summary_env):
    """整体失败路径（空 EN 转写）：状态机置 error 并记录原因，不 500；
    summarizing 守卫释放（可重试）。"""
    hub = main.Hub()
    tpath = hub.start_transcript()
    hub.append("12:00:01", "", "只有中文", None)
    main.hub.transcript_path = tpath
    main.runtime["translator"] = FakeTranslator()

    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    assert r.status_code == 200
    task = main.runtime["summary_task"]  # 先捕获引用（finally 会置 None）
    assert await _wait(lambda: main.status["summary"]["state"] == "error")
    await task
    s = main.status["summary"]
    assert "转写内容为空" in s["error"]
    assert main.runtime["summarizing"] is False
    assert not tpath.with_name(tpath.stem + ".summary.md").exists()
