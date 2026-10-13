# -*- coding: utf-8 -*-
"""tests/test_autostart.py — p3-3：开机自启（HKCU Run 项）读写 / 回读校验 / API。

**本文件绝不碰真实注册表**：所有用例把 `autostart._winreg` 换成一个内存假实现
（FakeWinreg），写入只落在字典里。真实注册表另有两道保护：
- conftest 的 autostart_registry_guard（会话级快照，结束断言未变）；
- 模块本身只增删**自己的值名**（VALUE_NAME），不枚举不动别人的启动项。

锁四件事：
① 命令构造：指向 desktop 入口 + `--minimized`（开发态用 pythonw，避免开机弹控制台）；
② 状态来自**真实注册表回读**（不设本地标记）——值不在=未启用、值不符=「开着但指向
   旧命令」这一独立状态，而不是骗人的「已启用」；
③ 失败可见：读/写/删的异常与「写完回读不认账」都必须 ok=false + error；
④ /api/autostart 只收布尔（写入 Run 键 = 开机执行入口，绝不接受客户端给的命令）。
"""
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import autostart as a
import main

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class FakeWinreg(types.ModuleType):
    """内存版 winreg：OpenKey 返回自身（自带上下文管理器协议）。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self, store=None, *, read_error=None, write_error=None,
                 key_missing=False, ignore_writes=False):
        super().__init__("winreg")
        self.store = dict(store or {})
        self.read_error = read_error
        self.write_error = write_error
        self.key_missing = key_missing
        self.ignore_writes = ignore_writes      # 模拟「写了但系统不认账」
        self.calls: list = []

    # —— 上下文协议（OpenKey/CreateKeyEx 的返回值当 key 用）——
    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def OpenKey(self, hive, path, reserved=0, access=0):
        self.calls.append(("OpenKey", path, access))
        if self.key_missing:
            raise FileNotFoundError(2, "系统找不到指定的文件。", path)
        if access == self.KEY_SET_VALUE and self.write_error:
            raise self.write_error
        if access == self.KEY_READ and self.read_error:
            raise self.read_error
        return self

    def CreateKeyEx(self, hive, path, reserved=0, access=0):
        self.calls.append(("CreateKeyEx", path, access))
        if self.write_error:
            raise self.write_error
        return self

    def QueryValueEx(self, key, name):
        if name not in self.store:
            raise FileNotFoundError(2, "系统找不到指定的文件。", name)
        return self.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.calls.append(("SetValueEx", name, value))
        if not self.ignore_writes:
            self.store[name] = value

    def DeleteValue(self, key, name):
        self.calls.append(("DeleteValue", name))
        if name not in self.store:
            raise FileNotFoundError(2, "系统找不到指定的文件。", name)
        del self.store[name]


@pytest.fixture
def fake_reg(monkeypatch):
    """默认：注册表里没有本应用的值。返回假模块，用例自行改 store。"""
    fake = FakeWinreg()
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    return fake


#: 自启命令的「应用入口」基线名：**两条分支必须一致**（评审员1 修复）。
#: 旧版 frozen 分支写 `"<exe>" --minimized`，而打包入口是 app/main.py，main.parse_args
#: 不认 --minimized（实跑 `unrecognized arguments`，exit 2）——开机即退，界面却显示
#: 「已启用」。dev 分支又要求指向 desktop.py，两条分支口径互相矛盾。
#: 现在两处都指向桌面壳；打包入口也已改成 desktop.py（scripts/build_exe.py）。
ENTRY = str(Path("app") / "desktop.py")


def test_dev_command_points_at_desktop_entry_with_minimized():
    parts = a.command_parts(Path("D:/proj"))
    assert parts[-1] == "--minimized", "自启必须带 --minimized（启动即进托盘）"
    assert parts[1].endswith(ENTRY), \
        "第二个参数必须指向 desktop 入口（不是 main.py：那没有窗口/托盘）"
    assert Path(parts[1]).is_absolute()
    # 开发态用 pythonw（无控制台窗口）——python.exe 会在每次开机弹一个黑框
    assert Path(parts[0]).name in ("pythonw.exe", "python.exe")
    if Path(sys.executable).with_name("pythonw.exe").is_file():
        assert Path(parts[0]).name == "pythonw.exe"


def test_command_line_quotes_every_part():
    line = a.command_line(Path("D:/my proj"))
    assert line.count('"') == 6, f"三段都该带引号（路径可能含空格）: {line}"
    assert line.endswith('"--minimized"')


def test_quote_arg_idempotent():
    assert a.quote_arg("x") == '"x"'
    assert a.quote_arg('"x"') == '"x"'      # 已带成对引号 → 原样


def test_frozen_uses_executable_as_entry(monkeypatch):
    """frozen 下命令 = exe 自身 + --minimized（exe 的入口就是桌面壳）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        parts = a.command_parts(Path("D:/proj"))
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
    assert parts == [sys.executable, "--minimized"]


def test_both_branches_name_the_same_entry_module(monkeypatch):
    """两条分支的应用入口必须**同源**——这是评审员1 指出的矛盾点：
    分开断言「带 --minimized」和「指向 desktop」是不够的，得把两件事焊在一起。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen_entry = a.command_parts(Path("D:/proj"))[0]
    assert Path(frozen_entry).name == Path(sys.executable).name, \
        "frozen 分支的入口应是 exe 自身（其打包入口已改为桌面壳）"
    monkeypatch.delattr(sys, "frozen", raising=False)
    dev_entry = a.command_parts(Path("D:/proj"))[1]
    assert dev_entry.endswith(ENTRY), "dev 分支的入口应是 app/desktop.py"


def test_flag_is_parsed_by_the_entry_the_command_names():
    """命令带的旗标必须被该入口真正解析——旧版正是漏了这一条断言。
    desktop 的桌面模式解析器认识 --minimized（dev 分支）；打包入口同源。"""
    import desktop
    _glossary, minimized, bad = desktop._extract_desktop_flags(["--minimized"])
    assert minimized is True and bad == []


def test_packaging_entry_matches_autostart_entry():
    """源码级契约（评审员1 修复）：打包入口必须就是自启命令指向的桌面壳。

    旧版 build_exe.py 打的是 app/main.py —— 与自启命令写的入口不是同一个，
    且 main 不认 --minimized，于是「打包版自启」开机即退。这条断言把「打包入口」
    与「自启入口」钉成一处：谁改回 main.py，这里立刻红。"""
    src = (ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    assert '"app" / "desktop.py"' in src, \
        "build_exe.py 的 PyInstaller 入口应为 app/desktop.py（自启命令解析 --minimized 的入口）"
    assert '"app" / "main.py",\n    ]' not in src, \
        "build_exe.py 不应再把 app/main.py 作为打包入口"


# —— ② 状态来自真实回读 ——

def test_state_not_enabled_when_value_absent(fake_reg):
    st = a.state(Path("D:/proj"))
    assert st["ok"] is True and st["error"] is None
    assert st["enabled"] is False and st["command"] is None
    assert "warning" not in st                 # 没启用就谈不上「指向不一致」
    assert st["value_name"] == "LectureLens"
    assert st["key_path"].startswith("HKCU\\")


def test_state_enabled_and_matching(fake_reg):
    fake_reg.store[a.VALUE_NAME] = a.command_line(Path("D:/proj"))
    st = a.state(Path("D:/proj"))
    assert st["enabled"] is True and st["matches"] is True
    assert "warning" not in st


def test_state_enabled_but_stale_is_its_own_state(fake_reg):
    """开着但指向旧路径 → enabled=True / matches=False + warning。
    这条状态必须存在：显示成「已启用」是骗人的（开机跑的是别处的旧命令）。"""
    fake_reg.store[a.VALUE_NAME] = r'"C:\old\.venv\Scripts\pythonw.exe" "C:\old\app\desktop.py" --minimized'
    st = a.state(Path("D:/proj"))
    assert st["enabled"] is True and st["matches"] is False
    assert "warning" in st and "不一致" in st["warning"]


def test_state_key_missing_is_not_an_error(monkeypatch):
    """Run 键本身不存在（极瘦系统/被清理）= 未启用，不是错误。"""
    fake = FakeWinreg(key_missing=True)
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    st = a.state(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is False


def test_state_read_error_is_visible(monkeypatch):
    fake = FakeWinreg(read_error=PermissionError(5, "拒绝访问。"))
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    st = a.state(Path("D:/proj"))
    assert st["ok"] is False and "读取注册表失败" in st["error"]
    assert st["enabled"] is False              # 读不到就是读不到，不假装


def test_winreg_unavailable_is_visible(monkeypatch, no_real_registry):
    st = a.state(Path("D:/proj"))
    assert st["ok"] is False and "仅 Windows 支持" in st["error"]


@pytest.fixture
def no_real_registry(monkeypatch):
    def _boom():
        raise ImportError("no module named 'winreg'")
    monkeypatch.setattr(a, "_winreg", _boom)


# —— ③ 写 / 删 + 回读校验 ——

def test_enable_writes_and_reads_back(fake_reg):
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is True
    assert fake_reg.store[a.VALUE_NAME] == a.command_line(Path("D:/proj"))
    assert st["enabled"] is True and st["matches"] is True


def test_enable_overwrites_stale_value(fake_reg):
    fake_reg.store[a.VALUE_NAME] = "old junk"
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is True and fake_reg.store[a.VALUE_NAME] == a.command_line(Path("D:/proj"))


def test_enable_write_error_is_visible(monkeypatch):
    fake = FakeWinreg(write_error=PermissionError(5, "拒绝访问。"))
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is False and "写入启动项失败" in st["error"]
    assert not fake.store


def test_enable_readback_mismatch_is_failure(monkeypatch):
    """写了但系统不认账（策略静默拦截）→ 必须 ok=false。
    「开关自己弹回去了」是这类功能最典型的假成功。"""
    fake = FakeWinreg(ignore_writes=True)
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    st = a.enable(Path("D:/proj"))
    assert st["ok"] is False and "回读校验失败" in st["error"]


def test_disable_deletes_and_reads_back(fake_reg):
    fake_reg.store[a.VALUE_NAME] = a.command_line(Path("D:/proj"))
    fake_reg.store["OtherApp"] = "keep me"
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is False
    assert fake_reg.store == {"OtherApp": "keep me"}, "绝不动别人的启动项"


def test_disable_is_idempotent_when_absent(fake_reg):
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is True and st["enabled"] is False


def test_disable_error_is_visible(monkeypatch):
    fake = FakeWinreg(write_error=PermissionError(5, "拒绝访问。"))
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    st = a.disable(Path("D:/proj"))
    assert st["ok"] is False and "删除启动项失败" in st["error"]


# —— ④ API ——

async def test_api_get_returns_registry_state(fake_reg):
    async with asgi_client() as c:
        r = await c.get("/api/autostart")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["enabled"] is False
    assert d["expected"].endswith('"--minimized"')


async def test_api_post_enable_then_disable(fake_reg):
    async with asgi_client() as c:
        r = await c.post("/api/autostart", json={"enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
        assert fake_reg.store[a.VALUE_NAME] == a.command_line(main.ROOT)
        r2 = await c.post("/api/autostart", json={"enabled": False})
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False and not fake_reg.store


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {"enabled": 1}, {}, {"enabled": None},
                                  {"enabled": True, "command": "calc.exe"}])
async def test_api_post_rejects_non_bool(fake_reg, body):
    """只收布尔。**特别锁住带 command 的请求体**：写入 Run 键 = 开机执行入口，
    接受客户端给的命令等于把开机执行权开放给任何持 token 的对端。"""
    async with asgi_client() as c:
        r = await c.post("/api/autostart", json=body)
    assert r.status_code in (400,), f"{body!r} 应被拒: {r.status_code}"
    assert not fake_reg.store, "被拒的请求不得写注册表"


async def test_api_post_rejects_extra_command_key_even_when_enabled_true(fake_reg):
    """enabled 合法但夹带 command：400 且**不写入**（不得「睁一只眼」只取 enabled）。"""
    async with asgi_client() as c:
        r = await c.post("/api/autostart",
                         json={"enabled": True, "command": "C:/evil.exe"})
    assert r.status_code == 400
    assert not fake_reg.store


async def test_api_post_failure_returns_500_with_error(monkeypatch):
    fake = FakeWinreg(write_error=PermissionError(5, "拒绝访问。"))
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    async with asgi_client() as c:
        r = await c.post("/api/autostart", json={"enabled": True})
    assert r.status_code == 500
    d = r.json()
    assert d["ok"] is False and "写入启动项失败" in d["error"]
    assert d["enabled"] is False


async def test_api_get_read_error_returns_200_with_ok_false(monkeypatch):
    """读失败也回 200：让设置页能把原因显示出来（而不是用 5xx 让整个设置页炸掉）。"""
    fake = FakeWinreg(read_error=OSError(5, "拒绝访问。"))
    monkeypatch.setattr(a, "_winreg", lambda: fake)
    async with asgi_client() as c:
        r = await c.get("/api/autostart")
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["error"]
