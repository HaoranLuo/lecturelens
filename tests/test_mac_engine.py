# -*- coding: utf-8 -*-
"""tests/test_mac_engine.py — Mac 适配第二批：音频采集 / MLX 转写后端 / 导出字体 / 档位。

开发机是 Windows，故「非 Windows / Apple 侧」一律用**替身平台与替身模块**驱动
（与 test_mac_adapt.py、test_power_notice.py 同款手法），Windows 侧行为不变由既有
用例继续钉住（test_loopback / test_exporters / test_models / test_tiers / test_hf_env）：

① loopback.py 的平台分发：Windows 走 WASAPI（原路径），非 Windows 走 sounddevice +
   BlackHole（按名找设备 / config.input_device 索引覆盖 / 找不到给安装指引）；
② asr.py 的 MLX 后端：设备解析（darwin+arm64+包在位 → mlx，否则回落）、
   mlx 返回形状适配器（三种形态 + 鸭子类型与流式消费者对得上）、
   两处上游事实差异（无 vad_filter、beam search 未实现）不炸；
③ models.py 的后端双表：短名→仓库、缓存条目目录、完整性判据按后端切；默认不变；
④ exporters.py 的字体平台链：macOS 候选/扫描/报错文案；Windows 口径逐字不变；
⑤ tiers.py：MLX 就绪与否只改推荐理由的措辞，阈值不变；
⑥ 前端静态断言：设备徽章（cuda/mlx/cpu）与 ERROR_MAP 的 mlx 条目。
"""
import io
import os
import queue
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import asr as asr_mod
import exporters as exp_mod
import loopback as lb_mod
import main
import models as models_mod
import tiers as tiers_mod

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
IS_WIN = os.name == "nt"


# ---------------- ① loopback：平台分发与设备选择 ----------------

class FakeSD:
    """替身 sounddevice：只提供 _resolve_sounddevice_device 用到的 query_devices。"""

    def __init__(self, devices):
        self.devices = devices

    def query_devices(self):
        return self.devices


_MIC = {"name": "MacBook Pro Microphone", "max_input_channels": 1,
        "default_samplerate": 48000.0}
_BH2 = {"name": "BlackHole 2ch", "max_input_channels": 2,
        "default_samplerate": 48000.0}
_BH16 = {"name": "BlackHole 16ch", "max_input_channels": 16,
         "default_samplerate": 48000.0}


def test_loopback_dispatches_by_platform(monkeypatch):
    """Windows → WASAPI；非 Windows → sounddevice。两条路各自被调用一次（互斥）。"""
    calls: list = []
    monkeypatch.setattr(lb_mod.LoopbackSegmenter, "_run_loopback_wasapi",
                        lambda self: calls.append("wasapi"))
    monkeypatch.setattr(lb_mod.LoopbackSegmenter, "_run_loopback_sounddevice",
                        lambda self: calls.append("sounddevice"))
    seg = lb_mod.LoopbackSegmenter(queue.Queue())
    monkeypatch.setattr(lb_mod.os, "name", "nt")
    seg._run_loopback()
    monkeypatch.setattr(lb_mod.os, "name", "posix")
    seg._run_loopback()
    assert calls == ["wasapi", "sounddevice"]


def test_loopback_picks_blackhole_by_name():
    """默认：按名字找 BlackHole（装 16ch 也命中）；通道数封顶 2。"""
    seg = lb_mod.LoopbackSegmenter(queue.Queue())
    assert seg._resolve_sounddevice_device(FakeSD([_MIC, _BH2])) == \
        (1, "BlackHole 2ch", 48000, 2)
    got = seg._resolve_sounddevice_device(FakeSD([_MIC, _BH16]))
    assert got[0] == 1 and got[3] == 2, "16ch 也只收 2 通道（downmix 后本就单声道）"


def test_loopback_device_index_override_wins():
    """config.input_device（索引）优先于按名查找——用户在设置页手选的设备说了算。"""
    seg = lb_mod.LoopbackSegmenter(queue.Queue(), device=0)
    assert seg._resolve_sounddevice_device(FakeSD([_MIC, _BH2]))[1] == _MIC["name"]


def test_loopback_guides_when_blackhole_missing():
    """找不到 BlackHole：报错必须带安装/路由指引（不是一句「设备不可用」）。"""
    seg = lb_mod.LoopbackSegmenter(queue.Queue())
    with pytest.raises(RuntimeError) as ei:
        seg._resolve_sounddevice_device(FakeSD([_MIC]))
    msg = str(ei.value)
    assert "BlackHole" in msg and "音频 MIDI 设置" in msg


def test_loopback_bad_index_is_an_error_not_a_substitution():
    """索引越界/无输入通道：如实报错，**不偷偷换设备**。"""
    seg = lb_mod.LoopbackSegmenter(queue.Queue(), device=9)
    with pytest.raises(RuntimeError):
        seg._resolve_sounddevice_device(FakeSD([_MIC, _BH2]))
    seg2 = lb_mod.LoopbackSegmenter(queue.Queue(), device=0)
    with pytest.raises(RuntimeError):
        seg2._resolve_sounddevice_device(FakeSD([{"name": "An Output Only",
                                                  "max_input_channels": 0,
                                                  "default_samplerate": 48000.0}]))


def test_loopback_sounddevice_path_reuses_the_shared_chain(monkeypatch):
    """sounddevice 分支的音频必须走既有链路（downmix → resample → 状态机），不另写一套。

    替身 InputStream 进入时投递一帧 48k 立体声；`_feed_resampled`（既有链路的入口）
    被打桩记账——它被调到即说明数据流没有另起炉灶，顺带验证设备原生率记进
    source_rate、downmix 后的单声道样本数正确（1024×2ch → 1024 样本）。"""
    fed: list = []

    def _fake_feed(self, mono):
        fed.append(np.asarray(mono).copy())
        self._stop.set()          # 收一帧即收工（否则要等 0.2s 空转超时）

    monkeypatch.setattr(lb_mod.LoopbackSegmenter, "_feed_resampled", _fake_feed)

    class FakeSD2(FakeSD):
        """替身 sounddevice：InputStream 返回一个「进入即投一帧」的上下文对象。"""

        def InputStream(self, **kw):
            captured.update(kw)
            seg = self.sd_seg

            class _Stream:
                def __enter__(self_inner):
                    block = np.zeros((1024, 2), dtype="float32")
                    seg._q_in.put(bytes(block))
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _Stream()

    captured: dict = {}
    fake_sd = FakeSD2([_MIC, _BH2])
    seg = lb_mod.LoopbackSegmenter(queue.Queue())
    fake_sd.sd_seg = seg
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    seg._run_loopback_sounddevice()
    assert seg.source_rate == 48000
    assert captured["channels"] == 2 and captured["samplerate"] == 48000
    assert captured["device"] == 1               # BlackHole 的索引（不是「跟随默认」）
    assert len(fed) == 1
    assert fed[0].shape == (1024,), f"downmix 后应为 1024 样本（单声道），实得 {fed[0].shape}"


# ---------------- ② asr：MLX 设备解析与返回形状适配 ----------------

def test_mlx_unavailable_on_windows_keeps_old_resolution():
    """Windows：mlx 恒不可用 → auto 仍是「CUDA 探测」，显式 mlx 回落 cpu。"""
    assert asr_mod.mlx_available() is False          # 本机 Windows
    assert asr_mod._resolve_device("cpu") == "cpu"
    assert asr_mod._resolve_device("cuda") == "cuda"
    assert asr_mod._resolve_device("mlx") == "cpu"   # 显式要 MLX 而本机没有 → CPU 兜底
    assert asr_mod.active_backend(None) == "ctranslate2"
    assert asr_mod.active_backend("mlx") == "ctranslate2"


def test_mlx_available_requires_darwin_arm64_and_package(monkeypatch):
    """三项缺一即不可用：非 darwin / 非 arm64 / 包不在位。"""
    monkeypatch.setattr(asr_mod.sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "x86_64")
    assert asr_mod.mlx_available() is False          # Intel Mac
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.ModuleType("mlx_whisper"))
    assert asr_mod.mlx_available() is True
    assert asr_mod._resolve_device("auto") == "mlx"  # auto 在 Mac arm64 上优先 MLX
    assert asr_mod.active_backend("auto") == "mlx"
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # import 失败路径
    with pytest.MonkeyPatch.context() as mp:
        mp.delitem(sys.modules, "mlx_whisper")
        assert asr_mod.mlx_available() is False


_MLX_RESULT = {
    "text": "（原样文本不用）",
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 1.25, "text": " Hello world.",
         "no_speech_prob": 0.02,
         "words": [{"word": " Hello", "start": 0.0, "end": 0.5, "probability": 0.9},
                   {"word": " world.", "start": 0.5, "end": 1.25, "probability": 0.8}]},
        {"start": 1.25, "end": 1.4, "text": "   ", "no_speech_prob": 0.97, "words": []},
    ],
}


def test_mlx_adapter_default_returns_joined_text():
    """默认形态 = 整句文本；由**段**拼（丢空段、段间补空格），不取 result['text']。"""
    assert asr_mod.mlx_to_shapes(_MLX_RESULT) == "Hello world."


def test_mlx_adapter_with_times_returns_text_and_segments():
    text, segs = asr_mod.mlx_to_shapes(_MLX_RESULT, with_times=True)
    assert text == "Hello world."
    assert segs == [{"start": 0.0, "end": 1.25, "text": "Hello world."}]


def test_mlx_adapter_word_timestamps_feeds_the_streaming_consumer():
    """word_timestamps 形态必须与**流式消费者**对得上（鸭子类型而非 dict）：
    streaming.WhisperASRAdapter 读 segment.no_speech_prob / .words[].start/.end/.word
    与 .end；顺带验证 no_speech_prob>0.9 的段在 ts_words 里被丢弃。"""
    import streaming
    segs = asr_mod.mlx_to_shapes(_MLX_RESULT, word_timestamps=True)
    assert [s.text for s in segs][0] == " Hello world."
    assert [w.word for w in segs[0].words] == [" Hello", " world."]
    ad = streaming.WhisperASRAdapter.__new__(streaming.WhisperASRAdapter)
    assert ad.ts_words(segs) == [(0.0, 0.5, " Hello"), (0.5, 1.25, " world.")]
    assert ad.segments_end_ts(segs) == [1.25, 1.4]


def test_mlx_adapter_never_raises_on_bad_shapes():
    """坏数据/空结果一律降级为空结果——形状问题绝不能变成字幕主链路的异常。"""
    assert asr_mod.mlx_to_shapes({}) == ""
    assert asr_mod.mlx_to_shapes(None, with_times=True) == ("", [])
    assert asr_mod.mlx_to_shapes({"segments": ["junk", None]}) == ""
    assert asr_mod.mlx_to_shapes(
        {"segments": [{"text": " x", "words": ["junk", None]}]}) == "x"


def test_mlx_transcribe_drops_unsupported_params(monkeypatch):
    """上游事实差异：mlx_whisper **没有** vad_filter、beam search 也未实现——
    _transcribe_mlx 必须把两者都挡在自己这一层（否则 TypeError / NotImplementedError）。"""
    seen: dict = {}

    fake = types.ModuleType("mlx_whisper")

    def transcribe(audio, **kw):
        seen.update(kw)
        return _MLX_RESULT

    fake.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    w = object.__new__(asr_mod.WhisperASR)
    w.language = "en"
    w.detected_lang = None
    w.backend = "mlx"
    w.model_ref = "mlx-community/whisper-small-mlx"
    out = w.transcribe(np.zeros(16000, dtype=np.float32), initial_prompt="p",
                       beam_size=5, vad_filter=True, word_timestamps=False)
    assert out == "Hello world."
    assert "vad_filter" not in seen and "beam_size" not in seen
    assert seen["path_or_hf_repo"] == "mlx-community/whisper-small-mlx"
    assert seen["condition_on_previous_text"] is False
    assert seen["word_timestamps"] is False


def test_mlx_transcribe_adopts_detected_language_only_on_long_segments(monkeypatch):
    """auto + 长段（>3s）→ 采纳 result['language']；短段不采纳（与 ct2 口径一致）。"""
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda audio, **kw: {"text": "", "segments": [],
                                           "language": "fr"}
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    w = object.__new__(asr_mod.WhisperASR)
    w.language = "auto"
    w.detected_lang = None
    w.backend = "mlx"
    w.model_ref = "mlx-community/whisper-small-mlx"
    w.transcribe(np.zeros(16000, dtype=np.float32))            # 1s 短段
    assert w.detected_lang is None
    w.transcribe(np.zeros(16000 * 4, dtype=np.float32))        # 4s 长段
    assert w.detected_lang == "fr"


def test_mlx_model_without_weights_falls_back_to_cpu(monkeypatch):
    """没有对应 MLX 权重的短名（手改 config 才会出现）→ 回落 CPU，不拿错仓库冒充。"""
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.ModuleType("mlx_whisper"))
    monkeypatch.setattr(asr_mod.sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")
    fake_model = SimpleNamespace(transcribe=lambda *a, **k: None)
    monkeypatch.setattr(asr_mod, "WhisperModel", lambda *a, **k: fake_model)
    w = asr_mod.WhisperASR(model_size="not-a-tier", device="mlx")
    assert w.device == "cpu" and w.backend == "ctranslate2"


# ---------------- ③ models：后端双表 ----------------

def test_mlx_repo_table_matches_the_tier_names():
    """MLX 表与 ct2 表**同一批短名**（档位一一对应），且都指向 mlx-community。"""
    assert set(models_mod.MLX_MODEL_REPOS) == set(models_mod.MODEL_REPOS)
    assert set(models_mod.PLANNED_MODELS) == set(models_mod.MLX_MODEL_REPOS)
    for name, repo in models_mod.MLX_MODEL_REPOS.items():
        assert repo.startswith("mlx-community/"), repo
        assert "whisper" in repo, repo
    # 上游命名不一致的已知坑：turbo 仓**不带** -mlx 后缀（写错就 404）
    assert models_mod.MLX_MODEL_REPOS["large-v3-turbo"] == \
        "mlx-community/whisper-large-v3-turbo"


def test_backend_switches_repo_entry_dir_and_required_file(tmp_path):
    """后端一换：仓库 id、缓存条目目录、「模型可用」判据三者一起换；默认口径不变。"""
    assert models_mod.repos_for("ctranslate2")["tiny"].startswith("Systran/")
    assert models_mod.repos_for("mlx")["tiny"] == "mlx-community/whisper-tiny-mlx"
    assert models_mod.repos_for(None) is models_mod.MODEL_REPOS       # 缺省 = ct2
    assert models_mod.repos_for("bogus") is models_mod.MODEL_REPOS    # 未知 = ct2

    ct2_entry = models_mod.entry_dir(tmp_path, "tiny")
    mlx_entry = models_mod.entry_dir(tmp_path, "tiny", "mlx")
    assert ct2_entry.name == "models--Systran--faster-whisper-tiny"
    assert mlx_entry.name == "models--mlx-community--whisper-tiny-mlx"
    assert ct2_entry != mlx_entry

    # 完整性判据：ct2 要 model.bin，mlx 要 weights.safetensors（老仓还有 weights.npz）
    snap = mlx_entry / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert models_mod.is_cached(mlx_entry, "mlx") is False, "只有 model.bin ≠ MLX 可用"
    (snap / "weights.safetensors").write_bytes(b"x")
    assert models_mod.is_cached(mlx_entry, "mlx") is True
    assert models_mod.snapshot_dir(mlx_entry, "mlx") == snap
    assert models_mod.required_files("ctranslate2") == ("model.bin",)


def test_model_info_reports_backend_repo(tmp_path):
    info = models_mod.model_info(tmp_path, "small", None, "mlx")
    assert info["repo"] == "mlx-community/whisper-small-mlx"
    assert info["cached"] is False and info["size"] == 0


def test_main_resolve_model_ref_is_backend_aware(monkeypatch, tmp_path):
    """mlx 后端：跳过随包目录（那是 ct2 权重），只认 MLX 缓存/仓库 id。"""
    monkeypatch.setattr(main.models_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "bundled_model_path", lambda name: tmp_path / "bundled")
    assert main.resolve_model_ref("small") == (str(tmp_path / "bundled"), "bundled")
    assert main.resolve_model_ref("small", "mlx") == (None, "hub")
    snap = tmp_path / "models--mlx-community--whisper-small-mlx" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "weights.safetensors").write_bytes(b"x")
    assert main.resolve_model_ref("small", "mlx") == (str(snap), "cache")


# ---------------- ④ exporters：字体平台链 ----------------

def test_windows_font_candidates_are_the_msyh_pair():
    """Windows 口径逐字不变：常规候选就是 FONT_REGULAR（读模块属性，可打桩）。"""
    if not IS_WIN:
        pytest.skip("Windows 侧字体口径只在 Windows 上验")
    assert exp_mod.regular_font_candidates() == (exp_mod.FONT_REGULAR,)
    assert exp_mod.DOCX_EA_FONT == "Microsoft YaHei"


def test_mac_font_chain_prefers_pingfang_then_hiragino(monkeypatch, tmp_path):
    """macOS：候选按序取**存在**的第一个（PingFang → Hiragino → 目录扫描）。"""
    monkeypatch.setattr(exp_mod.os, "name", "posix")
    ping = tmp_path / "PingFang.ttc"
    ping.write_bytes(b"x")
    monkeypatch.setattr(exp_mod, "MAC_FONT_CANDIDATES",
                        (str(ping), str(tmp_path / "Hiragino Sans GB.ttc")))
    assert exp_mod.regular_font_candidates() == (str(ping),)


def test_mac_font_scan_falls_back_to_system_dirs(monkeypatch, tmp_path):
    """标准路径都不在（macOS 15+ 的常见情况）→ 扫系统字体目录里的中文字体名关键字。"""
    monkeypatch.setattr(exp_mod.os, "name", "posix")
    d = tmp_path / "Fonts"
    d.mkdir()
    (d / "Songti.ttc").write_bytes(b"x")
    (d / "Helvetica.ttc").write_bytes(b"x")
    monkeypatch.setattr(exp_mod, "MAC_FONT_CANDIDATES",
                        (str(tmp_path / "nope1.ttc"), str(tmp_path / "nope2.ttc")))
    monkeypatch.setattr(exp_mod, "MAC_FONT_DIRS", (str(d),))
    assert exp_mod.regular_font_candidates() == (str(d / "Songti.ttc"),)


def test_pdf_font_error_on_mac_names_the_paths_and_guidance(monkeypatch, tmp_path):
    """macOS 全找不到 → RuntimeError 带 macOS 专属指引（Windows 文案原样保留）。"""
    monkeypatch.setattr(exp_mod.os, "name", "posix")
    monkeypatch.setattr(exp_mod, "MAC_FONT_CANDIDATES",
                        (str(tmp_path / "nope1.ttc"), str(tmp_path / "nope2.ttc")))
    monkeypatch.setattr(exp_mod, "MAC_FONT_DIRS", (str(tmp_path / "empty"),))

    class _Pdf:
        pass

    with pytest.raises(RuntimeError) as ei:
        exp_mod._register_pdf_fonts(_Pdf())
    msg = str(ei.value)
    assert "字体缺失" in msg and "macOS" in msg
    assert str(tmp_path / "nope1.ttc") in msg      # 把找过的路径告诉用户
    assert "exporters.FONT_REGULAR" in msg        # 可操作出口


def test_pdf_font_missing_on_windows_keeps_the_old_copy(monkeypatch):
    """Windows 的报错文案逐字未变（含「微软雅黑」与被打桩的实际路径）。"""
    if not IS_WIN:
        pytest.skip("Windows 文案只在 Windows 上验")
    monkeypatch.setattr(exp_mod, "FONT_REGULAR", "C:/definitely/not/a/font.ttf")

    class _Pdf:
        pass

    with pytest.raises(RuntimeError) as ei:
        exp_mod._register_pdf_fonts(_Pdf())
    msg = str(ei.value)
    assert "字体缺失" in msg and "微软雅黑" in msg
    assert "C:/definitely/not/a/font.ttf" in msg


def test_pdf_real_export_still_works_on_windows():
    """真生成一份 PDF（真库真字体）：平台分支不得影响 Windows 既有产出。"""
    pairs = [{"ts": "00:00:01", "en": "Hello", "zh": "你好"}]
    data = exp_mod.build_pdf(pairs, {"title": "t", "subtitle": "s"})
    assert isinstance(data, bytes) and data[:4] == b"%PDF"
    docx = exp_mod.build_docx(pairs, {"title": "t", "subtitle": "s"})
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        xml = z.read("word/document.xml")
    assert exp_mod.DOCX_EA_FONT.encode() in xml


# ---------------- ⑤ tiers：MLX 只改措辞，不改阈值 ----------------

def test_mlx_ready_only_changes_reason_wording():
    """Apple + ≥16GB 仍推「高」（阈值不变）；理由按后端实际说。"""
    base = {"gpu": None, "vram_gb": None, "ram_gb": 16.0, "cpu": 10, "apple": True}
    r_cpu = tiers_mod.recommend({**base, "mlx": False})
    r_mlx = tiers_mod.recommend({**base, "mlx": True})
    assert r_cpu["tier"]["id"] == r_mlx["tier"]["id"] == "high"
    assert "按 CPU 推理口径" in r_cpu["reason"]
    assert "MLX 加速已就绪" in r_mlx["reason"]
    # 老画像（没有 mlx 键，如外部调用方手搓的 dict）不得炸
    assert tiers_mod.recommend(base)["tier"]["id"] == "high"


def test_mlx_wording_on_smaller_apple_machines():
    hw = {"gpu": None, "vram_gb": None, "ram_gb": 8.0, "cpu": 8, "apple": True}
    assert "MLX 加速已就绪" in tiers_mod.recommend({**hw, "mlx": True})["reason"]
    assert "按 CPU 推理口径" in tiers_mod.recommend({**hw, "mlx": False})["reason"]


# ---------------- ⑥ 前端静态断言 ----------------

def test_index_html_device_badge_knows_mlx():
    """设备徽章按后端实际显示（cuda=GPU / mlx=MLX / 其余=CPU），两处同口径。"""
    assert "status_.device === 'mlx' ? 'MLX' : 'CPU'" in INDEX_HTML
    assert "s.device === 'mlx' ? 'MLX' : 'CPU'" in INDEX_HTML


def test_index_html_error_map_has_mlx_entry():
    """ERROR_MAP 里 mlx 有独立条目（Mac 用户不该只看到英文兜底原文）。"""
    assert '"mlx"' in INDEX_HTML and "MLX 加速初始化失败" in INDEX_HTML
