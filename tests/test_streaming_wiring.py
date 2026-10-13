# -*- coding: utf-8 -*-
"""tests/test_streaming_wiring.py — main.asr_loop 流式装配线单测（条目 11）。

验证 streaming.enabled 开关两条装配路径：
- true：stream_q 建队列并传给 MicSegmenter（扇出生效）、DecodeScheduler 启动
  并 attach stream_q、status['streaming']=True；
- false（默认）：stream_q 为 None、不建调度器——v0.2 行为零变化。
重型依赖全部打桩（MicSegmenter / build_asr / status_loop / load_api_key）。"""
import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

import main


class FakeSegmenter:
    instances: list["FakeSegmenter"] = []

    # **kw：p2-5 起 apply_segmenter 还会传断句参数（真 MicSegmenter 收这三个）
    def __init__(self, q_out, device=None, vad="silero", stream_q=None, **kw):
        self.q_out = q_out
        self.vad = vad
        self.stream_q = stream_q
        self.seg_kw = kw
        self.error = None
        self.thread = None
        self.last_feed_ts = 0.0
        FakeSegmenter.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {}


class FakeScheduler:
    instances: list["FakeScheduler"] = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.attached = None
        self.stopped = False
        FakeScheduler.instances.append(self)

    def start(self):
        self.started = True

    def attach_stream_q(self, q):
        self.attached = q

    def stop(self):
        self.stopped = True


@pytest.fixture
def stubbed(monkeypatch):
    FakeSegmenter.instances = []
    FakeScheduler.instances = []
    monkeypatch.setattr(main, "MicSegmenter", FakeSegmenter)
    monkeypatch.setattr(main, "DecodeScheduler", FakeScheduler)
    monkeypatch.setattr(main, "ARGS", SimpleNamespace(device=None))  # apply_segmenter 读 ARGS.device

    # build_asr 成功契约：置 ready（真实 build_asr 的行为由其单测覆盖）
    def _build(overrides=None):
        main.status["asr"] = "ready"
    monkeypatch.setattr(main, "build_asr", _build)

    async def _noop_status_loop():
        return

    monkeypatch.setattr(main, "status_loop", _noop_status_loop)
    monkeypatch.setattr(main, "load_api_key", lambda *a, **k: "")
    # 档位化（2026-10-13）：本文件只测流式装配，不关心自动选档——按「用户已选过」
    # 处理，跳过硬件检测子进程（否则 nvidia-smi 的耗时会把下面的取消时序拖出窗口）。
    monkeypatch.setattr(main, "user_picked_model", lambda: True)
    return SimpleNamespace(segments=FakeSegmenter.instances, schedulers=FakeScheduler.instances)


def _write_cfg(enabled: bool) -> None:
    main.CONFIG_PATH.write_text(json.dumps({"streaming": enabled}), encoding="utf-8")


def _args() -> SimpleNamespace:
    return SimpleNamespace(model=None, gpu=False, compute_type=None, vad=None,
                           streaming=False, lang=None, api_key="", glossary=[],
                           source=None, device=None)  # 条目14/15：CLI 覆盖占位（None=读 config）


async def _cancel_after_setup(task: asyncio.Task) -> None:
    """装配完成后让主循环停在暂停分支再取消（不卡执行器线程，同既有测试模式）。"""
    for _ in range(150):
        if main.runtime["scheduler"] is not None or main.status["asr"] == "ready":
            break
        await asyncio.sleep(0.02)
    main.status["paused"] = True
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_asr_loop_wires_streaming_when_enabled(stubbed):
    _write_cfg(True)
    main.hub.transcript_path = None
    task = asyncio.create_task(main.asr_loop(_args()))
    try:
        await _cancel_after_setup(task)
    finally:
        sched = main.runtime["scheduler"]
        if isinstance(sched, FakeScheduler):
            sched.stop()
        main.hub.transcript_path = None
        main.status["paused"] = False

    assert main.status["streaming"] is True
    assert main.runtime["stream_q"] is not None, "streaming 开启必须建扇出队列"
    assert len(stubbed.segments) == 1
    assert stubbed.segments[0].stream_q is main.runtime["stream_q"], \
        "MicSegmenter 必须接到扇出队列（条目11 音频出口）"
    assert len(stubbed.schedulers) == 1
    sch = stubbed.schedulers[0]
    assert sch.started and sch.attached is main.runtime["stream_q"]
    assert sch.kw["asr_provider"]() is main.runtime["asr"]  # provider 实时读 runtime（快照语义）
    assert callable(sch.kw["on_final"]) and callable(sch.kw["emit_partial"])
    assert sch.kw["base_prompt"]() == (main.runtime["prompt"] or "")
    assert main.runtime["scheduler"] is sch


async def test_asr_loop_without_streaming_is_zero_change(stubbed):
    _write_cfg(False)
    main.hub.transcript_path = None
    task = asyncio.create_task(main.asr_loop(_args()))
    try:
        for _ in range(150):
            if main.status["asr"] == "ready":
                break
            await asyncio.sleep(0.02)
        main.status["paused"] = True
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        main.hub.transcript_path = None
        main.status["paused"] = False

    assert main.status["streaming"] is False
    assert main.runtime["stream_q"] is None
    assert main.runtime["scheduler"] is None
    assert stubbed.schedulers == [], "streaming 关闭不得创建调度器"
    assert len(stubbed.segments) == 1
    assert stubbed.segments[0].stream_q is None, "扇出保持关闭（默认 None 零变化）"


# ---------------- payload 构造（条目12：audio_ts 贯穿 + partial 协议形状） ----------------

def test_final_payload_audio_ts_and_v02_compat():
    """final payload：v0.2 整句键集合（ts/en/zh/seg——保护清单第 2 条）不变，
    audio_ts 为纯增量（采集墙钟秒）；整句与流式两条路径共用同一构造函数。"""
    p = main.final_payload("12:00:00", "hello world", 3, 1798123456.5)
    assert p == {"ts": "12:00:00", "en": "hello world", "zh": "", "seg": 3,
                 "audio_ts": 1798123456.5}
    assert set(p) == {"ts", "en", "zh", "seg", "audio_ts"}
    # meta 透传缺失（理论兜底）时 audio_ts=None：前端 typeof number 检查跳过更新
    assert main.final_payload("t", "x", 1, None)["audio_ts"] is None


def test_partial_payload_shape():
    """partial 消息形状 {type,key,text,ts}：type 供前端分派，key 原位替换定位，
    ts 为广播墙钟（仅显示）；不含 en/zh/seg——不会误触发前端 final 分支。"""
    from scheduler import partial_payload
    p = partial_payload(7, "hello world")
    assert p["type"] == "partial" and p["key"] == 7 and p["text"] == "hello world"
    assert p["ts"]  # HH:MM:SS 广播墙钟
    assert set(p) == {"type", "key", "text", "ts"}


def test_adapter_prompt_base_truncated_context_tail_preserved():
    """评审修复：术语表 base_prompt 先截断（560），最近已提交上下文（init_prompt
    200 字符，防专名改口最有效的部分）必须完整保留在尾部——原实现整体 [:800]
    尾部截断，术语表接近上限时最近上下文被切掉或切半词。"""
    from streaming import WhisperASRAdapter

    captured = {}

    class CapASR:
        def transcribe(self, audio, initial_prompt=None, **kw):
            captured["prompt"] = initial_prompt
            return []

    ad = WhisperASRAdapter(CapASR(), base_prompt="G" * 700)   # 超过 base 上限 560
    ad.transcribe(np.zeros(1600, dtype=np.float32), init_prompt="C" * 200)
    p = captured["prompt"]
    assert p.startswith("G" * 560), "base_prompt 应先被截到 560"
    assert p.endswith("C" * 200), "最近上下文必须完整保留在尾部"
    assert len(p) <= WhisperASRAdapter.PROMPT_TOTAL_MAX_CHARS

    ad2 = WhisperASRAdapter(CapASR(), base_prompt="short glossary")
    ad2.transcribe(np.zeros(1600, dtype=np.float32), init_prompt="committed ctx")
    assert captured["prompt"] == "short glossary committed ctx"  # 短 base 原样拼接

    ad3 = WhisperASRAdapter(CapASR(), base_prompt="")            # 无术语表：仅上下文
    ad3.transcribe(np.zeros(1600, dtype=np.float32), init_prompt="ctx only")
    assert captured["prompt"] == "ctx only"
