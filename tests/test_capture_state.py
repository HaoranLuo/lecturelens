# -*- coding: utf-8 -*-
"""tests/test_capture_state.py — 分句状态机离线单测（升级计划条目 2）。

合成流与判定阈值与 scripts/smoke_test.py [1]/[1b] 完全一致——直接 import
smoke_test 的实现复用（单一事实来源，避免两处阈值漂移）；smoke_test 继续
负责真模型/真音频路径。被测状态机：capture.MicSegmenter.handle_block
（0.4s preroll / 700ms 静音收尾 / 500ms 最短语音 / 8s 软上限溢出 / 10s 硬切；
2026-10 首期反馈② 由 15/18s 下调）。"""
import queue

import numpy as np
import pytest

from capture import BLOCK, SR, MicSegmenter
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


def test_long_speech_no_soft_cap_shards():
    """~20s 连续语音（仅 0.4s 深 dip，无 700ms 静音）：溢出后应等静音对齐
    切点或硬切兜底，不得出现**在软上限处**的恒定硬切碎片
    （v0 块数/样本数单位错配回归——upgrade-plan.md §0.3 保护清单第 1 条）。

    2026-10 首期反馈②：默认软/硬上限已下调为 8s/10s（原 15s/18s）——连续
    说话时定稿不再等近 20 秒；本测试同时守住「软上限不得拿来硬切」。"""
    durs = run_vad_case("energy", synth_long_speech())
    assert durs, "未切出任何段"
    assert max(durs) >= 8.5, f"最长段 {max(durs)}s，溢出对齐逻辑未生效"
    shards = [d for d in durs if 7.8 <= d <= 8.1]
    assert not shards, f"存在 8s 硬切碎片（软上限被当作硬切）: {shards}"


def test_request_flush_cuts_current_segment():
    """⚡断句（首期反馈②）：不等静音/上限，下一块就把当前缓冲结算成段。"""
    q = queue.Queue()
    seg = MicSegmenter(q, vad="energy")
    silence = np.zeros(BLOCK, dtype=np.float32)
    speech = (0.08 * np.sin(2 * np.pi * 220 * np.arange(BLOCK) / SR)).astype(np.float32)
    for _ in range(35):                       # 约 2.2s 校准块（EnergyVAD 需 30 块）
        seg.handle_block(silence)
    for _ in range(int(2.0 / (BLOCK / SR))):  # ~2s 连续语音（远低于 8s 软上限）
        seg.handle_block(speech)
    assert seg.speaking, "能量 VAD 未进入说话态（测试前提失败）"
    assert q.empty(), "未到上限/未静音时不应切段"
    seg.request_flush()
    seg.handle_block(speech)                  # 下一块触发结算
    assert not q.empty(), "flush 后应立即产出当前段"
    _ts, audio = q.get_nowait()
    assert len(audio) >= int(0.5 * SR), "结算出的段应包含最短语音"
