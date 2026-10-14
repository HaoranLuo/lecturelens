# -*- coding: utf-8 -*-
"""build_macos.py — PyInstaller onedir + .app 打包（Mac 适配·第三批）。

**只在 macOS 上跑**：非 darwin 直接拒绝（见 main()）——这个脚本产出的是 .app 包、
执行的是 iconutil/codesign，在 Windows 上没有任何可做的有意义的事。纯函数部分
（参数构造 / Info.plist 补丁 / 产物断言）刻意与平台无关，故能在 Windows 上被
tests/test_macos_build.py 直接断言（免得「Mac 才能跑」变成「没人验得了」）。

用法（macOS）:
    .venv/bin/python scripts/build_macos.py              # 默认：构建 + ad-hoc 签名
    .venv/bin/python scripts/build_macos.py --no-sign    # 跳过签名（纯调试）
    .venv/bin/python scripts/build_macos.py --check-dist # 只校验既有 .app，不构建

产出 dist/LectureLens.app:
  Contents/MacOS/LectureLens          可执行（入口 = app/desktop.py，与 Windows 版同源；
                                      `--minimized` / `--server-only` / `--check` 全在这儿）
  Contents/Frameworks/…               PyInstaller 的 onedir 内容（= sys._MEIPASS）：Python
                                      运行时、ctranslate2/faster-whisper、webview 等
  Contents/Resources/web/…            只读数据页（--add-data web:web）——PyInstaller 把
                                      data-only 目录落在 Resources 并**交叉软链**到
                                      Frameworks（building/osx.py:188-190 / 414-421），
                                      所以 _MEIPASS/web 也读得到；断言两处任一存在即算齐
  Contents/Resources/glossary_packs/… B13 术语库包（同上，data-only）
  Contents/Info.plist                 标识/版本/麦克风用途说明（见 info_plist_patch）
  Contents/Resources/<icon>.icns      应用图标（assets/icon.icns；可由 PNG 生成）

**数据面不在 .app 里**（Mac 适配·第三批的硬规矩）：frozen 的 macOS 版把用户数据写在
`~/Library/Application Support/LectureLens`（app/main.py 的 `_frozen_root`）——.app 是
签名覆盖的只读包体，往包内写 config/笔记会破坏代码签名，也让「换新 .app」变成「数据
跟着丢」。模型也不随包（走 HF 缓存/下载，见 main.resolve_model_ref）。
故本脚本的产物断言里**没有** models/ 一项，也绝不允许 Contents/ 里出现 config.json 等。

图标（assets/icon.icns 不存在时脚本会尝试自动生成；也可手工执行）:
    mkdir -p /tmp/ll.iconset
    cp assets/icon_16.png  /tmp/ll.iconset/icon_16x16.png
    cp assets/icon_32.png  /tmp/ll.iconset/icon_16x16@2x.png
    cp assets/icon_32.png  /tmp/ll.iconset/icon_32x32.png
    cp assets/icon_64.png  /tmp/ll.iconset/icon_32x32@2x.png
    cp assets/icon_128.png /tmp/ll.iconset/icon_128x128.png
    cp assets/icon_256.png /tmp/ll.iconset/icon_128x128@2x.png
    cp assets/icon_256.png /tmp/ll.iconset/icon_256x256.png
    iconutil -c icns /tmp/ll.iconset -o assets/icon.icns
  （本机 assets 只到 256px，故 512/1024 两档用 256 顶替——图标边缘会略软，不影响使用；
  要更锐的图标就把 512/1024 的 PNG 补进 assets/ 再重跑。）

ad-hoc 签名（本脚本默认执行）: `codesign --force --deep -s - dist/LectureLens.app`
  —— 没有 Apple Developer 证书时的本机可用方案。**换一台 Mac / 走网盘传过去会被 Gatekeeper
  拦**（需右键打开、或 `xattr -dr com.apple.quarantine`）；公开分发要真证书 + 公证，
  那属于商业化项（红线外，见 docs/distribution-guide.md 的边界声明）。

已知边界（如实声明）: 本机（Windows）**无法实跑**本脚本——它的正确性由三处共同保证：
① 纯函数在 tests/test_macos_build.py 里断言；② PyInstaller 的 .app 布局引用了实源
（building/osx.py）；③ 产物断言在 Mac 上构建后立即跑（assert_app_tree）。
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "LectureLens"
DIST_APP = ROOT / "dist" / f"{APP_NAME}.app"
#: 入口 = 桌面壳（与 build_exe.py 同源；理由见该文件头部：自启命令写的 --minimized 只有它认）
ENTRY = ROOT / "app" / "desktop.py"
#: --add-data / --add-binary 的分隔符。macOS 是 ":"（Windows 是 ";"）。
#: **写死不从 os.pathsep 取**：这个脚本只在 macOS 跑，写死的结果在任何平台上都能被断言。
DATA_SEP = ":"
#: 版本号单一来源 = app/branding.py 的 APP_VERSION（与 Windows 版同一个口径）
BUNDLE_ID = "com.lecturelens.app"
#: Info.plist 里必须有的键（产物断言与单测都读它）
INFO_PLIST_KEYS = ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion",
                   "NSMicrophoneUsageDescription")
#: 麦克风用途说明（macOS 强制：没有这句，TCC 弹窗会说「未提供用途说明」并直接拒绝授权）
MIC_USAGE_DESCRIPTION = (
    "LectureLens 用麦克风采集课堂语音并实时生成双语字幕：音频只在本机用 "
    "faster-whisper 识别（不录音、不上传音频文件），识别出的原文才会发给 DeepSeek "
    "做翻译（可在设置页关掉翻译，改为纯英文字幕、完全不联网）。")

#: 用户数据目录（相对 ~）——必须与 app/main.py 的 _frozen_root('darwin') 一致
#: （tests/test_macos_build.py 有断言把两处钉在一起；这里再抄一份是为了让
#: verify_dist.py 能拿到「数据该落在哪」而不用 import 整个 main）。
MAC_DATA_DIR_REL = "Library/Application Support/LectureLens"

#: 必需产物（相对 .app 的路径）。data-only 目录（web/glossary_packs）由 PyInstaller 放在
#: Contents/Resources 并交叉软链到 Contents/Frameworks——断言时两处任一存在即算齐（见
#: assert_app_tree），但**列的是 Resources 那一份**（PyInstaller 的规范位置）。
REQUIRED_APP_FILES = (
    f"Contents/MacOS/{APP_NAME}",
    "Contents/Info.plist",
    "Contents/Resources/web/index.html",
    "Contents/Resources/web/float.html",     # 悬浮字幕条页（/float）
    "Contents/Resources/web/privacy.html",   # 隐私透明页（/privacy）
    # B13：术语库包（data-only 目录）。抽查一个文件即可证明整个目录被收进来——
    # 缺了它，打包版的术语库包区就是空的（开发机上看不出异常）。
    "Contents/Resources/glossary_packs/数学.md",
)
#: data-only 目录的交叉软链落点（PyInstaller 的 Frameworks 侧）：断言的第二候选根
FALLBACK_ROOT = "Contents/Frameworks"

#: 桌面壳依赖（darwin 侧）：与 build_exe.DESKTOP_SHELL_PACKAGES 的差别只在平台专属项——
#: pythonnet/clr_loader（Windows 的 .NET 桥）与 pyaudiowpatch（WASAPI）**不该**出现在
#: macOS 包里（装了也不存在），故这里不列。
DARWIN_SHELL_PACKAGES = (
    ("webview", "pywebview 窗口壳（cocoa 后端）"),
    ("pystray", "系统托盘（darwin 后端）"),
    ("PIL", "Pillow：品牌图标绘制与托盘状态灯"),
    ("qrcode", "手机同屏配对二维码（SVG 工厂）"),
    ("keyboard", "全局快捷键（darwin 后端 _darwinkeyboard）"),
    ("fpdf", "fpdf2：PDF 笔记导出"),
    ("docx", "python-docx：Word 笔记导出（含 templates/）"),
    ("genanki", "Anki 复习卡导出"),
)
#: 函数内惰性导入 + 平台动态挑后端的子模块（静态分析看不见分支里那一支）
DARWIN_HIDDEN_IMPORTS = (
    "webview.platforms.cocoa",     # webview.guilib 按平台动态选后端
    "keyboard._darwinkeyboard",    # keyboard 同上
    "pystray._darwin",             # pystray 同上
    "objc", "Foundation", "AppKit", "PyObjCTools",   # pyobjc 家族（webview/pystray 都要）
    "Quartz",                      # keyboard 的 darwin 后端用 Quartz 事件表
)
#: 装了才收的（MLX 后端）——没装时 PyInstaller 只会告警，但白名单式探测更干净：
#: 只有 find_spec 找得到才加 --hidden-import（缺了不影响构建，只是没有 MLX 加速）。
OPTIONAL_HIDDEN_IMPORTS = ("mlx_whisper", "mlx")
#: 官方 issue 351 清单（与 Windows 版一致：CPU 回落路径在 macOS 上同样要能跑）
CORE_COLLECT_ALL = ("ctranslate2", "faster_whisper", "huggingface_hub", "av")

#: 图标：优先用已生成的 .icns；没有就尝试由 assets/*.png 生成（见 build_icns）
ICON_ICNS = ROOT / "assets" / "icon.icns"
#: iconset 的 (iconset 文件名, assets 里的 PNG 名)——本机 assets 只到 256px，
#: 512/1024 两档用 256 顶替（iconutil 缺档会失败，故宁可放大也不要缺）。
ICONSET_MAP = (
    ("icon_16x16.png", "icon_16.png"),
    ("icon_16x16@2x.png", "icon_32.png"),
    ("icon_32x32.png", "icon_32.png"),
    ("icon_32x32@2x.png", "icon_64.png"),
    ("icon_128x128.png", "icon_128.png"),
    ("icon_128x128@2x.png", "icon_256.png"),
    ("icon_256x256.png", "icon_256.png"),
    ("icon_256x256@2x.png", "icon_256.png"),
    ("icon_512x512.png", "icon_256.png"),
    ("icon_512x512@2x.png", "icon_256.png"),
)


def app_version() -> str:
    """版本单一来源 = app/branding.py 的 APP_VERSION（复用 Windows 打包脚本的同一函数：
    两个脚本必须报同一个版本，抄一份就是给漂移留门）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_exe
    return build_exe.app_version()


def optional_hidden_imports() -> tuple:
    """装了才收的 hidden-import（见 OPTIONAL_HIDDEN_IMPORTS）。
    探测失败（模块不存在/环境异常）一律当「没装」——缺这个 import 只是没有 MLX 加速。"""
    import importlib.util
    out = []
    for mod in OPTIONAL_HIDDEN_IMPORTS:
        try:
            if importlib.util.find_spec(mod) is not None:
                out.append(mod)
        except Exception:
            continue
    return tuple(out)


def pyinstaller_args(icon: Path | None = None, extra_hidden: tuple = (),
                     version_file: Path | None = None) -> list[str]:
    """PyInstaller 参数列表（官方 API 的 pargs 形式，不含程序名）。
    与 build_exe.py 的结构一致，差别只在平台事实：":" 分隔符、--windowed（.app）、
    --osx-bundle-identifier、darwin 后端的 hidden-import、无 GPU DLL 收集。

    version_file 参数保留给测试/显式注入；macOS 的版本走 Info.plist（见 patch_info_plist），
    PyInstaller 的 --version-file 是 Windows 资源格式，macOS 上不适用。"""
    args = [
        "--noconfirm", "--clean",
        "--onedir", "--windowed",          # macOS：--windowed 才会产出 .app 包
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        "--name", APP_NAME,
        # D5：bundle id 决定代码签名与 TCC 授权的身份（改了它 = 用户要重新授权麦克风）
        "--osx-bundle-identifier", BUNDLE_ID,
        "--paths", str(ROOT / "app"),      # applog/asr/capture... 顶层导入的分析路径
        "--add-data", f"{ROOT / 'app' / 'web'}{DATA_SEP}web",
        # B13：术语库包（data-only 目录）→ Contents/Resources/glossary_packs
        "--add-data", f"{ROOT / 'glossary_packs'}{DATA_SEP}glossary_packs",
    ]
    if icon is not None and Path(icon).is_file():
        args += ["--icon", str(icon)]
    for pkg in CORE_COLLECT_ALL:
        args += ["--collect-all", pkg]
    for pkg, _why in DARWIN_SHELL_PACKAGES:
        args += ["--collect-all", pkg]
    for mod in (*DARWIN_HIDDEN_IMPORTS, *extra_hidden):
        args += ["--hidden-import", mod]
    args += [
        # uvicorn "auto" 协议为函数内惰性导入，显式点名防漏（装了才加，缺了无害）
        "--hidden-import", "uvicorn.protocols.http.httptools_impl",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.websockets_impl",
        "--hidden-import", "uvicorn.protocols.websockets.wsproto_impl",
        # 入口 = 桌面壳（同 Windows 版：--minimized/--server-only/--check 全由它分发）
        str(ENTRY),
    ]
    return args


def info_plist_patch(version: str) -> dict:
    """要补进 Contents/Info.plist 的键值（纯函数，便于单测）。

    - CFBundleShortVersionString / CFBundleVersion = APP_VERSION（Info.plist 的版本口径
      必须与 exe 属性/关于页同源；PyInstaller 默认写 "0.0.0"，所以必须补）；
    - NSMicrophoneUsageDescription：macOS 强制要求（缺了 TCC 弹窗直接拒绝授权），
      也是「隐私透明页」那段话的系统级落点；
    - CFBundleIdentifier 由 --osx-bundle-identifier 传，这里不改（改签名身份要重授权）。"""
    v = str(version)
    return {
        "CFBundleShortVersionString": v,
        "CFBundleVersion": v,
        "NSMicrophoneUsageDescription": MIC_USAGE_DESCRIPTION,
    }


def patch_info_plist(app_dir: Path | str, version: str) -> Path:
    """把 info_plist_patch 的键值合并进 .app 的 Info.plist（**保留** PyInstaller 写的
    其它键）。返回 Info.plist 路径；文件不存在 → FileNotFoundError（产物不完整）。"""
    plist = Path(app_dir) / "Contents" / "Info.plist"
    with plist.open("rb") as f:
        data = plistlib.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Info.plist 不是字典: {plist}")
    data.update(info_plist_patch(version))
    with plist.open("wb") as f:
        plistlib.dump(data, f)
    return plist


def build_icns(assets: Path | None = None, out: Path | None = None,
               runner=None, workdir: Path | None = None) -> Path | None:
    """由 assets/*.png 生成 .icns（调 macOS 的 iconutil）。

    返回生成的 .icns 路径；**任何失败都返回 None**（图标是装饰性的，绝不能拦住构建）——
    调用方据此退回 PyInstaller 自带的默认窗口图标。
    runner 可注入（单测在 Windows 上断言命令 argv 与 iconset 映射，不真跑 iconutil）。"""
    assets = Path(assets or (ROOT / "assets"))
    out = Path(out or ICON_ICNS)
    run = runner or subprocess.run
    if out.is_file():
        return out
    if sys.platform != "darwin" and runner is None:
        return None                      # 非 macOS 本就没有 iconutil
    try:
        work = Path(workdir or tempfile.mkdtemp(prefix="lecturelens-iconset-"))
        iconset = work / "ll.iconset"
        iconset.mkdir(parents=True, exist_ok=True)
        for name, src_name in ICONSET_MAP:
            src = assets / src_name
            if not src.is_file():
                print(f"[build] ⚠ 图标源缺失 {src.name}（iconset 第 {name} 档跳过）")
                continue
            shutil.copyfile(src, iconset / name)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 固定 argv、不经 shell（无注入面）
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
            check=True, capture_output=True, timeout=120)
        if out.is_file():
            print(f"[build] 图标已生成: {out}")
            return out
    except Exception as e:
        print(f"[build] ⚠ iconutil 生成 .icns 失败（{type(e).__name__}: {e}）"
              "——改用 PyInstaller 默认图标（图标不影响功能）")
    return None


def codesign_ad_hoc(app_dir: Path | str, runner=None) -> tuple:
    """ad-hoc 签名（codesign --force --deep -s -）。返回 (返回码, 输出)。

    为什么 `-s -`：没有开发者证书时，macOS 仍要求可执行的 arm64 代码有签名——ad-hoc
    是本机可用的最小方案。固定 argv、不经 shell；失败**不算构建失败**（如实打一行），
    但产物断言照跑（签名只影响「别的 Mac 上首次打开」）。"""
    run = runner or subprocess.run
    app = str(app_dir)
    try:
        p = run(["codesign", "--force", "--deep", "--sign", "-", app],
                capture_output=True, text=True, timeout=600)
        return int(p.returncode), ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 127, f"{type(e).__name__}: {e}"


def _present(app_dir: Path, rel: str) -> bool:
    """rel 在 .app 里存在（Resources 优先，Frameworks 交叉软链兜底——见模块头的布局说明）。"""
    if (Path(app_dir) / rel).exists():
        return True
    prefix = "Contents/Resources/"
    if rel.startswith(prefix):
        alt = FALLBACK_ROOT + "/" + rel[len(prefix):]
        return (Path(app_dir) / alt).exists()
    return False


def assert_app_tree(app_dir: Path | str = DIST_APP) -> None:
    """产物断言：必需文件齐备 + Contents/ 里**没有用户数据/密钥**。

    「没有用户数据」这条与 Windows 版同源（直接复用 build_exe.forbidden_hits——
    清单只有一份）：.app 被「原地跑过一次」留下的 config.json/logs/transcripts
    正是最容易被顺手发出去的东西。任一项不过 → SystemExit（不产出半成品）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_exe
    app = Path(app_dir)
    if not app.is_dir():
        raise SystemExit(f"[build] ✗ 找不到产物 {app}（先跑 scripts/build_macos.py）")
    missing = [rel for rel in REQUIRED_APP_FILES if not _present(app, rel)]
    if missing:
        raise SystemExit(f"[build] ✗ .app 缺文件: {missing}")
    for rel in REQUIRED_APP_FILES:
        where = "Resources/Frameworks"
        if not (app / rel).exists() and _present(app, rel):
            where = "Frameworks（交叉软链）"
        print(f"[build] 产物 ✓ {rel}（{where}）")
    hits = build_exe.forbidden_hits(app)
    if hits:
        raise SystemExit(
            f"[build] ✗ .app 内出现用户数据/密钥（{len(hits)} 处）: {hits[:8]}"
            f"——frozen 的 macOS 版把数据写在 ~/{MAC_DATA_DIR_REL}"
            f"（见 app/main.py 的 _frozen_root），包内绝不该有这些；"
            f"若在 .app 里跑过一次应用，请删掉重跑本脚本")
    total = sum(1 for _ in app.rglob("*"))
    print(f"[build] 用户数据/密钥扫描 ✓ {total} 个路径干净"
          f"（清单 {list(build_exe.FORBIDDEN_DIST_ENTRIES)}）")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="LectureLens macOS .app 打包（PyInstaller onedir + ad-hoc 签名）")
    ap.add_argument("--no-sign", dest="sign", action="store_false",
                    help="跳过 ad-hoc 签名（调试用；正式本机使用建议签）")
    ap.add_argument("--check-dist", action="store_true",
                    help="只对既有 dist/LectureLens.app 跑产物断言（不构建）")
    return ap.parse_args(argv)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            f"[build] ✗ 本脚本只在 macOS 上跑（当前 {sys.platform}）；"
            f"Windows 用 scripts/build_exe.py，产物是 dist/LectureLens/LectureLens.exe")
    args = parse_args()
    import build_exe          # 同目录的 Windows 构建脚本：复用它的两份事实源
                              # （prune_tool_state + FORBIDDEN_DIST_ENTRIES），不另抄一份

    if args.check_dist:
        print(f"[build] 只校验既有产物 {DIST_APP} …")
        assert_app_tree(DIST_APP)
        print(f"[build] ✅ 产物断言全过: {DIST_APP}")
        return

    version = app_version()
    icon = build_icns()
    extra = optional_hidden_imports()
    if extra:
        print(f"[build] 可选 hidden-import 已收: {list(extra)}")

    print("[build] ① PyInstaller 分析+收集（进程内，几分钟，耐心等）…")
    os.chdir(ROOT)   # 产物路径全部锚定仓库根（chdir 后与从根目录手跑等价）
    import PyInstaller.__main__ as pyi_main
    pyi_main.run(pyinstaller_args(icon, extra))

    # ①b：与 Windows 侧同一个 bug、同一份修法——`--add-data` 是整目录复制，会把源目录
    # 里的工具态目录（.mimosa/.zcode）搬进 Contents/Resources/。复用 build_exe 的实现
    # （只读它一个函数；本文件的 --add-data 口径与那边一致）。修在断言**之前**，
    # 否则会被 build_exe.FORBIDDEN_DIST_ENTRIES 的黑名单扫描判红。
    print("[build] ①b 摘掉工具态目录（--add-data 整目录复制带进来的）…")
    _pruned = build_exe.prune_tool_state(DIST_APP)
    print(f"[build]    摘掉 {len(_pruned)} 处" + (f": {_pruned}" if _pruned else "（无）"))

    print(f"[build] ② Info.plist 注入（版本 {version} + 麦克风用途说明）…")
    patch_info_plist(DIST_APP, version)

    if args.sign:
        print("[build] ③ ad-hoc 签名（codesign --force --deep -s -）…")
        rc, out = codesign_ad_hoc(DIST_APP)
        if rc != 0:
            print(f"[build] ⚠ 签名未成功（{rc}）: {out[:400]}"
                  "\n[build]   不影响本机右键打开；换机器分发会被 Gatekeeper 拦")

    print("[build] ④ 产物断言（.app 结构 + 用户数据扫描）…")
    assert_app_tree(DIST_APP)

    total = sum(f.stat().st_size for f in DIST_APP.rglob("*") if f.is_file())
    print(f"[build] ✅ 完成: {DIST_APP}")
    print(f"[build]    总大小 {total / 1e9:.2f} GB；版本 {version}；bundle id {BUNDLE_ID}")
    print(f"[build]    用户数据目录（首次运行自动创建）: ~/{MAC_DATA_DIR_REL}")
    print("[build]    启动实测: open dist/LectureLens.app"
          "（或 dist/LectureLens.app/Contents/MacOS/LectureLens）")
    print("[build]    无窗口自检（门禁）: "
          "dist/LectureLens.app/Contents/MacOS/LectureLens --check")
    print("[build]    纯服务模式: "
          "dist/LectureLens.app/Contents/MacOS/LectureLens --server-only --port 8798")


if __name__ == "__main__":
    main()
