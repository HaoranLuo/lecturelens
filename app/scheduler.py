# -*- coding: utf-8 -*-
"""scheduler.py — 流式解码调度（升级计划条目 11）：单解码工作线程 + PriorityQueue。

设计要点（docs/upgrade-plan.md §1 条目 11「解码调度取代裸 Lock」）：
- 任务三元组 (prio, seq, task) 进 queue.PriorityQueue：VAD final 段 prio=0、
  partial chunk prio=1（seq 单调递增保证同优先级 FIFO）——「定稿优先」由调度
  顺序表达；CTranslate2 调用因单工作线程天然串行。
- 任务携带**入队时的 asr 实例快照**（asr_ref）：build_asr 原子替换 runtime['asr']
  后，队列中旧实例任务自然排空（旧模型对象仍存活、在飞请求不受影响）；partial
  处理发现 asr_ref 与流式层绑定实例不同 → 重建 OnlineASRProcessor（Hypothesis-
  Buffer、已提交 prompt 随之重置，防跨模型 prompt 错位）。
- final 入队即递增 epoch；partial 携带入队时 epoch 快照。final 定稿会重置流式层
  （该段音频已由整句路径定稿，partial 预览不再需要）——入队早于该 final 的过期
  partial 直接丢弃；final 处理后另设幽灵抑制窗，丢弃紧随其后、混有已定稿段尾的
  新 epoch partial（epoch 检查先于 asr 热切换重置，两条路径都不可旁路）。
- partial 产出经 emit_partial 回调交 main.py（asyncio.run_coroutine_threadsafe
  进 hub 广播）；final 文本经 on_final 回调交回 asr_loop 的既有广播/笔记/翻译路径。
"""
import itertools
import logging
import queue
import threading
import time

import numpy as np

from streaming import SAMPLING_RATE, OnlineASRProcessor, WhisperASRAdapter

log = logging.getLogger("scheduler")

PRIO_FINAL = 0    # VAD 定稿段：优先于一切积压 partial（定稿先出字幕）
PRIO_PARTIAL = 1  # 流式 partial chunk：同优先级按 seq FIFO


def partial_payload(key: int, text: str) -> dict:
    """partial 广播消息（条目 11/12）：{type, key, text, ts}——不写笔记、不触发翻译；
    前端按 key 原位替换、final 覆盖。ts 为广播墙钟（仅供显示；音频时间由 final 的
    audio_ts 承担，见 main.final_payload）。纯函数供单测与前端联调口径对齐。"""
    return {"type": "partial", "key": key, "text": text,
            "ts": time.strftime("%H:%M:%S")}


class DecodeTask:
    __slots__ = ("prio", "seq", "asr_ref", "audio", "meta")

    def __init__(self, prio: int, seq: int, asr_ref, audio: np.ndarray, meta: dict):
        self.prio = prio
        self.seq = seq
        self.asr_ref = asr_ref
        self.audio = audio
        self.meta = meta

    def __repr__(self) -> str:  # 排障用；比较由 (prio, seq) 元组承担，task 本身不参与
        return f"DecodeTask(prio={self.prio}, seq={self.seq}, meta={self.meta})"


class DecodeScheduler:
    """final/partial 统一进一个 PriorityQueue；worker 线程串行解码；
    feeder 线程消费 MicSegmenter 的 stream_q 扇出，攒满 min_chunk 派 partial 任务。

    回调（均在 worker/feeder 线程执行，实现方自行线程安全）：
    - on_final(text: str, meta: dict)                —— final 整句转写完成（text 可为 ""）
    - emit_partial(payload: dict)                    —— partial 消息 {type,key,text,ts}
    - asr_provider() -> asr | None                   —— 当前 asr 实例（入队快照源）
    - paused() -> bool                               —— 暂停时 feeder 丢音频、partial 不解码
    - base_prompt() -> str                           —— 术语表 initial_prompt（持续注入）
    """

    MIN_CHUNK_GPU_S = 1.0   # GPU min-chunk 0.5–1.0s 取上限（计划条目 11）
    MIN_CHUNK_CPU_S = 1.5   # CPU 回退档 1–2s 取中值
    ACC_MAX_S = 30.0        # feeder 累积上限：无模型/暂停外的极端情况防内存膨胀

    def __init__(self, asr_provider, on_final, emit_partial,
                 paused=None, base_prompt=None, buffer_trimming=("segment", 15),
                 ghost_suppress_s: float | None = None):
        self.asr_provider = asr_provider
        self.on_final = on_final
        self.emit_partial = emit_partial
        self.paused = paused or (lambda: False)
        self.base_prompt = base_prompt or (lambda: "")
        self.buffer_trimming = buffer_trimming
        # 幽灵 partial 抑制窗（评审修复）：final 定稿后，feeder 紧接冲刷的 partial
        # 几乎必含 final 段尾部（seg 长度从不恰为 min_chunk 整数倍）且携带新 epoch——
        # 会把已定稿音频重新喂进刚重置的缓冲并重复广播。final 处理后抑制一个
        # min_chunk 周期（+0.5s 余量）：该窗口本就以静音为主，预览损失可忽略。
        # None=自动（min_chunk+0.5）；测试可注入小值。0/负值=禁用抑制。
        self.ghost_suppress_s = ghost_suppress_s
        self._suppress_until = 0.0

        self.pq: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()

        self._epoch = 0          # final 入队时 +1；partial 携带入队时快照
        self._key = 0            # partial 消息 key：流式层每次重置递增（前端原位替换定位）
        self._online: OnlineASRProcessor | None = None
        self._online_asr = None  # 流式层当前绑定的 asr 实例（热切换检测基准）
        self._min_chunk_s = self.MIN_CHUNK_GPU_S

        self._acc: list[np.ndarray] = []  # feeder 侧累积（攒满 min_chunk 才派任务）
        self.worker_thread: threading.Thread | None = None
        self.feeder_thread: threading.Thread | None = None
        self.stream_q: queue.Queue | None = None

    # ---------- 入队（事件循环/feeder 线程调用） ----------

    def enqueue_final(self, asr_ref, audio: np.ndarray, **meta) -> None:
        """VAD 定稿段。epoch 在入队时递增：此后入队的 partial 带新 epoch，
        定稿重置流式层后仍可继续预览；早于此 final 的 partial 全部判过期丢弃。"""
        self._epoch += 1
        meta["epoch"] = self._epoch
        seq = next(self._seq)
        self.pq.put((PRIO_FINAL, seq, DecodeTask(PRIO_FINAL, seq, asr_ref, audio, meta)))

    def enqueue_partial(self, asr_ref, audio: np.ndarray) -> None:
        seq = next(self._seq)
        self.pq.put((PRIO_PARTIAL, seq,
                     DecodeTask(PRIO_PARTIAL, seq, asr_ref, audio,
                                {"epoch": self._epoch})))

    # ---------- feeder：stream_q 扇出 → 攒块 → partial 任务 ----------

    def attach_stream_q(self, q: queue.Queue) -> None:
        self.stream_q = q
        self.feeder_thread = threading.Thread(target=self._feeder, daemon=True,
                                              name="stream-feeder")
        self.feeder_thread.start()

    def _feeder(self) -> None:
        while not self._stop.is_set():
            try:
                block = self.stream_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.feed_block(block)
            except Exception as e:
                log.warning(f"[scheduler] ⚠ feeder 喂块失败: {e}", exc_info=True)

    def feed_block(self, block: np.ndarray | None) -> bool:
        """累积一个原始音频块；攒满 min_chunk 派一个 partial 任务（返回是否入队）。
        暂停/幽灵抑制/无模型时清空累积（暂停不回放；抑制窗内的音频含已定稿段尾，
        丢弃后从新音频起算，不进 online 缓冲）。测试可直接调用。"""
        if block is None:
            return False
        if self.paused() or self._ghost_suppressed() or self.asr_provider() is None:
            self._acc.clear()
            return False
        self._acc.append(np.asarray(block, dtype=np.float32))
        total = sum(len(b) for b in self._acc)
        if total >= self.ACC_MAX_S * SAMPLING_RATE:
            self._acc.clear()  # 极端兜底：溢出整段丢弃，宁可丢预览不可涨内存
            return False
        if total >= self._min_chunk_s * SAMPLING_RATE:
            audio = np.concatenate(self._acc)
            self._acc.clear()
            self.enqueue_partial(self.asr_provider(), audio)
            return True
        return False

    # ---------- worker：单解码线程（CTranslate2 调用天然串行） ----------

    def start(self) -> None:
        asr = self.asr_provider()
        if asr is not None:
            self._min_chunk_s = self._min_chunk_for(asr)
        self.worker_thread = threading.Thread(target=self._worker, daemon=True,
                                              name="decode-worker")
        self.worker_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for t in (self.worker_thread, self.feeder_thread):
            if t is not None:
                t.join(timeout=5)

    def _worker(self) -> None:
        while not self._stop.is_set():
            if not self.step(timeout=0.2):
                continue

    def step(self, timeout: float = 0.2) -> bool:
        """取一个任务并处理（worker 主循环体；测试可直接调，同步可断言）。"""
        try:
            item = self.pq.get(timeout=timeout)
        except queue.Empty:
            return False
        _, _, task = item
        try:
            if task.prio == PRIO_FINAL:
                self._handle_final(task)
            else:
                self._handle_partial(task)
        except Exception as e:
            log.warning(f"[scheduler] ⚠ 解码任务失败: {e}", exc_info=True)
        finally:
            self.pq.task_done()
        return True

    def _handle_final(self, task: DecodeTask) -> None:
        """整句路径：默认参数转写（beam 1 + vad_filter + 无词级时间戳）+
        术语表 initial_prompt（与 main.py 非流式路径的 runtime['prompt'] 注入
        对称——经 base_prompt 通道，评审修复）；随后重置流式层——该段音频已定稿，
        partial 预览状态作废（final 覆盖），并进入幽灵抑制窗（见 _suppress）。
        转写异常就地捕获并仍回调 on_final（en='' + meta['error']）：step() 的
        外层 catch 只记日志，若此处不回调，main 流式分支的 await final_ready_q.get()
        将永久挂死（字幕/笔记/翻译全停）——评审修复。"""
        t0 = time.time()
        en = ""
        seg_times: list = []
        try:
            if task.asr_ref is not None:
                # p2-3：顺带取段级时间戳（with_times）供 SRT/导出用。收集零额外
                # 开销——segments 本就要遍历一次拼文本；且**不开** word_timestamps
                # （那个才加耗时）。替身/旧实现可能仍只回 str，故按类型解包。
                res = task.asr_ref.transcribe(
                    task.audio, initial_prompt=self.base_prompt() or None,
                    with_times=True)
                if isinstance(res, tuple):
                    en, seg_times = res[0], (res[1] or [])
                else:
                    en = res
        except Exception as e:
            log.warning(f"[scheduler] ⚠ final 转写失败（回调空结果防挂死）: {e}",
                        exc_info=True)
            meta = dict(task.meta)
            meta["decode_s"] = time.time() - t0
            meta["error"] = f"{type(e).__name__}: {e}"
            meta["seg_times"] = []      # p2-3：形状恒定，消费方无需判键存在
            self._reset_online(task.asr_ref)
            self._arm_ghost_suppress()
            self.on_final("", meta)
            return
        decode_s = time.time() - t0
        self._reset_online(task.asr_ref)
        self._arm_ghost_suppress()
        meta = dict(task.meta)
        meta["decode_s"] = decode_s
        # p2-3：段级时间戳（相对本段音频起点，秒）+ 音频时长，供导出侧换算绝对偏移
        meta["seg_times"] = seg_times
        meta["audio_s"] = len(task.audio) / SAMPLING_RATE
        # en 为空也要回调：asr_loop 在等待本 final 的结果，静默跳过会挂死主循环
        self.on_final(en, meta)
        if en:
            log.debug(f"[scheduler] final 解码 {decode_s:.2f}s"
                      f"（音频 {len(task.audio) / SAMPLING_RATE:.1f}s）")

    def _handle_partial(self, task: DecodeTask) -> None:
        if task.asr_ref is None or self.paused() or self._ghost_suppressed():
            return
        if task.meta.get("epoch") != self._epoch:
            return  # 早于最近一次 final 入队的过期预览：该段音频已定稿，丢弃。
            # epoch 检查必须先于 asr 热切换重置（评审修复）：否则热切换后处理的
            # 旧实例过期 partial 会白解码一次已定稿音频并把流式层重绑回旧实例。
        if task.asr_ref is not self._online_asr:
            # 热切换交接：以入队快照实例重建流式层（HypothesisBuffer / 已提交
            # prompt 重置，防跨模型 prompt 错位——条目 11 设计）。
            self._reset_online(task.asr_ref)
        self._online.insert_audio_chunk(task.audio)
        b, e, text = self._online.process_iter()
        text = (text or "").strip()
        if text:
            self.emit_partial(partial_payload(self._key, text))

    def _reset_online(self, asr_ref) -> None:
        """（重）建流式层状态并绑定 asr 实例。key 递增：每次重置开启新一段
        partial 预览（final 定稿/模型热切换都会走到这里）。"""
        self._online = OnlineASRProcessor(
            WhisperASRAdapter(asr_ref, base_prompt=self.base_prompt()),
            buffer_trimming=self.buffer_trimming,
        )
        self._online_asr = asr_ref
        self._key += 1
        if asr_ref is not None:
            self._min_chunk_s = self._min_chunk_for(asr_ref)

    def _arm_ghost_suppress(self) -> None:
        """final 定稿后布置幽灵抑制窗（时长见构造参数 ghost_suppress_s）。"""
        window = (self.ghost_suppress_s if self.ghost_suppress_s is not None
                  else self._min_chunk_s + 0.5)
        self._suppress_until = time.time() + window

    def _ghost_suppressed(self) -> bool:
        """抑制窗内：已入队的过期 partial 在 worker 侧丢弃，feeder 侧清空累积。"""
        return time.time() < self._suppress_until

    @staticmethod
    def _min_chunk_for(asr_ref) -> float:
        cuda = getattr(asr_ref, "device", "cpu") == "cuda"
        return DecodeScheduler.MIN_CHUNK_GPU_S if cuda else DecodeScheduler.MIN_CHUNK_CPU_S
