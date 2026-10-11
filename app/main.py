# -*- coding: utf-8 -*-
"""main.py — 课堂实时字幕翻译服务。
用法: python main.py [--glossary glossary/CS.md ...] [--model small] [--port 8787]
组件：麦克风 VAD 分句（capture）→ faster-whisper（asr）→ DeepSeek 翻译（translate）
     → WebSocket 字幕广播 + 双语笔记实时落盘（transcripts/）。"""
import argparse
import asyncio
import datetime
import queue
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

sys_path = str(Path(__file__).parent)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from asr import WhisperASR          # noqa: E402
from capture import MicSegmenter    # noqa: E402
from translate import DeepSeekTranslator, load_api_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
TRANSCRIPTS = ROOT / "transcripts"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", action="append", default=[], help="课程术语表 md，可多次")
    ap.add_argument("--model", default="small", help="faster-whisper 模型（tiny/base/small）")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--device", default=None, help="麦克风设备索引")
    ap.add_argument("--api-key", default=None, help="DeepSeek key（缺省读环境变量/.env）")
    return ap.parse_args()


class Hub:
    """广播中心：WebSocket 客户端 + 笔记文件双写。"""

    def __init__(self):
        self.clients: set = set()
        self.transcript_path: Path | None = None

    def start_transcript(self) -> Path:
        TRANSCRIPTS.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        p = TRANSCRIPTS / f"lecture_{stamp}.md"
        p.write_text(
            f"# 双语课堂笔记 {stamp}\n\n"
            "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n\n---\n\n",
            encoding="utf-8",
        )
        self.transcript_path = p
        return p

    def append(self, ts: str, en: str, zh: str) -> None:
        if not self.transcript_path:
            return
        with self.transcript_path.open("a", encoding="utf-8") as f:
            if en:
                f.write(f"- `{ts}` {en}\n")
            if zh:
                f.write(f"  - {zh}\n")

    async def broadcast(self, payload: dict) -> None:
        dead = []
        data = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()
app = FastAPI(title="LectureLens")
status = {"asr": "init", "translate": "init", "segments": 0, "last_en": "", "last_zh": ""}


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
async def api_status():
    return JSONResponse(status)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # 保活；客户端不发内容
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


async def asr_loop(args) -> None:
    """主循环：VAD 分句（后台线程）→ whisper（线程池）→ 翻译（async）→ 广播+落盘。"""
    from capture import MicSegmenter

    seg_q: queue.Queue = queue.Queue()
    segmenter = MicSegmenter(seg_q, device=args.device)
    segmenter.start()
    print("[main] 🎙 麦克风已启动，说话即出字幕")

    status["asr"] = "loading model"
    asr = await asyncio.to_thread(
        WhisperASR, model_size=args.model, language=args.lang
    )
    status["asr"] = "ready"
    hub.start_transcript()
    print(f"[main] 📝 笔记文件: {hub.transcript_path}")

    translator = None
    try:
        key = args.api_key or load_api_key(ROOT / ".env")
        if key:
            translator = DeepSeekTranslator(key, glossary_files=args.glossary)
            status["translate"] = "ready"
        else:
            status["translate"] = "disabled (no DEEPSEEK_API_KEY)"
            print("[main] ⚠ 未找到 DEEPSEEK_API_KEY —— 只出英文字幕，不翻译")
    except Exception as e:
        status["translate"] = f"error: {e}"
        print(f"[main] ⚠ 翻译初始化失败: {e} —— 只出英文字幕")

    while True:
        try:
            _, audio = await asyncio.to_thread(seg_q.get)
            en = await asyncio.to_thread(asr.transcribe, audio)
        except Exception as e:
            print(f"[pipeline] error: {e}")
            await asyncio.sleep(1)
            continue
        if not en:
            continue
        status["segments"] += 1
        status["last_en"] = en
        ts_clock = datetime.datetime.now().strftime("%H:%M:%S")

        # 两段式广播：英文立即上屏，中文翻译好了再补
        payload = {"ts": ts_clock, "en": en, "zh": "", "segments": status["segments"]}
        hub.append(ts_clock, en, "")
        await hub.broadcast(payload)

        if translator:
            try:
                zh = await translator.translate(en)
                status["last_zh"] = zh
                payload["zh"] = zh
                hub.append(ts_clock, "", zh)
                await hub.broadcast(payload)
            except Exception as e:
                print(f"[translate] error: {e}")
        print(f"[{ts_clock}] {en}\n      → {zh if translator else ''}")


def main():
    args = parse_args()

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(asr_loop(args))

    print(f"[main] LectureLens starting — port={args.port}, model={args.model}, lang={args.lang}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
