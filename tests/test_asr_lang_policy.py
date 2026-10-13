# -*- coding: utf-8 -*-
"""tests/test_asr_lang_policy.py — WhisperASR.transcribe 的 lang=auto 采纳策略
单测（条目 13① + 评审修复）。

评审修复点："auto" 不是合法语言码，任何分支都不得原样透传给 faster-whisper
（其 Tokenizer.__init__ 对不在 _LANGUAGE_CODES 的值抛 ValueError）——原实现
auto + 短段(≤3s) + 无检测历史（课程开头常态）时把 "auto" 透传，导致该段报错：
非流式=字幕与笔记静默丢失至首个 >3s 段自愈；流式=头几个 chunk 全失败且 final
若为开头短段则触发主循环挂死链；warmup(0.5s 静音) 同因静默无效。

用 object.__new__ 绕过 __init__（不加载真模型），注入 FakeModel 捕获 language 参数。"""
from types import SimpleNamespace

import numpy as np

from asr import WhisperASR

SR = 16000


class FakeModel:
    def __init__(self):
        self.calls: list[dict] = []

    def transcribe(self, audio, language=None, **kw):
        self.calls.append({"language": language})
        info = SimpleNamespace(language=language or "en", language_probability=0.9)
        return iter([]), info


def make_asr(language: str, detected: str | None = None) -> WhisperASR:
    w = object.__new__(WhisperASR)  # 绕过 __init__：不加载真模型
    w.language = language
    w.detected_lang = detected
    w.model = FakeModel()
    return w


def test_auto_short_segment_without_history_passes_none():
    """评审修复核心：auto + 短段(≤3s) + 无检测历史 → 传 None（检测转写、结果不
    采纳），绝不透传 'auto'。"""
    w = make_asr("auto", None)
    w.transcribe(np.zeros(SR, dtype=np.float32))          # 1s 短段
    assert w.model.calls[0]["language"] is None
    assert w.detected_lang is None                        # 短段不采纳检测


def test_auto_long_segment_detects_and_adopts():
    """长段(>3s)：传 None 让 whisper 检测，检测结果被采纳进 detected_lang。"""
    w = make_asr("auto", None)
    w.transcribe(np.zeros(SR * 4, dtype=np.float32))      # 4s 长段
    assert w.model.calls[0]["language"] is None
    assert w.detected_lang == "en"                        # FakeModel 无参时检测为 en


def test_auto_short_segment_reuses_adopted_language():
    """短段沿用上段采纳的语言（防短段误检翻转）。"""
    w = make_asr("auto", "zh")
    w.transcribe(np.zeros(SR, dtype=np.float32))
    assert w.model.calls[0]["language"] == "zh"


def test_fixed_language_passthrough_unchanged():
    """非 auto 语言码直传（v0.2 行为不变）。"""
    w = make_asr("zh", None)
    w.transcribe(np.zeros(SR, dtype=np.float32))
    assert w.model.calls[0]["language"] == "zh"


def test_warmup_safe_under_auto_without_history():
    """warmup(0.5s 静音) 在 auto 无历史时同样安全（评审员2 指出的静默无效场景）。"""
    w = make_asr("auto", None)
    w.warmup()
    assert w.model.calls and w.model.calls[0]["language"] is None
