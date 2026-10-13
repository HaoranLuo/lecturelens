# -*- coding: utf-8 -*-
"""build_exe.py — PyInstaller onedir 打包（升级计划条目 20）。

用法:
    .venv/Scripts/python.exe scripts/build_exe.py            # GPU 版（默认，含 cublas/cudnn DLL）
    .venv/Scripts/python.exe scripts/build_exe.py --no-gpu   # CPU 版

产出: dist/LectureLens/LectureLens.exe（onedir + console=True）
  - **入口 = app/desktop.py**（评审员1 修复）：exe 默认形态是**桌面壳**（原生窗口 +
    系统托盘 + 全局快捷键），与开机自启项写的命令（autostart.command_parts 的
    frozen 分支 = `"<exe>" --minimized`）是同一个入口——旧版入口是 app/main.py，
    而 main.parse_args 根本不认 --minimized（实跑：`unrecognized arguments`，
    exit 2），于是「开机自启」在打包版上会开机即退、界面却显示「已启用」。
    纯服务模式（LAN/手机访问）用 `LectureLens.exe --server-only`，由 desktop.py
    原样转发给 main，行为零漂移。
  - 顺带修好一个隐患：pywebview/pystray 只在 run_desktop 里惰性导入，旧入口
    （main.py）不引用它们，PyInstaller 静态分析也就收不进包——打包版即使入口能跑
    起来，也没有窗口壳可开。
  - 官方清单（faster-whisper issue 351）: --collect-all ctranslate2 / faster_whisper /
    huggingface_hub；av 一并 collect（PyAV 自带 ffmpeg DLL）；
  - GPU 版: venv 的 nvidia DLL 按 site-packages 原布局收集进
    _internal/nvidia/<pkg>/bin —— asr._setup_cuda_dlls 的 frozen 分支用同一套
    glob 在 _MEIPASS 下查找，开发/打包同一代码路径；
  - large-v3-turbo 模型随包: 从 HF 缓存快照（local-only，不联网）复制到
    dist/LectureLens/models/large-v3-turbo；frozen 下 main.bundled_model_path
    解析为本地路径传 WhisperModel，且 main.py 在 frozen 时设 HF_HUB_OFFLINE=1
    （禁止启动时联网下载；未随包的模型名在离线模式下明确报错而非挂网）；
  - 产物目录树断言（§2.2-20 路径类断言）: exe / web / 模型 / GPU DLL 齐备，
    且产物无 .env / config.json / transcripts（密钥与用户数据绝不进包）。

调用方式: 进程内调 PyInstaller.__main__.run（官方 API）——不经 shell、
不经 subprocess，无命令注入面（Mimosa 安全口径）。
已知边界（如实声明）: 干净机器双击实测、朋友机分发实测是 §2.2-20 的手动清单，
不在本脚本自动化范围（本机无干净 Windows 环境）。
"""
import argparse
import os
import shutil
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "LectureLens"
BUNDLE_MODEL = "large-v3-turbo"          # 随包模型（计划口径：仅 large-v3-turbo，≈1.5GB）
GPU_DLL_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")  # venv nvidia 目录（存在才收）

REQUIRED_DIST_FILES = (
    "LectureLens.exe",
    "_internal/web/index.html",
    f"models/{BUNDLE_MODEL}/model.bin",
)
FORBIDDEN_DIST_ENTRIES = (".env", "config.json", "transcripts", "logs")


def locate_model_snapshot(name: str) -> Path:
    """在 HF 缓存中定位 faster-whisper <name> 的快照目录（local-only，不联网）。
    Systran 官方仓库优先，回退任意 models--*--faster-whisper-<name>（本机实测
    turbo 来自 mobiuslabsgmbh 转换仓库——机器间仓库名可能不同，通配最稳）。"""
    home = os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    hub = Path(os.environ.get("HF_HUB_CACHE", str(Path(home) / "hub")))
    if not hub.is_dir():
        raise SystemExit(f"[build] ✗ 未找到 HF 缓存目录: {hub}（先在开发机下载模型）")
    hits = sorted(hub.glob(f"models--*--faster-whisper-{name}"))
    hits.sort(key=lambda p: 0 if p.name.startswith("models--Systran--") else 1)
    for repo in hits:
        snaps = sorted((repo / "snapshots").glob("*"),
                       key=lambda p: p.stat().st_mtime)
        with_bin = [s for s in snaps if (s / "model.bin").is_file()]
        if with_bin:
            snap = with_bin[-1]
            size_gb = sum(f.stat().st_size for f in snap.rglob("*")
                          if f.is_file()) / 1e9
            print(f"[build] 模型快照: {snap.name[:12]}…（{size_gb:.2f} GB）from {repo.name}")
            return snap
    raise SystemExit(f"[build] ✗ HF 缓存无 faster-whisper-{name} 快照（含 model.bin）")


def pyinstaller_args(gpu: bool) -> list[str]:
    """PyInstaller 参数列表（官方 API 的 pargs 形式，不含程序名）。
    全部为固定字面量 + 本仓库路径拼接。"""
    site_packages = Path(sysconfig.get_paths()["purelib"])
    args = [
        "--noconfirm", "--clean",
        "--onedir", "--console",           # 条目20：onedir（CTranslate2 不支持 onefile）+ 首版 console
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        "--name", "LectureLens",
        "--icon", str(ROOT / "assets" / "icon.ico"),  # p1-3：exe/任务栏/窗口图标
        "--paths", str(ROOT / "app"),      # applog/asr/capture... 顶层导入的分析路径
        "--add-data", f"{ROOT / 'app' / 'web'};web",
        # 官方 issue 351 清单 + av 保底
        "--collect-all", "ctranslate2",
        "--collect-all", "faster_whisper",
        "--collect-all", "huggingface_hub",
        "--collect-all", "av",
        # uvicorn "auto" 协议为函数内惰性导入，显式点名防漏（装了才加，缺了无害）
        "--hidden-import", "uvicorn.protocols.http.httptools_impl",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.websockets_impl",
        "--hidden-import", "uvicorn.protocols.websockets.wsproto_impl",
        # 入口 = 桌面壳（评审员1 修复，理由见文件头）：exe 要能承接开机自启写的
        # `--minimized`；--server-only 仍可退回纯服务模式。pystray/pywebview 也
        # 只有从这里才被静态分析收进包。
        str(ROOT / "app" / "desktop.py"),
    ]
    if gpu:
        for pkg in GPU_DLL_PACKAGES:
            bin_dir = site_packages / "nvidia" / pkg / "bin"
            if bin_dir.is_dir():
                # 保持 site-packages 原布局：_internal/nvidia/<pkg>/bin
                args += ["--add-binary", f"{bin_dir};nvidia/{pkg}/bin"]
            else:
                print(f"[build] ⚠ 未找到 nvidia/{pkg}（跳过；GPU 运行库不完整可能报 "
                      f"'cublas64_12.dll is not found'）")
    return args


def assert_dist_tree(gpu: bool) -> None:
    """§2.2-20 路径类断言：必需产物齐备；密钥/用户数据绝不进包。"""
    missing = [rel for rel in REQUIRED_DIST_FILES if not (DIST / rel).is_file()]
    if missing:
        raise SystemExit(f"[build] ✗ 产物缺文件: {missing}")
    if gpu:
        for pkg in GPU_DLL_PACKAGES:
            bin_dir = DIST / "_internal" / "nvidia" / pkg / "bin"
            dlls = list(bin_dir.glob("*.dll")) if bin_dir.is_dir() else []
            if pkg in ("cublas", "cudnn") and not dlls:
                raise SystemExit(f"[build] ✗ GPU 版缺 {pkg} DLL（_internal/nvidia/{pkg}/bin）")
            print(f"[build] nvidia/{pkg}/bin: {len(dlls)} 个 DLL")
    for entry in FORBIDDEN_DIST_ENTRIES:
        if (DIST / entry).exists():
            raise SystemExit(f"[build] ✗ 产物含 {entry}（密钥/用户数据不得随包）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gpu", dest="gpu", action="store_false",
                    help="不打 GPU 运行库（cublas/cudnn DLL），纯 CPU 版")
    args = ap.parse_args()
    gpu = args.gpu

    snap = locate_model_snapshot(BUNDLE_MODEL)
    os.chdir(ROOT)  # 打包产物路径全部锚定仓库根（chdir 后与从根目录手跑等价）

    print("[build] ① PyInstaller 分析+收集（进程内，几分钟，耐心等）…")
    import PyInstaller.__main__ as pyi_main
    pyi_main.run(pyinstaller_args(gpu))

    print("[build] ② 模型随包复制（≈1.6GB）…")
    model_dest = DIST / "models" / BUNDLE_MODEL
    if model_dest.is_dir():
        shutil.rmtree(model_dest)
    shutil.copytree(snap, model_dest)

    print("[build] ③ 产物目录树断言…")
    assert_dist_tree(gpu)

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"[build] ✅ 完成: {DIST}")
    print(f"[build]    总大小 {total / 1e9:.2f} GB；启动实测（桌面壳）: "
          f"dist\\LectureLens\\LectureLens.exe")
    print(f"[build]    纯服务模式（LAN/手机访问）: "
          f"dist\\LectureLens\\LectureLens.exe --server-only --port 8798")


if __name__ == "__main__":
    main()
