# -*- coding: utf-8 -*-
"""tests/test_capture_state.py — 分句状态机离线单测（升级计划条目 2）。

合成流与判定阈值与 scripts/smoke_test.py [1]/[1b] 完全一致——直接 import
smoke_test 的实现复用（单一事实来源，避免两处阈值漂移）；smoke_test 继续
负责真模型/真音频路径。被测状态机：capture.MicSegmenter.handle_block
（0.4s preroll / 700ms 静音收尾 / 500ms 最短语音 / 15s 软上限溢出 / 18s 硬切）。"""
import pytest

from smoke_test import run_vad_case, synth_long_speech, synth_stream


@pytest.mark.parametrize("vad_kind", ["silero", "energy"])
@pytest.mark.parametrize("with_hum", [False, True], ids=["quiet", "hum"])
def test_two_speech_bursts(vad_kind, with_hum):
    """静音1s→语音2s→静音1s→语音2s→静音1.5s：切出 1–3 段、每段 1.2–3.6s。

    叠加 120Hz 稳态嗡噪时同样成立（energy VAD 的自适应噪声底 + silero 的
    频谱鲁棒性——空调/投影仪噪声不应把整段流粘成一段）。"""
    durs = run_vad_case(vad_kind, synth_stream(with_hum))
    assert 1 <= len(durs) <= 3, f"切出段数异常: {durs}"
    assert all(1.2 <= d <= 3.6 for d in durs), f"段长异常: {durs}"


def test_long_speech_no_15s_shards():
    """~20s 连续语音（仅 0.4s 深 dip，无 700ms 静音）：溢出后应等静音对齐
    切点或 18s 硬切兜底，不得出现 15.0s 附近的恒定硬切碎片
    （v0 块数/样本数单位错配回归——upgrade-plan.md §0.3 保护清单第 1 条）。"""
    durs = run_vad_case("energy", synth_long_speech())
    assert durs, "未切出任何段"
    assert max(durs) >= 15.0, f"最长段 {max(durs)}s，溢出对齐逻辑未生效"
    shards = [d for d in durs if 14.8 <= d <= 15.1]
    assert not shards, f"存在 15s 硬切碎片: {shards}"
