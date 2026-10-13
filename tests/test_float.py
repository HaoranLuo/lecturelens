# -*- coding: utf-8 -*-
"""p1-7 测试：B1 悬浮字幕条——/float 路由、点击穿透端点（含降级）、
set_click_through 的失败降级路径（假窗口对象，无真 GUI）。"""
from contextlib import asynccontextmanager

import httpx
import pytest

import main


@pytest.fixture
def float_env(monkeypatch):
    """复位 float_control 注册表 + runtime 配置（防跨测试污染）。"""
    saved = dict(main.runtime)
    saved_ctrl = dict(main.float_control)
    main.runtime["config"].clear()
    main.runtime["config"].update(main.DEFAULT_CONFIG)
    main.float_control.clear()
    yield
    main.runtime.clear()
    main.runtime.update(saved)
    main.float_control.clear()
    main.float_control.update(saved_ctrl)


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_float_page_served(float_env):
    """/float 返回页面且含关键元素（无边框小窗内容层：最新一句 + 穿透/关闭按钮）。"""
    async with asgi_client() as c:
        r = await c.get("/float")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for anchor in ("btnThru", "btnHide", "LectureLens 字幕条"):
        assert anchor in r.text, f"/float 缺少 {anchor}"


async def test_click_through_without_desktop_503(float_env):
    """纯服务模式（未注册回调）→ 503 + 明确 error（页面据此提示仅桌面版可用）。"""
    async with asgi_client() as c:
        r = await c.post("/api/float/click-through", json={"on": True})
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert "桌面" in r.json()["error"]


async def test_click_through_delegates_to_registered_fn(float_env):
    """注册回调（模拟桌面壳）→ 请求转给它执行并回传结果。"""
    calls = []
    main.float_control["set_click_through"] = lambda on: calls.append(on) or True
    async with asgi_client() as c:
        r = await c.post("/api/float/click-through", json={"on": True})
        r2 = await c.post("/api/float/click-through", json={"on": False})
    assert r.status_code == 200 and r.json() == {"ok": True, "on": True}
    assert r2.status_code == 200 and r2.json() == {"ok": True, "on": False}
    assert calls == [True, False]


async def test_click_through_fn_false_means_degraded_501(float_env):
    """回调返回 False（ctypes 失败/句柄无效）→ 501 降级信号，页面提示可关闭窗口。"""
    main.float_control["set_click_through"] = lambda on: False
    async with asgi_client() as c:
        r = await c.post("/api/float/click-through", json={"on": True})
    assert r.status_code == 501
    assert "降级" in r.json()["error"]


async def test_click_through_fn_exception_becomes_501(float_env):
    """回调抛异常（不预期但可能）→ 501 而非 500：属"尽力而为"语义的一部分。"""
    def boom(on):
        raise OSError("no hwnd")
    main.float_control["set_click_through"] = boom
    async with asgi_client() as c:
        r = await c.post("/api/float/click-through", json={"on": True})
    assert r.status_code == 501


async def test_click_through_rejects_non_bool(float_env):
    main.float_control["set_click_through"] = lambda on: True
    async with asgi_client() as c:
        r = await c.post("/api/float/click-through", json={"on": "yes"})
        r2 = await c.post("/api/float/click-through", json=["x"])
    assert r.status_code == 400 and r2.status_code == 400


# ---------------- set_click_through 降级路径（假窗口，不碰真 GUI/ctypes） ----------------

def test_set_click_through_degrades_without_native():
    """窗口 native 未就绪/无句柄 → 返回 False 不抛（调用方据此降级）。"""
    import desktop

    class _NoNative:
        pass

    class _BadHandle:
        def ToInt32(self):
            raise AttributeError("窗口尚未创建")

    class _BadNative:
        Handle = _BadHandle()

    class _WinNoNative:
        pass

    class _WinBadHandle:
        native = _BadNative()

    assert desktop.set_click_through(_NoNative(), True) is False
    assert desktop.set_click_through(_WinNoNative(), True) is False
    assert desktop.set_click_through(_WinBadHandle(), True) is False


# ---------------- p1-review 修复项的测试 ----------------

def test_float_alive_false_without_handle():
    """修复⑨判据：native 非 None 但句柄无效/缺失 → False（窗口销毁后 native
    不清，旧判据会永远为真）。"""
    import desktop

    class _NoNative:
        pass

    class _BadHandle:
        def ToInt32(self):
            raise AttributeError("no hwnd")

    class _WinBad:
        native = _BadHandle()

    assert desktop._float_alive(_NoNative()) is False
    assert desktop._float_alive(_WinBad()) is False


def test_extract_glossary_forms_and_bad_flags():
    """修复③：--glossary 两种写法都透传；未识别旗标进 bad（提示 --server-only）。
    p3-3：函数扩为 _extract_desktop_flags，多返回一个 minimized（桌面壳专属旗标）。"""
    import desktop

    g, mini, bad = desktop._extract_desktop_flags(
        ["--glossary", "glossary/CS.md", "--glossary=glossary/EE.md", "--gpu"])
    assert g == ["glossary/CS.md", "glossary/EE.md"]
    assert mini is False
    assert bad == ["--gpu"]
    g2, mini2, bad2 = desktop._extract_desktop_flags(["--glossary"])  # 缺值：按坏旗标处理
    assert g2 == [] and mini2 is False and bad2 == ["--glossary"]
    g3, mini3, bad3 = desktop._extract_desktop_flags(["--minimized"])   # p3-3
    assert g3 == [] and mini3 is True and bad3 == []


def test_instance_lock_overwrites_corrupted_lock(tmp_path, monkeypatch):
    """修复⑩：损坏/半截锁内容（崩溃残留）→ 重试后判陈旧并原子覆盖成功。"""
    import json as _json
    import os

    import desktop

    lock = tmp_path / "desktop.lock"
    lock.write_text('{"pid": 123', encoding="ascii")  # 半截 JSON
    monkeypatch.setattr(desktop, "LOCK_PATH", lock)
    desktop._OWNED_LOCK.clear()
    assert desktop._acquire_instance_lock(59999) is True
    assert _json.loads(lock.read_text(encoding="ascii"))["pid"] == os.getpid()
    desktop._release_instance_lock()
    assert not lock.exists()


def test_instance_lock_live_process_conflicts(tmp_path, monkeypatch):
    """修复⑩：活着的 pid + 可连通端口 → 判冲突（不再覆盖）。"""
    import json as _json
    import os
    import socket

    import desktop

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    port = srv.getsockname()[1]
    lock = tmp_path / "desktop.lock"
    lock.write_text(_json.dumps({"pid": os.getpid(), "port": port}), encoding="ascii")
    monkeypatch.setattr(desktop, "LOCK_PATH", lock)
    desktop._OWNED_LOCK.clear()
    try:
        assert desktop._acquire_instance_lock(60001) is False  # 本进程 pid 活着 + 端口在听
    finally:
        srv.close()
