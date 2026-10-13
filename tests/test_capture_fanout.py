# -*- coding: utf-8 -*-
"""tests/test_capture_fanout.py — MicSegmenter 可选 stream_q 扇出单测（条目 11）。

保护清单第 1 条的唯一触碰点：采集循环取出块、handle_block 原样不动之后，
可选把同一块副本投递给流式管线。默认 stream_q=None 必须行为零变化。
纯逻辑离线：energy VAD + 直接喂块（不经声卡；扇出入口与 run() 循环共用
_consume_block，分句状态机本身由 test_capture_state 覆盖）。"""
import queue

import numpy as np

from capture import BLOCK, SR, MicSegmenter


def _block(level: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (level * rng.standard_normal(BLOCK)).astype(np.float32)


def _lecture_stream() -> list[np.ndarray]:
    """校准(30 静音块) → 语音(40 块 ≈2.6s) → 静音(25 块 ≈1.6s)：必切出 ≥1 段。"""
    blocks = [_block(0.001, i) for i in range(30)]
    blocks += [_block(0.2, 100 + i) for i in range(40)]
    blocks += [_block(0.001, 200 + i) for i in range(25)]
    return blocks


def test_fanout_default_none_is_zero_change():
    """默认 stream_q=None：只产定稿段，无扇出（v0.2 行为零变化）。"""
    seg = MicSegmenter(queue.Queue(), vad="energy")
    assert seg.stream_q is None
    for b in _lecture_stream():
        seg._consume_block(b)
    assert seg.q_out.qsize() >= 1, "定稿路径必须照常出段"


def test_fanout_delivers_every_block_when_enabled():
    """开启扇出：每个块（含静音块）按原顺序出现在 stream_q，且定稿路径照常。"""
    stream_q: queue.Queue = queue.Queue()
    seg = MicSegmenter(queue.Queue(), vad="energy", stream_q=stream_q)
    blocks = _lecture_stream()
    for b in blocks:
        seg._consume_block(b)
    assert stream_q.qsize() == len(blocks), \
        f"扇出必须逐块送达: {stream_q.qsize()}/{len(blocks)}"
    first = stream_q.get_nowait()
    assert first.shape == (BLOCK,) and first.dtype == np.float32
    assert np.array_equal(first, blocks[0]), "扇出保持块序与内容"
    assert seg.q_out.qsize() >= 1, "扇出不得影响定稿路径"


def test_fanout_drops_on_full_queue_not_block_segmenter():
    """stream_q 满（maxsize=1 且不消费）时丢块保定稿：分句器不被扇出反压卡死。"""
    stream_q: queue.Queue = queue.Queue(maxsize=1)
    seg = MicSegmenter(queue.Queue(), vad="energy", stream_q=stream_q)
    for b in _lecture_stream():
        seg._consume_block(b)
    assert stream_q.qsize() == 1  # 扇出最多滞留 1 块，其余让位
    assert seg.q_out.qsize() >= 1, "扇出阻塞时定稿路径必须照常出段"
