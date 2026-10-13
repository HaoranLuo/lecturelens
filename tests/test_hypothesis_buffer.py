# -*- coding: utf-8 -*-
"""tests/test_hypothesis_buffer.py — LocalAgreement-2 假设缓冲离线单测（条目 11）。

验收口径（§2.2-11）：公共前缀提交 / n-gram 去重 / 时间戳过滤。
被测对象：app/streaming.py 的 HypothesisBuffer（移植自 ufal/whisper_streaming，
MIT，语义与上游逐行一致）。全部离线秒级，不触碰模型。"""
from streaming import HypothesisBuffer


def test_flush_commits_longest_common_prefix():
    """flush() 提交最近两次假设的最长公共前缀；首次假设无对照不提交。"""
    h = HypothesisBuffer()
    h.insert([(0.0, 0.5, "hello"), (0.5, 1.0, "world")], offset=0.0)
    assert h.flush() == [], "第一次 flush 无前次假设可比，不得提交"
    assert [t for _, _, t in h.complete()] == ["hello", "world"]

    # 第二次假设延长了尾部：公共前缀 hello world 获得确认
    h.insert([(0.0, 0.5, "hello"), (0.5, 1.0, "world"), (1.0, 1.5, "again")], offset=0.0)
    committed = h.flush()
    assert [(a, b, t) for a, b, t in committed] == [(0.0, 0.5, "hello"), (0.5, 1.0, "world")]
    assert h.last_commited_time == 1.0
    assert [t for _, _, t in h.complete()] == ["again"]  # 未获二次确认的尾部留在 buffer

    # 假设延伸（公共前缀为 again）：只提交公共前缀，extra 留待下次确认
    h.insert([(1.0, 1.5, "again"), (1.5, 2.0, "extra")], offset=0.0)
    assert [t for _, _, t in h.flush()] == ["again"]
    assert [t for _, _, t in h.complete()] == ["extra"]


def test_insert_offset_shifts_and_filters_old_words():
    """insert 的 offset 把块内时间戳平移到全局时间轴；已提交时刻之前的词被过滤。"""
    h = HypothesisBuffer()
    h.insert([(0.0, 0.5, "a"), (0.5, 1.0, "b")], offset=10.0)
    assert h.flush() == []  # 首次假设无对照，不提交（但已进 buffer）
    h.insert([(0.0, 0.5, "a"), (0.5, 1.0, "b")], offset=10.0)
    committed = h.flush()
    assert committed == [(10.0, 10.5, "a"), (10.5, 11.0, "b")]  # 时间戳已平移
    assert h.last_commited_time == 11.0

    # 同一块音频再次解码（offset 相同）：所有词都 <= last_commited_time-0.1 → 全滤掉
    h.insert([(0.0, 0.5, "a"), (0.5, 1.0, "b")], offset=10.0)
    assert h.new == [], "落后于已提交时刻的词必须被过滤"
    assert h.flush() == []


def test_ngram_dedup_drops_repeated_committed_words():
    """解码器把已提交词又吐出来时，1..5-gram 去重从 new 头部剥掉重复词，
    只留真正的新内容；新内容仍需两次假设一致才提交（LocalAgreement 语义）。"""
    h = HypothesisBuffer()
    h.insert([(0.0, 0.5, "the"), (0.5, 1.0, "cat")], offset=0.0)
    assert h.flush() == []
    h.insert([(0.0, 0.5, "the"), (0.5, 1.0, "cat")], offset=0.0)
    assert [t for _, _, t in h.flush()] == ["the", "cat"]  # 前缀确认
    assert h.commited_in_buffer == [(0.0, 0.5, "the"), (0.5, 1.0, "cat")]

    # 新假设重复了已提交的 the cat（幻觉回放）+ 新词 runs：
    h.insert([(1.0, 1.5, "the"), (1.5, 2.0, "cat"), (2.0, 2.5, "runs")], offset=0.0)
    assert h.flush() == [], "去重剥掉重复词后 runs 尚未获二次确认"
    assert [t for _, _, t in h.complete()] == ["runs"]

    # 第二次假设再次含 runs：runs 提交，且 cat 后缀不会再被重复提交
    h.insert([(2.0, 2.5, "runs"), (2.5, 3.0, "fast")], offset=0.0)
    committed = h.flush()
    assert [(a, b, t) for a, b, t in committed] == [(2.0, 2.5, "runs")]


def test_pop_commited_trims_by_end_time():
    """pop_commited(time) 按 end <= time 裁剪已提交词（缓冲裁剪时回收内存）。"""
    h = HypothesisBuffer()
    h.insert([(0.0, 0.5, "a"), (0.5, 1.0, "b"), (1.0, 1.5, "c")], offset=0.0)
    assert h.flush() == []
    h.insert([(0.0, 0.5, "a"), (0.5, 1.0, "b"), (1.0, 1.5, "c")], offset=0.0)
    h.flush()
    h.pop_commited(1.0)
    assert [t for _, _, t in h.commited_in_buffer] == ["c"]
    h.pop_commited(99.0)
    assert h.commited_in_buffer == []
