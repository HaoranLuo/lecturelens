# -*- coding: utf-8 -*-
"""autostart.py — p3-3：开机自启（Windows 注册表 / macOS LaunchAgents）。

**Windows**：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
**macOS**：~/Library/LaunchAgents/com.lecturelens.plist（launchd 每用户 agent，
          RunAtLoad=true + KeepAlive=false；「开机」在 macOS 上的等价物是
          「登录到本用户会话即拉起」，见 plist_xml 的注释）
**其它平台**：明确不支持（state() 里 platform_ok=false + error 说明）

**事实源 = 系统本身**（ask：「开关状态回读真实注册表/启动项而非本地标记」）。本模块
不维护任何本地标记、不写 config.json 键——`state()` 每次都去问 winreg / 读 plist，
用户手工删掉启动项、或系统策略清掉它，界面下次刷新就会如实变回「未启用」。

写什么（两个平台共用 command_parts，单点构造）：
- frozen（PyInstaller onedir）→ `"<exe>" --minimized`
- 开发态 → `"<venv>\\pythonw.exe" "<root>\\app\\desktop.py" --minimized`
  （**pythonw 而非 python**：Windows 上 python.exe 会在每次开机弹一个黑色控制台窗口。
  macOS 没有 pythonw.exe 这个文件名，command_parts 自动回落到 sys.executable——
  经 launchd 拉起时本就没有终端窗口。
  `start_translate.bat` 不能用作自启入口——它结尾有 `pause >nul`，开机时会把
  一个等待按键的 cmd 窗口永远留在桌面上。）
- `--minimized` = 启动即进托盘，不弹主窗（见 desktop.run_desktop）。

**安全边界（本模块最要紧的一条，两个平台都适用）**：写进启动项的是「开机/登录自动
执行的命令」，是一个代码执行入口。故**命令一律由本模块按固定规则构造**，绝不接受
来自 HTTP 请求的路径/命令片段——`/api/autostart` 只收一个布尔（开/关）。若哪天有人
把参数改成「按客户端给的字符串写」，那就是把开机执行权开放给了任何持 token 的
对端（含 DNS-rebinding 场景下的恶意页面）。macOS 侧的 launchctl 调用同样只吃本模块
构造的固定 argv（不经 shell）。

只动**自己的**启动项（Windows 的值名 VALUE_NAME / macOS 的 plist 文件名 PLIST_NAME），
不枚举、不清理、不覆盖别人的启动项。
"""
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("autostart")

#: 每用户启动项（HKCU = 不需要管理员权限；HKLM 才要提权，且影响所有用户）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: 本应用在 Run 键里的值名（唯一标识；只增删这一个）
VALUE_NAME = "LectureLens"

# —— macOS（launchd）——
#: plist 文件名 = launchd 的 Label（唯一标识；只增删这一个文件）
LAUNCH_AGENT_LABEL = "com.lecturelens"
PLIST_NAME = LAUNCH_AGENT_LABEL + ".plist"
#: launchctl 的绝对路径：从 Finder/pywebview 启动的进程 PATH 极简，不能靠 PATH 找
LAUNCHCTL = "/bin/launchctl"
#: launchctl 上限（本地 IPC，正常毫秒级）；超时只降级、绝不阻塞界面
LAUNCHCTL_TIMEOUT = 15


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
    """应注册的启动命令（argv 列表）。纯函数（只读环境事实，无副作用）。

    两个平台共用：Windows 走注册表字符串、macOS 走 plist 的 ProgramArguments
    （逐段一个 <string>，不经 shell）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    base = Path(root) if root is not None else _default_root()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.is_file() else Path(sys.executable)   # 找不到 pythonw 就退回有窗版
    return [str(exe), str(base / "app" / "desktop.py"), "--minimized"]


def command_line(root: Path | None = None) -> str:
    """注册表里应存的命令行字符串（各段加引号）。macOS 侧只用于展示/比对。"""
    return " ".join(quote_arg(p) for p in command_parts(root))


# —— macOS：LaunchAgents plist ——

def launch_agents_dir() -> Path:
    """~/Library/LaunchAgents（每用户 agent 目录；enable 时按需创建）。"""
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    """本应用的 LaunchAgent plist 绝对路径（唯一标识 = PLIST_NAME）。"""
    return launch_agents_dir() / PLIST_NAME


def _xml_escape(s: str) -> str:
    """XML 文本节点转义（本地实现，避免为 3 个字符引入新 import）。
    路径里的 & < > 会让 plist 解析失败——那会变成「开关看着开着、实际不生效」。"""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plist_xml(root: Path | None = None) -> str:
    """要写入的 plist 内容（纯函数，便于单测逐字断言）。

    - RunAtLoad=true：**登录到本用户会话即启动**。macOS 的 LaunchAgent 没有「开机」
      概念（用户 agent 的时机是登录），这是「开机自启」在本平台的等价物；
    - KeepAlive=false：**不**在退出后拉起——托盘「退出」是用户的真意图，被 launchd
      拉回来就成了「退不掉」；
    - ProgramArguments = command_parts() 逐段一个 <string>（不经 shell，无注入面）。"""
    args = command_parts(root)
    body = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>Label</key>\n'
        f'    <string>{LAUNCH_AGENT_LABEL}</string>\n'
        '    <key>ProgramArguments</key>\n'
        f'    <array>\n{body}\n    </array>\n'
        '    <key>RunAtLoad</key>\n'
        '    <true/>\n'
        '    <key>KeepAlive</key>\n'
        '    <false/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def _launchd_domain() -> str:
    """launchd 的每用户 GUI 域 = gui/<uid>（bootstrap 的目标）。"""
    uid = os.getuid() if hasattr(os, "getuid") else -1
    return f"gui/{uid}"


def _launchd_service() -> str:
    """已加载 job 的服务目标（bootout 用）= gui/<uid>/<label>。"""
    return f"{_launchd_domain()}/{LAUNCH_AGENT_LABEL}"


def _launchctl(*args: str) -> tuple[int, str]:
    """跑一次 launchctl → (返回码, 输出)。**永不抛**：找不到/超时/异常一律折算成
    非 0 返回码 + 文案，由调用方如实上报（自启是锦上添花，不能拦住任何界面动作）。

    固定 argv、不经 shell（无注入面）；绝对路径 LAUNCHCTL（见其注释）。"""
    try:
        p = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True,
                           timeout=LAUNCHCTL_TIMEOUT, check=False)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 127, f"{type(e).__name__}: {e}"


def _launchd_read() -> tuple:
    """读 macOS 启动项 → (命令字符串 | None, 错误文案 | None)。

    事实源 = plist 文件**本身**（与 Windows 侧「事实源=注册表」同一纪律）：文件不在
    = 未启用（正常状态，不是错误）；解析不了/没有 ProgramArguments → 明确报错，
    绝不假装「未启用」（那会让用户以为一切正常）。"""
    p = plist_path()
    try:
        if not p.is_file():
            return None, None
        with p.open("rb") as f:
            data = plistlib.load(f)
    except FileNotFoundError:
        return None, None                       # 竞态：读取瞬间被删 = 未启用
    except Exception as e:
        return None, (f"解析 LaunchAgents plist 失败（{type(e).__name__}: {e}）"
                      f"——{p}")
    args = data.get("ProgramArguments") if isinstance(data, dict) else None
    if not isinstance(args, list) or not args:
        return None, f"plist 里没有 ProgramArguments（不是本应用写的？）——{p}"
    return " ".join(quote_arg(str(a)) for a in args), None


def _winreg():
    """延迟导入 winreg：非 Windows 下 import 本模块不应炸（服务主体仍可跑）。"""
    import winreg  # noqa: PLC0415 — 平台相关，故意延迟
    return winreg


def read_value() -> tuple:
    """读当前启动项 → (命令字符串 | None, 错误文案 | None)。

    平台分支：macOS 读 LaunchAgents plist；Windows（及其它）读 HKCU Run 值。
    不存在 = **未启用**（正常状态，不是错误）；其余异常才是错误。"""
    if sys.platform == "darwin":
        return _launchd_read()
    try:
        wr = _winreg()
    except Exception as e:                                   # 非 Windows / 阉割环境
        return None, f"winreg 不可用（{type(e).__name__}: {e}）——仅 Windows / macOS 支持开机自启"
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_READ) as k:
            val, _kind = wr.QueryValueEx(k, VALUE_NAME)
        return (str(val), None)
    except FileNotFoundError:
        return None, None                                    # 键或值不存在 = 未启用
    except OSError as e:
        return None, f"读取注册表失败（{type(e).__name__}: {e}）"


def state(root: Path | None = None) -> dict:
    """完整状态（**每次现读系统启动项**，不缓存）。

    enabled = 我们的启动项存在；matches = 存的命令与「现在应该写的」逐字一致。
    两者分开表达，是为了让「开着但指向旧路径」（换过项目位置/重建过 venv 的典型
    残留）能在界面上被说清楚，而不是显示成一个骗人的「已启用」。

    key_path / value_name 按平台给：macOS = plist 路径 + launchd Label，
    Windows = HKCU 键路径 + Run 值名（前端把这串直接显示给用户，位置要说真的）。"""
    want = command_line(root)
    stored, err = read_value()
    darwin = sys.platform == "darwin"
    out = {"ok": err is None, "error": err,
           "enabled": stored is not None, "matches": stored == want,
           "command": stored, "expected": want,
           "value_name": LAUNCH_AGENT_LABEL if darwin else VALUE_NAME,
           "key_path": ("~/Library/LaunchAgents/" + PLIST_NAME) if darwin
                       else ("HKCU\\" + RUN_KEY),
           "platform_ok": sys.platform in ("win32", "darwin")}
    if err is None and stored is not None and stored != want:
        out["warning"] = ("启动项指向的命令与当前不一致——"
                          "项目位置或 venv 变动过，保存一次即可改写")
    return out


# —— Windows：HKCU Run 值 ——

def _winreg_enable(root: Path | None = None) -> dict:
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


def _winreg_disable(root: Path | None = None) -> dict:
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


# —— macOS：LaunchAgents plist ——

def _launchd_enable(root: Path | None = None) -> dict:
    """写 plist + `launchctl bootstrap`（写入即「下次登录生效」；bootstrap 只是
    「本次会话立即加载」，失败降级为 warning——「开机自启」的承诺不该搭在一次
    本地 IPC 上）。写完一律回读校验（同 Windows 侧纪律）。"""
    p = plist_path()
    xml = plist_xml(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(xml, encoding="utf-8")
    except OSError as e:
        msg = f"写入 LaunchAgent plist 失败（{type(e).__name__}: {e}）"
        log.warning(f"[autostart] ⚠ {msg}")
        st = state(root)
        st["ok"], st["error"] = False, msg
        return st
    log.info(f"[autostart] 已写入开机自启 plist: {p}")
    # 先 bootout 再 bootstrap：已经加载时直接 bootstrap 会报「已存在」——先卸下
    # 再装上才是幂等的（未加载时 bootout 报错属正常，忽略）。
    _launchctl("bootout", _launchd_service())
    rc, out = _launchctl("bootstrap", _launchd_domain(), str(p))
    st = state(root)
    if st.get("ok") and not (st.get("enabled") and st.get("matches")):
        st["ok"] = False
        st["error"] = ("写入后回读校验失败：plist 未生效或内容不符"
                       f"（当前值 {st.get('command')!r}）")
        log.warning(f"[autostart] ⚠ {st['error']}")
        return st
    if rc != 0:
        # 既不假成功也不误报失败：plist 已在位（下次登录会拉起），只是本次会话没加载
        st["warning"] = ("plist 已写入（下次登录生效）；launchctl bootstrap 未成功"
                         f"（本次会话未加载）: {out[:200]}")
        log.warning(f"[autostart] ⚠ {st['warning']}")
    return st


def _launchd_disable(root: Path | None = None) -> dict:
    """`launchctl bootout` + 删 plist（幂等）。返回 state()；失败带 ok=False+error。"""
    _launchctl("bootout", _launchd_service())   # 未加载时返回非 0 属正常，忽略
    p = plist_path()
    try:
        p.unlink(missing_ok=True)             # 本来就没有 = 目标状态已达成（幂等）
    except OSError as e:
        msg = f"删除 LaunchAgent plist 失败（{type(e).__name__}: {e}）"
        log.warning(f"[autostart] ⚠ {msg}")
        st = state(root)
        st["ok"], st["error"] = False, msg
        return st
    log.info("[autostart] 开机自启已关闭（LaunchAgent plist 已删除）")
    st = state(root)
    if st.get("ok") and st.get("enabled"):
        st["ok"] = False
        st["error"] = f"删除后回读校验失败：plist 仍然存在（{st.get('command')!r}）"
        log.warning(f"[autostart] ⚠ {st['error']}")
    return st


# —— 对外：按平台分发 ——

def enable(root: Path | None = None) -> dict:
    """写入/覆盖启动项（macOS → plist+bootstrap；Windows → HKCU Run 值）。"""
    if sys.platform == "darwin":
        return _launchd_enable(root)
    return _winreg_enable(root)


def disable(root: Path | None = None) -> dict:
    """删除启动项（幂等；macOS → bootout+删 plist；Windows → 删 Run 值）。"""
    if sys.platform == "darwin":
        return _launchd_disable(root)
    return _winreg_disable(root)
