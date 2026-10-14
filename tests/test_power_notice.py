# -*- coding: utf-8 -*-
"""tests/test_power_notice.py — B8 插电小提醒（离线秒级）。

交付面（docs/notes-batch.md B8 行「插电小提醒：电池供电 + GPU 模型时，开课后一句
toast『检测到电池供电，建议插电获得满速』」+ 本条的 ask）：
- `system_info.on_battery()`：ctypes 读 Windows `GetSystemPowerStatus`（0=电池 / 1=插电 /
  255=未知），**非 Windows 一律 None**，任何失败都回 None、绝不抛（照 tiers._ram_gb 模板）；
- `/api/status` 现算字段 `on_battery`（glossary_rev 同款：不进 config、不挂钩子）；
- 前端 pollStatus：首次见到「确实在用电池」且 device==='cuda' → 一句 toast，每会话一次。

⚠️ 与本条 ask 的一处**口径冲突**（如实记录，勿按 ask 字面改回去）：ask ③ 写的是
「首次见到 `on_battery===false` … showToast『检测到电池供电…』」，但字段语义（名字 +
ACLineStatus 0/1 映射）与文案指向相反——`false` 是**插电**，那时提醒"电池供电"会对着
台式机（永远 false）每次开课误报。故实现取 `on_battery === true`（电池）才提醒：文案
定义了意图，条件服从文案。这条断言把该口径钉住。

三层：① 纯函数（非 Windows 分支 / 打桩 ctypes 成功与失败）；② /api/status 透传；
③ index.html 静态断言（本仓无 JS 测试运行器，前端一律静态断言）。
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import main
import system_info as system_info_mod

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------- 1) 纯函数：on_battery ----------------

class _StubKernel32:
    """GetSystemPowerStatus 的最小替身：按预设回填 ACLineStatus，可模拟调用失败/抛异常。"""

    def __init__(self, ac, rc=1, boom=False):
        self.ac, self.rc, self.boom, self.calls = ac, rc, boom, 0

    def GetSystemPowerStatus(self, ref):
        self.calls += 1
        if self.boom:
            raise OSError("模拟 syscall 失败")
        if self.ac is not None:
            ref.ACLineStatus = self.ac
        return self.rc


class _StubCtypes:
    """替身 ctypes：只提供 on_battery 用到的那几样（Structure/c_ubyte/c_ulong/byref/windll）。

    生产代码在函数里现声明结构体类，故这里只要给一个 Structure 基类；
    `byref()` 直接返回对象本身，替身再往它身上写 ACLineStatus。"""

    class Structure:
        pass

    c_ubyte = c_ulong = int

    def __init__(self, kernel32):
        self.kernel32 = kernel32
        self.windll = SimpleNamespace(kernel32=kernel32)

    @staticmethod
    def byref(obj):
        return obj


def _win(monkeypatch, kernel32):
    """把"当前平台"打成 win32 + 装上 ctypes 替身。"""
    monkeypatch.setattr(system_info_mod, "sys",
                        SimpleNamespace(platform="win32"), raising=True)
    stub = _StubCtypes(kernel32)
    monkeypatch.setattr(system_info_mod, "ctypes", stub, raising=True)
    return stub


def test_non_windows_never_touches_the_api_and_returns_none(monkeypatch):
    """非 Windows（Mac/Linux）分支：直接 None，**一次 ctypes 都不碰**——本机是 Windows，
    这条正是"非侵入路径"的证明（把平台改成 darwin，windll 换成碰一下就炸的替身）。"""
    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("非 Windows 分支不该访问 ctypes.windll")

    monkeypatch.setattr(system_info_mod, "sys",
                        SimpleNamespace(platform="darwin"), raising=True)
    monkeypatch.setattr(system_info_mod, "ctypes",
                        SimpleNamespace(windll=_Boom()), raising=True)
    assert system_info_mod.on_battery() is None


@pytest.mark.parametrize("ac, want", [(0, True), (1, False), (255, None), (None, None)])
def test_ac_line_status_mapping(monkeypatch, ac, want):
    """0=电池 → True；1=插电 → False；255（未知）与"没回填"→ None（不猜）。"""
    _win(monkeypatch, _StubKernel32(ac))
    assert system_info_mod.on_battery() is want


def test_call_failure_and_exception_degrade_to_none(monkeypatch):
    """返回值 0（调用失败）与抛异常都必须降级成 None——绝不抛、绝不猜。"""
    _win(monkeypatch, _StubKernel32(0, rc=0))
    assert system_info_mod.on_battery() is None
    _win(monkeypatch, _StubKernel32(0, boom=True))
    assert system_info_mod.on_battery() is None


def test_real_api_on_this_machine_is_bool_or_none():
    """本机（Windows）真调一次：只能是 True/False/None 三者之一，且不抛。

    不断言具体值——插不插电是环境，不是代码契约。"""
    v = system_info_mod.on_battery()
    assert v is None or isinstance(v, bool)


# ---------------- 2) /api/status 现算透传 ----------------

async def test_status_exposes_on_battery_computed_per_request(monkeypatch):
    """照 glossary_rev 的现算模式：改掉底层事实，同一个端点下一次请求就跟上。"""
    monkeypatch.setattr(main.system_info_mod, "on_battery", lambda: True)
    async with asgi_client() as c:
        assert (await c.get("/api/status")).json()["on_battery"] is True
    monkeypatch.setattr(main.system_info_mod, "on_battery", lambda: False)
    async with asgi_client() as c:
        assert (await c.get("/api/status")).json()["on_battery"] is False
    monkeypatch.setattr(main.system_info_mod, "on_battery", lambda: None)
    async with asgi_client() as c:
        body = (await c.get("/api/status")).json()
    assert "on_battery" in body and body["on_battery"] is None


def test_import_is_the_single_source(monkeypatch):
    """main 只经 system_info_mod 读供电——不许在端点里另接一份 ctypes 实现。"""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "system_info_mod.on_battery()" in src
    assert "windll" not in src                        # 真实现只在 system_info.py 里一处
    assert "ctypes" not in src.replace(
        "（ctypes 需要进程内窗口句柄，只有桌面壳有）", "")   # 注释里的说明不算（见 main.py:1254）


# ---------------- 3) index.html：提醒的接线与文案（静态断言） ----------------

def _poll_status() -> str:
    return HTML.split("async function pollStatus() {", 1)[1].split("\n}", 1)[0]


def test_notice_fires_on_battery_with_cuda_model_once_per_session():
    body = _poll_status()
    assert "status_.on_battery === true" in body      # 只有"确实在用电池"才提醒
    assert "status_.device === 'cuda'" in body        # CPU 推理不提这句
    assert "!batteryWarned" in body                   # 会话内一次
    assert "batteryWarned = true;" in body
    assert "showToast('检测到电池供电，建议插电获得满速', 'warn');" in body


def test_notice_flag_is_session_only_and_declared_once():
    """flag 只活在页面里：声明一处，且不落盘（localStorage 里没有它）。"""
    assert HTML.count("let batteryWarned = false;") == 1
    assert "localStorage.getItem('batteryWarned')" not in HTML
    assert "localStorage.setItem('batteryWarned'" not in HTML


def test_unknown_power_state_is_silent():
    """null（非 Windows / 读不到）绝不能触发提醒：`=== true` 严格判定就是这条的护栏。"""
    body = _poll_status()
    assert "status_.on_battery == true" not in body   # 松等号会把 null/1 也算进来
    assert "const onBattery = status_.on_battery === true;" in body
