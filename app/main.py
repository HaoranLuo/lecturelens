# -*- coding: utf-8 -*-
"""main.py — 课堂实时字幕翻译服务。
用法: python main.py [--glossary glossary/CS.md ...] [--model large-v3-turbo] [--gpu] [--port 8787]
组件：麦克风 VAD 分句（capture）→ faster-whisper（asr）→ DeepSeek 翻译（translate）
     → WebSocket 字幕广播 + 双语笔记实时落盘（transcripts/）。
配置：config.json（模型/设备/VAD）持久化，字幕页可热切换；CLI 参数优先级更高。
绑定：默认只监听 127.0.0.1（不对外）；config.lan_enabled=true（§7 手机同屏）→ 监听
     0.0.0.0（优先 --port/8787，被占换空闲端口）。**启动时读一次**，改键要重启才换绑。
鉴权：/api/* 与 /ws 校验 LAN token（条目19），127.0.0.1 豁免；首启生成 token 写 config.json 并打印。"""
import argparse
import asyncio
import contextlib
from collections import deque
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

# —— ROOT/WEB 推导（D2 起提前到第三方导入之前：HF 环境变量要在这里就能读到 config.json）——
# 条目20：PyInstaller frozen——PyInstaller onedir 下 Path(__file__) 指向 _internal 打包数据
# （只读），且 exe 的 cwd 语义与开发态不同；ROOT 必须是 exe 所在目录（config.json/token/
# transcripts/logs 跟着 exe 走、可写、换版不丢），WEB 用打包进 _internal 的只读数据
# （--add-data app/web;web）。
# Mac 适配·第三批：**数据面**的 ROOT 在 macOS 上不能用 exe 目录——frozen 的 macOS 产物是
# 一个 .app 包（Contents/ 是签名覆盖的只读包体），往包内写 config/笔记会破坏代码签名、
# 也让「换新 .app」变成「把数据一起换掉」。故 darwin 用用户数据目录
# ~/Library/Application Support/LectureLens（macOS 规范位置，Time Machine 照常备份），
# 只读资源（web/glossary_packs）仍走 _MEIPASS——与开发/Windows 完全同一套读取代码。
def _frozen_root(platform_name: str | None = None) -> Path:
    """frozen 下的数据根（ROOT）。纯函数（平台名可注入）——这是「用户数据落在哪」的
    唯一决定点，必须在 Windows 上也能对 darwin 分支做单测（本机跑不了 Mac）。

    platform_name=None 时**在调用时**读 sys.platform（而不是 def 时绑成默认值）：
    否则 monkeypatch sys.platform 的测试拿到的是导入那一刻的平台，形同虚设。"""
    if platform_name is None:
        platform_name = sys.platform
    if platform_name == "darwin":
        return Path.home() / "Library" / "Application Support" / "LectureLens"
    return Path(sys.executable).resolve().parent


if getattr(sys, "frozen", False):
    ROOT = _frozen_root()
    if sys.platform == "darwin":
        # 首次运行要有这个目录，否则后面 save_config/日志/笔记全部写失败。
        # 建不出来（权限/磁盘）在这里不抛：后续那条真正的写盘会给出更具体的错误。
        with contextlib.suppress(OSError):
            ROOT.mkdir(parents=True, exist_ok=True)
    WEB = Path(getattr(sys, "_MEIPASS", ROOT)) / "web"
    # B13：术语库包目录——打包版是**只读数据**（--add-data glossary_packs;glossary_packs
    # → _internal/glossary_packs），开发态读仓库根。与 WEB 同款双路径，见 packs.packs_dir。
    PACKS_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "glossary_packs"
else:
    ROOT = Path(__file__).resolve().parent.parent
    WEB = Path(__file__).resolve().parent / "web"
    PACKS_DIR = ROOT / "glossary_packs"
TRANSCRIPTS = ROOT / "transcripts"
CONFIG_PATH = ROOT / "config.json"
COURSES_PATH = ROOT / "courses.json"   # p2-1：课程表（与 config.json 同放根目录）

# —— D2（分发工程）：HuggingFace 环境必须在 huggingface_hub / faster_whisper 导入前落定 ——
# （那些库在 import 时把 env 读成模块级常量，之后再改就无效了。）旧行为是 frozen 时
# 无条件 HF_HUB_OFFLINE=1 —— 那是「打包版永远不能下载新档位」的根源：/api/models/download
# 于是恒以 local_files_only=True 进 models.download_model，本地没缓存必然失败。
# 现在：随包模型（bundled_model_path 的本地目录）离线可用，其余档位允许在线下载；
# 显式离线（环境里 HF_HUB_OFFLINE=1）仍被尊重；config.hf_mirror=true → HF_ENDPOINT 走
# **唯一预设镜像** hf-mirror.com（只认布尔键，绝不接受任意 URL）。详见 hfenv.py 模块头。
import hfenv  # noqa: E402
HF_ENV = hfenv.apply(CONFIG_PATH)   # 日志装配好之后（main() 里）再如实打一行

from applog import setup_logging, uvicorn_log_config  # noqa: E402
from ankideck import build_deck  # noqa: E402  条目18：Anki 复习卡生成
from branding import APP_VERSION  # noqa: E402  p1-3：版本号单一来源（status/关于页）
from asr import WhisperASR          # noqa: E402
import asr as asr_mod               # noqa: E402  Mac 适配：后端判定/MLX 就绪（active_backend）
import autostart as autostart_mod   # noqa: E402  p3-3：开机自启（HKCU Run 项读写）
import courses as courses_mod       # noqa: E402  p2-1：课程管理（courses.json 存储/校验）
import notes as notes_mod           # noqa: E402  p2-2：笔记库（transcripts 解析/索引/检索）
import exportdir as exportdir_mod   # noqa: E402  §3：导出位置自选（export_dir 校验/文件名）
import exporters                    # noqa: E402  p2-3：导出中心（md/SRT/PDF/Word）
import models as models_mod         # noqa: E402  p2-4：模型管理器（缓存/下载/删除）
import tiers as tiers_mod           # noqa: E402  档位化：硬件检测与模型档位推荐
import glossary as glossary_mod     # noqa: E402  p2-6：术语库（glossary/*.md 增删改）
import packs as packs_mod           # noqa: E402  B13：术语库包（glossary_packs/*.md → 课程术语表）
import hotkeys as hotkeys_mod       # noqa: E402  p3-1：全局快捷键（键名白名单/注册/降级）
import lan as lan_mod               # noqa: E402  §7：手机同屏（绑定决策/主网卡探测/配对二维码）
import system_info as system_info_mod   # noqa: E402  B8：系统级只读事实（供电状态）
from capture import SR, MicSegmenter  # noqa: E402
from loopback import LoopbackSegmenter  # noqa: E402  条目14（scipy 依赖一并就位）
from scheduler import DecodeScheduler  # noqa: E402
from summarize import TranscriptSummarizer  # noqa: E402  条目17：课后 AI 笔记摘要（map-reduce）
from translate import DeepSeekTranslator, glossary_prompt, load_api_key  # noqa: E402

log = logging.getLogger("main")

# p2-1：课程调色板——侧栏 8px 圆点的身份标识色。**故意不用 spec §二 的语义令牌**
# （--ok/--warn/--err 表达状态，拿去当课程身份会与「在线/告警」语义打架），也不含
# --gold（§一-2 金色专属中文译文/选中态/主按钮/品牌）。一组低饱和、暗底可辨的中性
# 色相；只做小圆点，绝不大面积铺底（§一-2）。这是对 spec 的扩展，见 courses.py 模块注释。
COURSE_PALETTE = ("#93a1b0", "#7fa6d9", "#9b8fd9", "#6fbfa8",
                  "#c9a87c", "#d98fa6", "#8fbf7f", "#b8a0d9")


def bundled_model_path(name: str) -> Path | None:
    """条目20：frozen 下解析随包模型目录（<exe>/models/<name>，由 build_exe.py 从
    HF 缓存快照复制落盘，须含 model.bin）；开发模式恒 None（按名走 HF 缓存，
    行为零变化）。目录缺失/被删 → None（走 resolve_model_ref 的下一档）。"""
    if not getattr(sys, "frozen", False):
        return None
    p = ROOT / "models" / name
    return p if (p / "model.bin").is_file() else None


def bundled_model_names() -> list:
    """frozen 构建**随包**的模型名（<exe>/models/<name> 且含 model.bin）；开发态恒空。

    D2(c)：打包版首启要默认选随包模型（否则朋友离线首启会卡在下载 DEFAULT_CONFIG
    里的 turbo）。轻量版只随包 small，完整版可能带多个——返回排序后的名字，由调用点
    按口径选一个。"""
    if not getattr(sys, "frozen", False):
        return []
    root = ROOT / "models"
    try:
        return sorted(p.name for p in root.iterdir()
                      if (p / "model.bin").is_file())
    except OSError:
        return []


def pick_bundled_default(names: list) -> str | None:
    """随包模型里选一个当默认（D2(c) 的纯逻辑部分，便于单测）。

    small 优先（最稳、纯 CPU 也能跑、体积最小）；否则取名字序第一个（完整版可能只带
    large-v3-turbo）。空列表 → None（调用点回落到旧行为并 warn）。用户随时可在设置页
    改选——这里只决定「首启那一次」。"""
    if not names:
        return None
    return "small" if "small" in names else names[0]


def hf_offline() -> bool:
    """D2：**显式离线开关** = 环境里的 `HF_HUB_OFFLINE=1`（用户/开发显式设置）。

    frozen **不再**自动置位（旧行为让打包版永远无法下载新档位）。下载端点据此走
    `local_files_only`；打包版默认 false = 可以下载更高档位。"""
    return os.environ.get(hfenv.OFFLINE_ENV) == "1"


def hf_mirror_active() -> bool:
    """D2：进程内**实际生效**的镜像状态（env 已指向预设镜像）。

    huggingface_hub 在 import 时读 env 常量，故改 config.hf_mirror 要**重启进程**才
    生效——UI 据此如实展示「已保存，重启后生效」，而不是假装立即生效。"""
    return os.environ.get(hfenv.ENDPOINT_ENV) == hfenv.MIRROR_ENDPOINT


def resolve_model_ref(name: str, backend: str = "ctranslate2") -> tuple:
    """D2(b)：模型解析次序 → 返回 `(本地路径或 None, 来源标签)`。

    ① `bundled`：随包目录（frozen）——**永远离线可用**，与任何 HF 开关无关；
    ② `cache`：HF 缓存里**已完成**的 snapshot（models.snapshot_dir）——「已经下载过
       的档位」离线也能加载，不必联网复核；
    ③ `hub`（None）：交给后端按模型名/仓库 id 取（首次使用联网下载）；调用点在
       离线开关开启时给可读错误（见 build_asr 的异常分支），不静默挂网。

    Mac 适配：backend="mlx" 时**跳过 ①**（随包目录里放的是 ctranslate2 权重，
    MLX 读不了），②的判据换成 weights.safetensors（models.snapshot_dir(backend)），
    ③交给 mlx_whisper 按 mlx-community 仓库 id 取（它内部走 huggingface_hub，
    hfenv 的镜像/离线开关同样生效）。"""
    if backend != "mlx":
        p = bundled_model_path(name)
        if p is not None:
            return str(p), "bundled"
    snap = models_mod.snapshot_dir(
        models_mod.entry_dir(models_mod.cache_dir(), name, backend), backend)
    if snap is not None:
        return str(snap), "cache"
    return None, "hub"

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
                  "max_seg_s": 8.0,       # 软上限：超长段不再硬切，改等静音对齐切点
                  "hard_cut_s": 10.0,     # 硬上限兜底：一直不停嘴时的强制切点
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
                  "float_x": None, "float_y": None,
                  # §3（导出位置自选）：None = 未设置 → 导出维持**浏览器下载**（现状）。
                  # 字符串 = 绝对路径，导出直接落盘到该目录并回 JSON {ok, path}。
                  # 组合串/路径都不是枚举值，故走专项校验 exportdir.dir_problem
                  # （绝对路径 / 可创建 / 可写 / 非系统关键目录），与 input_device 同款。
                  "export_dir": None,
                  # §5（下课一键归档）：点工具条「下课」时先弹二次确认。true = 弹
                  # （默认：一键归档会暂停收音并产生 LLM 费用，手滑的代价不小）；
                  # 可在设置页关掉（关掉后点「下课」立即开始，不再询问）。
                  "class_end_confirm": True,
                  # §7（手机扫码同屏）：false = 只监听 127.0.0.1（默认；与「不对外」
                  # 的既有姿态一致——服务不会因为在跑就暴露在同网段里）；true =
                  # 监听 0.0.0.0（优先 8787，被占则换端口），同 WiFi 持 token 设备可看字幕。
                  # **监听地址在进程内不可热改**：改这个键只是写进配置，换绑要重启
                  # 服务（/api/lan-qr 回 restart_required，面板文案照此如实说）。
                  "lan_enabled": False,
                  # D2（分发工程）：模型下载镜像开关（bool，默认 false=官方 huggingface.co）。
                  # true → 在 HF 库导入前设 HF_ENDPOINT=https://hf-mirror.com（**唯一预设
                  # 值**，见 hfenv.py）；国内网络下载模型加速。**非即时键**：HF 的 endpoint
                  # 常量在 import 时读入，改这个键只是写进配置，**重启应用**才生效
                  # （设置页文案与 /api/models 的 mirror 字段都按此如实说）。
                  "hf_mirror": False,
                  # B3（常用标签自定义）：笔记「预设 chips」的标签表（设置页 📝笔记 可增删）。
                  # 出厂值 = 旧的写死四标签（老用户零变化）。**即时键**：四个下发点都走
                  # main.tag_presets() 现读 runtime['config']，保存后下一次响应即新值。
                  # 取值是**列表**，塞不进 CONFIG_VALUE_CHOICES 那种静态枚举表，故走专项
                  # 校验 tags_presets_norm（非特判键会撞 CONFIG_VALUE_CHOICES[k] KeyError）。
                  "tags_presets": ["重点", "考试", "没听懂", "待复习"]}

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
                       "float_x", "float_y",
                       # §3：导出位置（字符串绝对路径或 null，走专项校验
                       # exportdir.dir_problem——不在 CONFIG_VALUE_CHOICES）
                       "export_dir",
                       # §5：下课归档的二次确认开关（布尔枚举，即时键）
                       "class_end_confirm",
                       # §7：手机同屏（布尔枚举；**非即时键**——写配置即存盘，但
                       # 监听地址要重启服务才换，见 DEFAULT_CONFIG 注释）
                       "lan_enabled",
                       # D2：模型下载镜像（布尔枚举；**非即时键**——HF endpoint 在库
                       # 导入时读入，重启应用才生效，见 hfenv.py）
                       "hf_mirror",
                       # B3：常用标签（预设 chips）。取值是**列表**（list[str]），走专项
                       # 校验 tags_presets_norm——不在 CONFIG_VALUE_CHOICES（那是枚举表，
                       # 对未特判键直接 KeyError，见 set_config 的专项分支）
                       "tags_presets")
# 条目13：lang 码表 = auto + en/zh + 常用语言码（whisper 语言码，UI 下拉为其常用子集）
LANG_CHOICES = ("auto", "en", "zh", "ja", "ko", "fr", "de", "es", "ru", "pt", "it")
# 条目16：translate.model 配置化——切档位前以 DeepSeek 官方定价页为准
TRANSLATE_MODEL_CHOICES = ("deepseek-chat", "deepseek-reasoner")
CONFIG_VALUE_CHOICES: dict = {
    "model": MODEL_CHOICES,
    # Mac 适配·第二批：device 加 "mlx"（Apple Silicon 的 mlx-whisper 后端）。
    # auto 的解析次序在 asr._resolve_device：darwin+arm64+mlx_whisper → mlx，
    # 否则维持原有的 CUDA 探测（Windows 行为零变化）；显式 "mlx" 而本机不具备
    # → 自动回落 CPU（不因后端缺失起不来）。
    "device": ("auto", "cpu", "cuda", "mlx"),
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
    # §5：下课归档的二次确认（前端点「下课」时现读；关掉 = 点一下就直接开始归档）
    "class_end_confirm": (False, True),
    # §7：手机同屏开关。写盘即生效（前端读 /api/lan-qr 的 enabled/active 自行区分
    # 「已开」与「已开但还没重启」），**换绑监听地址仍要重启**——不是即时键。
    "lan_enabled": (False, True),
    # D2：模型下载镜像开关。写盘即存；**HF endpoint 要重启应用才换**
    # （huggingface_hub 在 import 时读 env 常量），/api/models 的 mirror 字段回的是
    # 进程内实际生效值——设置页据此显式区分「已保存」与「已生效」。
    "hf_mirror": (False, True),
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
    # 2026-10 首期反馈②：旧默认三元组（700/15/18）会让连续说话的中文字幕等近 20 秒，
    # 默认已下调为 700/8/10。**只有恰好等于旧默认**的配置才迁移——用户明确调过的
    # 任何其它组合一律不动；迁移写盘一次后不再命中（幂等）。
    if (cfg.get("silence_ms") == 700 and cfg.get("max_seg_s") == 15.0
            and cfg.get("hard_cut_s") == 18.0):
        cfg["max_seg_s"], cfg["hard_cut_s"] = 8.0, 10.0
        try:
            save_config(cfg)
            log.info("[config] 分句参数迁移：max_seg_s 15→8s，hard_cut_s 18→10s"
                     "（首期反馈：连续说话时字幕等太久；可随时在设置页调回）")
        except Exception as e:
            log.warning(f"[config] ⚠ 分句参数迁移未能写盘（本次运行内生效）: {e}")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def user_picked_model() -> bool:
    """用户是否显式选过识别模型（档位化「自动推荐」的前提）。

    判据 = config.json 存在且含**非空 model 键**。首启 provision_token 只写
    auth_token 键，不会造成误判；用户一旦在设置页/字幕页选过模型（写盘），
    此后永远以用户的选择为准，不再自动推荐。文件损坏/读不到一律按「选过」处理
    （保守：宁可不动用户的配置，也不覆盖一个读不懂的文件）。"""
    try:
        if not CONFIG_PATH.is_file():
            return False
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return bool(isinstance(raw, dict) and raw.get("model"))
    except Exception:
        return True


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


#: B3（常用标签自定义）：预设 chips 的条数上限。不是产品限制而是**版面**限制——
#: 笔记库批量面板一行放 12 个 chip 已经很挤，且这份表每次列表刷新都要随响应带回前端。
TAG_PRESETS_MAX = 12


def tags_presets_norm(value) -> tuple[list[str] | None, str | None]:
    """B3：常用标签（config.tags_presets）的校验 + 规范化 → (标签表, 错误文案)。

    「专项校验」家族的一员（与 _hotkey_problem / _config_number_problem / export_dir
    同款）：取值是**列表**，塞不进 CONFIG_VALUE_CHOICES 那种静态枚举表——**不特判就会在
    set_config 里撞 `CONFIG_VALUE_CHOICES[k]` 直接 KeyError（500）**，这就是本分支存在的
    理由。

    规则（B3 口径）：必须是 list（不是字符串——`"重点"` 会被 Python 拆成一串单字，
    这种「看起来像成功了」的坏值最难查）；长度 1–TAG_PRESETS_MAX；每项过
    notes.normalize_tag（≤16 字符、禁一切空白/控制字符与 `#`，宽松接受开头的 `#`）；
    同名去重（保序）。

    **为什么返回规范化值而不是纯 bool**：与 export_dir（校验即原值）不同，这一支要
    去首尾空白/开头的 `#` 并去重——校验通过后**存规范形**，否则 config 里会留下
    `["重点", "#重点"]` 这种前端还得自己再兜一次的重复项（与 POST /api/notes/tags 的
    normalize_tags 同一条口径）。"""
    if not isinstance(value, list):
        return None, (f"非法 tags_presets: {value!r}"
                      f"（须为标签数组，如 ['重点', '考试']）")
    if not (1 <= len(value) <= TAG_PRESETS_MAX):
        return None, (f"非法 tags_presets: {len(value)} 条"
                      f"（允许 1–{TAG_PRESETS_MAX} 条；要清空请留一条或恢复默认）")
    out: list[str] = []
    for one in value:
        tag = notes_mod.normalize_tag(one)
        if tag is None:
            return None, (f"非法标签: {one!r}（≤{notes_mod.TAG_MAX_LEN} 字符；"
                          f"禁一切空白/控制字符与 #）")
        if tag not in out:          # 同名去重（保序：用户排的顺序有意义）
            out.append(tag)
    return out, None


def _tags_presets_problem(value) -> str | None:
    """B3：tags_presets 的错误文案视图（None = 合法）。规范化结果见 tags_presets_norm。

    单独留这一支是为了与其余 _problem 函数**同名同形**（错误文案一律走 400 的 error
    字段），调用方在 set_config 里成对使用：先 norm 拿规范形、problem 只用于报错。"""
    return tags_presets_norm(value)[1]


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
        # 评审2① 修复：**非环回**的 WS 连接（手机/局域网设备）。status.ws_clients 报的
        # 是这个集合的大小——len(clients) 会把本机自己的窗口算进去（桌面窗口、浏览器
        # 标签、悬浮条都会连 WS），面板上的「已连接 N 台设备」于是恒 ≥1、永远区分不出
        # 「手机连上了」和「只有我自己的窗口」，那条连通性诊断就废了。
        self.lan_clients: set = set()
        self.transcript_path: Path | None = None
        self.stamp: str | None = None   # p2-1：笔记时间戳（改写头时复用，不重新取时）
        self.note_seg = 0                    # 笔记级段号（ZH 锚点 / 时间轴 seg 口径）
        self.t0_audio: float | None = None   # p2-3：本场首段音频采集墙钟（时间轴原点）
        self.times: list = []                # p2-3：[{"seg","start","end"}] 绝对偏移秒

    def start_transcript(self, course: str | None = None,
                         avoid: set | None = None) -> Path:
        """开一篇新笔记 → 返回路径（同时把 hub 的笔记指针/时间戳切过去）。

        文件名是**分钟粒度**（lecture_YYYYMMDD_HHMM.md），所以"同一分钟再开一篇"
        是真实场景（重启、删掉正在记录的那篇后接力）。旧实现直接 write_text——
        同分钟重开会把前一篇**整篇截掉**（实测的数据丢失路径）；现在先避让：
        已存在（或在 avoid 名单里）就依次试 -2/-3…，绝不覆盖任何现有笔记。
        `avoid` 供 §4「删掉正在记录的那篇 → 接力新笔记」用：让它连**刚进回收站的
        那个名字**也避开，于是恢复旧笔记时不会撞名（两篇都在、各自可辨）。"""
        TRANSCRIPTS.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        p = TRANSCRIPTS / f"lecture_{stamp}.md"
        n = 2
        while p.exists() or (avoid and p.name in avoid):
            p = TRANSCRIPTS / f"lecture_{stamp}-{n}.md"
            n += 1
        p.write_text(note_header(stamp, course), encoding="utf-8")
        self.transcript_path = p
        self.stamp = stamp
        self.note_seg = 0         # 笔记级段号随新笔记归零（ZH 锚点/时间轴 sidecar 用）
        self.t0_audio = None      # p2-3：换笔记 = 时间轴重新起算
        self.times = []
        return p

    def next_note_seg(self) -> int:
        """本笔记内的下一段号（1 起）——ZH 锚点与 .times sidecar 的唯一口径。

        与会话级 status['segments'] 刻意分开：后者是「这场课一共说了几段」（工具栏
        徽章/前端行标识），前者是「这是本笔记的第几段」（文件内配对）。
        换笔记（§4 删掉正在记录的那篇 → 接力）时会话号继续、笔记号归零，若混用
        就会让新笔记的 EN 行（从 1 起）与锚点（接着上一场的号）对不上。"""
        self.note_seg += 1
        return self.note_seg

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

    def append(self, ts: str, en: str, zh: str, seg: int | None = None,
               note: Path | None = None) -> None:
        """往当前笔记追加一行（EN 或 ZH）。

        `seg` = **笔记级**段号（ZH 锚点；见 notes.parse 的配对规则），由调用方用
        next_note_seg() 取——不是会话级 status['segments']：换笔记后 EN 行从 1 重新
        计数，锚点必须同口径（评审实测：接力笔记若沿用会话号，整场中文列会静默消失）。

        `note` = 派发这段翻译时**它属于哪篇笔记**。晚到的译文若发现笔记已经换了
        （§4 删掉正在记录的那篇 → 接力新笔记），就直接丢弃：写进新笔记只会留下一条
        对不上任何 EN 行的孤儿中文行。"""
        if not self.transcript_path:
            return
        if note is not None and note != self.transcript_path:
            log.debug(f"[notes] 丢弃迟到译文（{seg}）：笔记已从 {note.name} 切到 "
                      f"{self.transcript_path.name}")
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
                 "hotkeys": None,  # p3-1：全局快捷键管理器（asr_loop 按持久化配置建）
                 # §7：本次进程**实际**监听的地址 {"host", "port"}——由入口写一次
                 # （main.main / desktop.start_server 调 set_bind）。/api/lan-qr 的
                 # 「是不是真的对外了」只信这个事实，不信配置意图。
                 "bind": None}

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


# —— 评审1① 修复：Host/Origin 守卫（DNS-rebinding 防线）——
# 背景（实测复现）：环回发 `GET /api/lan-qr` 带 `Host: evil.example` +
# `Origin: http://evil.example` 曾拿到 200，响应 url 里的 token 与 config 的
# auth_token 逐字相等；同形态读 /api/notes/list 也 200 带数据。这是与
# 「评审修复②（token 不出 API）」同一类通道被重复开口——环回豁免 + 只判 socket 对端。
# 攻击链：恶意页 → DNS rebinding 到自己控的域名解析到 127.0.0.1 → 浏览器把它当
# 同源，于是能**读到响应体**（CORS 拦不住同源请求）→ token 到手 = LAN 侧唯一门禁到手。
# 防线只能落在 **Host 头**上（浏览器独占该头，页面改不了）：rebinding 一定带攻击者
# 自己的域名，而那是个多标签域名。

#: 单标签主机名一律放行（localhost / 本机名 DESKTOP-ABC / 测试里的 test、testserver）。
#: 单标签名注册不了公网域名，也就做不成 rebinding 的载体。
SINGLE_LABEL_HOSTS_OK = True


def host_name(host_header: str | None) -> str:
    """Host 头 → 主机名（小写、去端口、去 IPv6 方括号）。畸形/空 → ""。"""
    s = str(host_header or "").strip()
    if not s:
        return ""
    if s.startswith("["):                      # [::1]:8787
        end = s.find("]")
        return s[1:end].lower() if end > 0 else ""
    if s.count(":") == 1:                      # host:port（IPv6 无括号时不这么写）
        s = s.split(":", 1)[0]
    return s.lower()


def env_allowed_hosts() -> set[str]:
    """逃生口：LECTURELENS_ALLOW_HOSTS=名字1,名字2 —— hosts 别名 / 隧道域名等自定
    场景用（与 LECTURELENS_NO_BROWSER 等同类，属开发机开关，不进 config 键）。"""
    raw = os.environ.get("LECTURELENS_ALLOW_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_is_allowed(host_header: str | None) -> bool:
    """Host 头是否可信（DNS-rebinding 的唯一判据，浏览器独占该头）。

    放行：① IP 字面量（127.0.0.1 / ::1 / 192.168.1.5 / [::1]——文档里两种用法都在此列）；
    ② 单标签主机名（localhost / 本机名 / 测试用的 test，见 SINGLE_LABEL_HOSTS_OK）；
    ③ 本机自己的主机名与 FQDN（socket.gethostname()/getfqdn()——学院机器上按名访问的场景）；
    ④ 环境变量 LECTURELENS_ALLOW_HOSTS 里显式列出的名字。
    拒绝：其余多标签域名（evil.example、x.nip.io、attacker.com…）——rebinding 的载体。
    """
    name = host_name(host_header)
    if not name:
        return False
    if name in env_allowed_hosts():
        return True
    try:                                       # ① IP 字面量（v4/v6 都算直连地址）
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name in {"localhost", socket.gethostname().lower(),
                socket.getfqdn().lower()}:
        return True
    if SINGLE_LABEL_HOSTS_OK and "." not in name:   # ② 单标签名
        return True
    return False


def origin_ok(origin: str | None, host_header: str | None) -> bool:
    """Origin 与请求 Host 同源吗。**没有 Origin → True**（同源导航/非浏览器客户端）。

    浏览器只在跨源请求与 POST/PUT/DELETE 上带 Origin，所以「有 Origin 且与 Host 不同源」
    = 某个网页在借用户浏览器的手调本机服务：读取那侧被 CORS 挡住，但**副作用**
    （改配置、轮询 token、弹文件夹窗口）不该在挡住之前就执行。"""
    if not origin or origin == "null":
        return not origin                          # "null"（沙箱 iframe/本地文件）一律拒
    try:
        p = urlparse(origin)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    origin_netloc = (p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80))
    host = host_name(host_header)
    host_port = None
    raw = str(host_header or "").strip()
    if raw.startswith("["):
        tail = raw[raw.find("]"):]
        if tail.startswith("]:") and tail[2:].isdigit():
            host_port = int(tail[2:])
    elif raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
        host_port = int(raw.rsplit(":", 1)[1])
    # 端口缺省时按 Host 头算不出，就用「主机名相同」判同源（同主机不同端口在浏览器里
    # 是不同源，但 Host 头里通常带端口；真带不上时宁可放行同主机的请求，别误伤本机页面）
    return origin_netloc[0] == host and (host_port is None or origin_netloc[1] == host_port)


def loopback_only_ok(peer: str | None, host_header: str | None,
                     origin: str | None = None) -> tuple[bool, str]:
    """「仅本机可用」端点的完整判据（评审1①）：socket 对端环回 **且** Host 是本机/直连
    地址 **且** 没有跨源 Origin。返回 (是否放行, 不放行的原因)。

    /api/lan-qr 与 /api/export/open 共用——两处都需要「只有本机能读/能触发」，而两者
    原先都只判了 socket 对端（rebinding 页面来自本机环回，peer 判据天然为真）。"""
    if not is_loopback_peer(peer):
        return False, f"对端不是本机（peer={peer}）"
    if not host_is_allowed(host_header):
        return False, f"Host 不是本机地址（Host={host_header!r}）"
    if not origin_ok(origin, host_header):
        return False, f"跨源请求（Origin={origin!r} 与 Host={host_header!r} 不同源）"
    return True, ""


class HostGuardMiddleware:
    """Host/Origin 守卫（评审1①）：整个 /api/* 与 /ws 不接受外部域名寻址。

    - Host 不在允许集 → 400（rebinding 的请求一定带攻击者自己的域名，见 host_is_allowed）；
    - Origin 存在且与请求 Host 不同源 → 400（跨站页面借浏览器的手调本机服务）。
    纯 ASGI（http + websocket 双 scope，以后新增端点自动纳入），与 TokenAuthMiddleware
    同一姿势。放行条件宽松到不误伤：IP/单标签名/本机 FQDN/环境变量白名单。
    """
    PROTECTED = ("/api/", "/ws")

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path") or ""
            if any(path == p or path.startswith(p) for p in self.PROTECTED):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                           for k, v in (scope.get("headers") or [])}
                host = headers.get("host")
                origin = headers.get("origin")
                if not host_is_allowed(host) or not origin_ok(origin, host):
                    log.warning(f"[host] ⚠ 拒绝可疑 Host/Origin: {scope['type']} {path} "
                                f"Host={host!r} Origin={origin!r} "
                                f"peer={(scope.get('client') or (None,))[0]}")
                    if scope["type"] == "http":
                        resp = JSONResponse(
                            {"ok": False,
                             "error": "不接受的 Host/Origin：本服务只认本机与直连地址"
                                      "（IP / localhost / 本机名），不接受外部域名寻址"
                                      f"（Host={host!r}）"},
                            status_code=400)
                        await resp(scope, receive, send)
                    else:
                        await send({"type": "websocket.close", "code": 1008})
                    return
        await self.asgi_app(scope, receive, send)


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
# 评审1① 修复：Host/Origin 守卫**在鉴权之外再套一层**（Starlette 的 add_middleware
# 后加者在外层）——恶意的 Host/Origin 请求连鉴权都不必走到就先被 400；且它覆盖整个
# /api/* 与 /ws（含未来新增端点），不靠逐个端点记得加。
app.add_middleware(HostGuardMiddleware)


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
    （持久化由 /api/config 的 _reload 完成）——构建失败时 runtime 与 config.json 零污染。

    D2(b)：模型来源交给 resolve_model_ref 的三档次序（随包 → 本地缓存 snapshot → 按名
    联网），故「已下载的档位」离线也能加载；只有本地没有且不在离线态时才真去联网。"""
    cfg = _resolve_build_cfg(overrides)
    # Mac 适配：后端由 device 解析规则唯一决定（Mac arm64 + mlx-whisper → mlx）。
    # 模型来源的两张表/两种完整性判据都跟着它走（见 resolve_model_ref）。
    backend = asr_mod.active_backend(cfg.get("device"))
    ref, src = resolve_model_ref(cfg["model"], backend)
    stage = {"bundled": "随包模型", "cache": "本地缓存"}.get(src, "首次运行需下载")
    set_load_progress(12, f"加载模型 {cfg['model']}（{stage}）", "asr")
    try:
        asr = WhisperASR(model_size=cfg["model"], device=cfg["device"],
                         compute_type=cfg["compute_type"],
                         language=cfg.get("lang") or "en",  # 条目13②：读 config（重启后持久化生效）
                         model_path=ref)  # D2：bundled/本地缓存路径；None = 按名（可能联网）
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
        # D2：本地没有这一档 + 显式离线 → 错误里直接说清出路（否则用户只看到
        # huggingface_hub 的 "Cannot find the requested files in the disk cache"）。
        if ref is None and hf_offline():
            status["asr"] = (f"error: {e}（本地没有 {cfg['model']} 缓存，且离线开关 "
                             f"HF_HUB_OFFLINE=1 已打开；关掉离线开关或改用已下载的档位）")
        else:
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
            # Mac 适配：内录设备覆盖（macOS = BlackHole 的 /api/devices 索引，与麦克风
            # 同一条 config.input_device 事实源）。Windows 的 WASAPI 路径不读它——仍走
            # 系统默认扬声器的 loopback，行为逐字不变。
            seg = LoopbackSegmenter(runtime["seg_q"], stream_q=runtime["stream_q"],
                                    device=cfg.get("input_device", ARGS.device),
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
    # §7 + 评审2①：手头连着几台**设备**（面板「已连接 N 台设备」）。数的是
    # hub.lan_clients = 非环回对端的 WS（手机/局域网设备）——环回那几条是本机自己的
    # 窗口（桌面窗口/浏览器标签/悬浮条都连 WS），算进来会让这个数字恒 ≥1、失去诊断意义。
    # 现算不缓存：集合在 ws 端点 accept/断开时维护，len() 是 O(1)。
    status["ws_clients"] = len(hub.lan_clients)
    # B8：供电状态现算（glossary_rev 同款：不进 config、不挂钩子、不缓存——读的就是
    # "这一刻"的事实；挂钩子的话「用户拔掉电源」这类系统事件我们根本收不到）。
    # 一次 GetSystemPowerStatus 是 µs 级本地 syscall，比 status_loop 里那个被降频到
    # 每 5 轮的 nvidia-smi 轻三个数量级（那个要起子进程），故不做节流。
    # 三态：True=电池 / False=插电 / None=非 Windows 或读不到（前端只在 True 上提醒）。
    status["on_battery"] = system_info_mod.on_battery()
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
            if k == "export_dir":
                # §3：字符串（绝对路径）或 null。**null 是有含义的值**（取消自选 →
                # 回到浏览器下载），故与 input_device 同款：特判放在 no-op 判空之前。
                # 这里只做纯语法校验（不建目录——校验阶段零文件系统副作用）；
                # 「可创建 + 可写」的实证在整体校验通过后、状态改动前做（见
                # ensure_writable 那段）。
                if v is not None and not isinstance(v, str):
                    return JSONResponse(
                        {"ok": False,
                         "error": f"非法 export_dir: {v!r}"
                                  f"（须为绝对路径字符串，或 null 取消自选）"},
                        status_code=400)
                if v is not None:
                    v = v.strip()
                    problem = exportdir_mod.path_problem(v)
                    if problem:
                        return JSONResponse({"ok": False, "error": problem},
                                            status_code=400)
                    if v == cfg.get(k):
                        continue  # 与现值相同 = no-op
                elif cfg.get(k) is None:
                    continue      # null 且本来就没设 = no-op
                changes[k] = v
                continue
            if k == "tags_presets":
                # B3：常用标签数组。特判放在 no-op 判空**之前**（列表永远不是 None/""，
                # 落下去会撞 CONFIG_VALUE_CHOICES[k] KeyError——500 而不是 400）。
                # 校验通过后存**规范形**（去首尾 #/空白 + 去重保序），与 notes.normalize_tags
                # 同一条口径：config.json 里不留 `["重点", "#重点"]` 这类前端还得再兜的重复项。
                norm, problem = tags_presets_norm(v)
                if problem:
                    return JSONResponse({"ok": False, "error": problem},
                                        status_code=400)
                if norm == cfg.get(k):
                    continue  # 与现值相同（含顺序）= no-op
                changes[k] = norm
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

        # §3：export_dir 的**落地实证**（建目录 + 写探针）。位置与其他校验同款：
        # 整体校验已通过、任何状态改动（cfg.update/save_config）之前——失败 → 400
        # 且 runtime/config.json 零污染。语法校验已在逐键阶段做过（零副作用），
        # 这里只测「这个位置真的能用」——手打一个不存在的盘符/只读目录也会被拦住。
        if changes.get("export_dir"):
            problem = exportdir_mod.ensure_writable(changes["export_dir"])
            if problem:
                return JSONResponse({"ok": False, "error": problem},
                                    status_code=400)

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
            if "lan_enabled" in instant:
                # §7：**不许写「立即生效」**——监听地址在 uvicorn 启动时定死，重新绑定
                # 会换掉整条服务（桌面窗口/在飞字幕全断）。日志与 /api/lan-qr 的
                # restart_required/message 三处口径一致。
                want = bool(instant["lan_enabled"])
                log.info(f"[config] 手机同屏（lan_enabled={want}）已保存："
                         + ("重启 LectureLens 后开始监听 0.0.0.0（同 WiFi 持 token 可见）"
                            if want else
                            "重启 LectureLens 后停止 LAN 监听（回到仅本机 127.0.0.1）"))
            if "hf_mirror" in instant:
                # D2：同样**不许写「立即生效」**——HF_ENDPOINT 是 huggingface_hub 在
                # import 时读进常量的，进程内改不了。日志与 /api/models 的 mirror 字段
                # （进程内实际生效值）口径一致。
                log.info(f"[config] 模型下载镜像（hf_mirror={bool(instant['hf_mirror'])}）"
                         f"已保存：重启 LectureLens 后"
                         + ("改走 hf-mirror.com 镜像（国内网络加速）"
                            if instant["hf_mirror"] else "回到官方 huggingface.co"))

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


async def kick_ws_clients(code: int = 1008) -> int:
    """关闭并移除当前所有字幕 WS 连接（token 轮换后调用）。返回被断开的条数。

    为什么需要它（评审2④b，本机实测）：鉴权只在**握手那一刻**做一次
    （TokenAuthMiddleware），ws_endpoint 要等对端断开才退出，broadcast 对已连接对象
    不再校验——所以旧实现轮换 token 只挡住**新**连接，轮换前就开着页面的人会继续收到
    字幕。「旧设备立刻失效」这句文案在「截图外泄、对方当下正开着页面」这个主诉上是假的。
    现在轮换时主动 close：旧页面断线后重连（页面自带重连）会被 403 拒绝——「立刻失效」
    才成立。本机窗口也在被踢之列：它靠环回豁免 + 自己的 token 重连，无需人工干预。"""
    n = 0
    for ws in list(hub.clients):
        try:
            await ws.close(code=code)
            n += 1
        except Exception as e:      # 已断/半开也别让轮换失败
            log.debug(f"[security] 关闭 WS 时异常（忽略）: {e}")
    hub.clients.clear()
    hub.lan_clients.clear()
    return n


@app.post("/api/security/token/regenerate")
async def regenerate_token():
    """p1-6 设置页安全卡：重新生成 LAN token（旧值失效，已配对手机需重新粘贴）。
    新值仅本响应回传一次（等价首启 provision_token 的 print 信息流）；值不落日志。
    与「auth_token 不可经 API 设置」（DEFAULT_CONFIG 注释）不冲突：端点不接受任何
    输入值，随机性由服务端 secrets 保证——用户无法指定 token。写盘只增补 auth_token
    单键（provision_token 同款），不动 config.json 其他内容（防把 CLI 覆盖值顺带持久化）。

    评审2④b：轮换后**主动断开所有已连 WS**（kick_ws_clients）——否则已经在线的页面
    会继续收字幕，「旧设备立刻失效」不成立。响应里回断开的条数，让调用方/日志能核对。"""
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
    kicked = await kick_ws_clients()
    log.info(f"[security] LAN token 已重新生成（值不落日志；已断开 {kicked} 条在线连接，"
             "它们需用新 token 重连）")
    return {"ok": True, "token": token, "disconnected": kicked}


# —— §7（手机扫码同屏）：LAN 绑定事实 + 配对二维码 ——
# 事实源纪律：**配置意图**（config.lan_enabled）与**内核事实**（本次进程真正绑上的
# 地址）是两件事。监听地址在 uvicorn 启动那一刻定死、进程内换不了，所以「刚打开
# 开关」与「真的对外了」之间隔一次重启——端点必须把这两件事分开说，否则用户会拿
# 着扫不通的二维码在手机前干瞪眼。

def set_bind(host: str, port: int) -> None:
    """记下本次进程实际监听的 (host, port)。入口各调一次（main.main /
    desktop.start_server）——/api/lan-qr 判断「是否真的对外」的唯一依据。

    为什么不让端点自己去看 uvicorn 的 socket：监听 socket 只有 uvicorn 手里有，
    而请求 scope 里的 scope['server'] 是**这条连接**的本地地址（从环回发出的请求
    永远是 127.0.0.1），据此判断不出「是否也监听着 LAN」。故由启动方显式记一笔。"""
    runtime["bind"] = {"host": str(host), "port": int(port)}


def lan_bind_state() -> dict:
    """手机同屏的两个事实 + 它们的分歧怎么读：
    - enabled：config.lan_enabled（面板开关显示的那个值 = 用户意图）；
    - active：本次进程监听在非环回地址上（事实上对外了）；
    - restart_required：两者不一致 = 改了但还没重启（绑定换不了）；
    - exposed_while_disabled：**配置说关、服务还开着**（刚关开关还没重启，同网段
      此刻仍连得上）——面板必须显眼提示，不能显示成「已关闭」。"""
    cfg = runtime["config"]
    enabled = bool(cfg.get("lan_enabled"))
    bind = runtime.get("bind") or {}
    host, port = bind.get("host"), bind.get("port")
    active = lan_mod.is_lan_host(host)
    return {"enabled": enabled, "active": active,
            "restart_required": enabled != active,
            "exposed_while_disabled": bool(active and not enabled),
            "host": str(host) if host else None,
            "port": int(port) if port else None}


def lan_message(state: dict, ip: str | None, port, auth_enabled: bool) -> str:
    """状态行的人话（面板直接显示）。四种组合各说各的——**不许用一句「已开启」
    糊过去**：真正的失败模式是用户以为开了、手机却连不上。"""
    if state["exposed_while_disabled"]:
        return ("已关闭手机同屏（配置已保存）——但本次进程仍在监听 LAN，"
                "重启 LectureLens 后才真正停止对外")
    if state["enabled"] and not state["active"]:
        return "已开启手机同屏——重启 LectureLens 后生效（监听地址要重启才能换）"
    if not state["enabled"]:
        return "手机同屏未开启（开启后重启 LectureLens 生效）"
    if not ip:
        return "已开启，但没探测到局域网地址——检查电脑是否连上 WiFi/网线"
    if not port:
        return "已开启，但没读到监听端口——稍后刷新重试"
    if not auth_enabled:
        return ("手机与电脑连同一 WiFi，扫码即可同屏"
                "（注意：LAN 鉴权已关闭，同 WiFi 任何人都能看字幕）")
    return "手机与电脑连同一 WiFi，扫码即可同屏"


@app.get("/api/lan-qr")
async def api_lan_qr(req: Request):
    """§7：手机同屏的配对二维码（**含访问 token，仅本机 127.0.0.1 可读**）。

    为什么本端点自己再判一次对端：URL 里带着 token，那就是凭据；LAN 侧若能读这个
    端点，同网段任何人（auth_enabled=false 时中间件放行一切，更没门禁）都能把 token
    拿走。判据与 /api/export/open 同款——只看 socket 对端，不认任何请求头。

    返回：{ok, enabled, active, restart_required, exposed_while_disabled, host, port,
          ip, url, svg, auth_enabled, message}
    - url/svg **只在真的能用时**才非空（enabled 且 active 且探到可用的主网卡地址）：
      宁可显式说清「还差什么」，也不给一个扫了连不上的二维码。
    - auth_enabled=false 时 URL 里的 token 在服务端不作数（同 WiFi 谁都能看字幕），
      故一并回传，让面板把风险摆出来。
    """
    peer = req.client.host if req.client else None
    # 评审1① 修复：判据从「只判 socket 对端」升级为 loopback_only_ok（对端环回 **且**
    # Host 是本机/直连地址 **且** 没有跨源 Origin）——DNS-rebinding 页面正是从环回打进来
    # 的（peer 判据天然为真），原先光看 peer 会把 token 递给任意网页。同样的判据在
    # /api/export/open，以及全局 HostGuardMiddleware（覆盖整个 /api/* 与 /ws）。
    ok, why = loopback_only_ok(peer, req.headers.get("host"),
                              req.headers.get("origin"))
    if not ok:
        log.warning(f"[lan] ⚠ 拒绝读取配对二维码（仅本机）: {why}")
        return JSONResponse(
            {"ok": False,
             "error": f"仅本机（127.0.0.1）可以读取配对二维码（内含访问 token）：{why}"},
            status_code=403)

    cfg = runtime["config"]
    state = lan_bind_state()
    # §7⑤：token 为空（config.json 被清空/首启流程没跑到）→ 先补发再编二维码，
    # 否则扫出来的 URL 里没有凭据，手机连上也过不了鉴权（token 是唯一门禁）。
    token = cfg.get("auth_token")
    if not token:
        token = provision_token()
        if token:
            cfg["auth_token"] = token
    # 端口以**这条连接的实际监听端口**为准（scope['server'][1]，uvicorn 从内核
    # socket 读的），退而取启动时记下的值——--port 0 或「8787 被占换端口」都不说谎。
    port = (req.scope.get("server") or (None, None))[1] or state["port"]
    try:
        port = int(port) if port else None
    except (TypeError, ValueError):
        port = state["port"]
    auth_enabled = bool(cfg.get("auth_enabled", True))
    ip = lan_mod.primary_lan_ip() if (state["enabled"] and state["active"]) else None
    url = lan_mod.lan_url(ip, port, token) if (ip and port) else None
    svg = None
    if url:
        try:
            svg = lan_mod.qr_svg(url)
        except Exception as e:  # 缺依赖/编码异常：URL 照给，二维码如实说没有
            log.warning(f"[lan] ⚠ 二维码生成失败（URL 仍可用）: {e}")
            return {"ok": True, **state, "ip": ip, "port": port, "url": url,
                    "svg": None, "auth_enabled": auth_enabled,
                    "message": f"二维码生成失败（可手动打开上面的 URL）: {e}"}
    return {"ok": True, **state, "ip": ip, "port": port, "url": url, "svg": svg,
            "auth_enabled": auth_enabled,
            "message": lan_message(state, ip, port, auth_enabled)}


@app.post("/api/pause")
async def pause():
    status["paused"] = True
    return {"ok": True, "paused": True}


@app.post("/api/resume")
async def resume():
    status["paused"] = False
    return {"ok": True, "paused": False}


@app.post("/api/flush")
async def flush():
    """立即断句（首期反馈②）：把当前正在积累的这句提前结算并翻译，
    不用等静音停顿或分段上限。暂停中/采集未就绪时如实拒绝。"""
    if status.get("paused"):
        return JSONResponse({"ok": False, "error": "已暂停——先点「继续」再说"},
                            status_code=409)
    seg = runtime.get("segmenter")
    if seg is None or not hasattr(seg, "request_flush"):
        return JSONResponse({"ok": False, "error": "采集器未就绪"},
                            status_code=503)
    seg.request_flush()
    return {"ok": True}


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
async def notes_list(q: str = "", course: str = "", tag: str = ""):
    """p2-2 笔记库列表：日期/课程/标签/首句摘要/是否已有摘要与卡组 + 全文检索。

    q 为全文检索（**含中文子串**，也命中**标签名**，见 notes.index）；course 按课程名
    精确过滤（侧栏课程下钻）；tag（§8）按标签精确过滤——三者**叠加**。另带
    tag_counts = 全局标签清单 + 计数（只受 q/course 影响，供筛选栏显示"选它能看到几篇"）。
    数据源是 transcripts/ 里的真文件，无独立索引。"""
    data = notes_mod.index(TRANSCRIPTS, query=q, course=course, tag=tag)
    data["ok"] = True
    data["query"] = q
    data["current"] = hub.transcript_path.name if hub.transcript_path else None
    # A2：预设标签随列表一起下发（笔记库的「🏷 打标签」面板 chips 直接吃它）。
    # 为什么不写死在前端：预设是**服务端的**口径（将来可在设置页自定义，见 B3），
    # 前端只保存一份启动兜底；列表每次刷新都带来现值的，面板就不会显示过期 chips。
    data["presets"] = tag_presets()
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
        "tags": parsed["tags"],          # §8：预览页头部的标签 chips 用
        "is_current": bool(hub.transcript_path and hub.transcript_path.name == p.name),
        # §6：摘要面板与「覆盖确认」的判据（有过 -> 重新生成前弹确认框）；备份是否
        # 已存在也一并回，让「编辑摘要」的说明文案能如实显示回退去处。
        "has_summary": summary_path(p).is_file(),
        "has_summary_backup": summary_backup_path(p).is_file(),
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


def export_target_dir() -> Path | None:
    """当前生效的导出目录（config.export_dir）→ Path；未设置 → None。

    每次现读 runtime['config']：设置页改完立刻生效，不需要重启也不需要重建任何东西。
    值只经 /api/config 写入（专项校验：绝对路径 / 可建 / 可写 / 非系统关键目录），
    故这里可以放心 Path()；仍然只接受非空字符串（手改 config.json 写成空串 = 未设置）。"""
    raw = runtime["config"].get("export_dir")
    return Path(raw) if isinstance(raw, str) and raw.strip() else None


@app.get("/api/notes/export")
async def notes_export(fmt: str = "md", name: str = ""):
    """p2-3 导出中心：一篇笔记导出为 md / srt / pdf / docx。

    name 缺省 = 当前笔记；走 note_target（目录内 lecture_*.md 白名单，反越界）。
    **失败必须用户可见**：格式非法 400、笔记不存在/非法 404、生成异常 500 且响应体
    带 `ok:false + error`（前端用 fetch 读它并弹 toast，不是丢一个白页给用户）。

    §3 增：config.export_dir 已设置 → **直接落盘**到该目录，返回 JSON
    `{ok, path, name, dir, bytes}`（前端 toast「已导出到 …」+ 打开按钮）；未设置 →
    维持浏览器下载（响应形态与状态码一律不变）。两种模式共用同一套文件名 =
    `{课程}_{日期}.{ext}`（课程为空则维持原名），见 exportdir.export_filename。"""
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
    dest = export_target_dir()

    def _render() -> tuple[dict, bytes]:
        """读盘 + 解析 + 生成，全在**线程**里跑（见下方 to_thread 说明）。

        返回 (解析结果, 产物字节)：解析结果只用来取课程/时间戳算文件名——落盘模式
        与下载模式都要它，故两种模式共用同一次解析（不再各读一遍盘）。"""
        parsed = notes_mod.parse(p.read_text(encoding="utf-8"))
        pairs = parsed["pairs"]
        if key == "md":
            return parsed, p.read_bytes()
        sidecar = exporters.read_times(p)
        if key == "srt":
            return parsed, exporters.srt_bytes(pairs, sidecar)
        meta = export_meta(p, parsed)
        return parsed, (exporters.build_pdf(pairs, meta, sidecar) if key == "pdf"
                        else exporters.build_docx(pairs, meta, sidecar))

    try:
        # 评审1 修：导出是**同步 CPU/IO 重活**，直接在协程里跑会卡住整个事件循环。
        # 实测（105 段真实笔记）fmt=pdf 处理 661.6ms，同期事件循环最大空档 661.7ms
        # （对照：docx 124.7ms / srt 3.5ms）——即整段导出期间 WS 广播、asr_loop
        # 取段与广播、翻译任务全部停摆；外推 1500 段（约 2 小时课）build_pdf 约
        # 1.9s，恢复后积压还可能触发追赶丢弃而丢段。与其它重活（build_asr /
        # apply_segmenter / summarize / download）一致，一律 to_thread。
        parsed, body = await asyncio.to_thread(_render)
    except Exception as e:
        log.warning(f"[export] ⚠ {key} 导出失败（{p.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"{key} 导出失败: {type(e).__name__}: {e}"},
            status_code=500)
    out_name = exportdir_mod.export_filename(p, parsed.get("course"), ext,
                                             parsed.get("stamp"))

    if dest is not None:
        # —— §3 落盘模式：写进用户选定的目录（同名避让 -2/-3…，绝不覆盖）——
        # B1：课程非空 → 再落一层 `{课程名}/` 子目录（"对应的笔记本"的物理版：导出目录
        # 在 Obsidian/同步盘里就是按课分开的文件夹）。目录名用**同一个**
        # exportdir.clean_component 清洗（与文件名同一条规则，不会出现「文件名干净、
        # 目录名带非法字符」）；课程为空 / 「未分类」/ 整条名字清洗后为空串（如课程名
        # 全是非法字符）→ 仍落导出根目录（与 §3 原行为一致，不凭空造目录）。
        # 写盘用既有 write_export：它自己 mkdir(parents=True)（exportdir.py:363），
        # 子目录不存在/被删都会自动补建，与「导出位置被挪走仍可用」同一条路径。
        sub = (exportdir_mod.clean_component(parsed.get("course"))
               if not exportdir_mod.course_is_empty(parsed.get("course")) else "")
        target = (dest / sub) if sub else dest
        # 先算长度（评审④）：命中 260 上限时 `open/replace` 抛的是裸 OSError，而这里
        # 能给出「换目录/改名」的可行动文案，且是 400 而不是 500。算式要带子目录，
        # 否则「课程名很长 + 目录很深」正好卡在 260 上时这里放行、写盘时才炸。
        too_long = exportdir_mod.path_length_problem(target, out_name)
        if too_long:
            return JSONResponse({"ok": False, "error": too_long}, status_code=400)
        try:
            out = await asyncio.to_thread(exportdir_mod.write_export, target, out_name, body)
        except Exception as e:
            log.warning(f"[export] ⚠ 落盘失败（{target}）: {e}")
            return JSONResponse(
                {"ok": False,
                 "error": f"导出到 {target} 失败: {type(e).__name__}: {e}"
                          f"（可在设置页重新选择导出位置）"},
                status_code=500)
        log.info(f"[export] {key} 导出 {p.name} → {out}（{len(body)} 字节，直接落盘）")
        # dir 仍是**配置的导出目录本身**（= /api/export/open 打开的那个）：文件可能在其
        # 下的课程子目录里，真实落点在 path 里逐字给出，前端 toast 显示的也是 path。
        return JSONResponse({"ok": True, "fmt": key, "path": str(out),
                             "name": out.name, "dir": str(dest), "bytes": len(body)})

    if key == "md" and out_name == p.name:
        return FileResponse(p, filename=p.name, media_type=media)
    log.info(f"[export] {key} 导出 {p.name} → {out_name}（{len(body)} 字节）")
    return Response(content=body, media_type=media, headers={
        "Content-Disposition": export_disposition(out_name),
        "Content-Length": str(len(body)),
    })


def export_disposition(filename: str) -> str:
    """下载响应的 Content-Disposition 头值（§3 起文件名可能是中文课程名）。

    必要性：Starlette 把响应头按 **latin-1** 编码——`数学_20261013.pdf` 直接写进
    `filename="…"` 会抛 UnicodeEncodeError，整个导出变成 500（不是"名字不好看"，
    是功能直接坏掉）。做法与 starlette 的 FileResponse 一致：需要时补 RFC 5987 的
    `filename*=utf-8''<百分号编码>`。
    两段都写：`filename="<ASCII 兜底>"` 给只认老写法的客户端；浏览器按 RFC 6266
    优先取 `filename*`（前端 exportFilename 同口径优先解析它）。"""
    ascii_name = "".join(ch if (ch.isascii() and ch not in '"\\') else "_"
                         for ch in filename)
    if ascii_name == filename:
        return f'attachment; filename="{filename}"'
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=utf-8''{quote(filename)}")


#: A3 批量导出 zip：单次上限（再多的包既难用、也容易撞上 260 路径与浏览器下载体验）
NOTE_ZIP_MAX = 50
#: A3 包里只支持这两种格式（pdf/docx 打包没有实际意义：单篇仍走 §3 的单篇端点）
ZIP_FORMATS = {"md": "md", "srt": "srt"}


def zip_export_name(today: datetime.date | None = None) -> str:
    """批量导出 zip 的文件名：`笔记导出_YYYYMMDD.zip`（日期 = 导出当天）。

    纯函数（today 可注入）——文件名格式要能被测试钉住，而不是每次现取今天。"""
    return f"笔记导出_{(today or datetime.date.today()).strftime('%Y%m%d')}.zip"


def backup_zip_name(today: datetime.date | None = None) -> str:
    """B10 全库备份 zip 的文件名：`LectureLens备份_YYYYMMDD.zip`。

    与 `zip_export_name` 分开两个纯函数而不是共用一个：两者的**读者不同**——
    「笔记导出_」是"我挑的这几篇"，「LectureLens备份_」是"整库快照（换电脑/保险）"，
    名字本身就是这两种东西的标签，混用会让人分不清手里这个包是什么。"""
    return f"LectureLens备份_{(today or datetime.date.today()).strftime('%Y%m%d')}.zip"


def all_note_paths() -> list[Path]:
    """B10：库里**全部**笔记 → 路径列表（按名排序，前缀过滤）。

    枚举口径 = `notes.index` 同一套：transcripts/ 下**非递归** glob `lecture_*.md`
    + `notes.is_note_file`——于是回收站（`.trash/` 是子目录）与派生物
    （`*.summary.md` / `*.bak.md`）**结构上**就不可能进来，不靠这里记得过滤。
    **不套 NOTE_ZIP_MAX**：那是"一次挑几篇"的选包上限，整库备份的价值恰恰在于
    "一篇不落"（本仓实测 176 篇笔记，超过 50 的库多得是）。
    排序只为**确定性**（同一库每次打包条目顺序一致，测试与跨机比对都省事）；
    条目名最终由 export_filename 按课程/日期生成，不依赖这个顺序。"""
    d = Path(TRANSCRIPTS)
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob(notes_mod.NOTE_GLOB) if notes_mod.is_note_file(p)),
                  key=lambda p: p.name)


def zip_targets(raw_files) -> tuple[list[Path] | None, str | None]:
    """校验批量导出请求里的 files → (路径列表, 错误文案)。

    **整单校验**（与 §2 / A2 的批量端点同一条纪律）：任一篇非法/不存在 → 400 且
    不产出半包 zip——用户拿到一个"少了几篇"的压缩包，比看到报错糟得多（而且往往
    过一阵才发现）。名字一律走 notes.safe_note_path（库内 lecture_*.md 白名单 +
    反越界），与读/导出/删除/改归属/打标签同一条唯一入口。"""
    if raw_files is None:
        return None, "缺少 files（要打包导出的笔记名列表）"
    if not isinstance(raw_files, list) or not raw_files:
        return None, "files 不能为空"
    if len(raw_files) > NOTE_ZIP_MAX:
        return None, f"一次最多打包 {NOTE_ZIP_MAX} 篇（收到 {len(raw_files)}）"
    paths: list[Path] = []
    seen: set = set()
    for f in raw_files:
        p = notes_mod.safe_note_path(TRANSCRIPTS, f)
        if p is None:
            return None, f"非法笔记名: {f!r}（只接受 transcripts/ 下的 lecture_*.md）"
        if not p.is_file():
            return None, f"笔记不存在: {f}"
        if p in seen:                    # 重复列同一篇：去重（幂等，包里不会有重复条目）
            continue
        seen.add(p)
        paths.append(p)
    return paths, None


@app.post("/api/notes/export/batch")
async def notes_export_batch(req: Request):
    """A3 批量导出 / B10 全库备份：{files[], fmt: md|srt} 或 {all: true, fmt} →
    打成**一个 zip**（标准库 zipfile，零新依赖）。

    - `all: true`（B10 一键打包备份）= 服务端自己枚举**库里全部**笔记
      （`all_note_paths`：与 notes.index 同款非递归 glob + is_note_file，故 .trash 与
      派生物天然不在内），**不受 NOTE_ZIP_MAX=50 约束**（那是挑几篇的选包上限）；
      与 `files` **不能同时给**（同时给 = 意图不明，直接 400，绝不猜）；
    - 未给 `all` 时 = 既有 A3 语义（必须给 files，50 篇封顶），一字未变；
    - export_dir 已设 → zip 直接落盘（`笔记导出_日期.zip` / `LectureLens备份_日期.zip`，
      写盘走 exportdir.write_export = 原子抢占 + os.replace，同名自动 `-2`：与单篇导出
      同一条并发安全口径）；未设 → 内存 zip 经下载返回（同一个 export_disposition）。
    两种模式**都不套课程子目录**（B1 只针对单篇落盘）：一个 zip 里常常是好几门课，
    按课分文件夹无处安放；包里条目名本身已经是 `{课程}_{日期}.ext`，解压即按需归类。
    校验整单做（见 zip_targets）——要么整包、要么明确报错，绝不给"少几篇"的包。
    """
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("files", "fmt", "all")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['files', 'fmt', 'all']）"},
            status_code=400)
    key = str(body.get("fmt") or "md").strip().lower()
    if key not in ZIP_FORMATS:
        return JSONResponse(
            {"ok": False,
             "error": f"批量导出只支持 md / srt（收到 fmt={body.get('fmt')!r}）"},
            status_code=400)
    # B10：all 是**显式**开关（布尔）。缺省 / false → 走既有 files 路径，
    # 「缺 files」的 400 语义原样保留（客户端忘了带 all 不会静默变成全库备份）。
    want_all = body.get("all", False)
    if not isinstance(want_all, bool):
        return JSONResponse(
            {"ok": False, "error": f"非法 all: {want_all!r}（须为布尔 true/false）"},
            status_code=400)
    if want_all and body.get("files"):
        return JSONResponse(
            {"ok": False, "error": "all 与 files 不能同时给（all=true 即导出库里全部笔记）"},
            status_code=400)
    if want_all:
        paths = all_note_paths()
        if not paths:
            return JSONResponse(
                {"ok": False, "error": "库里还没有笔记（transcripts/ 下没有 lecture_*.md）"},
                status_code=400)
        zip_name = backup_zip_name()
        label = "备份全部"
    else:
        paths, err = zip_targets(body.get("files"))
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        zip_name = zip_export_name()
        label = "批量导出"
    ext = ZIP_FORMATS[key]

    def _build() -> bytes:
        """读盘 + 解析 + 打包，全在线程里跑（与单篇导出同款：同步重活不堵事件循环）。"""
        buf = io.BytesIO()
        used: set = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                # **字节级读**（评审2 ① 实测的坑）：read_text 走通用换行，会把 CRLF 笔记
                # 静默翻成 LF——同一篇笔记单篇导出（走 read_bytes，逐字节保真）与批量 zip
                # 会给出不同字节，而本仓真实笔记 176 篇里 174 篇是全 CRLF。md 载荷因此
                # 直接用原字节；解析只借用解码后的文本算条目名/取段对，不当载荷来源。
                raw = p.read_bytes()
                parsed = notes_mod.parse(raw.decode("utf-8"))
                if key == "md":
                    data = raw
                else:
                    data = exporters.srt_bytes(parsed["pairs"],
                                               exporters.read_times(p))
                # 条目名 = 单篇导出的同名文件（`{课程}_{日期}.ext`；无课程归属则原名）。
                # 结构上不可能 zip-slip：clean_component 已把路径分隔符替换成 `-`，
                # 无课程时回落的是笔记自己的 stem（同为白名单内的名字）。
                entry = exportdir_mod.export_filename(p, parsed.get("course"), ext,
                                                      parsed.get("stamp"))
                zf.writestr(exportdir_mod.dedupe_names(entry, used), data)
        return buf.getvalue()

    try:
        blob = await asyncio.to_thread(_build)
    except Exception as e:
        log.warning(f"[export] ⚠ {label}打包失败（{len(paths)} 篇）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"批量导出失败: {type(e).__name__}: {e}"},
            status_code=500)
    # zip_name 已在上面的分支里定好（all 模式 = LectureLens备份_日期.zip；否则 = 笔记导出_日期.zip），
    # 这里**不要**再赋一次：早期版本的 `zip_name = zip_export_name()` 会把备份名盖掉。
    dest = export_target_dir()
    if dest is not None:
        too_long = exportdir_mod.path_length_problem(dest, zip_name)
        if too_long:
            return JSONResponse({"ok": False, "error": too_long}, status_code=400)
        try:
            out = await asyncio.to_thread(exportdir_mod.write_export, dest, zip_name, blob)
        except Exception as e:
            log.warning(f"[export] ⚠ 批量落盘失败（{dest}）: {e}")
            return JSONResponse(
                {"ok": False,
                 "error": f"导出到 {dest} 失败: {type(e).__name__}: {e}"
                          f"（可在设置页重新选择导出位置）"},
                status_code=500)
        log.info(f"[export] {label} {len(paths)} 篇 → {out}"
                 f"（{len(blob)} 字节，直接落盘）")
        return JSONResponse({"ok": True, "fmt": key, "all": want_all, "path": str(out),
                             "name": out.name, "dir": str(dest),
                             "count": len(paths), "bytes": len(blob)})
    # 未设导出目录 → 直接回内存里的 zip 字节（附件下载，文件名同单篇导出口径）。
    # 评审2 ② 实测后**刻意不用临时文件 + FileResponse**：starlette 的 FileResponse
    # 把 background 回调放在各个 send 分支之后、没有 try/finally——客户端中途断开
    # （手机切换网络、浏览器取消下载、ASGI 的 send 抛 ClientDisconnected）时清理被跳过，
    # %TEMP% 里会留下一整包 zip；而 blob 本来就在内存里（十几 MB 级别的上限也有 50 篇
    # 封顶兜着；B10 的全库备份没有封顶，但"全部笔记的 Markdown"本就是这个量级），
    # 少一个临时文件就少一类垃圾，也不再有"谁负责删"的问题。
    log.info(f"[export] {label} {len(paths)} 篇 → {zip_name}"
             f"（{len(blob)} 字节，浏览器下载）")
    return Response(content=blob, media_type="application/zip",
                    headers={"Content-Disposition": export_disposition(zip_name)})


@app.post("/api/export/open")
async def export_open(req: Request):
    """§3：在文件管理器里打开导出目录（设置页与导出 toast 的「打开」按钮）。

    **仅本机（环回）可用**：os.startfile 是在**运行服务的这台机器**上弹窗口，LAN/
    手机侧调用等于在别人的电脑上开窗；且 auth_enabled=false 时鉴权中间件全放行
    （回退开关的既定语义），端点必须自己再判一次对端——与 §7 /api/lan-qr「仅
    loopback 可访问」同一纪律。
    路径只取 config.export_dir：端点**不接受任何请求字段**（非空体一律 400），
    因此不存在「用参数打开任意路径/执行任意命令」的面。"""
    peer = req.client.host if req.client else None
    # 评审1① 修复：同 /api/lan-qr——只判 socket 对端不够（rebinding 页面就是从环回打
    # 进来的），必须连带判 Host/Origin，否则任意网页能让本机反复弹文件夹窗口并读出
    # export_dir 路径。
    ok, why = loopback_only_ok(peer, req.headers.get("host"),
                              req.headers.get("origin"))
    if not ok:
        log.warning(f"[export] ⚠ 拒绝打开导出目录（仅本机）: {why}")
        return JSONResponse(
            {"ok": False, "error": f"仅本机（127.0.0.1）可以打开导出文件夹：{why}"},
            status_code=403)
    body = await optional_json(req)
    if body:
        return JSONResponse(
            {"ok": False,
             "error": f"未知字段: {sorted(body)}（本端点不接受参数，"
                      f"打开的目录固定为 config.export_dir）"},
            status_code=400)
    dest = export_target_dir()
    if dest is None:
        return JSONResponse(
            {"ok": False, "error": "未设置导出位置（当前为浏览器下载模式）"},
            status_code=409)
    if not dest.is_dir():
        return JSONResponse(
            {"ok": False,
             "error": f"导出目录不存在: {dest}（可在设置页重新选择）"},
            status_code=409)
    problem = exportdir_mod.open_folder(dest)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=500)
    log.info(f"[export] 已在文件管理器打开导出目录: {dest}")
    return {"ok": True, "path": str(dest)}


# —— §6（可编辑摘要）：摘要原文的读/写 + 覆盖前自动备份 ——

def summary_path(p: Path) -> Path:
    """笔记对应的摘要路径（`lecture_X.md` → `lecture_X.summary.md`）。

    后缀的单一来源 = notes.SUMMARY_SUFFIX（列表徽章、本模块的读写端点、summarize
    的落盘三处共用同一常量）——同一条文件名规则在多处各拼一遍，改一处必漏一处。"""
    p = Path(p)
    return p.with_name(p.stem + notes_mod.SUMMARY_SUFFIX)


def summary_backup_path(p: Path) -> Path:
    """摘要的备份路径（`lecture_X.summary.bak.md`）。

    §6：重新生成摘要前把上一版（可能是人工修订过的）存这里，覆盖不再静默丢内容。
    备份的**实写**在 summarize.backup_existing（覆盖前那一刻），本函数只算路径。"""
    p = Path(p)
    return p.with_name(p.stem + notes_mod.SUMMARY_BACKUP_SUFFIX)


@app.get("/api/notes/summary")
async def notes_summary_get(name: str = ""):
    """§6：读一篇笔记的**摘要原文**（预览页「编辑摘要」用）。

    name = 笔记名（`lecture_*.md`，走 note_target 白名单；缺省 = 当前笔记，与
    POST /api/notes/summarize 同口径）。摘要路径**由笔记名派生**，不接受「直接
    指定某个 .summary.md」这类入参——派生的路径空间只有一格，比再开一张名字白名单
    更难漏（派生物文件名一律不可直接寻址，见 notes.safe_note_path 的既有纪律）。
    没有摘要 → 200 + exists:false（**不是错误**：预览页据此显示「还没生成摘要」）。"""
    p, err = note_target(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    sp = summary_path(p)
    bak = summary_backup_path(p)
    if not sp.is_file():
        return {"ok": True, "exists": False, "name": p.name, "summary": sp.name,
                "text": "", "sha": None, "size": 0, "mtime_us": None,
                "backup": bak.name, "has_backup": bak.is_file()}
    try:
        text = sp.read_text(encoding="utf-8")
        st = sp.stat()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"读取摘要失败: {type(e).__name__}: {e}"},
            status_code=500)
    return {"ok": True, "exists": True, "name": p.name, "summary": sp.name,
            "text": text, "sha": text_sha(text), "size": st.st_size,
            "mtime_us": st.st_mtime_ns // 1000,
            "backup": bak.name, "has_backup": bak.is_file()}


@app.put("/api/notes/summary")
async def notes_summary_save(req: Request):
    """§6：把人工修订后的摘要原文就地写回（**整篇替换**）。

    与 §4 笔记编辑同款纪律：
    - name 必填且走 note_path_arg（写操作不接受缺省落到当前笔记）；
    - base_sha（= GET 回的摘要指纹）必填，与当前不一致 → 409「摘要已被更新」——
      防「编辑面板开着、期间又点了一次生成摘要」把新版冲掉；
    - 原子替换 + 整篇保存统一按本平台行尾（同 notes_save 的口径）。
    摘要不存在 → 404（**先「摘要」生成再编辑**：不接受凭空创建——那会让用户以为
    自己写的是 AI 摘要，实际只是往派生位写了个任意文件）。
    「保存后重索引」在本项目天然成立（列表/索引每次现读现算）：这里只广播 notes，
    让别的窗口刷新预览。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("name", "text", "base_sha")]
    if unknown:
        return JSONResponse(
            {"ok": False,
             "error": f"未知字段: {unknown[0]}（允许: ['name', 'text', 'base_sha']）"},
            status_code=400)
    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "text 必须是字符串（摘要 md 原文）"},
                            status_code=400)
    if len(text.encode("utf-8")) > NOTE_EDIT_MAX:
        return JSONResponse(
            {"ok": False, "error": f"摘要文本过大（上限 {NOTE_EDIT_MAX // 1024} KB）"},
            status_code=400)
    base = body.get("base_sha")
    if not isinstance(base, str) or not base.strip():
        return JSONResponse(
            {"ok": False,
             "error": "缺少 base_sha：先 GET /api/notes/summary 取 sha 再保存"
                      "（防止覆盖编辑期间重新生成的摘要）"},
            status_code=400)
    # §6 评审③：**生成中不许改**。summarize 的收尾会把「当时盘上的那份」当旧版备份、
    # 再用 AI 版覆盖正文——用户在生成期间保存的修订会立刻缩进 .bak.md，而界面上刚
    # 显示过「保存成功」（看得见的现象是「我一保存正文就变了」）。与 POST 入口同一道
    # 守卫；等生成完成再改即可（那时 base_sha 也已随新正文刷新）。
    if runtime["summarizing"]:
        return JSONResponse(
            {"ok": False,
             "error": "摘要正在生成中，暂不能编辑——生成结束（正文落盘）后再改，"
                      "届时上一版会自动备份为 *.summary.bak.md"},
            status_code=409)
    p, err = note_path_arg(body.get("name"))
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    sp = summary_path(p)
    if not sp.is_file():
        return JSONResponse(
            {"ok": False,
             "error": f"这篇还没有摘要（{sp.name}）——先点「摘要」生成，再来编辑"},
            status_code=404)
    try:
        # 以下同步执行（读 stat → 写 .tmp → replace），中间无 await。
        if text_sha(sp.read_text(encoding="utf-8")) != base.strip():
            return JSONResponse(
                {"ok": False,
                 "error": "摘要在编辑期间已被更新（可能刚重新生成过）——"
                          "请重新打开再编辑，避免覆盖新版"},
                status_code=409)
        # newline=None：整篇保存统一按本平台行尾（与 notes_save 同口径；浏览器
        # textarea 的 value 被 DOM 规范成 \n，逐字节回放 CRLF 做不到）。
        note_atomic_write(sp, text, newline=None)
        st2 = sp.stat()
    except Exception as e:
        log.warning(f"[summary] ⚠ 摘要保存失败（{sp.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"保存失败（原文件已尽量保持原样）: "
                                   f"{type(e).__name__}: {e}"},
            status_code=500)
    log.info(f"[summary] 人工修订已保存 {sp.name}（{st2.st_size} 字节）")
    await broadcast_notes([p.name])
    return {"ok": True, "name": p.name, "summary": sp.name, "size": st2.st_size,
            "sha": text_sha(text), "mtime_us": st2.st_mtime_ns // 1000}


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
    out_path = summary_path(tpath)
    # §6：已有摘要 = 本次是**覆盖**。把这件事如实回给前端（确认框/提示文案用它），
    # 覆盖动作本身在 summarize.backup_existing 里先备份为 *.summary.bak.md——
    # 「人工修订版不被静默冲掉」由那一层保证，不依赖前端有没有弹确认框。
    overwrite = out_path.is_file()
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
                                 "failed": 0, "path": str(out_path), "error": None,
                                 # §6：备份文件名（覆盖才非 None，落盘时才填）
                                 "backup": summary_backup_path(tpath).name
                                           if overwrite else None}
            result = await TranscriptSummarizer(tr).summarize(
                tpath, out_path, on_progress=_progress)
            status["summary"].update(state="done", failed=len(result["failed"]),
                                     error=None,
                                     backup=(Path(result["backup"]).name
                                             if result.get("backup") else None))
            await hub.broadcast(_snapshot())
            log.info(f"[summary] ✅ 完成: {out_path.name}"
                     f"（{result['total']} 块，失败 {len(result['failed'])}"
                     f"{'，上一版已备份为 ' + result['backup'] if result.get('backup') else ''}）")
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
            "output": out_path.name,
            # §6：前端据此提示「已覆盖（先备份为 X）」；未覆盖时为 false/None
            "overwrite": overwrite,
            "backup": summary_backup_path(tpath).name if overwrite else None}


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
    spath = summary_path(tpath)          # §6：派生路径走同一处口径
    if not spath.is_file():
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
            result = await build_deck(tr, spath, apkg_path, title,
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


def course_terms(course: dict) -> tuple[int, bool]:
    """该课绑定术语表的词条数 → (条数, 术语表是否缺失)。

    B14「课程管理」每行要显示「N 篇笔记 · M 条术语」，M 由此处算：口径与术语页
    **同源**（glossary.read_file + count_entries = 同一份解析器），故「卡上写的条数」
    与「术语页看到的条目数」不会出现两套算法。

    读盘前先过 courses.glossary_problem 的**根锚定校验**（写路径同款白名单）：
    courses.json 是用户可手改的文件，统计读盘也绝不跟着越出项目根 / 读非 .md。
    未绑定或校验不过 → (0, False) 且**一个字节都不读**；绑了但文件不在 →
    (0, True)，由 UI 如实说「术语表未找到」而不是谎报 0 条。"""
    rel = course.get("glossary")
    if not rel or courses_mod.glossary_problem(ROOT, rel):
        return 0, False
    p = Path(courses_mod.resolve_glossary(ROOT, rel) or "")
    if not p.is_file():
        return 0, True
    try:
        return glossary_mod.count_entries(glossary_mod.read_file(p)["lines"]), False
    except Exception:                       # 统计只是展示信息，不该让课程接口 500
        return 0, False


def courses_view(data: dict | None = None, data_error: str | None = None) -> dict:
    """课程表视图：课程列表（含每课笔记数 + 术语条数）+ 当前课程 + 未分类名 + 调色板。

    笔记数由扫描 transcripts/*.md 头部得出（courses.count_notes）——**单一事实
    来源 = 笔记文件本身**，不额外维护 sidecar 索引，故不存在头/索引双写不一致。
    术语条数见 course_terms（同样是「读真文件」而不是另记一份索引）。"""
    if data is None:
        data, data_error = courses_mod.load_checked(COURSES_PATH)
    counts = courses_mod.count_notes(TRANSCRIPTS)
    items = []
    for c in data["courses"]:
        terms, gl_missing = course_terms(c)
        items.append(dict(c, notes=counts.get(c["name"], 0), terms=terms,
                          glossary_missing=gl_missing))
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
    **该课历史笔记的归属行一并改到「未分类」**；笔记文件与正文一律保留，绝不删数据。

    B14：为什么删课程要顺带回溯历史笔记（旧行为只重标「当前」那一篇）——
    设置页课程卡的删除确认按交付口径明确写着「N 篇笔记将归入未分类」，而旧实现
    只处理 active 课程的那一篇：其余笔记会在库里永远挂着一个**已经不存在的课名**
    （课程筛选栏里还会冒出这个幽灵项），文案就成了假话。这里复用 §2 已有的
    courses.rename_in_notes（只替换头部那一行，CRLF/正文/切换标记零改动）。"""
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
        tag = hub.retag_course(None)          # 正在记录的那篇：沿用既有 empty/marked 分流
        status["course"] = courses_mod.UNCATEGORIZED
        apply_course_glossary(None)
    # 历史笔记（含正在记录的那篇若头部仍写着该课名）→ 头部归属行改「未分类」。
    # 放在 retag 之后：先让「记录中」那篇按既有语义追加切换标记，再把残留的旧课名
    # 统一收口，两条路径不重叠、也不互相覆盖（rename_in_notes 只动头部那一行）。
    retagged = courses_mod.rename_in_notes(TRANSCRIPTS, course["name"],
                                           courses_mod.UNCATEGORIZED)
    log.info(f"[courses] 删除课程 {cid}（{course['name']}）"
             f"{'· 原当前课程（记录中笔记：' + tag + '）' if was_active else ''}"
             f" → {retagged} 篇笔记归入未分类")
    await broadcast_courses()
    return {"ok": True, "deleted": cid, "was_active": was_active,
            "retagged": retagged, "uncategorized": courses_mod.UNCATEGORIZED}


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


# —— §2（课后改归属）：笔记头的课程行**定稿改写**（单篇 / 批量）——
# 与课中切课语义分开：Hub.retag_course = 追加「切换标记」（保留"当时切过"的事实，
# 主归属仍是开课那门课）；这里 = **整篇归属更正**（改头，不新增标记行）——下课后
# 发现归错类就走这条。核心改写是 notes.set_course（纯函数，与解析侧同窗口）。
# 位置说明：本段属笔记族，但校验要用课程表的 id/名解析（courses.resolve_target）
# 与 course_body，故紧挨课程段放，依赖顺序一眼可见。


#: 单次批量改归属的文件数上限（防一次请求扫爆目录；笔记库单页远小于此数）
NOTE_MOVE_MAX = 200


def note_move_targets(body: dict, raw_files) -> tuple[list[Path] | None, str | None, str | None, int]:
    """校验改归属请求体 → (路径列表, 目标课程名, 错误文案, 错误状态码)。

    目标课程名在这里一并解析好回传：端点在"校验"与"写入"之间**不再二次 load
    课程表**（两次读之间课程恰被另一窗口删掉的话，第二次解析会得到空名——那会把
    字面量 None 写进笔记头。一次解析、一次使用，就没有这条缝）。

    **先全部校验、后动手**（与 /api/courses 的「先校验后落盘」同款纪律）：
    任何一篇非法/不存在、或课程不存在 → 整单拒绝，绝不半批写入（否则用户看到
    「移动成功」而一半笔记还挂在旧课程上）。

    状态码两种：400 = 请求本身的问题（缺字段/非法名/不存在/课程不存在，计划口径）；
    409 = 课程表损坏（与 /api/courses* 的 courses_corrupt 同一口径——写盘会把坏表
    当空表覆盖，所以本轮操作直接拒绝）。

    文件路径一律走 notes.safe_note_path（目录内 lecture_*.md 白名单 + 反越界），
    与读/导出/删除同一条唯一入口——改归属会**写**这些文件，更不能例外。"""
    unknown = [k for k in body if k not in ("file", "files", "course")]
    if unknown:
        return None, None, f"未知字段: {unknown[0]}（允许: ['file', 'files', 'course']）", 400
    if raw_files is None:
        return None, None, "缺少 file（单篇）或 files（批量）", 400
    files = raw_files if isinstance(raw_files, list) else [raw_files]
    if not files:
        return None, None, "files 不能为空", 400
    if len(files) > NOTE_MOVE_MAX:
        return None, None, f"一次最多改 {NOTE_MOVE_MAX} 篇（收到 {len(files)}）", 400
    data, derr = courses_mod.load_checked(COURSES_PATH)
    if derr:
        return None, None, COURSES_CORRUPT_HINT, 409
    name, err = courses_mod.resolve_target(data, body.get("course"))
    if err:
        return None, None, err, 400
    paths: list[Path] = []
    seen: set = set()
    for f in files:
        p = notes_mod.safe_note_path(TRANSCRIPTS, f)
        if p is None:
            return None, None, f"非法笔记名: {f!r}（只接受 transcripts/ 下的 lecture_*.md）", 400
        if not p.is_file():
            return None, None, f"笔记不存在: {f}", 400
        if p in seen:              # 同一次请求里重复列同一篇：去重（幂等，不报错）
            continue
        seen.add(p)
        paths.append(p)
    return paths, name, None, 200


def note_course_write(p: Path, name: str) -> str:
    """把一篇笔记的课程行改成 name → 动作 replaced / inserted / none。

    **改动必须原子且不与字幕追加打架**：读全文 → 变换 → 写同目录 .tmp → os.replace
    （同 courses.save 的原子替换；写一半断电也不会留下截断的笔记）。进程内**没有
    任何 await**：笔记的其它写者（Hub.append / retag_course / rename_in_notes）都在
    事件循环线程里同步执行，事件循环不会在"读"与"写"之间切走，故不存在与追加
    并发而丢字幕段的情况（这是本函数刻意保持同步的原因，勿改成 to_thread）。"""
    text = read_note_text(p)
    new, action = notes_mod.set_course(text, name)
    if new == text:
        return "none"              # 已是该归属：不动文件（mtime 也不变，列表不用白刷）
    # 必须用保真写（newline=""）：读进来的文本带着 \r\n，再走文本模式的 os.linesep
    # 展开会把每个 \n 变成 \r\n，得到 \r\r\n（读/写模式必须成对，实测踩过）。
    note_atomic_write(p, new)
    return action


def note_course_bad(msg: str, status: int = 400) -> JSONResponse:
    """§2 统一错误响应：400 = 文件/课程问题（计划口径的明确文案）；409 = 课程表损坏。"""
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


async def broadcast_notes(files: list[str]) -> None:
    """§2：笔记归属变了 → 广播（另一窗口的笔记库据此刷新，不必等下次手动刷新）。"""
    with contextlib.suppress(Exception):
        await hub.broadcast({"type": "notes", "files": list(files)})


@app.post("/api/notes/course")
async def set_note_course(req: Request):
    """§2 课后改归属（单篇）：{file, course} → 改写该篇笔记头的课程行。

    course = 课程 id / 课程名 / null（未分类）。返回 action：replaced（改头）/
    inserted（老笔记头部没有课程行 → 插入）/ none（本来就是该归属）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    paths, name, err, status = note_move_targets(body, body.get("file"))
    if err:
        return note_course_bad(err, status)
    p = paths[0]
    try:
        action = note_course_write(p, name)
    except Exception as e:
        log.warning(f"[notes] ⚠ 改归属失败（{p.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"改写失败（原文件未改动）: {type(e).__name__}: {e}"},
            status_code=500)
    log.info(f"[notes] 课程归属 {p.name} → {name}（{action}）")
    await broadcast_notes([p.name])
    return {"ok": True, "file": p.name, "course": name, "action": action}


@app.post("/api/notes/course/batch")
async def set_notes_course_batch(req: Request):
    """§2 课后改归属（批量）：{files[], course} → 逐篇改写笔记头的课程行。

    校验是整单口径（见 note_move_targets）；写入阶段的单篇 IO 失败逐条记进
    `failed` 并继续后续篇目——返回 200 + 明细，让界面能如实说「N 篇成功、M 篇失败」，
    而不是一个 500 把已成功的部分也藏起来。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    paths, name, err, status = note_move_targets(body, body.get("files"))
    if err:
        return note_course_bad(err, status)
    results: list[dict] = []
    failed: list[dict] = []
    for p in paths:
        try:
            results.append({"file": p.name, "action": note_course_write(p, name)})
        except Exception as e:
            log.warning(f"[notes] ⚠ 批量改归属失败（{p.name}）: {e}")
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    log.info(f"[notes] 批量课程归属 → {name}（成功 {len(results)}，失败 {len(failed)}）")
    if results:
        await broadcast_notes([r["file"] for r in results])
    return {"ok": True, "course": name, "moved": len(results),
            "results": results, "failed": failed}


# —— §4（笔记管理）：软删除（回收站）/ 编辑 / 重命名 / 批量删除 ——
# 安全口径与读端点同一条：所有名字先过 notes.safe_note_path（目录内笔记白名单 +
# 反越界），回收站操作另加一层目录包含性校验。回收站 = transcripts/.trash/
# （**同盘 rename**，秒级、不占额外空间、随时可还原）；索引与课程统计都用非递归
# glob，天然看不到它。**回收站里的文件无法用普通名字访问**：它的名字只能写成
# `.trash/xxx.md`，第一步就被 safe_note_path 的「不得含 / 」挡掉——「除回收站界面
# 操作外不可再被打开/编辑」是结构保证，不是靠端点各自记得判断。


#: 编辑保存的正文大小上限（2MB；实测 105 段真实笔记约 30KB，留足余量）
NOTE_EDIT_MAX = 2 * 1024 * 1024
#: 回收站的绝对路径（每次现算，测试里 TRANSCRIPTS 会被重定向）
TRASH_DIR_NAME = notes_mod.TRASH_DIR


def trash_dir() -> Path:
    return Path(TRANSCRIPTS) / TRASH_DIR_NAME


def note_path_arg(raw) -> tuple[Path | None, str | None]:
    """解析**库内**笔记名 → (路径, 错误文案)。

    与 note_target 的区别：写操作必须明确点哪一篇，**不接受缺省回落到当前笔记**
    （静默回落会把编辑/删除作用到用户没指定的那篇上，比报错糟）。"""
    p = notes_mod.safe_note_path(TRANSCRIPTS, raw)
    if p is None:
        return None, f"非法笔记名: {raw!r}（只接受 transcripts/ 下的笔记文件）"
    if not p.is_file():
        return None, f"笔记不存在: {raw}"
    return p, None


def trash_path_arg(raw) -> tuple[Path | None, str | None]:
    """解析**回收站内**笔记名 → (路径, 错误文案)。名字仍要过名字规则，路径再核对
    父目录确实是 .trash（名字规则已挡掉分隔符，这里是第二道）。"""
    problem = notes_mod.name_problem(raw)
    if problem:
        return None, f"非法笔记名: {raw!r}（{problem}）"
    d = trash_dir()
    cand = (d / raw).resolve()
    if cand.parent != d.resolve():
        return None, f"非法笔记名: {raw!r}（越出回收站目录）"
    if not cand.is_file():
        return None, f"回收站里没有这篇: {raw}"
    return cand, None


def note_batch_arg(body: dict, raw) -> tuple[list[Path] | None, str | None]:
    """{files: [...]} 批量入参的统一校验（**先全部校验、再动手**：任何一篇非法/
    不存在 → 整单拒绝，绝不半批执行——批量删一半比报错糟得多）。"""
    unknown = [k for k in body if k not in ("files", "course")]
    if unknown:
        return None, f"未知字段: {unknown[0]}（允许: ['files', 'course']）"
    if not isinstance(raw, list) or not raw:
        return None, "files 必须是非空数组"
    if len(raw) > NOTE_MOVE_MAX:
        return None, f"一次最多 {NOTE_MOVE_MAX} 篇（收到 {len(raw)}）"
    paths: list[Path] = []
    seen: set = set()
    for f in raw:
        p, err = note_path_arg(f)
        if err:
            return None, err
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)
    return paths, None


def read_note_text(p: Path) -> str:
    """读笔记正文，**不转换行尾**（newline=''）。

    评审 M4 实测：read_text() 的通用换行会把 \\r\\n 归一成 \\n，写回时文本模式又按
    os.linesep 展开——于是一篇手工维护成 LF 的笔记，只要改一次归属/标签就整篇翻成
    CRLF（git diff 全红），而 notes.set_course/set_tags 里「按原行尾保留」的分支在
    端点路径上永远走不到。行级改写（课程行/标签行）必须用这个入口读。"""
    with p.open(encoding="utf-8", newline="") as f:
        return f.read()


def note_atomic_write(p: Path, text: str, newline: str | None = "") -> None:
    """把整篇正文原子落盘（写同目录 .tmp → os.replace）。

    `newline=""`（默认）= **逐字节原样落盘**：行级改写（课程行/标签行）走它，文件
    里是什么行尾就保持什么行尾，不引入混排也不静默翻转。

    `newline=None` = 按平台行尾展开（os.linesep）：**编辑端点**专用——浏览器
    textarea 的 value 由 DOM 规范化成 \\n，无法逐字节回放 CRLF，故那边显式声明
    「保存整篇 = 统一按本平台行尾」（见 notes_save 的说明）。两处口径不同是有意的：
    行级改写保真、整篇保存归一。

    **进程内没有任何 await**（调用方必须在最后一步同步调用它），故不会与正在记录
    的字幕追加交错。"""
    # 临时名带 pid + 随机段（评审④低）：同一份文件也被 summarize.atomic_write_text
    # 写（摘要生成），两边若都用「<名>.tmp」就会共用一个临时文件——当前两边都在事件
    # 循环里、读写之间无 await 因而不会真交错，但任一侧挪进线程池就会互踩（第②条
    # 那类混合产物的复现路径）。名字不唯一 = 迟早踩，故两边都改成唯一名。
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        tmp.replace(p)
    except Exception:
        with contextlib.suppress(Exception):
            tmp.unlink()
        raise


def text_sha(text: str) -> str:
    """正文指纹（乐观并发凭据）= sha1(解码后的文本)。

    用**文本**而非字节：Windows 上写文件会把 \n 落成 \r\n，逐字节比对会把
    "同一篇笔记"误判成"有人改过"；解码后比较才是用户语义里的同一篇。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def live_restart_after_delete(deleted: list[Path]) -> bool:
    """删掉"正在记录的那篇"之后立刻接力一篇新笔记 → 是否真的接力了。

    必要性：删（软删）只是把文件移进回收站，而录制侧 hub.transcript_path 仍指着
    老路径——下一条字幕 append 会把**回收站里那个文件名**在原目录重新创建出来
    （split-brain：一边回收站里有笔记，一边又冒出个同名新文件接着写）。接力一篇
    新笔记（与启动时的行为一致，沿用当前课程），字幕继续有地方落，回收站内容也
    不会被后续写入污染。"""
    cur = hub.transcript_path
    if not cur or not any(p == cur for p in deleted):
        return False
    name = status.get("course")
    hub.start_transcript(None if name in (None, courses_mod.UNCATEGORIZED) else name,
                         avoid={cur.name})   # 连刚进回收站的名字也避开 → 恢复时不会撞名
    log.info(f"[notes] 被删的是正在记录的笔记 → 已接力新笔记 {hub.transcript_path.name}")
    return True


@app.get("/api/notes/raw")
async def notes_raw(name: str = ""):
    """§4 编辑用：笔记的 md 原文 + 版本戳 mtime_us。

    mtime_us 是保存时的「乐观并发」凭据：页面打开后若又有新字幕写进来，保存会被
    拒绝——避免"编辑面板里还是旧文本，一保存把新内容整段覆盖"（正在记录时尤其要紧）。"""
    p, err = note_path_arg(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    try:
        text = p.read_text(encoding="utf-8")
        st = p.stat()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"读取失败: {type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, "name": p.name, "text": text, "sha": text_sha(text),
            "tags": notes_mod.note_tags(text), "presets": tag_presets(),
            "mtime_us": st.st_mtime_ns // 1000, "size": st.st_size}


@app.put("/api/notes/{name}")
async def notes_save(name: str, req: Request):
    """§4 编辑：用请求体里的 md 原文整体替换笔记正文。

    「保存后重索引」在本项目里是**天然成立**的：索引/列表每次都按文件现读现算
    （notes.index），没有需要重建的内存索引；这里只需广播 notes 事件，让别的窗口刷新。

    base_sha（= GET /api/notes/raw 回的正文指纹）必填且必须与**当前**正文一致：
    不一致 → 409 明确文案（另有新字幕写进来）。用指纹而不是 mtime：Windows 的
    mtime 只有约 15ms 粒度（时钟滴答），毫秒级的两次写入会撞成同一戳，而这一检查
    要防的正是"页面开着好几分钟，期间来了新字幕"。

    行尾口径：整篇保存**统一按本平台行尾**（Windows=CRLF）落盘——浏览器 textarea 的
    value 被 DOM 规范成 \\n，无法逐字节回放 CRLF；行级改写（课程行/标签行）另走
    保真路径（main.read_note_text + note_atomic_write 默认 newline=""）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("text", "base_sha")]
    if unknown:
        return JSONResponse(
            {"ok": False,
             "error": f"未知字段: {unknown[0]}（允许: ['text', 'base_sha']）"},
            status_code=400)
    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "text 必须是字符串（md 原文）"},
                            status_code=400)
    if len(text.encode("utf-8")) > NOTE_EDIT_MAX:
        return JSONResponse(
            {"ok": False, "error": f"正文过大（上限 {NOTE_EDIT_MAX // 1024} KB）"},
            status_code=400)
    base = body.get("base_sha")
    if not isinstance(base, str) or not base.strip():
        return JSONResponse(
            {"ok": False,
             "error": "缺少 base_sha：先 GET /api/notes/raw 取 sha 再保存"
                      "（防止覆盖编辑期间新写入的字幕）"},
            status_code=400)
    p, err = note_path_arg(name)
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    try:
        # 以下同步执行（读 stat → 写 .tmp → replace），中间无 await：
        # 事件循环不会把字幕追加插进来，故不会丢段。
        if text_sha(p.read_text(encoding="utf-8")) != base.strip():
            return JSONResponse(
                {"ok": False,
                 "error": "笔记在编辑期间有新内容写入（可能正在记录字幕）——"
                          "请重新打开再编辑，避免覆盖新内容"},
                status_code=409)
        # newline=None：**整篇保存统一按本平台行尾**（Windows=CRLF）落盘。
        # 理由（评审 M4）：浏览器 textarea 的 value 由 DOM 规范成 \n，无法逐字节
        # 回放 CRLF，若按"原样"写反而会把 CRLF 笔记静默翻成 LF。行级改写
        # （课程行/标签行）走的是保真路径（read_note_text + 默认 newline=""），
        # 两者口径不同且各自写明；本平台录制的笔记本就是 CRLF，故这里归一即稳定。
        note_atomic_write(p, text, newline=None)
        st2 = p.stat()
    except Exception as e:
        log.warning(f"[notes] ⚠ 保存失败（{p.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"保存失败（原文件已尽量保持原样）: "
                                   f"{type(e).__name__}: {e}"},
            status_code=500)
    log.info(f"[notes] 编辑保存 {p.name}（{st2.st_size} 字节）")
    await broadcast_notes([p.name])
    return {"ok": True, "name": p.name, "size": st2.st_size,
            "mtime_us": st2.st_mtime_ns // 1000}


@app.post("/api/notes/rename")
async def notes_rename(req: Request):
    """§4 重命名：改文件名（**内容零改动**），派生文件（摘要/时间轴/卡组）跟着改。

    新名 sanitize 见 notes.normalize_note_name（补 .md / 补 lecture_ 前缀 / 禁路径
    分隔与系统保留名 / 长度限）。正在记录的那篇也能改名——改完把 hub.transcript_path
    指到新路径上，字幕继续写进同一个文件（否则录制会另起一个旧名文件）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("file", "to")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['file', 'to']）"},
            status_code=400)
    p, err = note_path_arg(body.get("file"))
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    new, nerr = notes_mod.normalize_note_name(body.get("to"))
    if nerr:
        return JSONResponse({"ok": False, "error": nerr}, status_code=400)
    if new == p.name:
        return JSONResponse({"ok": False, "error": f"新文件名与原名相同: {new}"},
                            status_code=400)
    dest = Path(TRANSCRIPTS) / new
    if dest.exists():
        return JSONResponse({"ok": False, "error": f"已存在同名笔记: {new}"},
                            status_code=400)
    old_name = p.name
    moved: list[str] = []
    try:
        p.replace(dest)
        moved.append(new)
        for old_sidecar, new_sidecar in zip(notes_mod.sidecar_paths(p),
                                            notes_mod.sidecar_paths(dest)):
            src = p.with_name(old_sidecar)
            if src.is_file():
                src.replace(dest.with_name(new_sidecar))
                moved.append(new_sidecar)
    except Exception as e:
        log.warning(f"[notes] ⚠ 重命名失败（{old_name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"重命名失败: {type(e).__name__}: {e}（已移动: {moved}）"},
            status_code=500)
    was_live = False
    if hub.transcript_path and hub.transcript_path.name == old_name:
        hub.transcript_path = dest       # 录制跟着改名走（否则会另起旧名文件）
        was_live = True
    log.info(f"[notes] 重命名 {old_name} → {new}（派生文件 {len(moved) - 1} 个；"
             f"{'正在记录，已重指' if was_live else '历史笔记'}）")
    await broadcast_notes([new, old_name])
    return {"ok": True, "file": old_name, "to": new, "sidecars": len(moved) - 1,
            "live": was_live}


@app.post("/api/notes/delete")
async def notes_delete(req: Request):
    """§4 删除 = **软删除**：连同派生文件移进 transcripts/.trash/（同盘 rename）。

    删的是"正在记录的那篇"时，顺手接力一篇新笔记（见 live_restart_after_delete）——
    不这么做，后面的字幕会把回收站里的文件名在原目录重新创建出来。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    paths, err = note_batch_arg(body, body.get("files"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    results: list[dict] = []
    failed: list[dict] = []
    for p in paths:
        try:
            moved = notes_mod.move_note_with_sidecars(p, trash_dir())
            results.append({"file": p.name, "moved": moved})
        except notes_mod.MoveConflict as e:
            # 回收站里已有同名文件（评审 M2：旧实现直接 replace，静默覆盖旧笔记
            # 与它的摘要）。如实报失败并点名冲突文件，绝不覆盖。
            log.warning(f"[notes] ⚠ 软删除冲突（{p.name}）: {e.names}")
            failed.append({"file": p.name,
                           "error": f"回收站里已有同名文件，未删除也未覆盖："
                                    f"{', '.join(e.names)}（先彻底删除/清空回收站）"})
        except Exception as e:
            log.warning(f"[notes] ⚠ 软删除失败（{p.name}）: {e}")
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    done = [Path(TRANSCRIPTS) / r["file"] for r in results]
    restarted = live_restart_after_delete(done)
    log.info(f"[notes] 软删除 {len(results)} 篇（失败 {len(failed)}）→ {TRASH_DIR_NAME}/"
             f"{'（已接力新笔记）' if restarted else ''}")
    if results:
        await broadcast_notes([r["file"] for r in results])
    return {"ok": True, "trashed": len(results), "results": results,
            "failed": failed, "restarted": restarted}


@app.get("/api/notes/trash")
async def notes_trash_list():
    """§4 回收站列表（按删除时间倒序）。只列笔记正文；同名派生文件跟着笔记一起搬运，
    不单独出现。"""
    d = trash_dir()
    items: list[dict] = []
    if d.is_dir():
        for p in d.glob(notes_mod.NOTE_GLOB):
            if not notes_mod.is_note_file(p):
                continue
            try:
                text = p.read_text(encoding="utf-8")
                st = p.stat()
            except Exception:
                continue              # 单篇坏文件不该让回收站整体 500
            parsed = notes_mod.parse(text)
            en, zh = notes_mod.first_sentence(parsed["pairs"])
            items.append({
                "name": p.name,
                "date": notes_mod.display_date(parsed["stamp"], p.name),
                "deleted_at": st.st_mtime,
                "size": st.st_size,
                "course": parsed["course"] or courses_mod.UNCATEGORIZED,
                "first_en": en,
                "first_zh": zh,
                "segments": parsed["segments"],
            })
    items.sort(key=lambda x: x["deleted_at"], reverse=True)
    return {"ok": True, "notes": items, "total": len(items), "dir": TRASH_DIR_NAME}


@app.post("/api/notes/trash/restore")
async def notes_trash_restore(req: Request):
    """§4 回收站：恢复（搬回 transcripts/）。目标位置已有同名文件（正文**或派生物**）
    → 该篇记 failed 并继续（**绝不覆盖**，评审 M2：旧实现只查正文，于是同名摘要
    会被静默盖掉，与这句承诺自相矛盾）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    raw = body.get("files")
    unknown = [k for k in body if k != "files"]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['files']）"},
            status_code=400)
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"ok": False, "error": "files 必须是非空数组"},
                            status_code=400)
    results: list[dict] = []
    failed: list[dict] = []
    for name in raw:
        p, err = trash_path_arg(name)
        if err:
            failed.append({"file": name, "error": err})
            continue
        try:
            notes_mod.move_note_with_sidecars(p, Path(TRANSCRIPTS))
            results.append({"file": p.name})
        except notes_mod.MoveConflict as e:
            log.warning(f"[notes] ⚠ 恢复冲突（{p.name}）: {e.names}")
            failed.append({"file": p.name,
                           "error": f"目标位置已有同名文件，未覆盖：{', '.join(e.names)}"
                                    f"（先改名或删掉它）"})
        except Exception as e:
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    log.info(f"[notes] 回收站恢复 {len(results)} 篇（失败 {len(failed)}）")
    if results:
        await broadcast_notes([r["file"] for r in results])
    return {"ok": True, "restored": len(results), "results": results, "failed": failed}


@app.post("/api/notes/trash/purge")
async def notes_trash_purge(req: Request):
    """§4 回收站：彻底删除（连派生文件一起 unlink，不可恢复）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    raw = body.get("files")
    unknown = [k for k in body if k != "files"]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['files']）"},
            status_code=400)
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"ok": False, "error": "files 必须是非空数组"},
                            status_code=400)
    results: list[dict] = []
    failed: list[dict] = []
    for name in raw:
        p, err = trash_path_arg(name)
        if err:
            failed.append({"file": name, "error": err})
            continue
        try:
            gone = _purge_one(p)
            results.append({"file": p.name, "removed": gone})
        except Exception as e:
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    log.info(f"[notes] 回收站彻底删除 {len(results)} 篇（失败 {len(failed)}）")
    return {"ok": True, "purged": len(results), "results": results, "failed": failed}


def _purge_one(p: Path) -> list[str]:
    """彻底删除回收站里的一篇（连派生文件）→ 实际删掉的文件名。"""
    gone = [p.name]
    p.unlink()
    for name in notes_mod.sidecar_paths(p):
        s = p.with_name(name)
        if s.is_file():
            s.unlink()
            gone.append(name)
    return gone


@app.post("/api/notes/trash/empty")
async def notes_trash_empty():
    """§4 回收站：清空（目录里所有**文件**，含派生文件；子目录不动——我们不放目录，
    真有也是别人的东西，不替用户删）。"""
    d = trash_dir()
    removed = 0
    failed: list[dict] = []
    if d.is_dir():
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            try:
                entry.unlink()
                removed += 1
            except Exception as e:
                failed.append({"file": entry.name, "error": f"{type(e).__name__}: {e}"})
    log.info(f"[notes] 清空回收站（删 {removed} 个文件，失败 {len(failed)}）")
    return {"ok": True, "removed": removed, "failed": failed}


# —— §8（标签）：笔记头块内的 `> 标签：#a #b` 一行 ——
# 存**文件里**不存 sidecar：可移植（笔记拷走标签跟着走）、随编辑/移动/重命名存活、
# git 友好。读写都只认头部区块（与课程行同窗口，见 notes.HEAD_SCAN）——正文里恰好
# 同形的引用行不受影响。写端点是**全量替换**（幂等）：重不重要由配置决定，前端拿
# 一次 GET 的现状做增删，再整表提交。


#: B3：常用标签的**出厂值**（= config.tags_presets 的默认；老用户零变化）。
#: 这不再是「唯一来源」——用户可在设置页 📝笔记 里自定义，运行时口径见 tag_presets()。
DEFAULT_TAG_PRESETS = ("重点", "考试", "没听懂", "待复习")


def tag_presets() -> list[str]:
    """B3：当前常用标签（预设 chips）= config.tags_presets，坏值回落出厂四标签。

    **取值函数而不是模块常量**：四个下发点（/api/notes/list、/api/notes/raw、
    tags_view、批量打标签）都现调它读 runtime['config']，所以设置页一保存，下一次
    响应就带新 chips——不需要任何「重启/重载」，也不会出现两份清单各说各话
    （前端那份 TAG_PRESETS 只保存服务端最近一次响应，见 index.html）。

    config.json 是**用户可手改的文件**：不是非空 list、或里面混着非法标签（含空白、
    超长、`#`）时**整体**回落出厂四值——下发的是 chips 数据，半截坏值流到前端只会
    渲染出一排看不出问题的怪 chip；这里宁可回到已知可用的一套。合法值走
    notes.normalize_tag 逐个规范化（与写路径同一口径，读侧不再有第二套规则）。"""
    raw = (runtime.get("config") or {}).get("tags_presets")
    if isinstance(raw, list) and 1 <= len(raw) <= TAG_PRESETS_MAX:
        out = [t for t in (notes_mod.normalize_tag(one) for one in raw) if t]
        if len(out) == len(raw):        # 有条目被规范化掉 = 文件里有坏值 → 整体回落
            return list(dict.fromkeys(out))
    return list(DEFAULT_TAG_PRESETS)


#: A2 批量打标签的两个方向：add = 补上这些标签；remove = 从这些笔记去掉这些标签
TAG_BATCH_MODES = ("add", "remove")


def merge_tags(current: list[str], tags: list[str], mode: str) -> list[str]:
    """把一组标签按方向并进某篇笔记**现有的**标签表（纯函数）。

    保序：现有标签在前、新增按提交顺序接在后面（幂等——已经有的不重复加）；
    remove 只筛掉命中项，顺序不变。"""
    if mode == "remove":
        drop = set(tags)
        return [t for t in current if t not in drop]
    out = list(current)
    for t in tags:
        if t not in out:
            out.append(t)
    return out


def note_tag_targets(body: dict, raw_files) -> tuple[list[Path] | None, list[str] | None,
                                                     str | None, str | None, int]:
    """校验批量打标签请求体 → (路径列表, 规范化标签, 方向, 错误文案, 错误状态码)。

    **先全部校验、后动手**（与 §2 course/batch 同一条纪律）：任何一篇非法/不存在、
    标签本身不合法、方向不认识、字段不认识 → 整单拒绝，绝不半批写入——否则用户看到
    「打好了」而一半笔记其实没打上。单篇**写入阶段**的失败才逐条记 failed 并继续
    （返回 200 + 明细，让界面如实说「N 篇成功、M 篇失败」）。

    文件路径一律走 notes.safe_note_path（库内 lecture_*.md 白名单 + 反越界），与
    读/导出/删除/改归属同一条唯一入口——这里同样要**写**这些文件。"""
    unknown = [k for k in body if k not in ("files", "tags", "mode")]
    if unknown:
        return None, None, None, \
            f"未知字段: {unknown[0]}（允许: ['files', 'tags', 'mode']）", 400
    mode = body.get("mode", "add")
    if mode not in TAG_BATCH_MODES:
        return None, None, None, \
            f"未知方向: {mode!r}（允许: {list(TAG_BATCH_MODES)}）", 400
    tags, err = notes_mod.normalize_tags(body.get("tags"))
    if err:
        return None, None, None, err, 400
    if not tags:
        return None, None, None, "至少要给一个标签（tags 为空）", 400
    files = body.get("files")
    if files is None:
        return None, None, None, "缺少 files（要批量打标签的笔记名列表）", 400
    if not isinstance(files, list) or not files:
        return None, None, None, "files 不能为空", 400
    if len(files) > NOTE_MOVE_MAX:
        return None, None, None, f"一次最多处理 {NOTE_MOVE_MAX} 篇（收到 {len(files)}）", 400
    paths: list[Path] = []
    seen: set = set()
    for f in files:
        p = notes_mod.safe_note_path(TRANSCRIPTS, f)
        if p is None:
            return None, None, None, \
                f"非法笔记名: {f!r}（只接受 transcripts/ 下的 lecture_*.md）", 400
        if not p.is_file():
            return None, None, None, f"笔记不存在: {f}", 400
        if p in seen:              # 重复列同一篇：去重（幂等，不报错也不算两次）
            continue
        seen.add(p)
        paths.append(p)
    return paths, tags, mode, None, 200


def note_tags_write(p: Path, tags: list[str]) -> str:
    """把标签行写成 tags（全量替换）→ 动作 replaced / inserted / removed / none。

    与 note_course_write 同款：读全文 → 变换 → 写 .tmp + os.replace，**中间无 await**，
    故不会与正在记录的字幕追加交错丢段。"""
    text = read_note_text(p)
    new, action = notes_mod.set_tags(text, tags)
    if new == text:
        return "none"
    note_atomic_write(p, new)
    return action


def tag_manage_targets() -> list[Path]:
    """B9：标签管理的扫描范围 = transcripts/ **直属**的 `lecture_*.md`（全库）。

    为什么由**服务端自己枚举**（而不是复用 tags/batch 收前端给的文件名）：
    ① 「全库重命名/删除标签」的用户意图就是「凡用了这个标签的都改」，让前端先列一遍再
    提交既多一次往返、又会在两次请求之间漏掉刚好写下的笔记；
    ② tags/batch 有 **200 篇上限**（那是「用户在列表里勾选」的批量语义），全库操作会
    直接撞墙——所以这一支刻意不复用它。
    范围纪律（与 courses.rename_in_notes / notes.index 同款）：
    - **非递归** glob → `.trash/` 里的笔记天然不在名单里（回收站只走回收站接口）；
    - `is_note_file` 排除 `.summary.md` / `.bak.md` 等派生物（同前缀同目录）；
    - 父目录必须**就是** transcripts（防御纵深：glob 结果被符号链接之类指歪时兜住）。"""
    d = Path(TRANSCRIPTS)
    out: list[Path] = []
    if not d.is_dir():
        return out
    for p in d.glob(notes_mod.NOTE_GLOB):
        if p.parent != d or not notes_mod.is_note_file(p):
            continue
        out.append(p)
    return sorted(out)


def tag_manage_merge(current: list[str], op: str, old: str, new: str | None
                     ) -> list[str] | None:
    """B9：一篇笔记的标签表 → 施加 rename/delete 后的新表；**没这篇的份 → None**。

    - `rename`（改名 = 合并语义）：命中的旧名换成新名，并**去重保序**——该篇若本来
      就带着新名（把 `#考试` 改叫 `#重点`，而它已经是 `#重点`），逐项替换会写出
      `> 标签：#重点 #重点`：文件里重复、解析侧却去重，POST 回显与随后的 GET 自相
      矛盾（M3 那条口径的翻版）；
    - `delete`：复用 merge_tags 的 remove 语义（与批量端点**同一个函数**，不另写一套）；
    - 没命中旧名 → None：调用方据此跳过该篇（不写盘、不计入 changed，幂等且零无谓 IO）。

    纯函数（不吃 IO），便于直接单测；读写由端点按既有 note_tags_write 走。"""
    if old not in current:
        return None
    if op == "rename":
        out: list[str] = []
        for t in current:
            t2 = new if t == old else t
            if t2 not in out:
                out.append(t2)
        return out
    return merge_tags(current, [old], "remove")


def tags_view(p: Path) -> dict:
    """一篇笔记的标签视图（GET/POST 共用同一形状，前端两个入口都吃它）。"""
    text = p.read_text(encoding="utf-8")
    st = p.stat()
    return {"ok": True, "file": p.name, "tags": notes_mod.note_tags(text),
            "sha": text_sha(text), "mtime_us": st.st_mtime_ns // 1000,
            "size": st.st_size, "presets": tag_presets()}


@app.get("/api/notes/tags")
async def notes_tags_get(name: str = ""):
    """§8 读一篇笔记的标签（name 缺省 = **当前正在记录的那篇**，字幕页 🏷 用）。

    为什么要有 GET：写端点是全量替换，而"增一个 / 删一个"必须先知道现状——没有它
    前端只能自己解析 md 或拉整篇原文（raw 会把几百 KB 带回浏览器）。"""
    if not (name or "").strip():
        p = hub.transcript_path
        if not (p and p.is_file()):
            return JSONResponse({"ok": False, "error": "还没有正在记录的笔记"},
                                status_code=404)
    else:
        p, err = note_path_arg(name)
        if p is None:
            return JSONResponse({"ok": False, "error": err}, status_code=404)
    try:
        return tags_view(p)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"读取失败: {type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/notes/tags")
async def notes_tags_set(req: Request):
    """§8 全量替换一篇笔记的标签（幂等）：{file, tags: [...]}。

    规则（notes.normalize_tags）：每个 ≤16 字符、禁空格/全角空格/#/换行、每篇 ≤20 个、
    同名去重；非法标签点名报 400（不静默丢弃——用户以为存上了才糟）。标签行不存在时
    插到**课程行下方**；tags=[] 则删掉该行。返回新 sha：前端"加完标签接着编辑正文"
    要把当次 PUT 的乐观并发凭据换成它，否则会自己把自己撞成 409。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("file", "tags")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['file', 'tags']）"},
            status_code=400)
    tags, err = notes_mod.normalize_tags(body.get("tags"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if not isinstance(body.get("file"), str) or not body.get("file", "").strip():
        return JSONResponse({"ok": False, "error": "缺少 file（要打标签的那篇笔记名）"},
                            status_code=400)
    p, err = note_path_arg(body.get("file"))
    if p is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        action = note_tags_write(p, tags)
        view = tags_view(p)
    except Exception as e:
        log.warning(f"[notes] ⚠ 标签写入失败（{p.name}）: {e}")
        return JSONResponse(
            {"ok": False, "error": f"写入失败（原文件未改动）: {type(e).__name__}: {e}"},
            status_code=500)
    log.info(f"[notes] 标签 {p.name} → {' '.join('#' + t for t in tags) or '（清空）'}"
             f"（{action}）")
    await broadcast_notes([p.name])
    # 回显**从文件解析出来的**标签表，而不是提交值（评审 M3）：写盘若与提交值有出入
    # （规范化差异/被别的写入插入），GET 与 POST 会自相矛盾——以文件为准则不会。
    view["action"] = action
    return view


@app.post("/api/notes/tags/batch")
async def notes_tags_batch(req: Request):
    """A2 批量打标签：{files[], tags[], mode: add|remove} → 逐篇合并标签行。

    与单篇端点（全量替换）的分工：这里收的是**一组标签 + 一个方向**，每篇的现状由
    服务端自己读（前端不必逐篇 GET，也就没有「读到一半、写到一半」的中间态）。
    校验是整单口径（见 note_tag_targets）；写入阶段的单篇失败逐条记 failed 并继续，
    返回 200 + 明细，让界面能如实说「N 篇成功、M 篇失败」，而不是一个 500 把已成功的
    部分也藏起来。成功篇目**只广播一次**（A1 那套批量语义：另一窗口刷新一次即可，
    200 篇逐条广播会把别的窗口打爆）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    paths, tags, mode, err, code = note_tag_targets(body, body.get("files"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=code)
    results: list[dict] = []
    failed: list[dict] = []
    for p in paths:
        try:
            text = read_note_text(p)             # 读全文只用来取**现有**标签（头部一行）
            current = notes_mod.note_tags(text)
            merged = merge_tags(current, tags, mode)
            if len(merged) > notes_mod.TAG_MAX_COUNT:
                # 不静默截断（与 normalize_tags 同一条纪律）：如实点名哪一篇、差在哪，
                # 其余篇目照常处理——用户可以先删几个标签再来一次。
                failed.append({"file": p.name,
                               "error": f"会超过每篇 {notes_mod.TAG_MAX_COUNT} 个标签上限"
                                        f"（该篇现有 {len(current)} 个）"})
                continue
            action = note_tags_write(p, merged)
            results.append({"file": p.name, "tags": merged, "action": action})
        except Exception as e:
            log.warning(f"[notes] ⚠ 批量打标签失败（{p.name}）: {e}")
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    verb = "加" if mode == "add" else "去"
    log.info(f"[notes] 批量{verb}标签 {' '.join('#' + t for t in tags)} → "
             f"成功 {len(results)}，失败 {len(failed)}")
    if results:
        await broadcast_notes([r["file"] for r in results])
    return {"ok": True, "tags": tags, "mode": mode, "changed": len(results),
            "presets": tag_presets(), "results": results, "failed": failed}


# —— B9（标签管理页）：全库重命名（含合并）/ 删除一个标签 ——
# 与 tags/batch 的分工：那支是「用户勾选的这 N 篇」（收文件名、上限 200）；这一支是
# **全库**（服务端自己枚举 transcripts/ 直属 lecture_*.md，见 tag_manage_targets）。
# 为什么值得单开一个端点：标签变多以后，「把 #考试 并成 #重点」要改的是"所有用过它的
# 笔记"——前端先列一遍再提交会漏掉这期间写下的笔记，而且会撞 batch 的 200 篇上限。


@app.post("/api/notes/tags/manage")
async def notes_tags_manage(req: Request):
    """B9：全库重命名（合并）/ 删除一个标签。

    请求体：`{"op": "rename"|"delete", "old": "#考试", "new": "重点"}`（rename 必填 new）。
    - `old`/`new` 都过 notes.normalize_tag（≤16 字、禁空白/控制字符/#；宽松接受开头的
      `#`）——**整单先校验后动手**：任一非法 → 400，一篇笔记都不动；
    - `rename` 即**合并**语义：笔记里同时有新旧名时去重（不会写出 `#重点 #重点`）；
    - 只扫 transcripts/ 直属的 lecture_*.md（`.trash/` 与非笔记派生物天然不在范围）；
    - 逐篇失败不中断（与批量端点同款：返回 200 + failed 明细，界面如实说 N 篇成功、
      M 篇失败），但**写完只广播一帧** notes 消息（全库操作逐篇广播会把别的窗口打爆）。"""
    body, bad = await course_body(req)
    if bad:
        return bad
    unknown = [k for k in body if k not in ("op", "old", "new")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['op', 'old', 'new']）"},
            status_code=400)
    op = body.get("op")
    if op not in ("rename", "delete"):
        return JSONResponse(
            {"ok": False, "error": f"未知操作 op={op!r}（可选: rename/delete）"},
            status_code=400)
    old = notes_mod.normalize_tag(body.get("old"))
    if old is None:
        return JSONResponse(
            {"ok": False, "error": f"非法标签: {body.get('old')!r}"
                                   f"（≤{notes_mod.TAG_MAX_LEN} 字符；"
                                   f"禁一切空白/控制字符与 #）"},
            status_code=400)
    new = None
    if op == "rename":
        new = notes_mod.normalize_tag(body.get("new"))
        if new is None:
            return JSONResponse(
                {"ok": False, "error": f"非法新标签: {body.get('new')!r}"
                                       f"（≤{notes_mod.TAG_MAX_LEN} 字符；"
                                       f"禁一切空白/控制字符与 #）"},
                status_code=400)

    paths = tag_manage_targets()
    if op == "rename" and new == old:
        # 改叫同一个名字：不是错误，但也没必要逐篇读盘（前端已禁用该按钮，脚本调用给个明确回执）
        log.info(f"[notes] 标签管理 rename #{old} → #{new}：新旧同名，未改动任何笔记")
        return {"ok": True, "op": op, "old": old, "new": new, "scanned": len(paths),
                "changed": 0, "files": [], "failed": [],
                "note": "新旧标签名相同，未改动任何笔记"}
    results: list[dict] = []
    failed: list[dict] = []
    for p in paths:
        try:
            current = notes_mod.note_tags(read_note_text(p))
            merged = tag_manage_merge(current, op, old, new)
            if merged is None:
                continue                     # 这篇没用过该标签：跳过（不写盘、不计 changed）
            action = note_tags_write(p, merged)
            if action != "none":
                results.append({"file": p.name, "tags": merged, "action": action})
        except Exception as e:
            log.warning(f"[notes] ⚠ 标签管理失败（{p.name}）: {e}")
            failed.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    verb = f"rename #{old} → #{new}" if op == "rename" else f"delete #{old}"
    log.info(f"[notes] 标签管理 {verb}：扫 {len(paths)} 篇、改 {len(results)} 篇、"
             f"失败 {len(failed)} 篇")
    if results:
        await broadcast_notes([r["file"] for r in results])   # 全库操作只广播一帧
    return {"ok": True, "op": op, "old": old, "new": new, "scanned": len(paths),
            "changed": len(results), "files": [r["file"] for r in results],
            "failed": failed}


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
    """模型管理器视图：缓存状态 + 当前模型 + 下载中 + 离线开关 + 镜像生效态。

    Mac 适配：后端由 device 解析规则决定（Mac arm64 + mlx-whisper → mlx）——
    展示/下载/删除的仓库与「完整性」判据都跟着它走（ct2 的 model.bin vs MLX 的
    weights.safetensors）。随包目录只有 ctranslate2 权重，故 mlx 下 bundled 恒 false。"""
    cache = models_mod.cache_dir()
    active = runtime["config"].get("model")
    backend = asr_mod.active_backend(runtime["config"].get("device"))
    data = models_mod.list_models(cache, active, backend)
    for m in data["models"]:
        m["bundled"] = bundled_model_present(m["name"]) if backend != "mlx" else False
        m["size_h"] = models_mod.human_size(m["size"])
    data["total_size_h"] = models_mod.human_size(data["total_size"])
    data["active"] = active
    data["backend"] = backend          # UI/排障用：本机实际会走哪个引擎
    data["downloading"] = runtime.get("downloading")
    # D2：offline = **显式离线开关**（env HF_HUB_OFFLINE=1，不再由 frozen 置位）；
    # mirror = 进程内**实际生效**的镜像态（点 hf_mirror 要重启才变）——两个都是
    # 「事实」而非「用户意图」，UI 据此如实展示。
    data["offline"] = hf_offline()
    data["mirror"] = hf_mirror_active()
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


@app.get("/api/tiers")
async def api_tiers():
    """模型档位视图（档位化）：四档人话 + 本机硬件检测与推荐。

    检测（nvidia-smi 子进程）与缓存扫描（dir_size）都是同步 IO，统一放线程——
    与 /api/models 同款纪律：设置页打开绝不能堵住字幕主链路。"""
    hw = await asyncio.to_thread(tiers_mod.detect_hardware)
    rec = tiers_mod.recommend(hw)
    snap = await asyncio.to_thread(models_snapshot)
    by_name = {m["name"]: m for m in snap.get("models", [])}
    active = runtime["config"].get("model")
    tiers_out = []
    for t in tiers_mod.TIERS:
        m = by_name.get(t["model"], {})
        tiers_out.append({
            "id": t["id"], "name": t["name"], "model": t["model"],
            "desc": t["desc"],
            "est_h": models_mod.human_size(t["size_mb"] * 1024 * 1024),
            "size_h": m.get("size_h", ""),
            "cached": bool(m.get("cached")),
            "partial": bool(m.get("partial")),
            "active": active == t["model"],
            "downloading": runtime.get("downloading") == t["model"],
            "recommended": rec["tier"]["id"] == t["id"],
        })
    return JSONResponse({
        "ok": True, "tiers": tiers_out, "hardware": hw,
        "reason": rec["reason"], "recommended_model": rec["tier"]["model"],
    })


@app.post("/api/models/download")
async def api_models_download(req: Request):
    """下载某模型到 HF 缓存。受理即返回，进度看 /api/status 的 load 字段。

    三道前置校验（校验与置位之间无 await，与 /api/config 守卫同款纪律）：
    白名单短名 → 400；已有下载在跑 → 409；模型切换进行中 → 409（两者共用
    status['load'] 这条进度条，并发会互相覆盖）。"""
    body = await optional_json(req)
    name = body.get("name")
    backend = asr_mod.active_backend(runtime["config"].get("device"))
    if not isinstance(name, str) or name not in models_mod.repos_for(backend):
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
    info = models_mod.model_info(cache, name, runtime["config"].get("model"), backend)
    info["bundled"] = bundled_model_present(name) if backend != "mlx" else False
    info["size_h"] = models_mod.human_size(info["size"])
    if info["cached"]:
        return {"ok": True, "already": True, "model": info}

    offline = hf_offline()   # D2：只认显式离线开关（frozen 不再自动置位）
    runtime["downloading"] = name
    status["model_dl"] = {"state": "running", "name": name, "error": None,
                          "freed": 0}

    dl_owner = f"dl:{name}"   # 进度条所有权令牌（评审2：见 _load_owner 的说明）

    def _progress(pct: int, stage: str) -> None:
        set_load_progress(pct, stage, dl_owner)

    async def _run() -> None:
        try:
            await asyncio.to_thread(models_mod.download_model, name, cache,
                                    _progress, local_files_only=offline,
                                    backend=backend)
            got = models_mod.model_info(cache, name,
                                        runtime["config"].get("model"), backend)
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
    log.info(f"[models] 开始下载 {name}（{models_mod.repos_for(backend)[name]}）"
             + ("（离线模式：仅查本地缓存）" if offline else ""))
    return {"ok": True, "started": True, "name": name,
            "repo": models_mod.repos_for(backend)[name]}


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
    backend = asr_mod.active_backend(runtime["config"].get("device"))
    if name not in models_mod.repos_for(backend):
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
        models_mod.remove_model, models_mod.cache_dir(), name, backend)
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


# —— B13：术语库包（glossary_packs/*.md → 课程术语表）——
# 包目录 = main.PACKS_DIR（dev = ROOT/glossary_packs；frozen = _MEIPASS/glossary_packs，
# 与 WEB 同款双路径）。装/卸都是**行级整块操作**（packs.install_block / uninstall_block）：
# 块用**双标记**框起、EOF 追加、卸载按标记切片——用户自加的词条既不被动也不被吞。
# 路由放在既有 /api/glossary 族之后（静态路径，不影响上面 /api/glossary/terms 的
# 「参数化路由必须排在后面」那条纪律——这里一个新参数路由都没有）。


def packs_view() -> dict:
    """术语库包列表：每包元信息 + 条目实数 + 已装到哪些术语表（按标记扫**真文件**）。

    「已装」以文件里的标记为准（单一事实来源 = 术语表文件本身），不另记索引——
    与课程统计（course_terms）、笔记数（count_notes）同一条纪律。
    **单包坏掉只坏自己那张卡**：packs.parse_pack 逐文件容错（坏包带 `error` 文案、
    条目数 0、不可安装），这里再兜一层——包目录本身出意外（权限/被占用）也只回空表 +
    一句明确原因，绝不让整页 500（与下面 glossary/ 文件的逐文件容错同一口径）。"""
    try:
        packs = packs_mod.list_packs(PACKS_DIR)
        list_error = None
    except Exception as e:              # 兜底：不把「读不出来」伪装成「一个包都没有」
        log.warning(f"[packs] ⚠ 包目录读取失败 {PACKS_DIR}: {type(e).__name__}: {e}")
        packs, list_error = [], f"包目录读取失败: {type(e).__name__}: {e}"
    installed: dict = {}
    for p in glossary_mod.list_files(ROOT):
        try:
            names = packs_mod.installed_names(glossary_mod.read_file(p)["lines"])
        except Exception as e:              # 展示信息，单个文件坏不该让端点 500
            log.debug(f"[packs] {p.name} 标记扫描失败（忽略）: {e}")
            continue
        for nm in names:
            installed.setdefault(nm, []).append(p.name)
    items = [{k: v for k, v in pk.items() if k != "entries"}
             for pk in packs]
    for it in items:
        it["installed_in"] = installed.get(it["name"], [])
    courses_data = courses_mod.load(COURSES_PATH)
    active = courses_data.get("active")
    cur = courses_mod.find(courses_data, active) if active else None
    return {"ok": True, "dir": f"{packs_mod.PACKS_DIR}/", "packs": items,
            "active": active,
            "active_name": cur["name"] if cur else courses_mod.UNCATEGORIZED,
            "limits": {"max_entries": glossary_mod.MAX_ENTRIES},
            "error": list_error,
            "translate_ready": runtime.get("translator") is not None}


@app.get("/api/glossary/packs")
async def api_packs():
    """内置术语库包列表（名称/简介/条目数/已装状态）。"""
    return packs_view()


def _pack_target_course(body: dict, data: dict) -> tuple[dict | None, JSONResponse | None]:
    """安装目标课程：请求里给了 course 用它，否则用**当前课程**（active）。

    没有课程可装 → 400 + 明确文案（不猜、不静默落到「未分类」——术语库包必须装进
    某门课的术语表，装进虚空是件没发生的事）。"""
    cid = body.get("course")
    if cid is None:
        cid = data.get("active")
    elif not isinstance(cid, str) or not cid.strip():
        return None, JSONResponse(
            {"ok": False, "error": "course 须为课程 id 字符串（省略 = 用当前课程）"},
            status_code=400)
    if not cid:
        return None, JSONResponse(
            {"ok": False,
             "error": "没有指定课程，且当前是「未分类」——术语库包要装进某门课的术语表："
                      "先在上课处选一门课，或在本请求里给 course"},
            status_code=400)
    course = courses_mod.find(data, cid.strip())
    if course is None:
        return None, JSONResponse({"ok": False, "error": f"课程不存在: {cid}"},
                                  status_code=404)
    return course, None


def _plan_course_glossary(course: dict) -> tuple[Path | None, str | None, bool, JSONResponse | None]:
    """算好「这个包要写进哪个术语表、要不要新建、要不要写回绑定」→ (路径, 待写绑定, 是否新建, 错误响应)。

    **纯计划，不碰文件系统、不改 course**（评审修复）：旧版在**校验之前**就把表建出来、
    把 `course["glossary"]` 改掉并落盘课程表——于是「包太大装不下」返回 400 之后，
    glossary/ 下却多出一份空表、课程绑定也被改成了它（请求失败却留下副作用）。
    现在由调用方在校验通过之后才落地：先 write_file（新表 = 表头 + 安装块，一次写成），
    再写回绑定 + 广播。

    - **未绑定**：待建/待用 `glossary/<课程名>.md`（NEW_FILE_HEADER 表头，撞名追加 -2/-3…
      ——绝不覆盖别人的文件）；
    - **已绑定**：沿用原路径（文件不在 → 也算「待建」，由安装那一步写出来）；
    - 绑定路径不在 `glossary/` 直属下（courses.json 手改过）→ 400 明确文案：
      glossary.write_file 的包含性校验会拒绝它，此处先给出**能看懂的原因**。"""
    rel = course.get("glossary")
    if not rel:
        existing = {p.name for p in glossary_mod.list_files(ROOT)}
        fname = packs_mod.course_glossary_name(course["name"], existing)
        path = glossary_mod.safe_glossary_path(ROOT, fname)
        if path is None:                    # 派生逻辑理论上不可能产出非法名，兜底而已
            return None, None, False, JSONResponse(
                {"ok": False, "error": f"无法为课程「{course['name']}」派生术语表文件名"},
                status_code=400)
        return path, f"{glossary_mod.GLOSSARY_DIR}/{fname}", not path.is_file(), None
    problem = courses_mod.glossary_problem(ROOT, rel)
    if problem:
        return None, None, False, JSONResponse({"ok": False, "error": problem},
                                               status_code=400)
    path = Path(courses_mod.resolve_glossary(ROOT, rel) or "")
    if path.parent.name != glossary_mod.GLOSSARY_DIR:
        return None, None, False, JSONResponse(
            {"ok": False,
             "error": f"该课绑定的术语表不在 {glossary_mod.GLOSSARY_DIR}/ 下：{rel}"
                      f"——术语库包只能装进 {glossary_mod.GLOSSARY_DIR}/ 里的 .md"
                      f"（可在设置页把课程术语表指向 glossary/… 或清空该字段）"},
            status_code=400)
    return path, None, not path.is_file(), None


@app.post("/api/glossary/packs/install")
async def api_packs_install(req: Request):
    """把术语库包整块装进某门课的术语表（B13）。

    请求体：`{"pack": "数学", "course": "<课程 id>" | null}`（course 省略/null = 当前课程）。
    行为：课程未绑定术语表 → 自动建 `glossary/<课程名>.md` 并写回绑定；同包重复安装 =
    先卸后装（块内条目**以包的译名为准**重写，被改译/被移除的条目在 overwritten /
    dropped 里如实回报，见 packs.install_block）；去重以用户已有为准（大小写不敏感，
    **块外**的同名条目不动）；`已有 + 新增 > MAX_ENTRIES` → 400 且**零副作用**；
    写盘成功后显式热重载（reload_glossary_now）。

    **顺序纪律（评审修复）**：先算路径（不建文件）→ 读现有内容 → 整块校验 → **全过之后**
    才落地（写表 → 写回绑定 → 广播）。旧版在校验前就把表和绑定落下了，于是「装不下」
    返回 400 之后，glossary/ 里却多出一份空表、课程绑定也被改写——请求失败却改了状态。"""
    body = await optional_json(req)
    unknown = [k for k in body if k not in ("pack", "course")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['pack', 'course']）"},
            status_code=400)
    name = body.get("pack")
    problem = packs_mod.name_problem(name)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    name = name.strip()
    pack = packs_mod.find_pack(PACKS_DIR, name)
    if pack is None:
        return JSONResponse(
            {"ok": False, "error": f"没有这个术语库包: {name}"
                                   f"（可用包见 GET /api/glossary/packs）"},
            status_code=404)
    if pack.get("error"):
        # 坏包（非 UTF-8 / 读不出来）：明确 400 而不是 500，也不当成「没有这个包」
        return JSONResponse({"ok": False, "error": f"装不了「{name}」：{pack['error']}"},
                            status_code=400)
    data, derr = courses_mod.load_checked(COURSES_PATH)
    bad = courses_corrupt(derr)
    if bad:
        return bad
    course, bad_resp = _pack_target_course(body, data)
    if bad_resp:
        return bad_resp
    path, bind_rel, created, bad_resp = _plan_course_glossary(course)
    if bad_resp:
        return bad_resp
    # 新表 = 表头 + 安装块（一次写成，不先留一份空表）；已有表按盘上内容校验
    base_lines_data = glossary_mod.read_file(path)
    if created:
        base_lines = glossary_mod.parse_lines(glossary_mod.NEW_FILE_HEADER)
        base_eol = "\n"
    else:
        base_lines, base_eol = base_lines_data["lines"], base_lines_data["eol"]
    lines, info, err = packs_mod.install_block(base_lines, name, pack["entries"])
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        glossary_mod.write_file(path, lines, base_eol)
    except Exception as e:
        log.warning(f"[packs] ⚠ 安装写入失败 {path.name}: {e}")
        return JSONResponse({"ok": False, "error": f"写入失败: {e}"}, status_code=500)
    if bind_rel:
        # 表写成了才写回绑定（顺序反过来的话，写失败会留下悬空绑定）；与 POST/PUT
        # /api/courses 同一条收尾口径：落盘 + （当前课程则热重载）+ 广播
        course["glossary"] = bind_rel
        courses_mod.save(COURSES_PATH, data)
        if data.get("active") == course["id"]:
            apply_course_glossary(course)
        await broadcast_courses()
    reloaded = reload_glossary_now(path)
    after = glossary_mod.read_file(path)
    log.info(f"[packs] 安装「{name}」→ {path.name}（课程「{course['name']}」· "
             f"新增 {info['added']} 条、跳过 {len(info['skipped'])} 条重复、"
             f"覆盖译名 {len(info['overwritten'])} 条、移除旧词条 {len(info['dropped'])} 条、"
             f"共 {len(after['entries'])} 条、新建表={'是' if created else '否'}、"
             f"重载={'是' if reloaded else '否'}）")
    return {"ok": True, "pack": name, "file": path.name, "rel": course.get("glossary"),
            "course": course["id"], "course_name": course["name"],
            "created_file": created, "added": info["added"], "skipped": info["skipped"],
            "skipped_count": len(info["skipped"]),
            "overwritten": info["overwritten"], "overwritten_count": len(info["overwritten"]),
            "dropped": info["dropped"], "dropped_count": len(info["dropped"]),
            "count": len(after["entries"]), "reloaded": reloaded}


@app.post("/api/glossary/packs/uninstall")
async def api_packs_uninstall(req: Request):
    """按双标记把某个包整块卸出术语表（B13）。

    请求体：`{"pack": "数学", "file": "数学.md"}`。**只认 glossary/ 直属 .md 的裸文件名**
    （与 POST /api/glossary 同一把安全闸）。标记缺失/不完整 → 409 且**不动文件**。"""
    body = await optional_json(req)
    unknown = [k for k in body if k not in ("pack", "file")]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"未知字段: {unknown[0]}（允许: ['pack', 'file']）"},
            status_code=400)
    name = body.get("pack")
    problem = packs_mod.name_problem(name)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    name = name.strip()
    path = glossary_mod.safe_glossary_path(ROOT, body.get("file"))
    if path is None:
        return JSONResponse(
            {"ok": False,
             "error": f"非法术语表文件名: {body.get('file')!r}"
                      f"（只接受 {glossary_mod.GLOSSARY_DIR}/ 下的 .md 文件名）"},
            status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"术语表不存在: {path.name}"},
                            status_code=404)
    data = glossary_mod.read_file(path)
    lines, removed, err = packs_mod.uninstall_block(data["lines"], name)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=409)
    # 并集去重的副作用如实回报：被移除的词条里，有哪些同时还由**别的已装包**提供
    # （它们当初被跳过、挂在被卸的这个块里）。不自动补写——卸载只做卸载。
    others = {}
    for nm in packs_mod.installed_names(data["lines"]):
        if nm == name:
            continue
        rec = packs_mod.find_pack(PACKS_DIR, nm)
        if rec:
            others[nm] = [en for en, _ in rec["entries"]]
    shared = packs_mod.shared_entries(data["lines"], name, others)
    try:
        glossary_mod.write_file(path, lines, data["eol"])
    except Exception as e:
        log.warning(f"[packs] ⚠ 卸载写入失败 {path.name}: {e}")
        return JSONResponse({"ok": False, "error": f"写入失败: {e}"}, status_code=500)
    reloaded = reload_glossary_now(path)
    after = glossary_mod.read_file(path)
    log.info(f"[packs] 卸载「{name}」← {path.name}（移除 {removed} 行、"
             f"余 {len(after['entries'])} 条、与其它已装包重合 {len(shared)} 条、"
             f"重载={'是' if reloaded else '否'}）")
    return {"ok": True, "pack": name, "file": path.name, "removed_lines": removed,
            "count": len(after["entries"]), "shared": shared,
            "shared_count": len(shared), "reloaded": reloaded}


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
    # 评审2①：按**对端**分档——非环回（手机/局域网设备）才计入「已连接 N 台设备」。
    # 判据与环回豁免同源（main.is_loopback_peer，只认 socket 对端，不认任何请求头）；
    # 对端缺失 → 保守地按「本机」处理（不计入设备数，宁可少报不谎报有人在看）。
    lan_peer = not is_loopback_peer(ws.client.host if ws.client else None)
    if lan_peer:
        hub.lan_clients.add(ws)
    # B11 手机同屏防误触：递手一帧 announce，把**服务端判定的** lan 真假告诉页面。
    # 为什么由服务端说：页面自己无法可靠判断"我是不是这台电脑上的窗口"，而这条决定
    # 关系到"谁能按下暂停/下课"——必须用对端地址（与 ws_clients 同源判据），
    # 客户端自报的 lan 一律不采信。本机窗口（桌面壳/浏览器标签/悬浮条都是环回）
    # 收到 lan=false → 界面零变化。
    # 递手帧发失败（对端刚断开/管道坏）不该把这条连接变成异常：记一条 debug 就继续
    # 走既有收循环（它的 try/finally 负责清理登记）。
    try:
        await ws.send_json({"type": "hello", "lan": lan_peer})
    except Exception:
        log.debug("[ws] 递手帧（hello）发送失败", exc_info=True)
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
        hub.lan_clients.discard(ws)


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
    # 模型档位化（2026-10-13）：命令行没指定、且用户从没显式选过模型时（config.json
    # 无 model 键——首启 provision_token 只写 auth_token），自动选一档。弱电脑首装
    # 不会硬吃 1.6GB 的 turbo；用户一旦选过（写盘）就永远以用户为准。
    # D2(c)：打包版（frozen）改为**默认选随包模型**——旧行为是直接跳过、于是用
    # DEFAULT_CONFIG 的 large-v3-turbo；而轻量版随包的是 small，朋友机器离线首启会
    # 卡在「本地没有该档位」（turbo 要联网下载）。随包模型恒离线可用，开箱即用。
    if not args.model and not user_picked_model():
        bundled = bundled_model_names() if getattr(sys, "frozen", False) else []
        pick = pick_bundled_default(bundled)
        if pick:
            cfg["model"] = pick
            log.info(f"[main] 未选过模型：打包版默认用**随包模型** {pick}"
                     f"（随包：{', '.join(bundled)}；离线可用，设置页可随时更换）")
        elif not getattr(sys, "frozen", False):
            rec = tiers_mod.recommend(await asyncio.to_thread(tiers_mod.detect_hardware))
            cfg["model"] = rec["tier"]["model"]
            log.info(f"[main] 未选过模型：按硬件自动选定「{rec['tier']['name']}」档"
                     f"（{rec['tier']['model']}）——{rec['reason']}；设置页可随时更换")
        else:
            log.warning("[main] 未选过模型且没有随包模型（打包版 models/ 为空）——"
                        "沿用默认 turbo；离线机器会在加载时提示下载失败")
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

    async def translate_and_broadcast(session_seg: int, note_seg: int,
                                      ts_clock: str, en: str) -> None:
        """后台翻译 → 二次广播中文 + 笔记补写；重试耗尽仍失败时降级补偿：
        字幕灰色『（翻译失败）』提示 + 笔记占位行，保证用户可见、笔记完整。
        条目16：每段先查术语表 mtime（双侧同步刷新），再带最近 3 条已定稿
        EN 上下文派译（仅参考前缀防指代/术语跨段漂移，1200 字符封顶）。

        两个段号刻意分开（评审 M1）：
        - session_seg → WS 载荷的 `seg`（前端行标识/译文配对，跨笔记保持唯一）；
        - note_seg + 派发时捕获的 note → 写进**文件**的 ZH 锚点（笔记内配对）。
        晚到的译文若发现笔记已经换了，hub.append 会丢弃它，不留孤儿中文行。"""
        note = hub.transcript_path
        try:
            sync_glossary_reload()
            context = list(en_history)
            en_history.append(en)
            # p1-5：读 runtime 而非闭包局部——首启向导热接入 key 后（/api/first-run/apikey
            # 重建 runtime['translator']），下一段翻译即用新实例；初始化后两者同引用，
            # 未接 key 时外层守卫（if runtime["translator"]）拦截，行为不变
            zh = await runtime["translator"].translate(en, context=context)
            status["last_zh"] = zh
            hub.append(ts_clock, "", zh, note_seg, note=note)
            # 只带 zh 不带 en：翻译乱序完成时，避免前端把旧段误判成新的英文段落
            await hub.broadcast({"ts": ts_clock, "seg": session_seg, "zh": zh})
            log.info(f"      → {zh}")
        except Exception as e:
            log.warning(f"[translate] error: {e}")
            hub.append(ts_clock, "", "（翻译失败）", note_seg, note=note)
            # err 标记：前端把该行渲染成灰色，区别于正常黄色译文
            await hub.broadcast(
                {"ts": ts_clock, "seg": session_seg, "zh": "（翻译失败）", "err": True}
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
            # 笔记级段号：ZH 锚点与 .times sidecar 都用它，**换笔记归零**（评审 M1）。
            # 先算预期值、EN 行落盘成功后再提交两处计数：append 抛异常时本段被外层
            # 跳过，锚点基数与 EN 行数仍然相等（先加后写在写失败时会错位一格）。
            note_seg = hub.note_seg + 1
            ts_clock = datetime.datetime.now().strftime("%H:%M:%S")
            hub.append(ts_clock, en, "")
            hub.note_seg = note_seg
            status["segments"] += 1
            status["last_en"] = en
            # 评审1 修：capture 投递的 audio_ts 是**窗口末尾**（_emit 组装完才 put），
            # 本段音频的**窗口起点** = 发射时刻 − 窗口时长。下游一律用起点：
            # ① record_time → SRT cue 不再整体后移（旧实现每段误差 = d_i − d_1）；
            # ② payload.audio_ts → 前端「延迟 ≈ N s」不再少报，向导「刚说的那句」
            #    判定也不再会把 d 秒前的旧话当成刚说（index.html:1488 写的就是
            #    「该句音频的采集墙钟秒…什么时候说的」——旧实现与它自己的注释矛盾）。
            audio_s = len(audio) / SR
            win_start = (audio_ts - audio_s) if audio_ts is not None else None
            hub.record_time(note_seg, win_start, seg_times, audio_s)
            await hub.broadcast(final_payload(ts_clock, en, status["segments"], win_start))
            log.info(f"[{ts_clock}] {en}")

            if runtime["translator"]:  # p1-5：读 runtime（闭包局部与它同引用，行为不变）
                # 翻译异步化：派后台任务，主循环立即回去取下一段音频
                while flights and flights[0].done():
                    flights.popleft()
                if len(flights) >= MAX_FLIGHTS:
                    await flights.popleft()  # 在飞已满：等最旧的任务完成再派新
                flights.append(asyncio.create_task(
                    translate_and_broadcast(status["segments"], note_seg, ts_clock, en)
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
        """等服务开始监听后再开浏览器：页面加载即显示模型加载进度条。

        §7：端口取**实际**绑上的那个（runtime['bind']，由 main() 在起服务前写好）——
        lan_enabled=true 且 8787 被占时会换端口，打开 args.port 会开到一个空页面上。"""
        await asyncio.sleep(2.5)
        if os.environ.get("LECTURELENS_NO_BROWSER"):
            return
        port = (runtime.get("bind") or {}).get("port") or args.port
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception as e:
            log.warning(f"[main] ⚠ 打开浏览器失败: {e}（手动访问 http://localhost:{port}）")

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

    # D2：HF 环境（离线开关/镜像）在模块导入时已落定（见文件头的 hfenv.apply），
    # 那时日志还没装配——这里补一行事实，排障时能一眼看出「这轮进程到底走不走镜像」。
    log.info(f"[main] HF endpoint={HF_ENV['endpoint'] or hfenv.OFFICIAL_ENDPOINT}"
             f"{'（hf-mirror 预设镜像）' if HF_ENV['mirror'] else ''}"
             f"；显式离线={HF_ENV['offline']}；打包版={bool(getattr(sys, 'frozen', False))}")

    # 条目19①：首次启动生成随机 token 写 config.json 并 print（--list-devices 纯
    # 查询已提前返回，不会建/写配置文件）
    provision_token()

    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        log.info(f"[main] 🎙 输入设备: {dev['name']}")
    except Exception as e:
        log.warning(f"[main] ⚠ 查询输入设备失败: {e}")

    # §7（手机扫码同屏）：绑定决策——**只在启动时读一次 config**。
    #   lan_enabled=false → 127.0.0.1:args.port（与本服务一贯的「不对外」姿态一致；
    #                       端口被占仍由 uvicorn 响亮报错退出，不静默换端口）
    #   lan_enabled=true  → 0.0.0.0，优先 args.port（默认 8787），**被占则换一个空闲
    #                       端口**（§7 明文），实际端口记进 runtime['bind']——
    #                       /api/lan-qr 的 URL 与 --open-browser 都以它为准。
    # 监听地址在进程内换不了（换绑=换整条服务），所以改键要重启：端点的
    # restart_required / 日志 / 面板文案三处同一口径。
    cfg_start = load_config()
    lan_on = bool(cfg_start.get("lan_enabled"))
    bind_host, bind_want = lan_mod.lan_bind(cfg_start, args.port)
    bind_port = lan_mod.pick_port(bind_host, bind_want) if lan_on else bind_want
    set_bind(bind_host, bind_port)
    if lan_on:
        lan_ip = lan_mod.primary_lan_ip()
        log.info(f"[lan] 📱 手机同屏已开启：监听 {bind_host}:{bind_port}"
                 f"（手机连同一 WiFi 打开 http://{lan_ip or '<电脑IP>'}:{bind_port}/ "
                 f"或扫字幕页「📱 手机同屏」面板里的二维码；需要访问 token）")
        if bind_port != bind_want:
            log.warning(f"[lan] ⚠ 首选端口 {bind_want} 被占用 → 实际监听 {bind_port}"
                        f"（配对 URL/二维码里的端口以此为准）")
    else:
        log.info(f"[lan] 手机同屏未开启（config.lan_enabled=false）：仅监听 "
                 f"{bind_host}:{bind_port}，LAN 侧连不上；开启后需重启 LectureLens 生效")

    # p1-1：lifespan 三件套（_open_browser_later/_watch_asr_task/lifespan）原内嵌
    # 在此，已提升为模块级 build_lifespan(args)（行为不变），desktop.py 复用同一份
    app.router.lifespan_context = build_lifespan(args)

    log.info(f"[main] LectureLens starting — host={bind_host}, port={bind_port}, "
             f"lang={args.lang}" + (", force-gpu" if args.gpu else ""))
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
    uvicorn.run(app, host=bind_host, port=bind_port,
                log_config=uvicorn_log_config(), proxy_headers=False)


if __name__ == "__main__":
    main()
