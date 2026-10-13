# -*- coding: utf-8 -*-
"""summarize.py — 课后 AI 笔记摘要（map-reduce，升级计划条目 17）。

管线（只借 LangChain 摘要教程的 map-reduce 形状，不引入 langchain）：
读 transcript → 按 EN 行边界分块（约 4000 字符，绝不切断句子）→ asyncio.gather
+ Semaphore(4) 并行逐块摘要（复用条目 6 重试封装 translate.DeepSeekTranslator
.chat_with_retry）→ reduce 为结构化 md（概览/要点/术语对照/存疑处）→ 写
lecture_*.summary.md。进度经 on_progress 回调上报，main.py 接到 /api/status
的 summary 键与 WS type=summary 消息。

「失败块标记，绝不整体失败」的三层兜底：
① 单块 map 重试耗尽 → 该块记入 failed，其余照常，reduce 时作为缺口标注；
② reduce LLM 调用失败 → 降级为本地拼接块摘要（未经合并去重，标明降级）；
③ 全部块失败 → 跳过 reduce，直接写失败清单文件。
任何路径下 summary 文件都写得出来；文件末尾固定附失败块清单（不依赖 LLM 遵嘱）。

§6（可编辑摘要）：摘要在预览页可**人工编辑**（main 的 /api/notes/summary 读写），
故每次生成前若已有同名摘要，先把它另存为 `*.summary.bak.md` 再覆盖——
「人工修订版不被静默冲掉」是这一层唯一的职责（备份本身写坏=没备份，故走原子替换）。
"""
import asyncio
import contextlib
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("summarize")

MAX_CHUNK_CHARS = 4000  # 计划口径：按约 4000 字符分块
CONCURRENCY = 4         # 计划口径：semaphore=4 并行逐块摘要
#: §6 覆盖前备份的后缀：`.summary.md` 的文件备份成 `.summary.bak.md`。与
#: notes.SUMMARY_BACKUP_SUFFIX 同口径（那边管「派生物跟着笔记走」的名字规则），
#: 本模块管「怎么给一个 .md 造备份名」，故保持自包含、不反向依赖 notes。
BACKUP_SUFFIX = ".bak.md"


def backup_path(out_path: Path) -> Path:
    """摘要的备份落点：`lecture_X.summary.md` → `lecture_X.summary.bak.md`。"""
    n = Path(out_path).name
    if n.lower().endswith(".md"):
        n = n[:-3]
    return Path(out_path).with_name(n + BACKUP_SUFFIX)


def atomic_write_text(path: Path, text: str) -> None:
    """整篇原子落盘（同目录 .tmp → os.replace）。行尾按平台默认（与旧实现
    `Path.write_text` 逐字节行为一致，Windows 上仍是 CRLF）。

    临时名带 pid + 随机段：同一份摘要也被 main.note_atomic_write（人工保存）写，
    两边若都叫「<名>.tmp」就共用一个临时文件（评审④低）——名字不唯一 = 迟早踩。"""
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        with contextlib.suppress(Exception):
            tmp.unlink()
        raise


def backup_existing(out_path: Path) -> Path | None:
    """§6：覆盖前把已有摘要另存为 `*.summary.bak.md` → 备份路径；没有旧文件 → None。

    备份内容 = **即将被覆盖的那一份**（每次生成都刷新），所以用户手上永远有一份
    「上一版」可回退；每次覆盖都留旧版会无限堆积，而人工修订的价值在最近一版。
    写失败直接抛（调用方 summarize 走的是「摘要整体失败」路径）——宁可不覆盖，
    也不要出现「备份没写成、原文件已被冲掉」的静默丢数据。"""
    src = Path(out_path)
    if not src.is_file():
        return None
    dst = backup_path(src)
    data = src.read_text(encoding="utf-8")
    atomic_write_text(dst, data)
    log.info(f"[summarize] 覆盖前已备份: {src.name} → {dst.name}"
             f"（{len(data)} 字符）")
    return dst

MAP_SYSTEM = (
    "You are a lecture-notes assistant. Below is one excerpt of a live-transcribed "
    "lecture transcript. Summarize it in Simplified Chinese (keep technical terms in "
    "English where appropriate), in exactly these markdown sections:\n"
    "### 要点\n"
    "- 3-6 bullets of the key points in this excerpt\n"
    "### 术语\n"
    "- `EN term = 中文译名` for important terms/definitions (skip section if none)\n"
    "### 存疑\n"
    "- anything unclear, likely transcription errors, or missing context "
    "(write 「无」 if none)\n"
    "Output ONLY these sections, no preamble."
)

REDUCE_SYSTEM = (
    "You are a lecture-notes editor. You receive per-chunk notes from a full lecture "
    "transcript (failed chunks are marked). Merge them into one structured study note "
    "in Simplified Chinese (keep technical terms in English), with exactly these "
    "markdown sections in this order:\n"
    "## 概览\n(one short paragraph: what this lecture is about)\n"
    "## 要点\n(merged, deduplicated bullets ordered by lecture flow)\n"
    "## 术语对照\n(merged `EN term = 中文译名` list, deduplicated)\n"
    "## 存疑处\n(merged uncertainties; note content gaps left by failed chunks)\n"
    "Output ONLY the final markdown."
)


def extract_en_lines(text: str) -> list[str]:
    """从笔记 md 抽取 EN 原文行（剥掉时间戳前缀），保持原顺序。
    格式契约以 main.Hub.append 为准：EN 行 = `- `HH:MM:SS` 文本`（顶格）；
    ZH 行以空白缩进开头（`  - ...`，可能带 `` `#seg` `` 锚）；其余（标题/
    `---`/引导行/空行）跳过。ZH 行绝不能在 strip 后再判定——strip 会把
    `  - `#3` 中文` 变成 EN 行形状导致译文混入。纯函数，供单测锁定
    「写侧格式 vs 读侧解析」不漂移。"""
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw or raw[0].isspace():
            continue  # ZH 续行（缩进）/空行
        if not raw.startswith("- `"):
            continue  # 标题 / --- / 引导行
        close = raw.find("`", 3)  # 起始反引号在 index 2，闭合从 3 起找
        if close < 0:
            continue  # 残缺行防御
        content = raw[close + 1:].strip()
        if content:
            lines.append(content)
    return lines


def chunk_lines(lines: list[str], max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """按 EN 行边界累积分块：整行进出，绝不切断句子；单行超预算独占一块。
    纯函数，供单测。"""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if cur else 0)  # 行间换行符开销
        if cur and size + add > max_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
            add = len(line)
        cur.append(line)
        size += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def chunk_transcript(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """transcript 全文 → EN 行分块列表（条目17「按 EN 行边界分块」入口）。"""
    return chunk_lines(extract_en_lines(text), max_chars)


class TranscriptSummarizer:
    """map-reduce 摘要器。translator 只被要求提供 `chat_with_retry(payload)->str`
    （条目 6 重试封装）与 `model` 属性——测试注入 fake，生产传 DeepSeekTranslator。"""

    def __init__(self, translator, max_chunk_chars: int = MAX_CHUNK_CHARS,
                 concurrency: int = CONCURRENCY):
        self.tr = translator
        self.max_chunk_chars = max_chunk_chars
        self.concurrency = concurrency

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

    async def _map_one(self, i: int, n: int, chunk: str) -> str:
        return await self._chat(
            MAP_SYSTEM, f"（第 {i + 1}/{n} 块，课堂转写原文节选）\n\n{chunk}", 1000)

    async def _reduce(self, notes: list[tuple[int, str]],
                      failed: dict[int, str], n: int) -> str:
        parts = [f"【第 {i + 1}/{n} 块】\n{note}" for i, note in notes]
        parts += [f"【第 {i + 1}/{n} 块——摘要失败：{err}】"
                  for i, err in sorted(failed.items())]
        user = (f"以下是整节课的逐块摘要素材（共 {n} 块，其中 {len(failed)} 块失败"
                f"已标注）。请合并为最终结构化笔记；失败块对应的内容缺口请在"
                f"「存疑处」注明：\n\n" + "\n\n".join(parts))
        return await self._chat(REDUCE_SYSTEM, user, 4000)

    async def summarize(self, transcript_path: Path, out_path: Path,
                        on_progress=None) -> dict:
        """全流程：分块 → 并行 map → reduce/降级 → 落盘。返回
        {"total", "failed", "reduce_ok", "path", "text", "backup"}。
        on_progress(done, total)：分块后先报 (0, n)，之后每完成一块报一次；
        在事件循环内同步调用（main.py 里只改 status + 派广播任务，零阻塞）。

        §6：落盘前若已有摘要 → 先备份为 `*.summary.bak.md`（backup 字段回传备份
        文件名或 None），保证人工修订版不被静默冲掉。"""
        text = transcript_path.read_text(encoding="utf-8")
        chunks = chunk_transcript(text, self.max_chunk_chars)
        if not chunks:
            raise ValueError("转写内容为空（没有 EN 原文行可摘要）")
        n = len(chunks)
        if on_progress:
            on_progress(0, n)
        sem = asyncio.Semaphore(self.concurrency)  # 每次运行独立建（不跨 loop 复用）
        notes: list[tuple[int, str]] = []
        failed: dict[int, str] = {}

        async def _one(i: int, chunk: str) -> None:
            async with sem:
                try:
                    note = await self._map_one(i, n, chunk)
                except Exception as e:  # noqa: BLE001 —— 单块失败必须兜住（兜底①）
                    failed[i] = f"{type(e).__name__}: {e}"
                    log.warning(f"[summarize] 第 {i + 1}/{n} 块摘要失败: {e}")
                else:
                    notes.append((i, note))
            if on_progress:
                on_progress(len(notes) + len(failed), n)

        await asyncio.gather(*(_one(i, c) for i, c in enumerate(chunks)))
        notes.sort()

        reduce_ok = True
        if notes:
            try:
                final = await self._reduce(notes, failed, n)
            except Exception as e:  # noqa: BLE001 —— reduce 失败降级（兜底②）
                reduce_ok = False
                log.warning(f"[summarize] reduce 失败，降级为本地拼接: {e}")
                final = self._fallback_md(notes, failed, n)
        else:
            reduce_ok = False  # 全部块失败（兜底③）
            final = self._fallback_md([], failed, n)

        if failed:
            # 失败标记固定落进文件正文，不依赖 LLM 在「存疑处」遵嘱
            final += "\n---\n\n> ⚠ " + self._failed_note(failed, n) + "\n"
        backup = backup_existing(out_path)   # §6：覆盖前备份（无旧文件 → None）
        atomic_write_text(out_path, final)
        log.info(f"[summarize] 摘要落盘: {out_path.name}"
                 f"（{n} 块，失败 {len(failed)}，reduce_ok={reduce_ok}"
                 f"{'，已备份 ' + backup.name if backup else ''}）")
        return {"total": n, "failed": sorted(failed), "reduce_ok": reduce_ok,
                "path": Path(out_path), "text": final,
                "backup": str(backup) if backup else None}

    @staticmethod
    def _failed_note(failed: dict[int, str], n: int) -> str:
        detail = "、".join(f"第 {i + 1} 块（{err}）" for i, err in sorted(failed.items()))
        return f"摘要生成时 {len(failed)}/{n} 块失败：{detail}，对应内容缺失。"

    def _fallback_md(self, notes: list[tuple[int, str]],
                     failed: dict[int, str], n: int) -> str:
        """reduce LLM 失败 / 全部块失败时的降级产物：结构化骨架 + 块摘要原样拼接。
        保证「绝不整体失败」——任何情况下 summary 文件都写得出来。"""
        if not notes:
            return "\n".join([
                "# 课堂笔记摘要\n",
                f"> ⚠ {self._failed_note(failed, n)}",
                "> 所有分块摘要均失败，未能生成有效内容。\n",
            ])
        parts = ["# 课堂笔记摘要\n",
                 "> ⚠ reduce 汇总失败，以下为逐块摘要降级拼接（未经合并去重）。\n"]
        for i, note in notes:
            parts.append(f"\n## 第 {i + 1} 块\n\n{note}\n")
        return "\n".join(parts)
