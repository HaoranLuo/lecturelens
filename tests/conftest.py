# -*- coding: utf-8 -*-
"""tests/conftest.py — pytest 全局夹具（升级计划条目 2：测试骨架）。

核心职责（docs/upgrade-plan.md §1 条目 2 ③「conftest 级全局隔离」）：
- 把 app.main 的 CONFIG_PATH / TRANSCRIPTS / COURSES_PATH 重定向到 tmp_path：
  任何测试触发 save_config / hub.start_transcript / 课程 CRUD 都不会碰仓库真实文件；
- 会话级快照仓库 config.json 与 courses.json（内容 + mtime_ns），teardown 时断言未被
  改动——任何测试绕过夹具写坏真实配置都会在这里暴露（test_config_isolation /
  test_courses_api 消费）。

约定：
- 随机性全部来自 smoke_test 合成函数内部的固定种子（rng(42)/rng(7)），无全局种子需要设；
- import main 会连带 import faster_whisper（离线、秒级），全部测试统一承担；
- 纯逻辑测试在本目录跑（秒级）；需要真模型/真服务/真音频的走 scripts/smoke_test.py。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "app"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def isolate_repo_paths(tmp_path, monkeypatch):
    """全局隔离：main.CONFIG_PATH / main.TRANSCRIPTS / main.COURSES_PATH → tmp_path
    （每个测试独立）。p2-1 起 COURSES_PATH 同样必须隔离——课程 CRUD 会落盘
    courses.json，不隔离就会写进仓库真实文件。"""
    import main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(main, "TRANSCRIPTS", tmp_path / "transcripts")
    monkeypatch.setattr(main, "COURSES_PATH", tmp_path / "courses.json")
    yield


@pytest.fixture(autouse=True)
def isolate_global_hotkeys(monkeypatch):
    """p3-1：**测试进程绝不注册真实全局热键**。

    asr_loop 会按配置调 main.apply_hotkeys()——真实实现会 import keyboard 并往
    操作系统装低层键盘钩子。跑单测不该改变开发机的键盘行为（钩子还留在 pytest
    进程里，回调指向已被回收的 status/闭包）。故全局打桩成记账桩：
    - 调用参数记进 yields 的列表，需要断言的用例直接读；
    - status['hotkeys'] 照真实形状写，状态类断言仍有意义。

    真实注册路径由 test_hotkeys.py 用**假 keyboard 模块**单独覆盖——那才是
    「实现对不对」的验证，且同样不碰 OS。"""
    import main
    calls: list = []

    def _stub(cfg=None):
        calls.append(cfg)
        st = {"enabled": True, "active": False, "reason": "starting",
              "bindings": dict(main.hotkeys_mod.DEFAULT_BINDINGS),
              "labels": dict(main.hotkeys_mod.BINDING_LABELS),
              # Mac 适配：state 形状多了「平台授权说明」（非 darwin 恒空串）。桩必须与
              # hotkeys.state() 同形状——test_hotkeys.test_status_shape_matches_manager_state
              # 钉的就是这件事。
              "hint": main.hotkeys_mod.accessibility_hint()}
        main.status["hotkeys"] = st
        return st

    monkeypatch.setattr(main, "apply_hotkeys", _stub)
    monkeypatch.setitem(main.runtime, "hotkeys", None)  # 每个测试从干净运行时开始
    yield calls


@pytest.fixture(scope="session")
def autostart_registry_guard():
    """p3-3：会话级快照真实 HKCU\\...\\Run 里的 LectureLens 值，结束时断言未被改动。

    开机自启是本仓库唯一会改**本机操作系统状态**的功能（写的是开机执行入口），
    跑单测不该动它——test_autostart.py 全部走内存假 winreg，本守卫兜住「以后有人
    写了不隔离的用例」这类回归。读不到（非 Windows）就不判定。"""
    import autostart

    before, err_before = autostart.read_value()

    def _assert_untouched() -> None:
        if err_before is not None:
            return
        now, err_now = autostart.read_value()
        assert err_now is None, f"会话结束后连注册表都读不了了: {err_now}"
        assert now == before, \
            f"测试改动了真实开机自启项: {before!r} → {now!r}（用例必须打桩 _winreg）"

    yield _assert_untouched
    _assert_untouched()


@pytest.fixture(scope="session")
def repo_config_guard():
    """会话开始快照仓库 config.json；可手动调用断言，会话结束再终检一次。

    判据 = 内容字节 + st_mtime_ns 双比对：只比内容会漏掉「写回相同内容」的
    意外落盘，mtime_ns 保证任何写动作都现形。"""
    p = ROOT / "config.json"
    snap = (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None

    def _assert_untouched() -> None:
        if snap is None:
            assert not p.is_file(), "仓库 config.json 原本不存在，测试期间不应新建"
            return
        assert p.is_file(), "仓库 config.json 被测试删除"
        now = (p.read_bytes(), p.stat().st_mtime_ns)
        assert now == snap, "仓库 config.json 被测试改写（内容或 mtime 变化）"

    yield _assert_untouched
    _assert_untouched()


@pytest.fixture(scope="session")
def repo_courses_guard():
    """p2-1：会话级快照仓库 courses.json，口径同 repo_config_guard。

    与 config.json 分开守卫：课程表是 p2-1 新引入的落盘文件，同样绝不能被测试
    绕过夹具写坏（test_courses_api 消费）。"""
    p = ROOT / "courses.json"
    snap = (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None

    def _assert_untouched() -> None:
        if snap is None:
            assert not p.is_file(), "仓库 courses.json 原本不存在，测试期间不应新建"
            return
        assert p.is_file(), "仓库 courses.json 被测试删除"
        now = (p.read_bytes(), p.stat().st_mtime_ns)
        assert now == snap, "仓库 courses.json 被测试改写（内容或 mtime 变化）"

    yield _assert_untouched
    _assert_untouched()
