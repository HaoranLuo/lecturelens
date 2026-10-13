# -*- coding: utf-8 -*-
"""tests/test_capture_guard.py — 采集守护基元离线单测（升级计划条目 1/2）。

覆盖：MicSegmenter.healthy 四态（未启动 / 活线程+新鲜心跳 / 断流心跳过期 /
error 优先）+ run() 异常不再静默（死因写入 self.error，供 watchdog 上报）。
真实设备占用/拔插的端到端行为留给 smoke_test [1c] 与手动清单。"""
import queue
import threading
import time

import capture
from capture import MicSegmenter


def _alive_segmenter() -> tuple[MicSegmenter, threading.Event]:
    """不打开真实设备：塞一个假 thread 对象模拟 start() 后的存活状态。"""
    seg = MicSegmenter(queue.Queue(), vad="energy")
    ev = threading.Event()
    seg.thread = threading.Thread(target=ev.wait, daemon=True)
    seg.thread.start()
    return seg, ev


def test_healthy_unstarted_thread_is_false():
    seg = MicSegmenter(queue.Queue(), vad="energy")
    assert seg.healthy is False  # 线程未启动


def test_healthy_alive_with_fresh_heartbeat_is_true():
    seg, ev = _alive_segmenter()
    try:
        seg.last_feed_ts = time.time()
        assert seg.healthy is True
    finally:
        ev.set()


def test_healthy_stale_heartbeat_is_false():
    seg, ev = _alive_segmenter()
    try:
        seg.last_feed_ts = time.time() - (MicSegmenter.FEED_STALE_S + 1)
        assert seg.healthy is False  # 断流：超过 FEED_STALE_S 无新块
    finally:
        ev.set()


def test_healthy_error_takes_priority():
    seg, ev = _alive_segmenter()
    try:
        seg.error = "PortAudioError: simulated"
        seg.last_feed_ts = time.time()
        assert seg.healthy is False  # 有致命错误时即使心跳新鲜也不健康
    finally:
        ev.set()


def test_run_records_exception_as_error(monkeypatch):
    """InputStream 构造抛异常：run() 正常返回（线程不死也不挂），
    死因写入 self.error 且 healthy 变 False——main.py 的 watchdog 据此上报重建。"""

    class BoomStream:
        def __init__(self, **kwargs):
            raise OSError("device busy")

    monkeypatch.setattr(capture.sd, "InputStream", BoomStream)
    seg = MicSegmenter(queue.Queue(), vad="energy")
    seg.run()  # 不应向调用方抛出
    assert seg.error is not None and "OSError" in seg.error
    assert seg.healthy is False


def test_run_stop_flag_is_silent(monkeypatch):
    """主动 stop 后的收尾异常不算故障：不写 error（capture.py run() 的
    `if self._stop.is_set(): return` 分支）。"""

    class BoomStream:
        def __init__(self, **kwargs):
            raise OSError("cancelled")

    monkeypatch.setattr(capture.sd, "InputStream", BoomStream)
    seg = MicSegmenter(queue.Queue(), vad="energy")
    seg.stop()  # 置位 _stop（thread 为 None，join 跳过）
    seg.run()
    assert seg.error is None
