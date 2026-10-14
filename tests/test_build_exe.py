# -*- coding: utf-8 -*-
"""tests/test_build_exe.py — D1（打包脚本重写，docs/notes-batch.md D 组）回归。

钉死四件事：
① 桌面壳依赖的**显式收集清单**与**构建后抽查清单**必须一一对应——这些包只在
   desktop.py / exporters.py 的函数内惰性导入，静态分析会漏。修前取证（本机既有
   dist 实测）：`dist/LectureLens/_internal/` 里 webview / PIL / pystray / qrcode /
   keyboard / fpdf / docx / genanki / pythonnet / clr_loader **一个目录都没有**——
   打包版没有窗口壳、没有托盘、没有二维码。缺哪一项，构建就该当场红。
② 版本注入：exe 版本资源由 branding.APP_VERSION 生成，生成物必须能被 PyInstaller
   自己的解析器读回（load_version_info_from_text_file，eval 路径），且落在
   `--clean` 不会抹掉的位置。
③ REQUIRED_DIST_FILES 补 float.html / privacy.html（桌面壳两个新页面）。
④ 随包模型默认 small，`--bundle-model` 可覆盖，产物断言跟着实际模型名走。

全是**纯逻辑**：不跑 PyInstaller、不碰真实 dist（构建分钟级，归 scripts/build_exe.py
实跑与 D3 产物探针）。产物断言的真假由 tmp_path 里的假 dist 驱动。
"""
from pathlib import Path

import pytest

import branding
import build_exe


# ---------------------------------------------------------------- ② 版本注入

def test_app_version_is_release_1_0_0():
    """D1②：定真版本 1.0.0（首个安装包的版本口径，不再是 -dev 语义）。
    打包脚本不另立版本——它必须读 branding.APP_VERSION（单一事实源）。"""
    assert branding.APP_VERSION == "1.0.0"
    assert build_exe.app_version() == branding.APP_VERSION


def test_version_quad_parses_release_and_strips_suffix():
    """Windows 版本资源固定四段数字；语义后缀（-dev/+build）先剥离再取数字段。"""
    assert build_exe.version_quad("1.0.0") == (1, 0, 0, 0)
    assert build_exe.version_quad("2.13") == (2, 13, 0, 0)
    assert build_exe.version_quad("1.2.3.4") == (1, 2, 3, 4)
    assert build_exe.version_quad("0.6.0-dev") == (0, 6, 0, 0)   # 旧口径也认
    assert build_exe.version_quad("1.2.3+build.5") == (1, 2, 3, 0)
    with pytest.raises(SystemExit):
        build_exe.version_quad("banana")          # 解析不了要炸，不能默默写 0.0.0.0


def test_version_file_reads_back_as_1_0_0(tmp_path):
    """生成物必须能被 PyInstaller 的解析器原样读回（版本资源经 eval 解析），
    且内容保持纯 ASCII（版本资源走 Windows codepage 表，中文在别的机器上会乱码）。"""
    from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file

    text = build_exe.version_info_text(branding.APP_VERSION)
    assert all(ord(c) < 128 for c in text), "版本资源文件必须纯 ASCII"

    path = build_exe.write_version_file(branding.APP_VERSION,
                                        tmp_path / "version_info.txt")
    assert path.is_file()
    info = load_version_info_from_text_file(str(path))
    strings = {}
    for table in info.kids[0].kids:          # StringFileInfo → StringTable
        for s in table.kids:
            strings[s.name] = s.val
    assert strings["FileVersion"] == "1.0.0", "资源管理器「详细信息」的 FileVersion"
    assert strings["ProductVersion"] == "1.0.0"
    assert strings["OriginalFilename"] == "LectureLens.exe"
    # ffi 里的四段版本（filevers/prodvers）也必须是 1.0.0.0
    quad = (info.ffi.fileVersionMS >> 16, info.ffi.fileVersionMS & 0xFFFF,
            info.ffi.fileVersionLS >> 16, info.ffi.fileVersionLS & 0xFFFF)
    assert quad == (1, 0, 0, 0)
    assert (info.ffi.productVersionMS, info.ffi.productVersionLS) == \
        (info.ffi.fileVersionMS, info.ffi.fileVersionLS)


def test_version_file_wired_into_args_and_outside_clean_path():
    """`--version-file` 指向生成物；且落点不能在 `--clean` 会清空的 workpath
    子目录里（PyInstaller 的 build_main.py:1160-1169 清的是 build/<specname>）。"""
    args = build_exe.pyinstaller_args(False)
    assert "--version-file" in args
    vf = Path(args[args.index("--version-file") + 1])
    assert vf == build_exe.VERSION_FILE
    workpath = Path(args[args.index("--workpath") + 1])
    assert vf.parent == workpath and vf.parent.name == "build", \
        "版本文件必须在 build/ 直属下（--clean 清的是 workpath=build/LectureLens）"


# ---------------------------------------------------------------- ① 桌面壳依赖

def test_pyinstaller_args_keep_onedir_and_assets_contract():
    """既有结构不动：onedir（CTranslate2 不支持 onefile）/ console / 图标 /
    web 页面随包 / 入口仍是桌面壳（自启命令要它解析 --minimized）。"""
    args = build_exe.pyinstaller_args(False)
    assert "--onedir" in args and "--console" in args
    assert args[args.index("--name") + 1] == "LectureLens"
    assert args[args.index("--icon") + 1] == str(build_exe.ROOT / "assets" / "icon.ico")
    assert f"{build_exe.ROOT / 'app' / 'web'};web" in args
    # 入口（pargs 的最后一个位置参数）= app/desktop.py
    assert args[-1] == str(build_exe.ROOT / "app" / "desktop.py")


def test_desktop_shell_deps_collected_and_spot_checked():
    """D1①：每个显式收集的包都要有 --collect-all；构建后抽查清单必须覆盖同一批包
    （收集了但没抽查 = 缺依赖会悄悄过；抽查了但没收集 = 构建必红）。"""
    args = build_exe.pyinstaller_args(False)
    collect_all = {args[i + 1] for i, a in enumerate(args) if a == "--collect-all"}
    hidden = {args[i + 1] for i, a in enumerate(args) if a == "--hidden-import"}
    packages = [pkg for pkg, _why in build_exe.DESKTOP_SHELL_PACKAGES]

    for pkg in packages:
        assert pkg in collect_all, f"桌面壳依赖 {pkg} 必须显式收集（否则打包版缺功能）"
    # 核心几件单独点名：防有人把清单删空还"自洽"
    assert {"webview", "pythonnet", "clr_loader", "pystray", "PIL", "qrcode",
            "keyboard", "fpdf", "docx", "genanki"} <= set(packages)
    # 函数内惰性导入的子模块（平台后端/导入名与包名不同者）
    assert {"webview.platforms.winforms", "clr", "qrcode.image.svg"} <= hidden
    # 抽查清单与收集清单一一对应（包名集合相等）
    checked = {pkg for pkg, _label, _pat in build_exe.DESKTOP_SHELL_ARTIFACTS}
    assert checked == set(packages), \
        "构建后抽查清单必须与收集清单一一对应（漏一处 → 缺依赖也悄悄过）"


def _full_dist(tmp_path: Path, bundle_model: str = "small") -> Path:
    """最小"全绿"假 dist：必需产物齐备（模型名可换），模型文件带 model.bin。"""
    dist = tmp_path / "LectureLens"
    (dist / "_internal" / "web").mkdir(parents=True)
    (dist / "LectureLens.exe").write_bytes(b"stub")
    for page in ("index.html", "float.html", "privacy.html"):
        (dist / "_internal" / "web" / page).write_text("<html></html>", encoding="utf-8")
    (dist / "models" / bundle_model).mkdir(parents=True)
    (dist / "models" / bundle_model / "model.bin").write_bytes(b"stub")
    return dist


def test_assert_dist_tree_rejects_missing_web_page(tmp_path, monkeypatch):
    """D1③：桌面壳两个新页面（悬浮条 /float、隐私页 /privacy）缺一即构建失败。"""
    dist = _full_dist(tmp_path)
    (dist / "_internal" / "web" / "float.html").unlink()   # 少一个页面
    monkeypatch.setattr(build_exe, "DIST", dist)

    with pytest.raises(SystemExit) as err:
        build_exe.assert_dist_tree(False, "small")
    assert "float.html" in str(err.value)


def test_assert_dist_tree_rejects_missing_shell_dep(tmp_path, monkeypatch):
    """D1①（核心）：产物齐全但桌面壳依赖没进包 → 构建失败（修前的 dist 正是这种
    "能打出来但没窗口壳"的半成品）。抽查表可注入，故此处用演示包驱动。"""
    dist = _full_dist(tmp_path)
    monkeypatch.setattr(build_exe, "DIST", dist)
    monkeypatch.setattr(build_exe, "DESKTOP_SHELL_ARTIFACTS",
                        (("demo", "演示包", "demo/__init__.py"),))

    with pytest.raises(SystemExit) as err:
        build_exe.assert_dist_tree(False, "small")
    assert "演示包" in str(err.value)
    # 补上该文件后同一棵树必须过（否则断言就是"永远红"，没人会看）
    (dist / "_internal" / "demo").mkdir()
    (dist / "_internal" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    build_exe.assert_dist_tree(False, "small")


def test_assert_dist_tree_rejects_secrets_and_user_data(tmp_path, monkeypatch):
    """密钥/用户数据绝不进包（清单见 FORBIDDEN_DIST_ENTRIES）。"""
    dist = _full_dist(tmp_path)
    monkeypatch.setattr(build_exe, "DIST", dist)
    monkeypatch.setattr(build_exe, "DESKTOP_SHELL_ARTIFACTS", ())  # 依赖抽查另测
    (dist / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        build_exe.assert_dist_tree(False, "small")
    assert "config.json" in str(err.value)


def test_forbidden_list_covers_every_root_anchored_write():
    """D3 评审修复①：清单 = **运行期 ROOT 锚定的全部写盘物**，一个都不能漏。

    盘点（main.ROOT = exe 所在目录）：config.json（配置）、courses.json（课程表，
    main.py:52）、.env（向导写 key，main.py:1593/1636）、transcripts（笔记）、logs（日志）、
    glossary（术语表，glossary.py:53 root=ROOT）、desktop.lock（单实例锁，desktop.py:87）。
    评审2 实测：glossary 与 courses.json 曾在清单外 →「在打包目录里建过术语表/课程」
    留下的东西就能混进分发目录。"""
    for name in (".env", "config.json", "courses.json", "transcripts", "logs",
                 "glossary", "desktop.lock"):
        assert name in build_exe.FORBIDDEN_DIST_ENTRIES, f"{name} 必须进禁用清单"
    assert ".log" in build_exe.FORBIDDEN_DIST_SUFFIXES


@pytest.mark.parametrize("rel", [
    "glossary/CS.md",                    # 术语表（旧清单缺项，且可能是嵌套位置）
    "courses.json",                      # 课程表（同上）
    "_internal/glossary/CS.md",          # 嵌套泄漏：旧实现只看顶层，查不到
    "_internal/config.json",             # 嵌套泄漏：同上
    "_internal/logs/lecturelens.log",     # 日志残留
    ".env", "transcripts/lecture_x.md", "logs/lecturelens.log",
    "server_smoke.log",                  # *.log 后缀规则
])
def test_forbidden_scan_is_deep_and_complete(tmp_path, rel):
    """深扫任意层级：任意位置命中清单或 *.log 后缀即失败（旧实现只判顶层存在性）。

    命中路径可以是 rel 本身，也可以是它的某个祖先目录（命中 `glossary/` 就等于命中
    `glossary/CS.md`——两者都让构建失败）。"""
    dist = _full_dist(tmp_path)
    victim = dist / rel
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("x", encoding="utf-8")
    hits = [h.replace("\\", "/") for h in build_exe.forbidden_hits(dist)]
    target = rel.replace("\\", "/")
    assert any(target == h or target.startswith(h + "/") for h in hits), \
        f"深扫漏掉了 {rel}: {hits}"


def test_forbidden_scan_allows_only_model_owned_config_json(tmp_path):
    """唯一例外：models/<name>/config.json（HF 快照里模型自带的同名文件）。
    例外必须精确——顶层与 _internal 里的同名文件仍要命中。"""
    dist = _full_dist(tmp_path)          # 内含 models/small/model.bin
    (dist / "models" / "small" / "config.json").write_text("{}", encoding="utf-8")
    assert build_exe.forbidden_hits(dist) == [], "模型自带的 config.json 不该命中"
    assert build_exe.model_owned_path(Path("models/small/config.json")) is True
    assert build_exe.model_owned_path(Path("config.json")) is False
    assert build_exe.model_owned_path(Path("_internal/config.json")) is False
    (dist / "config.json").write_text("{}", encoding="utf-8")
    hits = build_exe.forbidden_hits(dist)
    assert any(h.replace("\\", "/") == "config.json" for h in hits), hits


def test_assert_dist_tree_tracks_actual_bundle_model(tmp_path, monkeypatch):
    """④ 产物断言认的是**实际随包模型**：打 turbo 版时缺 turbo 目录照样红。"""
    dist = _full_dist(tmp_path, "small")     # 只有 small
    monkeypatch.setattr(build_exe, "DIST", dist)
    monkeypatch.setattr(build_exe, "DESKTOP_SHELL_ARTIFACTS", ())
    build_exe.assert_dist_tree(False, "small")          # 默认口径：过
    with pytest.raises(SystemExit) as err:
        build_exe.assert_dist_tree(False, "large-v3-turbo")
    assert "models/large-v3-turbo/model.bin" in str(err.value)


# ---------------------------------------------------------------- ④ 随包模型

def test_bundle_model_defaults_to_small_with_override():
    """D1④：随包模型默认 small（≈460MB，任何机器装完开箱可用）；更高档位走下载。
    `--bundle-model` 可覆盖（完整版给有 N 卡的机器），产物断言跟着实际模型名走。"""
    assert build_exe.BUNDLE_MODEL == "small"
    assert build_exe.parse_args([]).bundle_model == "small"
    assert build_exe.parse_args([]).gpu is True
    assert build_exe.parse_args(["--no-gpu"]).gpu is False
    assert build_exe.parse_args(["--check-dist"]).check_dist is True
    assert build_exe.parse_args(
        ["--bundle-model", "large-v3-turbo"]).bundle_model == "large-v3-turbo"

    assert "models/small/model.bin" in build_exe.required_dist_files()
    assert "models/large-v3-turbo/model.bin" in \
        build_exe.required_dist_files("large-v3-turbo")
    # 默认值必须留在 REQUIRED_DIST_FILES 之外的同一条口径上（模型名单独拼）
    assert all("models/" not in rel for rel in build_exe.REQUIRED_DIST_FILES)
