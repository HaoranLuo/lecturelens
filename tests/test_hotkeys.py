# -*- coding: utf-8 -*-
"""tests/test_hotkeys.py — p3-1（B12）全局快捷键：白名单 / 注册 / 降级 / 配置键。

本文件锁四件事：
① 键名白名单与规范化（纯函数，降级态下设置页仍靠它校验）；
② HotkeyManager 的三条路径——正常注册、导入失败降级、注册失败降级——
   全部用**假 keyboard 模块**驱动：单测绝不往操作系统装真实键盘钩子
   （conftest 的 isolate_global_hotkeys 已全局兜底，这里再显式自证）；
③ 三个配置键的白名单/校验/持久化/冲突拦截（走 ASGI 直调 /api/config）；
④ status['hotkeys'] 的形状与降级原因可见（设置页唯一的信息出口）。
"""
import asyncio
import json
import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

import main
import hotkeys as hk

# conftest 的 autouse 桩会替换 main.apply_hotkeys——在模块导入时捕获真身，
# 供「真实注册路径」用例使用（与 test_config_safety 捕获 REAL_BUILD_ASR 同款）
REAL_APPLY_HOTKEYS = main.apply_hotkeys


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def hk_env(monkeypatch):
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setitem(main.runtime, "hotkeys", None)
    return main.runtime


class FakeKeyboard(types.ModuleType):
    """假 keyboard：记录注册/注销，可按需在 add_hotkey 上抛错。"""

    def __init__(self, fail_on=None):
        super().__init__("keyboard")
        self.registered, self.removed = {}, []
        self.fail_on = fail_on

    def add_hotkey(self, combo, callback, **kw):
        if self.fail_on is not None and combo == self.fail_on:
            raise RuntimeError("键位被其它程序占用（模拟）")
        self.registered[combo] = callback

        def _remove():
            self.removed.append(combo)
            self.registered.pop(combo, None)
        return _remove


# ---------------- ① 白名单与规范化（纯函数） ----------------

def test_defaults_are_valid_and_distinct():
    for action, combo in hk.DEFAULT_BINDINGS.items():
        assert hk.combo_problem(combo) is None, f"默认绑定 {action}={combo} 自己不合法"
    assert len(set(hk.DEFAULT_BINDINGS.values())) == len(hk.DEFAULT_BINDINGS), \
        "两个动作默认键位不能相同（会一次按键触发两个动作）"
    assert hk.DEFAULT_BINDINGS == {"toggle_pause": "ctrl+alt+l",
                                   "toggle_float": "ctrl+alt+s"}   # ask 指定的键位


def test_config_keys_and_labels_cover_all_actions():
    assert set(hk.ACTION_CONFIG_KEY.values()) == set(hk.HOTKEY_CONFIG_KEYS)
    assert set(hk.BINDING_LABELS) == set(hk.ACTION_CONFIG_KEY), \
        "每个动作都要有中文名（设置页/日志共用这一份，不许两处漂移）"


@pytest.mark.parametrize("combo,expect", [
    ("ctrl+alt+l", "ctrl+alt+l"),
    ("Ctrl+Alt+L", "ctrl+alt+l"),            # 大小写
    ("ctrl + alt + l", "ctrl+alt+l"),        # 空格
    ("control+alt+s", "ctrl+alt+s"),         # control → ctrl
    ("win+alt+1", "alt+windows+1"),          # win → windows，且顺序固定
    ("windows+alt+1", "alt+windows+1"),      # 与上一行必须是同一个规范形
    ("meta+shift+f5", "shift+windows+f5"),
    ("ctrl+alt+f12", "ctrl+alt+f12"),
    ("ctrl+alt+0", "ctrl+alt+0"),
])
def test_normalize_canonical_forms(combo, expect):
    assert hk.normalize(combo) == expect
    assert hk.combo_problem(combo) is None


@pytest.mark.parametrize("combo", [
    "l",                 # 无修饰键：单键热键会在打字时误触发
    "ctrl",              # 只有修饰键
    "ctrl+l+m",          # 两个主键
    "ctrl+alt+xx",       # 不在白名单
    "ctrl++l",           # 空键名
    "ctrl+alt+",         # 尾部空
    "",                  # 空串
    None,                # 类型错
    123,
    "ctrl+ctrl+alt+l",   # 重复修饰键
    "ctrl+alt+space",    # 特殊键名不在白名单（按窄口径设计）
    "alt gr+x",          # 库的修饰键名但不在我们白名单
])
def test_combo_problem_rejects(combo):
    assert hk.combo_problem(combo) is not None, f"{combo!r} 应被拒绝"


def test_normalize_and_combo_problem_agree():
    for combo in list(hk.DEFAULT_BINDINGS.values()) + ["l", "ctrl+l+m", "xx+y"]:
        assert (hk.normalize(combo) is None) == (hk.combo_problem(combo) is not None)


def test_bindings_from_config_falls_back_on_garbage():
    """手改 config.json 写了非法值 → 回落默认，绝不把异常带上启动路径。"""
    got = hk.bindings_from_config({"hotkey_pause": "!!!", "hotkey_float": None})
    assert got == hk.DEFAULT_BINDINGS
    got2 = hk.bindings_from_config({"hotkey_pause": "Ctrl+Alt+P",
                                    "hotkey_float": "win+alt+2"})
    assert got2 == {"toggle_pause": "ctrl+alt+p", "toggle_float": "alt+windows+2"}


# ---------------- ② HotkeyManager 三条路径（假 keyboard，不碰 OS） ----------------

def test_manager_registers_and_unregisters(monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None,
                                    "toggle_float": lambda: None},
                           bindings=hk.DEFAULT_BINDINGS, enabled=True)
    st = mgr.start()
    assert st["active"] is True and st["reason"] is None
    assert set(fake.registered) == {"ctrl+alt+l", "ctrl+alt+s"}
    st2 = mgr.stop()
    assert st2["active"] is False and fake.registered == {}
    assert set(fake.removed) == {"ctrl+alt+l", "ctrl+alt+s"}


def test_manager_import_failure_degrades(monkeypatch):
    """import keyboard 失败（未安装/环境异常）→ 降级，reason 带 import: 前缀。"""
    monkeypatch.setitem(sys.modules, "keyboard", None)  # `import keyboard` 必抛
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None,
                                    "toggle_float": lambda: None},
                           bindings=hk.DEFAULT_BINDINGS, enabled=True)
    st = mgr.start()          # 不得抛出
    assert st["active"] is False
    assert str(st["reason"]).startswith("import:"), st
    assert "仅托盘" in mgr.state()["reason"] or st["reason"].startswith("import:")


def test_manager_register_failure_degrades_all_or_nothing(monkeypatch):
    """一个键位注册失败 → 已注册的也要注销（不留半可用状态），reason 带 register:。"""
    fake = FakeKeyboard(fail_on="ctrl+alt+s")
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None,
                                    "toggle_float": lambda: None},
                           bindings=hk.DEFAULT_BINDINGS, enabled=True)
    st = mgr.start()
    assert st["active"] is False
    assert str(st["reason"]).startswith("register:"), st
    assert fake.registered == {}, "半可用状态必须收敛成全无"


def test_manager_disabled_does_not_touch_os(monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None}, enabled=False)
    st = mgr.start()
    assert st["active"] is False and st["reason"] == "disabled"
    assert fake.registered == {}


def test_manager_callback_exception_is_swallowed(monkeypatch):
    """回调抛异常不能穿透到库的监听线程（否则一次 bug 全部热键失灵）。"""
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    boom = {"n": 0}

    def _bad():
        boom["n"] += 1
        raise ValueError("模拟回调异常")
    mgr = hk.HotkeyManager(actions={"toggle_pause": _bad},
                           bindings={"toggle_pause": "ctrl+alt+l"}, enabled=True)
    mgr.start()
    fake.registered["ctrl+alt+l"]()          # 直接触发回调——不得抛出
    assert boom["n"] == 1


def test_manager_rebind_takes_effect_and_clears_old(monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None},
                           bindings={"toggle_pause": "ctrl+alt+l"}, enabled=True)
    mgr.start()
    mgr.configure(bindings={"toggle_pause": "ctrl+alt+p"})
    mgr.start()
    assert set(fake.registered) == {"ctrl+alt+p"}, "旧键位必须注销（否则「改了还在」)"
    assert "ctrl+alt+l" in fake.removed


# ---------------- ③ main 侧集成：真实 apply_hotkeys（假 keyboard） ----------------

def test_apply_hotkeys_registers_and_updates_status(hk_env, monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    st = REAL_APPLY_HOTKEYS(dict(main.DEFAULT_CONFIG))
    assert st["active"] is True
    assert set(fake.registered) == {"ctrl+alt+l", "ctrl+alt+s"}
    assert main.status["hotkeys"]["active"] is True          # status 同步
    assert main.status["hotkeys"]["bindings"]["toggle_pause"] == "ctrl+alt+l"
    assert main.runtime["hotkeys"] is not None
    main.runtime["hotkeys"].stop()   # 收尾：不留钩子


def test_apply_hotkeys_disabled_by_config(hk_env, monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    cfg = {**main.DEFAULT_CONFIG, "hotkeys_enabled": False}
    st = REAL_APPLY_HOTKEYS(cfg)
    assert st["active"] is False and st["reason"] == "disabled"
    assert fake.registered == {} and main.status["hotkeys"]["reason"] == "disabled"


def test_apply_hotkeys_is_idempotent(hk_env, monkeypatch):
    """重复调用（asr_loop 启动 + 设置页改键都会调）不得叠加注册。"""
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    REAL_APPLY_HOTKEYS(dict(main.DEFAULT_CONFIG))
    REAL_APPLY_HOTKEYS(dict(main.DEFAULT_CONFIG))
    assert set(fake.registered) == {"ctrl+alt+l", "ctrl+alt+s"}
    main.runtime["hotkeys"].stop()


def test_status_shape_matches_manager_state():
    """status 初始化形状 == manager.state() 形状（前端解析同一份结构）。"""
    idle = hk.idle_state()
    mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None})
    assert set(idle) == set(mgr.state())
    assert set(main.status["hotkeys"]) == set(idle)


# ---------------- ④ 配置键：白名单 / 校验 / 持久化 / 冲突（ASGI 直调） ----------------

def test_keys_whitelisted_and_defaulted():
    for k in hk.HOTKEY_CONFIG_KEYS:
        assert k in main.ALLOWED_CONFIG_KEYS
    assert "hotkeys_enabled" in main.ALLOWED_CONFIG_KEYS
    assert main.CONFIG_VALUE_CHOICES["hotkeys_enabled"] == (False, True)
    assert main.DEFAULT_CONFIG["hotkeys_enabled"] is True
    assert main.DEFAULT_CONFIG["hotkey_pause"] == "ctrl+alt+l"
    assert main.DEFAULT_CONFIG["hotkey_float"] == "ctrl+alt+s"


def test_config_example_documents_them():
    from pathlib import Path
    ex = json.loads((Path(__file__).resolve().parent.parent / "config.example.json")
                    .read_text(encoding="utf-8"))
    assert ex["hotkey_pause"] == "ctrl+alt+l" and ex["hotkey_float"] == "ctrl+alt+s"
    assert ex["hotkeys_enabled"] is True


@pytest.mark.parametrize("value,expect", [
    ("ctrl+alt+p", "ctrl+alt+p"),
    ("Ctrl+Alt+P", "ctrl+alt+p"),        # 规范化后落盘
    ("control+shift+F9", "ctrl+shift+f9"),
])
async def test_rebind_persists_normalized(hk_env, value, expect):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"hotkey_pause": value})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"hotkey_pause": expect}   # 存的是规范形
    assert hk_env["config"]["hotkey_pause"] == expect
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["hotkey_pause"] == expect
    assert main.load_config()["hotkey_pause"] == expect      # 重启读得回来


@pytest.mark.parametrize("key,value", [
    ("hotkey_pause", "l"),           # 无修饰键
    ("hotkey_pause", "ctrl+l+m"),    # 两个主键
    ("hotkey_float", "ctrl+alt+zz"),  # 未知键名
    ("hotkey_float", "ctrl+"),       # 缺主键
    ("hotkey_float", 123),           # 类型错
    ("hotkey_float", True),          # bool 是 int 子类，同样要挡
])
async def test_invalid_combo_rejected_without_pollution(hk_env, key, value):
    snapshot = dict(hk_env["config"])
    async with asgi_client() as c:
        r = await c.post("/api/config", json={key: value})
    assert r.status_code == 400, f"{value!r} 应被拒绝: {r.text}"
    assert key in r.json()["error"]
    assert hk_env["config"] == snapshot
    assert not main.CONFIG_PATH.exists(), "非法值不得落盘"


async def test_normalized_equivalent_is_noop(hk_env):
    """"Ctrl+Alt+L" 与现值 "ctrl+alt+l" 是同一个键位——不该被当成一次变更
    （否则 UI 的回填对比会反复触发重注册）。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"hotkey_pause": "Ctrl+Alt+L"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": {}}
    assert not main.CONFIG_PATH.exists()


async def test_duplicate_combo_across_actions_rejected(hk_env):
    """两个动作撞同一组合 → 400（一次按键触发两个动作是 bug，不是特性）。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"hotkey_float": "ctrl+alt+l"})
    assert r.status_code == 400
    assert "hotkey_float" in r.json()["error"] and "hotkey_pause" in r.json()["error"]
    assert hk_env["config"]["hotkey_float"] == main.DEFAULT_CONFIG["hotkey_float"]


async def test_duplicate_detected_across_case(hk_env):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"hotkey_float": "CTRL+ALT+L"})
    assert r.status_code == 400, "规范化后相同的组合必须判为冲突"


async def test_toggle_enabled_key_persists(hk_env):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"hotkeys_enabled": False})
    assert r.status_code == 200
    assert hk_env["config"]["hotkeys_enabled"] is False
    assert json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))["hotkeys_enabled"] is False


async def test_changing_hotkey_triggers_reapply(hk_env):
    """改键必须触发重注册（apply_hotkeys 被调且拿到新配置）——否则「改了不生效」。"""
    seen = []
    main.apply_hotkeys = lambda cfg=None: seen.append(dict(cfg or {}))  # 覆写 conftest 桩
    try:
        async with asgi_client() as c:
            r = await c.post("/api/config", json={"hotkey_pause": "ctrl+alt+p"})
        assert r.status_code == 200
        assert seen, "改键没有触发 apply_hotkeys"
        assert seen[-1]["hotkey_pause"] == "ctrl+alt+p"
    finally:
        main.apply_hotkeys = REAL_APPLY_HOTKEYS


async def test_unrelated_change_does_not_reapply(hk_env):
    """无关键的改动不该重装热键（钩子拆装有成本，也没必要抖一下）。"""
    seen = []
    main.apply_hotkeys = lambda cfg=None: seen.append(1)
    try:
        async with asgi_client() as c:
            r = await c.post("/api/config", json={"fade_lines": 2})
        assert r.status_code == 200
        assert seen == []
    finally:
        main.apply_hotkeys = REAL_APPLY_HOTKEYS


# ---------------- ⑤ 托盘/快捷键共用同一写入口 ----------------

def test_toggle_pause_flips_and_is_shared(hk_env):
    main.status["paused"] = False
    assert main.toggle_pause("[test] ") is True
    assert main.status["paused"] is True
    assert main.toggle_pause("[test] ") is False
    assert main.status["paused"] is False


def test_toggle_float_writes_config_and_persists(hk_env):
    hk_env["config"]["floating_bar"] = False
    assert main.toggle_float("[test] ") is True
    assert hk_env["config"]["floating_bar"] is True
    assert json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))["floating_bar"] is True
    assert main.toggle_float("[test] ") is False
    assert hk_env["config"]["floating_bar"] is False


def test_tray_callbacks_use_shared_helpers():
    """托盘回调必须走 main.toggle_*（p3-1 起热键与托盘共用一份实现）——
    源码级断言：改回内联赋值就会漂移出两套语义。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "desktop.py") \
        .read_text(encoding="utf-8")
    assert "service.toggle_pause(" in src
    assert "service.toggle_float(" in src
