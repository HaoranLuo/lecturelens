# -*- coding: utf-8 -*-
"""capture.py — 音频分句状态机 + 麦克风采集。
产出：说话段（np.float32 @16k）队列，供 ASR 消费。
条目14：handle_block 状态机原样上移到 AudioSegmenter 基类（逻辑零改动），
MicSegmenter 只保留「打开麦克风源」的职责；loopback.py 的 LoopbackSegmenter
共享同一基类复用分句/watchdog，main.py 守护逻辑无需分支。
VAD 二选一：
- silero（默认）：faster-whisper 自带的 silero_vad_v6.onnx（无额外下载/依赖），
  按块输出语音概率，对教室稳态噪声（空调/风扇/翻书）比能量门限鲁棒。
- energy：RMS 门限 + 自适应噪声底。注意音频是 float32（幅度 [-1,1]），
  门限必须在 0.01 量级——v0 曾误用 int16 量级(250/200)导致永远检测不到语音。
两种 VAD 都返回 (enter_speech, quiet)，分句状态机与 VAD 实现解耦。"""
import logging
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

log = logging.getLogger("capture")

SR = 16000
BLOCK = 1024  # ~64ms per block（同时是 silero 的推理块长）


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


class EnergyVAD:
    """RMS 能量门限。
    噪声底 = 最近 ~19s 电平的 10 分位（最小统计法），双向自愈：
    初始 floor 估错或出现稳态嗡噪时都能自动跟上（v0 的指数滑动只会单向收敛，
    一开局把嗡噪当说话就会卡死）。启动先校准 ~2s。滞回防临界抖动。"""

    name = "energy"
    HISTORY = 300      # 块数 ≈ 19s
    CALIB_BLOCKS = 30  # 启动校准 ≈ 2s

    def __init__(self, factor: float = 3.0, floor: float = 0.004, min_threshold: float = 0.006):
        self.factor = factor
        self.noise_floor = floor
        self.min_threshold = min_threshold
        self.threshold = min_threshold
        self.level = 0.0
        self._levels: deque = deque(maxlen=self.HISTORY)

    def warmup(self) -> None:
        pass

    def feed(self, x: np.ndarray) -> tuple[bool, bool]:
        """返回 (enter_speech, quiet)：enter=开始/继续说话；quiet=低于退出线（计入静音）。"""
        self.level = rms(x)
        self._levels.append(self.level)
        self.threshold = max(self.noise_floor * self.factor, self.min_threshold)
        if len(self._levels) < self.CALIB_BLOCKS:
            self.noise_floor = float(np.median(self._levels))
            return False, True  # 校准期视为安静
        self.noise_floor = float(np.percentile(self._levels, 10))
        if self.level > self.threshold:
            return True, False
        if self.level < self.threshold * 0.7:
            return False, True
        return False, False  # 滞回区：不触发也不计静音

    def snapshot(self) -> dict:
        return {"vad": self.name, "level": round(self.level, 4),
                "threshold": round(self.threshold, 4)}


class SileroVAD:
    """faster-whisper 内置 silero v6 onnx 的流式封装。
    与 vad.py 批处理版的唯一区别：h/c/context 跨块保持，实现真流式（低延迟）。"""

    name = "silero"
    ENTER = 0.5
    EXIT = 0.35
    CHUNK = 512  # 该 onnx 固定输入 512+64=576，麦克风块(1024)按此切片推理

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self.session = get_vad_model().session
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros((1, 64), dtype="float32")
        self.prob = 0.0
        self.warmup()

    def _reset(self) -> None:
        self._h[:] = 0
        self._c[:] = 0
        self._context[:] = 0

    def warmup(self) -> None:
        self.feed(np.zeros(BLOCK, dtype="float32"))
        self._reset()

    def feed(self, x: np.ndarray) -> tuple[bool, bool]:
        x = np.asarray(x, dtype="float32").reshape(-1)
        prob = self.prob
        for i in range(0, len(x) - self.CHUNK + 1, self.CHUNK):
            chunk = x[i:i + self.CHUNK][None, :]
            row = np.concatenate([self._context, chunk], axis=1)  # (1, 576)
            out, self._h, self._c = self.session.run(
                None, {"input": row, "h": self._h, "c": self._c}
            )
            self._context = chunk[..., -64:]
            prob = float(np.asarray(out).reshape(-1)[-1])
        self.prob = prob  # 取块内最后一个 32ms 窗口的概率
        if self.prob > self.ENTER:
            return True, False
        if self.prob < self.EXIT:
            return False, True
        return False, False

    def snapshot(self) -> dict:
        return {"vad": self.name, "level": round(self.prob, 3), "threshold": self.ENTER}


def make_vad(kind: str):
    """按名字建 VAD；silero 不可用时回退 energy（打印原因）。"""
    if kind == "silero":
        try:
            v = SileroVAD()
            log.info("[vad] silero v6 ready (faster-whisper 内置)")
            return v
        except Exception as e:
            log.warning(f"[vad] ⚠ silero 初始化失败({e})，回退 energy VAD")
    log.info("[vad] energy VAD ready")
    return EnergyVAD()


class AudioSegmenter:
    """分句状态机基类（条目14：从 MicSegmenter 原样上移，逻辑零改动）。
    子类只需：① 打开自己的音频源（麦克风 sd.InputStream / WASAPI loopback
    PyAudioWPatch）；② 把 16k mono float32 块（推荐恰为 BLOCK 长，preroll 与
    silence_blocks 的块数语义依赖它）喂进 _consume_block。
    watchdog 接口（healthy / error / FEED_STALE_S / start / stop / snapshot）
    也在此层——main.py 的条目1 守护逻辑对任意子类通用。
    注：_q_in 里放什么由子类自定（麦克风放 ndarray 行，loopback 放原始字节）。"""

    def __init__(
        self,
        out_queue: queue.Queue,
        sample_rate: int = SR,
        silence_ms: int = 700,
        min_speech_ms: int = 500,
        # 2026-10 首期反馈②：连续说话时 15/18s 才定稿 → 中文字幕要等 20s+。
        # 默认下调到 8/10s（有静音仍优先按静音对齐切；只有一直不停嘴才用硬上限）。
        max_seg_s: float = 8.0,
        preroll_s: float = 0.4,
        vad: str = "silero",
        hard_cut_s: float = 10.0,
        stream_q: queue.Queue | None = None,
    ):
        self.q_out = out_queue
        # 条目11 可选扇出：原始音频块额外投递给流式管线（默认 None = 行为零变化，
        # 分句状态机逻辑不受影响——保护清单第 1 条允许的唯一触碰点）
        self.stream_q = stream_q
        self.sr = sample_rate
        self.silence_blocks = max(1, round(silence_ms / (BLOCK / SR * 1000)))
        self.min_speech_samples = int(min_speech_ms / 1000 * SR)
        self.max_seg_samples = int(max_seg_s * SR)
        self.hard_cut_samples = int(hard_cut_s * SR)  # 溢出后仍无静音的硬切兜底
        self.overflowed = False  # 缓冲已达软上限(max_seg_s)，改等静音对齐切点
        self.preroll: deque = deque(maxlen=int(preroll_s * SR / BLOCK))
        self._q_in: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        # 「立即断句」请求（首期反馈②）：只由服务线程置位、采集线程消费——
        # 缓冲的读写仍全部在采集线程，无跨线程写。
        self._flush = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None   # 采集线程致命异常（watchdog / UI 用）
        self.last_feed_ts: float = 0.0  # 最近一块音频到达的时刻（watchdog 用）
        self.speaking = False
        self.speech_buf: deque = deque()
        self.silence_count = 0
        self.vad = make_vad(vad)

    FEED_STALE_S = 3.0  # 超过该时长没有新音频块 = 采集断流（设备被抢/拔）

    @property
    def healthy(self) -> bool:
        """采集线程健康：无致命错误、线程存活、且最近 FEED_STALE_S 内有新块。
        静音时块也持续进来（VAD 判断在原始流之后），所以 last_feed_ts 停滞
        只能是采集断流，与『没人说话』不同。"""
        if self.error:
            return False
        if self.thread is None or not self.thread.is_alive():
            return False
        return (time.time() - self.last_feed_ts) < self.FEED_STALE_S

    # —— 分句状态机（与硬件解耦，smoke_test 直接喂块即可测） ——
    def request_flush(self) -> None:
        """请求「立即断句」：下一块音频到达时把当前缓冲立刻成段（不等静音/上限）。

        只置一个 Event，真正的切段在采集线程的 handle_block 里做（无跨线程写）。
        缓冲不足 min_speech 时按噪声丢弃（与正常切段同一条 _close 路径）。"""
        self._flush.set()

    def handle_block(self, x: np.ndarray) -> None:
        self.last_feed_ts = time.time()  # watchdog 心跳：有块进来 = 采集还活着
        enter, quiet = self.vad.feed(x)
        if enter:
            if not self.speaking:
                self.speaking = True
                self.speech_buf = deque(self.preroll)  # 带上前置音频防削字头
            self.speech_buf.append(x)
            self.silence_count = 0  # 语音继续：静音累计归零（v0 漏了这行，
            # 词间停顿的 quiet 块会跨语音段累计，攒够 11 块就在词中间误切）
        elif self.speaking:
            self.speech_buf.append(x)  # 词间停顿先保留
            if self.overflowed:
                # 已溢出：等 VAD 报静音立即对齐切点，不再等静音累计
                if quiet:
                    self._close()
                    return
            elif quiet:
                self.silence_count += 1
                if self.silence_count >= self.silence_blocks:
                    self._close()
                    return
            else:
                self.silence_count = 0
        else:
            self.preroll.append(x)
            return

        # 「立即断句」（首期反馈②）：用户点 ⚡断句 后，不等静音也不等上限，
        # 当前缓冲立即结算（_close 负责「太短按噪声丢弃」与成段）。
        if self._flush.is_set():
            self._flush.clear()
            if self.speaking:
                self._close()
                return

        # 软上限：缓冲达 max_seg_s 不立即硬切，标记溢出改等静音对齐；
        # 硬上限兜底：说话人始终不停（如整段朗读），超 hard_cut_s 仍无静音
        # 则在块边界硬切（_emit 复位 overflowed，speaking 保持连续收音）。
        # 注意 speech_buf 是块的 deque，len() 是块数——必须按样本数比较
        # （v0 用 len() 对 max_seg_samples 比较，单位错配导致 15s 上限从未生效）。
        buf_samples = sum(len(b) for b in self.speech_buf)
        if not self.overflowed and buf_samples >= self.max_seg_samples:
            self.overflowed = True
            log.info(f"[capture] 段超 {buf_samples / SR:.0f}s 仍在说话，改等静音对齐切点")
        elif self.overflowed and buf_samples >= self.hard_cut_samples:
            self._emit()

    def _close(self) -> None:
        if sum(len(b) for b in self.speech_buf) >= self.min_speech_samples:
            self._emit()
        else:
            self.speech_buf.clear()  # 太短当噪声丢弃
            self.overflowed = False
        self.speaking = False
        self.silence_count = 0

    def _emit(self) -> None:
        audio = np.concatenate(list(self.speech_buf)).astype(np.float32)
        self.speech_buf.clear()
        self.overflowed = False  # 每次切出后重新计软上限
        reset = getattr(self.vad, "_reset", None)
        if reset:
            reset()  # 段边界重置 VAD 状态
        self.q_out.put((time.time(), audio))

    def _consume_block(self, mono: np.ndarray) -> None:
        """采集线程取到一块后的统一入口：先走分句状态机，再可选扇出给流式管线。
        扇出满队即丢块（只影响 partial 预览，绝不反压定稿路径）——条目 11。"""
        self.handle_block(mono)
        if self.stream_q is not None:
            try:
                self.stream_q.put_nowait(mono)
            except queue.Full:
                pass

    def start(self) -> None:
        self.last_feed_ts = time.time()  # 启动宽限：3s 内 InputStream 打开并开始喂块
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def snapshot(self) -> dict:
        d = self.vad.snapshot()
        d["speaking"] = self.speaking
        return d


class MicSegmenter(AudioSegmenter):
    """麦克风采集（sounddevice InputStream）——只负责打开音频源，
    分句状态机/watchdog 全部继承自 AudioSegmenter（条目14）。"""

    def __init__(
        self,
        out_queue: queue.Queue,
        sample_rate: int = SR,
        silence_ms: int = 700,
        min_speech_ms: int = 500,
        max_seg_s: float = 8.0,
        preroll_s: float = 0.4,
        device: int | str | None = None,
        vad: str = "silero",
        hard_cut_s: float = 10.0,
        stream_q: queue.Queue | None = None,
    ):
        super().__init__(out_queue, sample_rate=sample_rate, silence_ms=silence_ms,
                         min_speech_ms=min_speech_ms, max_seg_s=max_seg_s,
                         preroll_s=preroll_s, vad=vad, hard_cut_s=hard_cut_s,
                         stream_q=stream_q)
        self.device = device

    def _callback(self, indata, frames, time_info, status):
        self._q_in.put(indata.copy())  # overflow 等状态 v0 不打日志防刷屏

    def run(self) -> None:
        """采集线程主函数。设备被 Teams/Zoom 抢占、拔插、VAD 崩溃等任何异常
        都不再静默杀死线程：死因存 self.error，由 main.py 的 watchdog 发现并重建。"""
        try:
            with sd.InputStream(
                samplerate=self.sr, channels=1, dtype="float32",
                blocksize=BLOCK, device=self.device, callback=self._callback,
            ):
                while not self._stop.is_set():
                    try:
                        block = self._q_in.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    self._consume_block(block[:, 0] if block.ndim > 1 else block)
        except Exception as e:
            if self._stop.is_set():
                return  # 主动 stop 时的收尾异常不算故障
            self.error = f"{type(e).__name__}: {e}"
            log.warning(f"[capture] ⚠ 采集线程异常退出: {self.error}")
