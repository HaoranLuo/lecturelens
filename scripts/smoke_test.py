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
   模型热切换 + 暂停/恢复 + 笔记导出（条目19）+ 录制页选课程往返（§1：建课→设为当前→
   status.course/笔记归属→WS 'courses' 广播，课程表零残留）+ 课后改归属往返（§2：单篇/
   批量改写笔记头课程行→WS 'notes' 广播→400 文案，临时笔记零残留）+ 笔记管理全链
   （§4：重命名→编辑保存/过期指纹拒覆盖→软删除→回收站恢复/彻底删除，零残留）+
   导出位置自选（§3：设 export_dir→导出落盘 JSON+文件名 {课程}_{日期}→同名 -2→
   非法值 400→清回 null 仍走浏览器下载；临时目录与 config 值零残留）+
   可编辑摘要（§6：读原文/指纹→过期指纹 409→编辑就地写回→覆盖前备份 *.summary.bak.md
   且不进笔记列表；真跑一次 map-reduce 要 API key，归 --translate 范畴不进门禁）+
   下课一键归档（§5：暂停判据/当前笔记可判定/摘要分支判据/导出当前笔记落盘/二次确认
   开关可读；不在门禁里真触发 LLM 摘要——本机有 key，会花钱且分钟级）+
   手机扫码同屏（§7：临时把 config.json 的 lan_enabled 打开→子进程真绑 0.0.0.0（日志
   与经 LAN IP 访问双证）→ /api/lan-qr 本机给 enabled/active/url/svg、LAN IP 带合法
   token 仍 403、关掉开关即不再给码并提示重启生效→ status.ws_clients 连接 +1/断开回落；
   页面里含手机同屏面板（按钮/面板/取数端点/设备数/hash token 入口）；
   config.json 的 lan_enabled 收尾按原值还原——只动这一个键，auth_token 不碰）
7) 流式（条目11/12）：[7a] TTS 句子按 0.5s 块喂真模型流式管线——partial 首字早于整句可用、
   committed 命中 KEYS、final meta 带采集时间戳、word_timestamps 耗时增幅记录在案；
   [7b] --streaming 服务级子用例——status.streaming 布线 + WS 稳定 + 暂停/恢复
8)  D1 打包契约：打包入口=桌面壳 / 版本注入 branding.APP_VERSION（exe 属性）/ 桌面壳
   依赖收集清单↔构建后抽查清单一一对应 / 必需产物含 float.html+privacy.html / 随包模型
   默认 small（不跑 PyInstaller——分钟级构建归 scripts/build_exe.py 实跑）
全部通过 exit 0；任何一步失败 exit 1。"""
import argparse
import asyncio
import contextlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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


def _drain_child(proc, buf: list) -> None:
    """持续抽干子进程 stdout（后台线程）——**不抽干会把服务冻死**。

    实测（本机，2026-10-13）：`subprocess.PIPE` 的管道缓冲一满，服务进程就阻塞在
    write 上，**整个事件循环随之停摆**——客户端只看到请求一个个超时，服务日志停在
    某一行不再增长；此时端点本身是好的（独立探针 300/300 OK、p50 8ms），日志量一多
    （麦克风拾到环境音 → faster-whisper DEBUG 行暴涨）就会在跑的靠后段固定踩中。
    旧写法只在收尾 `proc.communicate()` 读一次，等于让管道在整段测试期间无人排空。"""
    import threading

    def _run():
        try:
            for line in proc.stdout:
                buf.append(line.decode(errors="replace"))
        except Exception:
            pass

    threading.Thread(target=_run, name="smoke-stdout-drain", daemon=True).start()


def ws_handshake_rejected(port: int) -> tuple[bool, str]:
    """条目19：无 token 握手必须被拒——断言非 101（计划口径）；返回 (判定, 响应首行)
    供 record 备注实况。"""
    line = _ws_status_line(port)
    return (bool(line) and "101" not in line), line


def _restore_export_dir(base: str, headers: dict, prev) -> bool:
    """把 config.export_dir 还原成 smoke 开始时的值（§3/§5 段的收尾）。

    **必须重试**：本机实测出现过单次请求超时（同一时刻端点本身是 ms 级，属环境级
    抖动），而收尾这步原本被 contextlib.suppress 包着——静默失败会把一个指向临时
    目录的 export_dir 留在真 config.json 里（下次运行/用户导出就跑到已删除的目录去，
    实测踩过）。返回是否还原成功；三次都失败就打一行显眼 WARNING，不静默。"""
    import httpx                      # 本模块的 httpx 是各函数内延迟导入的
    for i in range(3):
        try:
            r = httpx.post(f"{base}/api/config", timeout=10, headers=headers,
                           json={"export_dir": prev})
            if r.status_code == 200:
                return True
            last = f"HTTP {r.status_code}"
        except Exception as e:        # noqa: BLE001 —— 重试后再报
            last = f"{type(e).__name__}: {e}"
        if i == 2:
            print(f"  ⚠ WARNING 还原 export_dir 失败（{last}）——"
                  f"config.json 里可能残留 {prev!r}，请手工改回 null")
        time.sleep(0.5)
    return False


# —— §7（手机扫码同屏）子用例的脚手架 ——

def _ws_open(host: str, port: int, token: str | None = None):
    """裸 socket 完成 WebSocket 握手并**保持连接打开**（§7：ws_clients 计数要用它）。
    失败返回 None。与 _ws_status_line 同一握手写法，只是不立即 close。

    host 参数是 §7 评审修复（评审2①）后新增的：设备数只数**非环回**对端，所以用例
    必须能指定「经 127.0.0.1 连（本机窗口）」与「经 LAN IP 连（手机形态）」两种来源。"""
    try:
        s = socket.create_connection((host, port), timeout=5)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        qs = f"?token={token}" if token else ""
        req = (f"GET /ws{qs} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = s.recv(200).decode(errors="replace")
        if resp and "101" in resp.splitlines()[0]:
            return s
        s.close()
    except Exception:
        pass
    return None


def _ws_clients_now(base: str, headers: dict) -> int | None:
    """读一次 status.ws_clients（读不到/非整数 → None）。"""
    import httpx
    try:
        v = httpx.get(f"{base}/api/status", timeout=5, headers=headers).json()
        return v.get("ws_clients") if isinstance(v.get("ws_clients"), int) else None
    except Exception:
        return None


def _raw_get(port: int, path: str, extra_headers: dict | None = None) -> tuple[int, str]:
    """裸 socket 发一个 GET（**Host/Origin 完全自己控制**）→ (状态码, 响应体)。

    为什么不用 httpx：Host 头必须自己造（httpx 按 URL 填 Host，正是要被绕过的那个头）。
    评审1① 的回归用例用它发 `Host: evil.example` 的 rebinding 形态请求。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        hs = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
        hs.update(extra_headers or {})
        head = "".join(f"{k}: {v}\r\n" for k, v in hs.items())
        s.sendall(f"GET {path} HTTP/1.1\r\n{head}\r\n".encode())
        raw, buf = b"", b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw += chunk
        s.close()
        text = raw.decode("latin-1")
        code = int(text.split(" ", 2)[1]) if text.startswith("HTTP/") else 0
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else text
        return code, body
    except Exception:
        return 0, ""


def _wait_ws_clients(base: str, headers: dict, want, deadline_s: float = 10.0) -> int | None:
    """轮询 status.ws_clients 直到满足 want(n)；返回最后一次读到的值（读不到 None）。

    必须轮询：客户端 close() 之后服务端是**异步**感知断连的（hub.clients 要等
    ws_endpoint 的 receive 抛 WebSocketDisconnect 才 discard），秒级内的瞬时值不可靠。"""
    import httpx
    t0, last = time.time(), None
    while time.time() - t0 < deadline_s:
        try:
            v = httpx.get(f"{base}/api/status", timeout=5, headers=headers).json()
            last = v.get("ws_clients")
        except Exception:
            last = None
        if isinstance(last, int) and want(last):
            return last
        time.sleep(0.25)
    return last if isinstance(last, int) else None


def _flip_lan_enabled(value: bool):
    """把仓库 config.json 的 lan_enabled 临时改成 value，返回**改动前的原值**
    （键本来不存在 → 返回字符串 "absent"，还原时把它删掉）。

    §7 的「服务绑定 0.0.0.0」只在启动时读一次配置，所以子进程必须在**启动前**
    看到这个键——只能改文件（POST /api/config 是运行时热键，改不了已绑定的监听）。
    读不到/写不进（config.json 缺失或损坏）返回 None → 调用方跳过 LAN 子用例，
    绝不硬造一个 config.json（那会在用户机器上凭空造出配置文件）。"""
    p = ROOT / "config.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
    except Exception:
        return None
    prev = data.get("lan_enabled", "absent")
    data["lan_enabled"] = bool(value)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return None
    return prev


def _restore_lan_enabled(prev) -> bool:
    """把 lan_enabled 还原成 smoke 开始前的值（"absent" = 删掉这个键）。

    **只动这一个键，不整文件回滚**：config.json 里的 auth_token 是服务端首启写的
    （provision_token），整文件回滚会把 token 抹掉——用户手机上已配对的设备会突然
    失效（那是比 LAN 键残留更烦人的副作用）。三次重试 + 失败大声警告，与
    _restore_export_dir 同款纪律。"""
    p = ROOT / "config.json"
    last = "?"
    for _ in range(3):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json 不是 JSON 对象")
            if prev == "absent":
                data.pop("lan_enabled", None)
            else:
                data["lan_enabled"] = prev
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:        # noqa: BLE001 —— 重试后再报
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.5)
    print(f"  ⚠ WARNING 还原 lan_enabled 失败（{last}）——config.json 里可能残留 "
          f"lan_enabled=true（下次启动会对外监听），请手工改回 {prev!r}")
    return False


def test_server(port: int = 8799) -> None:
    print(f"\n[5] 真实服务子进程 (port={port}，环回豁免禁用=token 必带)")
    import httpx
    # 本机实测：整条 smoke 跑在「ASR 模型 + 麦克风 + 真翻译」的服务上时，**偶发**单次
    # 请求超时（同一时刻端点本身是 ms 级——探针实测 300/300 OK、p50 8ms、max 33ms；
    # 服务器日志显示请求其实已完成），一两次就落在这段新增的 §3/§5/§6 子用例上。
    # 给 httpx 的两个顶层入口包**一次**重试：覆盖这种瞬断，别让一次抖动把整条生命线
    # 判红（重试的请求都是幂等的；POST /api/notes/summarize 若首次已受理，重试会拿到
    # 409「进行中」，而本段并不调它）。
    _orig_get, _orig_post = httpx.get, httpx.post

    def _once_retry(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (httpx.TimeoutException, httpx.TransportError):
            time.sleep(0.5)
            return fn(*a, **kw)

    httpx.get = lambda *a, **kw: _once_retry(_orig_get, *a, **kw)
    httpx.post = lambda *a, **kw: _once_retry(_orig_post, *a, **kw)
    # 条目19：豁免禁用 → 本机对端按非环回处理，覆盖「无 token 非环回 403」分支
    # （§2.2-19）；只影响本子进程，[7b] 流式段不设——顺带覆盖环回豁免放行分支。
    env = dict(os.environ)
    env["LECTURELENS_AUTH_NO_LOCAL_EXEMPT"] = "1"
    # §7：LAN 子用例要在**本段子进程里**验证「开启后真绑 0.0.0.0」+「/api/lan-qr 仅本机」。
    # 监听地址只在启动时读一次配置，所以必须在 Popen 之前把 lan_enabled 写进
    # config.json（值在 finally 里按原值还原；见 _restore_lan_enabled 的只动单键说明）。
    # 本段因此会短暂对同网段开放 8799 端口（token 门禁照旧生效），收尾即关。
    lan_prev = _flip_lan_enabled(True)
    if lan_prev is None:
        print("  ⚠ WARNING 无法临时打开 config.json 的 lan_enabled（读不到/写不进）"
              "——跳过 §7 LAN 绑定与配对二维码子用例")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    out_buf: list[str] = []
    _drain_child(proc, out_buf)   # 见 _drain_child：不抽干管道会把服务冻死（实测）
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
        # §3：导出卡与 toast 动作按钮必须真的在**服务端发出去的页面**里（不是只在源码里）
        record("§3 设置页导出卡在页面里（选择/打开/改回浏览器下载）",
               all(k in html for k in ("setExportState", "btnExportPick", "btnExportOpen",
                                       "btnExportClear", "/api/export/open", "toastAct")),
               "导出卡 4 个入口 + toast 动作按钮 + 打开端点齐备")
        # §7：手机同屏面板必须真的在**服务端发出去的页面**里（不是只在源码里）——
        # 按钮 / 面板 / 取数端点 / 设备数 / 待机；服务端侧另有 /api/lan-qr 的子用例
        record("§7 手机同屏面板在页面里（扫码/URL/设备数/开关/排查）",
               all(k in html for k in ("btnLan", "lanMask", "lanQr", "/api/lan-qr",
                                       "ws_clients", "#t=", "手机同屏", "setLanChk")),
               "按钮 + 面板 + 取数端点 + 设备数 + hash token 入口齐备")
        # 波次一 A1：全选套装也必须真的在**服务端发出去的页面**里——三态全选（含半选态
        # indeterminate）/ 反选按钮 / Ctrl+A 与「切筛选即清空」两条接线。浏览器里的实际
        # 点选行为不进自动化（本仓无 JS 运行器，见 tests/test_note_select_all.py 抬头）：
        # 此处只保证「页面里确实带着这套控件与接线」，行为由真机手测覆盖。
        record("波次一 A1 全选套装在页面里（三态全选/反选/Ctrl+A/筛选清空）",
               all(k in html for k in ("nbSelAll", "nbBatchInvert", "nbBatchHint",
                                       "indeterminate", "pickAllVisible", "clearNotePick",
                                       "view-notes")),
               "工具条三态全选 + 反选 + Ctrl+A 闸门 + 筛选硬清空齐备")
        # B14：课程管理卡同样必须真的在**服务端发出去的页面**里——标题/统计/查看笔记
        # 跳转/删除确认的那句承诺（服务端行为由上面 B14 段真服务往返覆盖）。
        record("B14 课程管理卡在页面里（统计/查看笔记/删除确认写归属）",
               all(k in html for k in ("课程管理", "cStat", "条术语", "cOpen", "查看笔记",
                                       "openCourseNotes", "篇笔记将归入", "courseRows")),
               "标题 + 统计 + 跳转 + 删除确认文案齐备")
        # A2：批量打标签的按钮与面板（服务端行为见下面 §8 之后的 A2 真服务往返）
        record("A2 批量打标签面板在页面里（按钮/chips/自定义/方向/应用）",
               all(k in html for k in ("nbBatchTag", "nbTagBar", "nbTagModeSel",
                                       "nbTagChips", "nbTagNew", "nbTagApply",
                                       "/api/notes/tags/batch", "closeNbTagPanel")),
               "按钮 + 面板 + 模式选择 + chips + 自定义输入 + batch 端点齐备")
        # A3：批量导出 zip 的按钮与接线（服务端行为见下面 §3 段之后的 A3 真服务往返）
        record("A3 批量导出按钮在页面里（导出所选/落盘给打开文件夹）",
               all(k in html for k in ("nbBatchZip", "导出所选", "exportPickedZip",
                                       "/api/notes/export/batch", "exportFilename(r, '笔记导出'")),
               "按钮 + 端点 + 两种模式的反馈齐备")
        # B12：设置页五分区标签页（五格 + 卡片挂 data-set-tab + 记住上次分区 + 深链入口）
        record("B12 设置分区标签页在页面里（五格/记住上次/深链）",
               all(k in html for k in ('id="setTabs"', 'id="setTab-lesson"',
                                       'id="setTab-notes"', 'id="setTab-link"',
                                       'id="setTab-look"', 'id="setTab-system"',
                                       'data-set-tab="lesson"', 'data-set-tab="notes"',
                                       "ll_settings_tab", "lanGoSet",
                                       "openWorkbench('settings', { tab: 'link' })")),
               "五格 + 卡片分区标记 + localStorage 键 + 深链入口齐备")
        # 条目19：ws_handshake_ok 改带 token 版 + 新增 ws_handshake_rejected
        record("WebSocket 握手（带 token）", ws_handshake_ok(port, token))
        rej_ok, rej_line = ws_handshake_rejected(port)
        record("无 token WS 握手被拒（非 101）", rej_ok, f"响应: {rej_line[:48] or '空'}")

        # —— §7 手机扫码同屏：绑定 / 配对二维码 / 已连接设备数 ——
        # ① 设备计数（status.ws_clients）：先钉类型（整数、非布尔），再钉两条分支——
        #    **环回对端（本机窗口）不计**、经 LAN IP 来的（手机形态）计 1，断开后回落。
        #    评审2① 的事实：数 Hub.clients 会把桌面窗口/浏览器标签/悬浮条都算进去，
        #    于是面板那个数字恒 ≥1、「手机连上了吗」永远看不出来。
        import lan as lan_mod                    # noqa: PLC0415 —— 与其余段同款延迟导入
        lan_ip = lan_mod.primary_lan_ip() if lan_prev is not None else None
        st_ws = httpx.get(f"{base}/api/status", timeout=5, headers=H).json()
        n0 = st_ws.get("ws_clients")
        record("§7 status.ws_clients 是整数（设备计数字段）",
               isinstance(n0, int) and not isinstance(n0, bool),
               f"ws_clients={n0!r} ({type(n0).__name__})")
        rec_name = "§7 ws_clients 只数非环回设备（本机窗口不计 / LAN 计入）"
        if not isinstance(n0, int):
            record(rec_name, False, f"status 无 ws_clients: {n0!r}")
        else:
            ws_lo = _ws_open("127.0.0.1", port, token)
            samples = []
            t0 = time.time()
            while time.time() - t0 < 1.0:        # 本机窗口连上后，设备数必须**不动**
                samples.append(_ws_clients_now(base, H))
                time.sleep(0.2)
            ws_lan = _ws_open(lan_ip, port, token) if lan_ip else None
            n_lan = (_wait_ws_clients(base, H, lambda n: n >= n0 + 1)
                     if ws_lan is not None else None)
            for sock in (ws_lo, ws_lan):
                if sock is not None:
                    sock.close()
            n_back = _wait_ws_clients(base, H, lambda n: n <= n0)
            lo_ignored = bool(samples) and all(v == n0 for v in samples)
            lan_counted = (n_lan is not None and n_lan >= n0 + 1) if lan_ip else True
            record(rec_name,
                   ws_lo is not None and lo_ignored and lan_counted and n_back == n0,
                   f"环回 WS 期间恒 {n0}（采样 {samples}）· LAN WS → {n_lan} · 断开后 {n_back}"
                   + ("" if lan_ip else "（无 LAN 地址，LAN 分支跳过）"))
        # ② 绑定决策（启动时按 lan_enabled 决定）：日志里必须写明真的在 0.0.0.0 上
        bind_line = next((l for l in out_buf if "手机同屏已开启" in l), "")
        record("§7 服务按开关绑定 0.0.0.0（启动决策落在日志）",
               f"监听 0.0.0.0:{port}" in bind_line,
               (bind_line.strip()[:96] or "⚠ 日志里没有「手机同屏已开启」行"))
        if lan_prev is not None:
            if not lan_ip:
                record("§7 LAN 可达 + /api/lan-qr 子用例", True,
                       "⚠ 本机探测不到局域网地址（离线/无网卡）——跳过（手动清单项）")
            else:
                lan_base = f"http://{lan_ip}:{port}"
                # ③ 真的对外：经主网卡地址也能访问（127.0.0.1 能连不代表绑了 0.0.0.0）。
                #    自连自己的 LAN IP 不走网线，不受入站防火墙影响（本机实测 200）。
                try:
                    r_lan = httpx.get(f"{lan_base}/api/status?token={token}", timeout=5)
                    lan_ok, lan_note = r_lan.status_code == 200, f"HTTP {r_lan.status_code}"
                except Exception as e:            # noqa: BLE001 —— 失败也要如实记账
                    lan_ok, lan_note = False, f"{type(e).__name__}: {e}"
                record("§7 手机同屏：经 LAN IP 可访问（真绑 0.0.0.0）", lan_ok,
                       f"{lan_base}/api/status → {lan_note}")
                # ④ 配对二维码（仅本机可取）：URL 形如 http://<LAN-IP>:<port>/#t=<token>
                try:
                    r_qr = httpx.get(f"{base}/api/lan-qr", timeout=5, headers=H)
                    qr = r_qr.json() if r_qr.status_code == 200 else {}
                except Exception as e:            # noqa: BLE001
                    r_qr, qr = None, {"_err": str(e)}
                url = qr.get("url") or ""
                record("§7 /api/lan-qr（本机）回 enabled/active/url/svg",
                       r_qr is not None and r_qr.status_code == 200
                       and qr.get("enabled") is True and qr.get("active") is True
                       and url.startswith(f"http://{lan_ip}:{port}/#t=")
                       and token in url and "<svg" in (qr.get("svg") or ""),
                       f"HTTP {getattr(r_qr, 'status_code', '—')} · "
                       f"{url.split('#')[0] or qr.get('_err', '无 URL')} · "
                       f"svg {len(qr.get('svg') or '')}B")
                # ④b url 里的 token 必须**真的能用**：用 Python 侧同款解析（与前端
                #     /(?:^#|&)t=/ 同一口径）把它取出来，经 LAN IP 带上它请求 → 200。
                #     这是「真机扫码」里唯一可自动化的那部分（URL 与凭据的有效性）：
                #     取出来的 token 若对不上，说明二维码给的是废码。
                tok = lan_mod.token_from_hash(url)
                try:
                    r_tok = httpx.get(f"{lan_base}/api/status?token={tok}", timeout=5)
                    tok_code = r_tok.status_code
                except Exception as e:            # noqa: BLE001
                    tok_code = f"{type(e).__name__}: {e}"
                record("§7 url 含 #t= 且码里的 token 可用（LAN IP 带回 → 200）",
                       "#t=" in url and tok == token and tok_code == 200,
                       f"token 取自 hash：{len(tok)} 字符 · 经 LAN IP 鉴权 → {tok_code}")
                # ⑤ 仅本机：**带合法 token** 从 LAN IP 请求也必须 403（否则同网段能把
                #    URL 里的 token 取走）。403 必须来自本端点自己（error 文案可辨），
                #    不是中间件的「需要访问 token」。
                try:
                    r_nl = httpx.get(f"{lan_base}/api/lan-qr?token={token}", timeout=5)
                    nl_body = r_nl.json() if r_nl.status_code == 403 else {}
                    nl_code = r_nl.status_code
                except Exception as e:            # noqa: BLE001
                    nl_code, nl_body = None, {"_err": f"{type(e).__name__}: {e}"}
                record("§7 /api/lan-qr 仅本机（LAN IP + 合法 token 仍 403）",
                       nl_code == 403 and nl_body.get("ok") is False
                       and "仅本机" in (nl_body.get("error") or ""),
                       f"HTTP {nl_code} · {(nl_body.get('error') or nl_body.get('_err') or '')[:44]}")
                # ⑦ 评审1①：DNS-rebinding 形态必须被拒——**裸 socket 自己造 Host**
                #    （httpx 按 URL 填 Host，绕不过去）。修前实测这个请求曾 200，且
                #    响应里的 token 与 config 的 auth_token 逐字相等。
                #    放在 ⑥（关开关）**之前**：此时开关还开着，⑦b 才能验到「合法 Host
                #    照旧拿到 #t= 的二维码」。
                code_evil, body_evil = _raw_get(
                    port, "/api/lan-qr",
                    {"Host": "evil.example", "Origin": "http://evil.example"})
                record("§7 DNS-rebinding 形态被拒（Host: evil.example → 非 200 且不含 token）",
                       code_evil == 400 and token not in body_evil and "#t=" not in body_evil,
                       f"HTTP {code_evil} · 响应未含 token={token not in body_evil}")
                # ⑦b 别误伤：同一条裸 socket 路径、合法 Host（本机地址）照旧 200 + 给码。
                #     本段服务开着 LECTURELENS_AUTH_NO_LOCAL_EXEMPT=1（环回也要 token），
                #     所以这里必须自带 token（否则 403 是鉴权拦的，不是 Host 守卫）。
                code_ok, body_ok = _raw_get(port, f"/api/lan-qr?token={token}")
                record("§7 合法 Host 不受影响（裸 socket Host: 127.0.0.1 → 200 且给码）",
                       code_ok == 200 and "#t=" in body_ok,
                       f"HTTP {code_ok} · 含 #t= {'#t=' in body_ok}")
                # ⑥ 关掉开关（运行时口径）：**配置说关就不再给码**，并如实提示「重启后
                #    才停止对外」——面板文案与真实行为一致的最后一道锁。
                try:
                    r_off = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                                       json={"lan_enabled": False})
                    qr2 = httpx.get(f"{base}/api/lan-qr", timeout=5, headers=H).json()
                except Exception as e:            # noqa: BLE001
                    r_off, qr2 = None, {"_err": str(e)}
                record("§7 关掉开关后不再给二维码（并提示重启生效）",
                       r_off is not None and r_off.status_code == 200
                       and qr2.get("enabled") is False and qr2.get("active") is True
                       and qr2.get("url") is None and qr2.get("svg") is None
                       and qr2.get("restart_required") is True
                       and "重启" in (qr2.get("message") or ""),
                       f"HTTP {getattr(r_off, 'status_code', '—')} · "
                       f"{(qr2.get('message') or qr2.get('_err') or '')[:44]}")

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

        # —— 条目①（§1 录制页选课程）：工具条「课程」下拉的服务侧契约 ——
        # 下拉的三条数据通路全在真机验证：① POST /api/courses 建课（内联新建走它）→
        # ② POST /api/courses/active 设为当前（笔记归属 = retag_course，正文里能查到）→
        # ③ WS 'courses' 广播（另一窗口/另一标签页据此即时刷新下拉）+ status.course 同步。
        # 先暂停收音：改归属与「追加正文」并发会让「改头 / 追加切换标记」二选一变得随机。
        # 课程表零残留：finally 先回落原 active，再删掉临时课程。
        smoke_name = f"smoke-课程-{int(time.time())}"
        courses0 = httpx.get(f"{base}/api/courses", timeout=5, headers=H).json()
        httpx.post(f"{base}/api/pause", timeout=5, headers=H)
        ws = ws_connect(port, token=token)
        created_id = None
        try:
            r_new = httpx.post(f"{base}/api/courses", timeout=5, headers=H,
                               json={"name": smoke_name})
            created_id = ((r_new.json().get("course") or {}).get("id")
                          if r_new.status_code == 200 else None)
            record("§1 新建课程（下拉内联新建端点）", bool(created_id),
                   f"HTTP {r_new.status_code} · id={created_id}")
            r_act = httpx.post(f"{base}/api/courses/active", timeout=5, headers=H,
                               json={"active": created_id})
            act = r_act.json() if r_act.status_code == 200 else {}
            st_pick = httpx.get(f"{base}/api/status", timeout=3, headers=H).json()
            note_kind = {"empty": "空笔记改头", "marked": "记录中追加切换标记",
                         "none": "无需改动"}.get(act.get("note"), "未知")
            note_txt = httpx.get(f"{base}/api/notes", timeout=5, headers=H).text
            record("§1 设为当前课程（status.course + 笔记归属）",
                   act.get("active_name") == smoke_name
                   and st_pick.get("course") == smoke_name
                   and act.get("note") in ("empty", "marked", "none")
                   and smoke_name in note_txt,      # 归属落在**笔记文件**里（单一事实来源）
                   f"active_name={act.get('active_name')} · note={note_kind} · "
                   f"status.course={st_pick.get('course')}")
            got, t0 = None, time.time()          # WS：另一窗口改课/切课的同步来源
            while ws is not None and time.time() - t0 < 6:
                msg = ws_recv_text(ws, timeout=1.5)
                if msg is None:
                    break
                if not msg:
                    continue
                try:
                    frame = json.loads(msg)
                except Exception:
                    continue
                if frame.get("type") == "courses":
                    got = frame
                    if frame.get("active_name") == smoke_name:
                        break                        # 建课那次广播排在前面，别停在它上面
            record("§1 WS 'courses' 广播（多窗口同步）",
                   bool(got) and got.get("active_name") == smoke_name
                   and any(c.get("id") == created_id for c in got.get("courses") or []),
                   (f"active_name={got.get('active_name')} · "
                    f"courses={len(got.get('courses') or [])}") if got else "未收到 courses 帧")
        finally:
            if ws is not None:
                with contextlib.suppress(Exception):
                    ws.close()
            with contextlib.suppress(Exception):     # 复原：先回落原 active（笔记头随之复原）
                httpx.post(f"{base}/api/courses/active", timeout=5, headers=H,
                           json={"active": courses0.get("active")})
            if created_id:
                with contextlib.suppress(Exception):
                    httpx.delete(f"{base}/api/courses/{created_id}", timeout=5, headers=H)
            with contextlib.suppress(Exception):
                httpx.post(f"{base}/api/resume", timeout=5, headers=H)
        c_after = httpx.get(f"{base}/api/courses", timeout=5, headers=H).json()
        record("§1 课程表零残留（smoke 复原）",
               c_after.get("active") == courses0.get("active")
               and len(c_after.get("courses") or []) == len(courses0.get("courses") or []),
               f"active={c_after.get('active')} · 课程数 "
               f"{len(c_after.get('courses') or [])}（原 {len(courses0.get('courses') or [])}）")

        # —— B14 课程管理重做：统计字段 + 删课回溯历史笔记归属 ——
        # ① 统计：/api/courses 每行带 notes / terms（术语条数=读该课术语表真文件），
        #    临时建一份术语表与一篇笔记，核对两个数各自对上；
        # ② 删课承诺：DELETE 后该课历史笔记头里的课名必须变成「未分类」（设置页删除
        #    确认框写的就是这句「N 篇笔记将归入未分类」），且正文一字不丢。
        b14_course = f"smoke-课程管理-{int(time.time())}"
        b14_note = "lecture_19990101_0008.md"
        b14_gloss = f"smoke-b14-{int(time.time())}.md"
        b14_gpath = ROOT / "glossary" / b14_gloss
        b14_npath = ROOT / "transcripts" / b14_note
        b14_ok = False
        try:
            (ROOT / "glossary").mkdir(parents=True, exist_ok=True)
            b14_gpath.write_text("# smoke 临时术语表\nalpha = 甲\nbeta = 乙\n",
                                 encoding="utf-8")
            b14_npath.write_text(
                f"# 双语课堂笔记 19990101_0008\n\n"
                "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                f"> 课程：{b14_course}\n\n---\n\n- `10:00:00` b14 smoke line\n",
                encoding="utf-8")
            r_c = httpx.post(f"{base}/api/courses", timeout=5, headers=H,
                             json={"name": b14_course, "glossary": f"glossary/{b14_gloss}"})
            b14_id = ((r_c.json().get("course") or {}).get("id")
                      if r_c.status_code == 200 else None)
            row = next((c for c in httpx.get(f"{base}/api/courses", timeout=5,
                                             headers=H).json()["courses"]
                        if c.get("name") == b14_course), {})
            record("B14 课程统计（N 篇笔记 · M 条术语）",
                   bool(b14_id) and row.get("notes") == 1 and row.get("terms") == 2
                   and row.get("glossary_missing") is False,
                   f"notes={row.get('notes')} · terms={row.get('terms')} · "
                   f"missing={row.get('glossary_missing')}")
            r_del = httpx.delete(f"{base}/api/courses/{b14_id}", timeout=5, headers=H)
            body = r_del.json() if r_del.status_code == 200 else {}
            txt = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                            params={"name": b14_note}).text
            record("B14 删课程 → 历史笔记归入「未分类」（确认框的那句承诺）",
                   r_del.status_code == 200 and body.get("retagged") == 1
                   and "> 课程：未分类" in txt and "> 课程：" + b14_course not in txt
                   and "b14 smoke line" in txt,
                   f"HTTP {r_del.status_code} · retagged={body.get('retagged')} · "
                   f"正文保留={'b14 smoke line' in txt}")
            b14_ok = True
        finally:
            with contextlib.suppress(Exception):
                b14_npath.unlink()
            with contextlib.suppress(Exception):
                b14_gpath.unlink()
            if not b14_ok:                      # 中途失败也别把临时课程留在表里
                with contextlib.suppress(Exception):
                    cid = next((c["id"] for c in httpx.get(f"{base}/api/courses", timeout=5,
                                                           headers=H).json()["courses"]
                                if c.get("name") == b14_course), None)
                    if cid:
                        httpx.delete(f"{base}/api/courses/{cid}", timeout=5, headers=H)
        record("B14 测试零残留（临时笔记 + 术语表 + 课程）",
               not b14_npath.exists() and not b14_gpath.exists()
               and all(c.get("name") != b14_course
                       for c in httpx.get(f"{base}/api/courses", timeout=5,
                                          headers=H).json()["courses"]),
               "三件都清干净")

        # —— 条目②（§2 课后改归属）：笔记头课程行的定稿改写 + 批量 + 往返 ——
        # 真服务上跑完整往返：两篇临时笔记（本进程直接落盘，服务只按名字认库内文件）
        # → 单篇改写 → 改回「未分类」→ 批量两篇 → 再批量改回 → 400 文案 → WS 'notes'
        # 广播。收尾删掉临时笔记与临时课程（transcripts/ 与课程表零残留）。
        move_course = f"smoke-归属-{int(time.time())}"
        tmp_notes = [f"lecture_19990101_{i:04d}.md" for i in (0, 1)]
        ws_n = ws_connect(port, token=token)
        move_id = None
        try:
            for name in tmp_notes:
                stamp = name[len("lecture_"):-len(".md")]      # 19990101_0000
                (ROOT / "transcripts" / name).write_text(
                    f"# 双语课堂笔记 {stamp}\n\n"
                    "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                    "> 课程：未分类\n\n---\n\n- `10:00:00` smoke line\n",
                    encoding="utf-8")
            r_new = httpx.post(f"{base}/api/courses", timeout=5, headers=H,
                               json={"name": move_course})
            move_id = ((r_new.json().get("course") or {}).get("id")
                       if r_new.status_code == 200 else None)
            r_one = httpx.post(f"{base}/api/notes/course", timeout=5, headers=H,
                               json={"file": tmp_notes[0], "course": move_id})
            one = r_one.json() if r_one.status_code == 200 else {}
            txt = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                            params={"name": tmp_notes[0]}).text
            record("§2 单篇改归属（改笔记头 · 正文不动）",
                   one.get("action") == "replaced" and one.get("course") == move_course
                   and f"课程：{move_course}" in txt and "smoke line" in txt,
                   f"HTTP {r_one.status_code} · action={one.get('action')} · 头行已改")
            r_back = httpx.post(f"{base}/api/notes/course", timeout=5, headers=H,
                                json={"file": tmp_notes[0], "course": None})
            back = r_back.json() if r_back.status_code == 200 else {}
            txt_back = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                                 params={"name": tmp_notes[0]}).text
            record("§2 改回「未分类」（往返）",
                   back.get("course") == "未分类" and "课程：未分类" in txt_back,
                   f"course={back.get('course')}")
            bt = httpx.post(f"{base}/api/notes/course/batch", timeout=5, headers=H,
                            json={"files": tmp_notes, "course": move_id})
            bt_body = bt.json() if bt.status_code == 200 else {}
            bt2 = httpx.post(f"{base}/api/notes/course/batch", timeout=5, headers=H,
                             json={"files": tmp_notes, "course": None})
            bt2_body = bt2.json() if bt2.status_code == 200 else {}
            txts = [httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                              params={"name": n}).text for n in tmp_notes]
            record("§2 批量改归属（2 篇 → 再批量改回）",
                   bt_body.get("moved") == 2 and bt_body.get("failed") == []
                   and bt2_body.get("moved") == 2
                   and all("课程：未分类" in t for t in txts),
                   f"moved={bt_body.get('moved')} → 回 {bt2_body.get('moved')}")
            e1 = httpx.post(f"{base}/api/notes/course", timeout=5, headers=H,
                            json={"file": "lecture_19990101_9999.md", "course": move_id})
            e2 = httpx.post(f"{base}/api/notes/course", timeout=5, headers=H,
                            json={"file": tmp_notes[0], "course": "幽灵课"})
            record("§2 400 明确文案（文件/课程不存在）",
                   e1.status_code == 400 and "笔记不存在" in (e1.json().get("error") or "")
                   and e2.status_code == 400 and "课程不存在" in (e2.json().get("error") or ""),
                   f"HTTP {e1.status_code}/{e2.status_code}")
            got_note, t0 = None, time.time()      # WS：另一窗口笔记库的刷新来源
            while ws_n is not None and time.time() - t0 < 6:
                msg = ws_recv_text(ws_n, timeout=1.5)
                if msg is None:
                    break
                if not msg:
                    continue
                try:
                    frame = json.loads(msg)
                except Exception:
                    continue
                if frame.get("type") == "notes":
                    got_note = frame
                    if set(frame.get("files") or []) == set(tmp_notes):
                        break                        # 单篇那两条广播排在前面，别停在它们上
            record("§2 WS 'notes' 广播（多窗口同步）",
                   bool(got_note) and set(got_note.get("files") or []) == set(tmp_notes),
                   f"files={got_note.get('files')}" if got_note else "未收到 notes 帧")
        finally:
            if ws_n is not None:
                with contextlib.suppress(Exception):
                    ws_n.close()
            for name in tmp_notes:
                with contextlib.suppress(Exception):
                    (ROOT / "transcripts" / name).unlink()
            if move_id:
                with contextlib.suppress(Exception):
                    httpx.delete(f"{base}/api/courses/{move_id}", timeout=5, headers=H)
        c_now = httpx.get(f"{base}/api/courses", timeout=5, headers=H).json()
        leftover_notes = [n for n in tmp_notes if (ROOT / "transcripts" / n).exists()]
        record("§2 测试零残留（临时笔记 + 临时课程）",
               not leftover_notes
               and all(c.get("name") != move_course for c in c_now.get("courses") or []),
               "临时笔记已删 · 临时课程已删" if not leftover_notes else f"残留 {leftover_notes}")

        # —— 条目④（§4 笔记管理）：重命名 → 编辑 → 软删除 → 回收站恢复/彻底删除 ——
        # 两篇临时笔记走完整条链：改名（内容不改、派生文件跟着搬）→ 编辑保存（正文
        # 指纹乐观并发：指纹不对必须 409，别覆盖新内容）→ 软删（列表排除、回收站可见）
        # → 恢复一篇 / 彻底删除另一篇。收尾按名字逐个清（只动自己造的，不碰别人的）。
        mg_notes = [f"lecture_19990101_{i:04d}.md" for i in (2, 3)]
        renamed = "lecture_smoke-重命名.md"
        mg_trash = ROOT / "transcripts" / ".trash"
        mg_all = set(mg_notes + [renamed])
        try:
            for name in mg_notes:
                stamp = name[len("lecture_"):-len(".md")]
                (ROOT / "transcripts" / name).write_text(
                    f"# 双语课堂笔记 {stamp}\n\n"
                    "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                    "> 课程：未分类\n\n---\n\n- `10:00:00` smoke manage line\n",
                    encoding="utf-8")
            # 派生文件：重命名/软删都该带着它一起走
            (ROOT / "transcripts" / f"{mg_notes[0][:-3]}.times.json").write_text(
                "[]", encoding="utf-8")
            r_ren = httpx.post(f"{base}/api/notes/rename", timeout=5, headers=H,
                               json={"file": mg_notes[0], "to": "smoke-重命名"})
            ren = r_ren.json() if r_ren.status_code == 200 else {}
            mg_all.add(f"{mg_notes[0][:-3]}.times.json")
            mg_all.add(f"{renamed[:-3]}.times.json")
            txt_ren = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                                params={"name": renamed}).text
            record("§4 重命名（内容不改 + 派生文件跟着改名）",
                   ren.get("to") == renamed and ren.get("sidecars") == 1
                   and "smoke manage line" in txt_ren
                   and (ROOT / "transcripts" / f"{renamed[:-3]}.times.json").is_file(),
                   f"to={ren.get('to')} · sidecars={ren.get('sidecars')}")
            raw = httpx.get(f"{base}/api/notes/raw", timeout=5, headers=H,
                            params={"name": renamed}).json()
            edited = raw["text"].replace("smoke manage line", "edited by smoke")
            r_edit = httpx.put(f"{base}/api/notes/{renamed}", timeout=5, headers=H,
                               json={"text": edited, "base_sha": raw["sha"]})
            r_stale = httpx.put(f"{base}/api/notes/{renamed}", timeout=5, headers=H,
                                json={"text": "覆盖试试", "base_sha": raw["sha"]})
            back = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                             params={"name": renamed}).text
            record("§4 编辑保存（正文指纹；指纹过期拒覆盖）",
                   r_edit.status_code == 200 and "edited by smoke" in back
                   and r_stale.status_code == 409 and "覆盖试试" not in back,
                   f"保存 HTTP {r_edit.status_code} · 过期指纹 HTTP {r_stale.status_code}")
            r_del = httpx.post(f"{base}/api/notes/delete", timeout=5, headers=H,
                               json={"files": [renamed, mg_notes[1]]})
            dele = r_del.json() if r_del.status_code == 200 else {}
            listed = [n["name"] for n in
                      httpx.get(f"{base}/api/notes/list", timeout=5, headers=H).json()["notes"]]
            trash_names = [n["name"] for n in
                           httpx.get(f"{base}/api/notes/trash", timeout=5, headers=H).json()["notes"]]
            record("§4 软删除（列表排除 + 回收站可见）",
                   dele.get("trashed") == 2
                   and renamed not in listed and mg_notes[1] not in listed
                   and renamed in trash_names and mg_notes[1] in trash_names,
                   f"trashed={dele.get('trashed')} · 列表已排除 · 回收站 {len(trash_names)} 篇")
            r_back = httpx.post(f"{base}/api/notes/trash/restore", timeout=5, headers=H,
                                json={"files": [mg_notes[1]]})
            restored_now = [n["name"] for n in
                            httpx.get(f"{base}/api/notes/list", timeout=5, headers=H).json()["notes"]]
            r_gone = httpx.post(f"{base}/api/notes/trash/purge", timeout=5, headers=H,
                                json={"files": [renamed]})
            trash_left = [n["name"] for n in
                          httpx.get(f"{base}/api/notes/trash", timeout=5, headers=H).json()["notes"]]
            record("§4 回收站：恢复一篇 / 彻底删除另一篇",
                   r_back.json().get("restored") == 1 and mg_notes[1] in restored_now
                   and r_gone.json().get("purged") == 1 and renamed not in trash_left
                   and not (mg_trash / renamed).exists(),
                   f"restored={r_back.json().get('restored')} · "
                   f"purged={r_gone.json().get('purged')}")
        finally:
            for name in mg_all | {f"{n[:-3]}.times.json" for n in mg_all}:
                for d in (ROOT / "transcripts", mg_trash):
                    with contextlib.suppress(Exception):
                        (d / name).unlink()
            if (ROOT / "transcripts" / ".trash").is_dir():
                with contextlib.suppress(OSError):
                    (ROOT / "transcripts" / ".trash").rmdir()   # 只在空目录时成功
        mg_left = [n for n in mg_all
                   if (ROOT / "transcripts" / n).exists() or (mg_trash / n).exists()]
        record("§4 测试零残留（临时笔记 + 回收站）",
               not mg_left, "按名字逐个清干净" if not mg_left else f"残留 {mg_left}")

        # —— 评审 M2：搬运绝不覆盖（回收站已有同名 → 删除必须失败且两份都完好）——
        clash = "lecture_19990101_0006.md"
        clash_path = ROOT / "transcripts" / clash
        clash_trash = mg_trash / clash
        try:
            clash_path.write_text("FIRST-VERSION\n", encoding="utf-8")
            httpx.post(f"{base}/api/notes/delete", timeout=5, headers=H,
                       json={"files": [clash]})
            first_in_trash = clash_trash.read_text(encoding="utf-8") \
                if clash_trash.is_file() else ""
            clash_path.write_text("SECOND-VERSION\n", encoding="utf-8")   # 同名再来一篇
            r_clash = httpx.post(f"{base}/api/notes/delete", timeout=5, headers=H,
                                 json={"files": [clash]}).json()
            record("§2/M2 回收站同名不做静默覆盖",
                   first_in_trash.startswith("FIRST-VERSION")
                   and r_clash.get("trashed") == 0 and len(r_clash.get("failed") or []) == 1
                   and "未删除也未覆盖" in (r_clash["failed"][0].get("error") or "")
                   and clash_trash.read_text(encoding="utf-8").startswith("FIRST-VERSION"),
                   f"trashed={r_clash.get('trashed')} · 回收站那份仍是最初版本")
        finally:
            for f in (clash_path, clash_trash):
                with contextlib.suppress(Exception):
                    f.unlink()

        # —— 条目⑧（§8 标签）：写入 → 读回 → 检索命中 → 筛选 → 清空 ——
        # 标签存在**笔记文件头块**的一行（存文件不存 sidecar）：故"写入读回"要用
        # 服务端读文件的那条路（/api/notes/file）核对，而不是只看响应体。
        tag_note = "lecture_19990101_0004.md"
        tag_path = ROOT / "transcripts" / tag_note
        try:
            tag_path.write_text(
                "# 双语课堂笔记 19990101_0004\n\n"
                "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                "> 课程：未分类\n\n---\n\n- `10:00:00` tag smoke line\n",
                encoding="utf-8")
            r_tag = httpx.post(f"{base}/api/notes/tags", timeout=5, headers=H,
                               json={"file": tag_note,
                                     "tags": ["重点", "没听懂", "smoke标签"]})
            tag_body = r_tag.json() if r_tag.status_code == 200 else {}
            tag_text = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                                 params={"name": tag_note}).text
            tag_lines = tag_text.splitlines()
            i_course = tag_lines.index("> 课程：未分类")
            got_tags = httpx.get(f"{base}/api/notes/tags", timeout=5, headers=H,
                                 params={"name": tag_note}).json()
            record("§8 标签写入/读回（行在课程行下方 + GET 读回）",
                   r_tag.status_code == 200 and tag_body.get("action") == "inserted"
                   and tag_lines[i_course + 1] == "> 标签：#重点 #没听懂 #smoke标签"
                   and got_tags.get("tags") == ["重点", "没听懂", "smoke标签"],
                   f"action={tag_body.get('action')} · 行位正确 · 读回 {got_tags.get('tags')}")
            listed = httpx.get(f"{base}/api/notes/list", timeout=5, headers=H).json()
            mine = [n for n in listed["notes"] if n["name"] == tag_note]
            counts = {t["tag"]: t["count"] for t in listed.get("tag_counts") or []}
            by_tag = httpx.get(f"{base}/api/notes/list", timeout=5, headers=H,
                               params={"tag": "重点"}).json()
            # 检索命中**标签名**：用只可能出现在标签里的词（真实笔记正文不会含它）
            by_q = httpx.get(f"{base}/api/notes/list", timeout=5, headers=H,
                             params={"q": "smoke标签"}).json()
            record("§8 索引 tags + 计数 + 按标签筛选 + 检索命中标签名",
                   bool(mine) and mine[0]["tags"] == ["重点", "没听懂", "smoke标签"]
                   and counts.get("重点", 0) >= 1
                   and any(n["name"] == tag_note for n in by_tag["notes"])
                   and [n["name"] for n in by_q["notes"]] == [tag_note],
                   f"tags={mine[0]['tags'] if mine else None} · 计数={counts.get('重点')} · "
                   f"筛选 {by_tag.get('matched')} 篇 · 检索命中 1 篇")
            r_clear = httpx.post(f"{base}/api/notes/tags", timeout=5, headers=H,
                                 json={"file": tag_note, "tags": []})
            cleared = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                                params={"name": tag_note}).text
            record("§8 清空标签（tags=[] 删掉整行）",
                   r_clear.json().get("action") == "removed" and "> 标签" not in cleared,
                   f"action={r_clear.json().get('action')}")
            # —— 评审 M3：换行家族/控制字符标签必须 400（不得写坏笔记头）——
            r_bad_tag = httpx.post(f"{base}/api/notes/tags", timeout=5, headers=H,
                                   json={"file": tag_note, "tags": ["a\u2028b"]})
            record("§8/M3 非法标签（\\u2028 等）400 且不落盘",
                   r_bad_tag.status_code == 400
                   and "非法标签" in (r_bad_tag.json().get("error") or "")
                   and "> 标签" not in httpx.get(f"{base}/api/notes/file", timeout=5,
                                                 headers=H,
                                                 params={"name": tag_note}).text,
                   f"HTTP {r_bad_tag.status_code}")
            # —— 评审 M4：行级改写不翻转行尾（CRLF 笔记不许变成 \r\r\n 或裸 LF）——
            bytes_after = tag_path.read_bytes()
            record("§8/M4 行级改写行尾保真（无 \\r\\r\\n / 无裸 LF）",
                   b"\r\r\n" not in bytes_after
                   and bytes_after.count(b"\n") == bytes_after.count(b"\r\n"),
                   f"CRLF={bytes_after.count(b'\r\n')} · 裸 LF="
                   f"{bytes_after.count(b'\n') - bytes_after.count(b'\r\n')}")
        finally:
            for n in (tag_note, f"{tag_note[:-3]}.times.json"):
                with contextlib.suppress(Exception):
                    (ROOT / "transcripts" / n).unlink()
        record("§8 测试零残留（临时笔记）",
               not tag_path.exists(), "临时笔记已删" if not tag_path.exists()
               else f"残留 {tag_note}")

        # —— A2 批量打标签：一次给两篇加一组标签 → 读回（以文件为准）→ 再移除一条 ——
        # 与 §8 的单篇链分开成两块：这里验的是**批量**语义（整单校验 + 逐篇合并 + 计数
        # + 单次广播）。读回一律走 /api/notes/file（服务端读盘），不看响应体自说自话。
        btag_notes = ["lecture_19990101_0009.md", "lecture_19990101_0010.md"]
        try:
            for i, name in enumerate(btag_notes):
                (ROOT / "transcripts" / name).write_text(
                    f"# 双语课堂笔记 19990101_00{9 + i:02d}\n\n"
                    "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                    "> 课程：未分类\n\n---\n\n- `10:00:00` batch tag smoke line\n",
                    encoding="utf-8")
            r_bt = httpx.post(f"{base}/api/notes/tags/batch", timeout=10, headers=H,
                              json={"files": btag_notes, "tags": ["重点", "smoke批"]})
            bt = r_bt.json() if r_bt.status_code == 200 else {}
            txts = [httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                              params={"name": n}).text for n in btag_notes]
            record("A2 批量打标签（两篇一次加上 → 读回以文件为准）",
                   r_bt.status_code == 200 and bt.get("changed") == 2
                   and bt.get("failed") == []
                   and all("> 标签：#重点 #smoke批" in t for t in txts),
                   f"changed={bt.get('changed')} · tags={bt.get('tags')}")
            r_bt2 = httpx.post(f"{base}/api/notes/tags/batch", timeout=10, headers=H,
                               json={"files": btag_notes, "tags": ["smoke批"],
                                     "mode": "remove"})
            bt2 = r_bt2.json() if r_bt2.status_code == 200 else {}
            t2 = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                           params={"name": btag_notes[0]}).text
            t_other = httpx.get(f"{base}/api/notes/file", timeout=5, headers=H,
                                params={"name": btag_notes[1]}).text
            r_bt3 = httpx.post(f"{base}/api/notes/tags/batch", timeout=10, headers=H,
                               json={"files": [btag_notes[0], "lecture_19990101_9999.md"],
                                     "tags": ["重点"]})
            record("A2 批量移除 + 整单校验（一篇不存在 → 400 且不半批写入）",
                   bt2.get("changed") == 2 and "> 标签：#重点" in t2
                   and "smoke批" not in t2
                   and r_bt3.status_code == 400
                   and "笔记不存在" in (r_bt3.json().get("error") or "")
                   and "> 标签：#重点" in t_other,     # 400 那次没动任何文件
                   f"remove changed={bt2.get('changed')} · "
                   f"整单 400 HTTP {r_bt3.status_code}")
        finally:
            for n in btag_notes:
                with contextlib.suppress(Exception):
                    (ROOT / "transcripts" / n).unlink()
        bt_left = [n for n in btag_notes if (ROOT / "transcripts" / n).exists()]
        record("A2 测试零残留（临时笔记）", not bt_left,
               "临时笔记已删" if not bt_left else f"残留 {bt_left}")

        # —— 条目③（§3 导出位置自选 + 文件名）：落盘往返 + 清回后仍走浏览器下载 ——
        # 真服务上跑完整链：设 export_dir（临时目录，成功即自动建目录）→ 导出 md 断言
        # JSON {ok, path} 与盘上文件（名 = {课程}_{日期}.md）→ 同名再导得 -2（绝不覆盖）
        # → 相对路径 400（配置零污染）→ 清回 null 后导出**回到浏览器下载**（attachment
        # 头 + 文件本体）。收尾：删临时目录、把 export_dir 还原成 smoke 开始时的值
        # （真机 config.json 零残留）。/api/export/open 只验「未设置 → 409」这一支——
        # 设了目录去调它会真在开发者机器上弹资源管理器窗口（真机手测项）。
        # 整段裹 try/except：本机偶发单次请求超时（端点本身 ms 级），不能因此把整条
        # smoke 崩掉——记一条 ❌ 继续跑后面的子用例（收尾的 config 还原另有重试）。
        ex_note = "lecture_19990101_0005.md"
        ex_stamp = "19990101_0005"
        ex_course = f"smoke-导出-{int(time.time())}"
        ex_dir = Path(tempfile.mkdtemp(prefix="lecturelens-smoke-export-"))
        ex_prev = httpx.get(f"{base}/api/config", timeout=5, headers=H).json().get("export_dir")
        try:
            (ROOT / "transcripts" / ex_note).write_text(
                f"# 双语课堂笔记 {ex_stamp}\n\n"
                "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                f"> 课程：{ex_course}\n\n---\n\n- `10:00:00` export smoke line\n",
                encoding="utf-8")
            r_set = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                               json={"export_dir": str(ex_dir)})
            record("§3 设置导出位置（白名单键 + 自动建目录）",
                   r_set.status_code == 200 and r_set.json().get("ok")
                   and ex_dir.is_dir(),
                   f"HTTP {r_set.status_code} · {ex_dir.name}")
            r_exp = httpx.get(f"{base}/api/notes/export", timeout=15, headers=H,
                              params={"name": ex_note, "fmt": "md"})
            exp = r_exp.json() if r_exp.status_code == 200 else {}
            exp_path = Path(exp.get("path") or "")
            want_name = f"{ex_course}_{ex_stamp[:8]}.md"
            record("§3 导出直接落盘（JSON {ok, path} + 文件名 {课程}_{日期}）",
                   exp.get("ok") is True and exp.get("name") == want_name
                   and exp_path.is_file()
                   and "export smoke line" in exp_path.read_text(encoding="utf-8"),
                   f"{exp.get('name')} · {exp.get('bytes')} 字节")
            r_exp2 = httpx.get(f"{base}/api/notes/export", timeout=15, headers=H,
                               params={"name": ex_note, "fmt": "md"}).json()
            record("§3 同名不覆盖（自动 -2，两份都在）",
                   r_exp2.get("name") == want_name[:-3] + "-2.md"
                   and exp_path.is_file() and Path(r_exp2.get("path") or "").is_file(),
                   f"{want_name} → {r_exp2.get('name')}")
            # B1：课程非空 → 落进 <导出目录>/<课程名>/ 子目录（"对应的笔记本"的物理版）。
            # 断言用服务端回的 path 反推父目录，而不是只看文件名——回归时能立刻看出
            # 「文件跑回根目录平铺」这类退化。收尾由 ex_dir 的 rmtree 一并清掉（含子目录）。
            record("B1 导出落进课程子目录（<导出目录>/<课程名>/）",
                   exp_path.parent == ex_dir / ex_course and exp_path.parent.is_dir()
                   and not (ex_dir / want_name).exists(),
                   f"{exp_path.parent.name}/{exp_path.name}")
            # B1：课程为空（未分类）→ 仍落导出根目录，不凭空建「未分类」文件夹
            (ROOT / "transcripts" / ex_note).write_text(
                f"# 双语课堂笔记 {ex_stamp}\n\n"
                "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                "> 课程：未分类\n\n---\n\n- `10:00:00` export smoke line\n",
                encoding="utf-8")
            r_exp3 = httpx.get(f"{base}/api/notes/export", timeout=15, headers=H,
                               params={"name": ex_note, "fmt": "md"})
            exp3 = r_exp3.json() if r_exp3.status_code == 200 else {}
            p3 = Path(exp3.get("path") or "")
            record("B1 课程为空 → 仍落导出根目录（不建「未分类」文件夹）",
                   r_exp3.status_code == 200 and p3.parent == ex_dir and p3.is_file()
                   and not (ex_dir / "未分类").exists(),
                   f"{p3.name} · 根目录子项={sorted(q.name for q in ex_dir.iterdir())}")
            # —— A3 批量导出 zip：真服务打包 → 包可解 → 条目数/条目名/内容断言 ——
            # 两篇**同课同日**的笔记 → 条目名必然重名，正好连 **包内避让** 一起验
            # （否则解压时后者盖前者，用户静默少文件）。ex_note 的课程在前一条 B1
            # 子用例里被改成了「未分类」，这里先写回 ex_course，两篇才真的同名。
            b3_note = "lecture_19990101_0011.md"
            try:
                (ROOT / "transcripts" / ex_note).write_text(
                    f"# 双语课堂笔记 {ex_stamp}\n\n"
                    "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                    f"> 课程：{ex_course}\n\n---\n\n- `10:00:00` export smoke line\n",
                    encoding="utf-8")
                (ROOT / "transcripts" / b3_note).write_text(
                    "# 双语课堂笔记 19990101_0011\n\n"
                    "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                    f"> 课程：{ex_course}\n\n---\n\n- `10:00:00` zip smoke line二\n",
                    encoding="utf-8")
                r_zip = httpx.post(f"{base}/api/notes/export/batch", timeout=30, headers=H,
                                   json={"files": [ex_note, b3_note], "fmt": "md"})
                zj = r_zip.json() if r_zip.status_code == 200 else {}
                zpath = Path(zj.get("path") or "")
                import zipfile as zipfile_mod     # noqa: PLC0415 —— 与其余段同款延迟导入
                names: list = []
                inner: dict = {}
                if zpath.is_file():
                    with zipfile_mod.ZipFile(zpath) as zf:
                        names = zf.namelist()
                        inner = {n: zf.read(n).decode("utf-8") for n in names}
                want_entries = [f"{ex_course}_19990101.md",
                                f"{ex_course}_19990101-2.md"]      # 同课同日 → -2 避让
                record("A3 批量导出 zip（落盘 → 可解 → 条目名避让/内容对得上）",
                       r_zip.status_code == 200 and zj.get("count") == 2
                       and zpath.is_file() and zpath.parent == ex_dir
                       and names == want_entries
                       and any("export smoke line" in t for t in inner.values())
                       and any("zip smoke line二" in t for t in inner.values()),
                       f"count={zj.get('count')} · {zj.get('name')} · 条目={names}")
                r_zip_empty = httpx.post(f"{base}/api/notes/export/batch", timeout=10,
                                         headers=H, json={"files": []})
                record("A3 空选 400（不空转、不产出空包）",
                       r_zip_empty.status_code == 400
                       and "不能为空" in (r_zip_empty.json().get("error") or ""),
                       f"HTTP {r_zip_empty.status_code}")
            finally:
                with contextlib.suppress(Exception):
                    (ROOT / "transcripts" / b3_note).unlink()
            record("A3 测试零残留（临时笔记）",
                   not (ROOT / "transcripts" / b3_note).exists(), "临时笔记已删")
            r_clash = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                                 json={"export_dir": str(ex_dir / "a<b")})
            record("§3 非法导出位置 400（非法字符，配置零污染）",
                   r_clash.status_code == 400
                   and "export_dir" in (r_clash.json().get("error") or "")
                   and httpx.get(f"{base}/api/config", timeout=5,
                                 headers=H).json().get("export_dir") == str(ex_dir),
                   f"HTTP {r_clash.status_code}")
            # 评审①（已实证）：`\\?\C:\Windows\Temp` 曾能整条绕过系统目录黑名单并真的
            # 写盘成功；UNC 则在**校验阶段**就会向该主机发起一次带当前用户凭据的 SMB
            # 连接（实测 WinError 53）。两者现在都必须 400，且配置零污染。
            r_ext = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                               json={"export_dir": "\\\\?\\C:\\Windows\\Temp"})
            r_unc = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                               json={"export_dir": "\\\\192.0.2.9\\share\\导出"})
            r_dev = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                               json={"export_dir": "\\\\.\\PhysicalDrive0"})
            still = httpx.get(f"{base}/api/config", timeout=5,
                              headers=H).json().get("export_dir")
            record("§3 扩展/设备前缀与 UNC 一律 400（评审①：绕过与 SMB 探测都堵死）",
                   r_ext.status_code == 400 and r_unc.status_code == 400
                   and r_dev.status_code == 400 and still == str(ex_dir)
                   and "前缀" in (r_ext.json().get("error") or "")
                   and "UNC" in (r_unc.json().get("error") or ""),
                   f"HTTP {r_ext.status_code}/{r_unc.status_code}/{r_dev.status_code} · "
                   f"配置未被改（={still}）")
            r_open_unset = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                                      json={"export_dir": None})
            r_open = httpx.post(f"{base}/api/export/open", timeout=5, headers=H)
            record("§3 未设置时「打开文件夹」409（不空转；仅本机放行见 pytest）",
                   r_open_unset.status_code == 200 and r_open.status_code == 409,
                   f"HTTP {r_open.status_code}")
            r_dl = httpx.get(f"{base}/api/notes/export", timeout=15, headers=H,
                             params={"name": ex_note, "fmt": "md"})
            record("§3 未设置=浏览器下载（attachment + 文件本体，恢复零变化）",
                   r_dl.status_code == 200
                   and "attachment" in r_dl.headers.get("content-disposition", "")
                   and "export smoke line" in r_dl.text,
                   f"HTTP {r_dl.status_code} · "
                   f"{r_dl.headers.get('content-type', '').split(';')[0]}")
        finally:
            with contextlib.suppress(Exception):
                (ROOT / "transcripts" / ex_note).unlink()
            _restore_export_dir(base, H, ex_prev)
            with contextlib.suppress(Exception):
                shutil.rmtree(ex_dir, ignore_errors=True)
        now_dir = httpx.get(f"{base}/api/config", timeout=10, headers=H).json().get("export_dir")
        record("§3 测试零残留（临时导出目录 + config 还原）",
               now_dir == ex_prev and not ex_dir.exists(),
               f"export_dir={now_dir!r}（原 {ex_prev!r}）")

        # —— 条目⑥（§6 可编辑摘要）：读原文 → 就地写回 → 指纹挡住过期保存 ——
        # 真服务上跑编辑往返：临时笔记 + 临时摘要（摘要文件由本进程直接落盘，等价于
        # 「AI 已经生成过」；真跑一次 map-reduce 要 API key 且花钱，属 --translate 的
        # 范畴，不进门禁）。断言：GET 读数与指纹 → PUT 过期指纹 409 且文件不动 →
        # PUT 正确指纹 200 且**盘上文件**就是新内容 → 列表徽章 has_summary → 摘要与
        # 备份都不出现在笔记列表里（派生物不进库）。
        # 覆盖前备份（*.summary.bak.md）在 pytest 里用假 translator 跑真 summarize()
        # 全链覆盖；这里用 summarize.backup_existing（同一实现）在真盘上验证命名与内容，
        # 并由**服务端**的 /api/notes/list 确认备份文件不进列表。
        sm_note = "lecture_19990101_0007.md"
        sm_stem = sm_note[:-3]
        sm_sum = sm_stem + ".summary.md"
        sm_bak = sm_stem + ".summary.bak.md"
        try:
            (ROOT / "transcripts" / sm_note).write_text(
                "# 双语课堂笔记 19990101_0007\n\n"
                "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
                "> 课程：未分类\n\n---\n\n- `10:00:00` summary smoke line\n",
                encoding="utf-8")
            r0 = httpx.get(f"{base}/api/notes/summary", timeout=5, headers=H,
                           params={"name": sm_note}).json()
            record("§6 还没有摘要时如实回 exists=false（不是错误）",
                   r0.get("ok") is True and r0.get("exists") is False
                   and r0.get("summary") == sm_sum and r0.get("backup") == sm_bak,
                   f"summary={r0.get('summary')}")
            (ROOT / "transcripts" / sm_sum).write_text("## 概览\nAI 原始版\n",
                                                      encoding="utf-8")
            g1 = httpx.get(f"{base}/api/notes/summary", timeout=5, headers=H,
                           params={"name": sm_note}).json()
            record("§6 读摘要原文（text + sha + 备份名）",
                   g1.get("exists") is True and "AI 原始版" in (g1.get("text") or "")
                   and bool(g1.get("sha")) and g1.get("has_backup") is False,
                   f"{g1.get('size')} 字节 · sha={str(g1.get('sha'))[:8]}…")
            r_stale = httpx.put(f"{base}/api/notes/summary", timeout=5, headers=H,
                                json={"name": sm_note, "text": "过期编辑器提交\n",
                                      "base_sha": "0" * 40})
            disk_now = (ROOT / "transcripts" / sm_sum).read_text(encoding="utf-8")
            record("§6 过期指纹 409 且盘上文件不动",
                   r_stale.status_code == 409 and "AI 原始版" in disk_now
                   and "过期编辑器" not in disk_now,
                   f"HTTP {r_stale.status_code}")
            edited = "## 概览\n人工修订版（smoke）\n\n## 要点\n- 手写要点\n"
            r_put = httpx.put(f"{base}/api/notes/summary", timeout=5, headers=H,
                              json={"name": sm_note, "text": edited,
                                    "base_sha": g1.get("sha")})
            disk2 = (ROOT / "transcripts" / sm_sum).read_text(encoding="utf-8")
            g2 = httpx.get(f"{base}/api/notes/summary", timeout=5, headers=H,
                           params={"name": sm_note}).json()
            record("§6 编辑保存就地写回（盘上正文 + 指纹随更新）",
                   r_put.status_code == 200 and "人工修订版（smoke）" in disk2
                   and g2.get("sha") == r_put.json().get("sha")
                   and "AI 原始版" not in disk2,
                   f"HTTP {r_put.status_code} · {r_put.json().get('size')} 字节")
            prev = httpx.get(f"{base}/api/notes/preview", timeout=5, headers=H,
                             params={"name": sm_note}).json()
            record("§6 预览回 has_summary（前端摘要块与覆盖确认的判据）",
                   prev.get("has_summary") is True
                   and prev.get("has_summary_backup") is False,
                   f"has_summary={prev.get('has_summary')}")
            # 覆盖前备份：同一实现（summarize.backup_existing）在真盘上验命名与内容
            sys.path.insert(0, str(ROOT / "app"))
            import summarize as _summarize
            bak = _summarize.backup_existing(ROOT / "transcripts" / sm_sum)
            listed = httpx.get(f"{base}/api/notes/list", timeout=5, headers=H).json()
            names = [n["name"] for n in listed["notes"]]
            mine = [n for n in listed["notes"] if n["name"] == sm_note]
            record("§6 覆盖前备份为 *.summary.bak.md（内容=被覆盖版；不进笔记列表）",
                   bak is not None and bak.name == sm_bak
                   and bak.read_text(encoding="utf-8") == disk2
                   and sm_sum not in names and sm_bak not in names
                   and bool(mine) and mine[0]["has_summary"] is True,
                   f"{bak.name if bak else None} · 列表 {len(names)} 篇（不含派生物）")
        finally:
            for extra in (sm_note, sm_sum, sm_bak):
                with contextlib.suppress(Exception):
                    (ROOT / "transcripts" / extra).unlink()
        left = [n for n in (sm_note, sm_sum, sm_bak)
                if (ROOT / "transcripts" / n).exists()]
        record("§6 测试零残留（临时笔记 + 摘要 + 备份）",
               not left, "三件都清干净" if not left else f"残留 {left}")

        # —— 条目⑤（§5 下课一键归档）：三步流程在真服务上的**判据契约** ——
        # 「下课」= 暂停收音 → 摘要 → 导出（前端按这些响应逐步判定成败）。这里逐条
        # 验判据可读；**不真的 POST /api/notes/summarize**——本机 .env 配了
        # DEEPSEEK_API_KEY（smoke 前面已断言 translate=ready），那会真跑一次分钟级、
        # 花钱的摘要并覆盖用户笔记的摘要；摘要步骤的两条分支（已有则跳过 / 无 key 则
        # 跳过 / 正在跑则等）在 tests/test_end_class.py 用假 translator 全覆盖。
        ng_dir = Path(tempfile.mkdtemp(prefix="lecturelens-smoke-endclass-"))
        cfg_end = httpx.get(f"{base}/api/config", timeout=5, headers=H).json()
        ng_prev = cfg_end.get("export_dir")

        def _end_class_checks() -> None:
            """三步判据逐条验（真服务）；任一步的瞬态网络错由外层兜住并记一条失败。"""
            r_pause = httpx.post(f"{base}/api/pause", timeout=5, headers=H)
            st_p = httpx.get(f"{base}/api/status", timeout=5, headers=H).json()
            record("§5 步骤① 暂停收音（判据 ok + status.paused）",
                   r_pause.status_code == 200 and r_pause.json().get("paused") is True
                   and st_p.get("paused") is True,
                   f"HTTP {r_pause.status_code} · paused={st_p.get('paused')}")
            httpx.post(f"{base}/api/resume", timeout=5, headers=H)  # 复原：不留暂停态
            cur = httpx.get(f"{base}/api/notes/list", timeout=5, headers=H).json()
            record("§5 当前笔记可判定（无笔记时前端明确提示、一步不跑）",
                   isinstance(cur.get("current"), str)
                   and cur["current"].endswith(".md"),
                   f"current={cur.get('current')}")
            sm = httpx.get(f"{base}/api/notes/summary", timeout=5, headers=H,
                           params={"name": cur.get("current")}).json()
            branch = "已有摘要 → 归档会跳过" if sm.get("exists") else "无摘要 → 会发起生成"
            record("§5 步骤② 摘要分支判据可读（exists + status.summary.state）",
                   sm.get("ok") is True and isinstance(sm.get("exists"), bool)
                   and isinstance(st_p.get("summary"), dict),
                   f"exists={sm.get('exists')}（{branch}）· "
                   f"state={(st_p.get('summary') or {}).get('state')}")
            # 步骤③：导出当前笔记（前端 endClass 第 3 步就是 name='' + 已设 export_dir）
            httpx.post(f"{base}/api/config", timeout=10, headers=H,
                       json={"export_dir": str(ng_dir)})
            try:
                r_end = httpx.get(f"{base}/api/notes/export", timeout=30, headers=H,
                                  params={"fmt": "md", "name": ""})
                endj = r_end.json() if r_end.status_code == 200 else \
                    {"error": f"HTTP {r_end.status_code}"}
            except Exception as e:                    # noqa: BLE001 —— 如实记失败
                endj = {"error": f"{type(e).__name__}: {e}"}
            # B1：有课程归属 → 落 <导出目录>/<课程名>/（未分类 → 仍落根）。期望目录用
            # **服务端同一个** exportdir.clean_component 算，避免两边清洗规则各写一份。
            import exportdir as exportdir_mod      # noqa: PLC0415 —— 与其余段同款延迟导入
            cur_course = next((n.get("course") for n in cur.get("notes") or []
                               if n.get("name") == cur.get("current")), None)
            sub = (exportdir_mod.clean_component(cur_course)
                   if not exportdir_mod.course_is_empty(cur_course) else "")
            want_dir = (ng_dir / sub) if sub else ng_dir
            end_path = Path(endj.get("path") or "")
            record("§5 步骤③ 导出当前笔记（§3 落盘 JSON {ok, path}）",
                   endj.get("ok") is True and end_path.is_file()
                   and end_path.parent == want_dir,
                   f"{endj.get('name') or endj.get('error')} · {endj.get('bytes')} 字节 · "
                   f"落点={(sub or '(导出根目录)')}")
            record("§5 二次确认开关在 config 里可读（默认 true）",
                   cfg_end.get("class_end_confirm") is True,
                   f"class_end_confirm={cfg_end.get('class_end_confirm')}")

        try:
            _end_class_checks()
        except Exception as e:          # noqa: BLE001 —— 瞬态网络错记一条失败，别崩掉整条 smoke
            record("§5 流程判据读取（请求异常）", False, f"{type(e).__name__}: {e}")
        finally:
            _restore_export_dir(base, H, ng_prev)
            with contextlib.suppress(Exception):
                shutil.rmtree(ng_dir, ignore_errors=True)
        record("§5 测试零残留（导出目录 + config 还原）",
               not ng_dir.exists()
               and httpx.get(f"{base}/api/config", timeout=10,
                             headers=H).json().get("export_dir") == ng_prev,
               f"export_dir={ng_prev!r}")

        # —— D2（分发工程）：模型下载语义与镜像开关在真服务上的契约 ——
        # (a)(b)(c) 的纯逻辑/源码级断言在 tests/test_hf_env.py；这里验三件只有真服务
        # 才有的东西：① /api/config 与 /api/models 的字段（含 offline/mirror 两个**事实**
        # 字段要与本进程环境一致——frozen 不再影响它们）② hf_mirror 布尔往返（写盘即存）
        # ③ 非布尔一律 400（绝不把字符串当 endpoint 用）。收尾按值还原（§3 export_dir 同款）。
        dm_cfg = httpx.get(f"{base}/api/config", timeout=5, headers=H).json()
        dm_mod = httpx.get(f"{base}/api/models", timeout=15, headers=H).json()
        dm_prev = dm_cfg.get("hf_mirror")
        want_offline = os.environ.get("HF_HUB_OFFLINE") == "1"
        want_mirror = os.environ.get("HF_ENDPOINT") == "https://hf-mirror.com"
        record("D2 /api/config 含 hf_mirror；/api/models 的 offline/mirror 与进程环境一致",
               isinstance(dm_prev, bool)
               and dm_mod.get("offline") is want_offline
               and dm_mod.get("mirror") is want_mirror,
               f"hf_mirror={dm_prev} · offline={dm_mod.get('offline')}"
               f"（env={want_offline}）· mirror={dm_mod.get('mirror')}（env={want_mirror}）")
        try:
            r_on = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                              json={"hf_mirror": True})
            got = httpx.get(f"{base}/api/config", timeout=5,
                            headers=H).json().get("hf_mirror")
            record("D2 hf_mirror 布尔键往返（写 true → 读回 true）",
                   r_on.status_code == 200 and got is True,
                   f"HTTP {r_on.status_code} · hf_mirror={got}")
            r_bad = httpx.post(f"{base}/api/config", timeout=5, headers=H,
                               json={"hf_mirror": "https://evil.example"})
            bad = r_bad.json() if r_bad.status_code != 200 else {}
            record("D2 hf_mirror 只收布尔（URL/字符串一律 400，绝不当时端点用）",
                   r_bad.status_code == 400 and "hf_mirror" in (bad.get("error") or ""),
                   f"HTTP {r_bad.status_code}")
        finally:
            httpx.post(f"{base}/api/config", timeout=5, headers=H,
                       json={"hf_mirror": bool(dm_prev)})
        now_m = httpx.get(f"{base}/api/config", timeout=5,
                          headers=H).json().get("hf_mirror")
        record("D2 hf_mirror 收尾还原（值回到 smoke 开始前）",
               now_m == bool(dm_prev), f"hf_mirror {dm_prev!r} → {now_m!r}")
    finally:
        httpx.get, httpx.post = _orig_get, _orig_post   # 还原（别影响后面的段）
        proc.terminate()
        try:
            proc.wait(timeout=10)      # stdout 由 _drain_child 的线程持续读走（见其说明）
        except Exception:
            proc.kill()
        out = "".join(out_buf)
        print("  ---- 服务日志尾部 ----")
        print("\n".join("  " + l for l in out.strip().splitlines()[-14:]))
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
        # §7：把临时打开的 lan_enabled 还原成 smoke 开始前的值（键本来不存在则删掉）。
        # 收尾（子进程已停）做——服务已不需要这个键，且失败只影响下一次启动的监听范围。
        if lan_prev is not None:
            restored = _restore_lan_enabled(lan_prev)
            try:
                now = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                now_v = now.get("lan_enabled", "absent")
            except Exception:
                now_v = None
            record("§7 lan_enabled 还原（config.json 与 smoke 开始前一致）",
                   restored and now_v == lan_prev,
                   f"lan_enabled {lan_prev!r} → {now_v!r}")


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


def ws_connect(port: int, timeout: float = 5.0, token: str | None = None):
    """裸 socket 完成 WebSocket 握手并保持连接（返回 socket 或 None）。

    token 非空时走 `?token=` 查询参数——[5] 段服务禁用了环回豁免，不带头会被 403；
    [7b] 段（环回豁免放行）不传，覆盖豁免分支。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        qs = f"?token={token}" if token else ""
        req = (f"GET /ws{qs} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
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
    out_buf2: list[str] = []
    _drain_child(proc, out_buf2)   # 同 [5]：不抽干管道会把服务冻死（见 _drain_child）
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
            proc.wait(timeout=10)      # stdout 由 _drain_child 的线程持续读走
        except Exception:
            proc.kill()
        out = "".join(out_buf2)
        print("  ---- 流式服务日志尾部 ----")
        print("\n".join("  " + l for l in out.strip().splitlines()[-8:]))
        time.sleep(1)
        leftover = True
        try:
            httpx.get(f"{base}/api/status", timeout=2)
        except Exception:
            leftover = False
        record("流式服务收尾无残留进程", not leftover,
               "端口已释放" if not leftover else "⚠ 端口仍在监听")


# ---------------------------------------------------------------- 8. 打包契约（D1）
def test_build_contract() -> None:
    """[8] 打包契约（D1 分发工程）：这些是"打包版能不能开窗"的硬前提，秒级可验——
    **不跑 PyInstaller**（分钟级，归 scripts/build_exe.py 实跑 + D3 产物探针），只断言
    脚本与版本口径没被改坏：
    ① 打包入口 = app/desktop.py（自启命令写的 --minimized 只有它认）；
    ② 版本注入 = branding.APP_VERSION，生成物是合法 VSVersionInfo（exe 属性带 1.0.0）；
    ③ 桌面壳依赖（webview/pythonnet/clr_loader/pystray/PIL/qrcode/keyboard/fpdf/
       docx/genanki/pyaudiowpatch）的收集清单与构建后抽查清单一一对应；
    ④ 必需产物含 float.html/privacy.html，随包模型默认 small。"""
    print("\n[8] 打包契约（D1：入口/版本注入/桌面壳依赖/产物断言）")
    problems: list[str] = []
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_exe as be

        import branding
        from PyInstaller.utils.win32.versioninfo import (
            load_version_info_from_text_file,
        )
    except Exception as e:
        record("打包契约（D1）", False, f"导入失败 {type(e).__name__}: {e}")
        return

    # ① 入口：自启命令（frozen 分支 = "<exe>" --minimized）指向的就是这个入口
    args = be.pyinstaller_args(False)
    if args[-1] != str(be.ROOT / "app" / "desktop.py"):
        problems.append(f"打包入口不是桌面壳: {args[-1]}")

    # ② 版本注入：exe 属性里的 FileVersion/ProductVersion
    version = branding.APP_VERSION
    try:
        if not all(ord(c) < 128 for c in be.version_info_text(version)):
            problems.append("版本资源文件含非 ASCII 字符（跨机器 codepage 会乱码）")
        with tempfile.TemporaryDirectory() as td:   # 写临时目录：不碰真实构建产物
            vf = be.write_version_file(version, Path(td) / "version_info.txt")
            info = load_version_info_from_text_file(str(vf))
        strings = {s.name: s.val
                   for table in info.kids[0].kids for s in table.kids}
        if strings.get("FileVersion") != version or strings.get("ProductVersion") != version:
            problems.append(f"版本资源不是 {version}: {strings.get('FileVersion')!r}")
    except Exception as e:
        problems.append(f"版本文件不可用 {type(e).__name__}: {e}")

    # ③ 收集清单 ↔ 构建后抽查清单一一对应（漏收 = 打包版没窗口壳/托盘/二维码）
    collect_all = {args[i + 1] for i, a in enumerate(args) if a == "--collect-all"}
    packages = {pkg for pkg, _why in be.DESKTOP_SHELL_PACKAGES}
    missing = sorted(packages - collect_all)
    if missing:
        problems.append(f"未显式收集: {missing}")
    checked = {pkg for pkg, _label, _pat in be.DESKTOP_SHELL_ARTIFACTS}
    if checked != packages:
        problems.append(f"抽查清单与收集清单不一致: {sorted(checked ^ packages)}")

    # ④ 产物断言（web 页）+ 随包模型默认 small
    for page in ("_internal/web/float.html", "_internal/web/privacy.html"):
        if page not in be.REQUIRED_DIST_FILES:
            problems.append(f"必需产物缺 {page}")
    if be.BUNDLE_MODEL != "small" or be.parse_args([]).bundle_model != "small":
        problems.append(f"随包模型默认不是 small: {be.BUNDLE_MODEL}")
    # ⑤ D3 评审修复①：用户数据/密钥清单必须覆盖**运行期所有 ROOT 锚定的写盘物**
    # （评审2 实测：glossary 与 courses.json 曾在清单外，而它们与 .env 同源——
    # 「在打包目录里建过术语表/课程」就会混进分发目录）。
    for name in (".env", "config.json", "courses.json", "transcripts",
                 "logs", "glossary", "desktop.lock"):
        if name not in be.FORBIDDEN_DIST_ENTRIES:
            problems.append(f"禁用清单缺 {name}")
    if ".log" not in be.FORBIDDEN_DIST_SUFFIXES:
        problems.append("禁用后缀缺 .log")

    record("打包契约（D1）", not problems,
           "；".join(problems) or f"入口/版本 {version}/依赖 {len(packages)} 件/产物断言齐备")


def test_installer_contract() -> None:
    """[9] 安装包契约（D4 分发工程）：秒级、离线，**不跑 ISCC**（编译分钟级，归
    scripts/verify_installer.py 实跑 + 真装真卸 + SHA256）。

    只钉"被顺手改坏会出大事"的那几条：
    ① 每用户安装：PrivilegesRequired=lowest + DefaultDirName={localappdata}\\LectureLens
       （撞 Program Files 就回到权限坑，D2 明确不支持）；
    ② **卸载不得删用户数据**：这是本组最要紧的一条——[UninstallDelete] 里出现
       transcripts/logs/glossary/config.json，或整目录删 {app}，就会把用户一学期的
       笔记一起抹掉，且不可逆；
    ③ 产物名口径 dist/installer/LectureLens-<版本>-setup.exe，版本取自 exe 版本资源
       （Windows 固定版本信息恒四段，须裁成三段语义版本）；
    ④ 随包「新手指引.txt」存在、是 UTF-8、且覆盖 SmartScreen/防火墙/向导/扫码四要点。

    注：断言前**先剥 `;` 注释**——.iss 里有一段专门解释"为什么故意不写
    [UninstallDelete]"的注释，里面必然出现那些词；不剥注释就等于让说明文字
    把断言喂饱（tests/test_verify_installer.py 同款处理，两处口径一致）。
    """
    print("\n[9] 安装包契约（D4：每用户安装/卸载保留数据/产物名/新手指引）")
    problems: list[str] = []
    iss = ROOT / "installer" / "lecturelens.iss"
    guide = ROOT / "installer" / "新手指引.txt"
    if not iss.is_file():
        record("安装包契约（D4）", False, f"缺 {iss.relative_to(ROOT)}")
        return

    raw = iss.read_text(encoding="utf-8")
    # 剥注释后的"有效脚本"——断言只认代码，不认说明文字
    code = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith(";"))

    # ① 每用户安装
    if not re.search(r"^PrivilegesRequired=lowest$", code, re.M):
        problems.append("PrivilegesRequired 不是 lowest")
    if not re.search(r"^DefaultDirName=\{localappdata\}\\LectureLens$", code, re.M):
        problems.append("DefaultDirName 不是 {localappdata}\\LectureLens")
    if "Program Files" in code:
        problems.append("出现 Program Files（全机安装，D2 不支持）")

    # ② 卸载不得删用户数据
    m = re.search(r"^\[UninstallDelete\](.*?)(?=^\[|\Z)", code, re.S | re.M)
    unsec = m.group(1) if m else ""
    for name in ("transcripts", "logs", "glossary", "config.json"):
        if name in unsec:
            problems.append(f"[UninstallDelete] 触及用户数据 {name}")
    if re.search(r'Name:\s*"\{app\}\\?"', unsec):
        problems.append("[UninstallDelete] 整目录删 {app}（会连笔记一起删）")

    # ③ 产物名 + 版本来源
    if not re.search(r"^OutputBaseFilename=LectureLens-\{#MyAppVersion\}-setup$", code, re.M):
        problems.append("OutputBaseFilename 口径不符")
    if "GetVersionNumbersString(" not in code:
        problems.append("版本未取自 exe 版本资源（应 GetVersionNumbersString）")
    if re.search(r'#define\s+MyAppVersion\s+"', code):
        problems.append("版本号被硬编码（破坏单一来源）")
    if 'Copy(Ver4, Len(Ver4) - 1, 2) == ".0"' not in code:
        problems.append("缺四段→三段版本裁剪守卫")
    # ⑤ 评审修复：四条卸载收尾消息都要覆盖且写明数据保留
    #    （漏 UninstalledAndNeedsRestart → 「程序还开着就卸载」这条高频路径上
    #     用户只看到英文原文、也不知道笔记被保留了）
    m2 = re.search(r"^\[Messages\](.*?)(?=^\[|\Z)", code, re.S | re.M)
    msgs = m2.group(1) if m2 else ""
    for name in ("ConfirmUninstall", "UninstalledAll",
                 "UninstalledMost", "UninstalledAndNeedsRestart"):
        line = next((ln for ln in msgs.splitlines() if ln.startswith(name + "=")), "")
        if not line:
            problems.append(f"[Messages] 未覆盖 {name}")
        elif "保留" not in line:
            problems.append(f"{name} 未写明数据已保留")
    # ⑥ 评审修复：TEST_VERSION 是测试专用钩子，必须条件编译且发版仍取自 exe 版本资源
    if "#ifdef TEST_VERSION" not in code:
        problems.append("缺 TEST_VERSION 条件编译（旧版测试包无从生成）")
    elif code.index("#ifdef TEST_VERSION") > code.index("GetVersionNumbersString("):
        problems.append("TEST_VERSION 分支位置不对（发版版本来源可能被劫持）")

    # ⑦ 新手指引：存在 + UTF-8 + 四要点
    if not guide.is_file():
        problems.append("缺 installer/新手指引.txt")
    else:
        try:
            gt = guide.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append(f"新手指引不是 UTF-8: {e}")
            gt = ""
        for need, why in (("仍要运行", "SmartScreen 指引"),
                          ("防火墙", "防火墙弹窗说明"),
                          ("向导", "首启三步向导"),
                          ("扫码", "手机同屏扫码")):
            if need not in gt:
                problems.append(f"新手指引缺{why}（{need}）")

    record("安装包契约（D4）", not problems,
           "；".join(problems) or "每用户安装/卸载保留数据/产物名口径/新手指引四要点齐备")


def test_distribution_guide_contract() -> None:
    """[10] 分发指南契约（D5）：秒级、离线。

    文档最容易坏的方式不是写错字，而是**悄悄说谎**——代码里加了自动更新、安装包
    换了名字/哈希、仓库口径变了，而指南还停在旧说法，朋友照着做就踩空。故这里钉：
    ① 指南存在且是 UTF-8；
    ② 五块内容齐（三渠道对比 / 发版 SOP / 首次使用 / 可复制文案 / FAQ）；
    ③ **诚实边界三条如实写明**：私有可见性、未签名、无自动更新（D5 原话
       「只写真实能力……不要承诺不存在的能力」）；
    ④ **文档↔代码对齐**：指南说"没有自动更新"，代码里就真没有；
    ⑤ **文档↔产物对齐**：指南里贴的安装包名与 SHA256 与 dist/installer 下真产物一致
       （产物不存在则跳过该条，不误报）。
    """
    print("\n[10] 分发指南契约（D5：三渠道/SOP/首次使用/文案/FAQ + 诚实边界）")
    problems: list[str] = []
    guide = ROOT / "docs" / "distribution-guide.md"
    if not guide.is_file():
        record("分发指南契约（D5）", False, f"缺 {guide.relative_to(ROOT)}")
        return
    raw = guide.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        record("分发指南契约（D5）", False, f"不是 UTF-8: {e}")
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("指南带了 BOM")

    # ② 五块内容
    for need, why in (("GitHub Release", "渠道对比"), ("网盘", "渠道对比"),
                      ("gh release create", "发版 SOP"),
                      ("SmartScreen", "首次使用指引"), ("仍要运行", "首次使用指引"),
                      ("```text", "可复制文案块"),
                      ("常见问题", "FAQ")):
        if need not in text:
            problems.append(f"缺{why}（{need}）")

    # ③ 诚实边界
    if "私有" not in text:
        problems.append("未写明仓库私有（Release 仅 collaborator 可见）")
    if "代码签名" not in text:
        problems.append("未写明没有代码签名")
    if "自动更新" not in text:
        problems.append("未写明没有自动更新")
    if "不公开分发" not in text and "刻意不做" not in text:
        problems.append("未守住「不公开分发」口径")

    # ④ 文档↔代码对齐：说了没有自动更新，代码里就得真没有
    hits = []
    for p in sorted((ROOT / "app").rglob("*.py")):
        body = p.read_text(encoding="utf-8", errors="replace")
        for kw in ("auto_update", "check_for_update", "updater_url", "自动更新"):
            if kw in body:
                hits.append(f"{p.name}:{kw}")
    if hits:
        problems.append(f"代码已有自动更新机制，指南口径需同步：{hits}")

    # ⑤ 文档↔产物对齐（产物不存在则跳过）
    cands = sorted((ROOT / "dist" / "installer").glob("LectureLens-*-setup.exe"),
                   key=lambda q: q.stat().st_mtime)
    if cands:
        setup = cands[-1]
        if setup.name not in text:
            problems.append(f"指南未提到当前安装包名 {setup.name}")
        sha = setup.with_name(setup.name + ".sha256")
        if sha.is_file():
            want = sha.read_text(encoding="utf-8").split()[0].lower()
            if want not in text.lower():
                problems.append("指南里的 SHA256 与产物 .sha256 不一致")

    record("分发指南契约（D5）", not problems,
           "；".join(problems) or "三渠道/SOP/首次使用/文案/FAQ 齐备 + 诚实边界与产物对齐")


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

    test_build_contract()   # [8] D1：打包契约（秒级；--vad-only 也跑——它不依赖模型）
    test_installer_contract()  # [9] D4：安装包契约（秒级离线；同上，--vad-only 也跑）
    test_distribution_guide_contract()  # [10] D5：分发指南契约（同上）

    print("\n========== 结果汇总 ==========")
    for name, ok, note in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else ""))
    fails = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"总计 {len(RESULTS)} 项，失败 {fails} 项")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
