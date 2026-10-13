# -*- coding: utf-8 -*-
"""autostart.py — p3-3：开机自启（HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run）。

**事实源 = 注册表本身**（ask：「开关状态回读真实注册表而非本地标记」）。本模块
不维护任何本地标记、不写 config.json 键——`state()` 每次都去问 winreg，用户手工
删掉启动项、或系统策略清掉它，界面下次刷新就会如实变回「未启用」。

写什么：
- frozen（PyInstaller onedir）→ `"<exe>" --minimized`
- 开发态 → `"<venv>\\pythonw.exe" "<root>\\app\\desktop.py" --minimized`
  （**pythonw 而非 python**：python.exe 会在每次开机弹一个黑色控制台窗口。
  `start_translate.bat` 不能用作自启入口——它结尾有 `pause >nul`，开机时会把
  一个等待按键的 cmd 窗口永远留在桌面上。）
- `--minimized` = 启动即进托盘，不弹主窗（见 desktop.run_desktop）。

**安全边界（本模块最要紧的一条）**：写进 Run 键的是「开机自动执行的命令」，
是一个代码执行入口。故**命令一律由本模块按固定规则构造**，绝不接受来自
HTTP 请求的路径/命令片段——`/api/autostart` 只收一个布尔（开/关）。若哪天有人
把参数改成「按客户端给的字符串写」，那就是把开机执行权开放给了任何持 token 的
对端（含 DNS-rebinding 场景下的恶意页面）。

只动**自己的值名**（VALUE_NAME），不枚举、不清理、不覆盖别人的启动项。
"""
import logging
import sys
from pathlib import Path

log = logging.getLogger("autostart")

#: 每用户启动项（HKCU = 不需要管理员权限；HKLM 才要提权，且影响所有用户）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: 本应用在 Run 键里的值名（唯一标识；只增删这一个）
VALUE_NAME = "LectureLens"


def _default_root() -> Path:
    """开发态的项目根（app/ 的上一级）。与 main.ROOT 的开发分支同源推导；
    frozen 下不参与命令构造，故不必与 main.ROOT 的 exe 分支对齐。"""
    return Path(__file__).resolve().parent.parent


def quote_arg(part) -> str:
    """单个 argv 片段加引号（路径含空格时必须）。已带成对引号则原样返回。"""
    p = str(part)
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        return p
    return f'"{p}"'


def command_parts(root: Path | None = None) -> list[str]:
    """应注册的启动命令（argv 列表）。纯函数（只读环境事实，无副作用）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    base = Path(root) if root is not None else _default_root()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.is_file() else Path(sys.executable)   # 找不到 pythonw 就退回有窗版
    return [str(exe), str(base / "app" / "desktop.py"), "--minimized"]


def command_line(root: Path | None = None) -> str:
    """注册表里应存的命令行字符串（各段加引号）。"""
    return " ".join(quote_arg(p) for p in command_parts(root))


def _winreg():
    """延迟导入 winreg：非 Windows 下 import 本模块不应炸（服务主体仍可跑）。"""
    import winreg  # noqa: PLC0415 — 平台相关，故意延迟
    return winreg


def read_value() -> tuple:
    """读注册表当前值 → (命令字符串 | None, 错误文案 | None)。

    值/键不存在 = **未启用**（正常状态，不是错误）；其余异常才是错误。"""
    try:
        wr = _winreg()
    except Exception as e:                                   # 非 Windows / 阉割环境
        return None, f"winreg 不可用（{type(e).__name__}: {e}）——仅 Windows 支持开机自启"
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_READ) as k:
            val, _kind = wr.QueryValueEx(k, VALUE_NAME)
        return (str(val), None)
    except FileNotFoundError:
        return None, None                                    # 键或值不存在 = 未启用
    except OSError as e:
        return None, f"读取注册表失败（{type(e).__name__}: {e}）"


def state(root: Path | None = None) -> dict:
    """完整状态（**每次现读注册表**，不缓存）。

    enabled = 我们的值名存在；matches = 存的命令与「现在应该写的」逐字一致。
    两者分开表达，是为了让「开着但指向旧路径」（换过项目位置/重建过 venv 的典型
    残留）能在界面上被说清楚，而不是显示成一个骗人的「已启用」。"""
    want = command_line(root)
    stored, err = read_value()
    out = {"ok": err is None, "error": err,
           "enabled": stored is not None, "matches": stored == want,
           "command": stored, "expected": want,
           "value_name": VALUE_NAME, "key_path": "HKCU\\" + RUN_KEY,
           "platform_ok": sys.platform == "win32"}
    if err is None and stored is not None and stored != want:
        out["warning"] = ("启动项指向的命令与当前不一致——"
                          "项目位置或 venv 变动过，保存一次即可改写")
    return out


def enable(root: Path | None = None) -> dict:
    """写入/覆盖启动项，**写完回读校验**。返回 state()；失败带 ok=False+error。"""
    cmd = command_line(root)
    try:
        wr = _winreg()
        # CreateKeyEx：Run 键正常都存在，缺了（极瘦系统/被清理）也能自愈创建
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as k:
            wr.SetValueEx(k, VALUE_NAME, 0, wr.REG_SZ, cmd)
    except Exception as e:
        msg = f"写入启动项失败（{type(e).__name__}: {e}）"
        log.warning(f"[autostart] ⚠ {msg}")
        st = state(root)
        st["ok"], st["error"] = False, msg
        return st
    log.info(f"[autostart] 已写入开机自启: {cmd}")
    st = state(root)
    # 回读校验：写完不认账（策略拦截/权限）必须如实报错，不能凭 SetValueEx 不抛
    # 就当成功——「开关自己弹回去了」是这类功能最典型的假成功。
    if st.get("ok") and not (st.get("enabled") and st.get("matches")):
        st["ok"] = False
        st["error"] = ("写入后回读校验失败：启动项未生效或内容不符"
                       f"（当前值 {st.get('command')!r}）")
        log.warning(f"[autostart] ⚠ {st['error']}")
    return st


def disable(root: Path | None = None) -> dict:
    """删除启动项（幂等）。返回 state()；失败带 ok=False+error。"""
    try:
        wr = _winreg()
        try:
            with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0,
                            wr.KEY_SET_VALUE) as k:
                wr.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            pass                          # 本来就没有 = 目标状态已达成（幂等）
    except Exception as e:
        msg = f"删除启动项失败（{type(e).__name__}: {e}）"
        log.warning(f"[autostart] ⚠ {msg}")
        st = state(root)
        st["ok"], st["error"] = False, msg
        return st
    log.info("[autostart] 开机自启已关闭（启动项已删除）")
    st = state(root)
    if st.get("ok") and st.get("enabled"):
        st["ok"] = False
        st["error"] = f"删除后回读校验失败：启动项仍然存在（{st.get('command')!r}）"
        log.warning(f"[autostart] ⚠ {st['error']}")
    return st
