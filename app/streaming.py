# -*- coding: utf-8 -*-
"""streaming.py — LocalAgreement-2 流式转写核心（升级计划条目 11）。

HypothesisBuffer / OnlineASRProcessor 移植自 ufal/whisper_streaming
（whisper_online.py，https://github.com/ufal/whisper_streaming），保留下方原
MIT 版权头。改动范围：剥离 librosa/soundfile/命令行/多后端选择，仅保留这两个
类；asr 适配本仓库 app/asr.py 的 WhisperASR（WhisperASRAdapter）。
算法出处：Macháček et al. 2023, https://arxiv.org/abs/2307.14743
——确认 = 最近两次假设的最长公共前缀（HypothesisBuffer.flush()）；
已提交文本取 200 字符后缀作下一轮 prompt；词级时间戳依赖 word_timestamps=True。

MIT License

Copyright (c) 2023 ÚFAL

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import logging

import numpy as np

log = logging.getLogger("streaming")

SAMPLING_RATE = 16000  # 与 capture.SR 一致


class HypothesisBuffer:
    """LocalAgreement-2 的假设缓冲：insert() 收下一次解码假设，flush() 提交
    「最近两次假设的最长公共前缀」。n-gram 去重抑制解码器把已提交词又吐一遍。"""

    def __init__(self):
        self.commited_in_buffer = []
        self.buffer = []
        self.new = []

        self.last_commited_time = 0
        self.last_commited_word = None

    def insert(self, new, offset):
        # compare self.commited_in_buffer and new. It inserts only the words in new that extend the commited_in_buffer, it means they are roughly behind last_commited_time and new in content
        # the new tail is added to self.new

        new = [(a + offset, b + offset, t) for a, b, t in new]
        self.new = [(a, b, t) for a, b, t in new if a > self.last_commited_time - 0.1]

        if len(self.new) >= 1:
            a, b, t = self.new[0]
            if abs(a - self.last_commited_time) < 1:
                if self.commited_in_buffer:
                    # it's going to search for 1, 2, ..., 5 consecutive words (n-grams) that are identical in commited and new. If they are, they're dropped.
                    cn = len(self.commited_in_buffer)
                    nn = len(self.new)
                    for i in range(1, min(min(cn, nn), 5) + 1):  # 5 is the maximum
                        c = " ".join([self.commited_in_buffer[-j][2] for j in range(1, i + 1)][::-1])
                        tail = " ".join(self.new[j - 1][2] for j in range(1, i + 1))
                        if c == tail:
                            words = []
                            for j in range(i):
                                words.append(repr(self.new.pop(0)))
                            words_msg = " ".join(words)
                            log.debug(f"removing last {i} words: {words_msg}")
                            break

    def flush(self):
        # returns commited chunk = the longest common prefix of 2 last inserts.

        commit = []
        while self.new:
            na, nb, nt = self.new[0]

            if len(self.buffer) == 0:
                break

            if nt == self.buffer[0][2]:
                commit.append((na, nb, nt))
                self.last_commited_word = nt
                self.last_commited_time = nb
                self.buffer.pop(0)
                self.new.pop(0)
            else:
                break
        self.buffer = self.new
        self.new = []
        self.commited_in_buffer.extend(commit)
        return commit

    def pop_commited(self, time):
        while self.commited_in_buffer and self.commited_in_buffer[0][1] <= time:
            self.commited_in_buffer.pop(0)

    def complete(self):
        return self.buffer


class OnlineASRProcessor:
    """流式解码状态机：音频缓冲 + HypothesisBuffer + 已提交文本。
    buffer_trimming 默认 ("segment", 15)（计划条目 11 口径）：缓冲超 15s 在已
    提交段边界裁剪；"sentence" 模式需要 tokenizer，本仓库不使用（tokenizer=None）。"""

    SAMPLING_RATE = SAMPLING_RATE

    def __init__(self, asr, tokenizer=None, buffer_trimming=("segment", 15)):
        """asr: 提供 sep / transcribe(audio, init_prompt=...) / ts_words(res) /
        segments_end_ts(res) 的适配对象（见 WhisperASRAdapter）。"""
        self.asr = asr
        self.tokenizer = tokenizer

        self.init()

        self.buffer_trimming_way, self.buffer_trimming_sec = buffer_trimming

    def init(self, offset=None):
        """run this when starting or restarting processing"""
        self.audio_buffer = np.array([], dtype=np.float32)
        self.transcript_buffer = HypothesisBuffer()
        self.buffer_time_offset = 0
        if offset is not None:
            self.buffer_time_offset = offset
        self.transcript_buffer.last_commited_time = self.buffer_time_offset
        self.commited = []

    def insert_audio_chunk(self, audio):
        self.audio_buffer = np.append(self.audio_buffer, audio)

    def prompt(self):
        """Returns a tuple: (prompt, context), where "prompt" is a 200-character suffix of commited text that is inside of the scrolled away part of audio buffer.
        "context" is the commited text that is inside the audio buffer. It is transcribed again and skipped. It is returned only for debugging and logging reasons.
        """
        k = max(0, len(self.commited) - 1)
        while k > 0 and self.commited[k - 1][1] > self.buffer_time_offset:
            k -= 1

        p = self.commited[:k]
        p = [t for _, _, t in p]
        prompt = []
        l = 0
        while p and l < 200:  # 200 characters prompt size
            x = p.pop(-1)
            l += len(x) + 1
            prompt.append(x)
        non_prompt = self.commited[k:]
        return self.asr.sep.join(prompt[::-1]), self.asr.sep.join(t for _, _, t in non_prompt)

    def process_iter(self):
        """Runs on the current audio buffer.
        Returns: a tuple (beg_timestamp, end_timestamp, "text"), or (None, None, "").
        The non-emty text is confirmed (committed) partial transcript.
        """

        prompt, non_prompt = self.prompt()
        log.debug(f"PROMPT: {prompt}")
        log.debug(f"CONTEXT: {non_prompt}")
        log.debug(f"transcribing {len(self.audio_buffer) / self.SAMPLING_RATE:2.2f} seconds from {self.buffer_time_offset:2.2f}")
        res = self.asr.transcribe(self.audio_buffer, init_prompt=prompt)

        # transform to [(beg,end,"word1"), ...]
        tsw = self.asr.ts_words(res)

        self.transcript_buffer.insert(tsw, self.buffer_time_offset)
        o = self.transcript_buffer.flush()
        self.commited.extend(o)
        completed = self.to_flush(o)
        log.debug(f">>>>COMPLETE NOW: {completed}")
        the_rest = self.to_flush(self.transcript_buffer.complete())
        log.debug(f"INCOMPLETE: {the_rest}")

        # there is a newly confirmed text

        if o and self.buffer_trimming_way == "sentence":  # trim the completed sentences
            if len(self.audio_buffer) / self.SAMPLING_RATE > self.buffer_trimming_sec:  # longer than this
                self.chunk_completed_sentence()

        if self.buffer_trimming_way == "segment":
            s = self.buffer_trimming_sec  # trim the completed segments longer than s,
        else:
            s = 30  # if the audio buffer is longer than 30s, trim it

        if len(self.audio_buffer) / self.SAMPLING_RATE > s:
            self.chunk_completed_segment(res)
            log.debug("chunking segment")

        log.debug(f"len of buffer now: {len(self.audio_buffer) / self.SAMPLING_RATE:2.2f}")
        return self.to_flush(o)

    def chunk_completed_sentence(self):
        if self.commited == []:
            return
        sents = self.words_to_sentences(self.commited)
        for s in sents:
            log.debug(f"\t\tSENT: {s}")
        if len(sents) < 2:
            return
        while len(sents) > 2:
            sents.pop(0)
        # we will continue with audio processing at this timestamp
        chunk_at = sents[-2][1]

        log.debug(f"--- sentence chunked at {chunk_at:2.2f}")
        self.chunk_at(chunk_at)

    def chunk_completed_segment(self, res):
        if self.commited == []:
            return

        ends = self.asr.segments_end_ts(res)

        t = self.commited[-1][1]

        if len(ends) > 1:

            e = ends[-2] + self.buffer_time_offset
            while len(ends) > 2 and e > t:
                ends.pop(-1)
                e = ends[-2] + self.buffer_time_offset
            if e <= t:
                log.debug(f"--- segment chunked at {e:2.2f}")
                self.chunk_at(e)
            else:
                log.debug(f"--- last segment not within commited area")
        else:
            log.debug(f"--- not enough segments to chunk")

    def chunk_at(self, time):
        """trims the hypothesis and audio buffer at "time"
        """
        self.transcript_buffer.pop_commited(time)
        cut_seconds = time - self.buffer_time_offset
        self.audio_buffer = self.audio_buffer[int(cut_seconds * self.SAMPLING_RATE):]
        self.buffer_time_offset = time

    def words_to_sentences(self, words):
        """Uses self.tokenizer for sentence segmentation of words.
        Returns: [(beg,end,"sentence 1"),...]
        """

        cwords = [w for w in words]
        t = " ".join(o[2] for o in cwords)
        s = self.tokenizer.split(t)
        out = []
        while s:
            beg = None
            end = None
            sent = s.pop(0).strip()
            fsent = sent
            while cwords:
                b, e, w = cwords.pop(0)
                w = w.strip()
                if beg is None and sent.startswith(w):
                    beg = b
                elif end is None and sent == w:
                    end = e
                    out.append((beg, end, fsent))
                    break
                sent = sent[len(w):].strip()
        return out

    def finish(self):
        """Flush the incomplete text when the whole processing ends.
        Returns: the same format as self.process_iter()
        """
        o = self.transcript_buffer.complete()
        f = self.to_flush(o)
        log.debug(f"last, noncommited: {f}")
        self.buffer_time_offset += len(self.audio_buffer) / 16000
        return f

    def to_flush(self, sents, sep=None, offset=0, ):
        # concatenates the timestamped words or sentences into one sequence that is flushed in one line
        # sents: [(beg1, end1, "sentence1"), ...] or [] if empty
        # return: (beg1,end-of-last-sentence,"concatenation of sentences") or (None, None, "") if empty
        if sep is None:
            sep = self.asr.sep
        t = sep.join(s[2] for s in sents)
        if len(sents) == 0:
            b = None
            e = None
        else:
            b = offset + sents[0][0]
            e = offset + sents[-1][1]
        return (b, e, t)


class WhisperASRAdapter:
    """把本仓库 WhisperASR 适配成 OnlineASRProcessor 需要的接口。
    流式解码参数（条目 11 设计）：beam_size=5、vad_filter=False（分块解码开
    vad_filter 会造成块边界/时间戳错位——技术评审）、word_timestamps=True
    （LocalAgreement 依赖词级时间戳）；condition_on_previous_text=False 由
    asr.transcribe 内部固定。base_prompt = 术语表 initial_prompt 持续注入
    （与已提交 200 字符后缀拼接，防专名改口），合计截断 800 字符保险。"""

    sep = ""  # faster-whisper 输出词自带空格，拼接用空串（沿用上游 FasterWhisperASR.sep）

    PROMPT_TOTAL_MAX_CHARS = 800  # whisper 提示词保险上限（whisper 限 ~224 token）
    # 术语表先截断（评审修复）：init_prompt（最近已提交上下文 200 字符，防专名
    # 改口最有效的部分）必须完整保留在尾部——原实现整体 [:800] 尾部截断，术语表
    # 接近上限时最近上下文被整段切掉或切半词。
    BASE_PROMPT_MAX_CHARS = 560

    def __init__(self, asr, base_prompt: str = ""):
        self.asr = asr
        self.base_prompt = (base_prompt or "").strip()

    def transcribe(self, audio, init_prompt=""):
        base = self.base_prompt[: self.BASE_PROMPT_MAX_CHARS].rstrip()
        init = (init_prompt or "").strip()
        parts = [p for p in (base, init) if p]
        prompt = " ".join(parts)
        prompt = prompt[: self.PROMPT_TOTAL_MAX_CHARS] or None  # 兜底（560+1+200 < 800）
        return self.asr.transcribe(
            audio,
            initial_prompt=prompt,
            beam_size=5,
            vad_filter=False,
            word_timestamps=True,
        )

    def ts_words(self, res):
        """[(beg, end, word), ...]；整段 no_speech_prob>0.9 视为静音幻觉丢弃
        （沿用上游 FasterWhisperASR.ts_words）。不剥词内空格——sep="" 拼接依赖它。"""
        o = []
        for segment in res:
            if segment.no_speech_prob > 0.9:
                continue
            for word in segment.words or []:
                o.append((word.start, word.end, word.word))
        return o

    def segments_end_ts(self, res):
        return [s.end for s in res]
