# -*- coding: utf-8 -*-
"""asr.py — faster-whisper 封装。默认 CPU int8（小模型对 5-15s 段足够实时），GPU 可选。"""
import numpy as np
from faster_whisper import WhisperModel


class WhisperASR:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
        cpu_threads: int = 4,
    ):
        self.language = language
        print(f"[asr] loading faster-whisper '{model_size}' on {device}/{compute_type} ...")
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )
        print("[asr] model ready")

    def transcribe(self, audio_f32: np.ndarray, sample_rate: int = 16000) -> str:
        if audio_f32.dtype != np.float32:
            audio_f32 = audio_f32.astype(np.float32)
        segments, _ = self.model.transcribe(
            audio_f32,
            language=self.language,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        parts = [s.text.strip() for s in segments if s.text.strip()]
        return " ".join(parts)
