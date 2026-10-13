# -*- coding: utf-8 -*-
"""tests/test_interaction.py — p3-5（B12 交互细节）：版式记忆 / 托盘-快捷键一致性。

四项交付里，能在 headless 单测里锁死的两件：
① **窗口版式记忆**：记忆值的「读→校验→用」与「用→写回→落盘」两条链路，
   含多显示器最要紧的两个坑——负坐标合法、屏外记忆必须降级（否则窗口「存在但
   看不见」，用户以为程序没起来）；
② **托盘菜单与快捷键行为一致**：两者必须落在同一份写入口（service.toggle_*），
   且托盘菜单把当前键位显示出来——一致性要「可核对」，不能只靠口头声明。

另外两件是前端/真机性质，不在本文件：键盘可达的焦点环（真浏览器核验）、
空状态 vs 加载态文案（真浏览器核验，见交付说明）。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import desktop
import hotkeys as hk
import main

ROOT = Path(__file__).resolve().parent.parent
#: conftest 的 isolate_global_hotkeys（autouse）会把 main.apply_hotkeys 换成记账桩
#: （防测试真注册全局热键）。一致性用例要的是**真实现**，故在模块导入时抓一份
#: 真身——与 test_hotkeys.REAL_APPLY_HOTKEYS 同款。
REAL_APPLY_HOTKEYS = main.apply_hotkeys


# ---------------- ① 版式记忆：读 + 校验 ----------------

def test_point_on_screen_accepts_inside_and_rejects_outside():
    """点口径：屏内 True（含 0,0 与负坐标），屏外 False。"""
    vs = desktop._virtual_screen()
    if vs is None:
        pytest.skip("无桌面会话，取不到虚拟屏指标")
    vx, vy, vw, vh = vs
    assert desktop._point_on_screen(vx + 10, vy + 10) is True
    assert desktop._point_on_screen(vx, vy) is True
    assert desktop._point_on_screen(vx + vw - 1, vy + vh - 1) is True
    assert desktop._point_on_screen(vx + vw + 5000, vy) is False
    assert desktop._point_on_screen(0, 0) is True          # 主屏原点
    assert desktop._point_on_screen(-100000, -100000) is False


def test_window_geometry_visible_needs_usable_area():
    """主窗口径：至少要有一块能看见能点的面积（120px），只露一角不算。"""
    vs = desktop._virtual_screen()
    if vs is None:
        pytest.skip("无桌面会话")
    vx, vy, vw, vh = vs
    assert desktop._geometry_visible(vx + 50, vy + 50, 1280, 800) is True
    assert desktop._geometry_visible(vx + vw - 10, vy + vh - 10, 1280, 800) is False
    assert desktop._geometry_visible(vx + vw + 9999, vy, 1280, 800) is False
    # 负坐标（主屏左侧的显示器）必须判为可用——写成非负区间就会误杀
    assert desktop._geometry_visible(vx - 400, vy + 100, 1280, 800) is True


def test_saved_point_handles_junk():
    ok = desktop._saved_point({"x": None, "y": None}, "x", "y")
    assert ok is None
    assert desktop._saved_point({"x": "abc", "y": 5}, "x", "y") is None
    assert desktop._saved_point({"x": 10, "y": None}, "x", "y") is None


@pytest.fixture
def geom_cfg(monkeypatch):
    """把 desktop 读配置的入口换成内存字典（不碰真实 config.json）。"""
    box = {"cfg": {}}
    monkeypatch.setattr(desktop.service, "load_config", lambda: dict(box["cfg"]))
    return box


def test_window_geometry_default_without_memory(geom_cfg):
    kw = desktop._window_geometry()
    assert kw == {"width": desktop.WINDOW_SIZE[0], "height": desktop.WINDOW_SIZE[1]}
    assert "x" not in kw and "y" not in kw      # 不传坐标 = create_window 居中


def test_window_geometry_uses_memory(geom_cfg):
    geom_cfg["cfg"] = {"window_w": 1000, "window_h": 700,
                       "window_x": 50, "window_y": 60}
    assert desktop._window_geometry() == {"width": 1000, "height": 700,
                                          "x": 50, "y": 60}


def test_window_geometry_drops_offscreen_position(geom_cfg):
    """换过显示器：位置失效但**尺寸仍保留**（只降级位置，不整条作废）。"""
    geom_cfg["cfg"] = {"window_w": 1000, "window_h": 700,
                       "window_x": 999999, "window_y": 999999}
    kw = desktop._window_geometry()
    assert kw == {"width": 1000, "height": 700}
    assert "x" not in kw


def test_window_geometry_clamps_absurd_size(geom_cfg):
    """手改配置成 10×10 → 夹到最小可点尺寸（否则窗口小到点不到按钮）。"""
    geom_cfg["cfg"] = {"window_w": 10, "window_h": 10}
    kw = desktop._window_geometry()
    assert kw["width"] == desktop.WINDOW_MIN_SIZE[0]
    assert kw["height"] == desktop.WINDOW_MIN_SIZE[1]


def test_window_geometry_survives_bad_types(geom_cfg):
    geom_cfg["cfg"] = {"window_w": "маленький", "window_h": None,
                       "window_x": "?", "window_y": 5}
    kw = desktop._window_geometry()
    assert kw == {"width": desktop.WINDOW_SIZE[0], "height": desktop.WINDOW_SIZE[1]}


def test_float_position_prefers_memory_then_default(geom_cfg):
    d = desktop._float_position({})
    assert "x" in d and "y" in d                     # 默认：底部居中偏上
    assert desktop._float_position({"float_x": 100, "float_y": 200}) == \
        {"x": 100, "y": 200}
    assert desktop._float_position({"float_x": 999999, "float_y": 999999}) == d


# ---------------- ① 版式记忆：写回 ----------------

class FakeWin:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class DeadWin:
    """已销毁的窗口：读几何抛异常（pywebview destroy 后即如此）。"""
    @property
    def x(self):
        raise RuntimeError("window destroyed")


def test_save_geometry_writes_and_persists(monkeypatch):
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    desktop._save_geometry(FakeWin(120, 80, 1400, 900), FakeWin(30, 40, 720, 150))
    cfg = main.runtime["config"]
    assert (cfg["window_x"], cfg["window_y"]) == (120, 80)
    assert (cfg["window_w"], cfg["window_h"]) == (1400, 900)
    assert (cfg["float_x"], cfg["float_y"]) == (30, 40)
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["window_w"] == 1400 and saved["float_x"] == 30


def test_save_geometry_never_clobbers_rest_of_config(monkeypatch):
    """**评审员1 回归**：落盘必须以磁盘现值为基底，只覆盖 6 个几何键。

    旧实现直接拿 `service.runtime["config"]` 整份落盘，而它初值是 DEFAULT_CONFIG
    （磁盘配置要到 asr_loop 跑到载入那一步才被换进来）。若退出发生在那一刻之前
    （asr_loop 早退/异常），整份落盘 = 把 config.json 静默重置成默认值——实测会清掉
    **auth_token**（LAN/手机端直接失去配对）与用户改过的 model/lang。"""
    seeded = {"model": "small", "lang": "zh", "auth_token": "TOKEN-STUB",
              "window_w": 1234, "window_h": 999}
    main.CONFIG_PATH.write_text(json.dumps(seeded), encoding="utf-8")
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))  # 恶劣前提

    desktop._save_geometry(FakeWin(0, 0, 1400, 900), None)

    after = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert after["model"] == "small", "整份落盘把 model 重置成了默认值"
    assert after["lang"] == "zh", "整份落盘把 lang 重置成了默认值"
    assert after["auth_token"] == "TOKEN-STUB", \
        "整份落盘清掉了 auth_token——LAN/手机端会静默失去配对"
    assert (after["window_w"], after["window_h"]) == (1400, 900), "几何没写进去"
    # 内存副本也对齐（只动几何键）
    assert main.runtime["config"]["window_w"] == 1400
    assert main.runtime["config"]["model"] == main.DEFAULT_CONFIG["model"]  # 内存不被动


@pytest.mark.parametrize("raw,expect", [
    ((10**9, -10**9, 5, 10**7), (20000, -20000, 320, 20000)),   # 全部越界 → 夹到边界
    ((100, 200, 1280, 800), (100, 200, 1280, 800)),             # 合法值原样
])
def test_save_geometry_clamps_to_config_ranges(monkeypatch, raw, expect):
    """**评审员1 回归（次要问题）**：落盘前必须夹取，否则配置里会留下 set_config
    自己都会拒绝的值（区间表 main.CONFIG_NUM_RANGES），用户下次动配置就撞「非法值」。"""
    main.CONFIG_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    x, y, w, h = raw
    desktop._save_geometry(FakeWin(x, y, w, h), None)
    after = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert (after["window_x"], after["window_y"],
            after["window_w"], after["window_h"]) == expect
    for k in ("window_x", "window_y", "window_w", "window_h"):
        assert main._config_number_problem(k, after[k]) is None, \
            f"{k}={after[k]} 会被 set_config 拒绝——我们不该把它写进配置"


def test_clamp_geom_rejects_unregistered_key():
    assert desktop._clamp_geom("not_a_key", 5) is None


def test_save_geometry_skips_destroyed_windows(monkeypatch):
    """窗口已销毁 → 一个字段都不写（否则会把 None 落盘，下次启动版式全丢）。"""
    monkeypatch.setitem(main.runtime, "config",
                        {**main.DEFAULT_CONFIG, "window_w": 1111,
                         "window_x": 222, "window_y": 333})
    desktop._save_geometry(DeadWin(), DeadWin())
    cfg = main.runtime["config"]
    assert cfg["window_w"] == 1111 and cfg["window_x"] == 222
    assert not main.CONFIG_PATH.exists(), "读不到几何就不该落盘"


def test_save_geometry_tolerates_none_windows(monkeypatch):
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    desktop._save_geometry(None, None)          # 不得抛
    assert not main.CONFIG_PATH.exists()


def test_read_geometry_returns_empty_on_dead_window():
    assert desktop._read_geometry(DeadWin()) == {}


# ---------------- ② 托盘 ↔ 快捷键一致性 ----------------

def _capture_hotkey_actions(monkeypatch, cfg):
    """捕获 apply_hotkeys 实际注册给热键的动作 callable（不打桩行为，只记录）。"""
    captured = {}
    real = hk.HotkeyManager

    class Recorder(real):
        def __init__(self, actions, **kw):
            captured.update(actions)
            super().__init__(actions, **kw)

    monkeypatch.setattr(main.hotkeys_mod, "HotkeyManager", Recorder)
    monkeypatch.setitem(main.runtime, "hotkeys", None)
    monkeypatch.setitem(main.runtime, "config", dict(cfg))
    monkeypatch.setattr(hk.HotkeyManager, "start", lambda self: self.state())
    REAL_APPLY_HOTKEYS(dict(cfg))     # 真实现（见 REAL_APPLY_HOTKEYS 的说明）
    return captured


def test_hotkey_and_tray_toggle_pause_are_equivalent(monkeypatch):
    """热键动作与托盘动作必须产出**同一个状态转移**（同一份写入口）。

    两边分别调用后暂停态应当一致；若哪天有人给其中一边加了额外条件（例如热键
    顺手改了别的键），这里立刻现形。"""
    acts = _capture_hotkey_actions(monkeypatch, main.DEFAULT_CONFIG)
    assert set(acts) >= {"toggle_pause", "toggle_float"}, "热键动作没被注册？"

    main.status["paused"] = False
    acts["toggle_pause"]()                      # 热键那条路
    by_hotkey = bool(main.status["paused"])

    main.status["paused"] = False
    main.toggle_pause("[托盘] ")                # 托盘那条路（desktop._toggle_pause 的实现）
    by_tray = bool(main.status["paused"])

    assert by_hotkey is True and by_tray is True


def test_hotkey_and_tray_toggle_float_are_equivalent(monkeypatch):
    acts = _capture_hotkey_actions(monkeypatch, main.DEFAULT_CONFIG)
    main.runtime["config"]["floating_bar"] = False
    acts["toggle_float"]()
    by_hotkey = bool(main.runtime["config"]["floating_bar"])

    main.runtime["config"]["floating_bar"] = False
    main.toggle_float("[托盘] ")
    by_tray = bool(main.runtime["config"]["floating_bar"])

    assert by_hotkey is True and by_tray is True


def test_tray_callbacks_delegate_to_shared_helpers():
    """源码级防线：托盘回调只准委派，不准内联自己写状态。

    内联会立刻出现两套语义（例如托盘写 False、热键翻转），而两条路写的是同一个
    config/status 键——那正是「行为不一致」的典型来源。"""
    src = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    assert "service.toggle_pause(" in src and "service.toggle_float(" in src
    for bad in ('service.status["paused"] = not',
                'config"]["floating_bar"] = enable'):
        assert bad not in src, f"托盘回调里出现了内联写状态: {bad}"


def test_only_expected_writers_touch_shared_keys():
    """收紧「谁可以写这两个共享键」：新增写入点必须在名单里。

    名单 = ①对外端点（/api/pause·/api/resume·/api/config 的即时键路径）；
    ②桌面壳的两处**降级/关闭**自愈（浮窗不可用写回 false、用户关浮窗等价关闭）。
    任何第 5 个写入点都该先解释清楚它为什么不是「另一套语义」。"""
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    desk_src = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    assert main_src.count('status["paused"] = ') == 3, \
        "main.py 里 status['paused'] 的写入点数量变了——确认新写入点是否与已有语义重复"
    assert main_src.count('cfg["floating_bar"] = ') == 1, \
        "main.py 里 config['floating_bar'] 只应由 toggle_float 写"
    assert desk_src.count('config"]["floating_bar"] = ') == 2, \
        "desktop.py 的两处写入应为「浮窗不可用自愈」与「用户关闭浮窗」"


def test_tray_menu_shows_current_hotkey_bindings(monkeypatch):
    """菜单要把**当前生效**键位显示出来（改过键就显示新键）——一致性可发现。"""
    monkeypatch.setattr(desktop, "_hotkey_hints",
                        lambda: {"toggle_pause": "ctrl+alt+p",
                                 "toggle_float": "ctrl+alt+o"})
    hints = desktop._hotkey_hints()
    m = desktop._build_tray_menu(
        visible=lambda: True, floating=lambda: False, paused=lambda: False,
        click_through=lambda: False, hints=hints,
        acts={k: (lambda *a: None) for k in
              ("toggle_window", "toggle_floating", "toggle_click_through",
               "toggle_pause", "open_browser", "quit")})
    assert m.items[1].text == "悬浮字幕条（CTRL+ALT+O）"
    assert m.items[3].text == "暂停字幕（CTRL+ALT+P）"


def test_hotkey_hints_fall_back_to_defaults(monkeypatch):
    """热键管理器还没建/已降级 → 提示回落默认值（菜单文案不该消失）。"""
    monkeypatch.setitem(main.runtime, "hotkeys", None)
    assert desktop._hotkey_hints() == dict(hk.DEFAULT_BINDINGS)


# ---------------- ② 配置键：白名单与校验 ----------------

def test_geometry_keys_whitelisted_and_validated_config():
    for k in ("window_w", "window_h", "window_x", "window_y", "float_x", "float_y"):
        assert k in main.ALLOWED_CONFIG_KEYS, f"{k} 未进白名单（硬规矩⑦）"
        assert k in main.CONFIG_NUM_RANGES, f"{k} 没有区间校验"
        assert k in main.CONFIG_INT_KEYS, f"{k} 应为整数键（几何值是像素）"
        assert main.DEFAULT_CONFIG[k] is None, "默认应为「未记忆」"


def test_negative_coords_are_legal():
    """多显示器：主屏左侧/上方的屏坐标是负的——非负区间会把合法版式判成非法。"""
    for k in ("window_x", "window_y", "float_x", "float_y"):
        assert main._config_number_problem(k, -1920) is None
    assert main._config_number_problem("window_w", 1280) is None


@pytest.mark.parametrize("key,value", [
    ("window_w", 100),           # 低于下界 320
    ("window_h", 999999),        # 高于上界
    ("window_x", 1280.5),        # 非整数
    ("float_y", True),           # bool 是 int 子类，必须显式排除
])
def test_geometry_invalid_values_rejected(key, value):
    assert main._config_number_problem(key, value) is not None


def test_config_example_documents_geometry():
    ex = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert set(ex) == set(main.ALLOWED_CONFIG_KEYS)
    for k in ("window_w", "window_x", "float_x"):
        assert ex[k] is None, "示例里几何默认值应为 null（表示还没记忆过）"
