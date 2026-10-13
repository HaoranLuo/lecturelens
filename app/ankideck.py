# -*- coding: utf-8 -*-
"""ankideck.py — Anki 复习卡生成（升级计划条目 18）。

管线（role prompt + few-shot → 生成 → 二次过滤低质卡（必需环节）→ genanki 打包）：
读条目 17 摘要 md → LLM 生成 12–18 张双语卡（正=问题，反=答案；一卡一知识点，
supermemo 最小信息原则）→ 二次 LLM 打分过滤（score < 阈值丢弃，定义类保留；
条目 18 调查明确「过滤不可省」）→ 截到上限 → genanki（纯 Python，MIT）打包
.apkg → main.py 经 GET /api/notes/deck 提供下载。

卡面纯文本从简（计划口径）：Model 只有两个字段 Question/Answer，无 CSS/HTML。
deck/model ID 恒定、note guid 由「transcript 名 + 正面」派生——同一节课重新
生成不产生 Anki 重复牌组/重复卡。不新增服务端取 URL 逻辑（唯一外呼仍是
translate.py 固定的 DeepSeek API，经 chat_with_retry 复用条目 6 重试）。"""
import asyncio
import hashlib
import json
import logging
import re

import genanki

log = logging.getLogger("ankideck")

QUALITY_THRESHOLD = 6   # 二次过滤：score(1-10) 低于阈值丢弃（定义类除外）
MAX_CARDS = 20          # 计划口径：10–20 张，生成后按分数截到上限
GEN_TARGET = "12-18"    # 生成阶段请求张数（过滤后落入 10–20 区间）

MODEL_ID = 1704201800   # 恒定 model ID（换值 = Anki 里出现重复模板，勿改）
DECK_MODEL = genanki.Model(
    MODEL_ID, "LectureLens QA",
    fields=[{"name": "Question"}, {"name": "Answer"}],
    templates=[{"name": "Card 1", "qfmt": "{{Question}}",
                "afmt": "{{Answer}}"}],
)

GENERATE_SYSTEM = (
    "You write study flashcards for university lectures. From the given lecture "
    "summary, write " + GEN_TARGET + " bilingual flashcards:\n"
    "- front: one focused question (keep technical terms in English)\n"
    "- back: concise answer in Simplified Chinese with key English terms in "
    "parentheses\n"
    "- one card = one knowledge point (minimum information principle)\n"
    "- prefer definitions, mechanisms, comparisons, formulas\n"
    "Example card: {\"front\": \"What does the learning rate control in gradient "
    "descent?\", \"back\": \"每一步参数更新的步长大小（step size）\"}\n"
    "Output STRICT JSON only: {\"cards\": [{\"front\": \"...\", \"back\": \"...\"}, ...]}"
)

FILTER_SYSTEM = (
    "You are a strict flashcard quality reviewer. For each numbered card, score "
    "1-10 for study value (clear, factual, self-contained, one knowledge point). "
    "Mark definitional cards (term/concept/formula definitions) with "
    "\"is_definition\": true — they are kept even with low scores. "
    "Output STRICT JSON only: "
    "{\"reviews\": [{\"index\": 0, \"score\": 7, \"is_definition\": false}, ...]}"
)


def extract_json_object(text: str) -> dict:
    """从 LLM 回复稳健提取 JSON 对象：剥 ``` 围栏、容忍前后杂文（取首 { 到末 }）。
    两段都失败抛 ValueError。纯函数，供单测。"""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s.strip())
    for candidate in (s, s[s.find("{"):s.rfind("}") + 1]
                      if 0 <= s.find("{") < s.rfind("}") else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError(f"LLM 回复不是合法 JSON 对象: {text[:120]!r}")


def normalize_cards(data: dict, max_cards: int = MAX_CARDS) -> list[tuple[str, str]]:
    """校验生成结果为 [(front, back), ...]：非空字符串字段、上限截断。
    结构不符的条目跳过（LLM 偶发多余键/缺键不炸）。"""
    raw = data.get("cards") if isinstance(data, dict) else None
    cards: list[tuple[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        front, back = item.get("front"), item.get("back")
        if isinstance(front, str) and front.strip() and isinstance(back, str) \
                and back.strip():
            cards.append((front.strip(), back.strip()))
    return cards[:max_cards]


def apply_filter(cards: list[tuple[str, str]], reviews,
                 threshold: int = QUALITY_THRESHOLD,
                 max_cards: int = MAX_CARDS) -> list[tuple[str, str]]:
    """纯函数：保留 score >= threshold 或 is_definition 的卡；按分数降序
    （原序为稳定次键）截到 max_cards。
    缺评级的卡按中性分（= threshold）保留——LLM 截断少评几条时宁可多留不误删；
    reviews 里 index 非法/score 非数值的条目忽略。"""
    by_idx: dict[int, tuple[float, bool]] = {}
    for r in (reviews if isinstance(reviews, list) else []):
        if not isinstance(r, dict):
            continue
        i = r.get("index")
        if isinstance(i, bool) or not isinstance(i, int) or i < 0:
            continue
        sc = r.get("score")
        if isinstance(sc, bool) or not isinstance(sc, (int, float)):
            continue
        by_idx[i] = (float(sc), bool(r.get("is_definition")))
    kept: list[tuple[float, int, str, str]] = []
    for i, (front, back) in enumerate(cards):
        sc, is_def = by_idx.get(i, (float(threshold), False))
        if sc >= threshold or is_def:
            kept.append((sc, i, front, back))
    kept.sort(key=lambda t: (-t[0], t[1]))
    return [(f, b) for _, _, f, b in kept[:max_cards]]


def deck_id_for(name: str) -> int:
    """transcript 文件名 → 稳定 32-bit deck ID：同一节课重打包 ID 不变
    （Anki 不重复建牌组），不同课不同名 → 不同 ID。（SHA-256 仅作稳定派生，
    非安全用途——Mimosa 扫描建议弃用 SHA-1，照改零成本。）"""
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16)


def write_apkg(cards: list[tuple[str, str]], out_path, title: str) -> None:
    """genanki 打包 .apkg（同步、纯本地，main.py 里 to_thread 调用）。"""
    deck = genanki.Deck(deck_id_for(title), f"LectureLens · {title}")
    for front, back in cards:
        deck.add_note(genanki.Note(
            DECK_MODEL, fields=[front, back],
            guid=genanki.guid_for(title, front)))  # 重生成不重复建卡
    genanki.Package(deck).write_to_file(str(out_path))
    log.info(f"[deck] .apkg 已写盘: {out_path}（{len(cards)} 卡）")


class DeckGenerator:
    """条目18 生成器。translator 依赖面与 summarize 一致：
    chat_with_retry(payload)->str（条目 6 重试封装）+ model 属性。"""

    def __init__(self, translator, threshold: int = QUALITY_THRESHOLD,
                 max_cards: int = MAX_CARDS):
        self.tr = translator
        self.threshold = threshold
        self.max_cards = max_cards

    async def _chat(self, system: str, user: str, max_tokens: int) -> str:
        payload = {
            "model": self.tr.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": False,
        }
        return await self.tr.chat_with_retry(payload)

    async def generate(self, summary_text: str, on_stage=None) -> dict:
        """两段 LLM：生成 → 二次过滤。返回 {"cards", "generated", "dropped"}。
        on_stage(stage, n_cards=None)：generating / filtering，供进度上报。"""
        if on_stage:
            on_stage("generating")
        raw = await self._chat(
            GENERATE_SYSTEM, f"Lecture summary:\n\n{summary_text}", 4000)
        cards = normalize_cards(extract_json_object(raw), self.max_cards)
        if not cards:
            raise ValueError("LLM 未生成任何有效卡（cards 为空或结构不符）")
        if on_stage:
            on_stage("filtering", len(cards))
        numbered = "\n".join(f"{i}. front: {f}\n   back: {b}"
                             for i, (f, b) in enumerate(cards))
        raw2 = await self._chat(
            FILTER_SYSTEM, f"Review these {len(cards)} cards:\n\n{numbered}", 2000)
        reviews = extract_json_object(raw2).get("reviews", [])
        kept = apply_filter(cards, reviews, self.threshold, self.max_cards)
        if not kept:
            raise ValueError("二次过滤后 0 张卡保留（全部低于阈值且非定义类）")
        return {"cards": kept, "generated": len(cards),
                "dropped": len(cards) - len(kept)}


async def build_deck(translator, summary_path, apkg_path, title: str,
                     on_stage=None) -> dict:
    """端到端：读摘要 → 生成+过滤 → genanki 打包。main.py 后台任务调用。"""
    summary_text = summary_path.read_text(encoding="utf-8")
    result = await DeckGenerator(translator).generate(summary_text,
                                                      on_stage=on_stage)
    await asyncio.to_thread(write_apkg, result["cards"], apkg_path, title)
    result["path"] = apkg_path
    return result
