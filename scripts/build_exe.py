# -*- coding: utf-8 -*-
"""build_exe.py — PyInstaller onedir 打包（D1 重写：桌面壳依赖齐备 + 版本注入 + 随包 small）。

用法:
    .venv/Scripts/python.exe scripts/build_exe.py            # 默认：GPU 运行库 + 随包 small（≈460MB）
    .venv/Scripts/python.exe scripts/build_exe.py --no-gpu   # CPU 版（不打 cublas/cudnn DLL）
    .venv/Scripts/python.exe scripts/build_exe.py --bundle-model large-v3-turbo   # 换随包模型
    .venv/Scripts/python.exe scripts/build_exe.py --check-dist                    # 只校验既有 dist，不构建

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
  - **D1① 桌面壳依赖显式收集**：窗口壳/托盘/图标/二维码/热键/三种导出（PDF/Word/
    Anki）WASAPI 环回——这些包**只在函数内惰性导入**（导入即弹窗/装钩子，不能进
    模块顶层），单靠静态分析会漏。清单见 DESKTOP_SHELL_PACKAGES / HIDDEN_IMPORTS；
    实测：D1 之前的 dist/_internal 里 webview/PIL/pystray/qrcode/keyboard/fpdf/
    docx/genanki/pythonnet/clr_loader **一个都没有**。构建后逐项抽查
    （DESKTOP_SHELL_ARTIFACTS，缺一即构建失败）。
  - **D1② 版本注入**：版本号唯一来源 = app/branding.py 的 APP_VERSION，构建时
    生成 PyInstaller `--version-file`（VSVersionInfo）写进 exe 属性——资源管理器
    「详细信息」的 FileVersion/ProductVersion = 1.0.0。
  - **D1③ 产物断言**补 float.html / privacy.html：桌面壳新增的两个页面
    （悬浮字幕条 / 隐私透明页）必须进 `_internal/web/`。
  - **D1④ 随包模型默认 small**（≈460MB，任何机器装完开箱可用）；更高的档位走既有
    档位下载（docs/notes-batch.md D3）。`--bundle-model` 可覆盖（须已在 HF 缓存），
    例如有 N 卡的机器可打 large-v3-turbo 完整版。frozen 下 main.bundled_model_path
    解析为本地路径传 WhisperModel；main.py 在 frozen 时设 HF_HUB_OFFLINE=1
    （随包模型离线可用，见 D2 条目）。
  - 官方清单（faster-whisper issue 351）: --collect-all ctranslate2 / faster_whisper /
    huggingface_hub；av 一并 collect（PyAV 自带 ffmpeg DLL）；
  - GPU 版: venv 的 nvidia DLL 按 site-packages 原布局收集进
    _internal/nvidia/<pkg>/bin —— asr._setup_cuda_dlls 的 frozen 分支用同一套
    glob 在 _MEIPASS 下查找，开发/打包同一代码路径；
  - 产物目录树断言（§2.2-20 路径类断言）: exe / web 页 / 模型 / GPU DLL / 桌面壳依赖
    齐备，且产物无 .env / config.json / transcripts（密钥与用户数据绝不进包）。

调用方式: 进程内调 PyInstaller.__main__.run（官方 API）——不经 shell、
不经 subprocess，无命令注入面（Mimosa 安全口径）。
已知边界（如实声明）: 干净机器双击实测、朋友机分发实测是 §2.2-20 的手动清单，
不在本脚本自动化范围（本机无干净 Windows 环境）。
"""
import argparse
import os
import re
import shutil
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "LectureLens"
BUNDLE_MODEL = "small"                   # D1④ 随包模型（≈460MB）；turbo 等档位走下载
GPU_DLL_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")  # venv nvidia 目录（存在才收）
#: 生成的版本文件落点：**必须放在 build/ 直属下**——PyInstaller 的 --clean 会清空
#: workpath（= build/<specname>，见 PyInstaller/building/build_main.py:1160-1169），
#: 放进去会被构建开始时抹掉；build/ 本身已在 .gitignore 里。
VERSION_FILE = ROOT / "build" / "version_info.txt"

#: D1③ 必需产物（模型文件按实际随包名拼，见 required_dist_files）
REQUIRED_DIST_FILES = (
    "LectureLens.exe",
    "_internal/web/index.html",
    "_internal/web/float.html",    # 悬浮字幕条页（/float）
    "_internal/web/privacy.html",  # 隐私透明页（/privacy）
)
#: D1③ / D3 评审修复：**用户数据/密钥清单**——凡在分发产物里出现即构建失败。
#: 口径 = 「运行期由 ROOT 锚定写盘的东西」（ROOT = exe 所在目录，见 main.ROOT）：
#:   config.json（配置）/ courses.json（课程表，main.py:52）/ .env（向导写 key，
#:   main.py:1593）/ transcripts（笔记）/ logs（日志）/ glossary（术语表，
#:   glossary.py:53 root=ROOT）/ desktop.lock（单实例锁，desktop.py:87）
#: **必须与运行期写盘点保持一致**：评审2 实测指出 glossary 与 courses.json 曾漏在
#: 清单外——「在打包目录里跑过一次/建过一门课」留下的东西就能混进分发目录。
FORBIDDEN_DIST_ENTRIES = (".env", "config.json", "courses.json",
                          "transcripts", "logs", "glossary", "desktop.lock")
#: 后缀黑名单：日志类残留（含 server_smoke.log 这类运行痕迹）一律不得进包
FORBIDDEN_DIST_SUFFIXES = (".log",)
#: 例外白名单（**只此一处**）：models/<name>/ 下**模型自带**的文件——HF 快照原样
#: 复制过来的 faster-whisper 文件里就有 config.json（与用户 config.json 同名不同物，
#: 实测 dist/LectureLens/models/small/config.json）。除此之外任何位置同名即命中。
MODEL_OWNED_ALLOWED = ("config.json",)

#: D1① 桌面壳依赖：**PyInstaller --collect-all 显式点名**（包名, 用途）。
#: 为什么不能只靠静态分析：它们全在函数内惰性导入（导入即弹窗/装系统钩子/选平台后端），
#: 旧构建漏收 → 打包版没有窗口壳也没有托盘。collect-all 同时收数据文件（webview 的
#: lib/ WebView2 DLL 与 js/ 桥脚本、docx 模板、fpdf 数据）与全部子模块。
DESKTOP_SHELL_PACKAGES = (
    ("webview", "pywebview 窗口壳（lib/ WebView2 DLL + js/ 注入脚本）"),
    ("pythonnet", "webview WinForms 后端的 .NET 桥（runtime/Python.Runtime.dll）"),
    ("clr_loader", "pythonnet 的运行时加载器"),
    ("pystray", "系统托盘（图标 + 菜单 + 退出通道）"),
    ("PIL", "Pillow：品牌图标绘制与托盘状态灯（branding.draw_app_icon）"),
    ("qrcode", "手机同屏配对二维码（lan.qr_svg 的 SVG 工厂）"),
    ("keyboard", "全局快捷键（hotkeys.py，Windows 后端 _winkeyboard）"),
    ("fpdf", "fpdf2：PDF 笔记导出（exporters）"),
    ("docx", "python-docx：Word 笔记导出（exporters，含 templates/）"),
    ("genanki", "Anki 复习卡导出（ankideck.build_deck）"),
    ("pyaudiowpatch", "WASAPI 环回采集（loopback.py，系统声音源）"),
)

#: D1① 函数内惰性导入的子模块——包名层面收不到的用 hidden-import 点名
#: （平台后端是运行时按 sys.platform 动态挑的，静态分析看不见分支里的那一支）。
HIDDEN_IMPORTS = (
    "webview.platforms.winforms",     # Windows 实际后端（webview.guilib 动态选平台）
    "webview.platforms.edgechromium",  # WebView2 渲染器（同上，两者配套）
    "clr",                            # pythonnet 的导入名（发行包名是 pythonnet）
    "qrcode.image.svg",               # lan.qr_svg 只要 SVG 工厂，避开 PIL 依赖
    "keyboard._winkeyboard",          # keyboard 按平台动态挑后端
)

#: D1① 构建后抽查（包名, 说明, 相对 _internal 的 glob）——每项至少命中 1 个路径，
#: 否则**构建失败**（不产出半成品）。包名与 DESKTOP_SHELL_PACKAGES 一一对应
#: （test_build_exe 有断言钉住：漏写哪一项抽查，测试立刻红）。
DESKTOP_SHELL_ARTIFACTS = (
    ("webview", "pywebview 包", "webview/__init__.py"),
    ("webview", "WebView2 桥 DLL", "webview/lib/*.dll"),
    ("pythonnet", "pythonnet .NET 桥", "pythonnet/runtime/Python.Runtime.dll"),
    ("clr_loader", "clr_loader", "clr_loader/__init__.py"),
    ("pystray", "pystray", "pystray/_win32.py"),
    ("PIL", "Pillow（含 C 扩展）", "PIL/_imaging*.pyd"),
    ("qrcode", "qrcode（SVG 工厂）", "qrcode/image/svg.py"),
    ("keyboard", "keyboard（Windows 后端）", "keyboard/_winkeyboard.py"),
    ("fpdf", "fpdf2", "fpdf/fpdf.py"),
    ("docx", "python-docx（模板）", "docx/templates/default.docx"),
    ("genanki", "genanki", "genanki/package.py"),
    ("pyaudiowpatch", "pyaudiowpatch（环回采集扩展）", "**/_portaudiowpatch*.pyd"),
)

_VER_CORE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?\s*$")


def app_version() -> str:
    """版本单一来源 = app/branding.py 的 APP_VERSION（D1②：打包不另立版本口径）。"""
    if str(ROOT / "app") not in sys.path:
        sys.path.insert(0, str(ROOT / "app"))
    import branding  # 惰性导入：branding 的 PIL 是函数内导入，此处不拉图形栈
    return branding.APP_VERSION


def version_quad(version: str) -> tuple:
    """'1.0.0' → (1, 0, 0, 0)；Windows 版本资源固定四段数字。
    先剥 -dev / +build 之类的后缀（语义段不进数字），再要求余下是纯数字点分。"""
    core = version.split("-", 1)[0].split("+", 1)[0]
    m = _VER_CORE.match(core)
    if not m:
        raise SystemExit(f"[build] ✗ 版本号无法转成 Windows 四段版本: {version!r}")
    return tuple(int(g) if g else 0 for g in m.groups())


def version_info_text(version: str) -> str:
    """D1②：生成 PyInstaller --version-file 的内容（VSVersionInfo 的 Python 字面量，
    PyInstaller 用 eval() 读，见 utils/win32/versioninfo.py 的
    load_version_info_from_text_file）。**内容全 ASCII**：版本资源走 Windows
    codepage 表，写中文容易在别的机器上乱码；中文名称留在关于页（前端）里。
    StringTable '040904B0' = en-US + Unicode(1200)，与 VarFileInfo 的 Translation
    对应；FileVersion/ProductVersion 是资源管理器「详细信息」里显示的字符串。"""
    quad = version_quad(version)
    fields = (
        ("CompanyName", "LectureLens"),
        ("FileDescription", "LectureLens - lecture subtitle translator & note taker"),
        ("FileVersion", version),
        ("InternalName", "LectureLens"),
        ("LegalCopyright", "LectureLens (private build, not for redistribution)"),
        ("OriginalFilename", "LectureLens.exe"),
        ("ProductName", "LectureLens"),
        ("ProductVersion", version),
    )
    strings = ",\n".join(
        f"            StringStruct({name!r}, {value!r})" for name, value in fields)
    return (
        "# UTF-8\n"
        "# Generated by scripts/build_exe.py (do not edit by hand).\n"
        "# Version source: APP_VERSION in app/branding.py; PyInstaller parses\n"
        "# this file with eval() (--version-file). Keep it ASCII-only.\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={quad},\n"
        f"    prodvers={quad},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "    ),\n"
        "  kids=[\n"
        "    StringFileInfo(\n"
        "      [\n"
        "      StringTable(\n"
        "        '040904B0',\n"
        "        [" + strings + "]\n"
        "        )]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )


def write_version_file(version: str = None, path=None) -> Path:
    """把版本资源写盘，返回路径（默认 build/version_info.txt；见 VERSION_FILE 注释）。"""
    out = Path(path or VERSION_FILE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(version_info_text(version or app_version()), encoding="utf-8")
    return out


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


def pyinstaller_args(gpu: bool, version_file=None) -> list[str]:
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
        # D1②：版本资源（exe 属性 FileVersion/ProductVersion = branding.APP_VERSION）
        "--version-file", str(version_file or VERSION_FILE),
        "--paths", str(ROOT / "app"),      # applog/asr/capture... 顶层导入的分析路径
        "--add-data", f"{ROOT / 'app' / 'web'};web",
        # 官方 issue 351 清单 + av 保底
        "--collect-all", "ctranslate2",
        "--collect-all", "faster_whisper",
        "--collect-all", "huggingface_hub",
        "--collect-all", "av",
    ]
    # D1①：桌面壳依赖（清单与理由见 DESKTOP_SHELL_PACKAGES）
    for pkg, _why in DESKTOP_SHELL_PACKAGES:
        args += ["--collect-all", pkg]
    # D1①：函数内惰性导入的子模块（平台后端等，静态分析看不见的那一支）
    for mod in HIDDEN_IMPORTS:
        args += ["--hidden-import", mod]
    args += [
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


def required_dist_files(bundle_model: str = BUNDLE_MODEL) -> tuple:
    """D1③④：必需产物清单（web 页 + 实际随包模型）。"""
    return (*REQUIRED_DIST_FILES, f"models/{bundle_model}/model.bin")


def model_owned_path(rel: Path) -> bool:
    """`models/<name>/<模型自带文件>` —— 唯一允许与用户数据同名的位置。

    例外必须**精确**：只在 models/ 之下（≥3 段）、且只有模型快照自带的文件名。
    顶层 config.json / _internal/config.json 都不满足该式，仍会被命中。"""
    return (len(Path(rel).parts) >= 3 and Path(rel).parts[0] == "models"
            and Path(rel).name in MODEL_OWNED_ALLOWED)


def forbidden_hits(root: Path) -> list:
    """递归找出分发产物里的用户数据/密钥：**任意层级**的名字命中 FORBIDDEN_DIST_ENTRIES
    或后缀命中 FORBIDDEN_DIST_SUFFIXES（唯一例外见 model_owned_path）。

    返回「相对 root 的路径」列表（空 = 干净）。评审2：旧实现只看顶层存在性，
    `_internal/glossary/…` 这类嵌套泄漏与顶层 glossary/ 一并由本函数兜住。"""
    root = Path(root)
    hits = []
    try:
        paths = list(root.rglob("*"))
    except OSError:
        return []
    for p in paths:
        rel = p.relative_to(root)
        if model_owned_path(rel):
            continue
        if p.name in FORBIDDEN_DIST_ENTRIES or p.suffix.lower() in FORBIDDEN_DIST_SUFFIXES:
            hits.append(str(rel))
    return hits


def assert_dist_tree(gpu: bool, bundle_model: str = BUNDLE_MODEL) -> None:
    """§2.2-20 路径类断言：必需产物齐备 + D1① 桌面壳依赖抽查；
    密钥/用户数据绝不进包。任一项缺失 → SystemExit（构建失败，不产出半成品）。"""
    missing = [rel for rel in required_dist_files(bundle_model)
               if not (DIST / rel).is_file()]
    if missing:
        raise SystemExit(f"[build] ✗ 产物缺文件: {missing}")
    internal = DIST / "_internal"
    for _pkg, label, pattern in DESKTOP_SHELL_ARTIFACTS:
        hits = sorted(internal.glob(pattern))
        if not hits:
            raise SystemExit(
                f"[build] ✗ 桌面壳依赖缺失: {label}（_internal/{pattern} 无命中）"
                "——检查 DESKTOP_SHELL_PACKAGES / HIDDEN_IMPORTS 收集清单")
        print(f"[build] 桌面壳依赖 ✓ {label}: {len(hits)} 项（{pattern}）")
    if gpu:
        for pkg in GPU_DLL_PACKAGES:
            bin_dir = internal / "nvidia" / pkg / "bin"
            dlls = list(bin_dir.glob("*.dll")) if bin_dir.is_dir() else []
            if pkg in ("cublas", "cudnn") and not dlls:
                raise SystemExit(f"[build] ✗ GPU 版缺 {pkg} DLL（_internal/nvidia/{pkg}/bin）")
            print(f"[build] nvidia/{pkg}/bin: {len(dlls)} 个 DLL")
    # D3 评审修复②：用户数据/密钥**深扫任意层级**。旧实现只看顶层存在性——
    # `glossary/`、`courses.json` 当年甚至不在清单里，而「在打包目录里跑过一次」
    # 正是它们出现的路径（向导写 .env 的同一道理）。错误信息给出命中路径，
    # 并指向最常见成因：打包前在 dist 目录里跑过应用/建过课。
    hits = forbidden_hits(DIST)
    if hits:
        raise SystemExit(
            f"[build] ✗ 产物含用户数据/密钥（{len(hits)} 处）: {hits[:8]}"
            f"——运行期 ROOT 锚定的东西绝不进包；若在打包目录里跑过应用/建过课，"
            f"请先清掉（或直接重跑本脚本覆盖 dist）再做断言")
    print(f"[build] 用户数据/密钥扫描 ✓ 全树 {len(list(DIST.rglob('*')))} 个路径干净"
          f"（清单 {list(FORBIDDEN_DIST_ENTRIES)} + 后缀 {list(FORBIDDEN_DIST_SUFFIXES)}）")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="LectureLens PyInstaller onedir 打包（D1）")
    ap.add_argument("--no-gpu", dest="gpu", action="store_false",
                    help="不打 GPU 运行库（cublas/cudnn DLL），纯 CPU 版")
    ap.add_argument("--bundle-model", default=BUNDLE_MODEL, metavar="NAME",
                    help=f"随包模型名（须已在 HF 缓存；默认 {BUNDLE_MODEL}，"
                         f"≈460MB 开箱可用；更快的档位走档位下载）")
    ap.add_argument("--check-dist", action="store_true",
                    help="只对既有 dist 跑产物断言（不构建；供门禁/verify_dist 复用）")
    return ap.parse_args(argv)


def main() -> None:
    args = parse_args()
    gpu = args.gpu
    bundle_model = args.bundle_model

    if args.check_dist:
        print(f"[build] 只校验既有产物（bundle_model={bundle_model}, gpu={gpu}）…")
        assert_dist_tree(gpu, bundle_model)
        print(f"[build] ✅ 产物断言全过: {DIST}")
        return

    version = app_version()
    vf = write_version_file(version)
    print(f"[build] 版本注入: {version} → {vf}（exe 属性 FileVersion/ProductVersion）")
    snap = locate_model_snapshot(bundle_model)
    os.chdir(ROOT)  # 打包产物路径全部锚定仓库根（chdir 后与从根目录手跑等价）

    print("[build] ① PyInstaller 分析+收集（进程内，几分钟，耐心等）…")
    import PyInstaller.__main__ as pyi_main
    pyi_main.run(pyinstaller_args(gpu, vf))

    print(f"[build] ② 模型随包复制（{bundle_model}）…")
    model_dest = DIST / "models" / bundle_model
    if model_dest.is_dir():
        shutil.rmtree(model_dest)
    shutil.copytree(snap, model_dest)

    print("[build] ③ 产物目录树断言（含桌面壳依赖抽查）…")
    assert_dist_tree(gpu, bundle_model)

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"[build] ✅ 完成: {DIST}")
    print(f"[build]    总大小 {total / 1e9:.2f} GB；版本 {version}；随包模型 {bundle_model}")
    print(f"[build]    启动实测（桌面壳）: dist\\LectureLens\\LectureLens.exe")
    print(f"[build]    无窗口自检（门禁）: "
          f"dist\\LectureLens\\LectureLens.exe --check")
    print(f"[build]    纯服务模式（LAN/手机访问）: "
          f"dist\\LectureLens\\LectureLens.exe --server-only --port 8798")


if __name__ == "__main__":
    main()
