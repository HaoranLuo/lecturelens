# -*- coding: utf-8 -*-
"""tests/test_resample.py — loopback 重采样/downmix 离线单测（升级计划条目 14）。

验收口径（§2.2-14）：「48k→16k 重采样后 8kHz 以上无混叠能量」——
对正弦扫频信号（扫过新 Nyquist）做 FFT，断言降采样后带外能量占比可忽略；
另验证 downmix (L+R)/2 与 44.1k 非整数比路径。全部纯函数、离线秒级。"""
import numpy as np
import pytest
from scipy.signal import chirp

from capture import SR
from loopback import downmix, resample_to_16k


def test_downmix_stereo_mean():
    x = np.array([[0.8, 0.2], [1.0, -1.0], [-0.6, 0.4]], dtype="float32")
    out = downmix(x)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [0.5, 0.0, -0.1], atol=1e-6)


def test_downmix_mono_passthrough():
    x = np.array([0.1, -0.2, 0.3], dtype="float32")
    assert downmix(x) is not None
    np.testing.assert_allclose(downmix(x), x)


def test_resample_identity_at_16k():
    """已是 16k 的输入原样返回（不引入重采样损耗/延迟）。"""
    rng = np.random.default_rng(42)
    x = rng.standard_normal(16_000).astype("float32") * 0.1
    y = resample_to_16k(x, SR)
    assert y is x or np.array_equal(y, x)


def test_resample_48k_length():
    x = np.zeros(48_000, dtype="float32")  # 1s @48k
    assert len(resample_to_16k(x, 48000)) == SR


def test_resample_tone_above_nyquist_vanishes():
    """无混叠的最纯判据：12kHz 纯音（> 新 Nyquist 8k）@48k → 16k 后应被抗混叠
    滤波器滤除（实测 -55dB）；naive 抽点则把它全幅镜像到 4kHz。阈值 2% 留
    21dB 余量。注：测试频点避开 16k 整数倍（48k/16k=3 的退化采样）。"""
    sr = 48000
    t = np.arange(2 * sr) / sr  # 2s
    x = (0.5 * np.sin(2 * np.pi * 12000 * t)).astype("float32")
    y = resample_to_16k(x, sr)
    n = x[::3]  # naive 对照
    ratio = float(np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(n ** 2)))
    assert ratio < 0.02, f"带外纯音残余 {ratio:.4f} ≥ 2%（抗混叠未生效）"


def test_resample_passband_tones_intact():
    """带内纯音应无损通过（RMS 比 ∈ [0.9, 1.1]）。7.9k 起进入过渡带
    （resample_poly kaiser β=5.0 短滤波器的已知特性），不在此断言范围。"""
    sr = 48000
    t = np.arange(2 * sr) / sr
    for f in (2000, 5000, 7000):
        x = (0.5 * np.sin(2 * np.pi * f * t)).astype("float32")
        y = resample_to_16k(x, sr)
        ratio = float(np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(x ** 2)))
        assert 0.9 <= ratio <= 1.1, f"{f}Hz 带内损耗: RMS 比 {ratio:.4f}"


def test_resample_sweep_no_aliasing_above_8k():
    """验收核心（§2.2-14「正弦扫频 FFT 断言」）：0.5→24kHz 线性扫频 @48k
    （能量按 Hz 均匀分布，8kHz 以上占 68%）→ 16k 后总能量应只剩带内部分：
    实测 proper/naive = 0.306（理论 0.319 = 7.5k/23.5k）；naive 抽点则 100%
    保留（混叠折回带内）。断言 < 0.45（≥55% 带外能量被滤除，判别度充足）
    且 > 0.2（防过激滤波把带内也削了）。近 Nyquist 7.9–8k 残余 ≤ 2%
    （实测 0.86%，为滤波器过渡带内真实带内内容的衰减，非混叠）。"""
    sr = 48000
    dur = 5.0
    t = np.arange(int(sr * dur)) / sr
    x = (0.5 * chirp(t, f0=500, f1=24000, t1=dur)).astype("float32")
    y = resample_to_16k(x, sr)
    n = x[::3]  # naive 对照：无抗混叠
    ratio = float(np.sum(y ** 2) / np.sum(n ** 2))
    assert 0.2 < ratio < 0.45, f"扫频能量比 {ratio:.4f}（预期 ≈0.32：只保留带内）"
    spec = np.abs(np.fft.rfft(y)) ** 2
    freqs = np.fft.rfftfreq(len(y), 1 / SR)
    near_nyq = float(spec[freqs >= 7900].sum() / spec.sum())
    assert near_nyq < 0.02, f"近 Nyquist 残余 {near_nyq:.4f} ≥ 2%"


def test_resample_44k1_non_integer_ratio():
    """非 16k 整数倍的设备率（44.1k）走 gcd 约简（up=160/down=441）不出错，
    输出长度符合 up/down 语义。"""
    sr = 44100
    x = np.zeros(sr, dtype="float32")  # 1s
    y = resample_to_16k(x, sr)
    assert abs(len(y) - SR) <= 320  # ceil(n*up/down) 的边界余量


@pytest.mark.parametrize("bad", [0, -8000])
def test_resample_invalid_rate_raises(bad):
    with pytest.raises(ValueError):
        resample_to_16k(np.zeros(16, dtype="float32"), bad)
