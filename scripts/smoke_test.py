# -*- coding: utf-8 -*-
"""smoke_test.py — 离线全管线自检（不需要麦克风）。
用法: .venv/Scripts/python.exe scripts/smoke_test.py [--translate] [--server] [--gpu] [--vad-only]
--vad-only: 只跑 [1]/[1b]/[1c] VAD 分句段（离线秒级回归，条目 2；纯逻辑单测在 tests/ 走 pytest）
1)  VAD 分句状态机：合成音频（安静/语音突发/稳态嗡噪）过 silero 和 energy 两种 VAD
1b) 超长切段：~20s 连续语音（无 700ms 静音）→ 溢出后对齐静音/硬切兜底，无 15s 碎片
1c) 麦克风 watchdog 基元：MicSegmenter.healthy 属性（未启动/活线程心跳/断流/error）
2)  ASR：Windows SAPI TTS 合成英文 wav → faster-whisper 识别（计时）
3) --gpu: large-v3-turbo CUDA 基准（RTF + 显存），与 CPU small 对比
4) --translate: 用 DEEPSEEK_API_KEY 实调翻译 API（产生极少量费用）
5) --server:    起真实服务子进程（环回豁免禁用），验证 HTTP/WS token 鉴权 + backlog 上报 +
   模型热切换 + 暂停/恢复 + 笔记导出（条目19）
7) 流式（条目11/12）：[7a] TTS 句子按 0.5s 块喂真模型流式管线——partial 首字早于整句可用、
   committed 命中 KEYS、final meta 带采集时间戳、word_timestamps 耗时增幅记录在案；
   [7b] --streaming 服务级子用例——status.streaming 布线 + WS 稳定 + 暂停/恢复
全部通过 exit 0；任何一步失败 exit 1。"""
import argparse
import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from applog import setup_logging  # noqa: E402
from capture import BLOCK, SR, MicSegmenter  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'} — {name}" + (f"（{note}）" if note else ""))


# ---------------------------------------------------------------- 1. VAD 状态机
def synth_stream(with_hum: bool):
    """4 段式合成流：静音1s → 语音2s → 静音1s → 语音2s → 静音1.5s（16k float32）。
    with_hum=True 时全程叠加 120Hz 稳态嗡噪（模拟空调/投影仪噪声）。"""
    rng = np.random.default_rng(42)
    t = np.arange(int(SR * 7.5)) / SR

    speech = np.zeros_like(t)
    for a, b in [(1.0, 3.0), (4.0, 6.0)]:
        seg = (t >= a) & (t < b)
        env = 0.5 * (1 - np.cos(2 * np.pi * 1.5 * (t[seg] - t[seg][0])))
        speech[seg] = env * (
            0.6 * np.sin(2 * np.pi * 220 * t[seg])
            + 0.3 * np.sin(2 * np.pi * 440 * t[seg])
            + 0.15 * np.sin(2 * np.pi * 660 * t[seg])
        )
        speech[seg] += 0.05 * rng.standard_normal(seg.sum())
    stream = speech + 0.002 * rng.standard_normal(len(t))
    if with_hum:
        stream += 0.02 * np.sin(2 * np.pi * 120 * t)  # RMS≈0.014 稳态噪声
    return stream.astype(np.float32)


def run_vad_case(vad_kind: str, stream: np.ndarray) -> list[float]:
    q: queue.Queue = queue.Queue()
    seg = MicSegmenter(q, vad=vad_kind)
    for i in range(0, len(stream) - BLOCK, BLOCK):
        seg.handle_block(stream[i:i + BLOCK])
    durs = []
    while not q.empty():
        _, audio = q.get()
        durs.append(round(len(audio) / SR, 2))
    return durs


def test_vad() -> None:
    print("\n[1] VAD 分句状态机")
    for vad_kind in ("silero", "energy"):
        for with_hum in (False, True):
            case = f"{vad_kind}{'(+嗡噪)' if with_hum else '(安静)'}"
            try:
                durs = run_vad_case(vad_kind, synth_stream(with_hum))
                ok = 1 <= len(durs) <= 3 and all(1.2 <= d <= 3.6 for d in durs)
                record(f"VAD {case}", ok, f"切出 {len(durs)} 段: {durs}s")
            except Exception as e:
                record(f"VAD {case}", False, f"异常: {e}")


# ---------------------------------------------------------------- 1b. 超长切段
def synth_long_speech(total_s: float = 22.0) -> np.ndarray:
    """连续长语音流：开头 2s 纯静音（energy VAD 校准），随后 ~20s 语音中间
    无任何 >700ms 静音——每 3s 嵌一个 0.4s 深 dip（低于滞回退出线，触发
    quiet 但短于 silence_blocks，不足以正常关段）。旧逻辑会在这里切出恒定
    15s 硬切碎片；新逻辑应溢出后对齐 dip（静音点）关段或 18s 硬切兜底。"""
    rng = np.random.default_rng(7)
    n = int(SR * total_s)
    t = np.arange(n) / SR
    env = 0.10 + 0.05 * np.sin(2 * np.pi * 2.5 * t)      # 词节奏起伏，RMS 恒 > 0.01
    for d0 in np.arange(3.0, total_s, 3.0):              # 0.4s 深 dip → quiet 块
        dip = (t >= d0) & (t < d0 + 0.4)
        env[dip] = 0.001
    # 噪声随包络缩放：dip 段才是真静音（否则 0.02 噪声底让 VAD 永不报 quiet）
    speech = env * (np.sin(2 * np.pi * 220 * t) + 0.2 * rng.standard_normal(n))
    stream = np.where(t >= 2.0, speech, 0.0) + 0.002 * rng.standard_normal(n)
    return stream.astype(np.float32)


def test_overflow_cut() -> None:
    print("\n[1b] 超长切段对齐静音点（energy VAD，~20s 连续语音）")
    try:
        stream = synth_long_speech()
        durs = run_vad_case("energy", stream)
        print(f"  切出 {len(durs)} 段: {durs}s")
        # 溢出后不再立即硬切：所有段都应长于软上限（对齐静音或 18s 硬切兜底）
        record("超长段存在（≥15s 溢出后切出）", len(durs) >= 1 and max(durs) >= 15.0,
               f"最长 {max(durs) if durs else 0}s")
        # 旧逻辑特征：在 15.0s 附近的块边界硬切碎片（15s 软上限恰好触发）
        shards = [d for d in durs if 14.8 <= d <= 15.1]
        record("无恒定 15s 硬切碎片", not shards, f"特征碎片: {shards or '无'}")
    except Exception as e:
        record("超长切段对齐静音点", False, f"异常: {e}")


# ---------------------------------------------------------------- 1c. watchdog 基元
def test_healthy() -> None:
    print("\n[1c] 麦克风 watchdog 基元（healthy 属性，无麦克风）")
    import threading
    try:
        seg = MicSegmenter(queue.Queue(), vad="energy")
        # 属性存在；线程未启动（smoke 不开真实采集流）时 healthy 为 False
        record("healthy 属性存在（未启动=False）",
               isinstance(seg.healthy, bool) and seg.healthy is False,
               f"healthy={seg.healthy}")
        # 模拟 start() 后的初始状态：活线程 + 新鲜心跳 → True（采集线程 3s 内必有块）
        ev = threading.Event()
        seg.thread = threading.Thread(target=ev.wait, daemon=True)
        seg.thread.start()
        seg.last_feed_ts = time.time()
        record("healthy 活线程+新鲜心跳=True", seg.healthy is True)
        seg.last_feed_ts = time.time() - (MicSegmenter.FEED_STALE_S + 1)
        record("healthy 断流心跳过期=False", seg.healthy is False)
        seg.error = "PortAudioError: simulated"
        seg.last_feed_ts = time.time()
        record("healthy 有 error 优先=False", seg.healthy is False)
    except Exception as e:
        record("watchdog 基元", False, f"异常: {e}")
    finally:
        ev.set()


# ---------------------------------------------------------------- 2. TTS + ASR
def synth_tts(text: str, path: Path) -> np.ndarray:
    """Windows SAPI 合成英文 wav，读回并重采样到 16k float32。"""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{text}'); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   timeout=60, capture_output=True)
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    if sr != SR:
        idx = np.arange(0, len(audio), sr / SR)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


SENTENCE = ("Today we will talk about neural networks, attention mechanism, "
            "and gradient descent.")
KEYS = ("neural", "attention", "gradient")


def test_asr() -> str:
    print("\n[2] ASR 基线（TTS 合成 → CPU small）")
    wav_path = ROOT / "scripts" / "_tts_sample.wav"
    try:
        audio = synth_tts(SENTENCE, wav_path)
        from asr import WhisperASR
        asr = WhisperASR(model_size="small", device="cpu")
        t0 = time.time()
        en = asr.transcribe(audio)
        dt = time.time() - t0
        dur = len(audio) / SR
        low = en.lower()
        hits = sum(k in low for k in KEYS)
        record("ASR CPU small", hits >= 2,
               f"命中 {hits}/3 · 耗时 {dt:.2f}s / 音频 {dur:.1f}s → RTF {dt / dur:.2f}")
        return en
    except Exception as e:
        record("ASR CPU small", False, f"异常: {e}")
        return ""
    finally:
        wav_path.unlink(missing_ok=True)


def gpu_vram_mb() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        used, total = out.stdout.strip().splitlines()[0].split(",")
        return f"{int(used) / 1024:.1f}/{int(total) / 1024:.1f} GB"
    except Exception:
        return "nvidia-smi 不可用"


def test_gpu() -> None:
    print("\n[3] GPU 基准（large-v3-turbo / CUDA）")
    wav_path = ROOT / "scripts" / "_tts_gpu.wav"
    try:
        audio = synth_tts(SENTENCE, wav_path)
        from asr import WhisperASR
        asr = WhisperASR(model_size="large-v3-turbo", device="auto")
        assert asr.device == "cuda", f"device 解析为 {asr.device}，未上 GPU"
        dur = len(audio) / SR
        asr.transcribe(audio)  # warmup（首次调用含 kernel 编译）
        t0 = time.time()
        en = asr.transcribe(audio)
        dt = time.time() - t0
        low = en.lower()
        hits = sum(k in low for k in KEYS)
        record("GPU large-v3-turbo", hits >= 2,
               f"命中 {hits}/3 · 稳态耗时 {dt:.2f}s → RTF {dt / dur:.2f} · 显存 {gpu_vram_mb()}")
        print(f"  GPU 结果: {en}")
    except Exception as e:
        record("GPU large-v3-turbo", False, f"异常: {e}")
    finally:
        wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 4. 翻译
def test_translate(text_en: str) -> None:
    print("\n[4] DeepSeek 翻译（实调 API）")
    if not text_en:
        record("DeepSeek 翻译", False, "无 ASR 输出可翻")
        return
    try:
        from translate import DeepSeekTranslator, load_api_key
        key = load_api_key(ROOT / ".env")
        assert key, "未找到 DEEPSEEK_API_KEY"
        glossary = ROOT / "glossary" / "CS.md"
        tr = DeepSeekTranslator(key, glossary_files=[str(glossary)])
        assert len(tr.entries) > 0, "术语表解析为空"
        zh = asyncio.run(tr.translate(text_en))
        print(f"  翻译结果: {zh}")
        has_cjk = any("\u4e00" <= c <= "\u9fff" for c in zh)
        record("DeepSeek 翻译", has_cjk, f"术语 {len(tr.entries)} 条")
    except Exception as e:
        record("DeepSeek 翻译", False, f"异常: {e}")


# ---------------------------------------------------------------- 5. 真实服务
def _ws_status_line(port: int, token: str | None = None) -> str:
    """裸 socket 发 WebSocket 握手，返回响应首行（网络错误返回 ""）。
    条目19：101=接受（带 token 或环回豁免）；403=拒绝（无 token 且豁免禁用——
    服务端在 accept 前发 websocket.close，uvicorn 以 HTTP 403 完成握手拒绝）。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        qs = f"?token={token}" if token else ""
        req = (f"GET /ws{qs} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = s.recv(200).decode(errors="replace")
        s.close()
        return resp.splitlines()[0] if resp else ""
    except Exception:
        return ""


def ws_handshake_ok(port: int, token: str | None = None) -> bool:
    """裸 socket WebSocket 握手：带 token（或环回豁免）应收到 101（/ws 路由正常）。"""
    return "101" in _ws_status_line(port, token)


def ws_handshake_rejected(port: int) -> tuple[bool, str]:
    """条目19：无 token 握手必须被拒——断言非 101（计划口径）；返回 (判定, 响应首行)
    供 record 备注实况。"""
    line = _ws_status_line(port)
    return (bool(line) and "101" not in line), line


def test_server(port: int = 8799) -> None:
    print(f"\n[5] 真实服务子进程 (port={port}，环回豁免禁用=token 必带)")
    import httpx
    # 条目19：豁免禁用 → 本机对端按非环回处理，覆盖「无 token 非环回 403」分支
    # （§2.2-19）；只影响本子进程，[7b] 流式段不设——顺带覆盖环回豁免放行分支。
    env = dict(os.environ)
    env["LECTURELENS_AUTH_NO_LOCAL_EXEMPT"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    token, H = "", {}
    try:
        # 条目19①：等服务端 provision_token 把随机 token 写进 config.json
        #（首启生成；已存在则立即可读。值不打印进 smoke 输出。）
        deadline = time.time() + 60
        while time.time() < deadline and not token:
            try:
                disk = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                token = str(disk.get("auth_token") or "")
            except Exception:
                token = ""
            if not token:
                time.sleep(0.5)
        record("首启 token 生成并写 config.json", bool(token),
               "auth_token 已就绪（值不落 smoke 输出）" if token else "⚠ 未读到 auth_token")
        if token:
            H = {"X-LectureLens-Token": token}
        # 无 token HTTP 必须被拒（豁免已禁用；服务端口先于模型就绪打开）
        noauth_code = -1
        t0 = time.time()
        while time.time() < t0 + 20 and noauth_code == -1:
            try:
                noauth_code = httpx.get(f"{base}/api/status", timeout=3).status_code
            except Exception:
                time.sleep(0.5)
        record("无 token 非环回 403", noauth_code == 403, f"HTTP {noauth_code}")

        deadline = time.time() + 300
        st = {}
        while time.time() < deadline:
            try:
                st = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
                if st.get("asr") == "ready":
                    break
            except Exception:
                pass
            time.sleep(2)
        record("服务启动 & 模型加载", st.get("asr") == "ready",
               f"status={st.get('asr')} · {st.get('device')}/{st.get('compute_type')}")
        record("带 token HTTP 200（?token= 查询参数）",
               httpx.get(f"{base}/api/status?token={token}", timeout=3).status_code == 200,
               "HTTP 与 WS 同一 token 提取路径")
        record("translate 状态就绪", "translate" in st, str(st.get("translate")))
        # 修复5：队列积压可见——/api/status 必须暴露 backlog（数值型段数）
        record("status 含 backlog 字段", isinstance(st.get("backlog"), int),
               f"backlog={st.get('backlog')}")
        # 条目9④：前端 ui_metrics 上行并入 status（默认 0，浏览器端才有非零值）
        record("status 含 ui_zh_dropped 字段", isinstance(st.get("ui_zh_dropped"), int),
               f"ui_zh_dropped={st.get('ui_zh_dropped')}")

        html = httpx.get(f"{base}/", timeout=5).text
        record("字幕页可访问（/ 不拦，token 粘贴入口）", "LectureLens" in html and "btnPause" in html)
        # 条目19：ws_handshake_ok 改带 token 版 + 新增 ws_handshake_rejected
        record("WebSocket 握手（带 token）", ws_handshake_ok(port, token))
        rej_ok, rej_line = ws_handshake_rejected(port)
        record("无 token WS 握手被拒（非 101）", rej_ok, f"响应: {rej_line[:48] or '空'}")

        # 配置读取 + 模型热切换（切 CPU base，快）
        cfg = httpx.get(f"{base}/api/config", timeout=5, headers=H).json()
        record("/api/config 读取", "model" in cfg, f"model={cfg.get('model')}")
        # 条目4：非法 model → 400 且不污染（status.asr 仍 ready、config.json 不变）
        r_bad = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                           json={"model": "not-a-model"})
        st_bad = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
        record("非法 model 400 且不污染",
               r_bad.status_code == 400 and st_bad.get("asr") == "ready",
               f"HTTP {r_bad.status_code} · asr={st_bad.get('asr')}")
        # 切换目标自适应：当前已是 base 则换 small，保证确有切换发生（进度条才可见）
        swap_to = "base" if cfg.get("model") != "base" else "small"
        r = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                       json={"model": swap_to, "device": "cpu", "compute_type": "int8"})
        # 加载进度可见：切换后 /api/status 应出现 load={pct,stage}，就绪后清除
        t0 = time.time()
        load_seen, load_cleared = None, False
        swapped = False
        while time.time() - t0 < 120:
            st = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
            if st.get("load") and not load_seen:
                load_seen = st["load"]
            if load_seen and not st.get("load"):
                load_cleared = True
            if st.get("model") == swap_to and st.get("asr") == "ready":
                swapped = True
                break
            time.sleep(1)
        record(f"模型热切换({swap_to})", swapped and r.json().get("ok"),
               f"{time.time() - t0:.0f}s · 服务未重启")
        record("加载进度条数据可见", bool(load_seen) and load_cleared,
               f"观察到 {load_seen} → 就绪后清除")
        # 换回默认（条目4 save-after-build：构建成功才写 config.json——必须在 terminate
        # 前等构建完成，否则 restore 丢失、config.json 停在 swap_to，下次启动变样）
        httpx.post(f"{base}/api/config", timeout=5, headers=H,
                   json={"model": cfg.get("model"), "device": cfg.get("device"),
                         "compute_type": cfg.get("compute_type")})
        t0 = time.time()
        restored = False
        while time.time() - t0 < 240:
            st = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
            if (st.get("model") == cfg.get("model") and st.get("asr") == "ready"
                    and not st.get("load")):
                restored = True
                break
            time.sleep(1)
        time.sleep(1)  # save_config 在构建成功后同步执行：留一拍避免文件竞态
        restore_ok = restored and f'"{cfg.get("model")}"' in (
            (ROOT / "config.json").read_text(encoding="utf-8")
            if (ROOT / "config.json").is_file() else "")
        record("换回默认且落盘（restore）", restore_ok,
               f"{time.time() - t0:.0f}s · config.json 写回 {cfg.get('model')}")

        # 条目13：语言热切换——POST lang → status 反映 + config.json 持久化 + 服务不重启
        # （asr.language 属性热改在服务进程内发生；属性级单测在 tests/test_config_safety.py）
        orig_lang = cfg.get("lang", "en")
        swap_lang = "zh" if orig_lang != "zh" else "en"
        r_lang = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                            json={"lang": swap_lang})
        st_lang = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
        record("语言热切换（status 反映）",
               r_lang.json().get("ok") and st_lang.get("lang") == swap_lang,
               f"{orig_lang} → {swap_lang} · 服务未重启")
        time.sleep(0.3)  # save_config 在 POST 同步执行；留一拍防文件竞态（同模型 restore 口径）
        cfg_disk = {}
        try:
            cfg_disk = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        record("lang 持久化落盘（重启后仍 zh 的前提）", cfg_disk.get("lang") == swap_lang,
               f"config.json lang={cfg_disk.get('lang')}")
        r_bad_lang = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                                json={"lang": "xx"})
        record("非法 lang 400", r_bad_lang.status_code == 400, f"HTTP {r_bad_lang.status_code}")
        httpx.post(f"{base}/api/config", timeout=5, headers=H,
                   json={"lang": orig_lang})
        st_rest = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
        record("语言恢复原值", st_rest.get("lang") == orig_lang, f"→ {orig_lang}")

        # 暂停/恢复
        httpx.post(f"{base}/api/pause", timeout=5, headers=H)
        p1 = httpx.get(f"{base}/api/status", timeout=3, headers=H).json().get("paused")
        httpx.post(f"{base}/api/resume", timeout=5, headers=H)
        p2 = httpx.get(f"{base}/api/status", timeout=3, headers=H).json().get("paused")
        record("暂停/恢复", (p1 is True) and (p2 is False), f"pause={p1} → resume={p2}")

        # 档位化（2026-10-13）：/api/tiers 四档 + 单一推荐 + 硬件画像（真机检测）
        r = httpx.get(f"{base}/api/tiers", timeout=10, headers=H)
        tj = r.json() if r.status_code == 200 else {}
        tiers_list = tj.get("tiers", [])
        rec_model = tj.get("recommended_model")
        tier_ok = (r.status_code == 200 and len(tiers_list) == 4
                   and rec_model in [t.get("model") for t in tiers_list]
                   and all(t.get("name") and t.get("est_h") for t in tiers_list))
        rec_name = next((t["name"] for t in tiers_list if t.get("recommended")), "—")
        hw = tj.get("hardware", {})
        record("档位视图 /api/tiers", tier_ok,
               f"推荐「{rec_name}」({rec_model}) · gpu={hw.get('gpu')} "
               f"vram={hw.get('vram_gb')} ram={hw.get('ram_gb')}")

        # 笔记导出
        r = httpx.get(f"{base}/api/notes", timeout=5, headers=H)
        record("笔记导出 /api/notes",
               r.status_code == 200 and "双语课堂笔记" in r.text,
               f"{r.status_code} · {len(r.text)} bytes")
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode(errors="replace")
            print("  ---- 服务日志尾部 ----")
            print("\n".join("  " + l for l in out.strip().splitlines()[-14:]))
        except Exception:
            proc.kill()
        # 干净收尾验证：端口必须已释放，不留孤儿进程（带 token 探测，防止
        # 「服务还活着但 403 被误判为已退出」——豁免禁用下无 token 会得到 403）
        time.sleep(1)
        leftover = True
        try:
            r = httpx.get(f"{base}/api/status", timeout=2, headers=H)
            leftover = r.status_code == 200
        except Exception:
            leftover = False
        record("测试收尾无残留进程", not leftover, "端口已释放" if not leftover else "⚠ 端口仍在监听")
        # 条目3：logs/lecturelens.log 落盘断言（utf-8 + 三类关键字 + 无 key 泄漏）
        log_path = ROOT / "logs" / "lecturelens.log"
        try:
            ltext = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
            record("日志落盘含三类关键字",
                   all(k in ltext for k in ("model ready", "config 已更新", "笔记文件")),
                   f"{log_path.name} {len(ltext)} bytes")
            fake = os.environ.get("DEEPSEEK_API_KEY", "")
            record("日志无 key 泄漏", not fake or fake not in ltext,
                   "假/真 key 均不得出现" if fake else "环境无 DEEPSEEK_API_KEY，跳过泄漏比对")
        except Exception as e:
            record("日志落盘断言", False, f"异常: {e}")


# ---------------------------------------------------------------- 7. 流式管线
def test_streaming_core() -> None:
    """[7a] 处理器级：TTS 句子按 0.5s 块实时节奏喂真模型流式管线（CPU small）。
    验收（§2.2-11/12）：partial 首字早于全部音频可用（< 整句模式首显）、committed
    命中 KEYS、final meta 携带采集时间戳（audio_ts 贯穿）、partial 先于 final。"""
    print("\n[7a] 流式管线核心（TTS → 0.5s 块 → silero VAD → LocalAgreement-2，CPU small）")
    wav_path = ROOT / "scripts" / "_tts_stream.wav"
    try:
        audio = synth_tts(SENTENCE, wav_path)
        from asr import WhisperASR
        from scheduler import DecodeScheduler
        total_s = len(audio) / SR
        asr = WhisperASR(model_size="small", device="cpu")
        t0 = time.time()
        asr.transcribe(audio)          # 整句口径基准（beam1/vad_filter=True）
        t_whole = time.time() - t0
        t0 = time.time()
        asr.transcribe(audio, beam_size=5, vad_filter=False, word_timestamps=True)
        t_stream = time.time() - t0
        record("word_timestamps 耗时增幅（记录在案）", True,
               f"流式口径 {t_stream:.2f}s / 整句 {t_whole:.2f}s = "
               f"{t_stream / max(t_whole, 1e-9):.1f}x（同段音频，非阈值断言）")

        events: list[tuple] = []       # (时刻, "partial"/"final", 内容)
        finals: list[dict] = []
        s = DecodeScheduler(
            asr_provider=lambda: asr,
            paused=lambda: False,
            base_prompt=lambda: "",
            on_final=lambda text, meta: (finals.append({"text": text, "meta": meta}),
                                         events.append((time.time(), "final", text))),
            emit_partial=lambda p: events.append((time.time(), "partial", p["text"])),
        )
        s.start()
        seg_q: queue.Queue = queue.Queue()
        seg = MicSegmenter(seg_q, vad="silero", stream_q=queue.Queue())
        s.attach_stream_q(seg.stream_q)

        t_start = time.time()
        step = SR // 2                                  # 0.5s 块（§2.3 [7] 口径）
        n_speech = len(audio) // step
        for i in range(n_speech):                       # 语音块按实时节奏喂
            seg._consume_block(audio[i * step:(i + 1) * step])
            time.sleep(0.5)
        for i in range(16):                             # 尾部 1s 静音（快喂）触发 VAD 收尾
            seg._consume_block(np.zeros(BLOCK, dtype=np.float32))
        fed_s = time.time() - t_start                   # 全部音频「播完」的时刻
        while seg_q.qsize() and len(finals) < 4:        # 定稿段入调度器（携带采集时间戳）
            ts_audio, seg_audio = seg_q.get_nowait()
            s.enqueue_final(asr, seg_audio, ts_audio=ts_audio)
        deadline = time.time() + 60
        while time.time() < deadline and (s.pq.unfinished_tasks or seg_q.qsize()):
            time.sleep(0.2)
            while seg_q.qsize() and len(finals) < 4:
                ts_audio, seg_audio = seg_q.get_nowait()
                s.enqueue_final(asr, seg_audio, ts_audio=ts_audio)
        s.stop()

        partials = [e for e in events if e[1] == "partial"]
        record("流式 partial 有产出", len(partials) >= 1,
               f"{len(partials)} 条 partial · {len(finals)} 条 final")
        first_partial_s = (partials[0][0] - t_start) if partials else float("inf")
        record("partial 首字早于整句可用", first_partial_s < total_s,
               f"首条 partial @ {first_partial_s:.1f}s < 音频全长 {total_s:.1f}s"
               f"（整句模式首显 ≈ {total_s:.1f}s + 解码 {t_whole:.1f}s）")
        first_final_t = next((t for t, k, _ in events if k == "final"), None)
        order_ok = (bool(partials) and first_final_t is not None
                    and partials[0][0] <= first_final_t)
        record("消息序 partial→final", bool(order_ok),
               f"events={[k for _, k, _ in events][:6]}…")
        en_final = " ".join(f["text"] for f in finals).lower()
        hits = sum(k in en_final for k in KEYS)
        record("流式 committed 命中 KEYS", hits >= 2, f"命中 {hits}/{len(KEYS)} · {en_final[:60]}")
        ts_ok = all(isinstance(f["meta"].get("ts_audio"), float) for f in finals) and finals
        record("final meta 携带采集时间戳", bool(ts_ok),
               f"ts_audio={finals[0]['meta'].get('ts_audio') if finals else '无 final'}")
    except Exception as e:
        record("流式管线核心", False, f"异常: {e}")
    finally:
        wav_path.unlink(missing_ok=True)


def ws_connect(port: int, timeout: float = 5.0):
    """裸 socket 完成 WebSocket 握手并保持连接（返回 socket 或 None）。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        req = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(1024)
            if not chunk:
                return None
            resp += chunk
        return s if b"101" in resp.split(b"\r\n")[0] else None
    except Exception:
        return None


def ws_recv_text(s, timeout: float = 2.0) -> str | None:
    """读一帧服务端→客户端 text 帧（服务端帧不 mask；不处理分片）。超时返回 ""。"""
    try:
        s.settimeout(timeout)
        hdr = s.recv(2)
        if len(hdr) < 2:
            return None
        opcode, ln = hdr[0] & 0x0F, hdr[1] & 0x7F
        if ln == 126:
            ext = s.recv(2)
            ln = int.from_bytes(ext, "big")
        elif ln == 127:
            ext = s.recv(8)
            ln = int.from_bytes(ext, "big")
        data = b""
        while len(data) < ln:
            chunk = s.recv(ln - len(data))
            if not chunk:
                return None
            data += chunk
        return data.decode("utf-8") if opcode == 1 else ""
    except socket.timeout:
        return ""          # 无消息（无音频场景正常）
    except Exception:
        return None


def test_streaming_server(port: int = 8798) -> None:
    """[7b] 服务级：--streaming 子进程——status.streaming 布线、WS 收循环稳定、
    暂停/恢复。如实声明：无麦克风环境下服务无音频输入，partial→final 消息序的
    端到端断言由 [7a]（处理器级回调序）+ pytest payload 单测覆盖。"""
    print(f"\n[7b] 流式服务级 (--streaming 子进程, port={port}, CPU base 加速布线验证)")
    import httpx
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--port", str(port),
         "--streaming", "--model", "base", "--compute-type", "int8"],
        # 注意 --device 是麦克风设备索引（不是 ASR device）——[7b] 不传，让服务用
        # config 的麦克风配置；ASR 加速靠 --model base + config 的 device/compute_type。
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 180
        st = {}
        while time.time() < deadline:
            try:
                st = httpx.get(f"{base}/api/status", timeout=3).json()
                if st.get("asr") == "ready":
                    break
            except Exception:
                pass
            time.sleep(2)
        record("流式服务就绪", st.get("asr") == "ready", f"status={st.get('asr')}")
        record("status.streaming 布线开启", st.get("streaming") is True,
               f"streaming={st.get('streaming')}")

        s = ws_connect(port)
        # 条目19：本子进程未禁环回豁免——无 token 握手成功即覆盖「环回豁免放行」分支
        record("WS 握手（流式服务，无 token 环回豁免）", s is not None)
        stable = s is not None
        n_msg, detail = 0, "无音频场景以超时帧为常态"
        if s is not None:
            try:                      # 收循环 5s：收到消息必须可解析（协议兼容性）
                end = time.time() + 5
                while time.time() < end:
                    msg = ws_recv_text(s, timeout=1.0)
                    if msg is None:   # 连接被服务端关闭/帧解析失败
                        stable, detail = False, "连接被关闭/帧解析失败"
                        break
                    if msg:
                        n_msg += 1
                        json.loads(msg)
                if stable:
                    detail = f"收到 {n_msg} 条 WS 消息且全部 JSON 可解析"
            except Exception as e:
                stable, detail = False, f"收循环异常: {e}"
            finally:
                s.close()
        record("WS 收循环 5s 稳定", stable, detail)

        p1 = p2 = None
        try:
            httpx.post(f"{base}/api/pause", timeout=5)
            p1 = httpx.get(f"{base}/api/status", timeout=3).json().get("paused")
            httpx.post(f"{base}/api/resume", timeout=5)
            p2 = httpx.get(f"{base}/api/status", timeout=3).json().get("paused")
        except Exception:
            pass
        record("流式服务暂停/恢复", (p1 is True) and (p2 is False), f"pause={p1} → resume={p2}")
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode(errors="replace")
            print("  ---- 流式服务日志尾部 ----")
            print("\n".join("  " + l for l in out.strip().splitlines()[-8:]))
        except Exception:
            proc.kill()
        time.sleep(1)
        leftover = True
        try:
            httpx.get(f"{base}/api/status", timeout=2)
        except Exception:
            leftover = False
        record("流式服务收尾无残留进程", not leftover,
               "端口已释放" if not leftover else "⚠ 端口仍在监听")


# ----------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--translate", action="store_true", help="实调 DeepSeek 翻译")
    ap.add_argument("--server", action="store_true", help="起真实服务验证 HTTP/WS/热切换")
    ap.add_argument("--gpu", action="store_true", help="large-v3-turbo CUDA 基准")
    ap.add_argument("--vad-only", action="store_true",
                    help="只跑 [1]/[1b]/[1c] VAD 分句段（离线秒级回归；条目 2，供快速回归/未来 CI）")
    args = ap.parse_args()

    # 条目3：app 模块已 print→logger；smoke 复用同一控制台格式，但不写日志文件
    # （避免测试噪声混进 logs/lecturelens.log）
    setup_logging(ROOT / "logs", file=False)

    print(f"LectureLens smoke test — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    test_vad()
    test_overflow_cut()
    test_healthy()
    if args.vad_only:
        pass  # 只跑上面 [1] 三段；真模型/TTS/服务/翻译全部跳过
    else:
        en = test_asr()
        if args.gpu:
            test_gpu()
        if args.translate:
            test_translate(en)
        if args.server:
            test_server()
        test_streaming_core()   # [7a] 条目11/12：真模型流式管线（TTS + CPU small）
        test_streaming_server()  # [7b] 条目12：--streaming 服务级布线（子进程）

    print("\n========== 结果汇总 ==========")
    for name, ok, note in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else ""))
    fails = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"总计 {len(RESULTS)} 项，失败 {fails} 项")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
