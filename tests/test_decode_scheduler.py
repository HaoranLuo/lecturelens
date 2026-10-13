# -*- coding: utf-8 -*-
"""tests/test_decode_scheduler.py — 流式解码调度离线单测（升级计划条目 11）。

验收口径（§2.2-11）：fake 实例验证 final 先于 partial（PriorityQueue 定稿优先）、
实例快照交接（热切换后旧实例任务排空、流式层重置）。多数用例不开线程、以
DecodeScheduler.step() 同步驱动保证确定性；另有一条真实 worker 线程冒烟。
同步断言的调度语义：final prio=0 / partial prio=1（同优先级 seq FIFO）、
过期 partial（final 入队前的 epoch）丢弃、partial 消息形状、暂停语义。"""
import threading
import time

import numpy as np

from scheduler import DecodeScheduler

SR = 16000


def audio_s(sec: float) -> np.ndarray:
    return np.zeros(int(sec * SR), dtype=np.float32)


class FakeWord:
    def __init__(self, s, e, w):
        self.start, self.end, self.word = s, e, w


class FakeSeg:
    def __init__(self, words):
        self.words = words
        self.no_speech_prob = 0.0
        self.end = words[-1].end if words else 0.0


class FakeASR:
    """duck-typed WhisperASR：整句路径返回文本；词级路径（流式 adapter 调用）
    返回带词级时间戳的 segments。calls 记录每次调用的参数供断言。"""

    def __init__(self, name="a", device="cpu"):
        self.name = name
        self.device = device
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def transcribe(self, audio, initial_prompt=None, **kw):
        rec = {"word": bool(kw.get("word_timestamps")), "prompt": initial_prompt,
               "beam": kw.get("beam_size"), "vad": kw.get("vad_filter")}
        with self.lock:
            self.calls.append(rec)
        if kw.get("word_timestamps"):
            return [FakeSeg([FakeWord(0.0, 0.5, " hello"), FakeWord(0.5, 1.0, " world")]),
                    FakeSeg([FakeWord(1.0, 1.5, " again")])]
        return f"text:{self.name}"


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.finals: list[str] = []
        self.partials: list[dict] = []
        self.order: list[tuple] = []
        self.last_meta: dict | None = None

    def on_final(self, en, meta):
        with self.lock:
            self.finals.append(en)
            self.order.append(("final", en))
            self.last_meta = dict(meta)

    def emit_partial(self, payload):
        with self.lock:
            self.partials.append(payload)
            self.order.append(("partial", payload["text"]))


def make_scheduler(asr_provider, **kw) -> tuple[DecodeScheduler, Recorder]:
    rec = Recorder()
    s = DecodeScheduler(asr_provider=asr_provider, on_final=rec.on_final,
                        emit_partial=rec.emit_partial, **kw)
    return s, rec


def drain(s: DecodeScheduler, tries: int = 200) -> None:
    """同步清空队列（不开线程）：step 返回 False 即队列空。"""
    for _ in range(tries):
        if not s.step(timeout=0.0):
            return


# ---------------- 定稿优先（PriorityQueue prio 语义） ----------------

def test_final_processed_before_queued_partials():
    """final 入队虽晚于两个 partial，但 prio=0 必须先于一切积压 partial 解码；
    早于 final 入队的 partial（过期 epoch）丢弃，final 后入队的继续预览。
    ghost_suppress_s=0 关闭幽灵抑制窗——本用例专测 epoch/优先级语义。"""
    fake = FakeASR("a")
    s, rec = make_scheduler(lambda: fake, ghost_suppress_s=0)
    s.enqueue_partial(fake, audio_s(1.0))   # epoch 0
    s.enqueue_partial(fake, audio_s(1.0))   # epoch 0
    s.enqueue_final(fake, audio_s(2.0))     # epoch → 1
    s.enqueue_partial(fake, audio_s(1.0))   # epoch 1（final 之后，仍有效）
    s.enqueue_partial(fake, audio_s(1.0))   # epoch 1

    drain(s)

    assert rec.order[0] == ("final", "text:a"), f"定稿必须最先处理: {rec.order}"
    assert rec.finals == ["text:a"]
    # 过期 partial 2 个被丢（不解码）；有效 partial 第 1 次无确认文本、
    # 第 2 次提交两次假设的公共前缀（hello world again 全部一致）→ 恰好 1 条广播
    assert [t for k, t in rec.order if k == "partial"] == ["hello world again"]
    word_calls = [c for c in fake.calls if c["word"]]
    assert len(word_calls) == 2, f"过期 partial 不得解码: {fake.calls}"
    assert [c for c in fake.calls if not c["word"]] == [
        {"word": False, "prompt": None, "beam": None, "vad": None}]  # 整句路径默认参数


def test_final_empty_text_still_callbacks_and_meta_passthrough():
    """空转写结果也必须回调（asr_loop 在等它，静默跳过会挂死主循环）；
    入队 meta（如采集时间戳）原样透传，decode_s 由 worker 补充。"""
    class SilentASR(FakeASR):
        def transcribe(self, audio, initial_prompt=None, **kw):
            out = super().transcribe(audio, initial_prompt, **kw)
            return "" if not kw.get("word_timestamps") else out

    fake = SilentASR("a")
    s, rec = make_scheduler(lambda: fake)
    s.enqueue_final(fake, audio_s(1.0), ts_audio=123.5)
    drain(s)
    assert rec.finals == [""]
    assert rec.last_meta["ts_audio"] == 123.5
    assert isinstance(rec.last_meta["decode_s"], float)


# ---------------- 实例快照交接（热切换） ----------------

def test_hot_swap_uses_enqueue_snapshot_and_resets_stream_state():
    """partial/final 任务携带入队时 asr 快照：模型热切换后，旧实例任务照常
    用旧实例解码排空；partial 处理发现绑定实例变化 → 流式层重置绑新实例。"""
    fake_a, fake_b = FakeASR("a"), FakeASR("b")
    holder = {"asr": fake_a}
    s, rec = make_scheduler(lambda: holder["asr"], ghost_suppress_s=0)

    s.enqueue_partial(fake_a, audio_s(1.0))   # 切换前入队（快照 = a）
    holder["asr"] = fake_b                    # 模拟 build_asr 原子替换 runtime['asr']
    s.enqueue_partial(fake_b, audio_s(1.0))   # 切换后入队（快照 = b）
    drain(s)

    assert [c["word"] for c in fake_a.calls] == [True]   # a 的 partial 用 a 排空
    assert [c["word"] for c in fake_b.calls] == [True]   # b 触发流式层重建后解码
    assert s._online_asr is fake_b, "流式层必须重绑新实例（HypothesisBuffer/prompt 重置）"
    assert rec.partials == []  # 各自都是首次假设，尚无确认文本

    # 旧实例 final 在切换后排空：仍由入队快照实例 a 整句解码（非词级参数）
    s.enqueue_final(fake_a, audio_s(2.0))
    drain(s)
    assert rec.finals == ["text:a"]
    whole = [c for c in fake_a.calls if not c["word"]]
    assert whole == [{"word": False, "prompt": None, "beam": None, "vad": None}]

    # 切换后新 partial：绑定 b、epoch 为 final 后新值 → 正常预览
    s.enqueue_partial(fake_b, audio_s(1.0))
    s.enqueue_partial(fake_b, audio_s(1.0))
    drain(s)
    assert [p["text"] for p in rec.partials] == ["hello world again"]
    assert s._online_asr is fake_b


# ---------------- partial 消息形状与流式解码参数 ----------------

def test_partial_payload_shape_and_streaming_decode_params():
    """partial 消息 {type:'partial', key, text, ts}；流式解码参数 =
    beam_size=5 / vad_filter=False / word_timestamps=True（条目 11 设计）。"""
    fake = FakeASR("a")
    s, rec = make_scheduler(lambda: fake, base_prompt=lambda: "GLOSSARY TERMS")
    s.enqueue_partial(fake, audio_s(1.0))
    s.enqueue_partial(fake, audio_s(1.0))
    drain(s)

    assert len(rec.partials) == 1
    p = rec.partials[0]
    assert p["type"] == "partial"
    assert p["text"] == "hello world again"  # 两次一致假设的公共前缀全量提交
    assert p["key"] == s._key and p["ts"]  # key 与当前流式层代次一致，供前端原位替换
    c = fake.calls[0]
    assert (c["beam"], c["vad"], c["word"]) == (5, False, True)
    assert c["prompt"] == "GLOSSARY TERMS"  # 术语表 initial_prompt 持续注入


# ---------------- feeder 攒块节奏 / 暂停与无模型 ----------------

def test_feed_block_accumulates_to_min_chunk_gpu_cpu():
    """feeder 攒满 min_chunk 才派 partial 任务：GPU 1.0s、CPU 1.5s（条目 11）。"""
    fake_gpu, fake_cpu = FakeASR("g", device="cuda"), FakeASR("c", device="cpu")
    s, rec = make_scheduler(lambda: fake_gpu)

    s._reset_online(fake_gpu)
    assert [s.feed_block(audio_s(0.4)) for _ in range(2)] == [False, False]
    assert s.feed_block(audio_s(0.3)) is True            # 累计 1.1s ≥ 1.0（GPU）
    assert s.pq.qsize() == 1

    s._reset_online(fake_cpu)                             # 切到 CPU 实例 → 1.5s 档
    assert [s.feed_block(audio_s(0.5)) for _ in range(3)] == [False, False, True]
    assert s.pq.qsize() == 2


def test_feed_block_drops_when_paused_or_no_asr():
    """暂停不回放、无模型不积压：两种情况丢弃累积（含已累积部分）。"""
    holder = {"asr": FakeASR("a")}
    paused = [False]
    s, rec = make_scheduler(lambda: holder["asr"], paused=lambda: paused[0])

    holder["asr"] = None
    assert s.feed_block(audio_s(1.0)) is False
    holder["asr"] = holder["asr"] or FakeASR("back")
    paused[0] = True
    assert s.feed_block(audio_s(1.0)) is False
    assert s.pq.qsize() == 0 and not rec.partials

    paused[0] = False
    assert s.feed_block(audio_s(0.6)) is False            # 暂停期间累积已清空，重算
    assert s.feed_block(audio_s(0.5)) is True             # 0.6+0.5 ≥ 1.0


def test_partial_skipped_when_paused_at_decode_time():
    """暂停时已入队的 partial 在解码时刻被跳过（不解码不广播）；恢复后继续。"""
    fake = FakeASR("a")
    paused = [False]
    s, rec = make_scheduler(lambda: fake, paused=lambda: paused[0])

    s.enqueue_partial(fake, audio_s(1.0))
    paused[0] = True
    drain(s)
    assert not rec.partials and not fake.calls, "暂停期间不得解码 partial"

    paused[0] = False
    s.enqueue_partial(fake, audio_s(1.0))
    s.enqueue_partial(fake, audio_s(1.0))
    drain(s)
    assert [p["text"] for p in rec.partials] == ["hello world again"]


# ---------------- 真实 worker 线程冒烟 ----------------

def test_worker_thread_delivers_final_and_stops():
    fake = FakeASR("a")
    s, rec = make_scheduler(lambda: fake)
    s.start()
    try:
        s.enqueue_final(fake, audio_s(1.0))
        deadline = time.time() + 5
        while not rec.finals and time.time() < deadline:
            time.sleep(0.02)
        assert rec.finals == ["text:a"], "worker 线程必须在超时前交付 final"
    finally:
        s.stop()
    assert not s.worker_thread.is_alive(), "stop() 后 worker 线程应退出"


# ---------------- 评审修复：异常回调契约 / 术语表对称 / 幽灵抑制 / epoch 顺序 ----------------

def test_final_transcribe_error_still_callbacks_and_recovers():
    """final 转写异常必须在 _handle_final 内就地捕获并回调 on_final（en='' +
    meta['error']）——step() 外层 catch 只记日志，若不回调，main 流式分支的
    await final_ready_q.get() 永久挂死（评审复现：Recorder.finals 为空）。"""
    class BoomASR(FakeASR):
        def __init__(self, name="a"):
            super().__init__(name)
            self.boom = True

        def transcribe(self, audio, initial_prompt=None, **kw):
            if self.boom and not kw.get("word_timestamps"):
                raise RuntimeError("cuda OOM simulated")
            return super().transcribe(audio, initial_prompt, **kw)

    fake = BoomASR("a")
    s, rec = make_scheduler(lambda: fake, ghost_suppress_s=0)
    s.enqueue_final(fake, audio_s(1.0), ts_audio=7.0)
    drain(s)
    assert rec.finals == [""], "异常 final 也必须回调（防主循环挂死）"
    assert "RuntimeError" in rec.last_meta.get("error", "")
    assert rec.last_meta["ts_audio"] == 7.0
    fake.boom = False                      # 异常后主循环可继续：下一 final 照常交付
    s.enqueue_final(fake, audio_s(1.0))
    drain(s)
    assert rec.finals == ["", "text:a"]


def test_final_injects_base_prompt_symmetric_with_nonstreaming():
    """流式 final 必须与 main.py 非流式路径一样注入术语表 initial_prompt
    （经 base_prompt 通道）——原裸调 transcribe(audio) 使 final 失去术语表加成。"""
    fake = FakeASR("a")
    s, rec = make_scheduler(lambda: fake, base_prompt=lambda: "GLOSSARY",
                            ghost_suppress_s=0)
    s.enqueue_final(fake, audio_s(1.0))
    drain(s)
    whole = [c for c in fake.calls if not c["word"]]
    assert whole and whole[0]["prompt"] == "GLOSSARY"
    assert rec.finals == ["text:a"]


def test_ghost_partial_suppressed_after_final():
    """final 定稿后，feeder 紧随冲刷的新 epoch partial 几乎必含已定稿段尾——
    抑制窗内 worker 丢弃、feeder 清空累积，不得重复广播定稿文本；窗口过后恢复。"""
    fake = FakeASR("a")
    s, rec = make_scheduler(lambda: fake, ghost_suppress_s=60.0)  # 长窗便于断言
    s.enqueue_final(fake, audio_s(2.0))
    s.enqueue_partial(fake, audio_s(1.0))   # final 后的新 epoch partial（幽灵）
    drain(s)
    assert rec.finals == ["text:a"]
    assert rec.partials == [], "幽灵 partial 不得重复广播已定稿文本"
    assert [c for c in fake.calls if c["word"]] == [], "抑制窗内 partial 不得解码"
    assert s.feed_block(audio_s(1.0)) is False and s._acc == [], \
        "抑制窗内 feeder 应清空累积"
    s._suppress_until = 0.0                 # 模拟窗口到期（免 sleep）
    s.enqueue_partial(fake, audio_s(1.0))
    s.enqueue_partial(fake, audio_s(1.0))
    drain(s)
    assert [p["text"] for p in rec.partials] == ["hello world again"], \
        "窗口过后 partial 预览恢复"


def test_epoch_checked_before_hot_swap_reset():
    """epoch 过期检查必须先于 asr 热切换重置：热切换后处理的旧实例过期 partial
    不得白解码已定稿音频、不得把流式层重绑回旧实例（评审复现：_key 多跳 2 次）。"""
    fake_a, fake_b = FakeASR("a"), FakeASR("b")
    holder = {"asr": fake_a}
    s, rec = make_scheduler(lambda: holder["asr"], ghost_suppress_s=0)
    s.enqueue_partial(fake_a, audio_s(1.0))   # epoch 0，ref a（切换前入队）
    holder["asr"] = fake_b                    # 热切换
    s.enqueue_final(fake_b, audio_s(2.0))     # epoch 1，ref b（新实例时代入队）
    drain(s)
    assert rec.finals == ["text:b"]
    assert [c for c in fake_a.calls if c["word"]] == [], \
        "过期 partial 不得用旧实例解码（epoch 检查先于重置）"
    assert s._online_asr is fake_b, "流式层不得被过期 partial 重绑回旧实例"
