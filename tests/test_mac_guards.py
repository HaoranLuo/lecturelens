# -*- coding: utf-8 -*-
"""tests/test_mac_guards.py — Mac 适配守卫单测（**Windows 上就能跑**的六组不变量）。

为什么再开一个文件（与 test_mac_adapt / test_mac_engine / test_macos_build 的关系）：
那三个文件是**逐模块的深挖**（desktop 五处守卫的字段级行为、MLX 后端与两张模型表、
build_macos/verify_dist 的纯函数）。本文件是**不变量级的合并守卫**——每条只钉一个
「谁改坏它就会出事」的事实，一屏能看完，跑一次不到 1 秒：

① MLX 守卫：非 darwin / 非 arm64 / 库不在 / **库在但 import 炸**（坏安装）四种情况
   都不得启用 mlx，且必须回落 CPU 而不是抛（用户机器上装坏一个包不该让应用起不来）；
② MLX 返回形状适配器：mlx 风格 dict → 三种既有形态，且**与 faster-whisper 路径逐值等价**
   （把两条后端喂同一段内容，三种形态的结果必须一字不差——这是「换后端不改调用方」的定义）；
③ launchd plist 文本生成：RunAtLoad/KeepAlive/ProgramArguments 的纯函数断言
   （plistlib 回读 + --minimized + 路径转义）；
④ desktop 平台降级分支：非 Windows（os.name != "nt"）下五个入口喂各种**假窗口对象**
   （None/无 native/native=None/句柄会抛）都不得抛异常，并给出既有降级语义；
⑤ requirements 平台 marker 静态断言：pyaudiowpatch 只在 win32、mlx-whisper 只在 darwin；
   其余依赖不许被误加 marker（Windows 侧安装结果逐字不变）；
⑥ ROOT 的 macOS 分支：frozen + darwin → ~/Library/Application Support/LectureLens
   （纯函数 + 源级接线双证：光有分支没人走等于没写）；
⑦ docs/mac-guide.md 的事实对齐（文档最容易「悄悄说谎」——把指南里的路径/ID/命令与
   代码里的唯一事实源逐条对照，改一处忘另一处立刻红）。
"""
import plistlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import asr as asr_mod
import loopback as lb_mod
import main

ROOT = Path(__file__).resolve().parent.parent


# ================================================================ ① MLX 守卫
def _fake_mlx_module():
    """假 mlx_whisper 模块（只要「能 import 成功」这件事——真调用由适配器用例覆盖）。"""
    return types.ModuleType("mlx_whisper")


@pytest.fixture
def no_mlx(monkeypatch):
    """默认：模块不在（sys.modules 里塞 None → import 抛 ImportError）。"""
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)
    return monkeypatch


def test_mlx_never_enabled_off_darwin(no_mlx, monkeypatch):
    """非 darwin（Windows/Linux）：无论装没装、什么架构，都不得启用 mlx。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")
    assert asr_mod.mlx_available() is False
    assert asr_mod._resolve_device("auto") in ("cpu", "cuda")   # 仍是原有的 CUDA 探测口径
    assert asr_mod._resolve_device("mlx") == "cpu"              # 显式要 mlx → 回落 CPU
    assert asr_mod.active_backend("mlx") == "ctranslate2"


def test_mlx_never_enabled_on_non_arm64(no_mlx, monkeypatch):
    """darwin 但非 arm64（Intel Mac）：装了 mlx-whisper 也不启用（mlx 只跑 Apple Silicon）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module())
    assert asr_mod.mlx_available() is False
    assert asr_mod._resolve_device("mlx") == "cpu"


def test_mlx_falls_back_without_the_package(no_mlx, monkeypatch):
    """darwin + arm64 但没装包：回落 CPU、**不抛**（缺一个可选后端不是故障）。

    auto 的口径：MLX 不在时**沿用它本来的解析规则**（有 CUDA 卡就是 cuda、否则 cpu）
    ——本机（Windows 开发机有 N 卡）也会走到这里，故断言写成「与 MLX 无关的既有结果」。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")
    assert asr_mod.mlx_available() is False
    assert asr_mod._resolve_device("auto") in ("cpu", "cuda")
    assert asr_mod._resolve_device("mlx") == "cpu"
    assert asr_mod.active_backend("auto") == "ctranslate2"


def test_mlx_broken_install_is_not_fatal(monkeypatch):
    """darwin + arm64 + **库在但 import 即炸**（坏安装/依赖缺失）：当作不可用，绝不抛。

    用一个真的 meta_path finder 让 `import mlx_whisper` 抛异常，并断言 finder **确实被调用过**
    ——否则这条用例与「库不在」无法区分，等于没测。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")

    class _BoomFinder:
        def __init__(self):
            self.calls = 0

        def find_module(self, name, path=None):        # 兼容老式查找协议
            return self if name == "mlx_whisper" else None

        def find_spec(self, name, path=None, target=None):
            if name == "mlx_whisper":
                self.calls += 1
                raise ImportError("libmlx.dylib 缺失（模拟坏安装）")
            return None

    finder = _BoomFinder()
    monkeypatch.delitem(sys.modules, "mlx_whisper", raising=False)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    assert asr_mod.mlx_available() is False
    assert finder.calls >= 1, "finder 没被调用 → 这条用例没测到「import 抛异常」的路径"
    assert asr_mod._resolve_device("auto") in ("cpu", "cuda")   # 回落既有解析规则
    assert asr_mod._resolve_device("mlx") == "cpu"


def test_mlx_enabled_only_with_all_three_conditions(monkeypatch):
    """darwin + arm64 + 库在位 → 才启用（auto 优先 MLX；显式 mlx 走 MLX）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(asr_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module())
    assert asr_mod.mlx_available() is True
    assert asr_mod._resolve_device("auto") == "mlx"
    assert asr_mod._resolve_device("mlx") == "mlx"
    assert asr_mod.active_backend(None) == "mlx"


# ================================================================ ② 形状适配器
#: 同一段内容的两种表示（值必须一一对应，才能断言「两条后端产出相同」）
_CT2_SEGMENTS = [
    {"text": " Hello world.", "start": 0.0, "end": 1.25, "no_speech_prob": 0.02,
     "words": [(" Hello", 0.0, 0.5), (" world.", 0.5, 1.25)]},
    {"text": "  ", "start": 1.25, "end": 1.4, "no_speech_prob": 0.97, "words": []},
    {"text": " Second line.", "start": 1.4, "end": 2.5, "no_speech_prob": 0.1,
     "words": [(" Second", 1.4, 1.8), (" line.", 1.8, 2.5)]},
]


def _ct2_result():
    """faster-whisper 侧的等价产物：段对象（鸭子类型）+ info。"""

    class _Word:
        def __init__(self, w, s, e):
            self.word, self.start, self.end = w, s, e

    class _Seg:
        def __init__(self, d):
            self.text, self.start, self.end = d["text"], d["start"], d["end"]
            self.no_speech_prob = d["no_speech_prob"]
            self.words = [_Word(*w) for w in d["words"]]

    return iter([_Seg(d) for d in _CT2_SEGMENTS]), SimpleNamespace(language="en")


def _mlx_result() -> dict:
    """mlx_whisper 侧的等价产物：dict（键名与真库一致，见 mlx_whisper 0.4.3 实源）。"""
    return {
        "text": "（本适配器不直接用 result['text']）",
        "language": "en",
        "segments": [
            {"id": i, "seek": 0, "text": d["text"], "start": d["start"], "end": d["end"],
             "no_speech_prob": d["no_speech_prob"],
             "words": [{"word": w, "start": s, "end": e, "probability": 0.9}
                       for w, s, e in d["words"]]}
            for i, d in enumerate(_CT2_SEGMENTS)
        ],
    }


def _make_asr(backend: str, monkeypatch):
    """造一个绕过 __init__ 的 WhisperASR：两条后端各自接上自己的替身模型。"""
    w = object.__new__(asr_mod.WhisperASR)
    w.language = "en"
    w.detected_lang = None
    w.backend = backend
    w.model_ref = "mlx-community/whisper-small-mlx"
    if backend == "mlx":
        mod = types.ModuleType("mlx_whisper")
        mod.transcribe = lambda audio, **kw: _mlx_result()
        monkeypatch.setitem(sys.modules, "mlx_whisper", mod)
    else:
        class _Model:
            def transcribe(self, audio, **kw):
                return _ct2_result()

        w.model = _Model()
    return w


@pytest.mark.parametrize("flags", [
    {"word_timestamps": False, "with_times": False},   # 默认：整句 str
    {"word_timestamps": False, "with_times": True},    # with_times： (text, segs)
    {"word_timestamps": True, "with_times": False},    # 流式：段对象列表
])
def test_mlx_adapter_matches_the_ct2_shapes(flags, monkeypatch):
    """同一段内容：mlx 后端与 faster-whisper 后端的三种返回形态必须**逐值等价**。

    等价的范围：默认形态比文本；with_times 比 (文本, 段级时间戳列表)；
    流式形态比「时间戳三元组列表」（也就是 streaming.WhisperASRAdapter 真正消费的东西）
    —— 后者比直接比对象属性更贴近契约（属性名可能演化，消费行为不会）。"""
    import numpy as np
    import streaming

    audio = np.zeros(16000, dtype="float32")
    ct2 = _make_asr("ctranslate2", monkeypatch)
    mlx = _make_asr("mlx", monkeypatch)
    got_ct2 = ct2.transcribe(audio, **flags)
    got_mlx = mlx.transcribe(audio, **flags)
    if flags["word_timestamps"]:
        ad = streaming.WhisperASRAdapter.__new__(streaming.WhisperASRAdapter)
        assert ad.ts_words(got_mlx) == ad.ts_words(got_ct2), "词级时间戳序列必须等价"
        assert ad.segments_end_ts(got_mlx) == ad.segments_end_ts(got_ct2)
        assert [s.text for s in got_mlx] == [s.text for s in got_ct2]
    else:
        assert got_mlx == got_ct2, f"形态 {flags} 不等价: {got_mlx!r} vs {got_ct2!r}"
        if flags["with_times"]:
            assert got_mlx[1] == got_ct2[1]


def test_mlx_adapter_is_a_pure_function():
    """纯函数：同样的输入两次调用结果相同；不碰全局状态；坏输入降级为空结果。"""
    r = _mlx_result()
    first = asr_mod.mlx_to_shapes(r, with_times=True)
    assert first == asr_mod.mlx_to_shapes(r, with_times=True)
    assert first[0] == "Hello world. Second line."       # 空段丢弃、段间一个空格
    assert first[1][0] == {"start": 0.0, "end": 1.25, "text": "Hello world."}
    assert asr_mod.mlx_to_shapes({}) == ""
    assert asr_mod.mlx_to_shapes(None, with_times=True) == ("", [])
    assert asr_mod.mlx_to_shapes({"segments": [None, "junk"]}) == ""


# ================================================================ ③ launchd plist
def test_launchd_plist_text_invariants(monkeypatch, tmp_path):
    """plist 文本的纯函数断言：Label / RunAtLoad=true / KeepAlive=false /
    ProgramArguments=[<命令…>, --minimized]（顺序与 command_parts 一致）。"""
    import autostart as a
    monkeypatch.setattr(a, "sys", SimpleNamespace(platform="darwin", frozen=False,
                                                  executable=sys.executable))
    monkeypatch.setattr(a, "launch_agents_dir", lambda: tmp_path)
    xml = a.plist_xml(Path("D:/proj"))
    data = plistlib.loads(xml.encode("utf-8"))          # 必须是合法 plist（回读即证）
    assert data["Label"] == a.LAUNCH_AGENT_LABEL == "com.lecturelens"
    assert data["RunAtLoad"] is True, "登录即启动（macOS 的「开机自启」等价物）"
    assert data["KeepAlive"] is False, "退出后不许拉起（托盘「退出」必须是真退出）"
    assert data["ProgramArguments"] == a.command_parts(Path("D:/proj"))
    assert data["ProgramArguments"][-1] == "--minimized", "自启必须进托盘、不弹主窗"
    assert a.plist_path() == tmp_path / a.PLIST_NAME


def test_launchd_plist_escapes_paths(monkeypatch, tmp_path):
    """路径里的 & < > 必须转义，否则 plist 解析失败 = 「开关看着开着、实际不生效」。"""
    import autostart as a
    monkeypatch.setattr(a, "sys", SimpleNamespace(platform="darwin", frozen=False,
                                                  executable=sys.executable))
    monkeypatch.setattr(a, "launch_agents_dir", lambda: tmp_path)
    weird = Path("D:/a & b <c>")
    data = plistlib.loads(a.plist_xml(weird).encode("utf-8"))
    assert data["ProgramArguments"] == a.command_parts(weird)


def test_launchd_plist_command_comes_only_from_command_parts(monkeypatch, tmp_path):
    """安全纪律：plist 里的命令**只能**来自 command_parts（不接受任何用户输入片段）。

    参数化几个 root（含带空格与 XML 特殊字符的）——ProgramArguments 必须与
    command_parts(root) **逐字相同**：既证明「命令由本模块固定构造」，也证明转义不改变语义。"""
    import autostart as a
    monkeypatch.setattr(a, "sys", SimpleNamespace(platform="darwin", frozen=False,
                                                  executable=sys.executable))
    monkeypatch.setattr(a, "launch_agents_dir", lambda: tmp_path)
    for root in (Path("D:/proj"), Path("/Users/x/My Proj"), Path("D:/a & b <c>")):
        data = plistlib.loads(a.plist_xml(root).encode("utf-8"))
        assert data["ProgramArguments"] == a.command_parts(root)
        assert data["ProgramArguments"][-1] == "--minimized"


# ================================================================ ④ desktop 降级
class _NoNative:
    pass


class _NativeNone:
    native = None


class _HandleBoom:
    class _H:
        def ToInt32(self):
            raise AttributeError("窗口尚未创建")

    native = _H()


class _HandleZero:
    native = SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 0))


@pytest.mark.parametrize("win", [_NoNative(), _NativeNone(), _HandleBoom(), _HandleZero()])
def test_desktop_posix_branches_never_raise(win, monkeypatch):
    """非 Windows + 各种畸形窗口对象：五个入口都不得抛（Mac 上没窗口壳也得能起服务）。"""
    import desktop
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="posix"))

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError(f"非 Windows 分支不该碰 ctypes.windll.{name}")

    monkeypatch.setattr(desktop, "ctypes", SimpleNamespace(windll=_Boom()))
    native = getattr(win, "native", None) is not None
    assert desktop._native_present(win) is native
    # 非 Windows 的存活判据 = native 已创建（shown_ok=None 即首次尝试放行）
    assert desktop._float_alive(win, None) is native
    assert desktop.set_click_through(win, True) is False          # 恒降级
    assert desktop._virtual_screen() is None                     # 不设防路径
    assert desktop._float_position({}) == {}                     # 交窗口管理器
    assert desktop._warn_already_running() is None               # 日志降级、不弹框


def test_desktop_posix_degradation_semantics(monkeypatch):
    """降级语义（不只是「不抛」）：native 未建出来 → 判死（宽限窗后按既有路径降级）；
    native 在 → 首次尝试放行；试过但没成功 → 判死。"""
    import desktop
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(desktop, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=SimpleNamespace(IsWindow=lambda h: 1))))
    assert desktop._float_alive(_NoNative(), None) is False
    assert desktop._float_alive(_NativeNone(), True) is False
    assert desktop._float_alive(_HandleBoom(), None) is True     # native 在 = 放行首次
    assert desktop._float_alive(_HandleBoom(), True) is True
    assert desktop._float_alive(_HandleBoom(), False) is False   # 试过没成功 → 判死


# ================================================================ ⑤ requirements markers
def _requirements(path: Path) -> list:
    import packaging.requirements as pr
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(pr.Requirement(line))
    return out


def test_requirements_markers_pin_the_platform_specific_packages():
    """平台专属依赖必须带 marker：pyaudiowpatch(win32) / mlx-whisper(darwin)。

    断言到**求值**层面（不是「有没有 marker 这个词」）：在本机 win32 上
    pyaudiowpatch 要装、mlx-whisper 必须被跳过——这正是「Windows 安装结果不变」的定义。"""
    reqs = {r.name: r for r in _requirements(ROOT / "requirements.txt")}
    assert "pyaudiowpatch" in reqs and "mlx-whisper" in reqs
    pw = reqs["pyaudiowpatch"].marker
    mlx = reqs["mlx-whisper"].marker
    assert pw is not None and "sys_platform" in str(pw) and "win32" in str(pw)
    assert mlx is not None and "sys_platform" in str(mlx) and "darwin" in str(mlx)
    assert pw.evaluate() is True, "Windows 上 pyaudiowpatch 必须仍然安装"
    assert mlx.evaluate() is False, "Windows 上 mlx-whisper 必须被跳过"


def test_other_requirements_are_platform_neutral():
    """其余依赖不许被顺手加 marker（加了就不是「跨平台同一套依赖」了）。"""
    for r in _requirements(ROOT / "requirements.txt"):
        if r.name in ("pyaudiowpatch", "mlx-whisper"):
            continue
        assert r.marker is None, f"{r.name} 不该带 marker: {r.marker}"


def test_dev_requirements_do_not_duplicate_runtime_packages():
    """requirements-dev.txt 只放开发工具；平台专属运行依赖不许在那边再列一遍
    （两处各一份 = 迟早漂移），相关说明只该以注释形式出现。"""
    dev = _requirements(ROOT / "requirements-dev.txt")
    names = {r.name.lower() for r in dev}
    assert "mlx-whisper" not in names and "pyaudiowpatch" not in names
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "mlx-whisper" in text and "sys_platform" in text, "至少要留一行说明指向 requirements.txt"


# ================================================================ ⑥ ROOT 的 macOS 分支
def test_frozen_root_uses_application_support_on_mac(monkeypatch):
    """monkeypatch sys.platform + sys.frozen → Application Support 路径形状。

    注意范围（如实声明）：main 在**导入时**就定死了 ROOT，这里验的是那个决定的
    **唯一定点** `_frozen_root()`（导入分支就是它的调用方，接线另有一条源级断言）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    got = main._frozen_root()
    assert got == Path.home() / "Library" / "Application Support" / "LectureLens"
    assert got.is_absolute() and got.name == "LectureLens"
    assert "Library" in got.parts and "Application Support" in got.parts
    monkeypatch.setattr(sys, "platform", "win32")
    assert main._frozen_root() == Path(sys.executable).resolve().parent


def test_frozen_root_wiring_is_real():
    """源级接线：frozen 分支必须真的走 `_frozen_root()`，且 darwin 分支会确保目录存在。
    光有纯函数没人调用 = 白写（Mac 的数据又会落进 .app 里）。"""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    frozen_block = src.split('if getattr(sys, "frozen", False):', 1)[1].split("else:", 1)[0]
    assert "ROOT = _frozen_root()" in frozen_block, "frozen 的 ROOT 必须来自 _frozen_root()"
    assert 'sys.platform == "darwin"' in frozen_block
    assert "ROOT.mkdir(parents=True, exist_ok=True)" in frozen_block, \
        "macOS 首次运行要给数据目录建好（否则后面写 config/日志全失败）"
    assert '_MEIPASS' in frozen_block, "只读资源（web/glossary_packs）仍必须走 _MEIPASS"


# ================================================================ ⑦ 指南事实对齐
GUIDE = ROOT / "docs" / "mac-guide.md"


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_guide_exists_utf8_without_bom():
    assert GUIDE.is_file(), f"缺 {GUIDE.relative_to(ROOT)}"
    raw = GUIDE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "指南不得带 BOM（仓库一律按 utf-8 读）"
    raw.decode("utf-8")


def test_guide_covers_the_required_sections():
    """ask 点名的八块内容都在（缺哪块都是没写完）。"""
    t = _guide_text()
    for key in ("Xcode", "Command Line Tools", "Homebrew", "BlackHole", "辅助功能",
                "麦克风", "requirements.txt", "--check", "build_macos.py", "iconutil",
                "codesign", "Gatekeeper", "xattr", "验收", "已知限制", "logs/lecturelens.log"):
        assert key in t, f"指南缺「{key}」"


def test_guide_states_the_acceptance_checklist():
    """真机验收清单要逐项可勾：ask 点名的九个动作/判据都得能对上。"""
    t = _guide_text()
    for item in ("RTF", "BlackHole", "launchd", "开机自启", "托盘", "悬浮条",
                 "手机同屏", "PDF", "--check"):
        assert item in t, f"验收清单缺「{item}」"


def test_guide_facts_match_the_code():
    """指南里的路径/ID/命令必须与代码里的唯一事实源一致（改一处忘另一处立刻红）。"""
    import autostart as a
    import build_macos as bm
    t = _guide_text()
    assert bm.MAC_DATA_DIR_REL in t or "Library/Application Support/LectureLens" in t
    assert bm.BUNDLE_ID in t
    assert a.LAUNCH_AGENT_LABEL in t and a.PLIST_NAME in t, "launchd 的 Label/plist 名要写对"
    assert lb_mod.BLACKHOLE_DEFAULT_NAME in t
    assert "build_macos.py" in t and "build_exe.py" in t   # 两个平台的打包脚本都要点名
    assert "com.apple.quarantine" in t


def test_readme_links_the_mac_guide():
    """README 要指向指南（否则没人找得到它）。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/mac-guide.md" in readme
