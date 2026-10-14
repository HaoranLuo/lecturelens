# -*- coding: utf-8 -*-
"""loopback.py — 系统声音内录（升级计划条目 14；Mac 适配·第二批）。

采集链路（两个平台共用后半段）：打开系统声音设备（48k/设备原生率，多声道 float32）
→ downmix (L+R)/2 → scipy.signal.resample_poly 抗混叠重采样到 16k → 切成 BLOCK(1024)
块 → 复用 capture.AudioSegmenter 的分句状态机（零改动；preroll/silence_blocks 的块数
语义依赖块长恰为 1024）。

平台分支（只在**打开设备**这一步）：
- **Windows**：PyAudioWPatch 的 WASAPI loopback（默认扬声器），**延迟导入**——
  缺包/非 Windows 不影响其余链路；路径与行为逐字未动。
- **macOS（非 Windows）**：sounddevice + **BlackHole** 虚拟音频设备（设备名默认
  "BlackHole 2ch"，可用 config.input_device 传索引覆盖——/api/devices 会列出它）。
  设备原生采样率 × 2ch → 同一套 downmix → resample → _feed_resampled。

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
import os
import queue
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from capture import BLOCK, SR, AudioSegmenter

log = logging.getLogger("loopback")

LOOPBACK_FRAMES = 3072  # 48k 下每次回调 ≈64ms，重采样后恰为一个 16k BLOCK

#: macOS 内录设备的默认名（BlackHole 2ch 的出厂设备名；装 BlackHole 16ch 或改过名的
#: 机器可用同款关键字命中）。config.input_device 传**索引**时优先按索引取。
BLACKHOLE_DEFAULT_NAME = "BlackHole 2ch"
#: 名字匹配用的关键字（小写子串；比全名匹配宽容些）
BLACKHOLE_KEYWORD = "blackhole"
#: macOS 上内录需要的外部前提（错误文案里给可操作指引，不只是报错）
BLACKHOLE_GUIDE = ("macOS 内录需要先装 BlackHole（免费虚拟音频设备，"
                   "`brew install blackhole-2ch` 或官网 existential.audio/blackhole），"
                   "再到「音频 MIDI 设置」把系统输出指向它——BGM/视频声才会进内录；"
                   "装好后重启 LectureLens")


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
    """系统声音内录采集 → downmix → 16k → 共享分句状态机。
    run() 打开设备前 source_rate 为 None；打开后填充设备原生采样率（status 展示）。

    device：**内录设备覆盖**（可选）。macOS 上 = /api/devices 列出的索引（BlackHole），
    由 main.apply_segmenter 从 config.input_device 传入（与 MicSegmenter 同款）；
    Windows 的 WASAPI 路径仍走系统默认扬声器，不读这个值（行为逐字不变）。"""

    def __init__(self, out_queue: queue.Queue, device=None, **kw):
        # 条目14：loopback 强制 silero VAD——调用方传 vad 也被忽略
        kw["vad"] = "silero"
        super().__init__(out_queue, **kw)
        self.device = device
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
        """采集主体：**平台分发**（只在「打开设备」这一步分叉）。

        Windows → WASAPI loopback（PyAudioWPatch）；非 Windows（macOS）→
        sounddevice + BlackHole。两条分支的设备数据都汇进同一套
        downmix → resample_to_16k → _feed_resampled，故切段/VAD/流式扇出行为一致。"""
        if os.name == "nt":
            self._run_loopback_wasapi()
        else:
            self._run_loopback_sounddevice()

    def _run_loopback_wasapi(self) -> None:
        """Windows：默认扬声器的 WASAPI loopback（原路径，逐字未动）。
        pyaudiowpatch **延迟导入**：非 Windows 装不上这个包，缺包也不影响其余链路。"""
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

    def _resolve_sounddevice_device(self, sd) -> tuple[int, str, int, int]:
        """非 Windows（macOS）：选内录设备 → (索引, 名字, 原生采样率, 通道数)。

        次序（与 ask 口径一致）：
        ① config.input_device 传了**索引**（/api/devices 列出的那个）→ 就用它，
           索引不可用（越界/无输入通道）如实报错，不偷偷换设备；
        ② 否则按名字找 BlackHole（默认 "BlackHole 2ch"，装了 16ch/改过名也命中）；
        ③ 都找不到 → RuntimeError 带安装与路由指引（watchdog 会按节奏重建，
           用户装好 BlackHole 后无需重启应用即可自愈）。"""
        devs = sd.query_devices()

        def _usable(d) -> bool:
            return int(d.get("max_input_channels") or 0) > 0

        want = self.device
        if isinstance(want, bool):        # bool 是 int 子类，先挡掉（config 层已挡，双保险）
            want = None
        if isinstance(want, int) and want >= 0:
            try:
                d = devs[want]
            except Exception:
                d = None
            if d is None or not _usable(d):
                raise RuntimeError(
                    f"内录设备索引 {want} 不可用（无此设备或没有输入通道）——"
                    f"请在设置页重新选设备（见 /api/devices 的列表）。{BLACKHOLE_GUIDE}")
            return (want, str(d.get("name") or want),
                    int(d["default_samplerate"]), min(2, int(d["max_input_channels"])))

        for i, d in enumerate(devs):
            if not _usable(d):
                continue
            name = str(d.get("name") or "")
            if BLACKHOLE_KEYWORD in name.lower():
                return (i, name, int(d["default_samplerate"]),
                        min(2, int(d["max_input_channels"])))

        names = [str(d.get("name")) for d in devs if _usable(d)]
        log.warning(f"[loopback] 可用输入设备: {names}")
        raise RuntimeError(f"找不到 BlackHole 等内录设备。{BLACKHOLE_GUIDE}")

    def _run_loopback_sounddevice(self) -> None:
        """macOS 内录：BlackHole 虚拟设备（设备原生率 × 2ch，float32）→ downmix →
        16k → 共享分句状态机。

        blocksize 交给 PortAudio 自选（不设 LOOPBACK_FRAMES：sounddevice 的块长由后端
        与设备决定，**余数由 _feed_resampled 的 resid 缓冲吸收**——块长不必等于 BLOCK，
        这是那条链路本来就能处理的情况）。silero 强制在 __init__ 保证。"""
        import sounddevice as sd

        idx, name, rate, channels = self._resolve_sounddevice_device(sd)
        self.source_rate = rate
        log.info(f"[loopback] 内录设备: {name} @ {rate}Hz × {channels}ch"
                 f" → downmix → {SR}Hz（silero VAD）")

        def _cb(indata, frames, time_info, status_flags):
            self._q_in.put(bytes(indata))   # copy 入队（indata 是复用缓冲，与 _pa_callback 同款）

        stream = sd.InputStream(samplerate=rate, channels=channels, dtype="float32",
                                device=idx, callback=_cb)
        with stream:
            while not self._stop.is_set():
                try:
                    data = self._q_in.get(timeout=0.2)
                except queue.Empty:
                    continue  # 空转窗口让 _stop 可被检查（stop 延迟 ≤0.2s）
                raw = np.frombuffer(data, dtype=np.float32).reshape(-1, channels)
                self._feed_resampled(downmix(raw))

    def snapshot(self) -> dict:
        d = super().snapshot()
        if self.source_rate:
            d["source_rate"] = self.source_rate
        return d
