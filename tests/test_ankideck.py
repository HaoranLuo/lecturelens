# -*- coding: utf-8 -*-
"""tests/test_ankideck.py — Anki 复习卡生成离线单测（升级计划条目 18）。

覆盖：
- JSON 稳健提取（围栏/前后杂文）、卡结构校验、二次过滤纯函数（阈值丢弃、
  定义类保留、缺评级中性保留、分数截上限）；
- genanki 真实打包（纯 Python 本地，离线可跑）：文件落盘 + deck/note ID 稳定；
- DeckGenerator 编排（fake translator 注入 chat_with_retry——条目 6 重试封装
  依赖面）：两段 LLM 调用、空生成/全被过滤/非法 JSON 的失败语义；
- POST /api/notes/deck 触发 + GET /api/notes/deck 下载端点布线（ASGI 直调，
  不进 lifespan）：404/409 前置校验、状态机、WS type=deck 广播、文件字节一致。
真实 LLM 调用由真机自调覆盖（条目 18 验收门槛 = 首次实调后自用一周，
mock 只证管线跑通——计划原文）；本文件全部离线秒级。"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

import ankideck
import main
from ankideck import (DeckGenerator, apply_filter, build_deck, deck_id_for,
                      extract_json_object, normalize_cards, write_apkg)

GEN_JSON = json.dumps({"cards": [
    {"front": "What is attention?", "back": "注意力机制（attention）"},
    {"front": "Weak card?", "back": "低质卡"},
    {"front": "What is RNN?", "back": "循环神经网络（RNN）定义"},
    {"front": "Another weak?", "back": "又一张低质"},
]}, ensure_ascii=False)
REVIEWS_JSON = json.dumps({"reviews": [
    {"index": 0, "score": 9, "is_definition": False},
    {"index": 1, "score": 2, "is_definition": False},   # 低于阈值 → 丢
    {"index": 2, "score": 3, "is_definition": True},    # 定义类低分 → 保留
    {"index": 3, "score": 5, "is_definition": False},   # 低于阈值 → 丢
]}, ensure_ascii=False)


# ---------------- JSON 稳健提取 / 卡校验（纯函数） ----------------

def test_extract_json_plain_fenced_and_noisy():
    obj = {"cards": []}
    plain = json.dumps(obj)
    assert extract_json_object(plain) == obj
    assert extract_json_object(f"```json\n{plain}\n```") == obj
    assert extract_json_object(f"好的，以下是卡片：\n{plain}\n希望有帮助") == obj


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        extract_json_object("完全不是 JSON 的回复")
    with pytest.raises(ValueError):
        extract_json_object("[1, 2, 3]")  # 合法 JSON 但不是对象


def test_normalize_cards_validates_and_strips():
    data = {"cards": [
        {"front": "  F1  ", "back": "B1"},
        {"front": "", "back": "B2"},          # 空 front → 跳过
        {"front": "F3"},                      # 缺 back → 跳过
        "not a dict",                          # 结构不符 → 跳过
        {"front": "F5", "back": "  "},        # 空 back → 跳过
        {"front": "F6", "back": "B6"},
    ]}
    assert normalize_cards(data) == [("F1", "B1"), ("F6", "B6")]
    assert normalize_cards({}) == []
    # 上限截断
    many = {"cards": [{"front": f"F{i}", "back": "B"} for i in range(30)]}
    assert len(normalize_cards(many)) == ankideck.MAX_CARDS


def test_apply_filter_threshold_and_definition():
    cards = [("q0", "a0"), ("q1", "a1"), ("q2", "a2"), ("q3", "a3")]
    reviews = [{"index": 0, "score": 9, "is_definition": False},
               {"index": 1, "score": 2, "is_definition": False},
               {"index": 2, "score": 3, "is_definition": True},
               {"index": 3, "score": 5, "is_definition": False}]
    kept = apply_filter(cards, reviews, threshold=6)
    assert kept == [("q0", "a0"), ("q2", "a2")]  # 高分 + 定义类；低分非定义丢弃


def test_apply_filter_missing_review_neutral_keep_and_bad_entries():
    cards = [("q0", "a0"), ("q1", "a1"), ("q2", "a2")]
    # q0 无评级 → 中性保留；q1 index 非法（bool）→ 条目忽略 → 中性保留；
    # q2 score 非数值 → 条目忽略 → 中性保留
    reviews = [{"index": True, "score": 1}, {"index": 2, "score": "high"}]
    assert apply_filter(cards, reviews, threshold=6) == cards


def test_apply_filter_caps_by_score_stable_order():
    cards = [(f"q{i}", "a") for i in range(25)]
    reviews = [{"index": i, "score": 6 + (i % 5), "is_definition": False}
               for i in range(25)]  # 25 张全过线 → 截断到 20 生效
    kept = apply_filter(cards, reviews, threshold=6, max_cards=20)
    assert len(kept) == 20
    scores = [r["score"] for r in reviews]
    kept_idx = [cards.index(k) for k in kept]
    kept_scores = [scores[i] for i in kept_idx]
    assert kept_scores == sorted(kept_scores, reverse=True)  # 分数降序
    assert min(kept_scores) >= 6                             # 全部过阈值


def test_deck_id_stable_and_distinct():
    assert deck_id_for("lecture_20261012_1530") == deck_id_for("lecture_20261012_1530")
    assert deck_id_for("lecture_a") != deck_id_for("lecture_b")


# ---------------- genanki 真实打包（本地纯 Python） ----------------

def test_write_apkg_real_file(tmp_path):
    out = tmp_path / "lecture_x.apkg"
    cards = [("What is attention?", "注意力机制（attention）"),
             ("What is RNN?", "循环神经网络（RNN）定义")]
    write_apkg(cards, out, "lecture_x")
    assert out.is_file() and out.stat().st_size > 1000  # sqlite zip 结构非空壳


def test_write_apkg_stable_deck_id(tmp_path):
    """同一 transcript 名两次打包 deck ID 一致（Anki 不重复建牌组的前提）。
    .apkg 是 zip 容器，需解出 collection.anki2 再读 sqlite。"""
    import sqlite3
    import zipfile
    ids_per_run = []
    for i in range(2):
        p = tmp_path / f"{i}.apkg"
        write_apkg([("f", "b")], p, "lecture_same")
        with zipfile.ZipFile(p) as z:
            z.extract("collection.anki2", tmp_path / f"extract{i}")
        db = tmp_path / f"extract{i}" / "collection.anki2"
        with sqlite3.connect(str(db)) as conn:
            (decks_json,) = conn.execute("SELECT decks FROM col").fetchone()
        decks = json.loads(decks_json)
        records = decks.values() if isinstance(decks, dict) else decks
        ids_per_run.append({d["name"]: d["id"] for d in records})
    key = "LectureLens · lecture_same"
    assert ids_per_run[0][key] == ids_per_run[1][key]


# ---------------- DeckGenerator 编排（fake translator） ----------------

class FakeTranslator:
    """依赖面同 summarize 测试：chat_with_retry + model；按 system 分流。"""

    def __init__(self, gen_reply: str = GEN_JSON, filter_reply: str = REVIEWS_JSON):
        self.model = "deepseek-chat"
        self.gen_reply = gen_reply
        self.filter_reply = filter_reply
        self.calls: list[dict] = []

    async def chat_with_retry(self, payload: dict) -> str:
        self.calls.append(payload)
        system = payload["messages"][0]["content"]
        if system == ankideck.GENERATE_SYSTEM:
            return self.gen_reply
        if system == ankideck.FILTER_SYSTEM:
            return self.filter_reply
        raise AssertionError(f"未知 system prompt: {system[:40]}")


async def test_generator_happy_path_two_llm_calls():
    fake = FakeTranslator()
    stages: list[tuple] = []
    result = await DeckGenerator(fake).generate(
        "## 要点\n- 快排", on_stage=lambda s, n=None: stages.append((s, n)))
    assert len(fake.calls) == 2  # 生成 + 过滤（计划：二次过滤必需环节）
    assert result["generated"] == 4 and result["dropped"] == 2
    assert result["cards"] == [("What is attention?", "注意力机制（attention）"),
                               ("What is RNN?", "循环神经网络（RNN）定义")]
    assert stages == [("generating", None), ("filtering", 4)]


async def test_generator_empty_cards_raises():
    fake = FakeTranslator(gen_reply='{"cards": []}')
    with pytest.raises(ValueError, match="未生成任何有效卡"):
        await DeckGenerator(fake).generate("要点")


async def test_generator_all_filtered_raises():
    """过滤必须真能把卡清零（全部低分且非定义类）→ 明确报错，不出空卡组。"""
    reviews = json.dumps({"reviews": [
        {"index": i, "score": 1, "is_definition": False} for i in range(4)]})
    fake = FakeTranslator(filter_reply=reviews)
    with pytest.raises(ValueError, match="0 张卡保留"):
        await DeckGenerator(fake).generate("要点")


async def test_generator_malformed_gen_json_raises():
    fake = FakeTranslator(gen_reply="我觉得没法出卡")
    with pytest.raises(ValueError, match="合法 JSON"):
        await DeckGenerator(fake).generate("要点")


async def test_generator_malformed_filter_json_raises():
    fake = FakeTranslator(filter_reply="打不了分")
    with pytest.raises(ValueError, match="合法 JSON"):
        await DeckGenerator(fake).generate("要点")


async def test_build_deck_writes_apkg(tmp_path):
    summary = tmp_path / "lecture_x.summary.md"
    summary.write_text("## 要点\n- 快排 O(n log n)\n", encoding="utf-8")
    apkg = tmp_path / "lecture_x.apkg"
    result = await build_deck(FakeTranslator(), summary, apkg, "lecture_x")
    assert apkg.is_file() and result["path"] == apkg
    assert result["cards"] and result["generated"] == 4  # 结果含卡清单与计数


# ---------------- POST / GET /api/notes/deck 端点布线 ----------------

@asynccontextmanager
async def asgi_client():
    import httpx
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def deck_env():
    """复位模块级单例状态（hub/status/runtime 卡组相关键），测试互不污染。"""
    saved = (main.status["deck"], main.runtime["deck_running"],
             main.runtime["deck_task"], main.hub.transcript_path,
             main.runtime["translator"])
    main.status["deck"] = {"state": "idle", "stage": None, "generated": 0,
                           "kept": 0, "dropped": 0, "path": None, "error": None}
    main.runtime["deck_running"] = False
    main.runtime["deck_task"] = None
    main.hub.transcript_path = None
    main.runtime["translator"] = None
    yield
    (main.status["deck"], main.runtime["deck_running"],
     main.runtime["deck_task"], main.hub.transcript_path,
     main.runtime["translator"]) = saved


async def _wait(pred, timeout: float = 3.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _make_transcript_with_summary(main_hub) -> None:
    """conftest 已把 TRANSCRIPTS 重定向 tmp_path：造 transcript + 同名 summary。"""
    tpath = main_hub.start_transcript()
    main_hub.append("12:00:01", "alpha beta", "", None)
    tpath.with_name(tpath.stem + ".summary.md").write_text(
        "# 课堂笔记摘要\n\n## 要点\n- 快排 O(n log n)\n", encoding="utf-8")
    main.hub.transcript_path = tpath


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


async def test_deck_get_404_without_apkg(deck_env):
    async with asgi_client() as c:
        r = await c.get("/api/notes/deck")
    assert r.status_code == 404  # 无 transcript
    _make_transcript_with_summary(main.hub)
    async with asgi_client() as c:
        r2 = await c.get("/api/notes/deck")
    assert r2.status_code == 404  # 有 transcript 但还没生成


async def test_deck_post_guards(deck_env):
    async with asgi_client() as c:
        r = await c.post("/api/notes/deck")
    assert r.status_code == 404  # 无 transcript

    _make_transcript_with_summary(main.hub)
    async with asgi_client() as c:
        r2 = await c.post("/api/notes/deck")
    assert r2.status_code == 409 and "翻译未启用" in r2.json()["error"]

    main.runtime["translator"] = FakeTranslator()
    main.runtime["deck_running"] = True  # 直接置守卫（比计时竞争更确定）
    async with asgi_client() as c:
        r3 = await c.post("/api/notes/deck")
    assert r3.status_code == 409 and "进行中" in r3.json()["error"]


async def test_deck_post_requires_summary_first(deck_env):
    hub = main.Hub()
    main.hub.transcript_path = hub.start_transcript()  # 无 summary 文件
    main.runtime["translator"] = FakeTranslator()
    async with asgi_client() as c:
        r = await c.post("/api/notes/deck")
    assert r.status_code == 409 and "summarize" in r.json()["error"]


async def test_deck_post_end_to_end_and_get_download(deck_env):
    """受理即返回 → 后台生成+过滤+打包 → done + .apkg → GET 下载字节一致。"""
    _make_transcript_with_summary(main.hub)
    main.runtime["translator"] = FakeTranslator()
    ws = FakeWS()
    main.hub.clients.add(ws)
    try:
        tpath = main.hub.transcript_path
        async with asgi_client() as c:
            r = await c.post("/api/notes/deck")
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["started"] is True
        assert j["output"] == tpath.stem + ".apkg"
        assert main.runtime["deck_running"] is True
        task = main.runtime["deck_task"]  # 先捕获引用（finally 会置 None）

        assert await _wait(lambda: main.status["deck"]["state"] == "done")
        await task
        s = main.status["deck"]
        assert s["state"] == "done" and s["stage"] is None
        assert s["generated"] == 4 and s["kept"] == 2 and s["dropped"] == 2
        assert s["path"].endswith(".apkg")
        assert main.runtime["deck_running"] is False
        apkg = tpath.with_name(tpath.stem + ".apkg")
        assert apkg.is_file()

        async with asgi_client() as c:
            resp = await c.get("/api/notes/deck")
        assert resp.status_code == 200
        assert resp.content == apkg.read_bytes()  # 下载 = 落盘字节

        msgs = [json.loads(m) for m in ws.sent]
        assert any(m.get("type") == "deck" and m.get("state") == "done"
                   for m in msgs)
        assert any(m.get("type") == "deck" and m.get("stage") == "filtering"
                   for m in msgs)  # 阶段进度有广播
    finally:
        main.hub.clients.discard(ws)


async def test_deck_post_llm_failure_marks_error(deck_env):
    """整体失败路径（生成非法 JSON）：状态机置 error 并记录原因，不 500；
    守卫释放可重试；不残留 .apkg。"""
    _make_transcript_with_summary(main.hub)
    main.runtime["translator"] = FakeTranslator(gen_reply="不是 JSON")
    async with asgi_client() as c:
        r = await c.post("/api/notes/deck")
    assert r.status_code == 200
    task = main.runtime["deck_task"]
    assert await _wait(lambda: main.status["deck"]["state"] == "error")
    await task
    s = main.status["deck"]
    assert "合法 JSON" in s["error"]
    assert main.runtime["deck_running"] is False
    tpath = main.hub.transcript_path
    assert not tpath.with_name(tpath.stem + ".apkg").exists()
