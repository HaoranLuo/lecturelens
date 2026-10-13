# -*- coding: utf-8 -*-
"""loopback.py — WASAPI loopback 系统声音内录（升级计划条目 14）。

采集链路：PyAudioWPatch 打开默认扬声器的 loopback 设备（48k/设备原生率，
多声道 float32）→ downmix (L+R)/2 → scipy.signal.resample_poly 抗混叠重采样
到 16k → 切成 BLOCK(1024) 块 → 复用 capture.AudioSegmenter 的分句状态机
（零改动；preroll/silence_blocks 的块数语义依赖块长恰为 1024）。

设计要点：
- VAD 强制 silero（构造时覆盖 vad 参数）：系统声音场景下 energy 门限的自适应
  噪声底会被 TTS/视频这类稳态播放内容拖着走，silero 频谱判决更稳。
- 与 MicSegmenter 共享同一基类：watchdog（healthy/error/FEED_STALE_S）、
  start/stop、流式扇出（stream_q）全部继承——main.py 条目1 守护逻辑零分支。
- 设备打不开只写 self.error：不崩服务，watchdog 按 MIC_REBUILD_COOLDOWN
  节奏重建，期间 status['mic'] 显示错误原因（与麦克风路径同语义）。
- 回退口径（计划 §条目14）：换 soundcard 库的差异集中在 _run_loopback 一处。
"""
import logging
import queue
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from capture import BLOCK, SR, AudioSegmenter

log = logging.getLogger("loopback")

LOOPBACK_FRAMES = 3072  # 48k 下每次回调 ≈64ms，重采样后恰为一个 16k BLOCK


def downmix(x: np.ndarray) -> np.ndarray:
    """多声道 → 单声道：通道均值 ((L+R)/2)。纯函数，供单测。"""
    x = np.asarray(x, dtype="float32")
    return x.mean(axis=1).astype("float32") if x.ndim > 1 else x


def resample_to_16k(x: np.ndarray, sr_in: int) -> np.ndarray:
    """任意输入采样率 → 16k 抗混叠重采样。纯函数，供单测。

    scipy.signal.resample_poly（计划裁定：3 倍降采样必须抗混叠，「线性插值」
    即 naive 抽点不可用）；整数比走 gcd 约简（48k → up=1/down=3），
    非 16k 整数倍的率（如 44.1k）同样适用。"""
    x = np.asarray(x, dtype="float32")
    sr_in = int(sr_in)
    if sr_in == SR:
        return x
    if sr_in <= 0:
        raise ValueError(f"非法采样率: {sr_in}")
    g = gcd(sr_in, SR)
    return resample_poly(x, SR // g, sr_in // g).astype("float32")


class LoopbackSegmenter(AudioSegmenter):
    """默认扬声器 WASAPI loopback 采集 → downmix → 16k → 共享分句状态机。
    run() 打开设备前 source_rate 为 None；打开后填充设备原生采样率（status 展示）。"""

    def __init__(self, out_queue: queue.Queue, **kw):
        # 条目14：loopback 强制 silero VAD——调用方传 vad 也被忽略
        kw["vad"] = "silero"
        super().__init__(out_queue, **kw)
        self.source_rate: int | None = None
        self._resid = np.zeros(0, dtype=np.float32)  # 不足一个 BLOCK 的重采样余数

    # —— PyAudio 回调：与 MicSegmenter._callback 同构（copy 入队防竞争） ——
    def _pa_callback(self, in_data, frame_count, time_info, status_flags):
        self._q_in.put(bytes(in_data))
        return (None, 0)  # pyaudio.paContinue

    def _feed_resampled(self, mono: np.ndarray) -> None:
        """重采样 → 攒余数 → 切 BLOCK 块喂状态机（与麦克风路径块长一致）。
        纯逻辑入口，供单测（真硬件路径经 run() → _pa_callback 到这里）。"""
        y = resample_to_16k(mono, self.source_rate or SR)
        buf = np.concatenate([self._resid, y]) if len(self._resid) else y
        n_full = (len(buf) // BLOCK) * BLOCK
        for i in range(0, n_full, BLOCK):
            self._consume_block(buf[i:i + BLOCK])
        self._resid = buf[n_full:]

    def run(self) -> None:
        """采集线程主函数：设备打不开/运行中异常都不崩——死因写 self.error，
        由 main.py 的 watchdog（条目1）发现并按 MIC_REBUILD_COOLDOWN 重建。"""
        try:
            self._run_loopback()
        except Exception as e:
            if self._stop.is_set():
                return  # 主动 stop 时的收尾异常不算故障
            self.error = f"{type(e).__name__}: {e}"
            log.warning(f"[loopback] ⚠ 采集线程异常退出: {self.error}")

    def _run_loopback(self) -> None:
        import pyaudiowpatch as pyaudio  # 延迟导入：缺包不影响其余链路与测试

        with pyaudio.PyAudio() as pa:
            try:
                # 注意：计划原文引用 get_best_wasapi_loopback()，但 0.2.12.9 实测
                # （v04-14 实施时本机验证）API 为 get_default_wasapi_loopback()
                # ——按依赖事实修正；无默认输出设备时抛 OSError → 优雅报错路径
                dev = pa.get_default_wasapi_loopback()
            except (OSError, IOError) as e:
                # 优雅报错：转成 RuntimeError 落 self.error，交给 watchdog 节奏
                raise RuntimeError(
                    f"找不到可用的 WASAPI loopback 设备（检查扬声器是否可用）: {e}") from e
            rate = int(dev["defaultSampleRate"])
            channels = max(1, int(dev["maxInputChannels"]))
            self.source_rate = rate
            log.info(f"[loopback] 设备: {dev['name']} @ {rate}Hz × {channels}ch"
                     f" → downmix → {SR}Hz（silero VAD）")

            # PyAudio 对带 stream_callback 的 open() 自动 start_stream
            stream = pa.open(
                format=pa.get_format_from_width(
                    pyaudio.get_sample_size(pyaudio.paFloat32)),
                channels=channels, rate=rate, input=True,
                input_device_index=dev["index"],
                frames_per_buffer=LOOPBACK_FRAMES,
                stream_callback=self._pa_callback,
            )
            try:
                while not self._stop.is_set():
                    try:
                        data = self._q_in.get(timeout=0.2)
                    except queue.Empty:
                        continue  # 空转窗口让 _stop 可被检查（stop 延迟 ≤0.2s）
                    raw = np.frombuffer(data, dtype=np.float32).reshape(-1, channels)
                    self._feed_resampled(downmix(raw))
            finally:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def snapshot(self) -> dict:
        d = super().snapshot()
        if self.source_rate:
            d["source_rate"] = self.source_rate
        return d
