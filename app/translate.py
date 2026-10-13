# -*- coding: utf-8 -*-
"""translate.py — DeepSeek EN→ZH 翻译（OpenAI 兼容接口，httpx 直调零额外依赖）。
术语表：glossary/*.md 每行一条 `EN term = 中文译名`（或 Tab 分隔），注入 system prompt；
英文术语同时作为 faster-whisper 的 initial_prompt，提高专业词识别率。
条目16：translate() 支持注入最近已定稿 EN 上下文（仅参考防指代漂移）；
术语表 mtime 变化自动重载 entries+system（调用方须同步刷 whisper 侧 prompt）。
条目17：重试循环抽为 chat_with_retry 通用入口，课后摘要（summarize.py）复用。"""
import asyncio
import logging
from pathlib import Path

import httpx

log = logging.getLogger("translate")

API_URL = "https://api.deepseek.com/chat/completions"
SYSTEM_TMPL = (
    "You are a professional EN→ZH translator for academic lectures. "
    "Translate the user's English lecture excerpt into natural, concise Simplified Chinese. "
    "Keep technical terms accurate. Output ONLY the Chinese translation, no explanations.\n"
    "{glossary}"
)

# —— 条目16：翻译上下文窗口（user 前缀，仅参考防指代/术语跨段漂移） ——
CTX_MARK = "Context for reference only; do not translate"
CTX_MAX_CHARS = 1200  # 上下文前缀预算（对齐 glossary_prompt 的截断做法）


def build_user_message(text_en: str, context: list[str] | None = None,
                       max_chars: int = CTX_MAX_CHARS) -> str:
    """user message = （可选）已定稿 EN 上下文前缀 + 待译文本。纯函数，供单测。
    无上下文时原样返回（既有请求形状零变化——test_translate_retry 回归锁定）。
    预算截断：整行优先、丢最旧保最新；单行仍超预算保最新尾部。"""
    lines = [c.strip() for c in (context or []) if c and c.strip()]
    if not lines:
        return text_en
    head = f"{CTX_MARK}:\n"
    tail = "\n\nTranslate the following lecture excerpt:\n"
    budget = max_chars - len(head) - len(tail)
    if budget <= 0:
        return text_en
    while len(lines) > 1 and sum(len(x) for x in lines) + len(lines) - 1 > budget:
        lines.pop(0)                 # 超预算：丢最旧的整行（至少保留最新一行）
    block = "\n".join(lines)
    if len(block) > budget:          # 单行仍超预算：保最新尾部
        block = block[-budget:]
    return f"{head}{block}{tail}{text_en}"


def load_glossary_entries(paths: list[str] | None) -> list[tuple[str, str]]:
    """解析术语表为 [(en, zh), ...]；`#` 开头为注释行。"""
    entries: list[tuple[str, str]] = []
    for p in paths or []:
        f = Path(p)
        if not f.is_file():
            log.warning(f"[translate] ⚠ 术语表不存在: {p}")
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                en, _, zh = line.partition("=")
            elif "\t" in line:
                en, _, zh = line.partition("\t")
            else:
                continue
            en, zh = en.strip(), zh.strip()
            if en and zh:
                entries.append((en, zh))
    return entries


def glossary_prompt(entries: list[tuple[str, str]], max_chars: int = 800) -> str:
    """英文术语串，用作 whisper initial_prompt（whisper 限 ~224 token，此处截断保险）。"""
    return "; ".join(en for en, _ in entries)[:max_chars]


class DeepSeekTranslator:
    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 glossary_files: list[str] | None = None,
                 retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0)):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 未设置（环境变量或 .env）")
        self.model = model
        self.retry_delays = retry_delays  # 重试间隔（指数退避），测试可缩短
        self.glossary_files = list(glossary_files or [])  # 条目16：mtime 重载按此清单
        self._load_glossary()
        self._mtimes = self._glossary_mtimes()
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
        log.info(f"[translate] ready (model={model}, glossary terms={len(self.entries)})")

    # —— 条目16：术语表 mtime 自动重载（entries + system 一体重建，不存在只刷一半） ——
    def _glossary_mtimes(self) -> dict[str, int]:
        """清单内文件的 mtime_ns 快照；缺失/不可读的文件不进表（删除=变化）。"""
        mt: dict[str, int] = {}
        for p in self.glossary_files:
            try:
                f = Path(p)
                if f.is_file():
                    mt[p] = f.stat().st_mtime_ns
            except OSError:
                pass
        return mt

    def _load_glossary(self) -> None:
        self.entries = load_glossary_entries(self.glossary_files)
        glossary = ""
        if self.entries:
            glossary = "Glossary (use these translations):\n" + "\n".join(
                f"{en} = {zh}" for en, zh in self.entries
            )
        self.system = SYSTEM_TMPL.format(glossary=glossary)

    def set_glossary_files(self, files: list[str]) -> bool:
        """p2-1：整体替换术语表清单并立即重载（课程切换用），返回是否发生变化。

        与 reload_if_changed 的分工：后者按 **mtime** 检测同清单下内容被编辑；
        本方法处理**清单本身**被替换（换课程 = 换文件集合，mtime 比对对此无感，
        因为新文件根本不在旧清单里）。调用方（main.apply_course_glossary）负责
        同步刷新 whisper 侧 runtime['prompt']——只刷翻译侧会出现两侧术语不一致。"""
        new = list(files or [])
        if new == self.glossary_files:
            return False
        self.glossary_files = new
        self._load_glossary()
        self._mtimes = self._glossary_mtimes()
        log.info(f"[translate] 术语表清单已替换（{len(new)} 个文件，"
                 f"terms={len(self.entries)}）")
        return True

    def reload_now(self) -> None:
        """p2-6：**无条件**重载术语表（调用方已知内容变了，不依赖 mtime 判定）。

        reload_if_changed 按 st_mtime_ns 判「变了吗」，而本机文件系统会把快速重写的
        时间戳折叠成同一个——实测 300 次连写里 266 次 st_mtime_ns 完全相同。
        术语页「保存即生效」若只靠 mtime 就会随机失效（改完词条没反应），故写盘成功后
        直接调本方法。调用方（main）负责同步刷新 whisper 侧 runtime['prompt']。"""
        self._load_glossary()
        self._mtimes = self._glossary_mtimes()
        log.info(f"[translate] 术语表已强制重载（terms={len(self.entries)}）")

    def reload_if_changed(self) -> bool:
        """术语表 mtime 变化 → 重载 entries+system，返回是否发生重载。
        纯 stat 比对（每段调用一次的开销可忽略）；文件删除也算变更。
        调用方（main.sync_glossary_reload）负责同步刷新 whisper 侧 runtime['prompt']
        ——只刷翻译侧会出现两侧术语不一致（技术评审 missing 项）。"""
        mt = self._glossary_mtimes()
        if mt == self._mtimes:
            return False
        self._mtimes = mt
        self._load_glossary()
        log.info(f"[translate] 术语表变更已重载（terms={len(self.entries)}）")
        return True

    async def translate(self, text_en: str, context: list[str] | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": build_user_message(text_en, context)},
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
            "stream": False,
        }
        return await self.chat_with_retry(payload)

    async def chat_with_retry(self, payload: dict) -> str:
        """条目6重试封装（通用入口）：429/5xx 与超时/网络类异常按 retry_delays
        指数退避（默认 1s/2s/4s），最多 3 次重试（共 4 次请求）；401/400 等
        永久性错误立即抛，重试无意义。最终失败向上抛，由调用方做失败补偿。
        translate() 与条目17课后摘要（summarize.py）共用——payload 由调用方
        组装（model/messages/temperature/max_tokens 自带），本方法只管重试与
        解包 assistant 文本（strip）。"""
        req = httpx.Request("POST", API_URL)
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0,) + self.retry_delays):
            if attempt:
                await asyncio.sleep(delay)
            try:
                r = await self.client.post(API_URL, json=payload)
                r.raise_for_status()
                data = r.json()
                return (data["choices"][0]["message"]["content"] or "").strip()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code != 429 and code < 500:
                    raise  # 401/403/400/422…：鉴权或请求问题，重试不会好
                last_exc = e
                log.warning(f"[chat] HTTP {code}，尝试 {attempt + 1}/{len(self.retry_delays) + 1} 失败")
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                log.warning(f"[chat] {type(e).__name__}，尝试 {attempt + 1}/{len(self.retry_delays) + 1} 失败")
        raise last_exc


def load_api_key(env_file: Path | None = None) -> str:
    import os
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    # 条目10⑤：不再内置硬编码绝对路径兜底——.env 路径一律由调用方传入
    # （main.py 传 ROOT/.env；smoke_test 同）。
    for candidate in ([env_file] if env_file else []):
        if candidate and candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""
