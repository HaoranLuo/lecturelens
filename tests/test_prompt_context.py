# -*- coding: utf-8 -*-
"""tests/test_prompt_context.py — prompt 构造离线单测（升级计划条目 2）。

覆盖当前已实现的构造层：术语表解析（load_glossary_entries）、whisper
initial_prompt 截断（glossary_prompt）、翻译 system prompt 注入。
条目 16（翻译上下文窗口 + 术语 mtime 双侧重载）落地后在本文件追加：
build_user_message 前缀构造与 1200 截断、translate payload 形状（mtime
双侧同步刷新在 tests/test_glossary_reload.py）。
v02-3：术语表缺失警告由 print 改为 logging，缺文件用例从 capsys 切到 caplog。"""
import json
import logging

import httpx

from translate import (CTX_MARK, SYSTEM_TMPL, DeepSeekTranslator,
                       build_user_message, glossary_prompt,
                       load_glossary_entries)


def test_load_glossary_entries(tmp_path):
    g = tmp_path / "CS.md"
    g.write_text(
        "# 注释行\n"
        "attention = 注意力\n"
        "transformer\t变换器\n"          # Tab 分隔
        "没有分隔符的行\n"                # 跳过
        "\n"
        "  spaced = 空格版  \n",         # 前后空白被 strip
        encoding="utf-8",
    )
    assert load_glossary_entries([str(g)]) == [
        ("attention", "注意力"),
        ("transformer", "变换器"),
        ("spaced", "空格版"),
    ]


def test_load_glossary_missing_file_is_empty(caplog):
    with caplog.at_level(logging.WARNING, logger="translate"):
        entries = load_glossary_entries(["Z:/definitely/not/here.md"])
    assert entries == []
    assert "术语表不存在" in caplog.text  # 警告经 logging（条目3）


def test_load_glossary_none_is_empty():
    assert load_glossary_entries(None) == []


def test_glossary_prompt_truncates():
    """whisper initial_prompt 限 ~224 token，超长必须截断到 max_chars。"""
    entries = [(f"term{i}", f"译{i}") for i in range(100)]
    p = glossary_prompt(entries, max_chars=50)
    assert len(p) <= 50
    assert p.startswith("term0")


def test_system_template_requires_translation_only():
    assert "Output ONLY the Chinese translation" in SYSTEM_TMPL
    assert "{glossary}" in SYSTEM_TMPL  # 术语段落占位存在


def test_translator_system_prompt_embeds_glossary(tmp_path):
    g = tmp_path / "CS.md"
    g.write_text("gradient = 梯度\n", encoding="utf-8")
    tr = DeepSeekTranslator("test-key", glossary_files=[str(g)])
    assert "gradient = 梯度" in tr.system
    assert len(tr.entries) == 1


# ---------------- 条目16：翻译上下文窗口（user 前缀 + 1200 截断） ----------------

def test_user_message_without_context_unchanged():
    """无上下文 → user content 原样（既有请求形状零变化，test_translate_retry
    的 payload 断言依赖此行为）。"""
    assert build_user_message("gradient descent") == "gradient descent"
    assert build_user_message("hello", []) == "hello"
    assert build_user_message("hello", ["", "   "]) == "hello"  # 全空白上下文 = 无


def test_user_message_with_context_marks_reference_only():
    """最近 3 条已定稿 EN 拼进前缀并标注仅参考；待译文本始终在最后。"""
    msg = build_user_message("today we continue",
                             ["previous topic one", "previous topic two"])
    assert CTX_MARK in msg
    assert "previous topic one" in msg and "previous topic two" in msg
    assert msg.rstrip().endswith("today we continue")
    # 顺序：上下文（旧→新）在待译文本之前
    assert msg.index("previous topic one") < msg.index("today we continue")


def test_user_message_strips_context_lines():
    msg = build_user_message("T", ["  spaced line  ", "\t tabbed \n"])
    assert "spaced line" in msg and "tabbed" in msg
    assert "  spaced line  " not in msg  # 前后空白被 strip


def test_user_message_caps_at_1200_chars_dropping_oldest():
    """§2.2-16「1200 截断」：预算约束的是上下文前缀（待译文本在后面、长度不设限）。
    超预算先丢最旧整行，最新一条必须保留，前缀部分不超 1200。"""
    lines = [f"line{i} " + "x" * 600 for i in range(3)]  # 3×606 ≈ 1818 > 1200
    msg = build_user_message("TARGET", lines)
    assert len(msg) <= 1200 + len("TARGET")   # 前缀 ≤1200，加上待译文本
    assert "line2" in msg          # 最新保留
    assert "line0" not in msg      # 最旧被丢
    assert msg.rstrip().endswith("TARGET")


def test_user_message_single_long_line_keeps_tail():
    """单行自身超预算：保最新尾部（最新内容价值最高）。"""
    msg = build_user_message("T", ["y" * 2000])
    assert len(msg) <= 1200 + len("T")
    assert msg.endswith("T")
    assert "y" * 100 in msg


async def test_translate_payload_embeds_context_prefix():
    """§2.2-16「smoke_test --translate 带上下文请求」的离线等价：payload.user
    = 上下文前缀 + 待译文本；system 形状不变；无 context 时保持原样。"""
    tr = DeepSeekTranslator("test-key", retry_delays=(0.0, 0.0, 0.0))
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    tr.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"})

    await tr.translate("new sentence", context=["older line", "older line 2", "newest line"])
    msgs = captured["json"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert CTX_MARK in user
    assert "newest line" in user and "older line" in user
    assert user.endswith("new sentence")

    captured.clear()
    await tr.translate("plain sentence")   # 无 context：user 原样（零变化）
    assert captured["json"]["messages"][1]["content"] == "plain sentence"
