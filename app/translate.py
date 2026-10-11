# -*- coding: utf-8 -*-
"""translate.py — DeepSeek EN→ZH 翻译（OpenAI 兼容接口，httpx 直调零额外依赖）。
术语表：glossary/*.md 内每行一条 `EN term = 中文译名`，注入 system prompt 提升专业词准确率。"""
import asyncio
from pathlib import Path

import httpx

API_URL = "https://api.deepseek.com/chat/completions"
SYSTEM_TMPL = (
    "You are a professional EN→ZH translator for academic lectures. "
    "Translate the user's English lecture excerpt into natural, concise Simplified Chinese. "
    "Keep technical terms accurate. Output ONLY the Chinese translation, no explanations.\n"
    "{glossary}"
)


def load_glossary(paths: list[str] | None) -> str:
    if not paths:
        return ""
    entries = []
    for p in paths:
        f = Path(p)
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and ("=" in line or "\t" in line):
                    entries.append(line.replace("=", " = "))
    if not entries:
        return ""
    return "Glossary (use these translations):\n" + "\n".join(entries)


class DeepSeekTranslator:
    def __init__(self, api_key: str, model: str = "deepseek-chat", glossary_files: list[str] | None = None):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 未设置（环境变量或 .env）")
        self.model = model
        self.glossary = load_glossary(glossary_files)
        self.system = SYSTEM_TMPL.format(glossary=self.glossary)
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
        print(f"[translate] ready (model={model}, glossary terms={len(entries)})" if self.glossary
              else f"[translate] ready (model={model}, no glossary)")

    async def translate(self, text_en: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": text_en},
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
            "stream": False,
        }
        r = await self.client.post(API_URL, json=payload)
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


def load_api_key(env_file: Path | None = None) -> str:
    import os
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    for candidate in ([env_file] if env_file else []) + [
        Path(r"D:\projects\lecture-translate\.env"),
    ]:
        if candidate and candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""
