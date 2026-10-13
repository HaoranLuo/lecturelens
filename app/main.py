# -*- coding: utf-8 -*-
"""main.py — 课堂实时字幕翻译服务。
用法: python main.py [--glossary glossary/CS.md ...] [--model large-v3-turbo] [--gpu] [--port 8787]
组件：麦克风 VAD 分句（capture）→ faster-whisper（asr）→ DeepSeek 翻译（translate）
     → WebSocket 字幕广播 + 双语笔记实时落盘（transcripts/）。
配置：config.json（模型/设备/VAD）持久化，字幕页可热切换；CLI 参数优先级更高。
鉴权：/api/* 与 /ws 校验 LAN token（条目19），127.0.0.1 豁免；首启生成 token 写 config.json 并打印。"""
import argparse
import asyncio
import contextlib
from collections import deque
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

# 条目20：PyInstaller frozen——离线加载随包模型（必须在下方 asr→faster_whisper→
# huggingface_hub 导入前设置：huggingface_hub 的开关常量在 import 时读取）
if getattr(sys, "frozen", False):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

from applog import setup_logging, uvicorn_log_config  # noqa: E402
from ankideck import build_deck  # noqa: E402  条目18：Anki 复习卡生成
from branding import APP_VERSION  # noqa: E402  p1-3：版本号单一来源（status/关于页）
from asr import WhisperASR          # noqa: E402
import autostart as autostart_mod   # noqa: E402  p3-3：开机自启（HKCU Run 项读写）
import courses as courses_mod       # noqa: E402  p2-1：课程管理（courses.json 存储/校验）
import notes as notes_mod           # noqa: E402  p2-2：笔记库（transcripts 解析/索引/检索）
import exporters                    # noqa: E402  p2-3：导出中心（md/SRT/PDF/Word）
import models as models_mod         # noqa: E402  p2-4：模型管理器（缓存/下载/删除）
import glossary as glossary_mod     # noqa: E402  p2-6：术语库（glossary/*.md 增删改）
import hotkeys as hotkeys_mod       # noqa: E402  p3-1：全局快捷键（键名白名单/注册/降级）
from capture import SR, MicSegmenter  # noqa: E402
from loopback import LoopbackSegmenter  # noqa: E402  条目14（scipy 依赖一并就位）
from scheduler import DecodeScheduler  # noqa: E402
from summarize import TranscriptSummarizer  # noqa: E402  条目17：课后 AI 笔记摘要（map-reduce）
from translate import DeepSeekTranslator, glossary_prompt, load_api_key  # noqa: E402

log = logging.getLogger("main")

# 条目20：ROOT 推导补 sys.frozen 分支（技术评审 missing 项）——PyInstaller onedir 下
# Path(__file__) 指向 _internal 打包数据（只读），且 exe 的 cwd 语义与开发态不同；
# ROOT 必须是 exe 所在目录（config.json/token/transcripts/logs 跟着 exe 走、可写、
# 换版不丢），WEB 用打包进 _internal 的只读数据（--add-data app/web;web）。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    WEB = Path(getattr(sys, "_MEIPASS", ROOT)) / "web"
else:
    ROOT = Path(__file__).resolve().parent.parent
    WEB = Path(__file__).resolve().parent / "web"
TRANSCRIPTS = ROOT / "transcripts"
CONFIG_PATH = ROOT / "config.json"
COURSES_PATH = ROOT / "courses.json"   # p2-1：课程表（与 config.json 同放根目录）

# p2-1：课程调色板——侧栏 8px 圆点的身份标识色。**故意不用 spec §二 的语义令牌**
# （--ok/--warn/--err 表达状态，拿去当课程身份会与「在线/告警」语义打架），也不含
# --gold（§一-2 金色专属中文译文/选中态/主按钮/品牌）。一组低饱和、暗底可辨的中性
# 色相；只做小圆点，绝不大面积铺底（§一-2）。这是对 spec 的扩展，见 courses.py 模块注释。
COURSE_PALETTE = ("#93a1b0", "#7fa6d9", "#9b8fd9", "#6fbfa8",
                  "#c9a87c", "#d98fa6", "#8fbf7f", "#b8a0d9")


def bundled_model_path(name: str) -> Path | None:
    """条目20：frozen 下解析随包模型目录（<exe>/models/<name>，由 build_exe.py 从
    HF 缓存快照复制落盘，须含 model.bin）；开发模式恒 None（按名走 HF 缓存，
    行为零变化）。目录缺失/被删 → None → WhisperModel 按名离线加载（HF_HUB_OFFLINE=1
    下明确报错，不静默联网）。"""
    if not getattr(sys, "frozen", False):
        return None
    p = ROOT / "models" / name
    return p if (p / "model.bin").is_file() else None

DEFAULT_CONFIG = {"model": "large-v3-turbo", "device": "auto",
                  "compute_type": None, "vad": "silero", "streaming": False,
                  "lang": "en", "source": "mic", "input_device": None,
                  "translate_model": "deepseek-chat",
                  "auth_enabled": True,   # 条目19：LAN token 鉴权开关（回退 = false）
                  "auth_token": None,     # 条目19：首启生成（provision_token），不可经 API 设置
                  "first_run_done": False,  # p1-5：首启向导标记（完成/跳过置 true；设置页可重开）
                  "floating_bar": False,  # p1-7：B1 悬浮字幕条显隐（桌面壳轮询此键同步窗口）
                  # p2-5（B4 断句参数面板）：分句/积压调优。**默认值必须与
                  # capture.py 构造签名里的默认值逐字一致**（test_seg_params 有断言钉住），
                  # 否则「恢复默认」会把采集器调到一个面板上看不出来的状态。
                  "silence_ms": 700,      # 连续静音多久算一句结束（capture.MicSegmenter）
                  "max_seg_s": 15.0,      # 软上限：超长段不再硬切，改等静音对齐切点
                  "hard_cut_s": 18.0,     # 硬上限兜底：一直不停嘴时的强制切点
                  "backlog_drop": 5,      # 积压超此段数触发追赶丢弃（asr_loop 每轮读）
                  "backlog_keep": 2,      # 追赶丢弃后保留的最新段数
                  # p2-7（B2 历史行渐隐）：当前句上方做「半透明 + 缩小」的条数（0/1/2）。
                  # 纯视觉——不删条目、不影响 1000 条回看。
                  "fade_lines": 1,
                  # p3-1（B12 快捷键体系）：全局热键。默认开——「按了没反应」必须能在
                  # 设置页看到原因（注册失败会降级并在状态行注明），而不是静默失效。
                  # 组合串不是枚举（任意修饰键×主键），故不进 CONFIG_VALUE_CHOICES，
                  # 走专项校验 _hotkey_problem（与 input_device 同款口径）。
                  "hotkeys_enabled": True,
                  "hotkey_pause": "ctrl+alt+l",
                  "hotkey_float": "ctrl+alt+s",
                  # p3-5（B12 交互细节）：窗口版式记忆。None = 还没记忆过（用默认版式），
                  # 桌面壳退出时写入实测几何。坐标**允许为负**（多显示器：主屏左侧的
                  # 那块屏 x<0），故区间是 (-20000, 20000) 而不是非负区间。
                  "window_w": None, "window_h": None,
                  "window_x": None, "window_y": None,
                  "float_x": None, "float_y": None}

# —— 条目4：/api/config 全键白名单（键集合 + 每键取值范围）——
# 新增键必须进 ALLOWED_CONFIG_KEYS + CONFIG_VALUE_CHOICES（白名单校验）
MODEL_CHOICES = ("tiny", "base", "small", "medium",
                 "large-v3", "large-v3-turbo")  # 与字幕页下拉框/CLI help 一致
ALLOWED_CONFIG_KEYS = ("model", "device", "compute_type", "vad", "lang", "streaming",
                       "source", "input_device", "translate_model", "auth_enabled",
                       "first_run_done",  # p1-5：首启向导标记（true=完成/跳过，false=重开）
                       "floating_bar",    # p1-7：B1 悬浮字幕条显隐（桌面壳轮询同步）
                       # p2-5（B4）：分句/积压调优（数值区间校验，见 CONFIG_NUM_RANGES）
                       "silence_ms", "max_seg_s", "hard_cut_s",
                       "backlog_drop", "backlog_keep",
                       "fade_lines",      # p2-7（B2）：历史行渐隐条数（0/1/2）
                       # p3-1（B12）：全局快捷键开关与两个键位（键名字符串走
                       # hotkeys.combo_problem 白名单校验，见 _hotkey_problem）
                       "hotkeys_enabled", "hotkey_pause", "hotkey_float",
                       # p3-5（B12）：窗口版式记忆（数值区间校验，见 CONFIG_NUM_RANGES）
                       "window_w", "window_h", "window_x", "window_y",
                       "float_x", "float_y")
# 条目13：lang 码表 = auto + en/zh + 常用语言码（whisper 语言码，UI 下拉为其常用子集）
LANG_CHOICES = ("auto", "en", "zh", "ja", "ko", "fr", "de", "es", "ru", "pt", "it")
# 条目16：translate.model 配置化——切档位前以 DeepSeek 官方定价页为准
TRANSLATE_MODEL_CHOICES = ("deepseek-chat", "deepseek-reasoner")
CONFIG_VALUE_CHOICES: dict = {
    "model": MODEL_CHOICES,
    "device": ("auto", "cpu", "cuda"),
    "compute_type": (None, "int8", "float16", "int8_float16"),
    "vad": ("silero", "energy"),
    "lang": LANG_CHOICES,          # 条目13：变更热生效（改 asr.language，无需重建模型）
    "streaming": (False, True),    # 条目11 灰度开关，默认关；变更重启服务后生效
    "source": ("mic", "loopback"),  # 条目14：mic=麦克风 / loopback=系统声音内录（热切换）
    "translate_model": TRANSLATE_MODEL_CHOICES,  # 条目16：热切换（改 translator.model）
    "auth_enabled": (False, True),  # 条目19：即时键，改后下一请求即生效（回退开关）
    "first_run_done": (False, True),  # p1-5：即时键，向导完成/跳过置 true，设置页重开置 false
    "floating_bar": (False, True),    # p1-7：即时键，桌面壳 1s 轮询同步悬浮窗显隐
    # p2-7（B2）：渐隐条数。取值是**小整数枚举**而非布尔，故直接进枚举表即可
    # （0/1/2 三个离散档，不需要 p2-5 那套区间校验）。注意 False == 0，传 false
    # 会被当成 0（不渐隐）——语义上等价，无害。
    "fade_lines": (0, 1, 2),
    "hotkeys_enabled": (False, True),  # p3-1：即时键，改后立即重注册/注销热键
    # input_device 不在静态码表里：取值 = None 或非负设备索引（动态依赖本机设备枚举，
    # 条目15 在 set_config 里走专项校验 _input_device_problem）
    # hotkey_pause/hotkey_float 同理不在码表里：取值 = 白名单键名组合串（p3-1，
    # 在 set_config 里走专项校验 _hotkey_problem）
}

# —— p2-5（B4 断句参数面板）——
# 数值型键不放进 CONFIG_VALUE_CHOICES（那是「取值枚举表」，数值区间塞不进去），
# 与 input_device 同款：在 set_config 里走专项校验 _config_number_problem。
#: 键 → (最小, 最大)。上下界按 capture 状态机的物理含义定：
#: silence_ms 太小会把词间断成碎句、太大则整段拖到硬切；max_seg_s/hard_cut_s
#: 上界防「一句话字幕挂到下一节课」。
CONFIG_NUM_RANGES: dict = {
    "silence_ms": (200, 3000),
    "max_seg_s": (3.0, 30.0),
    "hard_cut_s": (5.0, 60.0),
    "backlog_drop": (2, 50),
    "backlog_keep": (1, 20),
    # p3-5（B12）窗口版式记忆。上下界是「防手改配置写出荒唐值」的兜底，不是产品
    # 限制：尺寸下限取桌面窗口最小宽的保守值、上限远大于任何单屏；坐标**必须允许
    # 负值**——多显示器里主屏左侧/上方的屏就是负坐标，写成非负区间会把合法版式
    # 判成非法（本机 8 屏环境实测 x 可为负）。
    "window_w": (320, 20000), "window_h": (240, 20000),
    "window_x": (-20000, 20000), "window_y": (-20000, 20000),
    "float_x": (-20000, 20000), "float_y": (-20000, 20000),
}
CONFIG_INT_KEYS = ("silence_ms", "backlog_drop", "backlog_keep",
                   # p3-5：几何值都是整数像素；允许浮点会让配置里出现 1280.5 这种
                   # 无从解释的值，且 create_window 只吃 int
                   "window_w", "window_h", "window_x", "window_y",
                   "float_x", "float_y")  # 其余按浮点收
#: 变更需**重建采集器**才生效的键（apply_segmenter 构造时读入）
SEGMENT_REBUILD_KEYS = ("silence_ms", "max_seg_s", "hard_cut_s")
#: 每轮循环现读、无需重建的键
SEGMENT_LIVE_KEYS = ("backlog_drop", "backlog_keep")
#: 「恢复默认」覆盖的全部键（单一来源 = DEFAULT_CONFIG）
SEGMENT_KEYS = SEGMENT_REBUILD_KEYS + SEGMENT_LIVE_KEYS
MODEL_KEYS = ("model", "device", "compute_type")  # 变更需重建 ASR → save-after-build


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning(f"[config] ⚠ 读取失败({e})，用默认配置")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _input_device_problem(idx: int) -> str | None:
    """条目15②：input_device 索引存在性校验（sounddevice 打桩可测）。
    返回错误文案；None = 合法或无法校验——设备枚举失败（无音频 API 的离线
    环境）时放行，采集层 watchdog 兜底。校验只挡明显手误（越界/输出设备），
    不保证设备可独占打开。"""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        if idx >= len(devs) or devs[idx]["max_input_channels"] <= 0:
            return f"非法 input_device: {idx}（设备不存在或没有输入通道）"
    except Exception as e:
        log.debug(f"[devices] ⚠ 枚举设备失败，跳过存在性校验: {e}")
    return None


def _config_number_problem(key: str, value) -> str | None:
    """p2-5：数值型配置键校验（类型 + 区间）。返回错误文案；None = 合法。

    bool 是 int 子类，必须显式排除——否则 True 会被当成合法的 1 悄悄写进配置。"""
    lo, hi = CONFIG_NUM_RANGES[key]
    if isinstance(value, bool):
        return f"非法 {key}: {value!r}（须为数值，不是布尔）"
    if key in CONFIG_INT_KEYS:
        if not isinstance(value, int):
            return f"非法 {key}: {value!r}（须为整数）"
    elif not isinstance(value, (int, float)):
        return f"非法 {key}: {value!r}（须为数值）"
    if not (lo <= float(value) <= hi):
        return f"非法 {key}: {value!r}（允许范围 {lo}–{hi}）"
    return None


def _hotkey_problem(key: str, value) -> str | None:
    """p3-1：热键组合串校验（键名白名单）。返回错误文案；None = 合法。

    与 input_device 同款「专项校验」：合法取值是「任意修饰键 × 任意主键」的组合，
    塞不进 CONFIG_VALUE_CHOICES 那种静态枚举表。校验本身在 hotkeys 模块里（纯函数、
    不依赖 keyboard 库）——**降级态下设置页仍能校验**，不会因为库没了就放行垃圾值。

    空串是全局约定里的 no-op 占位（见 set_config 的 `v == ""` 分支），到不了这
    一层；这里只处理真正要被写入的值。"""
    problem = hotkeys_mod.combo_problem(value)
    if problem:
        return f"非法 {key}: {value!r}（{problem}）"
    return None


def _config_cross_problem(merged: dict) -> str | None:
    """p2-5：跨键组合约束。单键各自合法 ≠ 组合合法。

    hard_cut_s 必须**严格大于** max_seg_s：capture 的状态机是「到 max_seg_s 标记
    溢出、改等静音对齐；仅当已溢出且到 hard_cut_s 才强制切」。若 hard_cut_s ≤
    max_seg_s，溢出标记一置位、下一块就满足硬切条件——「超长段等静音」的语义被
    完全抵消，而面板上两个数字看起来都还各自合法（这种组合只能在这一层拦）。"""
    hc = float(merged.get("hard_cut_s", 0) or 0)
    ms = float(merged.get("max_seg_s", 0) or 0)
    if hc <= ms:
        return (f"非法组合：hard_cut_s({hc:g}) 必须大于 max_seg_s({ms:g})"
                f"——否则硬切先于软上限触发，「超长段改等静音对齐」失效")
    bk = int(merged.get("backlog_keep", 0) or 0)
    bd = int(merged.get("backlog_drop", 0) or 0)
    if bk >= bd:
        return (f"非法组合：backlog_keep({bk}) 必须小于 backlog_drop({bd})"
                f"——否则丢完仍高于阈值，追赶丢弃形同虚设")
    # p3-1：两个热键撞到同一组合 → 一次按键同时触发暂停与浮窗切换，用户只会
    # 看到「按键行为很怪」。keyboard 库允许同一组合注册多个回调（实测），
    # 所以这层是本项目自己的纪律，只能在这里拦。比较前先规范化——否则
    # "Ctrl+Alt+L" 与 "ctrl+alt+l" 会被当成两个不同的组合而漏判。
    hp = hotkeys_mod.normalize(merged.get("hotkey_pause"))
    hf = hotkeys_mod.normalize(merged.get("hotkey_float"))
    if hp is not None and hp == hf:
        return (f"非法组合：hotkey_pause 与 hotkey_float 不能相同（都是 {hp}）"
                f"——否则一次按键会同时触发两个动作")
    return None


def config_backlog_thresholds(cfg: dict) -> tuple[int, int]:
    """p2-5：从 config 取 (积压阈值, 保留段数)，缺键回落模块常量。

    这里**再夹一次边界**（配置层已有区间与跨键校验）：手改 config.json 能绕过
    /api/config，而 keep ≥ drop 会让「丢完仍高于阈值」，每轮空转；drop 过大则积压
    永远不触发追赶、延迟失控。夹一次的成本可忽略。"""
    drop = int(cfg.get("backlog_drop") or BACKLOG_DROP_THRESHOLD)
    keep = int(cfg.get("backlog_keep") or BACKLOG_KEEP)
    drop = max(2, min(drop, 200))
    keep = max(1, min(keep, drop - 1))
    return drop, keep


def backlog_trim(q: "queue.Queue", drop_th: int, keep_n: int) -> int:
    """积压超 drop_th 时丢最旧的、只留最新 keep_n 段；返回丢弃条数（0 = 未触发）。

    宁可缺句不可慢半拍：转写慢于采集时，旧段写出来字幕也早已过时。"""
    if q.qsize() <= drop_th:
        return 0
    dropped = 0
    while q.qsize() > keep_n:
        try:
            q.get_nowait()
            dropped += 1
        except queue.Empty:
            break
    return dropped


def segment_kwargs(cfg: dict) -> dict:
    """p2-5：传给采集器构造函数的断句参数。

    只带**配置里确实存在**的键：缺键时让 capture 用自己的默认值，绝不在这里再抄
    一份默认数字（两处默认值必然漂移）。"""
    return {k: cfg[k] for k in SEGMENT_REBUILD_KEYS if cfg.get(k) is not None}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", action="append", default=[], help="课程术语表 md，可多次")
    ap.add_argument("--model", default=None,
                    help="覆盖 config.json（tiny/base/small/medium/large-v3/large-v3-turbo）")
    ap.add_argument("--gpu", action="store_true", help="强制 CUDA（缺省按 config 的 device=auto）")
    ap.add_argument("--compute-type", dest="compute_type", default=None,
                    help="覆盖计算精度（GPU: float16/int8_float16；CPU: int8）")
    ap.add_argument("--lang", default=None,
                    help="覆盖 config 的识别语言（条目13；如 en/zh/auto，缺省读 "
                         "config.json 的 lang，再缺省 en）")
    ap.add_argument("--vad", default=None, choices=["silero", "energy"],
                    help="覆盖 config 的 VAD（silero 默认 / energy 能量门限）")
    ap.add_argument("--source", default=None, choices=["mic", "loopback"],
                    help="覆盖 config 的音源（条目14：mic=麦克风 / loopback=WASAPI "
                         "系统声音内录；CLI 最高优先级，回退开关口径见计划）")
    ap.add_argument("--streaming", action="store_true",
                    help="覆盖 config：开启流式 partial 字幕（LocalAgreement-2；"
                         "条目11/12 灰度与 smoke_test 服务级子用例用）")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--device", default=None,
                    help="麦克风设备索引或名称（条目15：最高优先级，覆盖 config 的 "
                         "input_device；缺省读 config，再缺省跟随系统默认输入设备）")
    ap.add_argument("--api-key", default=None, help="DeepSeek key（缺省读环境变量/.env）")
    ap.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    ap.add_argument("--open-browser", action="store_true",
                    help="服务就绪后自动打开字幕页（快捷方式用；环境变量 "
                         "LECTURELENS_NO_BROWSER=1 可强制跳过）")
    return ap.parse_args()


def note_header(stamp: str, course: str | None) -> str:
    """p2-1：笔记头。课程行**恒定写出**（未选课 = 「未分类」）——恒定格式让
    courses.count_notes 的头部解析永远有得解，不靠「有课才写」的隐式约定。"""
    return (f"# 双语课堂笔记 {stamp}\n\n"
            "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
            f"{courses_mod.COURSE_HEADER_PREFIX}"
            f"{course or courses_mod.UNCATEGORIZED}\n\n"
            "---\n\n")


def note_has_body(text: str) -> bool:
    """笔记是否已开始记录正文（存在 `- \\`ts\\` …` 条目行）。

    判据 = append() 的落笔格式（EN 行 `- \\`{ts}\\` …` / ZH 行 `  - \\`#{seg}\\` …`）。
    用作 retag_course 的分流依据：只有头 = 还没上课，改写头最干净。"""
    return any(line.strip().startswith("- `") for line in text.splitlines())


def unpack_transcribe(res) -> tuple[str, list]:
    """p2-3：兼容解包 asr.transcribe 的返回。with_times=True 时是 (text, segs)；
    替身/旧实现仍可能只回 str —— 视为「本段没有段级时间戳」（导出侧自有降级路径），
    绝不因为形状不同而让字幕主链路抛异常。"""
    if isinstance(res, tuple):
        return (res[0] or ""), (res[1] or [])
    return (res or ""), []


class Hub:
    """广播中心：WebSocket 客户端 + 笔记文件双写。"""

    def __init__(self):
        self.clients: set = set()
        self.transcript_path: Path | None = None
        self.stamp: str | None = None   # p2-1：笔记时间戳（改写头时复用，不重新取时）
        self.t0_audio: float | None = None   # p2-3：本场首段音频采集墙钟（时间轴原点）
        self.times: list = []                # p2-3：[{"seg","start","end"}] 绝对偏移秒

    def start_transcript(self, course: str | None = None) -> Path:
        TRANSCRIPTS.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        p = TRANSCRIPTS / f"lecture_{stamp}.md"
        p.write_text(note_header(stamp, course), encoding="utf-8")
        self.transcript_path = p
        self.stamp = stamp
        self.t0_audio = None      # p2-3：换笔记 = 时间轴重新起算
        self.times = []
        return p

    def record_time(self, seg: int, window_start: float | None,
                    seg_times: list | None, audio_s: float) -> None:
        """p2-3：记录一段的绝对时间区间（秒，自本场首段音频**窗口起点**起算）并落 sidecar。

        区间 = 段内第一个 whisper 子段的 start → 最后一个子段的 end（真实语音边界）；
        没有子段时间戳（替身实现/转写异常）时退化为整段音频区间。t0 = 本场首段的
        **窗口起点**，故导出时间轴恒以「本场第一句的音频开头」为 0。

        **参数必须是窗口起点，不是 capture 投递的时间戳**（评审 1 修）：capture 的
        `q_out.put((time.time(), audio))` 在 `_emit` **末尾**才执行——那一刻整段缓冲
        已组装完毕（0.4s preroll + 语音 + ~0.7s 尾随静音），所以那个时间戳是**发射
        时刻 ≈ 窗口末尾**。旧实现直接拿它当起点，导致第 i 段的起点误差恰为
        `d_i − d_1`（d = 该段窗口时长）：每一条 SRT cue 都整体后移，相邻段还会因重叠
        被 _clamp_cues 压成最短 cue——位置与时长双错，而钳制只会把错误抹平、不报警。
        调用方按 `窗口起点 = 发射时刻 − 窗口时长` 换算后传入。
        """
        if window_start is None:
            return
        if self.t0_audio is None:
            self.t0_audio = float(window_start)
        off = max(0.0, float(window_start) - self.t0_audio)   # 时钟回拨兜底
        start, end = off, off + float(audio_s or 0.0)
        if seg_times:
            try:
                start = off + float(seg_times[0].get("start") or 0.0)
                end = off + float(seg_times[-1].get("end") or 0.0)
            except (AttributeError, TypeError, ValueError):
                start, end = off, off + float(audio_s or 0.0)
        if end <= start:                                   # 异常/零长：退回音频区间
            end = start + float(audio_s or 0.0)
        self.times.append({"seg": int(seg),
                           "start": round(max(0.0, start), 3),
                           "end": round(max(0.0, end), 3)})
        # 落盘必须自吞异常：本函数在 asr_loop 的 try 内、且**先于**本段广播调用，
        # 一旦写盘抛出，外层 except 会跳过 broadcast → 字幕整段不显示。时间戳是
        # 锦上添花，绝不允许它把字幕主链路带下水（跨段单调性不在这里保证——
        # 导出侧 _clamp_cues 统一收口，此处如实记录原始值）。
        if self.transcript_path:
            with contextlib.suppress(Exception):
                exporters.write_times(self.transcript_path, self.t0_audio, self.times)

    def retag_course(self, course: str | None) -> str:
        """p2-1：切换当前课程时维护笔记归属。返回 "empty" / "marked" / "none"。

        分流依据 = 笔记是否已有正文（note_has_body）：服务**启动即建笔记头**，而
        用户通常开课前才在侧栏选课程——此时笔记只有头，改写头最干净（否则头里会
        永远写着「未分类」）。一旦已开始记录，改写头会让前半段归属说谎，改为**追加
        一行切换标记**：不新建文件、不丢数据，且 note_course 仍返回原课程（笔记的主
        归属 = 它开课时的课程），切换事实保留在正文里。"""
        p = self.transcript_path
        if not p or not p.is_file():
            return "none"
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            return "none"
        new_name = course or courses_mod.UNCATEGORIZED
        if courses_mod.note_course(text) == new_name:
            return "none"
        stamp = self.stamp or p.stem.removeprefix("lecture_")
        try:
            if note_has_body(text):
                with p.open("a", encoding="utf-8") as f:
                    f.write(f"\n{courses_mod.COURSE_HEADER_PREFIX}{new_name}\n\n")
                return "marked"
            p.write_text(note_header(stamp, new_name), encoding="utf-8")
            return "empty"
        except Exception as e:
            log.warning(f"[courses] ⚠ 笔记课程重标失败: {e}")
            return "none"

    def append(self, ts: str, en: str, zh: str, seg: int | None = None) -> None:
        if not self.transcript_path:
            return
        with self.transcript_path.open("a", encoding="utf-8") as f:
            if en:
                f.write(f"- `{ts}` {en}\n")  # en 只在主循环串行追加，天然有序
            if zh:
                # 翻译乱序完成时晚到的 zh 仍会按完成序落盘：行内携带 `#seg`
                # 锚定它的 EN 父行，防止错误嵌到后一段的 en 之下（WS 端有 seg 键控，
                # 文件路径此前没键控——异步化翻译引入的配对错乱，评审1发现）
                tag = f" `#{seg}`" if seg is not None else ""
                f.write(f"  -{tag} {zh}\n")

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

# status：给字幕页轮询的全量状态；runtime：跨端点共享的可变运行时
status = {"asr": "init", "translate": "init", "mic": "init", "vad": {},
          "backlog": 0, "segments": 0, "last_en": "", "last_zh": "", "paused": False,
          "model": None, "device": None, "compute_type": None, "vram": None,
          "ui_zh_dropped": 0, "streaming": False,
          "version": APP_VERSION,  # p1-3：关于页/诊断展示（只读展示值，非 config 键）
          "lang": "en", "lang_auto": None,  # 条目13：配置语言 / auto 模式下最近采纳的检测语言
          "source": "mic",  # 条目14：当前音源 mic / loopback（apply_segmenter 时刷新）
          "course": courses_mod.UNCATEGORIZED,  # p2-1：当前课程名（未选 = 未分类）
          "fade_lines": DEFAULT_CONFIG["fade_lines"],  # p2-7：历史行渐隐条数（0/1/2）
          "load": None}  # load: {"pct": int, "stage": str} 模型加载进度，完成置 None
                         # ui_zh_dropped: 条目9④ 前端上报「因历史裁剪未显示」的翻译条数
# 条目17：课后摘要任务状态（state: idle/running/done/error；failed=失败块数；
# path=summary 文件路径；error=整体失败原因——分块失败不算整体失败）
status["summary"] = {"state": "idle", "done": 0, "total": 0, "failed": 0,
                     "path": None, "error": None}
# 条目18：Anki 卡组任务状态（state: idle/running/done/error；stage: generating/
# filtering；generated/kept/dropped=生成/保留/过滤丢弃卡数）
status["deck"] = {"state": "idle", "stage": None, "generated": 0, "kept": 0,
                  "dropped": 0, "path": None, "error": None}
# p2-4：模型下载任务终态（进度走 status['load'] 复用进度条；这里只记结果/错误，
# 否则任务一结束进度条清空、失败原因无处可看）
status["model_dl"] = {"state": "idle", "name": None, "error": None, "freed": 0}
# p3-1：全局快捷键状态（enabled/active/reason/bindings）——设置页要能显示「注册失败
# 已降级为仅托盘操作」及其原因，光靠日志用户看不到。形状由 hotkeys.idle_state()
# 单一来源给出，避免这里手写的字段与 HotkeyManager.state() 漂移。
status["hotkeys"] = hotkeys_mod.idle_state()

#: status['load'] 的**写入者令牌**（评审2 修）。build_asr 与「模型下载」都写这条进度条，
#: 而两者的 stage 文案**都含「模型」二字**——旧实现按 `'模型' in stage` 判归属必然误清：
#: 实测下载任务的 finally 把切换中的「加载模型 tiny（首次运行需下载）」清成 None，
#: 此后一整轮模型切换都没有进度条。改为显式令牌：谁写的谁才有权清。
_load_owner: dict = {"token": None}


def set_load_progress(pct: int, stage: str, owner: str) -> None:
    """写模型相关进度并登记所有者（owner 取 "asr" 或 "dl:<模型名>"）。"""
    status["load"] = {"pct": int(pct), "stage": stage}
    _load_owner["token"] = owner


def clear_load_progress(owner: str) -> None:
    """只清**自己写的那条**进度；令牌已被别人接管时不动（那是别人的进度条）。"""
    if _load_owner["token"] == owner:
        _load_owner["token"] = None
        status["load"] = None
runtime: dict = {"asr": None, "segmenter": None, "seg_q": None,
                 "stream_q": None, "scheduler": None,
                 "config": dict(DEFAULT_CONFIG), "prompt": None, "reloading": False,
                 "translator": None,  # 条目16：供热切 translate_model 与术语表双侧同步
                 "cli_glossary": [],  # p2-1：CLI --glossary 基线（课程术语表叠加在其上）
                 "downloading": None, "download_task": None,  # p2-4：模型下载守卫/句柄
                 "summarizing": False, "summary_task": None,  # 条目17：摘要守卫与任务句柄
                 "deck_running": False, "deck_task": None,  # 条目18：卡组守卫与任务句柄
                 "hotkeys": None}  # p3-1：全局快捷键管理器（asr_loop 按持久化配置建）

ARGS = None  # main() 里赋值


# —— p3-1（B12）全局快捷键 ——

def toggle_pause(source: str = "?") -> bool:
    """暂停/恢复字幕的**唯一写入口**（事实源 status['paused']）。

    托盘菜单、全局热键两处入口共用它，字幕页 /api/pause·/api/resume 是「置定值」
    语义（幂等设置，非翻转），保持原样——三条路写的是同一个 status 键，所以
    「UI 轮询 / 托盘 1s 轮询」都会自动跟上，不需要额外的通知机制。"""
    status["paused"] = not bool(status.get("paused"))
    log.info(f"[pause] {source}切换字幕 → {'暂停' if status['paused'] else '继续'}")
    return status["paused"]


def toggle_float(source: str = "?") -> bool:
    """B1 悬浮字幕条显隐的**唯一写入口**（事实源 config.floating_bar）。

    托盘菜单、全局热键共用。窗口本体在桌面壳进程里，显隐由托盘轮询线程按
    config.floating_bar 统一执行（唯一执行点，见 desktop._poll_tray_state），
    此处只改配置并落盘。"""
    cfg = runtime["config"]
    enable = not bool(cfg.get("floating_bar"))
    cfg["floating_bar"] = enable
    with contextlib.suppress(Exception):
        save_config(cfg)
    log.info(f"[float] {source}悬浮字幕条{'开启' if enable else '关闭'}"
             "（config.floating_bar）")
    return enable


def apply_hotkeys(cfg: dict | None = None) -> dict:
    """按配置（重）注册全局快捷键；幂等，**任何失败都不抛出**。

    调用点：① asr_loop 读完持久化配置时（重启后键位跟着 config.json 走，
    与 p2-7 fade_lines 回填同款）；② /api/config 改了热键相关键时（热生效）。
    收尾：lifespan shutdown 里 stop()。

    返回 status['hotkeys'] 的当前快照（便于调用点直接断言/日志）。"""
    cfg = runtime["config"] if cfg is None else cfg
    mgr = runtime.get("hotkeys")
    if mgr is None:
        mgr = hotkeys_mod.HotkeyManager(
            actions={"toggle_pause": lambda: toggle_pause("[快捷键] "),
                     "toggle_float": lambda: toggle_float("[快捷键] ")},
            bindings=hotkeys_mod.bindings_from_config(cfg),
            enabled=bool(cfg.get("hotkeys_enabled", True)))
        runtime["hotkeys"] = mgr
    else:
        mgr.configure(bindings=hotkeys_mod.bindings_from_config(cfg),
                      enabled=bool(cfg.get("hotkeys_enabled", True)))
    # 兜底：热键是锦上添花，注册路径上的任何意外都不能让服务起不来（生命线）
    try:
        snapshot = mgr.start()
    except Exception as e:  # pragma: no cover — start() 自身已不抛，防未来改动回归
        log.warning(f"[hotkeys] ⚠ 注册流程异常（已忽略，热键不可用）: "
                    f"{type(e).__name__}: {e}")
        snapshot = mgr.state()
    status["hotkeys"] = snapshot
    return snapshot


# —— 条目19：LAN token 鉴权（/api/* 与 /ws 全校验；127.0.0.1 豁免只认 socket 对端）——

def is_loopback_peer(host: str | None) -> bool:
    """对端是否环回地址。只信 socket 对端地址（scope['client'][0]，HTTP 侧同为
    Request.client.host），绝不接受 X-Forwarded-For 等请求头（可伪造——技术评审
    审查项）。IPv4-mapped IPv6（::ffff:127.0.0.1）解包后再判；None/非法 → False
    （fail closed）。"""
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def token_ok(provided: str | None, expected: str | None) -> bool:
    """token 恒时比较（hmac.compare_digest 防时序侧信道）；任一侧为空 → False
    （expected 空 = 服务端无 token，对非环回一律拒绝）。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided).encode("utf-8"),
                              str(expected).encode("utf-8"))


def auth_allows(expected: str | None, provided: str | None, peer: str | None,
                enabled: bool = True, exempt_loopback: bool = True) -> bool:
    """条目19① 鉴权判定纯函数（middleware 单一出口，tests/test_auth.py 全矩阵）：
    - enabled=False（auth_enabled=false，计划的回退开关）→ 全放行；
    - 环回对端（exempt_loopback 且 is_loopback_peer(peer)）→ 放行——豁免只认
      socket 对端地址，调用方禁止把请求头传进 peer；
    - 其余非环回：expected 非空且 provided 恒时匹配。"""
    if not enabled:
        return True
    if exempt_loopback and is_loopback_peer(peer):
        return True
    return token_ok(provided, expected)


def local_exempt_enabled() -> bool:
    """环回豁免开关：env LECTURELENS_AUTH_NO_LOCAL_EXEMPT=1 禁用（smoke_test [5]
    子进程用它把本机对端当非环回测，覆盖 403 分支——§2.2-19）。每请求读一次，
    开销可忽略，且便于测试 monkeypatch 注入。"""
    return os.environ.get("LECTURELENS_AUTH_NO_LOCAL_EXEMPT") != "1"


def provided_token(scope) -> str | None:
    """从 ASGI scope 提取请求携带的 token：HTTP 头 X-LectureLens-Token 或
    ?token= 查询参数（HTTP 与 WS 握手同源，头优先）；无 → None。"""
    for k, v in scope.get("headers") or []:
        if k.lower() == b"x-lecturelens-token":
            return v.decode("latin-1").strip() or None
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    vals = qs.get("token")
    return vals[-1] if vals else None


class TokenAuthMiddleware:
    """条目19①：/api/* 与 /ws 全端点 token 鉴权（纯 ASGI，http+websocket 双 scope，
    以后新增的 /api 端点自动纳入，无需逐端点记得加守卫）。拒绝方式：HTTP →
    403 JSON；WS → accept 前发 websocket.close——uvicorn 据此以 HTTP 403 拒绝
    握手（protocols/websockets_impl.py:296-302），smoke_test 裸 socket 断言
    非 101 即成立。豁免只认 scope['client']（socket 对端），不读任何
    X-Forwarded-For 类头。"""
    PROTECTED = ("/api/", "/ws")

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path") or ""
            if any(path == p or path.startswith(p) for p in self.PROTECTED):
                cfg = runtime["config"]
                peer = (scope.get("client") or (None,))[0]
                if not auth_allows(
                    cfg.get("auth_token"), provided_token(scope), peer,
                    enabled=bool(cfg.get("auth_enabled", True)),
                    exempt_loopback=local_exempt_enabled(),
                ):
                    log.warning(f"[auth] ⚠ 拒绝未鉴权请求: {scope['type']} "
                                f"{path} peer={peer}")
                    if scope["type"] == "http":
                        resp = JSONResponse(
                            {"error": "需要访问 token（X-LectureLens-Token 头或 "
                                      "?token=；本机 127.0.0.1 豁免）"},
                            status_code=403)
                        await resp(scope, receive, send)
                    else:
                        await send({"type": "websocket.close", "code": 1008})
                    return
        await self.asgi_app(scope, receive, send)


app.add_middleware(TokenAuthMiddleware)


def provision_token() -> str | None:
    """条目19①：启动时确保 config.json 存在随机 auth_token——缺失则用 secrets
    生成（源码/示例不写字面量）、写回文件并 print 到控制台。token 不经 logging
    （不落 logs/lecturelens.log，RedactFilter 也不认识它——不打印即不泄漏）。
    返回当前 token；写盘失败也返回生成值（LAN 访问 fail closed，本机豁免不受
    影响）。只增补 auth_token 键，不动文件里其他内容。"""
    try:
        on_disk: dict = {}
        if CONFIG_PATH.is_file():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            on_disk = loaded if isinstance(loaded, dict) else {}
    except Exception as e:
        log.warning(f"[auth] ⚠ 读取 config.json 失败（{e}），将重建含 token 的配置")
        on_disk = {}
    token = on_disk.get("auth_token")
    if token:
        log.info("[auth] 访问 token 已存在（config.json: auth_token），LAN 访问需携带")
        return str(token)
    token = secrets.token_urlsafe(24)
    on_disk["auth_token"] = token
    try:
        CONFIG_PATH.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        log.warning(f"[auth] ⚠ token 写入 config.json 失败（{e}）——LAN 访问将被"
                    "拒绝，本机 127.0.0.1 豁免不受影响")
        return token
    print(f"\n[auth] 已生成 LAN 访问 token（已写入 config.json 的 auth_token）：\n"
          f"       {token}\n"
          f"       手机等非本机设备首次访问时粘贴一次，字幕页会记住（localStorage）。\n")
    log.info("[auth] 已生成访问 token 并写入 config.json（值不落日志）")
    return token


def _resolve_build_cfg(overrides: dict | None = None) -> dict:
    """条目4 save-after-build：试构建配置 = runtime['config'] + overrides（不改原）。"""
    cfg = dict(runtime["config"])
    if overrides:
        cfg.update(overrides)
    return cfg


def build_asr(overrides: dict | None = None) -> None:
    """按 runtime['config']（可被 overrides 试构建）加载 ASR 并原子替换 runtime['asr']
    （旧模型在飞请求不受影响）。全程打点 status['load']（百分比+阶段），字幕页据此显示进度条。
    条目4④ save-after-build：模型键以 overrides 构建，成功才合并进 runtime['config']
    （持久化由 /api/config 的 _reload 完成）——构建失败时 runtime 与 config.json 零污染。"""
    cfg = _resolve_build_cfg(overrides)
    set_load_progress(12, f"加载模型 {cfg['model']}（首次运行需下载）", "asr")
    try:
        asr = WhisperASR(model_size=cfg["model"], device=cfg["device"],
                         compute_type=cfg["compute_type"],
                         language=cfg.get("lang") or "en",  # 条目13②：读 config（重启后持久化生效）
                         model_path=str(mp) if (mp := bundled_model_path(cfg["model"]))
                         else None)  # 条目20：frozen 用随包本地路径，开发模式 None 走 HF 缓存
        set_load_progress(65, f"{asr.device.upper()} 预热推理", "asr")
        asr.warmup()  # 消化 kernel 编译等一次性开销，首句不卡
        runtime["asr"] = asr
        if overrides:
            runtime["config"].update(overrides)  # 构建成功才合并（条目4）
        status["asr"] = "ready"
        status["model"] = asr.model_size
        status["device"] = asr.device
        status["compute_type"] = asr.compute_type
        clear_load_progress("asr")
    except Exception as e:
        clear_load_progress("asr")
        status["asr"] = f"error: {e}"
        raise


_segmenter_lock = threading.Lock()  # 串行化重建：watchdog 与 /api/config 的 vad 切换可并发进入


def apply_segmenter() -> None:
    """按 config 的 vad/source 重建分句器（共用同一个音频队列），旧线程优雅停止。
    全函数持锁：'读 old → 写 runtime[segmenter] → stop old' 若被并发交错，
    会留下一个从未被 stop 的孤儿分句器（两份音频进同一队列，且 healthy 掩盖故障）。
    锁内的分句器构造（含 VAD 加载）与 stop 的 join 都在 to_thread 里执行，
    不会阻塞事件循环。
    条目14：source=loopback 时用 LoopbackSegmenter（WASAPI loopback，
    强制 silero VAD 在类内保证）；device 改读 config 的 input_device
    （计划点名的 main.py:150 硬编码修正；键的白名单化/UI 归条目15），
    无该键时回退 CLI --device。watchdog（status_loop）按 healthy 对两种
    音源通用，不健康时重建也走本函数 = 自动遵循当前 source。"""
    with _segmenter_lock:
        cfg = runtime["config"]
        # p2-5：断句参数在**构造时**读入（状态机把它们缓存成 samples/blocks），
        # 故改这三个键必须走重建；backlog_* 是每轮现读的，不在此列。
        seg_kw = segment_kwargs(cfg)
        if cfg.get("source") == "loopback":
            seg = LoopbackSegmenter(runtime["seg_q"], stream_q=runtime["stream_q"],
                                    **seg_kw)
        else:
            seg = MicSegmenter(runtime["seg_q"],
                               device=cfg.get("input_device", ARGS.device),
                               vad=cfg["vad"], stream_q=runtime["stream_q"], **seg_kw)
        seg.start()
        old = runtime["segmenter"]
        runtime["segmenter"] = seg
        status["source"] = cfg.get("source", "mic")
        if old:
            old.stop()


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/privacy")
async def privacy():
    """p1-3：隐私透明页（B9 最小版；design-borrowing.md:64 的完整版待后续任务）。
    路径不在 /api/、/ws 前缀内 → 不触发条目19 token 鉴权，关于页/浏览器可直接
    链接；LAN 对端也可读（页面只含静态声明，无任何接口数据）。"""
    return FileResponse(WEB / "privacy.html")


@app.get("/float")
async def float_bar():
    """p1-7：B1 悬浮字幕条页面（桌面壳第二窗口载入；浏览器直接打开亦可预览）。
    与 /privacy 同为非 /api 路径：不触发鉴权（内容只是最新一句字幕展示）。"""
    return FileResponse(WEB / "float.html")


# p1-7：B1 悬浮条的桌面壳控制回调（desktop.py 启动后注册；纯服务/浏览器环境保持空）。
# 注册方向 desktop→main（main 不 import desktop，防循环依赖）；端点据此把请求转给
# 桌面壳执行（ctypes 需要进程内窗口句柄，只有桌面壳有）。
float_control: dict = {}


@app.post("/api/float/click-through")
async def float_click_through(req: Request):
    """p1-7：B1 悬浮条点击穿透开关（尽力而为——Win32 WS_EX_TRANSPARENT 由桌面壳执行，
    失败自动降级为可关闭窗口）。未注册（纯服务模式/浏览器直开）→ 503 + 明确 error，
    页面据此提示「穿透仅桌面版可用」。"""
    body = await req.json()
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        return JSONResponse({"ok": False, "error": "on 须为布尔值"}, status_code=400)
    fn = float_control.get("set_click_through")
    if fn is None:
        return JSONResponse(
            {"ok": False, "error": "桌面壳不可用（纯服务模式）——点击穿透仅桌面版可用"},
            status_code=503)
    try:
        ok = bool(fn(bool(body["on"])))
    except Exception as e:
        log.warning(f"[float] ⚠ 点击穿透执行失败: {e}")
        ok = False
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "点击穿透设置失败（已降级为可关闭窗口）"},
            status_code=501)
    return {"ok": True, "on": bool(body["on"])}


@app.get("/api/status")
async def api_status():
    # p3-2：术语修订号现算进 status——前端 2s 轮询发现它变了就丢弃术语高亮缓存
    # 重拉（含把已渲染字幕按新词表重刷）。**不在每条术语变更路径上挂钩子**：
    # 变更入口有三条（POST 写盘 / 课程切换 / mtime 热重载），挂钩必漏一条，
    # 而现算只是 O(词条数)——本机实测 10 条 14µs / 100 条 76µs / 500 条 346µs
    # （timeit 各 2000 次均值），相对 2s 轮询周期可忽略。
    status["glossary_rev"] = glossary_terms_payload()["rev"]
    return JSONResponse(status)


@app.get("/api/config")
async def get_config():
    # 评审修复②：auth_token 不出 API——端点曾原样 JSONResponse(runtime["config"])，
    # 生产态 token 会明文返回给环回豁免对端与持 token 的 LAN 客户端（叠加环回豁免
    # 无 Origin 校验，DNS-rebinding 场景可读走凭据）。token 只经首启 print /
    # config.json 分发；POST 侧本就封禁（不在 ALLOWED_CONFIG_KEYS）。
    return JSONResponse({k: v for k, v in runtime["config"].items()
                         if k != "auth_token"})


# —— p3-3（B12）开机自启：状态与开关 ——
# 事实源 = HKCU Run 注册表项本身（不设本地标记、不加 config 键）：用户手工删掉
# 启动项 / 系统策略清掉它，GET 下次就如实回「未启用」。
@app.get("/api/autostart")
async def api_autostart_get():
    """回读真实注册表（winreg）——读失败（非 Windows/权限）也返回 200 + ok=false +
    error，让设置页能把原因显示出来，而不是静默显示成「未启用」。"""
    return autostart_mod.state(ROOT)


@app.post("/api/autostart")
async def api_autostart_set(req: Request):
    """开/关开机自启。**只接受 enabled 一个布尔键**——写入 Run 键的是「开机自动
    执行的命令」，命令一律由 autostart 模块按固定规则构造；接受客户端给的路径/
    命令片段等于把开机执行权开放给任何持 token 的对端（含 DNS-rebinding 下的恶意
    页面）。未知键一律 400（与 /api/config 的「未知配置键」同款纪律）：若只取
    enabled 而放过夹带的 command，客户端会以为它生效了——那是最坏的一种「静默」。"""
    body = await optional_json(req)
    extra = set(body) - {"enabled"}
    if extra:
        return JSONResponse(
            {"ok": False,
             "error": f"未知字段: {sorted(extra)}（本端点只接受 enabled，"
                      f"启动命令由服务端按当前安装位置构造）"},
            status_code=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False,
             "error": f"非法 enabled: {enabled!r}（须为布尔 true/false）"},
            status_code=400)
    res = autostart_mod.enable(ROOT) if enabled else autostart_mod.disable(ROOT)
    # 写/删失败（或回读校验不过）→ 5xx + 明确 error：开关不能假成功
    return JSONResponse(res, status_code=200 if res.get("ok") else 500)


@app.get("/api/devices")
async def api_devices():
    """条目15①：过滤后的输入设备列表（max_input_channels > 0），UI 设备下拉消费。
    current = config 当前的 input_device（null = 跟随系统默认）。
    枚举失败不 500：devices=[] + error 字段——字幕页照常工作，仅下拉为空
    （手机等无输入设备的场景也走这里，前端显示系统默认一项）。"""
    devices: list = []
    err: str | None = None

    def _enum() -> list:
        import sounddevice as sd
        devs = sd.query_devices()
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = -1
        return [{"index": i, "name": d["name"], "inputs": int(d["max_input_channels"]),
                 "default": i == default_in}
                for i, d in enumerate(devs) if d["max_input_channels"] > 0]

    try:
        devices = await asyncio.to_thread(_enum)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.warning(f"[devices] ⚠ 枚举输入设备失败: {err}")
    payload: dict = {"devices": devices,
                     "current": runtime["config"].get("input_device")}
    if err:
        payload["error"] = err
    return JSONResponse(payload)


@app.post("/api/config")
async def set_config(req: Request):
    # 竞态守卫（评审1 TOCTOU）：检查与置位之间不得有 await——否则两个并发 POST
    # 都能通过守卫、各自 spawn _reload（实测两个 WhisperModel 并发加载，后完成者
    # 覆盖 runtime['asr']）。预约后全程持守卫（覆盖 req.json / apply_segmenter 两个
    # 让出点），错误路径由 finally 释放；模型键路径把守卫移交 _reload（其 finally 释放）。
    if runtime["reloading"]:
        return {"ok": False, "error": "模型切换进行中，稍后再试"}
    runtime["reloading"] = True
    handed_off = False
    try:
        body = await req.json()
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"},
                                status_code=400)
        cfg = runtime["config"]
        # —— 条目4②：先整体校验，动任何状态之前完成（runtime/config.json/status 零触碰）——
        changes: dict = {}
        for k, v in body.items():
            if k not in ALLOWED_CONFIG_KEYS:
                return JSONResponse(
                    {"ok": False,
                     "error": f"未知配置键: {k}（允许: {list(ALLOWED_CONFIG_KEYS)}）"},
                    status_code=400)
            if k == "input_device":
                # 条目15②：动态索引校验。null = 重置为跟随系统默认（其他键 null 仍是
                # no-op 占位——input_device 必须可重置所以特判）；bool 是 int 子类须排除
                if isinstance(v, bool) or (v is not None and
                                           (not isinstance(v, int) or v < 0)):
                    return JSONResponse(
                        {"ok": False,
                         "error": f"非法 input_device: {v!r}（须为非负整数索引或 null）"},
                        status_code=400)
                if v is not None:
                    problem = _input_device_problem(v)
                    if problem:
                        return JSONResponse({"ok": False, "error": problem},
                                            status_code=400)
                if v == cfg.get(k):
                    continue  # 与现值相同 = no-op
                changes[k] = v
                continue
            if k in hotkeys_mod.HOTKEY_CONFIG_KEYS:
                # p3-1：热键组合串。写在 no-op 判空**之前**——组合串本身不是空串
                # 语义的值，且要比对的是规范化之后的等价性（"Ctrl+Alt+L" 与现值
                # "ctrl+alt+l" 是同一个键位，不该被当成一次变更去重注册）。
                if not isinstance(v, str):
                    return JSONResponse(
                        {"ok": False,
                         "error": f"非法 {k}: {v!r}（须为字符串，如 ctrl+alt+l）"},
                        status_code=400)
                problem = _hotkey_problem(k, v)
                if problem:
                    return JSONResponse({"ok": False, "error": problem},
                                        status_code=400)
                if hotkeys_mod.normalize(v) == cfg.get(k):
                    continue  # 规范化后与现值相同 = no-op
                changes[k] = hotkeys_mod.normalize(v)  # 存规范形（去重/比对唯一口径）
                continue
            if v is None or v == "" or v == cfg.get(k):
                continue  # 未提供/空串/与现值相同 = no-op（前端下拉占位语义）
            if k in CONFIG_NUM_RANGES:
                # p2-5：数值键走区间校验（不查 CONFIG_VALUE_CHOICES——那里是枚举表）
                problem = _config_number_problem(k, v)
                if problem:
                    return JSONResponse({"ok": False, "error": problem},
                                        status_code=400)
                changes[k] = v
                continue
            choices = CONFIG_VALUE_CHOICES[k]
            if v not in choices:
                allowed = [c for c in choices if c is not None]
                return JSONResponse(
                    {"ok": False, "error": f"非法 {k}: {v!r}（允许: {allowed}）"},
                    status_code=400)
            changes[k] = v
        if not changes:
            return {"ok": True, "changed": {}}
        # p2-5：跨键组合校验——单键各自合法不代表组合合法（最典型：hard_cut_s 调到
        # 低于 max_seg_s，面板上两个数字都「在范围内」，却把等静音语义抵消掉）。
        # 与逐键校验同款纪律：任何状态改动之前完成。
        cross = _config_cross_problem({**cfg, **changes})
        if cross:
            return JSONResponse({"ok": False, "error": cross}, status_code=400)
        # 评审2 修：模型键与「模型下载」双向互斥。下载侧本来就拦了 reloading，
        # 但 /api/config 的模型键没拦 downloading——实测 downloading='small' 在跑时
        # POST {model:'tiny'} 返回 200 并真的开建，两者共用同一条 status['load']
        # 进度条，会互相覆盖。此处拦在**任何状态改动之前**（与逐键校验收尾一致）。
        dl_busy = runtime.get("downloading")
        if dl_busy and any(k in MODEL_KEYS for k in changes):
            return JSONResponse(
                {"ok": False,
                 "error": f"模型 {dl_busy} 正在下载，暂不能切换模型"
                          f"（两者共用加载进度条，且下载会占用磁盘与带宽）"},
                status_code=409)

        # —— 即时键（vad/lang 等）：赋值 → 保存 → apply_segmenter（条目4③，现状语义）——
        instant = {k: v for k, v in changes.items() if k not in MODEL_KEYS}
        if instant:
            cfg.update(instant)  # 整体校验已通过，此时赋值不再污染
            save_config(cfg)
            if ("vad" in instant or "source" in instant or "input_device" in instant
                    or any(k in instant for k in SEGMENT_REBUILD_KEYS)):
                # 守卫期间执行（silero 加载秒级）：期间到达的模型键 POST 被拒——
                # 语义收紧为「配置写入串行化」，防即时键与构建并发交错。
                # 条目14：source 热切换复用 apply_segmenter（旧线程优雅停止）
                # 条目15：input_device 变更同走重建；重建失败保留旧采集器
                # （apply_segmenter 先构造后 swap，构造抛出时 runtime['segmenter']
                # 未动、旧线程照跑）——返回干净错误响应而非裸 500。同请求若还带
                # 模型键则未受理（save-after-build 未启动），前端可单独重发。
                try:
                    await asyncio.to_thread(apply_segmenter)
                except Exception as e:
                    log.warning(f"[config] ⚠ 采集器重建失败（已保留旧采集器）: {e}")
                    return JSONResponse(
                        {"ok": False,
                         "error": f"采集器重建失败（已保留旧采集器；新值已存入配置，"
                                  f"重启后生效）: {e}"},
                        status_code=500)
            if "fade_lines" in instant:
                # p2-7：纯呈现参数——只进 status 供字幕页下一轮轮询采用（无需刷新）
                status["fade_lines"] = instant["fade_lines"]
                log.info(f"[display] 历史行渐隐条数 → {instant['fade_lines']}")
            if any(k in instant for k in hotkeys_mod.HOTKEY_CONFIG_KEYS) \
                    or "hotkeys_enabled" in instant:
                # p3-1：热键热生效——重绑=先注销再注册（configure+start 幂等）。
                # 在事件循环里直接调：add_hotkey 是毫秒级（库内部起守护线程装钩子），
                # 不阻塞到需要 to_thread 的程度；失败只降级、不抛（apply_hotkeys 兜底）。
                apply_hotkeys(cfg)
            if "lang" in instant:
                # 条目13①：语言热切换——改 asr 实例属性即生效，无需重建模型；
                # 重置 auto 检测历史，防旧语言的短段沿用污染新模式
                asr = runtime["asr"]
                if asr is not None:
                    asr.language = instant["lang"]
                    asr.detected_lang = None
                status["lang"] = instant["lang"]
                status["lang_auto"] = None
                log.info(f"[lang] ASR 语言热切换: {instant['lang']}（无需重建模型）")
            if "translate_model" in instant:
                # 条目16：翻译模型热切换——translator.model 每次请求时读，改属性即生效
                tr = runtime.get("translator")
                if tr is not None:
                    tr.model = instant["translate_model"]
                    log.info(f"[config] translate.model 热切换: {instant['translate_model']}")
            log.info(f"config 已更新: {instant}")  # 关键字「config 已更新」= 条目3验收口径
            if "streaming" in instant:
                log.info("[config] streaming 开关已保存，重启服务后生效（条目11 灰度）")

        # —— 模型键（条目4④ save-after-build）：以 overrides 构建，成功才合并+持久化；
        # 失败仅置 status['asr']=error（build_asr 内），runtime['config'] 与 config.json 零污染 ——
        build_overrides = {k: v for k, v in changes.items() if k in MODEL_KEYS}
        if build_overrides:
            handed_off = True  # 守卫移交 _reload，由其 finally 释放
            status["asr"] = f"loading model ({build_overrides.get('model', cfg['model'])})"
            log.info(f"config 已受理（save-after-build）: {build_overrides}")

            async def _reload():
                try:
                    await asyncio.to_thread(build_asr, build_overrides)
                    save_config(runtime["config"])  # 构建成功才持久化（合并已在 build_asr 内完成）
                    log.info(f"config 已更新: {build_overrides}")
                except Exception as e:
                    log.warning(f"[config] ⚠ 模型切换失败: {e}")  # build_asr 已写 status，零污染
                finally:
                    runtime["reloading"] = False

            asyncio.create_task(_reload())

        return {"ok": True, "changed": changes}
    finally:
        if not handed_off:
            runtime["reloading"] = False


@app.post("/api/config/segmentation/reset")
async def reset_segmentation():
    """p2-5（B4）：把断句/积压调优键恢复成 DEFAULT_CONFIG 的值。

    默认值**只存在于后端的 DEFAULT_CONFIG 一处**——前端不抄一份数字，否则两处默认
    迟早漂移（面板显示 700、实际写 800 这类故障最难查）。恢复结果同样过一遍跨键
    约束：万一有人把默认值改坏了，这里会明确报错，而不是静默写入非法组合。
    只在确实改了「需重建」的键时才动采集器（只改积压阈值不必打断在飞的那一段）。"""
    if runtime["reloading"]:
        return JSONResponse({"ok": False, "error": "模型切换进行中，稍后再试"},
                            status_code=409)
    targets = {k: DEFAULT_CONFIG[k] for k in SEGMENT_KEYS}
    cross = _config_cross_problem({**runtime["config"], **targets})
    if cross:
        return JSONResponse(
            {"ok": False, "error": f"内置默认值自身不合法（请报 bug）: {cross}"},
            status_code=500)
    cfg = runtime["config"]
    changes = {k: v for k, v in targets.items() if cfg.get(k) != v}
    if not changes:
        return {"ok": True, "changed": {}, "values": targets}
    cfg.update(changes)
    save_config(cfg)
    if any(k in changes for k in SEGMENT_REBUILD_KEYS):
        try:
            await asyncio.to_thread(apply_segmenter)
        except Exception as e:
            log.warning(f"[config] ⚠ 采集器重建失败（已保留旧采集器）: {e}")
            return JSONResponse(
                {"ok": False,
                 "error": f"默认值已存入配置，但采集器重建失败（重启后生效）: {e}"},
                status_code=500)
    log.info(f"[config] 分句参数已恢复默认: {changes}")
    return {"ok": True, "changed": changes, "values": targets}


@app.post("/api/first-run/apikey")
async def first_run_apikey(req: Request):
    """p1-5 首启向导②：DeepSeek key 热接入，免重启。
    流程：写 ROOT/.env（DEEPSEEK_API_KEY=...，.env 已 gitignore——凭据不进 git）
    并同步进程 env（os.environ 优先级高于 .env，不同步会导致重启后被旧 shell
    env 顶回）→ 热重建 runtime['translator']（asr_loop 主循环 p1-5 起读 runtime，
    下一段翻译即用新实例）→ status['translate']='ready'。
    key 只收不回：响应与日志均不含 key（RedactFilter 兜底）；/api/ 前缀自动纳入
    条目19 token 鉴权。key 有效性由向导③真实翻译验证，失败可回②重填。"""
    body = await req.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"},
                            status_code=400)
    key = str(body.get("api_key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "api_key 不能为空"}, status_code=400)
    try:
        env_path = ROOT / ".env"
        lines = (env_path.read_text(encoding="utf-8").splitlines()
                 if env_path.is_file() else [])
        lines = [ln for ln in lines if not ln.strip().startswith("DEEPSEEK_API_KEY=")]
        lines.append(f"DEEPSEEK_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["DEEPSEEK_API_KEY"] = key
    except Exception as e:
        log.warning(f"[first-run] ⚠ .env 写入失败: {e}")
        return JSONResponse({"ok": False, "error": f".env 写入失败: {e}"},
                            status_code=500)
    try:
        tr = await asyncio.to_thread(
            DeepSeekTranslator, key,
            model=runtime["config"].get("translate_model") or "deepseek-chat",
            glossary_files=[])
        runtime["translator"] = tr
        runtime["prompt"] = glossary_prompt(tr.entries) if tr.entries else None
        # p2-1：向导热接 key 后同样接上当前课程术语表（否则首次配 key 会丢课程表）
        with contextlib.suppress(Exception):
            _cd = courses_mod.load(COURSES_PATH)
            apply_course_glossary(courses_mod.find(_cd, _cd.get("active"))
                                  if _cd.get("active") else None)
        status["translate"] = "ready"
    except Exception as e:
        status["translate"] = f"error: {e}"
        log.warning(f"[first-run] ⚠ 翻译器重建失败: {e}")
        return JSONResponse({"ok": False,
                             "error": f"key 已存入 .env，但翻译器重建失败: {e}"},
                            status_code=500)
    log.info("[first-run] DeepSeek key 已接入（.env 已更新，翻译器热重建，值不落日志）")
    return {"ok": True, "translate": "ready"}


@app.get("/api/security")
async def api_security():
    """p1-6 设置页安全/凭据卡：只回放布尔与来源枚举，**任何凭据字符都不回传**
    （沿用评审修复②「token 不出 API」口径——设置页的掩码是固定占位符，不反映
    真实 token；DEEPSEEK_API_KEY 同理只报存在性，值永远只在环境变量/.env 里）。"""
    cfg = runtime["config"]
    key_env = bool(os.environ.get("DEEPSEEK_API_KEY"))
    key_file = False
    try:
        env_path = ROOT / ".env"
        if env_path.is_file():
            key_file = any(
                ln.strip().startswith("DEEPSEEK_API_KEY=")
                and ln.split("=", 1)[1].strip()
                for ln in env_path.read_text(encoding="utf-8").splitlines())
    except Exception as e:
        log.debug(f"[security] .env 检查失败: {e}")
    return {"auth_enabled": bool(cfg.get("auth_enabled", True)),
            "auth_token_set": bool(cfg.get("auth_token")),
            "key_set": key_env or key_file,
            "key_source": "env" if key_env else ("file" if key_file else "none")}


@app.post("/api/security/token/regenerate")
async def regenerate_token():
    """p1-6 设置页安全卡：重新生成 LAN token（旧值立即失效，已配对手机需重新粘贴）。
    新值仅本响应回传一次（等价首启 provision_token 的 print 信息流）；值不落日志。
    与「auth_token 不可经 API 设置」（DEFAULT_CONFIG 注释）不冲突：端点不接受任何
    输入值，随机性由服务端 secrets 保证——用户无法指定 token。写盘只增补 auth_token
    单键（provision_token 同款），不动 config.json 其他内容（防把 CLI 覆盖值顺带持久化）。"""
    token = secrets.token_urlsafe(24)
    runtime["config"]["auth_token"] = token
    try:
        on_disk: dict = {}
        if CONFIG_PATH.is_file():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            on_disk = loaded if isinstance(loaded, dict) else {}
        on_disk["auth_token"] = token
        CONFIG_PATH.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        log.warning(f"[security] ⚠ token 写盘失败: {e}——本进程内已生效，重启后可能回退")
        return JSONResponse({"ok": False, "error": f"token 写盘失败: {e}"},
                            status_code=500)
    log.info("[security] LAN token 已重新生成（值不落日志；已配对设备需用新值重连）")
    return {"ok": True, "token": token}


@app.post("/api/pause")
async def pause():
    status["paused"] = True
    return {"ok": True, "paused": True}


@app.post("/api/resume")
async def resume():
    status["paused"] = False
    return {"ok": True, "paused": False}


@app.get("/api/notes")
async def notes():
    if hub.transcript_path and hub.transcript_path.is_file():
        return FileResponse(hub.transcript_path, filename=hub.transcript_path.name,
                            media_type="text/markdown")
    return JSONResponse({"error": "no transcript yet"}, status_code=404)


# —— p2-2：笔记库（列表 / 预览 / 按名导出）——
# name 全部走 notes.safe_note_path（目录内 lecture_*.md 白名单 + 反越界），
# 缺省则回落到当前正在记的笔记 —— 老调用方（不带 name）行为零变化。


async def optional_json(req: Request) -> dict:
    """容错读 JSON 体：无体/坏 JSON/非对象 → {}。

    必要性：/api/notes/summarize 与 /api/notes/deck 历史上**不带请求体**调用
    （scripts/smoke_test.py、既有 pytest 用例），直接 await req.json() 会抛，
    故 name 这类可选参数必须走这个入口。"""
    try:
        body = await req.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def note_target(name) -> tuple[Path | None, str | None]:
    """解析目标笔记 → (路径, 错误文案)。

    给了 name 但非法/不存在时**返回错误而不回退**到当前笔记——静默回退会把
    摘要/卡组写到用户没指定的那篇上，比报错更糟。"""
    if name is None or (isinstance(name, str) and not name.strip()):
        p = hub.transcript_path
        if not (p and p.is_file()):
            return None, "no transcript yet"
        return p, None
    p = notes_mod.safe_note_path(TRANSCRIPTS, name)
    if p is None:
        return None, f"非法笔记名: {name!r}（只接受 transcripts/ 下的 lecture_*.md）"
    if not p.is_file():
        return None, f"笔记不存在: {name}"
    return p, None


@app.get("/api/notes/list")
async def notes_list(q: str = "", course: str = ""):
    """p2-2 笔记库列表：日期/课程/首句摘要/是否已有摘要与卡组 + 全文检索。

    q 为全文检索（**含中文子串**，见 notes._matches 的说明）；course 按课程名
    精确过滤（侧栏课程下钻）。数据源是 transcripts/ 里的真文件，无独立索引。"""
    data = notes_mod.index(TRANSCRIPTS, query=q, course=course)
    data["ok"] = True
    data["query"] = q
    data["current"] = hub.transcript_path.name if hub.transcript_path else None
    return data


@app.get("/api/notes/preview")
async def notes_preview(name: str = ""):
    """p2-2 双栏 EN|ZH 对照预览：解析指定笔记的 EN/ZH 行对。

    配对规则见 notes.parse 模块注释（第 i 条 EN ↔ `#i` 的 ZH）。"""
    p, err = note_target(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    parsed = notes_mod.parse(p.read_text(encoding="utf-8"))
    en, zh = notes_mod.first_sentence(parsed["pairs"])
    return {
        "ok": True,
        "name": p.name,
        "date": notes_mod.display_date(parsed["stamp"], p.name),
        "stamp": parsed["stamp"],
        "course": parsed["course"] or courses_mod.UNCATEGORIZED,
        "segments": parsed["segments"],
        "translated": parsed["paired"],
        "first_en": en,
        "first_zh": zh,
        "is_current": bool(hub.transcript_path and hub.transcript_path.name == p.name),
        "pairs": parsed["pairs"],
    }


@app.get("/api/notes/file")
async def notes_file(name: str = ""):
    """p2-2 按名导出/下载单篇笔记（md）。name 缺省 = 当前笔记（等价 GET /api/notes）。"""
    p, err = note_target(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    return FileResponse(p, filename=p.name, media_type="text/markdown")


#: p2-3 导出中心的格式白名单 → (扩展名, 媒体类型)。srt/pdf/docx 现生成，md 直接回原文
EXPORT_FORMATS = {
    "md": ("md", "text/markdown"),
    "srt": ("srt", "application/x-subrip"),
    "pdf": ("pdf", "application/pdf"),
    "docx": ("docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


def export_meta(p: Path, parsed: dict) -> dict:
    """PDF/Word 的标题与元信息行（两文档同构，单一来源）。"""
    stamp = parsed.get("stamp") or p.stem.removeprefix("lecture_")
    course = parsed.get("course") or courses_mod.UNCATEGORIZED
    date = notes_mod.display_date(parsed.get("stamp"), p.name)
    return {
        "title": f"双语课堂笔记 {stamp}",
        "subtitle": f"课程：{course} · {date} · {parsed['segments']} 段"
                    f"（已译 {parsed['paired']}）",
        "course": course,
        "date": date,
    }


@app.get("/api/notes/export")
async def notes_export(fmt: str = "md", name: str = ""):
    """p2-3 导出中心：一篇笔记导出为 md / srt / pdf / docx。

    name 缺省 = 当前笔记；走 note_target（目录内 lecture_*.md 白名单，反越界）。
    **失败必须用户可见**：格式非法 400、笔记不存在/非法 404、生成异常 500 且响应体
    带 `ok:false + error`（前端用 fetch 读它并弹 toast，不是丢一个白页给用户）。"""
    key = (fmt or "md").strip().lower()
    if key not in EXPORT_FORMATS:
        return JSONResponse(
            {"ok": False,
             "error": f"不支持的导出格式 fmt={fmt!r}（可选: {list(EXPORT_FORMATS)}）"},
            status_code=400)
    p, err = note_target(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    ext, media = EXPORT_FORMATS[key]
    if key == "md":
        return FileResponse(p, filename=p.name, media_type=media)
    out_name = f"{p.stem}.{ext}"

    def _render() -> bytes:
        """读盘 + 解析 + 生成，全在**线程**里跑（见下方 to_thread 说明）。"""
        parsed = notes_mod.parse(p.read_text(encoding="utf-8"))
        pairs = parsed["pairs"]
        sidecar = exporters.read_times(p)
        if key == "srt":
            return exporters.build_srt(pairs, sidecar)[0].encode("utf-8")
        meta = export_meta(p, parsed)
        return (exporters.build_pdf(pairs, meta, sidecar) if key == "pdf"
                else exporters.build_docx(pairs, meta, sidecar))

    try:
        # 评审1 修：导出是**同步 CPU/IO 重活**，直接在协程里跑会卡住整个事件循环。
        # 实测（105 段真实笔记）fmt=pdf 处理 661.6ms，同期事件循环最大空档 661.7ms
        # （对照：docx 124.7ms / srt 3.5ms）——即整段导出期间 WS 广播、asr_loop
        # 取段与广播、翻译任务全部停摆；外推 1500 段（约 2 小时课）build_pdf 约
        # 1.9s，恢复后积压还可能触发追赶丢弃而丢段。与其它重活（build_asr /
        # apply_segmenter / summarize / download）一致，一律 to_thread。
        body = await asyncio.to_thread(_render)
    except Exception as e:
        log.warning(f"[export] ⚠ {key} 导出失败（{p.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"{key} 导出失败: {type(e).__name__}: {e}"},
            status_code=500)
    log.info(f"[export] {key} 导出 {p.name} → {out_name}（{len(body)} 字节）")
    return Response(content=body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{out_name}"',
        "Content-Length": str(len(body)),
    })


@app.post("/api/notes/summarize")
async def notes_summarize(req: Request):
    """条目17：课后 AI 笔记摘要（map-reduce）。受理即返回（LLM 摘要耗时分钟级，
    不占请求），进度看 /api/status 的 summary 键或 WS type=summary 消息；产物写
    transcript 同目录同名 .summary.md（lecture_*.summary.md，条目18 消费块摘要）。
    前置校验先于置位（检查+置位之间无 await，与 /api/config 守卫同款纪律）；
    同一时刻只允许一个摘要任务（并发第二请求 409）。失败语义：整体失败（如
    空转写）只写 status['summary']['error'] 不 500——单块失败由 summarize.py
    标记进产物，绝不整体失败。"""
    if runtime["summarizing"]:
        return JSONResponse({"ok": False, "error": "摘要已在进行中，请等待完成"},
                            status_code=409)
    # p2-2：可指定历史笔记（body {name}）；缺省 = 当前笔记（老行为不变）
    body = await optional_json(req)
    tpath, err = note_target(body.get("name"))
    if tpath is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    tr = runtime.get("translator")
    if tr is None:
        return JSONResponse(
            {"ok": False, "error": "翻译未启用（无 DEEPSEEK_API_KEY），摘要不可用"},
            status_code=409)
    # tpath 已是本次任务的快照：摘要期间 start_transcript 换新笔记不影响本次
    out_path = tpath.with_name(tpath.stem + ".summary.md")
    runtime["summarizing"] = True

    def _snapshot() -> dict:
        return {"type": "summary", **status["summary"]}

    def _progress(done: int, total: int) -> None:
        status["summary"]["done"] = done
        status["summary"]["total"] = total
        asyncio.create_task(hub.broadcast(_snapshot()))

    async def _run() -> None:
        try:
            status["summary"] = {"state": "running", "done": 0, "total": 0,
                                 "failed": 0, "path": str(out_path), "error": None}
            result = await TranscriptSummarizer(tr).summarize(
                tpath, out_path, on_progress=_progress)
            status["summary"].update(state="done", failed=len(result["failed"]),
                                     error=None)
            await hub.broadcast(_snapshot())
            log.info(f"[summary] ✅ 完成: {out_path.name}"
                     f"（{result['total']} 块，失败 {len(result['failed'])}）")
        except Exception as e:
            status["summary"].update(state="error", error=f"{type(e).__name__}: {e}")
            log.warning(f"[summary] ⚠ 摘要失败: {e}")
            with contextlib.suppress(Exception):
                await hub.broadcast(_snapshot())
        finally:
            runtime["summarizing"] = False
            runtime["summary_task"] = None

    runtime["summary_task"] = asyncio.create_task(_run())
    return {"ok": True, "started": True, "transcript": tpath.name,
            "output": out_path.name}


@app.get("/api/notes/deck")
async def notes_deck_download(name: str = ""):
    """条目18：下载 .apkg 卡组（先 POST /api/notes/deck 生成）。
    p2-2：?name= 指定历史笔记；缺省 = 当前笔记（老行为不变）。"""
    p, err = note_target(name)
    if p is None:
        return JSONResponse({"error": err}, status_code=404)
    apkg = p.with_name(p.stem + ".apkg")
    if apkg.is_file():
        return FileResponse(apkg, filename=apkg.name,
                            media_type="application/octet-stream")
    return JSONResponse(
        {"error": "no deck yet（先 POST /api/notes/deck 生成）"}, status_code=404)


@app.post("/api/notes/deck")
async def notes_deck_generate(req: Request):
    """条目18：从条目17摘要生成 Anki 卡组（后台任务，进度看 /api/status 的
    deck 键或 WS type=deck；产物与 transcript 同目录同名 .apkg，GET 下载）。
    生成→二次过滤两段 LLM（复用条目6重试），源料 = 摘要文件（先 summarize）。
    前置校验先于置位（无 await）；同一时刻只允许一个卡组任务（409）。"""
    if runtime["deck_running"]:
        return JSONResponse({"ok": False, "error": "卡组生成已在进行中"},
                            status_code=409)
    # p2-2：可指定历史笔记（body {name}）；缺省 = 当前笔记（老行为不变）
    body = await optional_json(req)
    tpath, err = note_target(body.get("name"))
    if tpath is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    tr = runtime.get("translator")
    if tr is None:
        return JSONResponse(
            {"ok": False, "error": "翻译未启用（无 DEEPSEEK_API_KEY），卡组生成不可用"},
            status_code=409)
    summary_path = tpath.with_name(tpath.stem + ".summary.md")
    if not summary_path.is_file():
        return JSONResponse(
            {"ok": False, "error": "尚无摘要文件，请先 POST /api/notes/summarize"},
            status_code=409)
    apkg_path = tpath.with_name(tpath.stem + ".apkg")
    title = tpath.stem
    runtime["deck_running"] = True

    def _snapshot() -> dict:
        return {"type": "deck", **status["deck"]}

    def _on_stage(stage: str, n_cards: int | None = None) -> None:
        status["deck"]["state"] = "running"
        status["deck"]["stage"] = stage
        if n_cards is not None:
            status["deck"]["generated"] = n_cards
        asyncio.create_task(hub.broadcast(_snapshot()))

    async def _run() -> None:
        try:
            status["deck"] = {"state": "running", "stage": "generating",
                              "generated": 0, "kept": 0, "dropped": 0,
                              "path": str(apkg_path), "error": None}
            result = await build_deck(tr, summary_path, apkg_path, title,
                                      on_stage=_on_stage)
            status["deck"].update(state="done", stage=None,
                                  generated=result["generated"],
                                  kept=len(result["cards"]),
                                  dropped=result["dropped"], error=None)
            await hub.broadcast(_snapshot())
            log.info(f"[deck] ✅ 完成: {apkg_path.name}"
                     f"（生成 {result['generated']}，过滤丢弃 "
                     f"{result['dropped']}，保留 {len(result['cards'])}）")
        except Exception as e:
            status["deck"].update(state="error", stage=None,
                                  error=f"{type(e).__name__}: {e}")
            log.warning(f"[deck] ⚠ 卡组生成失败: {e}")
            with contextlib.suppress(Exception):
                await hub.broadcast(_snapshot())
        finally:
            runtime["deck_running"] = False
            runtime["deck_task"] = None

    runtime["deck_task"] = asyncio.create_task(_run())
    return {"ok": True, "started": True, "transcript": tpath.name,
            "output": apkg_path.name}


# —— p2-1：课程管理（courses.json）——
# 校验纪律对齐 /api/config：未知键/非法值一律 400 且**状态零触碰**（先校验后落盘）；
# 写盘走 courses.save 的原子替换。/api/ 前缀已自动纳入条目19 token 鉴权。


#: 课程表读写失败时的统一提示（读/写两侧共用同一句话）
COURSES_CORRUPT_HINT = "课程表文件损坏——为避免覆盖，本次操作已被拒绝。请修复或删除 courses.json 后重试。"


def courses_corrupt(data_error: str | None) -> JSONResponse | None:
    """写端点统一守卫：课程表坏了就**拒绝写回**，返回 409 + 可见错误。

    为什么必须先拦（评审1）：写端点的模式是 load → 改 → save，而 load 在坏文件上
    返回空表；不拦就会把这份空表落盘，静默抹掉整张课程表（含「POST active=null
    取消选课」这种看似无害的请求）。"""
    if not data_error:
        return None
    return JSONResponse({"ok": False, "error": COURSES_CORRUPT_HINT,
                         "detail": data_error}, status_code=409)


def courses_view(data: dict | None = None, data_error: str | None = None) -> dict:
    """课程表视图：课程列表（含每课笔记数）+ 当前课程 + 未分类名 + 调色板。

    笔记数由扫描 transcripts/*.md 头部得出（courses.count_notes）——**单一事实
    来源 = 笔记文件本身**，不额外维护 sidecar 索引，故不存在头/索引双写不一致。"""
    if data is None:
        data, data_error = courses_mod.load_checked(COURSES_PATH)
    counts = courses_mod.count_notes(TRANSCRIPTS)
    items = [dict(c, notes=counts.get(c["name"], 0)) for c in data["courses"]]
    active = data.get("active")
    cur = courses_mod.find(data, active) if active else None
    out = {"ok": True, "courses": items, "active": active,
           "active_name": cur["name"] if cur else courses_mod.UNCATEGORIZED,
           "uncategorized": courses_mod.UNCATEGORIZED,
           "palette": list(COURSE_PALETTE)}
    if data_error:
        # 读侧也要显式告知：否则 UI 只是显示「0 门课」，用户以为课程被删了
        out["error"] = COURSES_CORRUPT_HINT
        out["detail"] = data_error
    return out


async def broadcast_courses() -> None:
    """课程表变化后广播，让多窗口（字幕页/工作台）同步。"""
    with contextlib.suppress(Exception):
        await hub.broadcast({"type": "courses", **courses_view()})


async def course_body(req: Request) -> tuple[dict | None, JSONResponse | None]:
    """解析课程请求体；非 JSON/非对象 → (None, 400 响应)。"""
    try:
        body = await req.json()
    except Exception:
        return None, JSONResponse({"ok": False, "error": "请求体必须是 JSON"},
                                  status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"},
                                  status_code=400)
    return body, None


def apply_course_glossary(course: dict | None) -> bool:
    """把当前课程的术语表接到翻译侧（并同步 whisper 侧 prompt，口径同
    sync_glossary_reload——只刷翻译侧会让两侧术语不一致）。

    生效清单 = CLI `--glossary`（runtime['cli_glossary']，基线）+ 课程的术语表
    （存储为相对根路径，此处转绝对；课程无表 = 回落到 CLI 基线）。无 translator
    （未配 key）时 no-op。返回是否发生替换。"""
    tr = runtime.get("translator")
    if tr is None:
        return False
    base = list(runtime.get("cli_glossary") or [])
    g = courses_mod.resolve_glossary(ROOT, (course or {}).get("glossary"))
    if not tr.set_glossary_files(base + ([g] if g else [])):
        return False
    runtime["prompt"] = glossary_prompt(tr.entries) if tr.entries else None
    return True


@app.get("/api/courses")
async def list_courses():
    """课程列表（工作台侧栏 + 设置页课程卡消费）。"""
    return courses_view()


@app.post("/api/courses")
async def create_course(req: Request):
    """新建课程。name 必填；color 缺省取调色板首色；glossary 可空。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    fields, err = courses_mod.validation_error(body, ROOT)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if "name" not in fields:
        return JSONResponse({"ok": False, "error": "缺少课程名 name"}, status_code=400)
    data, derr = courses_mod.load_checked(COURSES_PATH)
    bad = courses_corrupt(derr)
    if bad:
        return bad
    if len(data["courses"]) >= courses_mod.MAX_COURSES:
        return JSONResponse(
            {"ok": False, "error": f"课程数已达上限 {courses_mod.MAX_COURSES}"},
            status_code=400)
    if any(c["name"] == fields["name"] for c in data["courses"]):
        return JSONResponse({"ok": False, "error": f"课程名已存在: {fields['name']}"},
                            status_code=400)
    course = {"id": courses_mod.new_id(fields["name"],
                                       {c["id"] for c in data["courses"]}),
              "name": fields["name"],
              "color": fields.get("color") or courses_mod.default_color(),
              "glossary": fields.get("glossary")}
    data["courses"].append(course)
    courses_mod.save(COURSES_PATH, data)
    log.info(f"[courses] 新建课程 {course['id']}（{course['name']}）")
    await broadcast_courses()
    return {"ok": True, "course": course}


@app.put("/api/courses/{cid}")
async def update_course(cid: str, req: Request):
    """改课程（name/color/glossary 任一子集）。改名会同步已写笔记头里的课程名，
    否则按名统计会把历史笔记漏掉（see courses.rename_in_notes）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    fields, err = courses_mod.validation_error(body, ROOT)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    data, derr = courses_mod.load_checked(COURSES_PATH)
    bad = courses_corrupt(derr)
    if bad:
        return bad
    course = courses_mod.find(data, cid)
    if course is None:
        return JSONResponse({"ok": False, "error": f"课程不存在: {cid}"},
                            status_code=404)
    if "name" in fields and any(c["name"] == fields["name"] and c["id"] != cid
                                for c in data["courses"]):
        return JSONResponse({"ok": False, "error": f"课程名已存在: {fields['name']}"},
                            status_code=400)
    old_name = course["name"]
    course.update(fields)
    courses_mod.save(COURSES_PATH, data)
    renamed = 0
    if "name" in fields and fields["name"] != old_name:
        renamed = courses_mod.rename_in_notes(TRANSCRIPTS, old_name, fields["name"])
    if data.get("active") == cid and apply_course_glossary(course):
        log.info(f"[courses] 当前课程术语表热重载（{course['name']}）")
    status["course"] = (course["name"] if data.get("active") == cid
                        else status.get("course"))
    log.info(f"[courses] 更新课程 {cid}（{list(fields)}；改名同步笔记 {renamed} 篇）")
    await broadcast_courses()
    return {"ok": True, "course": course, "notes_retagged": renamed}


@app.delete("/api/courses/{cid}")
async def delete_course(cid: str):
    """删课程。若删的正是当前课程 → active 归 null（未分类）并把笔记重标为未分类；
    历史笔记文件一律保留（只失去分组归属，绝不删数据）。"""
    data, derr = courses_mod.load_checked(COURSES_PATH)
    bad = courses_corrupt(derr)
    if bad:
        return bad
    course = courses_mod.find(data, cid)
    if course is None:
        return JSONResponse({"ok": False, "error": f"课程不存在: {cid}"},
                            status_code=404)
    data["courses"] = [c for c in data["courses"] if c["id"] != cid]
    was_active = data.get("active") == cid
    if was_active:
        data["active"] = None
    courses_mod.save(COURSES_PATH, data)
    if was_active:
        tag = hub.retag_course(None)
        status["course"] = courses_mod.UNCATEGORIZED
        apply_course_glossary(None)
        log.info(f"[courses] 删除当前课程 {cid} → 归入未分类（笔记归属：{tag}）")
    else:
        log.info(f"[courses] 删除课程 {cid}（{course['name']}）")
    await broadcast_courses()
    return {"ok": True, "deleted": cid, "was_active": was_active}


@app.post("/api/courses/active")
async def set_active_course(req: Request):
    """上课时选课程：active = 课程 id 或 null（null = 「未分类」默认态）。
    选中即把该课术语表接到翻译侧，并把笔记归属改到该课（见 Hub.retag_course）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k != "active"]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['active']）"},
            status_code=400)
    cid = body.get("active")
    if cid is not None and (not isinstance(cid, str) or not cid.strip()):
        return JSONResponse(
            {"ok": False, "error": "active 须为课程 id 字符串或 null（未分类）"},
            status_code=400)
    data, derr = courses_mod.load_checked(COURSES_PATH)
    bad = courses_corrupt(derr)
    if bad:
        return bad
    course = None
    if cid is not None:
        course = courses_mod.find(data, cid.strip())
        if course is None:
            return JSONResponse({"ok": False, "error": f"课程不存在: {cid}"},
                                status_code=404)
        cid = course["id"]
    data["active"] = cid
    courses_mod.save(COURSES_PATH, data)
    name = course["name"] if course else courses_mod.UNCATEGORIZED
    tag = hub.retag_course(course["name"] if course else None)
    status["course"] = name
    gloss = bool(course and apply_course_glossary(course))
    log.info(f"[courses] 当前课程 → {name}（笔记归属：{tag}；术语表重载：{gloss}）")
    await broadcast_courses()
    return {"ok": True, "active": cid, "active_name": name,
            "note": tag, "glossary_reloaded": gloss}


# —— p2-4：模型管理器（缓存状态 / 下载 / 删除）——
# 安全口径见 models.py 模块注释：对外只认白名单短名，客户端无法指定 repo/URL。
# 下载进度复用 status['load']（字幕页/设置页同一条进度条 UI）；终态与错误另记
# status['model_dl']，避免任务结束后进度条一清而错误无处可看。


def bundled_model_present(name: str) -> bool:
    """frozen 构建是否把该模型随包带上了（开发模式恒 False）。"""
    try:
        return bundled_model_path(name) is not None
    except Exception:
        return False


def models_snapshot() -> dict:
    """模型管理器视图：缓存状态 + 当前模型 + 下载中 + 离线开关。"""
    cache = models_mod.cache_dir()
    active = runtime["config"].get("model")
    data = models_mod.list_models(cache, active)
    for m in data["models"]:
        m["bundled"] = bundled_model_present(m["name"])
        m["size_h"] = models_mod.human_size(m["size"])
    data["total_size_h"] = models_mod.human_size(data["total_size"])
    data["active"] = active
    data["downloading"] = runtime.get("downloading")
    data["offline"] = os.environ.get("HF_HUB_OFFLINE") == "1"
    data["dl"] = status["model_dl"]
    return data


async def broadcast_models() -> None:
    """模型表变化后广播。dir_size 要全量遍历缓存目录（评审2 实测可堵事件循环），
    故快照也放线程里算——广播本身是锦上添花，但绝不能顺手把字幕主链路堵住。"""
    with contextlib.suppress(Exception):
        await hub.broadcast(
            {"type": "models", **(await asyncio.to_thread(models_snapshot))})


@app.get("/api/models")
async def api_models():
    """计划内模型的缓存状态 / 磁盘占用（纯读本地缓存，不联网）。

    dir_size 会对每个模型目录全量 rglob（用户缓存 5GB 时文件数以千计），属同步 IO；
    放到线程里跑，否则一次「打开设置页」就可能让 /api/status 与 WS 广播卡一拍。"""
    data = await asyncio.to_thread(models_snapshot)
    data["ok"] = True
    return data


@app.post("/api/models/download")
async def api_models_download(req: Request):
    """下载某模型到 HF 缓存。受理即返回，进度看 /api/status 的 load 字段。

    三道前置校验（校验与置位之间无 await，与 /api/config 守卫同款纪律）：
    白名单短名 → 400；已有下载在跑 → 409；模型切换进行中 → 409（两者共用
    status['load'] 这条进度条，并发会互相覆盖）。"""
    body = await optional_json(req)
    name = body.get("name")
    if not isinstance(name, str) or name not in models_mod.MODEL_REPOS:
        return JSONResponse(
            {"ok": False,
             "error": f"未知模型: {name!r}（可选: {list(models_mod.PLANNED_MODELS)}）"},
            status_code=400)
    if runtime.get("downloading"):
        return JSONResponse(
            {"ok": False,
             "error": f"已有模型正在下载：{runtime['downloading']}，请等待完成"},
            status_code=409)
    if runtime["reloading"]:
        return JSONResponse({"ok": False, "error": "模型切换进行中，稍后再试"},
                            status_code=409)
    cache = models_mod.cache_dir()
    info = models_mod.model_info(cache, name, runtime["config"].get("model"))
    info["bundled"] = bundled_model_present(name)
    info["size_h"] = models_mod.human_size(info["size"])
    if info["cached"]:
        return {"ok": True, "already": True, "model": info}

    offline = os.environ.get("HF_HUB_OFFLINE") == "1"
    runtime["downloading"] = name
    status["model_dl"] = {"state": "running", "name": name, "error": None,
                          "freed": 0}

    dl_owner = f"dl:{name}"   # 进度条所有权令牌（评审2：见 _load_owner 的说明）

    def _progress(pct: int, stage: str) -> None:
        set_load_progress(pct, stage, dl_owner)

    async def _run() -> None:
        try:
            await asyncio.to_thread(models_mod.download_model, name, cache,
                                    _progress, local_files_only=offline)
            got = models_mod.model_info(cache, name, runtime["config"].get("model"))
            status["model_dl"].update(state="done", error=None)
            log.info(f"[models] ✅ {name} 下载完成"
                     f"（{models_mod.human_size(got['size'])}）")
        except Exception as e:
            status["model_dl"].update(state="error",
                                      error=f"{type(e).__name__}: {e}")
            log.warning(f"[models] ⚠ {name} 下载失败: {e}")
        finally:
            # 只清**自己写的**那条进度（令牌归属）。旧实现按 `'模型' in stage` 判归属，
            # 而 build_asr 的文案「加载模型 X（首次运行需下载）」同样含「模型」二字——
            # 实测下载任务收尾时把切换中的进度清成 None，此后一整轮切换都没有进度条。
            clear_load_progress(dl_owner)
            runtime["downloading"] = None
            await broadcast_models()

    runtime["download_task"] = asyncio.create_task(_run())
    log.info(f"[models] 开始下载 {name}（{models_mod.MODEL_REPOS[name]}）"
             + ("（离线模式：仅查本地缓存）" if offline else ""))
    return {"ok": True, "started": True, "name": name,
            "repo": models_mod.MODEL_REPOS[name]}


@app.delete("/api/models/{name}")
async def api_models_delete(name: str):
    """删除某模型的**缓存目录**（不动随包内置副本，也不动别的仓库）。

    使用中禁删，三道都要看：
    ① config 当前模型（正常态）；
    ② runtime['asr'] 实际加载的模型（切换失败时两者会不一致）；
    ③ **runtime['reloading']**（评审2 修）——模型切换是「先构建、成功才合并 config」，
       构建期间 ①② 都还指向**旧**模型，于是「切换进行中删掉目标模型」会 200 并真删
       （实测 reloading=True 切到 tiny 时 DELETE /api/models/tiny → 200 freed=4096，
       而同一状态下的 POST download 却正确 409）；前端删除按钮只按 m.active 禁用，
       正好在切换完成前显示目标可删，兜不住。故切换中一律禁删。"""
    if name not in models_mod.MODEL_REPOS:
        return JSONResponse(
            {"ok": False,
             "error": f"未知模型: {name!r}（可选: {list(models_mod.PLANNED_MODELS)}）"},
            status_code=400)
    if runtime.get("reloading"):
        return JSONResponse(
            {"ok": False,
             "error": "模型切换进行中，暂不能删除（避免删掉正在加载的目标）"},
            status_code=409)
    if name == runtime["config"].get("model"):
        return JSONResponse(
            {"ok": False, "error": f"模型使用中，不能删除：{name}（请先切到别的模型）"},
            status_code=409)
    loaded = getattr(runtime.get("asr"), "model_size", None)
    if loaded == name:
        return JSONResponse(
            {"ok": False, "error": f"识别器正在加载 {name}，不能删除"},
            status_code=409)
    if runtime.get("downloading") == name:
        return JSONResponse({"ok": False, "error": f"{name} 正在下载中，不能删除"},
                            status_code=409)
    # 评审2 修：rmtree 可能上万文件/上 GB，同步跑会堵住事件循环（/api/status 与 WS
    # 广播一起停）；与其它重活一致走线程。
    freed, err = await asyncio.to_thread(
        models_mod.remove_model, models_mod.cache_dir(), name)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=409)
    log.info(f"[models] 删除 {name} 缓存，释放 {models_mod.human_size(freed)}")
    await broadcast_models()
    return {"ok": True, "deleted": name, "freed": freed,
            "freed_h": models_mod.human_size(freed)}


# —— p2-6（B8）：术语库管理（glossary/*.md）——
# 安全口径见 glossary.py：对外只收**裸文件名**，服务端解析到 glossary/ 内并校验
# 包含性——这是**写操作**，越界就能改任意文件，故只允许 glossary/ 直属的 .md。
# 写盘成功后**显式**触发重载：本机文件系统会把快速重写的时间戳折叠成同一个
# （实测 300 次里 266 次 st_mtime_ns 相同），只靠 reload_if_changed 会随机不生效。


def reload_glossary_now(path: Path) -> bool:
    """写盘后显式重载术语表（仅当该文件处于**生效清单**里）。返回是否重载。

    不在清单里 = 用户编辑的是别的课程/别的用途的表，不该动当前 prompt。
    两侧同步沿用既有口径：翻译侧 entries/system 与 whisper 侧 runtime['prompt']
    一起刷（runtime['prompt'] 同时喂 streaming base_prompt 与整句 initial_prompt）。"""
    tr = runtime.get("translator")
    if tr is None:
        return False
    files = [Path(f).resolve() for f in (getattr(tr, "glossary_files", None) or [])]
    if Path(path).resolve() not in files:
        return False
    tr.reload_now()
    runtime["prompt"] = glossary_prompt(tr.entries) if tr.entries else None
    log.info(f"[glossary] {Path(path).name} 已重载并两侧同步（terms={len(tr.entries)}）")
    return True


def glossary_files_view() -> list[dict]:
    """glossary/ 下每个文件：词条 + 被哪些课程引用 + 是否正在生效。"""
    tr = runtime.get("translator")
    active = {str(Path(f).resolve())
              for f in (getattr(tr, "glossary_files", None) or [])}
    used: dict = {}
    try:
        for c in courses_mod.load(COURSES_PATH)["courses"]:
            rel = c.get("glossary")
            if not rel:
                continue
            abs_ = courses_mod.resolve_glossary(ROOT, rel)
            if abs_:
                used.setdefault(str(Path(abs_).resolve()), []).append(c["name"])
    except Exception as e:
        log.debug(f"[glossary] 课程引用解析失败（忽略）: {e}")
    out = []
    for p in glossary_mod.list_files(ROOT):
        data = glossary_mod.read_file(p)
        key = str(p.resolve())
        out.append({
            "name": p.name,
            "rel": f"{glossary_mod.GLOSSARY_DIR}/{p.name}",
            "entries": data["entries"],
            "count": len(data["entries"]),
            "used_by": used.get(key, []),
            "active": key in active,
        })
    return out


def glossary_terms_payload() -> dict:
    """p3-2：**当前生效**词条快照（前端术语高亮的数据源）+ 修订号 rev。

    词条来源 = `translator.entries`，即翻译 prompt 与 whisper initial_prompt
    实际使用的那一份（apply_course_glossary / reload_glossary_now /
    sync_glossary_reload 三条路径都把结果落在这一个属性上）。**刻意不直接读
    glossary/*.md**：高亮的意义是「屏幕上这个词正是引擎在保的那个词」，若按文件
    内容高亮，未被课程绑定的表也会被点亮——那是在暗示一件没发生的事。
    无 translator（未配 DeepSeek key）时术语表本就不参与任何一侧，故返回空表，
    并用 translate_ready 让 UI 能解释「为什么没高亮」。

    rev 是 terms+files 的短哈希：内容相同则稳定（前端可安全缓存），任一变化则
    必然不同。前端每次渲染的 O(词条数) 预算只落在 rev 变化那一下，不在每帧。"""
    tr = runtime.get("translator")
    terms: list = []
    seen: set = set()
    for en, zh in (getattr(tr, "entries", None) or []):
        en, zh = str(en).strip(), str(zh).strip()
        if not en or not zh or en.lower() in seen:
            continue
        seen.add(en.lower())
        terms.append({"en": en, "zh": zh})
    files = [Path(f).name for f in (getattr(tr, "glossary_files", None) or [])]
    blob = json.dumps({"terms": terms, "files": files},
                      ensure_ascii=False, sort_keys=True)
    return {"ok": True, "rev": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12],
            "terms": terms, "count": len(terms), "files": files,
            "translate_ready": tr is not None}


@app.get("/api/glossary/terms")
async def api_glossary_terms():
    """p3-2：术语高亮用的词条表（见 glossary_terms_payload 的取舍说明）。

    路由必须**注册在 /api/glossary 之后**也安全（FastAPI 按路径匹配，二者不同
    路径，无遮蔽问题）；此注释只是提醒后来者：若将来加 /api/glossary/{name}
    这类参数路由，务必放在本函数之后再声明。"""
    return glossary_terms_payload()


@app.get("/api/glossary")
async def api_glossary():
    """术语库：文件列表（含词条）+ 生效状态 + 课程引用 + 限额。"""
    files = glossary_files_view()
    return {
        "ok": True,
        "dir": f"{glossary_mod.GLOSSARY_DIR}/",
        "files": files,
        "active_count": sum(1 for f in files if f["active"]),
        "translate_ready": runtime.get("translator") is not None,
        "limits": {"max_entries": glossary_mod.MAX_ENTRIES,
                   "max_field": glossary_mod.MAX_FIELD},
    }


@app.post("/api/glossary")
async def api_glossary_write(req: Request):
    """新增 / 更新 / 删除词条，或新建术语表文件；写回后即时热重载。

    请求体：{"file": "CS.md", "op": "add"|"update"|"delete"|"create",
             "en": ..., "zh": ..., "old_en": ...}（按 op 取用）。
    `file` 是**裸文件名**，服务端只认 glossary/ 直属的 .md。"""
    body = await optional_json(req)
    op = body.get("op")
    if op not in ("add", "update", "delete", "create"):
        return JSONResponse(
            {"ok": False,
             "error": f"未知操作 op={op!r}（可选: add/update/delete/create）"},
            status_code=400)
    path = glossary_mod.safe_glossary_path(ROOT, body.get("file"))
    if path is None:
        return JSONResponse(
            {"ok": False,
             "error": f"非法术语表文件名: {body.get('file')!r}"
                      f"（只接受 {glossary_mod.GLOSSARY_DIR}/ 下的 .md 文件名）"},
            status_code=400)

    if op == "create":
        if path.is_file():
            return JSONResponse({"ok": False, "error": f"文件已存在: {path.name}"},
                                status_code=409)
        try:
            glossary_mod.write_file(
                path, glossary_mod.parse_lines(glossary_mod.NEW_FILE_HEADER))
        except Exception as e:
            log.warning(f"[glossary] ⚠ 新建失败 {path.name}: {e}")
            return JSONResponse({"ok": False, "error": f"新建失败: {e}"},
                                status_code=500)
        log.info(f"[glossary] 新建术语表 {path.name}")
        return {"ok": True, "op": op, "file": path.name, "count": 0,
                "entries": [], "reloaded": False}

    if not path.is_file():
        return JSONResponse(
            {"ok": False, "error": f"术语表不存在: {path.name}（可先新建）"},
            status_code=404)
    data = glossary_mod.read_file(path)
    lines = data["lines"]
    if op == "add":
        lines, err = glossary_mod.add_entry(lines, body.get("en"), body.get("zh"))
    elif op == "update":
        lines, err = glossary_mod.update_entry(lines, body.get("old_en"),
                                               body.get("en"), body.get("zh"))
    else:
        lines, err = glossary_mod.delete_entry(lines, body.get("en"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        glossary_mod.write_file(path, lines, data["eol"])
    except Exception as e:
        log.warning(f"[glossary] ⚠ 写入失败 {path.name}: {e}")
        return JSONResponse({"ok": False, "error": f"写入失败: {e}"},
                            status_code=500)
    reloaded = reload_glossary_now(path)
    after = glossary_mod.read_file(path)
    log.info(f"[glossary] {path.name} {op} 完成（{len(after['entries'])} 条，"
             f"重载={'是' if reloaded else '否（不在生效清单）'}）")
    return {"ok": True, "op": op, "file": path.name,
            "entries": after["entries"], "count": len(after["entries"]),
            "reloaded": reloaded}


def final_payload(ts_clock: str, en: str, seg: int, audio_ts: float | None) -> dict:
    """final 广播 payload（条目12①：audio_ts = 本段音频的**窗口起点**墙钟秒，
    贯穿采集→广播全程，前端据此显示「延迟 ≈ N s」）。键集合兼容 v0.2 整句
    payload（ts/en/zh/seg，保护清单第 2 条「EN 即时广播、ZH 到货补发」不变），
    audio_ts 为纯增量字段。

    评审1 修：这里收到的已经是**窗口起点**（= capture 发射时刻 − 窗口时长），
    不再是 capture 投递的发射时刻。旧口径下「延迟」少报了整段窗口时长 + 解码 +
    翻译，「向导③是否刚说话」也会把 d 秒前的旧话判成刚说。"""
    return {"ts": ts_clock, "en": en, "zh": "", "seg": seg, "audio_ts": audio_ts}


def apply_ui_metrics(data) -> bool:
    """条目9④：前端 WS 上行 ui_metrics 并入 /api/status（纯函数，供单测）。
    zh_dropped = 前端「因历史裁剪未显示」的翻译条数；只接受非负 int（bool 除外），
    多客户端同时在线时最后写入生效（只读展示值，不做聚合）。"""
    if not isinstance(data, dict) or data.get("type") != "ui_metrics":
        return False
    v = data.get("zh_dropped")
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return False
    status["ui_zh_dropped"] = v
    return True


def sync_glossary_reload() -> bool:
    """条目16：术语表 mtime 变化 → 翻译侧 entries/system 与 whisper 侧
    runtime['prompt'] 必须同步刷新（runtime['prompt'] 同时喂 streaming 的
    base_prompt 与整句 initial_prompt，只刷翻译侧会出现两侧术语不一致——
    技术评审 missing 项）。返回是否发生重载。纯 stat 比对，每段调用一次
    的开销可忽略；无 translator / 无术语表清单时为 no-op。"""
    tr = runtime.get("translator")
    if tr is None or not getattr(tr, "glossary_files", None):
        return False
    if tr.reload_if_changed():
        runtime["prompt"] = glossary_prompt(tr.entries)
        return True
    return False


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        while True:
            msg = await ws.receive_text()  # 保活 + ui_metrics 上行（条目9④）
            try:
                apply_ui_metrics(json.loads(msg))
            except (ValueError, TypeError):
                continue  # 非 JSON 的保活内容（如客户端 ping）忽略
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


MIC_REBUILD_COOLDOWN = 30  # 采集器重建的最小间隔（秒），防设备持续占用时反复横跳
BACKLOG_DROP_THRESHOLD = 5  # 音频段积压超过该数触发追赶丢弃（可按机器快慢调）
BACKLOG_KEEP = 2            # 追赶丢弃后保留的最新段数
TRANSLATE_CONTEXT_WINDOW = 3  # 条目16：注入翻译的最近 N 条已定稿 EN；回退口径 = 置 0


async def status_loop() -> None:
    """每秒刷新 VAD 电平与麦克风健康（watchdog：采集断流/线程死亡时上报并自动
    重建一次采集器，最多每 MIC_REBUILD_COOLDOWN 秒重试）；GPU 模式下每 5 秒
    顺带查显存（nvidia-smi 失败静默跳过）。"""
    last_rebuild = 0.0
    tick = 0
    while True:
        # 条目13：语言状态同步（配置值 + auto 模式下最近采纳的检测语言，UI 下拉消费）
        status["lang"] = runtime["config"].get("lang", "en")
        asr_now = runtime["asr"]
        status["lang_auto"] = getattr(asr_now, "detected_lang", None) if asr_now else None
        seg = runtime["segmenter"]
        if seg:
            try:
                status["vad"] = seg.snapshot()
            except Exception:
                log.debug("VAD snapshot 失败", exc_info=True)  # 条目3：不再静默 pass
            if runtime["seg_q"] is not None:
                status["backlog"] = runtime["seg_q"].qsize()  # 积压段数（字幕页徽章）
            # 麦克风 watchdog：不健康（线程死/断流 >3s/有 error）→ 记录并重建
            try:
                if seg.healthy:
                    status["mic"] = "ok"
                else:
                    err = seg.error or f"no audio for >{seg.FEED_STALE_S:.0f}s"
                    status["mic"] = f"error: {err}"
                    if time.time() - last_rebuild >= MIC_REBUILD_COOLDOWN:
                        last_rebuild = time.time()
                        log.warning(f"[mic] ⚠ 采集不健康({err})，重建采集器")
                        await asyncio.to_thread(apply_segmenter)
            except Exception as e:
                status["mic"] = f"error: rebuild failed ({e})"
                log.warning(f"[mic] ⚠ watchdog 异常: {e}")
        tick += 1
        if tick % 5 == 0 and status.get("device") == "cuda":
            try:
                out = await asyncio.to_thread(
                    subprocess.run,
                    ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=4,
                )
                used, total = out.stdout.strip().splitlines()[0].split(",")
                status["vram"] = f"{int(used) / 1024:.1f}/{int(total) / 1024:.1f} GB"
            except Exception:
                log.debug("nvidia-smi 查询失败", exc_info=True)  # 条目3：不再静默 pass
        await asyncio.sleep(1)


async def asr_loop(args) -> None:
    """主循环：VAD 分句（后台线程）→ whisper（线程池）→ 广播英文+落盘；
    翻译拆成独立后台任务（二次广播中文+笔记补写），识别不再等 DeepSeek 往返。
    在飞翻译任务最多 MAX_FLIGHTS=2，超出先等最旧完成，防雪崩。
    单段失败只跳过该段；暂停时排空队列丢弃音频；模型热切换由 /api/config 触发。"""
    cfg = dict(load_config())
    if args.model:
        cfg["model"] = args.model
    if args.gpu:
        cfg["device"] = "cuda"
    if args.compute_type:
        cfg["compute_type"] = args.compute_type
    if args.vad:
        cfg["vad"] = args.vad
    if args.source:
        cfg["source"] = args.source  # 条目14：CLI --source 最高优先级（--vad 同款惯例）
    if args.streaming:
        cfg["streaming"] = True  # CLI 优先级最高（--model/--gpu/--vad 同款惯例）
    if args.lang:
        cfg["lang"] = args.lang  # 条目13③：CLI --lang 最高优先级（default=None，显式传才覆盖）
    if args.device is not None:
        # 条目15③：CLI --device 最高优先级——接入 config 化的 input_device（否则
        # config 化永不生效，技术评审点名的遗漏）。argparse 未指定 type，数字串
        # 归一为索引（sounddevice 的 str 语义是设备名查找，"3" 会被当设备名）；
        # 非数字串按设备名透传（sounddevice 支持按名查找）。
        dev: int | str = (int(args.device) if str(args.device).isdigit()
                          else args.device)
        cfg["input_device"] = dev
    runtime["config"] = cfg
    # p2-7：呈现类开关从持久化配置回填到 status——字幕页靠 2s 轮询 status 拿它，
    # 不回填的话重启后会被模块级默认值顶掉（用户在设置页选的值“重启就丢”）。
    status["fade_lines"] = cfg.get("fade_lines", DEFAULT_CONFIG["fade_lines"])
    # p3-1：全局快捷键按持久化配置注册（重启后键位跟着 config.json 走；注册失败
    # 只降级、不影响启动——见 apply_hotkeys 的兜底）。
    apply_hotkeys(cfg)
    log.info(f"[main] config: model={cfg['model']}, device={cfg['device']}, vad={cfg['vad']}, "
             f"lang={cfg.get('lang')}, source={cfg.get('source', 'mic')}")

    runtime["seg_q"] = queue.Queue()
    asyncio.create_task(status_loop())

    # 条目11：流式 partial 管线（streaming.enabled 灰度，默认 false 零变化）。
    # 开启时：MicSegmenter 扇出原始块到 stream_q → feeder 攒 min_chunk 派 partial
    # 任务；final 段与 partial 共享同一个 PriorityQueue（final prio=0 定稿优先）。
    # 关闭时：stream_q/scheduler 复位为 None，主循环走原整句路径，行为与 v0.2 一致。
    runtime["stream_q"] = None
    runtime["scheduler"] = None
    scheduler: DecodeScheduler | None = None
    final_ready_q: asyncio.Queue | None = None
    if bool(cfg.get("streaming", False)):
        runtime["stream_q"] = queue.Queue()
        status["streaming"] = True
        loop = asyncio.get_running_loop()
        final_ready_q = asyncio.Queue()

        async def _send_partial(payload: dict) -> None:
            if status["paused"]:
                return  # 暂停不广播预览（在飞任务收尾时已越线的一律丢弃）
            await hub.broadcast(payload)

        scheduler = DecodeScheduler(
            asr_provider=lambda: runtime["asr"],   # 入队时快照 → 热切换旧任务排空
            paused=lambda: status["paused"],
            base_prompt=lambda: runtime["prompt"] or "",  # 术语表持续注入（条目11 防专名改口）
            on_final=lambda en, meta: loop.call_soon_threadsafe(
                final_ready_q.put_nowait, (en, meta)),
            emit_partial=lambda p: asyncio.run_coroutine_threadsafe(_send_partial(p), loop),
        )
        scheduler.start()
        scheduler.attach_stream_q(runtime["stream_q"])
        runtime["scheduler"] = scheduler
        log.info("[main] 流式 partial 字幕已开启（LocalAgreement-2，final 定稿优先）")
    else:
        status["streaming"] = False

    # 初始化段（麦克风/模型）整体保护：任何异常不再让字幕任务静默死亡，
    # 而是写 status['asr'] 供字幕页警示；主循环继续跑，等 /api/config 热切换修复
    status["asr"] = f"loading model ({cfg['model']})"
    try:
        apply_segmenter()
        log.info("[main] 🎙 麦克风已启动，说话即出字幕")
        # 评审1场景2：启动初始构建（首次下载可达分钟级）必须持守卫——否则期间任意
        # 模型键 POST 绕过守卫与启动构建并发，两个 build_asr 后完成者覆盖 runtime['asr']
        runtime["reloading"] = True
        try:
            await asyncio.to_thread(build_asr)
        finally:
            runtime["reloading"] = False
    except Exception as e:
        status["asr"] = f"error: init failed ({e})"
        log.warning(f"[main] ⚠ 初始化失败: {e} —— 服务保持，/api/config 可重试")
    # p2-1：开课笔记头带当前课程归属（courses.json 的 active；未选 = 「未分类」）。
    # 用户多在**开课前**才选课程，届时笔记仍是空头，/api/courses/active 会改写头。
    _ccur = None
    try:
        _cdata = courses_mod.load(COURSES_PATH)
        _ccur = courses_mod.find(_cdata, _cdata.get("active")) if _cdata.get("active") else None
        status["course"] = _ccur["name"] if _ccur else courses_mod.UNCATEGORIZED
        hub.start_transcript(_ccur["name"] if _ccur else None)
        log.info(f"[main] 📝 笔记文件: {hub.transcript_path}"
                 f"（课程：{status['course']}）")
    except Exception as e:
        log.warning(f"[main] ⚠ 笔记文件创建失败: {e} —— 字幕照常，笔记/导出不可用")

    translator = None
    runtime["prompt"] = None
    runtime["cli_glossary"] = list(args.glossary)  # p2-1：课程术语表叠加的基线
    try:
        key = args.api_key or load_api_key(ROOT / ".env")
        if key:
            translator = DeepSeekTranslator(
                key, model=cfg.get("translate_model") or "deepseek-chat",  # 条目16②配置化
                glossary_files=args.glossary)
            if translator.entries:
                runtime["prompt"] = glossary_prompt(translator.entries)
            status["translate"] = "ready"
        else:
            status["translate"] = "disabled (no DEEPSEEK_API_KEY)"
            log.warning("[main] ⚠ 未找到 DEEPSEEK_API_KEY —— 只出英文字幕，不翻译")
    except Exception as e:
        status["translate"] = f"error: {e}"
        log.warning(f"[main] ⚠ 翻译初始化失败: {e} —— 只出英文字幕")
    runtime["translator"] = translator  # 条目16：/api/config 热切 + 术语表双侧同步用

    # p2-1：启动即接上当前课程的术语表（清单 = CLI --glossary 基线 + 课程表文件）
    if translator is not None:
        try:
            if apply_course_glossary(_ccur):
                log.info(f"[courses] 课程术语表已接入（{status['course']}）")
        except Exception as e:
            log.warning(f"[courses] ⚠ 课程术语表接入失败: {e} —— 退回 CLI 术语表")

    MAX_FLIGHTS = 2                 # 同一时刻最多在飞的翻译任务数
    flights: deque = deque()        # 在飞翻译任务队列（先进先出，等最旧）
    # 条目16：最近 N 条已定稿 EN 的滑动窗口（dispatch 时快照，不含当前段——
    # 翻译失败也已定稿广播，故 append 在派发前）
    en_history: deque = deque(maxlen=TRANSLATE_CONTEXT_WINDOW)

    async def translate_and_broadcast(seg: int, ts_clock: str, en: str) -> None:
        """后台翻译 → 二次广播中文 + 笔记补写；重试耗尽仍失败时降级补偿：
        字幕灰色『（翻译失败）』提示 + 笔记占位行，保证用户可见、笔记完整。
        条目16：每段先查术语表 mtime（双侧同步刷新），再带最近 3 条已定稿
        EN 上下文派译（仅参考前缀防指代/术语跨段漂移，1200 字符封顶）。"""
        try:
            sync_glossary_reload()
            context = list(en_history)
            en_history.append(en)
            # p1-5：读 runtime 而非闭包局部——首启向导热接入 key 后（/api/first-run/apikey
            # 重建 runtime['translator']），下一段翻译即用新实例；初始化后两者同引用，
            # 未接 key 时外层守卫（if runtime["translator"]）拦截，行为不变
            zh = await runtime["translator"].translate(en, context=context)
            status["last_zh"] = zh
            hub.append(ts_clock, "", zh, seg)
            # 只带 zh 不带 en：翻译乱序完成时，避免前端把旧段误判成新的英文段落
            await hub.broadcast({"ts": ts_clock, "seg": seg, "zh": zh})
            log.info(f"      → {zh}")
        except Exception as e:
            log.warning(f"[translate] error: {e}")
            hub.append(ts_clock, "", "（翻译失败）", seg)
            # err 标记：前端把该行渲染成灰色，区别于正常黄色译文
            await hub.broadcast(
                {"ts": ts_clock, "seg": seg, "zh": "（翻译失败）", "err": True}
            )

    while True:
        try:
            if status["paused"]:
                # 暂停：丢弃积累的音频段（恢复后不回放旧语音），电平表照常工作
                while not runtime["seg_q"].empty():
                    try:
                        runtime["seg_q"].get_nowait()
                    except queue.Empty:
                        break
                await asyncio.sleep(0.2)
                continue

            if runtime["asr"] is None:  # 初始加载失败，等 /api/config 修复
                await asyncio.sleep(1)
                continue

            # 追赶丢弃：积压超阈值说明消费慢于产出，旧段转写出来字幕也早已过时
            # —— 丢最旧的只留最新几段，宁可缺句不可慢半拍（log 留痕）
            # p2-5：阈值改从 config 现读（无需重建采集器）；缺键回落模块常量。
            drop_th, keep_n = config_backlog_thresholds(runtime["config"])
            dropped = backlog_trim(runtime["seg_q"], drop_th, keep_n)
            if dropped:
                log.warning(f"[pipeline] ⚠ 追赶丢弃 {dropped} 段旧音频"
                            f"（积压超 {drop_th}），保留最新 {keep_n} 段追实时")

            if scheduler is not None:
                # 流式路径：final 段交给解码调度器（prio=0 定稿优先于积压 partial），
                # 任务携带 runtime['asr'] 入队快照——热切换后旧实例任务自然排空。
                # 条目12①：seg_q 元组首元素 = 音频采集墙钟秒，经 meta 透传进 payload.audio_ts。
                ts_audio, audio = await asyncio.to_thread(runtime["seg_q"].get)
                scheduler.enqueue_final(runtime["asr"], audio, ts_audio=ts_audio)
                en, meta = await final_ready_q.get()
                audio_ts = meta.get("ts_audio")
                decode_s = meta.get("decode_s", 0.0)
                # p2-3：流式 final 由调度器在转写时顺带带回段级时间戳（同一条
                # _handle_final 路径，故流式/整句两侧行为一致）
                seg_times = meta.get("seg_times") or []
            else:
                ts_audio, audio = await asyncio.to_thread(runtime["seg_q"].get)
                t0 = time.time()
                en, seg_times = unpack_transcribe(await asyncio.to_thread(
                    runtime["asr"].transcribe, audio,
                    initial_prompt=runtime["prompt"], with_times=True))
                audio_ts = ts_audio  # 条目12①：不再丢弃采集时间戳
                decode_s = time.time() - t0
            # 条目3打点：每段转写耗时（debug 级——只落盘，控制台行为不变）
            log.debug(f"[asr] seg#{status['segments'] + 1} 转写耗时 {decode_s:.2f}s"
                      f"（音频 {len(audio) / SR:.1f}s）")
        except Exception as e:
            log.warning(f"[pipeline] error: {e}")
            await asyncio.sleep(1)
            continue
        if not en:
            continue

        try:
            status["segments"] += 1
            status["last_en"] = en
            ts_clock = datetime.datetime.now().strftime("%H:%M:%S")
            hub.append(ts_clock, en, "")
            # 评审1 修：capture 投递的 audio_ts 是**窗口末尾**（_emit 组装完才 put），
            # 本段音频的**窗口起点** = 发射时刻 − 窗口时长。下游一律用起点：
            # ① record_time → SRT cue 不再整体后移（旧实现每段误差 = d_i − d_1）；
            # ② payload.audio_ts → 前端「延迟 ≈ N s」不再少报，向导「刚说的那句」
            #    判定也不再会把 d 秒前的旧话当成刚说（index.html:1488 写的就是
            #    「该句音频的采集墙钟秒…什么时候说的」——旧实现与它自己的注释矛盾）。
            audio_s = len(audio) / SR
            win_start = (audio_ts - audio_s) if audio_ts is not None else None
            hub.record_time(status["segments"], win_start, seg_times, audio_s)
            await hub.broadcast(final_payload(ts_clock, en, status["segments"], win_start))
            log.info(f"[{ts_clock}] {en}")

            if runtime["translator"]:  # p1-5：读 runtime（闭包局部与它同引用，行为不变）
                # 翻译异步化：派后台任务，主循环立即回去取下一段音频
                while flights and flights[0].done():
                    flights.popleft()
                if len(flights) >= MAX_FLIGHTS:
                    await flights.popleft()  # 在飞已满：等最旧的任务完成再派新
                flights.append(asyncio.create_task(
                    translate_and_broadcast(status["segments"], ts_clock, en)
                ))
        except Exception as e:
            log.warning(f"[segment] error: {e}")


def build_lifespan(args):
    """构造 app 的 lifespan 上下文管理器（p1-1：原为 main() 内嵌函数，提升为模块
    级，desktop.py 与 main() 复用同一份启停代码——单一事实来源防两入口漂移）。
    args：main.parse_args() 的 Namespace，或字段等价的占位（desktop.py 传无覆盖
    值）。原实现闭包里用全局 ARGS 与局部 args（main() 里 args = ARGS =
    parse_args()，两值同源），此处统一用入参 args，行为不变。"""

    async def _open_browser_later() -> None:
        """等服务开始监听后再开浏览器：页面加载即显示模型加载进度条。"""
        await asyncio.sleep(2.5)
        if os.environ.get("LECTURELENS_NO_BROWSER"):
            return
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{args.port}")
        except Exception as e:
            log.warning(f"[main] ⚠ 打开浏览器失败: {e}（手动访问 http://localhost:{args.port}）")

    def _watch_asr_task(task: asyncio.Task) -> None:
        """识别主循环的 done-callback：任务意外死亡时把异常写进状态与日志，
        不再静默（cancelled = lifespan 正常关闭，不算故障）。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            status["asr"] = f"error: loop crashed ({exc})"
            log.error(f"[main] ⚠ 识别主循环崩溃: {exc!r}")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(asr_loop(args))
        task.add_done_callback(_watch_asr_task)
        if args.open_browser:
            asyncio.create_task(_open_browser_later())
        yield
        task.cancel()
        # 关停收尾：停分句器（不再喂音频）并向 seg_q 投递哨兵，解除卡在
        # to_thread(seg_q.get) 里的执行器线程——否则 uvicorn 经
        # loop.shutdown_default_executor() join 它时，Ctrl+C 会挂到有人说话
        # （v0 已有，评审1指出修复6未闭合）
        seg = runtime["segmenter"]
        if seg:
            seg.stop()
        if runtime["seg_q"] is not None:
            runtime["seg_q"].put(None)  # 哨兵：让阻塞的 get 返回、线程退出
        # 条目11：停流式解码调度器（feeder/worker 线程以 0.2s 轮询 _stop 事件退出；
        # feeder 消费 stream_q，须在分句器停止后收尾防竞态）
        sched = runtime["scheduler"]
        if sched is not None:
            sched.stop()
            runtime["scheduler"] = None
            log.info("[main] 🛑 流式解码调度器已停止")
        # p3-1：注销全局热键。进程退出本会带走钩子，但桌面壳/测试里同一个进程可能
        # 反复起停服务（--check 起停一次、桌面壳退出再启动），显式注销才不会把
        # 旧键位留给下一个实例——那时旧回调仍指向已停用的 status/闭包。
        mgr = runtime.get("hotkeys")
        if mgr is not None:
            try:
                status["hotkeys"] = mgr.stop()
            except Exception as e:  # stop() 自身不抛，防未来改动回归
                log.warning(f"[hotkeys] ⚠ 注销热键异常（忽略）: {type(e).__name__}: {e}")
        log.info("[main] 🛑 分句器已停止")

    return lifespan


def main():
    global ARGS
    args = ARGS = parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    # 条目3：结构化日志落盘（服务运行才建 logs/；--list-devices 是纯 CLI 查询不建）
    setup_logging(ROOT / "logs")

    # 条目19①：首次启动生成随机 token 写 config.json 并 print（--list-devices 纯
    # 查询已提前返回，不会建/写配置文件）
    provision_token()

    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        log.info(f"[main] 🎙 输入设备: {dev['name']}")
    except Exception as e:
        log.warning(f"[main] ⚠ 查询输入设备失败: {e}")

    # p1-1：lifespan 三件套（_open_browser_later/_watch_asr_task/lifespan）原内嵌
    # 在此，已提升为模块级 build_lifespan(args)（行为不变），desktop.py 复用同一份
    app.router.lifespan_context = build_lifespan(args)

    log.info(f"[main] LectureLens starting — port={args.port}, lang={args.lang}"
             + (", force-gpu" if args.gpu else ""))
    # uvicorn 经 dictConfig 纳入同一落盘（applog.uvicorn_log_config）：
    # 自带 handler 清空、propagate 到 root；WARNING 级别维持原 log_level='warning'
    # 的控制台表现（INFO 行本来就被抑制）
    # 评审修复①：proxy_headers=False——uvicorn 默认 True 会安装
    # ProxyHeadersMiddleware（config.py:225/543），把受信对端
    # （FORWARDED_ALLOW_IPS，缺省 "127.0.0.1,::1"，config.py:363）发来的
    # X-Forwarded-For 改写进 scope["client"]：默认环境下本机带 XFF 会被误判为
    # 非环回（403，fail-closed 破坏豁免）；FORWARDED_ALLOW_IPS=* 下 LAN 对端
    # 伪造 XFF:127.0.0.1 可整体绕过环回豁免（实测复现 101）。本服务无前置反代，
    # 条目19 豁免只认 socket 对端——直接关闭代理头改写。
    uvicorn.run(app, host="0.0.0.0", port=args.port,
                log_config=uvicorn_log_config(), proxy_headers=False)


if __name__ == "__main__":
    main()
