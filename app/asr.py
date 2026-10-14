# -*- coding: utf-8 -*-
"""asr.py — 转写后端封装（faster-whisper / Apple MLX）。

设备：auto（Mac arm64 且 mlx_whisper 可导入 → MLX；否则检测 CUDA，回退 CPU）
      / cpu / cuda（faster-whisper + ctranslate2）/ mlx（Apple Silicon）
- **MLX 后端**（Mac 适配·第二批）：mlx-whisper 在 Apple GPU/统一内存上跑 Whisper，
  权重来自 `models.MLX_MODEL_REPOS`（写死白名单，mlx-community）。与 ctranslate2
  体系**互不干扰**：模型表、缓存判据（weights.safetensors）、下载路径各自独立；
  显式 device="mlx" 而 MLX 不可用（非 Mac/非 arm64/没装包）→ 自动回落 CPU。
- MLX 的两处**事实差异**（已在本文件内消化，调用方零感知）：
  ① 没有 vad_filter（那是 faster-whisper 的扩展，传了会被当未知 decode option
     抛 TypeError）——静音过滤在采集侧状态机已做过一遍，如实忽略并记一次日志；
  ② mlx_whisper 0.4.3 的 beam search **未实现**（decoding.py 直接
     NotImplementedError）——流式路径的 beam_size=5 必须丢掉，回落贪心解码。

Windows 坑：pip 安装的 nvidia-cublas/cudnn DLL 不在默认搜索路径，
需 os.add_dll_directory 指向 site-packages/nvidia/*/bin 后才能加载。
"""
import glob
import logging
import os
import platform
import site
import sys

import numpy as np
from faster_whisper import WhisperModel

import models as models_mod   # MLX 权重表（只读常量；models 不 import asr，无环）

log = logging.getLogger("asr")

SR = 16000

#: 一次性日志去重（MLX 的参数差异只提示一次，避免每段转写刷屏）
_LOGGED_ONCE: set = set()


def _log_once(key: str, msg: str) -> None:
    if key not in _LOGGED_ONCE:
        _LOGGED_ONCE.add(key)
        log.info(msg)


def mlx_available() -> bool:
    """MLX 后端是否可用 = darwin + arm64 + mlx_whisper 可导入（三项缺一即 False）。

    为什么三项都要：mlx/mlx-whisper 只支持 Apple Silicon（Intel Mac 装了也跑不了），
    import 失败则连模型都构造不出来。**先查平台再 import**——非 darwin 一次第三方
    import 都不做（Windows/Linux 的启动开销与行为零变化）。"""
    if sys.platform != "darwin":
        return False
    try:
        if platform.machine().lower() not in ("arm64", "aarch64"):
            return False
    except Exception:
        return False
    try:
        import mlx_whisper  # noqa: F401
    except Exception as e:
        log.info(f"[asr] MLX 不可用（import mlx_whisper 失败：{type(e).__name__}: {e}）")
        return False
    return True


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
    """设备解析：cpu/cuda 原样；mlx（显式）→ 不可用则**回落 CPU**；auto 的次序是
    「Mac arm64 有 MLX 就用 MLX」→ 否则沿用原有的 CUDA 探测（有卡 → cuda，无卡/探测
    失败 → cpu）。非 darwin 上 mlx_available() 恒 False，故 Windows/Linux 行为逐字不变。"""
    if device == "mlx":
        # 显式要 MLX 而本机不具备（非 Mac / Intel Mac / 没装 mlx-whisper）→ CPU 兜底，
        # 不抛异常（ask 口径：MLX 不可用/非 arm64 自动回落现有 CPU；日志留痕）。
        if mlx_available():
            return "mlx"
        log.warning("[asr] ⚠ 要求 device=mlx 但本机 MLX 不可用（需 Apple Silicon + "
                    "mlx-whisper），本次回落 CPU 推理")
        return "cpu"
    if device in ("cuda", "cpu"):
        return device
    if mlx_available():
        return "mlx"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception as e:
        log.warning(f"[asr] ⚠ CUDA 检测失败({e})，回退 CPU")
    return "cpu"


def active_backend(device: str | None = None) -> str:
    """给定 device 配置值 → 实际会用哪个后端（"mlx" / "ctranslate2"）。

    模型管理器用它决定「该看/该下哪张仓库表」（ctranslate2 的 Systran 权重 vs
    MLX 的 mlx-community 权重）；不传 = 按 auto 解析（与 DEFAULT_CONFIG 一致）。"""
    return "mlx" if _resolve_device(str(device or "auto")) == "mlx" else "ctranslate2"


# —— MLX 返回形状适配（纯函数，见 mlx_to_shapes） ——

class _MlxWord:
    """mlx_whisper 词条 dict 的鸭子类型替身（流式适配器只读 start/end/word）。"""

    __slots__ = ("word", "start", "end")

    def __init__(self, word: str, start: float, end: float):
        self.word = word
        self.start = start
        self.end = end


class _MlxSegment:
    """mlx_whisper 段 dict 的鸭子类型替身（faster-whisper 段对象被读的属性：
    start/end/text/no_speech_prob/words —— 见 streaming.WhisperASRAdapter）。"""

    __slots__ = ("start", "end", "text", "no_speech_prob", "words")

    def __init__(self, start: float, end: float, text: str,
                 no_speech_prob: float, words: list):
        self.start = start
        self.end = end
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.words = words


def _mlx_segments(result) -> list:
    """mlx 结果 dict → [_MlxSegment]（坏数据/null 一律跳过，绝不为形状问题抛异常）。"""
    out: list = []
    segs = result.get("segments") if isinstance(result, dict) else None
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        words = []
        for w in (s.get("words") or []):
            if not isinstance(w, dict):
                continue
            words.append(_MlxWord(str(w.get("word") or ""),
                                  float(w.get("start") or 0.0),
                                  float(w.get("end") or 0.0)))
        out.append(_MlxSegment(float(s.get("start") or 0.0),
                               float(s.get("end") or 0.0),
                               str(s.get("text") or ""),
                               float(s.get("no_speech_prob") or 0.0),
                               words))
    return out


def mlx_to_shapes(result, *, word_timestamps: bool = False,
                  with_times: bool = False):
    """mlx_whisper.transcribe 的 dict 输出 → 本模块的**三种返回形态**（纯函数，可单测）。

    形态对齐 faster-whisper 路径（见 WhisperASR.transcribe 的 docstring，三者互斥）：
    - word_timestamps=True → 段对象列表（鸭子类型，供流式 LocalAgreement 取词级时间戳）；
    - with_times=True      → (text, [{"start","end","text"}]，时间相对本段音频起点)；
    - 默认                 → 拼接后的整句文本。

    文本一律**由段拼**（丢空段、段间一个空格）而不是直接取 result["text"]：两条后端
    产出的笔记落盘格式必须一致（faster-whisper 路径就是这么拼的）。"""
    segs = _mlx_segments(result)
    if word_timestamps:
        return segs
    kept = [s for s in segs if s.text.strip()]
    text = " ".join(s.text.strip() for s in kept)
    if with_times:
        return text, [{"start": s.start, "end": s.end, "text": s.text.strip()}
                      for s in kept]
    return text


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
        None = 按名走 HF 缓存（开发态行为零变化）。Mac 适配：解析到 mlx 后端时，
        model_path = 本地 MLX 权重目录（缓存命中）或 None（按仓库 id 交给 mlx_whisper
        联网取——它内部走 huggingface_hub，hfenv 的镜像开关同样生效）。"""
        self.language = language          # 配置口径：具体语言码 / "auto"（条目13）
        self.detected_lang: str | None = None  # auto 模式下最近一次「采纳」的检测语言
        self.device = _resolve_device(device)
        self.model_size = model_size
        self.backend = "ctranslate2"
        self.model_repo: str | None = None      # MLX 权重仓（短名 → 白名单表）
        if self.device == "mlx":
            repo = models_mod.MLX_MODEL_REPOS.get(model_size)
            if repo is None:
                # 档位表里的 6 个短名都有 MLX 权重；其它名字（手改 config 才会出现）
                # 按 CPU 兜底——**不能**拿一个不对应的仓库去加载，那等于用 A 的权重
                # 冒充 B（"模型未下载"的错误至少是诚实的）。
                log.warning(f"[asr] ⚠ 没有 {model_size} 的 MLX 权重，回落 CPU 推理")
                self.device = "cpu"
            else:
                self.backend = "mlx"
                self.model_repo = repo
                # mlx 没有 compute_type 概念：权重精度由仓库决定（我们只挑 fp16 仓，
                # 量化仓另建条目不在这张表里），Apple GPU 计算按 fp16 —— status 里
                # 显示 float16 就是「实际计算精度」这个事实。
                self.compute_type = "float16"
                # 本地路径（D2 的 bundled/缓存命中）优先，否则传仓库 id ——
                # mlx_whisper 内部对不存在的路径走 huggingface_hub.snapshot_download
                # （hfenv 的镜像/离线开关同样生效：它 import 的就是 huggingface_hub）。
                self.model_ref: str = model_path or self.model_repo
                log.info(f"[asr] loading MLX whisper '{self.model_ref}' on mlx/float16 ...")
                # 模型实体由 mlx_whisper 的 ModelHolder 惰性加载（首次 transcribe 时
                # 真正读盘；同一路径只加载一次）——这里不预加载，warmup() 会触发它。
                return
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
        MLX 后端（device=mlx）走 _transcribe_mlx，返回形状与上面三种完全一致。

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
        if self.backend == "mlx":
            return self._transcribe_mlx(
                audio_f32, lang_arg=lang_arg, dur_s=dur_s,
                initial_prompt=initial_prompt, beam_size=beam_size,
                vad_filter=vad_filter, word_timestamps=word_timestamps,
                with_times=with_times)
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

    def _transcribe_mlx(self, audio_f32: np.ndarray, *, lang_arg, dur_s: float,
                        initial_prompt, beam_size: int, vad_filter: bool,
                        word_timestamps: bool, with_times: bool):
        """MLX（Apple Silicon）路径：调用 mlx_whisper.transcribe 后交给纯函数
        mlx_to_shapes 折成本模块的三种返回形态（调用方零感知）。

        与 faster-whisper 路径的**两处事实差异**（都是上游能力差异，不是取舍）：
        ① mlx_whisper 没有 `vad_filter`（那是 faster-whisper 的扩展）——传过去会被
           `DecodingOptions(**kwargs)` 当未知字段直接 TypeError。本仓库的静音过滤在
           采集侧状态机（silero/energy）已经做过一遍，故如实忽略并记一次日志。
        ② mlx_whisper 0.4.3 的 beam search **未实现**（decoding.py:436
           `NotImplementedError`）——流式路径传的 beam_size=5 只能丢掉，回落贪心。
           这是「Mac 上流式可用，但解码策略与 Windows/CUDA 的 beam=5 略有不同」的
           诚实交代（写进日志，不假装一致）。
        `condition_on_previous_text=False` 与 faster-whisper 路径同口径（流式上下文
        由 OnlineASRProcessor 自己管 prompt）。"""
        import mlx_whisper
        if int(beam_size or 0) > 1:
            _log_once("mlx-beam",
                      "[asr] MLX 后端暂不支持 beam search（mlx_whisper 未实现），"
                      "本次起回落贪心解码")
        if vad_filter:
            _log_once("mlx-vad",
                      "[asr] MLX 后端没有 vad_filter 参数（faster-whisper 的扩展）——"
                      "静音过滤由采集侧状态机完成，行为一致")
        result = mlx_whisper.transcribe(
            audio_f32,
            path_or_hf_repo=self.model_ref,
            language=lang_arg,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
            word_timestamps=bool(word_timestamps),
            verbose=None,      # 不打字幕到控制台（服务端日志才是一切事实来源）
        )
        if self.language == "auto" and dur_s > 3.0:
            detected = result.get("language") if isinstance(result, dict) else None
            if detected:
                self.detected_lang = detected  # 仅长段采纳（与 faster-whisper 侧同款）
        return mlx_to_shapes(result, word_timestamps=word_timestamps,
                             with_times=with_times)

    def warmup(self) -> None:
        """加载后跑一次 0.5s 静音推理，把 GPU kernel 编译等一次性开销提前消化，
        避免课堂上第一句话突然卡 1-3 秒。失败不影响服务（仅打日志）。"""
        try:
            self.transcribe(np.zeros(SR // 2, dtype="float32"))
        except Exception as e:
            log.warning(f"[asr] ⚠ warmup 失败（不影响使用）: {e}")
