# -*- coding: utf-8 -*-
"""tiers.py — 模型档位化（2026-10-13 用户拍板方案）。

对用户只暴露四档「思考强度」式命名（流畅/标准/高/极致），不出现 tiny/small
等原名；档位 → 模型短名的映射固定在本文件。持久化与白名单仍走原 model 键
（真实短名）——本模块只承担两件事：
1) **推荐层**：纯读检测本机硬件（不联网），保守推荐一个档位（宁小勿大）；
2) **展示层数据**：给设置页/工具条提供档位名、说明、预计体积。

检测口径（对用户的承诺「检测可能不准，故保守 + 随时可手动覆盖」）：
- NVIDIA 显卡/显存只信 nvidia-smi（WMI 对大显存读数不准——本机实测 8GB 报成
  4GB，故不用 WMI 读显存；nvidia-smi 缺失/超时按「无加速卡」处理）
- Apple Silicon 按 platform 判断（arm64）；当前 Mac 走 CPU 推理，与 Windows
  共用同一套阈值（Mac 的 MLX 加速是后续独立工程，届时只调阈值不改接口）
- 内存：Windows 用 GlobalMemoryStatusEx（ctypes，零新依赖），其余平台 sysconf

推荐阈值（宁小勿大，可手动覆盖）：
显存 ≥10GB→极致；≥4GB→高；Apple 且内存 ≥16GB→高；其余内存 ≥8GB→标准；否则流畅。
"""
import ctypes
import os
import platform
import subprocess

#: 四档定义。size_mb 为约数（与 HF 仓库实际体积同量级，用于「需下载 ≈ X」提示；
#: 真实占用以模型管理器的磁盘统计为准）。顺序 = 设置页展示顺序（弱→强）。
TIERS: tuple = (
    {"id": "fast", "name": "流畅", "model": "base",
     "size_mb": 140, "desc": "最快、最省资源；老旧电脑也能流畅出字幕"},
    {"id": "balanced", "name": "标准", "model": "small",
     "size_mb": 460, "desc": "普通笔记本的平衡点；没有独立显卡也够用"},
    {"id": "high", "name": "高", "model": "large-v3-turbo",
     "size_mb": 1600, "desc": "识别明显更准；有独立显卡或新芯片的电脑推荐"},
    {"id": "max", "name": "极致", "model": "large-v3",
     "size_mb": 2900, "desc": "当前最强精度；适合大显存显卡，速度换质量"},
)

#: 模型短名 → 档位。medium/tiny 不在档位表里——「高级」区按原名直管。
MODEL_TO_TIER: dict = {t["model"]: t for t in TIERS}


def tier_of(model: str | None) -> dict | None:
    """模型短名所属档位；不在档位表（medium/tiny/None）→ None（前端按原名显示）。"""
    return MODEL_TO_TIER.get(model) if model else None


def _ram_gb() -> float:
    """物理内存 GB（读不到返回 0——推荐逻辑把它当最低档处理，宁小勿大）。"""
    try:
        if platform.system() == "Windows":
            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys / (1024 ** 3)
        else:
            return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                    / (1024 ** 3))
    except Exception:
        pass
    return 0.0


def _nvidia() -> tuple[str | None, float | None]:
    """(显卡名, 显存 GB)。nvidia-smi 不可用/失败/超时 → (None, None)。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None, None
        name, _, mib = out.stdout.strip().splitlines()[0].rpartition(",")
        name = name.strip() or None
        try:
            vram = float(mib.strip()) / 1024 if mib.strip() else None
        except ValueError:
            vram = None
        return name, vram
    except Exception:
        return None, None


def detect_hardware() -> dict:
    """本机硬件画像（纯读、不联网、绝不抛异常——检测失败只降级推荐，不拦启动）。"""
    gpu, vram = _nvidia()
    return {
        "gpu": gpu,                       # NVIDIA 卡名；无 → None
        "vram_gb": round(vram, 1) if vram else None,
        "ram_gb": round(_ram_gb(), 1),
        "cpu": os.cpu_count() or 0,
        "apple": (platform.system() == "Darwin"
                  and platform.machine().lower() in ("arm64", "aarch64")),
    }


def recommend(hw: dict) -> dict:
    """按硬件画像保守推荐档位。返回 {"tier","reason","hardware"}。

    「保守」的含义：推荐的档位必须在这台机器上**跑得舒服**（留余量），而不是
    跑得动的最大值——用户嫌小可在设置页一键换档（换档只是重载模型，代价低）。"""
    vram = hw.get("vram_gb") or 0
    ram = hw.get("ram_gb") or 0
    if hw.get("gpu") and vram >= 10:
        tier, why = (MODEL_TO_TIER["large-v3"],
                     f"检测到显卡 {hw['gpu']}（{vram:.1f} GB 显存）")
    elif hw.get("gpu") and vram >= 4:
        tier, why = (MODEL_TO_TIER["large-v3-turbo"],
                     f"检测到显卡 {hw['gpu']}（{vram:.1f} GB 显存）")
    elif hw.get("gpu"):
        tier, why = (MODEL_TO_TIER["small"],
                     f"显卡 {hw['gpu']} 的显存读数不可用（nvidia-smi 异常），"
                     "按保守档推荐")
    elif hw.get("apple") and ram >= 16:
        tier, why = (MODEL_TO_TIER["large-v3-turbo"],
                     f"检测到 Apple 芯片（{ram:.0f} GB 统一内存）")
    elif ram >= 8:
        tier, why = (MODEL_TO_TIER["small"],
                     f"未检测到可加速的 NVIDIA 显卡（内存约 {ram:.0f} GB）"
                     if not hw.get("apple")
                     else f"Apple 芯片内存约 {ram:.0f} GB（按 CPU 推理口径保守推荐）")
    else:
        tier, why = (MODEL_TO_TIER["base"],
                     f"未检测到可加速显卡，内存约 {ram:.0f} GB（按最省档推荐）")
    return {"tier": tier, "reason": why, "hardware": hw}
