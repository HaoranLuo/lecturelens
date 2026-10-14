# -*- coding: utf-8 -*-
"""tests/test_macos_build.py — Mac 适配第三批：打包脚本 / ROOT 数据面 / 脚本平台分支。

开发机是 Windows，所以本文件的作用是**让 Mac 侧的东西在这台机上也可验**：

① `app/main.py` 的 `_frozen_root()`：frozen 的 Windows（exe 目录）与 macOS
   （~/Library/Application Support/LectureLens）两条分支都是纯函数可断言的；
② `scripts/build_macos.py`：纯函数部分（参数构造 / Info.plist 补丁 / 产物断言 /
   .icns 生成命令）在 Windows 上直接断言；`main()` 非 darwin 必须拒绝；
③ `scripts/verify_dist.py` 的 macOS 分支纯函数（.app 定位、plutil 输出解析、
   两道路径闸）——Windows 侧行为不变由 test_verify_dist.py 继续钉住；
④ `scripts/smoke_test.py` 的平台分支：TTS（Windows=SAPI / macOS=say+afconvert）、
   [3] GPU 基准仅 Windows、[8] 打包契约按平台分派；
⑤ 文档口径：README / CLAUDE.md 里写明了 macOS 的数据目录与打包方式（防漂移）。
"""
import json
import plistlib
import subprocess
import sys
import tempfile
import types
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import branding                 # noqa: E402  （app/ 已由 tests/conftest.py 放进 sys.path）
import build_macos as bm        # noqa: E402
import smoke_test as sm         # noqa: E402
import verify_dist as vd        # noqa: E402

IS_MAC = sys.platform == "darwin"


# ---------------- ① app/main.py：frozen 数据根 ----------------

def test_frozen_root_windows_is_exe_dir():
    """Windows（及其它非 darwin）：数据根 = exe 所在目录（既有口径不变）。"""
    got = main._frozen_root("win32")
    assert got == Path(sys.executable).resolve().parent
    assert main._frozen_root("linux") == got          # 其它平台同样回落 exe 目录


def test_frozen_root_macos_is_user_data_dir():
    """macOS：数据根 = ~/Library/Application Support/LectureLens（.app 只读，不许写）。"""
    got = main._frozen_root("darwin")
    assert got == Path.home() / "Library" / "Application Support" / "LectureLens"
    assert got.is_absolute()
    # 必须在 .app 之外——这正是这条分支存在的理由
    assert "Library/Application Support" in got.as_posix()


def test_data_dir_copies_agree():
    """三处口径不许漂移：main._frozen_root / build_macos.MAC_DATA_DIR_REL /
    verify_dist.MAC_DATA_DIR（verify_dist 与 smoke 都读前者）。"""
    want = Path.home() / bm.MAC_DATA_DIR_REL
    assert main._frozen_root("darwin") == want
    assert vd.MAC_DATA_DIR == want


# ---------------- ② scripts/build_macos.py ----------------

def test_main_refuses_on_non_darwin():
    """非 macOS 直接拒绝（不产出半成品、不碰 PyInstaller）。"""
    if IS_MAC:
        pytest.skip("本用例验的是非 darwin 的拒绝路径")
    with pytest.raises(SystemExit) as ei:
        bm.main()
    assert "只在 macOS" in str(ei.value) and "build_exe.py" in str(ei.value)


def test_pyinstaller_args_macos_shape():
    args = bm.pyinstaller_args()
    assert args[-1] == str(bm.ENTRY) and bm.ENTRY.name == "desktop.py", \
        "入口必须是桌面壳（自启命令的 --minimized 只有它认）"
    assert "--windowed" in args and "--onedir" in args
    assert "--osx-bundle-identifier" in args and bm.BUNDLE_ID in args
    assert "--version-file" not in args, "macOS 的版本走 Info.plist，不是 Windows 资源格式"
    assert "--icon" not in args, "没传图标时不该出现 --icon（由 build_icns 决定）"


def test_pyinstaller_args_data_uses_colon_separator():
    args = bm.pyinstaller_args()
    add_data = [args[i + 1] for i, a in enumerate(args) if a == "--add-data"]
    assert add_data, "必须带 --add-data（web 与 glossary_packs）"
    for a in add_data:
        assert bm.DATA_SEP == ":" and ":" in a
        assert ";" not in a, f"macOS 的 --add-data 分隔符是 ':'，' ;' 会静默丢目录: {a}"
    assert any(a.endswith(":web") for a in add_data)
    assert any(a.endswith(":glossary_packs") for a in add_data)


def test_pyinstaller_args_collects_darwin_backends_not_windows_ones():
    args = bm.pyinstaller_args()
    collect_all = {args[i + 1] for i, a in enumerate(args) if a == "--collect-all"}
    hidden = {args[i + 1] for i, a in enumerate(args) if a == "--hidden-import"}
    # 桌面壳依赖清单 ↔ 收集清单一一对应（漏收 = 打包版没有窗口壳）
    assert {pkg for pkg, _w in bm.DARWIN_SHELL_PACKAGES} <= collect_all
    # darwin 后端 hidden-import（平台动态挑的那一支，静态分析看不见）
    for mod in ("webview.platforms.cocoa", "keyboard._darwinkeyboard", "pystray._darwin"):
        assert mod in hidden, mod
    # Windows 专属依赖不该出现在 macOS 包里
    assert not ({"pythonnet", "clr_loader", "pyaudiowpatch"} & collect_all)


def test_pyinstaller_args_accepts_optional_icon():
    args = bm.pyinstaller_args(icon=Path("C:/nope/icon.icns"))
    assert "--icon" not in args, "图标不存在时不该传给 PyInstaller（它会硬失败）"


def test_info_plist_patch_keys_and_version():
    patch = bm.info_plist_patch("1.2.3")
    assert patch["CFBundleShortVersionString"] == "1.2.3"
    assert patch["CFBundleVersion"] == "1.2.3"
    assert set(bm.INFO_PLIST_KEYS) - {"CFBundleIdentifier"} <= set(patch)
    mic = patch["NSMicrophoneUsageDescription"]
    assert mic and any("\u4e00" <= c <= "\u9fff" for c in mic), \
        "麦克风用途说明必须是中文（TCC 弹窗直接展示给用户）"


def test_patch_info_plist_merges_and_preserves(tmp_path):
    """补丁只**加/改**自己那几个键：PyInstaller 写的 CFBundleExecutable 等必须原样保留。"""
    app = tmp_path / "LectureLens.app"
    (app / "Contents").mkdir(parents=True)
    plist = app / "Contents" / "Info.plist"
    with plist.open("wb") as f:
        plistlib.dump({"CFBundleExecutable": "LectureLens",
                       "CFBundleIdentifier": bm.BUNDLE_ID,
                       "CFBundleShortVersionString": "0.0.0"}, f)
    got = bm.patch_info_plist(app, "1.0.0")
    assert got == plist
    data = plistlib.loads(plist.read_bytes())
    assert data["CFBundleShortVersionString"] == "1.0.0"
    assert data["CFBundleVersion"] == "1.0.0"
    assert data["NSMicrophoneUsageDescription"]
    assert data["CFBundleExecutable"] == "LectureLens"          # 原有键保留
    assert data["CFBundleIdentifier"] == bm.BUNDLE_ID           # 身份不被改（改=重授权）


def test_patch_info_plist_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bm.patch_info_plist(tmp_path / "Nope.app", "1.0.0")


def _fake_app(tmp_path: Path, *, cross_link: bool = False) -> Path:
    """造一个假的 .app 树（PyInstaller 布局：数据在 Resources；
    cross_link=True 时改成只放 Frameworks 侧，模拟交叉软链）。"""
    app = tmp_path / "LectureLens.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "web").mkdir(parents=True)
    (app / "Contents" / "Resources" / "glossary_packs").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "LectureLens").write_bytes(b"MZ")
    base = (app / "Contents" / "Frameworks" if cross_link
            else app / "Contents" / "Resources")
    (base / "web").mkdir(parents=True, exist_ok=True)
    (base / "glossary_packs").mkdir(parents=True, exist_ok=True)
    for p in ("index.html", "float.html", "privacy.html"):
        (base / "web" / p).write_text("x", encoding="utf-8")
    (base / "glossary_packs" / "数学.md").write_text("x", encoding="utf-8")
    with (app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": bm.BUNDLE_ID,
                       "CFBundleShortVersionString": "1.0.0"}, f)
    if cross_link:
        for d in ("web", "glossary_packs"):
            (app / "Contents" / "Resources" / d).rmdir()
    return app


def test_assert_app_tree_passes_on_complete_bundle(tmp_path):
    bm.assert_app_tree(_fake_app(tmp_path))          # 不抛即通过


def test_assert_app_tree_accepts_frameworks_cross_link(tmp_path):
    """PyInstaller 把 data-only 目录放 Resources 并交叉软链到 Frameworks——
    只在一侧存在也必须算齐（否则 Mac 上会因为布局差异误报构建失败）。"""
    bm.assert_app_tree(_fake_app(tmp_path, cross_link=True))


def test_assert_app_tree_catches_missing_page(tmp_path):
    app = _fake_app(tmp_path)
    (app / "Contents" / "Resources" / "web" / "privacy.html").unlink()
    with pytest.raises(SystemExit) as ei:
        bm.assert_app_tree(app)
    assert "缺文件" in str(ei.value) and "privacy.html" in str(ei.value)


def test_assert_app_tree_catches_user_data_inside_bundle(tmp_path):
    """数据面纪律：.app 里出现 config.json/.env/transcripts 即构建失败
    （「在 .app 里跑过一次」正是它们出现的路径）。"""
    app = _fake_app(tmp_path)
    (app / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        bm.assert_app_tree(app)
    assert "用户数据/密钥" in str(ei.value)


def test_assert_app_tree_missing_app_raises(tmp_path):
    with pytest.raises(SystemExit):
        bm.assert_app_tree(tmp_path / "Nope.app")


def test_build_icns_command_argv_and_mapping(tmp_path):
    """iconutil 的调用形状（argv 固定、不经 shell）+ iconset 档位映射——
    在本机（非 macOS）用替身 runner 断言，不真跑 iconutil。"""
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("icon_16.png", "icon_32.png", "icon_64.png", "icon_128.png",
                 "icon_256.png"):
        (assets / name).write_bytes(b"png")
    calls: list = []

    def runner(argv, **kw):
        calls.append(argv)
        out = Path(argv[argv.index("-o") + 1])
        out.write_bytes(b"icns")          # 模拟 iconutil 产出
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    out = tmp_path / "icon.icns"
    got = bm.build_icns(assets=assets, out=out, runner=runner, workdir=tmp_path / "w")
    assert got == out and out.is_file()
    argv = calls[0]
    assert argv[0] == "iconutil" and argv[1:3] == ["-c", "icns"]
    iconset = Path(argv[3])
    assert iconset.is_dir(), "先落 iconset 目录再 iconutil"
    written = sorted(p.name for p in iconset.iterdir())
    assert "icon_16x16.png" in written and "icon_512x512@2x.png" in written
    assert set(written) == {n for n, _s in bm.ICONSET_MAP}


def test_build_icns_failure_is_soft(tmp_path):
    """iconutil 失败只降级（返回 None），绝不拦住构建——图标是装饰性的。"""
    def runner(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)

    assert bm.build_icns(assets=tmp_path, out=tmp_path / "icon.icns",
                         runner=runner, workdir=tmp_path / "w") is None


def test_codesign_argv_is_fixed_and_soft():
    calls: list = []

    def runner(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    rc, out = bm.codesign_ad_hoc("dist/LectureLens.app", runner=runner)
    assert (rc, out) == (0, "")
    assert calls[0][:5] == ["codesign", "--force", "--deep", "--sign", "-"]
    assert calls[0][-1].endswith("LectureLens.app")
    # 失败不抛（返回码如实回传）
    def boom(argv, **kw):
        raise OSError("codesign not found")
    rc2, out2 = bm.codesign_ad_hoc("x.app", runner=boom)
    assert rc2 != 0 and out2


def test_build_macos_reuses_build_exe_single_sources():
    """版本与「用户数据/密钥」清单都复用 build_exe（单一事实源，不另抄一份）。"""
    import build_exe
    assert bm.app_version() == build_exe.app_version()
    assert bm.app_version() == __import__("branding").APP_VERSION


def test_build_macos_prunes_tool_state_like_windows():
    """评审④：macOS 侧同一个 bug、同一份修法——`--add-data` 整目录复制会把
    源目录里的 `.mimosa/` 搬进 Contents/Resources/。

    为什么必须复用 build_exe.prune_tool_state 而不是在 build_macos 里再写一遍：
    两条产物的 `--add-data` 口径相同（本文件 pyinstaller_args 用 ":" 分隔，Windows
    用 ";"），修法也必须同源，否则又变成"两份清单迟早漂移"。

    源码级断言（不在 macOS 上就只验源码）：删掉那一步调用，本测当场红。
    """
    import build_exe
    src = Path(bm.__file__).read_text(encoding="utf-8")
    assert "prune_tool_state(" in src, "build_macos 必须摘掉工具态目录"
    assert "build_exe.prune_tool_state(DIST_APP)" in src, \
        "必须复用 build_exe 的同一实现（不另写一份）"
    # 与断言同源：黑名单那一道在 Mac 侧也生效（assert_app_tree → build_exe.forbidden_hits）
    assert ".mimosa" in build_exe.FORBIDDEN_DIST_ENTRIES
    # prune 对任意产物根都成立（DIST_APP 就是 .app 路径）；纯逻辑，Windows 上可跑
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="bm-prune-"))
    try:
        (d / "Contents" / "Resources" / ".mimosa").mkdir(parents=True)
        assert build_exe.prune_tool_state(d) == [
            str(Path("Contents") / "Resources" / ".mimosa")]
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------- ③ scripts/verify_dist.py 的 macOS 纯函数 ----------------

@pytest.mark.skipif(IS_MAC, reason="Windows 产物常量只在 Windows 宿主上成立")
def test_verify_dist_windows_paths_unchanged():
    """Windows 口径不变：产物根 = dist/LectureLens、可执行文件名是字面量 exe。"""
    assert vd.DIST_ROOT == ROOT / "dist" / "LectureLens"
    assert vd.EXE_NAME == "LectureLens.exe"
    assert vd.MAC_BINARY_REL.endswith("Contents/MacOS/LectureLens")


def test_verify_dist_mac_binary_literal():
    assert vd.MAC_BINARY_REL == "LectureLens.app/Contents/MacOS/LectureLens"
    assert "/" in vd.MAC_BINARY_REL and vd.MAC_BINARY_REL.endswith("LectureLens")


# —— 评审修复：src_exe 的推导（旧写法把 .app 名拼两遍 → macOS 上 main() 提前 exit 1） ——

def test_binary_path_windows_and_mac():
    """可执行文件路径的唯一推导入口：Windows = <onedir>/LectureLens.exe；
    macOS = <.app>/Contents/MacOS/LectureLens（收的是 .app **自身**）。"""
    assert vd.binary_path(Path("/d/dist/LectureLens"), mac=False) == \
        Path("/d/dist/LectureLens/LectureLens.exe")
    assert vd.binary_path(Path("/d/dist/LectureLens.app"), mac=True) == \
        Path("/d/dist/LectureLens.app/Contents/MacOS/LectureLens")
    # 缺省跟随本机平台（Windows 上就是 exe 口径，行为不变）
    assert vd.binary_path(vd.DIST_ROOT, mac=False).name == vd.EXE_NAME


def test_binary_path_lands_inside_a_real_app_tree(tmp_path):
    """给定 .app 根 → 推出来的路径必须**落在 Contents/MacOS/ 且 is_file()**
    （评审给的正是这条判据：只断言字符串本身拦不住「拼两遍」这类错）。"""
    app = _fake_app(tmp_path)
    got = vd.binary_path(app, mac=True)
    assert got.is_file(), f"推导出的可执行文件不存在: {got}"
    assert got.parent == app / "Contents" / "MacOS"
    assert app in got.parents


def test_mac_binary_rel_agrees_with_binary_path(tmp_path):
    """两种语义不许漂移：MAC_BINARY_REL 相对 .app 的**父目录**（启动用），
    binary_path 收 .app **自身**——同一个文件必须两条路都能指到。"""
    app = tmp_path / vd.MAC_APP_NAME
    assert app.parent / vd.MAC_BINARY_REL == vd.binary_path(app, mac=True)
    assert Path(vd.MAC_BINARY_REL).parts[0] == vd.MAC_APP_NAME


def test_mac_app_name_matches_what_build_macos_produces():
    """跨模块一致：verify_dist 认的 .app 名 == build_macos 产出的 .app 名
    （改名一处忘另一处 = 验证脚本永远找不到产物）。"""
    assert vd.MAC_APP_NAME == bm.DIST_APP.name
    assert bm.REQUIRED_APP_FILES[0] == f"Contents/MacOS/{vd.MAC_BINARY_NAME}"


def test_verify_dist_main_reaches_checks_on_mac_simulation(tmp_path, monkeypatch, capsys):
    """**端到端**模拟 macOS：造真 .app 布局，只把模块级常量与三个外部依赖换成替身
    （plutil / Popen / HTTP），整条 main() 必须跑到结果汇总且**全过**。

    评审修复前这条必红：src_exe 被拼成 `…/LectureLens.app/LectureLens.app/…`，
    main() 在任何检查项之前就 `return 1`（RESULTS 长度 0、一个 ✅/❌ 都不产生）。
    顺带钉住启动纪律：argv 全是字面量、cwd 是拷贝的父目录、且 argv[0] == MAC_BINARY_REL。"""
    app = _fake_app(tmp_path / "dist")
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    for n in ("config.json", "logs", "transcripts"):
        p = data_dir / n
        p.mkdir(exist_ok=True) if n != "config.json" else p.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(vd, "IS_MAC", True)
    monkeypatch.setattr(vd, "DIST_ROOT", app)
    monkeypatch.setattr(vd, "MAC_DATA_DIR", data_dir)
    monkeypatch.setattr(vd, "port_busy", lambda port: False)
    monkeypatch.setattr(vd.time, "sleep", lambda _s: None)   # 循环/收尾的等待
    # --timeout 1：替身如果哪天失效，这里 1 秒就红——而不是在就绪循环里空转到默认 240 秒
    monkeypatch.setattr(sys, "argv", ["verify_dist.py", "--port", "8798", "--timeout", "1"])

    def fake_run(argv, **kw):
        assert argv[0] == "plutil", f"macOS 侧版本读取应走 plutil: {argv}"
        # 版本从 branding 派生（不写死字面量）：verify_dist 的版本探针改成派生自
        # APP_VERSION 之后，这里若钉一个固定版本，发版升版本时这条端到端模拟会假红。
        return SimpleNamespace(
            returncode=0,
            stdout=f'  "CFBundleShortVersionString" => "{branding.APP_VERSION}"\n',
            stderr="")

    launched: dict = {}

    class _Proc:
        def __init__(self, argv, **kw):
            launched["argv"] = argv
            launched["cwd"] = Path.cwd()
            # 关键一条（必须在 Popen 那一刻查：main() 收尾会把拷贝删掉）：
            # 从 cwd 出发，MAC_BINARY_REL 这条相对路径**真的指得到可执行文件**
            launched["binary_exists"] = (Path.cwd() / vd.MAC_BINARY_REL).is_file()

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(vd.subprocess, "run", fake_run)
    monkeypatch.setattr(vd.subprocess, "Popen", _Proc)

    def fake_get(port, path, timeout=5.0):
        if path == vd.API_STATUS:
            return 200, json.dumps({"model": "large-v3-turbo", "asr": "ready",
                                    "version": branding.APP_VERSION}).encode()
        if path == vd.PACKS_API:
            return 200, json.dumps({"packs": [{"name": "数学", "count": 3}]}).encode()
        return 200, b"<html></html>"

    monkeypatch.setattr(vd, "get", fake_get)

    rc = vd.main()
    out = capsys.readouterr().out
    assert "找不到" not in out, "评审修复点：可执行文件路径不该再拼错"
    assert rc == 0 and all(ok for _n, ok, _t in vd.RESULTS), vd.RESULTS
    names = [n for n, _ok, _t in vd.RESULTS]
    assert any("CFBundleShortVersionString" in n for n in names), names
    assert any("用户目录" in n for n in names), names        # 数据面在包外的断言
    assert any("Contents 内无用户数据" in n for n in names), names
    # 启动纪律：字面量相对路径 + chdir 到**拷贝的临时根**（.app 的父目录）
    assert launched["argv"][0] == vd.MAC_BINARY_REL
    assert launched["argv"][1:] == ["--server-only", "--port", "8798"]
    assert str(launched["cwd"]).startswith(str(Path(tempfile.gettempdir())))
    assert "lecturelens-verify-" in launched["cwd"].name, f"应 chdir 进拷贝根: {launched['cwd']}"
    assert launched["binary_exists"] is True, "相对路径在启动时的 cwd 下必须指得到可执行文件"


def test_mac_bundle_plist_locates_info_plist(tmp_path):
    app = tmp_path / "LectureLens.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")
    (app / "Contents" / "MacOS" / "LectureLens").write_bytes(b"MZ")
    want = app / "Contents" / "Info.plist"
    assert vd.mac_bundle_plist(app) == want
    assert vd.mac_bundle_plist(app / "Contents" / "MacOS" / "LectureLens") == want


def test_mac_bundle_plist_raises_when_absent(tmp_path):
    with pytest.raises(SystemExit):
        vd.mac_bundle_plist(tmp_path / "Nothing.app")


def test_parse_plutil_version():
    """`plutil -p` 的输出解析（真机上就是这样几行）——解析不到时返回空串，不猜。"""
    sample = (
        '{\n  "CFBundleExecutable" => "LectureLens"\n'
        '  "CFBundleIdentifier" => "com.lecturelens.app"\n'
        '  "CFBundleShortVersionString" => "1.0.0"\n'
        '  "NSMicrophoneUsageDescription" => "…"\n}\n')
    assert vd.parse_plutil_version(sample) == "1.0.0"
    assert vd.parse_plutil_version("") == ""
    assert vd.parse_plutil_version("CFBundleShortVersionString => 1.0.0") == ""


# ---------------- ④ scripts/smoke_test.py 的平台分支 ----------------

def test_synth_tts_windows_branch_is_sapi(monkeypatch):
    """Windows：仍是 SAPI/PowerShell（argv 形状不变）。"""
    if IS_MAC:
        pytest.skip("Windows SAPI 分支只在 Windows 上验")
    calls: list = []

    def runner(argv, **kw):
        calls.append(argv)
        raise RuntimeError("stop here")     # 只验参数，不真合成

    monkeypatch.setattr(sm.subprocess, "run", runner)
    with pytest.raises(RuntimeError):
        sm.synth_tts("hello", Path("x.wav"))
    assert calls[0][0] == "powershell"
    assert "System.Speech" in calls[0][-1]


def test_synth_tts_macos_branch_uses_say_and_afconvert(monkeypatch, tmp_path):
    """macOS：say 出 AIFF → afconvert 转 16k 单声道 WAV（argv 固定、不经 shell）。
    替身 runner 顺手写一个**真 16k 单声道 wav**，好让后面的读回逻辑也一起验。"""
    calls: list = []
    wav_path = tmp_path / "tts.wav"

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == "afconvert":
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x01" * 1600)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sm.sys, "platform", "darwin")
    monkeypatch.setattr(sm.subprocess, "run", runner)
    audio = sm.synth_tts("hello world", wav_path)
    assert [c[0] for c in calls] == ["say", "afconvert"]
    assert calls[0][1] == "-o" and str(calls[0][2]).endswith(".aiff")
    assert calls[1][:4] == ["afconvert", "-f", "WAVE", "-d"]
    assert "LEI16@16000" in calls[1] and "-c" in calls[1] and "1" in calls[1]
    assert audio.dtype.name == "float32" and len(audio) == 1600
    assert not list(tmp_path.glob("*.aiff")), "中间产物 AIFF 应被清掉"


def test_smoke_gpu_section_skips_off_windows(monkeypatch, capsys):
    """[3] GPU 基准仅 Windows：非 Windows 如实跳过、**不记入结果**（跳过 ≠ 通过）。"""
    if IS_MAC:
        pytest.skip("本用例验的是非 Windows 的跳过路径")
    before = len(sm.RESULTS)
    monkeypatch.setattr(sm.sys, "platform", "darwin")
    sm.test_gpu()
    out = capsys.readouterr().out
    assert "该段仅 Windows" in out
    assert len(sm.RESULTS) == before, "跳过不该产生 ✅/❌ 记录"


def test_smoke_build_contract_dispatches_to_macos(monkeypatch):
    """[8] 打包契约按平台分派：darwin → macOS 那支（断言 build_macos 的纯函数）。"""
    before = len(sm.RESULTS)
    monkeypatch.setattr(sm.sys, "platform", "darwin")
    sm.test_build_contract()
    added = sm.RESULTS[before:]
    assert len(added) == 1 and added[0][0] == "打包契约（macOS）"
    assert added[0][1] is True, added[0][2]


# ---------------- ⑤ 文档口径 ----------------

def test_docs_state_the_platform_facts():
    """README/CLAUDE 必须写明 macOS 的数据目录与打包脚本——否则用户按 Windows 口径
    找不到自己的笔记（这条是文档漂移的守卫，改坏了立刻红）。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for text, name in ((readme, "README"), (claude, "CLAUDE")):
        assert "Library/Application Support/LectureLens" in text, name
    assert "build_macos.py" in readme and "build_macos.py" in claude
    assert ".app" in readme
    # Windows 专属事实必须带平台标注（bat 只在 Windows 用）
    assert "start_translate.bat" in readme and "macOS" in readme
