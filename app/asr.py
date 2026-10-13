# -*- coding: utf-8 -*-
"""asr.py — faster-whisper 封装。
设备：auto（检测 CUDA，回退 CPU）/ cpu / cuda；GPU 默认 float16，CPU 默认 int8。
Windows 坑：pip 安装的 nvidia-cublas/cudnn DLL 不在默认搜索路径，
需 os.add_dll_directory 指向 site-packages/nvidia/*/bin 后才能加载。"""
import glob
import logging
import os
import site
import sys

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("asr")

SR = 16000


def _setup_cuda_dlls() -> None:
    """把 pip nvidia 包的 DLL 目录注册进进程搜索路径（仅 Windows 需要，幂等）。
    双保险：add_dll_directory 对现代加载方式生效；PATH 兜底 ctranslate2
    运行时（首次推理）的 LoadLibraryW 标准搜索——实测缺 PATH 会报
    "cublas64_12.dll is not found"。
    条目20：PyInstaller frozen 下 site.getsitepackages() 为空——把 _MEIPASS
    （build_exe.py 把 nvidia DLL 按 site-packages 原布局收集到
    _internal/nvidia/*/bin）加入候选集，开发/打包同一套查找代码。"""
    if os.name != "nt":
        return
    dirs: list[str] = []
    sps = set(site.getsitepackages()) | {site.getusersitepackages()}
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            sps.add(meipass)
    for sp in sps:
        dirs += glob.glob(os.path.join(sp, "nvidia", "*", "bin"))
        dirs += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
    if not dirs:
        return
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    path = os.environ.get("PATH", "")
    missing = [d for d in dirs if d not in path]
    if missing:
        os.environ["PATH"] = ";".join(missing) + ";" + path


def _resolve_device(device: str) -> str:
    if device in ("cuda", "cpu"):
        return device
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception as e:
        log.warning(f"[asr] ⚠ CUDA 检测失败({e})，回退 CPU")
    return "cpu"


class WhisperASR:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: str | None = None,
        language: str = "en",
        cpu_threads: int = 4,
        model_path: str | None = None,
    ):
        """model_path（条目20）：随包模型的本地目录——传入时 WhisperModel 用它加载
        （faster-whisper 原生支持本地路径），model_size 仅作展示名（status/UI）；
        None = 按名走 HF 缓存（开发态行为零变化）。"""
        self.language = language          # 配置口径：具体语言码 / "auto"（条目13）
        self.detected_lang: str | None = None  # auto 模式下最近一次「采纳」的检测语言
        self.device = _resolve_device(device)
        self.model_size = model_size
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")
        if self.device == "cuda":
            _setup_cuda_dlls()
        log.info(f"[asr] loading faster-whisper '{model_path or model_size}' "
                 f"on {self.device}/{self.compute_type} ...")
        self.model = WhisperModel(
            model_path or model_size,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=cpu_threads,
        )
        log.info("[asr] model ready")

    def transcribe(
        self,
        audio_f32: np.ndarray,
        initial_prompt: str | None = None,
        sample_rate: int = 16000,
        beam_size: int = 1,
        vad_filter: bool = True,
        word_timestamps: bool = False,
        with_times: bool = False,
    ):
        """转写。默认参数 = v0.2 整句路径的硬编码值（asr.py 原 beam_size=1 /
        vad_filter=True），调用点零变化。流式路径（条目 11）传 beam_size=5、
        vad_filter=False（分块解码开 VAD 会造成块边界/时间戳错位——技术评审）、
        word_timestamps=True（LocalAgreement-2 依赖词级时间戳）——此时返回原始
        segments 列表（含词级时间戳），否则返回拼接后的整句文本。
        sample_rate != 16000 时线性重采样（mic/loopback 主路径恒为 16k，仅兜底）。

        p2-3：word_timestamps=False 时再传 with_times=True → 返回
        `(text, segs)`，segs = [{"start","end","text"}]，时间**相对本段音频起点**
        （秒，faster-whisper 原始段级时间戳）。收集时间戳零额外开销——segments
        本来就要遍历一次拼文本，只是顺手把 start/end 也留下；**不启用
        word_timestamps**（那才会显著加耗时，见 smoke_test 的耗时增幅记录）。
        三种返回形态互斥且各自向后兼容：word_timestamps → list；with_times →
        (str, list)；默认 → str。"""
        if audio_f32.dtype != np.float32:
            audio_f32 = audio_f32.astype(np.float32)
        if sample_rate != 16000:
            n_out = int(round(len(audio_f32) * 16000 / sample_rate))
            audio_f32 = np.interp(
                np.linspace(0.0, len(audio_f32) - 1, n_out, dtype=np.float64),
                np.arange(len(audio_f32), dtype=np.float64),
                audio_f32,
            ).astype(np.float32)
        # 条目13①：auto = 让 whisper 检测，但仅段长 >3s 采纳检测结果——
        # 短段沿用上段采纳的语言（检测在短段上不可靠，防误检翻转）；无历史时
        # 短段传 None 检测转写但不采纳（评审修复："auto" 不是合法语言码，原样
        # 透传会让 faster-whisper 的 Tokenizer 抛 ValueError，课程开头所有 ≤3s
        # 段全部失败——含 warmup 与流式全部 chunk）。非 auto 语言码直接传。
        dur_s = len(audio_f32) / 16000.0
        lang_arg = self.language
        if lang_arg == "auto":
            if dur_s > 3.0:
                lang_arg = None
            elif self.detected_lang:
                lang_arg = self.detected_lang
            else:
                lang_arg = None  # 检测转写；结果不采纳（下方仅 dur_s>3s 才写入）
        segments, info = self.model.transcribe(
            audio_f32,
            language=lang_arg,
            beam_size=beam_size,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
            word_timestamps=word_timestamps,
        )
        if self.language == "auto" and dur_s > 3.0:
            detected = getattr(info, "language", None)
            if detected:
                self.detected_lang = detected  # 仅长段采纳（info.language 检测即得）
        if word_timestamps:
            return list(segments)  # 流式路径消费原始 segments（adapter 取词级时间戳）
        if with_times:
            parts: list[str] = []
            segs: list[dict] = []
            for s in segments:
                txt = (s.text or "").strip()
                if not txt:
                    continue
                parts.append(txt)
                segs.append({"start": float(getattr(s, "start", 0.0) or 0.0),
                             "end": float(getattr(s, "end", 0.0) or 0.0),
                             "text": txt})
            return " ".join(parts), segs
        parts = [s.text.strip() for s in segments if s.text.strip()]
        return " ".join(parts)

    def warmup(self) -> None:
        """加载后跑一次 0.5s 静音推理，把 GPU kernel 编译等一次性开销提前消化，
        避免课堂上第一句话突然卡 1-3 秒。失败不影响服务（仅打日志）。"""
        try:
            self.transcribe(np.zeros(SR // 2, dtype="float32"))
        except Exception as e:
            log.warning(f"[asr] ⚠ warmup 失败（不影响使用）: {e}")
