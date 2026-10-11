# -*- coding: utf-8 -*-
"""capture.py — 麦克风采集 + 能量 VAD 分句。
产出：说话段（np.float32 @16k）队列，供 ASR 消费。
自适应噪声底：安静时缓慢学习环境噪声，门限 = 噪声底 × 系数。"""
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 1024  # ~64ms per block


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


class MicSegmenter:
    def __init__(
        self,
        out_queue: queue.Queue,
        sample_rate: int = SR,
        silence_ms: int = 700,
        min_speech_ms: int = 500,
        max_seg_s: float = 15.0,
        factor: float = 3.0,
        floor: float = 250.0,
        preroll_s: float = 0.4,
        device: int | str | None = None,
    ):
        self.q_out = out_queue
        self.sr = sample_rate
        self.silence_blocks = max(1, int(silence_ms / 1000 * 1000 / (BLOCK / SR * 1000)))
        self.min_speech_samples = int(min_speech_ms / 1000 * SR)
        self.max_seg_samples = int(max_seg_s * SR)
        self.factor = factor
        self.noise_floor = floor
        self.preroll: deque = deque(maxlen=int(preroll_s * SR / BLOCK))
        self.device = device
        self._q_in: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_rms = 0.0
        self.speaking = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass  # overflow 等可忽略，v0 不打日志防刷屏
        self._q_in.put(bytes(indata)) if False else self._q_in.put(indata.copy())

    def _emit(self, buf: deque) -> None:
        audio = np.concatenate(list(buf)).astype(np.float32)
        self.q_out.put((time.time(), audio))

    def run(self) -> None:
        speech_buf: deque = deque()
        silence_count = 0
        with sd.InputStream(
            samplerate=self.sr, channels=1, dtype="float32",
            blocksize=BLOCK, device=self.device, callback=self._callback,
        ):
            while not self._stop.is_set():
                try:
                    block = self._q_in.get(timeout=0.2)
                except queue.Empty:
                    continue
                x = block[:, 0] if block.ndim > 1 else block
                level = rms(x)
                self.last_rms = level
                threshold = max(self.noise_floor * self.factor, 200.0)
                voiced = level > threshold

                if voiced:
                    silence_count = 0
                    if not speech_buf:
                        self.speaking = True
                        speech_buf.extend(self.preroll)  # 带上前置音频防削字头
                    speech_buf.append(x)
                    if len(speech_buf) > self.max_seg_samples:
                        self._emit(speech_buf)
                        speech_buf.clear()
                        silence_count = 0
                else:
                    if speech_buf:
                        speech_buf.append(x)
                        silence_count += 1
                        if silence_count >= self.silence_blocks:
                            total = sum(len(b) for b in speech_buf)
                            if total >= self.min_speech_samples:
                                self._emit(speech_buf)
                            speech_buf.clear()
                            self.speaking = False
                            silence_count = 0
                    else:
                        # 安静：缓慢学习环境噪声底
                        self.noise_floor = (
                            self.noise_floor * 0.95 + level * 0.05
                        )
                        self.preroll.append(x)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=2)
