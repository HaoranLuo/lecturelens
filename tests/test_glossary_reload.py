# -*- coding: utf-8 -*-
"""tests/test_glossary_reload.py — 术语表 mtime 自动重载单测（升级计划条目 16）。

验收口径（§2.2-16）：glossary mtime 变化 → 翻译侧 entries/system 与 whisper 侧
runtime['prompt'] 必须同步刷新（只刷翻译侧会出现两侧术语不一致——技术评审
missing 项）。全部离线：tmp_path 术语表；DeepSeekTranslator 构造不发任何网络
请求（AsyncClient 仅建连接池）。

**为什么 _write_glossary 要显式推 mtime**：reload_if_changed 按 st_mtime_ns 判
「术语表变了吗」，而本机文件系统对**快速重写**会把两次写入折叠成同一个时间戳——
实测 300 次连写里有 266 次 st_mtime_ns 完全相同。于是「重写 = 变更」这个前提在
CI/本机上只有 ~11% 成立，用例会随机变红（单独跑 6/6 过、全量跑偶发失败，本仓库
p2-3 与 p2-5 两次实现期间各撞到一次）。显式把 mtime 往前推 1ms 抹掉的是**环境噪声**
而不是断言：被测语义（mtime 变了就该重载）一字未改，真实使用中用户改术语表也是
分钟级间隔，mtime 必然不同。"""
import os

import main
from translate import DeepSeekTranslator, glossary_prompt


def _write_glossary(p, term: str) -> None:
    prev = p.stat().st_mtime_ns if p.exists() else 0
    p.write_text(f"{term} = 译名\n", encoding="utf-8")
    st = p.stat()
    # 必须是**相对上一次**递增，而不是恒定 +1ms：恒定偏移在本机上无效——两次自然
    # 写入常常落在同一时间戳刻度，各加 1ms 后仍然相等。实测「本次 <= 上次就推」的
    # 写法 200/200 严格递增（os.utime 按 ns 原样落盘，仅 ~100ns 取整）。
    if st.st_mtime_ns <= prev:
        os.utime(p, ns=(st.st_atime_ns, prev + 1_000_000))


def _make_translator(g) -> DeepSeekTranslator:
    return DeepSeekTranslator("test-key", glossary_files=[str(g)])


def test_reload_no_change_is_false(tmp_path):
    g = tmp_path / "CS.md"
    _write_glossary(g, "attention")
    tr = _make_translator(g)
    assert tr.reload_if_changed() is False     # 纯 stat 比对：没变不重载
    assert len(tr.entries) == 1


def test_reload_on_change_updates_entries_and_system(tmp_path):
    """§2.2-16 核心：mtime 变化 → entries 与 system 一体重建（不存在只刷一半）。"""
    g = tmp_path / "CS.md"
    _write_glossary(g, "attention")
    tr = _make_translator(g)
    _write_glossary(g, "transformer")          # 重写 → mtime 变化
    assert tr.reload_if_changed() is True
    assert tr.entries == [("transformer", "译名")]
    assert "transformer = 译名" in tr.system
    assert "attention" not in tr.system        # 旧术语已出
    assert tr.reload_if_changed() is False     # 重载后快照已更新，不会连触


def test_reload_on_file_deletion_clears_entries(tmp_path):
    g = tmp_path / "CS.md"
    _write_glossary(g, "attention")
    tr = _make_translator(g)
    g.unlink()                                 # 文件删除 = 变化（mtimes 键消失）
    assert tr.reload_if_changed() is True
    assert tr.entries == []


def test_main_sync_updates_whisper_prompt(tmp_path, monkeypatch):
    """main.sync_glossary_reload：翻译侧重载后 whisper 侧 runtime['prompt']
    必须同步刷新（streaming 的 base_prompt 与整句 initial_prompt 都读它）。
    无变化时 False 且 prompt 不被触碰（初值由 asr_loop 启动时写入）。"""
    g = tmp_path / "CS.md"
    _write_glossary(g, "attention")
    tr = _make_translator(g)
    monkeypatch.setitem(main.runtime, "translator", tr)
    monkeypatch.setitem(main.runtime, "prompt", None)

    assert main.sync_glossary_reload() is False        # 无变化：不重载不触碰 prompt
    assert main.runtime["prompt"] is None
    _write_glossary(g, "transformer")                  # mtime 变化
    assert main.sync_glossary_reload() is True
    assert main.runtime["prompt"] == "transformer"
    assert main.runtime["prompt"] == glossary_prompt(tr.entries)  # 两侧一致


def test_main_sync_without_translator_is_false(monkeypatch):
    monkeypatch.setitem(main.runtime, "translator", None)
    assert main.sync_glossary_reload() is False


def test_main_sync_without_glossary_files_is_false(monkeypatch):
    tr = DeepSeekTranslator("test-key")        # 无术语表清单
    monkeypatch.setitem(main.runtime, "translator", tr)
    assert tr.glossary_files == []
    assert main.sync_glossary_reload() is False


def test_translator_model_default_and_configurable():
    """条目16：translate.model 配置化——默认 deepseek-chat，可注入（payload
    每次请求读 self.model，热切换改属性即生效）。"""
    assert DeepSeekTranslator("test-key").model == "deepseek-chat"
    tr = DeepSeekTranslator("test-key", model="deepseek-reasoner")
    assert tr.model == "deepseek-reasoner"
