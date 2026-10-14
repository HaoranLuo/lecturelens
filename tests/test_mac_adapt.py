# -*- coding: utf-8 -*-
"""tests/test_mac_adapt.py — Mac 适配第一批：桌面壳 / 开机自启 / 全局快捷键 / 前端文案。

开发机是 Windows，故「非 Windows 分支」一律用**替身平台**驱动（把模块看到的
sys/os 换成 darwin/posix，把 ctypes.windll 与 launchctl 换成碰一下就炸/只记账的
替身）——这正是"非侵入路径"的证明（与 tests/test_power_notice.py 同款手法）：

① desktop.py 五处 win32 调用在非 Windows 下**一次都不碰**，且按既有降级口径返回；
   悬浮条存活判据在非 Windows 下**不会把浮窗判死**（本批点名的「必须修」——
   判死会让轮询线程写回 config.floating_bar=false，把悬浮条强制关掉）；
② autostart 的 launchd 分支：plist 内容（RunAtLoad/KeepAlive/ProgramArguments）、
   写入回读、幂等删除、launchctl 失败只降级成 warning、plist 解析失败如实报错；
③ hotkeys 的 cmd/command 别名（两个平台的规范形）与 macOS 辅助功能授权文案
   （非 darwin 的 reason/提示文案逐字不变）；
④ index.html / float.html 的平台化文案与接线（结构断言，同本仓前端条目口径）。

Windows 侧行为不变由既有用例继续钉住：test_float（穿透降级）、test_autostart
（HKCU Run 读写/API）、test_hotkeys（白名单/降级/配置键）、desktop --check（门禁）。
"""
import logging
import os
import plistlib
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import autostart as a
import desktop
import hotkeys as hk

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = (os.name == "nt") and (sys.platform == "win32")
INDEX_HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
FLOAT_HTML = (ROOT / "app" / "web" / "float.html").read_text(encoding="utf-8")


def _fake_sys(platform: str) -> SimpleNamespace:
    """替身 sys：只带被测代码真正读的属性（platform/executable/frozen）。"""
    return SimpleNamespace(platform=platform, executable=sys.executable, frozen=False)


class _Boom:
    """碰一下就炸的属性代理：非 Windows 分支若偷碰 Win32 API，用例立刻红。"""

    def __getattr__(self, name):
        raise AssertionError(f"非 Windows 分支不该访问 ctypes.windll.{name}")


@pytest.fixture
def as_posix(monkeypatch):
    """让 desktop 模块以为自己在 macOS/Linux：os.name=posix + ctypes.windll 变炸弹。"""
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(desktop, "ctypes", SimpleNamespace(windll=_Boom()))
    return desktop


# ---------------- ① desktop.py：非 Windows 分支 ----------------

def test_virtual_screen_none_on_posix_keeps_lenient_path(as_posix):
    """多屏包围盒：非 Windows 取不到 → None = 既有「不设防」路径（一律判真）。"""
    assert as_posix._virtual_screen() is None
    assert as_posix._geometry_visible(-5000, -5000, 1280, 800) is True
    assert as_posix._point_on_screen(-5000, -5000) is True


def test_float_position_hands_position_to_window_manager_on_posix(as_posix):
    """悬浮条默认位置：非 Windows 不猜坐标（交给 pywebview 默认位）；记忆值仍优先。"""
    assert as_posix._float_position({}) == {}
    assert as_posix._float_position({"float_x": "100", "float_y": "200"}) == \
        {"x": 100, "y": 200}
    assert as_posix._float_position({"float_x": "abc", "float_y": "200"}) == {}


def test_set_click_through_returns_false_on_posix(as_posix):
    """点击穿透：非 Windows 恒 False → 走既有降级（浮窗保持可点击可关闭）。"""

    class _Win:
        native = object()

    assert as_posix.set_click_through(_Win(), True) is False
    assert as_posix.set_click_through(_Win(), False) is False


def test_warn_already_running_falls_back_to_log_on_posix(as_posix, caplog):
    """双开提示：非 Windows 没有 MessageBoxW → 降级为日志（碰 windll 会被炸弹拦住）。"""
    with caplog.at_level(logging.WARNING, logger="desktop"):
        as_posix._warn_already_running()
    assert any("另有实例在运行" in r.getMessage() for r in caplog.records)


def test_warn_already_running_windows_path_unchanged(monkeypatch):
    """Windows 路径不变：仍是 MessageBoxW（替身记账，不真弹窗）。"""
    calls: list = []

    class _U32:
        def MessageBoxW(self, *args):
            calls.append(args)
            return 1

    monkeypatch.setattr(desktop, "ctypes",
                        SimpleNamespace(windll=SimpleNamespace(user32=_U32())))
    desktop._warn_already_running()
    assert calls and calls[0][0] == 0 and "已在运行" in calls[0][1]


def test_native_present_only_proves_creation(as_posix):
    """native 缺失/None → False；有 native → True（它只证明「创建过」，见 _float_alive）。"""

    class _None:
        native = None

    class _Ok:
        native = object()

    assert as_posix._native_present(_None()) is False
    assert as_posix._native_present(_Ok()) is True
    assert as_posix._native_present(object()) is False     # 连属性都没有


def test_float_alive_posix_does_not_kill_the_float_bar(as_posix):
    """**本批「必须修」**：非 Windows 的存活判据 = native 未销毁 + 曾成功 show/hide。

    - shown_ok=None（还没试过）→ 放行首次尝试（否则「要 show 成功才算活、要活才去
      show」互锁，Mac 上浮窗永远起不来）；
    - shown_ok=False（试过、没成功）→ False（交宽限窗；到期后按既有降级写回 config）；
    - native 没有 → False（子窗没建出来，同 Windows 的创建失败）。"""

    class _Created:
        native = object()

    class _NotCreated:
        native = None

    assert as_posix._float_alive(_Created(), None) is True
    assert as_posix._float_alive(_Created(), True) is True
    assert as_posix._float_alive(_Created(), False) is False
    assert as_posix._float_alive(_NotCreated(), None) is False
    assert as_posix._float_alive(_NotCreated(), True) is False


@pytest.mark.skipif(not IS_WIN, reason="Windows 侧判据只在 Windows 上真调")
def test_float_alive_windows_path_still_uses_iswindow():
    """Windows 路径逐字不变：判据仍是 user32.IsWindow(句柄)（shown_ok 不参与）。"""

    class _BadHandle:
        def ToInt32(self):
            raise AttributeError("no hwnd")

    class _Bad:
        native = _BadHandle()

    class _Zero:
        native = SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 0))

    assert desktop._float_alive(_Bad()) is False
    assert desktop._float_alive(_Bad(), True) is False          # 参数不影响判据
    assert desktop._float_alive(_Zero()) is False               # 真调 IsWindow(0) == 0


# ---------------- ② autostart：launchd 分支 ----------------

@pytest.fixture
def darwin(tmp_path, monkeypatch):
    """把 autostart 看到的世界切成 macOS：平台=darwin + LaunchAgents 目录 → tmp_path。
    返回「本应用 plist 的路径」（目录已建好，等价于 ~/Library/LaunchAgents 存在）。"""
    monkeypatch.setattr(a, "sys", _fake_sys("darwin"))
    d = tmp_path / "LaunchAgents"
    d.mkdir()
    monkeypatch.setattr(a, "launch_agents_dir", lambda: d)
    return d / a.PLIST_NAME


def test_plist_xml_is_a_valid_launch_agent(darwin):
    """plist 逐字要求（ask）：RunAtLoad=true、KeepAlive=false、
    ProgramArguments=[<命令>, --minimized]；入口仍是桌面壳。"""
    data = plistlib.loads(a.plist_xml(Path("D:/proj")).encode("utf-8"))
    assert data["Label"] == a.LAUNCH_AGENT_LABEL
    assert data["ProgramArguments"] == a.command_parts(Path("D:/proj"))
    assert data["ProgramArguments"][-1] == "--minimized"
    assert data["ProgramArguments"][1].endswith(str(Path("app") / "desktop.py"))
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False


def test_plist_xml_escapes_special_paths(darwin):
    """路径里的 & < > 必须转义：否则 plist 解析失败 = 「开关看着开着、实际不生效」。"""
    weird = Path("D:/a & b <c>")
    data = plistlib.loads(a.plist_xml(weird).encode("utf-8"))
    assert data["ProgramArguments"] == a.command_parts(weird)


def test_state_not_enabled_when_plist_absent(darwin):
    st = a.state(Path("D:/proj"))
    assert st["ok"] is True and st["error"] is None
    assert st["enabled"] is False and st["command"] is None
    assert st["platform_ok"] is True
    assert st["value_name"] == a.LAUNCH_AGENT_LABEL
    assert st["key_path"].startswith("~/Library/LaunchAgents/")
    assert "warning" not in st


def test_state_enabled_but_stale_is_its_own_state(darwin):
    """plist 指向旧命令（换过项目位置/重建 venv 的残留）→ 独立状态 + warning。"""
    darwin.write_text(a.plist_xml(Path("C:/old")), encoding="utf-8")
    st = a.state(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is True and st["matches"] is False
    assert "warning" in st and "不一致" in st["warning"]


def test_enable_writes_plist_and_bootstraps(darwin, monkeypatch):
    """enable = 写 plist + （先 bootout 再）bootstrap gui/<uid> <plist>，写完回读校验。"""
    calls: list = []

    def _lt(*args):
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(a, "_launchctl", _lt)
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is True and st["matches"] is True
    assert darwin.is_file()
    assert calls[0][0] == "bootout"                       # 幂等：先卸下
    assert calls[1][0] == "bootstrap"
    assert calls[1][1].startswith("gui/") and calls[1][2] == str(darwin)


def test_enable_is_idempotent_readback_after_rewrite(darwin, monkeypatch):
    """重复 enable（用户换过项目位置后再开一次）= 覆盖 plist 并回读一致。"""
    monkeypatch.setattr(a, "_launchctl", lambda *args: (0, ""))
    a.enable(Path("D:/old"))
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is True and st["matches"] is True
    assert a.command_parts(Path("D:/proj"))[1] in darwin.read_text(encoding="utf-8")


def test_enable_bootstrap_failure_only_warns(darwin, monkeypatch):
    """bootstrap 失败**不**算假成功也不算误报失败：plist 已在位（下次登录会拉起）→
    ok=True + warning（本次会话没加载），设置页会把这句话显示出来。"""

    def _lt(*args):
        return (0 if args[0] == "bootout" else 5), "Bootstrap failed: 5: Input/output error"

    monkeypatch.setattr(a, "_launchctl", _lt)
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is True
    assert "bootstrap" in st["warning"] and "下次登录" in st["warning"]


def test_enable_write_failure_is_visible(darwin, tmp_path, monkeypatch):
    """plist 写不进去（LaunchAgents 建不出来）→ ok=false + 原因，绝不静默。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(a, "plist_path", lambda: blocker / "sub" / a.PLIST_NAME)
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is False and "写入 LaunchAgent plist 失败" in st["error"]


def test_disable_bootouts_and_removes_plist(darwin, monkeypatch):
    calls: list = []
    monkeypatch.setattr(a, "_launchctl",
                        lambda *args: (calls.append(args) or (0, "")))
    darwin.write_text(a.plist_xml(Path("D:/proj")), encoding="utf-8")
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is False
    assert not darwin.exists()
    assert calls[0][0] == "bootout" and calls[0][1].startswith("gui/")


def test_disable_is_idempotent_when_absent(darwin, monkeypatch):
    """没加载过（bootout 报错）且 plist 不在 → 目标状态已达成，ok=true。"""
    monkeypatch.setattr(a, "_launchctl",
                        lambda *args: (5, "Boot-out failed: 5: Input/output error"))
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is False


def test_disable_error_is_visible(darwin, monkeypatch):
    """删除失败（权限）→ ok=false + 原因。"""
    monkeypatch.setattr(a, "_launchctl", lambda *args: (0, ""))
    darwin.write_text(a.plist_xml(Path("D:/proj")), encoding="utf-8")
    monkeypatch.setattr(Path, "unlink",
                        lambda self, **kw: (_ for _ in ()).throw(PermissionError(1, "拒绝")))
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is False and "删除 LaunchAgent plist 失败" in st["error"]


def test_malformed_plist_is_an_error_not_a_silent_off(darwin):
    """plist 存在但解析不了 → ok=false（「读不到就是读不到」，不假装未启用）。"""
    darwin.write_text("not a plist at all", encoding="utf-8")
    st = a.state(Path("D:/proj"))
    assert st["ok"] is False and st["enabled"] is False
    assert "plist" in st["error"]


def test_plist_without_program_arguments_is_an_error(darwin):
    """不是本应用写的 plist（没有 ProgramArguments）→ 明确报错。"""
    darwin.write_bytes(plistlib.dumps({"Label": a.LAUNCH_AGENT_LABEL}))
    st = a.state(Path("D:/proj"))
    assert st["ok"] is False and "ProgramArguments" in st["error"]


def test_platform_ok_is_false_on_other_platforms(monkeypatch):
    """其它平台（Linux 等）：platform_ok=false + 明确原因（不假装未启用）。"""
    monkeypatch.setattr(a, "sys", _fake_sys("linux"))

    def _boom():
        raise ImportError("no module named 'winreg'")

    monkeypatch.setattr(a, "_winreg", _boom)
    st = a.state(Path("D:/proj"))
    assert st["platform_ok"] is False and st["ok"] is False and st["error"]


def test_windows_state_unchanged_platform_ok_and_key_path():
    """Windows 侧字段口径不变（本机真跑 winreg 分支，且 platform_ok=true）。"""
    st = a.state(ROOT)
    assert st["ok"] is True
    assert st["platform_ok"] is True
    assert st["key_path"].startswith("HKCU\\")
    assert st["value_name"] == a.VALUE_NAME


# ---------------- ③ hotkeys：cmd/command 与 macOS 授权文案 ----------------

def test_cmd_and_command_aliases_normalize_to_the_library_target():
    """cmd/command 与 win/windows/meta/super 归一成同一个键（keyboard 库的归一目标
    在 Windows 上是 windows：实跑 keyboard.parse_hotkey 三种写法同一元组）。"""
    assert hk.normalize("cmd+alt+l") == "alt+windows+l"
    assert hk.normalize("command+shift+f5") == hk.normalize("win+shift+f5")
    assert hk.combo_problem("cmd+alt+l") is None
    assert hk.combo_problem("command+l") is None


def test_meta_key_canonical_is_command_on_mac(monkeypatch):
    """darwin 上规范名 = command（设置页显示的才是用户键盘上真有的那个键）；
    在 Windows 写的 "windows+alt+l" 配置在 Mac 上仍能读（同一份规范形）。"""
    monkeypatch.setattr(hk, "sys", _fake_sys("darwin"))
    assert hk.normalize("cmd+alt+l") == "alt+command+l"
    assert hk.normalize("windows+alt+l") == "alt+command+l"
    assert hk.normalize("meta+alt+1") == "alt+command+1"
    assert "command" in hk.whitelist_hint()


def test_windows_copy_unchanged_for_whitelist_and_hint(monkeypatch):
    """非 darwin 文案逐字不变：白名单提示与授权提示（空串）。"""
    monkeypatch.setattr(hk, "sys", _fake_sys("win32"))
    assert hk.whitelist_hint() == ("修饰键 ctrl/alt/shift/windows 至少一个 + 主键一个"
                                   "（a-z / 0-9 / f1-f12）")
    assert hk.accessibility_hint() == ""
    assert hk._with_accessibility_hint("register: x: boom") == "register: x: boom"
    assert hk.idle_state()["hint"] == ""


def test_mac_accessibility_hint_is_delivered_to_the_settings_page(monkeypatch):
    """darwin：授权说明既进降级 reason，也随 state()/idle_state() 下发（设置页据此
    在状态行下面显示一句「先给辅助功能授权」）。"""
    monkeypatch.setattr(hk, "sys", _fake_sys("darwin"))
    assert "辅助功能" in hk.accessibility_hint()
    assert "辅助功能" in hk.idle_state()["hint"]
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None}, enabled=False)
    assert "辅助功能" in mgr.state()["hint"]
    assert "辅助功能" in hk._with_accessibility_hint("register: x: boom")


class _FailingKeyboard(types.ModuleType):
    def __init__(self, exc):
        super().__init__("keyboard")
        self.exc = exc

    def add_hotkey(self, combo, callback, **kw):
        raise self.exc


def test_register_failure_reason_carries_mac_hint(monkeypatch):
    monkeypatch.setattr(hk, "sys", _fake_sys("darwin"))
    monkeypatch.setitem(sys.modules, "keyboard", _FailingKeyboard(ValueError("Error 13")))
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None}, enabled=True)
    st = mgr.start()
    assert st["active"] is False and st["reason"].startswith("register:")
    assert "辅助功能" in st["reason"]


def test_register_failure_reason_has_no_hint_on_windows(monkeypatch):
    """Windows 的降级 reason 逐字不变（不加任何平台尾巴）。"""
    monkeypatch.setattr(hk, "sys", _fake_sys("win32"))
    monkeypatch.setitem(sys.modules, "keyboard", _FailingKeyboard(ValueError("boom")))
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None}, enabled=True)
    st = mgr.start()
    assert st["reason"] == "register: toggle_pause=ctrl+alt+l: ValueError: boom"


# ---------------- ④ 前端：平台化文案与接线（结构断言） ----------------

def test_index_html_autostart_target_is_platform_driven():
    """启动项位置由服务端 key_path 下发（Windows: HKCU Run / macOS: LaunchAgents），
    前端不自己判平台；占位元素在（JS 未跑时显示 Windows 默认文案）。"""
    assert re.search(r'id="setAutostartTarget"', INDEX_HTML)
    assert "d.key_path" in INDEX_HTML
    assert "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in INDEX_HTML


def test_index_html_autostart_renders_launchctl_warning():
    """已启用分支也要显示 warning（macOS「plist 已写入但本次会话未加载」）。"""
    assert "d.warning ?" in INDEX_HTML


def test_index_html_hotkey_hint_line_and_aliases():
    """热键卡：平台授权说明元素 + cmd/command 别名说明（macOS 的 ⌘）。"""
    assert re.search(r'id="setHotkeyHint"[^>]*hidden', INDEX_HTML)
    assert "hk.hint" in INDEX_HTML
    assert "cmd/command" in INDEX_HTML


def test_float_page_click_through_copy_names_windows_only():
    """穿透按钮的提示写明仅 Windows（macOS 自动降级为可关闭窗口）。"""
    assert "仅 Windows" in FLOAT_HTML
