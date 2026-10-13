# -*- coding: utf-8 -*-
"""tests/test_loopback.py — WASAPI loopback 采集层离线单测（升级计划条目 14）。

不碰真硬件：
① 状态机复用——把 16k 合成流上采样成 48k 立体声（模拟扬声器原始流），
  经 LoopbackSegmenter._feed_resampled 走完整 loopback 链路（downmix →
  resample_poly → 攒余数切 BLOCK），切段结果应与麦克风路径一致
  （对照 tests/test_capture_state.py / smoke_test [1] 的同阈值断言）；
② 构造函数强制 silero VAD（调用方传 vad 也被覆盖）；
③ 设备打不开 → self.error 优雅落地、线程退出不悬挂（watchdog 接管的前提），
  且 _stop 未被误置（watchdog 可继续重建）。"""
import queue
import sys
import time
import types

import numpy as np
import pytest
from scipy.signal import resample_poly

from capture import SR
from loopback import LoopbackSegmenter
from smoke_test import synth_stream


def _stereo_48k(stream_16k: np.ndarray, rate_in: int = 48000) -> np.ndarray:
    """16k mono 合成流 → 48k 立体声（模拟「扬声器在放」的设备侧原始数据）。
    左右声道增益/微延迟不同，同时验证 downmix 不破坏切段。"""
    g = np.gcd(rate_in, SR)
    mono = resample_poly(stream_16k, rate_in // g, SR // g).astype("float32")
    right = np.concatenate([np.zeros(3, dtype="float32"), mono[:-3]]) * 0.9
    return np.stack([mono, right], axis=1)


def _drain_durations(q: queue.Queue) -> list[float]:
    durs = []
    while not q.empty():
        _, audio = q.get()
        durs.append(round(len(audio) / SR, 2))
    return durs


def test_loopback_segmentation_matches_mic_path():
    """完整链路：48k 立体声设备流 → downmix → resample → 16k BLOCK →
    共享状态机。切段 1–3 段、每段 1.2–3.6s（与 test_capture_state 同阈值）。"""
    stream = synth_stream(with_hum=False)   # 静音1s→语音2s→静音1s→语音2s→静音1.5s
    stereo48 = _stereo_48k(stream)
    q: queue.Queue = queue.Queue()
    seg = LoopbackSegmenter(q)
    seg.source_rate = 48000                 # 模拟 run() 打开设备后的状态
    chunk = 600                             # ≈12.5ms@48k，非整除BLOCK → 检验攒余数逻辑
    for i in range(0, len(stereo48) - chunk, chunk):
        seg._feed_resampled(stereo48[i:i + chunk])
    durs = _drain_durations(q)
    assert 1 <= len(durs) <= 3, f"切出段数异常: {durs}"
    assert all(1.2 <= d <= 3.6 for d in durs), f"段长异常: {durs}"


def test_loopback_forces_silero_vad():
    """条目14：loopback 强制 silero——显式传 vad='energy' 也被覆盖。"""
    seg = LoopbackSegmenter(queue.Queue(), vad="energy")
    assert seg.vad.name == "silero"


def test_loopback_snapshot_reports_source_rate():
    seg = LoopbackSegmenter(queue.Queue())
    assert "source_rate" not in seg.snapshot()
    seg.source_rate = 48000
    assert seg.snapshot()["source_rate"] == 48000


def test_loopback_device_failure_is_graceful(monkeypatch):
    """设备打不开（get_default_wasapi_loopback 抛 OSError）→ 死因落 self.error、
    线程退出不悬挂、不崩进程；_stop 未被误置（watchdog 可按
    MIC_REBUILD_COOLDOWN 重建）。"""
    class FakePA:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_default_wasapi_loopback(self):
            raise OSError("no loopback device found")

    fake_mod = types.ModuleType("pyaudiowpatch")
    fake_mod.PyAudio = FakePA
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake_mod)

    q: queue.Queue = queue.Queue()
    seg = LoopbackSegmenter(q)
    seg.start()
    try:
        t0 = time.time()
        while seg.error is None and time.time() - t0 < 5:
            time.sleep(0.05)
        assert seg.error, "5s 内未见错误落地（线程疑似挂死）"
        assert "找不到可用的 WASAPI loopback 设备" in seg.error
        assert not seg.healthy          # watchdog 会判不健康并触发重建
        seg.thread.join(timeout=2)      # error 落地与线程退出存在微秒级竞态，join 收口
        assert seg.thread is not None and not seg.thread.is_alive()  # 线程已退出
        assert not seg._stop.is_set()   # 非主动 stop，重建可用
    finally:
        seg.stop()
