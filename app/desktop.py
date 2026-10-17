# -*- coding: utf-8 -*-
"""desktop.py — LectureLens 桌面壳（P1「壳与颜值」，docs/design-borrowing.md §P1）。

用法:
  python app/desktop.py                                # 桌面模式: 原生窗口 + 系统托盘
  python app/desktop.py --glossary glossary/CS.md      # 桌面模式 + 课程术语表（可多次）
  python app/desktop.py --force-unlock                 # 清残留/误判的单实例锁后启动
  python app/desktop.py --minimized                    # p3-3：启动即进托盘（不弹主窗）
                                                       #   开机自启项用的就是这个（见 autostart.py）
  python app/desktop.py --server-only [main.py 旗标…]  # 等价 python app/main.py（旗标原样转发；
                                                       #  与桌面模式同一套绑定规则：默认只听
                                                       #  127.0.0.1，开了手机同屏才对外）
  python app/desktop.py --check                        # headless 自检（门禁）: 退出码 0/1

桌面模式结构:
  后台 daemon 线程跑 uvicorn（复用 main.py 的 app）：默认仅监听 127.0.0.1 的 **8787**
  （被占才回落空闲端口——B15 评审修复①，理由见 DESKTOP_PREFERRED_PORT；原先用内核随机
  端口，导致窗口 origin 每次都变、前端 localStorage 偏好每次失忆）；
  开了手机同屏（config `lan_enabled`，§7）则监听 0.0.0.0（同号 8787，被占换端口），
  手机连同一 WiFi 扫码即可看字幕——桌面窗口自己始终走 127.0.0.1:<port>，不受影响。
  主进程 webview.create_window + webview.start
  （Windows 默认 Edge WebView2 后端，Win11 自带）。
  - 关闭窗口(X) = 隐藏到托盘：pywebview 6 的 closing 事件 handler 返回 False 即取消
    关闭（webview/platforms/winforms.py:400-402 on_closing → args.Cancel=True，本仓库
    .venv 实源核实）；真退出只在托盘菜单「退出」。
  - 系统托盘（p1-2）：菜单 = 显示/隐藏主窗（动态文本，与窗口可见性互斥闭环，
    左键/双击切换）/ 悬浮字幕条（B1 钩子）/ 暂停·继续字幕（与字幕页同一
    status['paused'] 事实源）/ 打开字幕页 / 退出；图标随状态变色——tray_state
    纯函数折算 main.status（error 红 > paused 黄 > ok 绿，状态灯样式 =
    design-spec §六），1s 轮询线程刷新（变色 + tooltip + update_menu）。
  - 单实例：ROOT/desktop.lock（pid+port JSON；pid 存活且端口仍可连通才算冲突，
    端口连通性兜底 PID 复用误判）。
  - 日志/鉴权/启停与 main.py 同一套：setup_logging + provision_token +
    build_lifespan（main.py 提取的模块级函数）；TokenAuthMiddleware 随 app 附带，
    127.0.0.1 对端豁免使窗口与本机探活免 token。
  - --check 不带 lifespan（不加载模型不开麦，秒级），只验：桌面三依赖导入、托盘
    图标生成、服务线程起、HTTP 探活 /api/status 与 /。
"""
import argparse
import atexit
import contextlib
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import uvicorn  # noqa: E402

import branding  # noqa: E402  品牌图标/状态色/版本号单一事实源（p1-3）
import hotkeys  # noqa: E402  p3-5：托盘菜单里的键位提示取默认值兜底
import lan as lan_mod  # noqa: E402  §7：手机同屏的绑定决策（0.0.0.0 / 127.0.0.1 / 端口）
import main as service  # noqa: E402  app/main.py：app/lifespan/日志/token 工具全在这

log = logging.getLogger("desktop")

WINDOW_TITLE = "LectureLens"
WINDOW_SIZE = (1280, 800)      # ask：默认 1280x800 可调
WINDOW_MIN_SIZE = (960, 600)   # 字幕页 900px 主列 + 工具条的舒适下限
SERVER_READY_TIMEOUT = 60      # uvicorn startup 上限（含 lifespan 建任务，不等待模型）
STOP_JOIN_TIMEOUT = 15         # 优雅停机 join 上限

# —— 托盘状态（p1-2）：ask 口径「运行绿/暂停黄/错误红」= design-spec §六状态灯
# 「在线绿/暂停黄/错误红」同一语义；色值单一来源 = branding.STATE_COLORS（§2 令牌）——
_STATE_COLORS = branding.STATE_COLORS
_STATE_TITLES = {
    "ok": "LectureLens — 运行中",
    "paused": "LectureLens — 已暂停（托盘菜单可继续）",
    "error": "LectureLens — 异常（见字幕页状态或 logs/）",
}

# —— B1 悬浮字幕条显隐（p1-7 落地：真实窗口在 run_desktop 创建）——
# 事实源 = config.floating_bar（托盘勾选 / 设置页开关 / 浮窗 × 按钮三处入口都写
# 它）；本 Event 由托盘轮询线程同步为「浮窗当前应显示」，供菜单 checked 读取。
_floating_on: threading.Event = threading.Event()

# —— 单实例锁（仅桌面模式参与；--server-only/--check 与旧 main.py 等价，不加约束）——
# resolve() 规范化为绝对路径：锁文件只有常量来源（无用户输入），读写一律走 Path
# 方法（os.open/create/write_text），不经过内置 open()（Mimosa 拦截项的修复）。
LOCK_PATH = (service.ROOT / "desktop.lock").resolve()
_OWNED_LOCK: dict = {}  # 本进程持有的锁内容；释放前校验所有权，防误删新实例的锁
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# —— B15 评审修复①：桌面窗口的 origin 必须跨启动稳定，否则前端偏好「每次开都失忆」 ——
# 症状（评审实读 + 实跑）：用户选了浅色主题，下次开应用又回深色。两条独立原因：
#   ① 端口随机：start_server() 原先用 port=0 → 内核随机分配（lan.lan_bind(false,0) =
#      ('127.0.0.1', 0)、lan.pick_port(...,0) = 0，本机实跑），窗口按端口载入
#      http://127.0.0.1:<port>/，而 localStorage **按 origin（含端口）隔离** → 每次启动
#      都是新 origin，ll_theme / ll_font（字号）/ ll_settings_tab（设置分区）/ LL_TOKEN
#      （LAN 配对记忆）全部读不到。评审给的四次桌面运行日志端口各异（65241/58804/
#      51724/50304）即此事。
#   ② 存储不持久：webview.start() 默认 private_mode=True（site-packages/webview/
#      __init__.py:198「cookies and local storage are not preserved」；winforms 后端把
#      cache_dir 指到临时目录、关窗即 rmtree）→ 就算 origin 不变也留不下东西。
# 本常量 + WEBVIEW_PROFILE_DIR（见下）分别治这两条。
# 取 8787（与 --server-only / 手机同屏同一个号）：① 与用户手敲/文档里的地址一致；
# ② 开关「手机同屏」时不换 origin（lan_enabled 开关前后窗口都是 127.0.0.1:8787），
# 偏好不会因为换个监听地址而丢。被占则 lan.pick_port 回落空闲端口（窗口照常起得来，
# 只是那一次偏好会另存一份——「宁可降级也不让窗口起不来」同 §7 口径）。
DESKTOP_PREFERRED_PORT: int = lan_mod.LAN_PREFERRED_PORT

#: pywebview 的**持久化** profile 目录（cookies/localStorage/IndexedDB 等站点数据）。
#: 锚在 ROOT：与 config.json/transcripts/logs 同款「程序目录即用户数据目录」。
#: ⚠️ 它含 localStorage（用扫码配对过的机器会带着 LAN token），因此名字必须进
#: scripts/build_exe.py 的 FORBIDDEN_DIST_ENTRIES，绝不能进分发包（同 config.json 口径）。
#: 名字刻意**不叫** webview —— 那会和 PyInstaller 收进包的 pywebview 包目录同名，
#: 让 D3 的「用户数据/密钥不得进包」扫描把合法产物也报出来。
WEBVIEW_PROFILE_DIR = (service.ROOT / "webview_profile")


def persistent_storage_kwargs() -> dict:
    """webview.start 的持久化 profile 参数；不可用就回落默认（私有模式）。

    为什么必须显式关掉 private_mode：见 WEBVIEW_PROFILE_DIR 的注释（默认 True 会让
    localStorage 每次关窗即蒸发，B15 主题 + 既有字号/分区/配对记忆全跟着失效）。
    为什么先探测可写：pywebview 的 __set_storage_path 在目录建不出来/不可写时**直接抛
    WebViewException**（webview/__init__.py:487-498）——那会让整个窗口起不来。
    「界面偏好记不住」可以降级，「应用打不开」不行（同 §7「8787 被占也要能起窗口」）。"""
    try:
        WEBVIEW_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if not os.access(WEBVIEW_PROFILE_DIR, os.W_OK):
            raise OSError("目录不可写")
    except Exception as e:
        log.warning(f"[desktop] ⚠ 无法准备持久化 WebView2 profile（{e}）"
                    "→ 本次不记忆界面偏好（主题/字号/分区）")
        return {}
    return {"private_mode": False, "storage_path": str(WEBVIEW_PROFILE_DIR)}


def _pid_alive(pid: int) -> bool:
    """进程是否存活。Windows: OpenProcess(查询受限信息)，拿到句柄=活；非 Windows
    退回 os.kill(pid, 0)（本项目仅在 Windows 跑 GUI，分支只为可移植留门）。
    p1-review：ACCESS_DENIED(5) 说明进程存在但无权打开（如对方以管理员启动）——
    必须判活，否则第二实例放行造成双开。"""
    pid = int(pid)
    if pid <= 0:
        return False
    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        k32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_listening(port: int) -> bool:
    """127.0.0.1:port 是否有实例在听（TCP connect 探测，0.3s 上限）。"""
    if not (0 < int(port) < 65536):
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def _acquire_instance_lock(port: int) -> bool:
    """单实例锁（ask：锁文件方案）。锁文件 = LOCK_PATH（ROOT/desktop.lock，已
    resolve 规范化的常量路径，无用户输入成分）。内容 JSON {"pid", "port"}。
    判定：已有锁且 (pid 存活 且 (无端口记录 或 127.0.0.1:port 可连通)) → 已有
    实例，返回 False；否则陈旧锁（崩溃残留/同 PID 复用）→ 覆盖。端口连通性是
    PID 复用的兜底：复用者**还要**恰好监听那个端口才算冲突。
    ⚠️ B15 评审修复① 之后端口默认固定 8787，这条兜底比「随机端口」时弱一点
    （评审口径的诚实交代）：残留锁 + pid 被复用 + 有别的进程占着 8787（例如用户同时
    跑着 --server-only）三者同时成立时，会把「无实例」误判为「已运行」，补救手段是
    `--force-unlock`（README 已写明）。取舍理由：origin 稳定性是**每次启动**都在生效的
    真实收益，而这是三个独立小概率事件同时发生的误判，且有一键挽回。
    锁文件不可创建（只读目录等）→ fail-open：告警并放行，锁问题不拦用户启动。
    p1-review 加固（并发竞争收敛）：
    - 读侧短重试：O_EXCL 创建与写入非原子，竞争方可能读到空/半截内容——重试
      数次再判，避免把「对方正在写」误判为陈旧锁；
    - 覆盖走 temp + os.replace 原子替换（残留锁场景下两个进程各自覆盖也不会
      写出半截内容，最终一方胜出）；
    - 覆盖后回读校验所有权：锁里不是自己的 pid（被另一方抢先写入）→ 本实例
      主动退出（返回 False），把「双开」收敛为「恰一个实例」。
    极端场景仍有微秒窗口不可完全消除（无 OS 级文件锁的正确性用 msvcrt.locking
    需重构读写路径），回读校验把风险压到可忽略；另有 --force-unlock 作为人工
    挽回手段（误判冲突时不用重启）。
    必须在服务起来之后调用（port 才是真实端口）。"""
    payload = json.dumps({"pid": os.getpid(), "port": int(port)})
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    except OSError as e:
        log.warning(f"[desktop] ⚠ 单实例锁不可用({e})——跳过互斥继续启动")
        return True
    else:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(payload)
        _OWNED_LOCK.update(json.loads(payload))
        return True

    old = None
    for _ in range(4):  # 短重试：等可能的并发写者完成（4×50ms=200ms 封顶）
        try:
            old = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            break
        except Exception:
            time.sleep(0.05)
    pid = int((old or {}).get("pid") or 0)
    old_port = int((old or {}).get("port") or 0)
    if _pid_alive(pid) and (old_port == 0 or _port_listening(old_port)):
        return False
    try:
        tmp = LOCK_PATH.with_name(LOCK_PATH.name + ".tmp")
        tmp.write_text(payload, encoding="ascii")
        os.replace(tmp, LOCK_PATH)  # 原子替换：并发覆盖下内容总是完整
    except OSError as e:
        log.warning(f"[desktop] ⚠ 陈旧单实例锁覆盖失败({e})——跳过互斥继续启动")
        _OWNED_LOCK.update({"pid": os.getpid(), "port": int(port)})
        return True
    try:
        back = json.loads(LOCK_PATH.read_text(encoding="ascii"))
    except Exception:
        back = {}
    if int(back.get("pid") or 0) != os.getpid():
        log.warning("[desktop] ⚠ 单实例锁并发竞争：锁已被另一实例写入——本实例退出")
        return False
    _OWNED_LOCK.update({"pid": os.getpid(), "port": int(port)})
    return True


def _release_instance_lock() -> None:
    """释放本进程的锁：只在锁文件当前记录仍是本进程时删除（atexit 可能晚于
    新实例覆盖锁，此时绝不能删——否则双实例互斥失效）。"""
    if not _OWNED_LOCK:
        return
    try:
        cur = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    if cur.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            LOCK_PATH.unlink()
    _OWNED_LOCK.clear()


def _warn_already_running() -> None:
    """双开时给 GUI 用户一个看得见的提示；无桌面会话/调用失败则静默。

    非 Windows（macOS/Linux）：没有 user32.MessageBoxW，**降级为日志一行**（择简：
    不为一句提示引入新依赖；调用点已经 log.error 写明了锁文件位置。托盘通知
    （icon.notify）要等 pystray icon 建好，而本函数在它之前调用，取不到）。"""
    if os.name != "nt":
        log.warning("[desktop] 另有实例在运行（非 Windows 无 MessageBox，"
                    "见上一条 log.error 的锁文件位置）")
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            0, "LectureLens 已在运行（请查看系统托盘）。", WINDOW_TITLE, 0x40)
    except Exception:
        pass


# —— 服务线程（main.app + uvicorn；默认仅 127.0.0.1，§7 手机同屏开启时监听 0.0.0.0）——

def start_server(port: int = DESKTOP_PREFERRED_PORT):
    """后台 daemon 线程起 uvicorn，返回 (线程, server, 实际端口)。

    绑定地址跟随 config.lan_enabled（§7，只在启动时读一次）：
    - false（默认）→ host=127.0.0.1, port 参数（默认 DESKTOP_PREFERRED_PORT=8787）；
    - true（手机同屏）→ host=0.0.0.0，优先 8787（被占则换一个空闲端口：桌面上往往
      同时跑着别的实例/别的服务，**不能因为 8787 被占就让窗口起不来**）。
    桌面窗口自己始终走 http://127.0.0.1:<port>——0.0.0.0 同样接环回连接，窗口不受影响。
    **端口默认值不再是 0（随机）**：那会让窗口 origin 每次启动都变、前端的 localStorage
    偏好（B15 主题等）每次失忆——理由与「被占也要起得来」的取舍见 DESKTOP_PREFERRED_PORT
    的注释；传 0 仍表示「交给内核挑」。
    实际端口仍以 server.servers[0].sockets 为准（uvicorn/server.py 先填 servers
    :155/:164 再置 started :205，等 started 再读，无竞态），并记进
    service.set_bind——/api/lan-qr 的配对 URL 要用它（换成别的端口时不能说 8787）。
    proxy_headers=False 与 main.py 同款：无前置反代，禁 XFF 改写 scope['client']，
    防伪造环回豁免（条目19 安全姿势）。"""
    cfg = service.load_config()
    host, want = lan_mod.lan_bind(cfg, lan_mod.LAN_PREFERRED_PORT if
                                  cfg.get("lan_enabled") else int(port))
    want = lan_mod.pick_port(host, want)
    config = uvicorn.Config(service.app, host=host, port=int(want),
                            log_config=service.uvicorn_log_config(),
                            proxy_headers=False)
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, name="uvicorn-desktop", daemon=True)
    th.start()
    deadline = time.time() + SERVER_READY_TIMEOUT
    while not server.started:
        if not th.is_alive():
            raise RuntimeError("服务线程意外退出（详见日志）")
        if time.time() > deadline:
            raise RuntimeError(f"服务 {SERVER_READY_TIMEOUT}s 内未就绪")
        time.sleep(0.05)
    real_port = server.servers[0].sockets[0].getsockname()[1]
    service.set_bind(host, real_port)
    return th, server, real_port


def stop_server(server, th: threading.Thread) -> None:
    """优雅停机：should_exit 让 uvicorn 走 lifespan 收尾（停分句器/调度器/哨兵），
    join 上限 STOP_JOIN_TIMEOUT；线程是 daemon，超时也不悬挂进程退出。"""
    server.should_exit = True
    th.join(timeout=STOP_JOIN_TIMEOUT)


# —— B1 悬浮字幕条（p1-7）——
FLOAT_TITLE = "LectureLens 字幕条"
FLOAT_SIZE = (720, 150)        # 只容两行（EN+ZH）；无边框固定尺寸

# Win32：点击穿透（ask：WS_EX_TRANSPARENT via ctypes，尽力而为）
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED


def set_click_through(window, on: bool) -> bool:
    """尽力而为的点击穿透：给窗口加/去 WS_EX_TRANSPARENT（+LAYERED）。
    成功 True；任何失败（native 未就绪/句柄无效/API 调用失败）返回 False，由调用方
    降级（窗口保持可点击可关闭，设置页已注明降级路径）。纯函数化（传 window 对象）
    便于单测用假对象验证降级路径——不用真窗口。
    window.native 由 winforms 后端在窗口创建后赋值（BrowserForm，winforms.py:195）。

    **非 Windows（macOS/Linux）恒返回 False**：WS_EX_* 是 Win32 扩展窗口样式，
    macOS 没有对应物（要么改 NSWindow 层级、要么在事件层吞鼠标，都要另做一套）。
    走既有降级路径，前端文案已说明「仅 Windows 可用、其余平台保持可关闭窗口」。"""
    if os.name != "nt":
        log.info("[float] 点击穿透仅 Windows 可用——已降级（浮窗保持可点击可关闭）")
        return False
    try:
        hwnd = int(window.native.Handle.ToInt32())
        u32 = ctypes.windll.user32
        ex = u32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if bool(ex & _WS_EX_TRANSPARENT) == bool(on):
            return True
        new = (ex | _WS_EX_TRANSPARENT | _WS_EX_LAYERED) if on \
            else (ex & ~_WS_EX_TRANSPARENT)
        if not u32.SetWindowLongW(hwnd, _GWL_EXSTYLE, new):
            return False
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_FLAGS)
        return True
    except Exception as e:
        log.warning(f"[float] ⚠ 点击穿透设置失败（降级为可关闭窗口）: {e}")
        return False


def _float_position(cfg: dict | None = None) -> dict:
    """悬浮条位置：优用**记忆值**（p3-5），否则屏幕底部居中偏上（课堂第二块屏常见位）。
    记忆值同样要过可交叠校验——拔掉第二块屏后旧坐标可能落在屏外，那时窗口是「存在
    但看不见」，比回到默认位更糟。取屏幕尺寸失败就不传坐标（pywebview 默认位）。

    非 Windows（macOS/Linux）：不传坐标（返回 {}）——没有 GetSystemMetrics，而
    pywebview 的 screen API 要等 start() 之后才有值（本函数在窗口创建之前调用，
    时机上取不到）；**不猜坐标**，交给窗口管理器的默认摆放。记忆值仍优先（见上，
    非 Windows 下 _virtual_screen 为 None，交叠校验按「不设防」放行）。"""
    saved = _saved_point(cfg or {}, "float_x", "float_y")
    if saved:
        return {"x": saved[0], "y": saved[1]}
    if os.name != "nt":
        return {}
    try:
        u32 = ctypes.windll.user32
        sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        w, h = FLOAT_SIZE
        return {"x": max(0, (sw - w) // 2), "y": max(0, sh - h - 160)}
    except Exception:
        return {}


# —— p3-5（B12）：窗口版式记忆 ——
#: Win32 虚拟屏指标（多显示器合并包围盒；主屏左侧/上方的屏坐标为**负**）
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79
#: 记忆版式至少要留这么多像素落在当前桌面上，否则视为「屏幕换了、旧坐标已失效」
_MIN_VISIBLE = 120


def _virtual_screen() -> tuple | None:
    """(x, y, w, h) 虚拟屏包围盒；取不到（无桌面会话/API 失败）→ None。

    **仅 Windows 实现**（GetSystemMetrics 的 SM_*VIRTUALSCREEN）。非 Windows
    （macOS/Linux）**一律 None** = 走既有「不设防」路径：_geometry_visible /
    _point_on_screen 在 None 下一律判真，记忆版式与记忆点原样采信（越界交给窗口
    管理器自己收敛，宁可位置差一点也不能让窗口藏在屏外看不见）。不做伪包围盒：
    pywebview 的 screens 列表要等 start() 之后才有值，而本函数在窗口创建之前就被
    调用——时机上取不到，硬编一个只会制造假坐标。"""
    if os.name != "nt":
        return None
    try:
        u32 = ctypes.windll.user32
        return (u32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
                u32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
                u32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
                u32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    except Exception:
        return None


def _geometry_visible(x: int, y: int, w: int, h: int) -> bool:
    """**主窗**口径：给定矩形是否与当前桌面有足够交集（多显示器包围盒）。

    纯函数（只读屏指标），便于 headless 单测直接喂坐标验证降级路径。"""
    vs = _virtual_screen()
    if vs is None:
        return True          # 取不到屏信息就不否决，交给 create_window 的默认行为
    vx, vy, vw, vh = vs
    if vw <= 0 or vh <= 0:
        return True
    ix = max(0, min(x + w, vx + vw) - max(x, vx))
    iy = max(0, min(y + h, vy + vh) - max(y, vy))
    return ix >= _MIN_VISIBLE and iy >= _MIN_VISIBLE


def _point_on_screen(x: int, y: int) -> bool:
    """**记忆点**（悬浮条位置）口径：点是否还落在当前桌面上。

    与主窗用不同的判据是必须的：_MIN_VISIBLE 是「窗口要有一块能看见能点的面积」，
    而点没有面积——套用矩口径会让任何合法点都算「120px 交集不足」而被误杀
    （本项第一版就是这么写的，实测 (100,200) 被判屏外）。"""
    vs = _virtual_screen()
    if vs is None:
        return True
    vx, vy, vw, vh = vs
    if vw <= 0 or vh <= 0:
        return True
    return vx <= x < vx + vw and vy <= y < vy + vh


def _saved_point(cfg: dict, kx: str, ky: str) -> tuple | None:
    """从配置取一个记忆点 (x, y)；未记忆/非法/已跑到屏外 → None。"""
    x, y = cfg.get(kx), cfg.get(ky)
    if x is None or y is None:
        return None
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return None
    return (x, y) if _point_on_screen(x, y) else None


def _window_geometry() -> dict:
    """主窗 create_window 的版式参数：优先记忆值，不可用则回默认尺寸（居中）。

    **刻意不收入参**：版式必须从 service.load_config() 读（不是 runtime['config']）
    ——runtime 的磁盘加载发生在 asr_loop 任务里，而本函数在 lifespan 启动完成、
    任务尚未被调度时就跑（start_server 只等 server.started），此时
    runtime['config'] 还是 DEFAULT_CONFIG；若由调用方传 runtime 配置进来，「记忆
    版式」会永远读不到，表现为「每次都忘」。签名不给人传错的机会。"""
    try:
        cfg = service.load_config()
    except Exception as e:
        log.warning(f"[desktop] ⚠ 读取配置失败（用默认版式）: {e}")
        cfg = {}
    w = cfg.get("window_w")
    h = cfg.get("window_h")
    try:
        w = int(w) if w is not None else WINDOW_SIZE[0]
        h = int(h) if h is not None else WINDOW_SIZE[1]
    except (TypeError, ValueError):
        w, h = WINDOW_SIZE
    # 夹到最小尺寸以上（配置被手改成 10×10 时窗口会小到点不到按钮）
    w = max(WINDOW_MIN_SIZE[0], min(w, 20000))
    h = max(WINDOW_MIN_SIZE[1], min(h, 20000))
    kw: dict = {"width": w, "height": h}
    x, y = cfg.get("window_x"), cfg.get("window_y")
    if x is not None and y is not None:
        try:
            x, y = int(x), int(y)
            if _geometry_visible(x, y, w, h):
                kw.update(x=x, y=y)
            else:
                log.info("[desktop] 记忆版式已不在当前桌面上（换过显示器？）"
                         "——回落到默认居中位置")
        except (TypeError, ValueError):
            pass
    return kw


def _read_geometry(win) -> dict:
    """读一个 pywebview 窗口的当前几何（window.x/y/width/height，window.py:216-254）。
    窗口已销毁/未创建 → 空字典（绝不抛：退出路径上抛异常会吞掉真正的收尾）。"""
    out: dict = {}
    try:
        out = {"x": int(win.x), "y": int(win.y),
               "w": int(win.width), "h": int(win.height)}
    except Exception:
        return {}
    return out


def _clamp_geom(key: str, value: int) -> int | None:
    """把几何值夹到 main.CONFIG_NUM_RANGES 的合法区间内；夹不了（键没登记）→ None。

    **落盘前必须夹**：窗口管理器报回来的值理论上都在范围内，但这条路径写的是
    config.json——一旦写进一个 set_config 自己都会拒绝的值（比如 12.5 或 999999），
    用户下次在设置页/接口上动任何配置都会撞上「非法值」，而那个值是我们偷偷塞进去的
    （评审员1 指出的次要问题）。"""
    rng = service.CONFIG_NUM_RANGES.get(key)
    if not rng:
        return None
    lo, hi = rng
    try:
        return int(max(lo, min(hi, int(value))))
    except (TypeError, ValueError):
        return None


def _save_geometry(main_win, float_win) -> None:
    """退出时把实测几何写回 config 并落盘（p3-5：ask 的「退出时存 config」）。

    **以磁盘现值为基底，只覆盖 6 个几何键**（评审员1 修复）：旧实现直接拿
    `service.runtime["config"]` 整份落盘，而它初值是 dict(DEFAULT_CONFIG)、只有
    asr_loop 跑到配置载入那一步才被换成磁盘配置。若退出发生在那一刻之前（asr_loop
    异常/取消早退），整份落盘 = 把 config.json 静默重置成默认值——含清掉
    **auth_token**（LAN/手机端直接失去配对）与用户改过的 model/lang。后果是静默的、
    且用户无从察觉，所以这里改成：读盘 → 只改几何键 → 夹取 → 写回。
    磁盘优先还有第二个好处：与 /api/config 的并发写撞车时丢的是几何值，而不是
    别人的配置（旧实现会连 model/token 一起回滚）。"""
    g = _read_geometry(main_win) if main_win is not None else {}
    f = _read_geometry(float_win) if float_win is not None else {}
    if not (g or f):
        return                      # 什么都没读到（窗口已销毁）→ 不写，避免把 None 落盘
    try:
        cfg = service.load_config()          # 磁盘现值 = 权威基底
    except Exception as e:
        log.warning(f"[desktop] ⚠ 版式记忆跳过（读不到磁盘配置）: {e}")
        return
    patch: dict = {}
    if g:
        for k, v in (("window_x", g["x"]), ("window_y", g["y"]),
                     ("window_w", g["w"]), ("window_h", g["h"])):
            cv = _clamp_geom(k, v)
            if cv is not None:
                patch[k] = cv
    if f:
        for k, v in (("float_x", f["x"]), ("float_y", f["y"])):
            cv = _clamp_geom(k, v)
            if cv is not None:
                patch[k] = cv
    if not patch:
        return
    cfg.update(patch)
    # 内存副本也跟着对齐（只动这几个键）：下次读 runtime['config'] 的代码看到的
    # 几何与磁盘一致，不必等重启。
    with contextlib.suppress(Exception):
        service.runtime["config"].update(patch)
    try:
        service.save_config(cfg)
        log.info(f"[desktop] 版式已记忆: 主窗 {g or '—'} / 悬浮条 {f or '—'}")
    except Exception as e:
        log.warning(f"[desktop] ⚠ 版式记忆写入失败: {e}")


def _native_present(window) -> bool:
    """pywebview 窗口的 native 句柄/对象是否已创建。**只证明「创建过」**：native 是
    窗口创建后赋值、销毁时不清的（winforms.py:195 赋值；cocoa.py:595 赋值，关窗只在
    windowWillClose_ 里删 BrowserView.instances + webview.windows，不清 native）——
    所以它不能单独当存活判据（见 _float_alive）。"""
    return getattr(window, "native", None) is not None


def _float_alive(window, shown_ok: bool | None = None) -> bool:
    """窗口是否真实存活（p1-review 修复⑨的判据增强）。

    **Windows**：native 是构造时赋值、窗口销毁不清（winforms 只删
    BrowserView.instances[uid]），单靠 `native is not None` 判断会永远为真——
    必须查 user32.IsWindow(句柄)。

    **非 Windows（macOS/Linux）**：没有 IsWindow，判据取两个事实 ——
    native 已创建（未销毁）**且** 本会话至少成功 show/hide 过一次（shown_ok，
    由 _poll_tray_state 的 float_ready 记录）。为什么必须有后半句：native 在窗口
    销毁后不清，单靠它会把「已被销毁/从未真正可控」的窗口判成活。为什么
    shown_ok=None 要单独放行：**「要 show 成功才算活、要活才去 show」是互锁的**
    ——首次尝试必须放行（由调用方的宽限窗 _float_wait_until 兜底），否则悬浮条
    在 Mac 上永远起不来（本项点名的「必须修」）。"""
    if os.name != "nt":
        if not _native_present(window):
            return False
        return True if shown_ok is None else bool(shown_ok)
    try:
        return bool(ctypes.windll.user32.IsWindow(int(window.native.Handle.ToInt32())))
    except Exception:
        return False


class DesktopApi:
    """§3：暴露给页面的原生交互（pywebview `js_api` → `window.pywebview.api`）。

    只暴露**零参数、零副作用**的最小面：弹系统原生文件夹选择框，把用户**自己选的**
    目录路径原样返回（取消/失败 → None）。页面拿到路径后再走 POST /api/config 写入，
    仍要过 export_dir 的专项校验（绝对路径/可创建/可写/非系统关键目录）。

    **绝不接受前端传入的路径**：那等于把本机文件系统的选择权交给页面（含
    DNS-rebinding 场景下的恶意页）；原生对话框是用户亲手操作的，路径来源可信。

    为什么用 js_api 而不是在服务端弹框：桌面壳与服务是两个线程/两种运行形态，
    文件夹选择框是 Win32 原生 UI，只有 webview 窗口线程能弹——纯服务模式没有它，
    前端自动退化为「手动输入路径 + 打开文件夹」（见 index.html 的导出卡）。"""

    def pick_export_dir(self) -> str | None:
        """弹原生文件夹选择框 → 用户选中的绝对路径；取消/不可用 → None。"""
        try:
            import webview
            wins = getattr(webview, "windows", None) or []
            if not wins:
                log.warning("[desktop] ⚠ 原生文件夹选择不可用（窗口未就绪）")
                return None
            res = wins[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            log.warning(f"[desktop] ⚠ 原生文件夹选择失败: {type(e).__name__}: {e}")
            return None
        if not res:                      # 用户取消 → None/空
            return None
        first = res[0] if isinstance(res, (list, tuple)) else res
        if not first:
            return None
        log.info(f"[desktop] 用户选择导出目录: {first}（由页面走 /api/config 校验写入）")
        return str(first)


# —— 托盘（pystray + pillow；图标色值取 docs/design-spec.md §2 令牌）——

def tray_state(asr: str = "", mic: str = "", paused: bool = False) -> str:
    """托盘状态判定纯函数（--check headless 单测）：error > paused > ok。
    asr/mic 任一以 'error' 开头（main.status 的错误口径，如
    'error: init failed (...)') → 'error'；paused → 'paused'；
    其余（init/ready/loading model）→ 'ok'。"""
    if str(asr).startswith("error") or str(mic).startswith("error"):
        return "error"
    if paused:
        return "paused"
    return "ok"


def _tray_image(state: str = "ok"):
    """托盘图标（state ∈ tray_state 取值域）= 品牌应用图标 64px + 右下状态灯。
    绘制单一事实源在 branding.draw_app_icon（p1-3：与 assets/icon.ico 同一设计
    ——深蓝黑底 + 金色镜头/字幕条；状态灯圆心 (50,50)，样式 = design-spec §六
    「圆点 + 同色 8% 光晕」，--check 的像素断言锚点不变）。"""
    return branding.draw_app_icon(64, state)


def _hotkey_hints() -> dict:
    """托盘菜单里的快捷键提示（p3-5：让「托盘与快捷键行为一致」**看得见**）。

    取**当前生效**的绑定（用户改过键就显示新键），拿不到（热键管理器还没建/
    注册失败降级）则回落模块默认值——菜单文案不该因为热键不可用而消失。"""
    try:
        return dict(service.runtime["hotkeys"].state()["bindings"])
    except Exception:
        return dict(hotkeys.DEFAULT_BINDINGS)


def _build_tray_menu(*, visible, floating, paused, click_through, acts, hints=None):
    """构建托盘菜单（独立模块函数，使 --check 能 headless 构造验证）。pystray
    MenuItem 的 text/checked 均接受 callable、求值时传入 item 自身
    （pystray _base.py:466-478 实源）。visible/floating/paused/click_through：
    无参 callable → 当前布尔；acts：六动作 callable。动态项的实时刷新由状态轮询调
    icon.update_menu() 完成（win32 缓存 HMENU，_base.py:261 文档口径）。
    p1-7：+「字幕条点击穿透」——穿透开启后悬浮窗收不到鼠标事件，本菜单项是
    解除穿透的唯一交互入口（页面/设置页只剩说明）。
    p3-5：hints = {动作: 键位} → 在对应项后附「（Ctrl+Alt+L）」——托盘与全局快捷键
    走的是同一份实现（service.toggle_pause / toggle_float），把键位写在菜单上，
    「二者一致」这件事才可核对、可发现（否则用户根本不知道有快捷键）。"""
    import pystray
    hints = hints or {}

    def _with_hint(text: str, action: str) -> str:
        c = hints.get(action)
        return f"{text}（{c.upper()}）" if c else text

    return pystray.Menu(
        # 左键/双击 = 切换主窗显隐；动态文本即互斥闭环的菜单侧表达
        pystray.MenuItem(lambda item: "隐藏主窗" if visible() else "显示主窗",
                         acts["toggle_window"], default=True),
        pystray.MenuItem(_with_hint("悬浮字幕条", "toggle_float"),
                         acts["toggle_floating"],
                         checked=lambda item: bool(floating())),
        pystray.MenuItem("字幕条点击穿透", acts["toggle_click_through"],
                         checked=lambda item: bool(click_through())),
        pystray.MenuItem(lambda item: _with_hint(
            "继续字幕" if paused() else "暂停字幕", "toggle_pause"),
            acts["toggle_pause"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开字幕页（浏览器）", acts["open_browser"]),
        pystray.MenuItem("退出", acts["quit"]),
    )


# —— 桌面模式 ——

def run_desktop(glossary: list[str] | None = None,
                minimized: bool = False) -> int:
    """桌面模式主流程，返回退出码。glossary = --glossary 透传的术语表路径列表
    （p1-review 修复③：术语表注入是 README 主推能力，旧 bat 直通 main.py 时可用，
    改走 desktop 后不能丢——其余旗标经 config.json/设置页热切，仍走 --server-only）。

    p3-3：minimized = --minimized——主窗**创建即隐藏**，只留托盘（开机自启的
    默认姿态：开机不该弹窗打断人）。窗口仍是真窗口（winforms 用 Opacity=0 +
    Show/Hide 建，见 winforms.py:777-781），故托盘「显示主窗」照常能拉出来。"""
    # 与 main.main() 同一套启动副作用：日志落盘 + 首启 token（值只 print 不落日志）
    service.setup_logging(service.ROOT / "logs")
    service.provision_token()
    try:  # 信息行对齐 main.py:1110-1115
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        log.info(f"[desktop] 🎙 输入设备: {dev['name']}")
    except Exception as e:
        log.warning(f"[desktop] ⚠ 查询输入设备失败: {e}")

    # 无 CLI 覆盖的占位 args：asr_loop 语义 = 纯 config.json（与无参 main.py 一致）；
    # glossary 例外：由 --glossary 透传（p1-review 修复③）。
    # 字段必须覆盖 asr_loop/build_lifespan 读取的全部属性（main.py:882-897/1142）。
    ns = argparse.Namespace(model=None, gpu=False, compute_type=None, vad=None,
                            source=None, streaming=False, lang=None, device=None,
                            api_key=None, glossary=list(glossary or []),
                            open_browser=False)
    service.ARGS = ns  # main.apply_segmenter 兜底读 ARGS.device（main.py:430）
    service.app.router.lifespan_context = service.build_lifespan(ns)

    th, server, port = start_server()
    # §7：绑定事实取自 start_server 记下的 runtime['bind']（is_lan_host 已含 None/解析
    # 失败的 fail-closed 分支），不重复判一次「有没有值」。真开了就说真的（面板/二维码
    # 里的地址与端口都以此为准）；没开就说没开——不含糊其词，也不写「立即生效」
    # （换绑要重启，见 main.lan_bind_state）。
    bind_lan = lan_mod.is_lan_host((service.runtime.get("bind") or {}).get("host"))
    if bind_lan:
        _lan_ip = lan_mod.primary_lan_ip()
        log.info(f"[desktop] 服务就绪: http://127.0.0.1:{port}"
                 f"（📱 手机同屏已开启：同时监听 0.0.0.0:{port}，手机连同一 WiFi 扫 "
                 f"http://{_lan_ip or '<电脑IP>'}:{port}/ 的配对二维码）")
    else:
        log.info(f"[desktop] 服务就绪: http://127.0.0.1:{port}（仅本机监听；"
                 "手机同屏在字幕页/设置页开启，重启 LectureLens 后生效）")

    if not _acquire_instance_lock(port):
        log.error(f"[desktop] 已有 LectureLens 实例在运行（锁: {LOCK_PATH}）")
        stop_server(server, th)
        _warn_already_running()
        return 2
    atexit.register(_release_instance_lock)

    # GUI 依赖延迟导入：--server-only 路径完全不触碰 webview/pystray/PIL
    import pystray
    import webview

    quit_requested = threading.Event()  # 「退出」标志：closing 事件据此放行销毁
    _exit_deadline = threading.Event()  # 正常收尾完成标志（finally 置位）——退出看门狗用
    window_visible = threading.Event()  # 主窗可见性事实源（互斥闭环，唯一写入口改）
    if not minimized:
        window_visible.set()            # create_window 默认显示（--minimized 则保持清空）
    poller_stop = threading.Event()     # 托盘状态轮询的停机标志
    _click_through_on = threading.Event()  # 字幕条当前是否穿透（p1-7，会话级状态）

    def _set_window_visible(v: bool) -> None:
        """主窗可见性唯一写入口（互斥闭环）：托盘切换/X 关窗藏托盘全部经此，
        布尔事实与窗口实际状态不漂移。show/hide 走 Invoke（winforms.py:494-500），
        托盘线程调用安全。"""
        if v:
            window.show()
            window_visible.set()
        else:
            window.hide()
            window_visible.clear()

    def _toggle_window(icon, item):
        _set_window_visible(not window_visible.is_set())

    def _toggle_floating(icon, item):
        """B1 悬浮字幕条显隐（p1-7）：写 config.floating_bar——与设置页开关、浮窗
        × 按钮同一事实源；窗口显隐由托盘轮询线程统一执行（唯一执行点），
        托盘勾选 checked 随之更新。
        p3-1：改走 service.toggle_float——与全局快捷键（Ctrl+Alt+S）共用同一份
        实现，两条入口不会各自漂移（原先此处内联的赋值逻辑已上移）。"""
        service.toggle_float("[托盘] ")

    def _toggle_click_through(icon, item):
        """点击穿透开关（p1-7，尽力而为）：穿透开启后悬浮窗收不到鼠标事件——
        本托盘菜单项是解除穿透的唯一交互入口。set_click_through 失败（降级）
        不翻转状态并通知用户。"""
        want = not _click_through_on.is_set()
        if set_click_through(window2, want):
            if want:
                _click_through_on.set()
            else:
                _click_through_on.clear()
            log.info(f"[desktop] 字幕条点击穿透{'开启' if want else '关闭'}")
        else:
            log.warning("[desktop] ⚠ 点击穿透不可用（降级：字幕条保持可点击可关闭）")
            with contextlib.suppress(Exception):
                icon.notify("点击穿透不可用（已降级：字幕条保持可点击可关闭）",
                            WINDOW_TITLE)

    def _toggle_pause(icon, item):
        """暂停/继续字幕：直改 service.status['paused']——与字幕页 /api/pause·
        /api/resume 同一事实源，双向闭环（页面轮询即时可见，托盘动态文本随之变）。
        p3-1：改走 service.toggle_pause——与全局快捷键（Ctrl+Alt+L）共用同一份
        实现（原先此处内联的翻转逻辑已上移，语义未变）。"""
        service.toggle_pause("[托盘] ")

    def _open_browser(icon, item):
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def _quit(icon, item):
        """托盘「退出」——唯一真出口。先立标志（放行 closing），再停托盘与服务，
        最后销毁窗口让 webview.start() 返回（p1-7：两窗都要 destroy，否则
        pywebview 的 _shutdown 等不到全部窗口关闭，start() 不返回）。destroy →
        winforms destroy_window 的 Invoke（winforms.py:1039-1047）跨线程安全；
        Close() 会走 FormClosing，由 _on_closing 的 quit_requested 守卫放行。

        p3-5：**销毁之前**先记忆版式——destroy 之后 window.x/y 就读不到了（对象仍
        在但已无原生窗口），那是「每次退出都记不住」的静默失效。

        首期反馈⑤（Mac）：cocoa 的 destroy 是 AppHelper.callAfter（异步投递），
        且退出可能被非守护线程（keyboard 的 darwin 监听等）拖住 → webview.start()
        不返回、进程变成"窗口没了但退不掉"的僵尸。这里每个动作独立兜底，并起一个
        5s 看门狗：仍未退出就释放锁、冲刷日志、硬退出。"""
        _save_geometry(window, window2)   # 必须在 destroy 之前
        quit_requested.set()
        server.should_exit = True
        with contextlib.suppress(Exception):
            icon.stop()
        # 每个销毁动作独立兜底：任一平台后端的 destroy 抛异常都不能把退出卡在半路
        with contextlib.suppress(Exception):
            window.destroy()
        with contextlib.suppress(Exception):
            window2.destroy()

        def _force_exit_if_stuck() -> None:
            if _exit_deadline.wait(5.0):
                return  # 主线程已正常收尾，进程会自己退出
            with contextlib.suppress(Exception):
                log.warning("[desktop] ⚠ 退出超时（主循环未返回/非守护线程拖住）"
                            "——强制退出")
                logging.shutdown()
            _release_instance_lock()
            os._exit(0)

        threading.Thread(target=_force_exit_if_stuck, name="quit-watchdog",
                         daemon=True).start()

    def _poll_tray_state(icon, stop: threading.Event) -> None:
        """托盘状态轮询（1s daemon，p1-7 起由 webview.start(func=) 回调启动——
        窗口就绪后才有第一个 tick）：tray_state 折算 service.status → 变色 +
        tooltip（icon 属性赋值自动 NIM_MODIFY，pystray _base.py:128-134）+ 动态
        菜单刷新；同时同步 B1 浮窗显隐（config.floating_bar → window2.show/hide，
        唯一执行点：托盘/设置页/浮窗×按钮的写入都在此收敛）。win32 菜单句柄有
        缓存，动态 text/checked 不调 update_menu 不重建（_base.py:261 文档口径）
        ——仅在状态特征变化时刷，避免每秒重建。"""
        last: tuple | None = None
        float_ready = False  # 曾成功 show/hide 过一次（子窗创建完成）才算就绪；
                             # 此前的 show/hide 会被 winforms 静默忽略
        float_wait_until = time.time() + 15  # 等子窗创建（_create_children 等主窗
                                             # shown 最长 10s）的宽限窗
        while not stop.wait(1.0):
            try:
                # B1 浮窗同步（p1-7）：配置驱动 + 句柄真实存活（p1-review 修复⑨：
                # native 非 None 不等于活着；窗口被销毁/创建失败时不能静默 no-op）
                # first_try = 非 Windows 的首次尝试放行：那里的判据要求「曾成功
                # show/hide 过一次」，而第一次尝试前这个事实还不存在（互锁）——
                # 宽限窗内先放行 native 已创建的窗口去试一次，成功即转正。
                # Windows 分支 first_try 恒 False（IsWindow 是唯一真相源）。
                first_try = (os.name != "nt" and not float_ready
                             and time.time() < float_wait_until
                             and _native_present(window2))
                if _float_alive(window2, float_ready) or first_try:
                    want_float = bool(service.runtime["config"].get("floating_bar"))
                    if not float_ready or want_float != _floating_on.is_set():
                        if want_float:
                            window2.show()
                            _floating_on.set()
                        else:
                            window2.hide()
                            _floating_on.clear()
                        float_ready = True
                        log.info(f"[desktop] 悬浮字幕条已{'显示' if want_float else '隐藏'}")
                elif time.time() >= float_wait_until:
                    # 窗口不可用（创建超时失败/被非常规销毁）——如实降级：写回
                    # config=false，否则托盘持续勾选「开启」而窗口根本不在（永久
                    # 静默失效；用户可在日志看到原因，重开即再触发一次创建尝试）
                    if bool(service.runtime["config"].get("floating_bar")):
                        log.warning("[desktop] ⚠ 悬浮条窗口不可用（创建失败或已被销毁）"
                                    "——已写回 config.floating_bar=false")
                        service.runtime["config"]["floating_bar"] = False
                        with contextlib.suppress(Exception):
                            service.save_config(service.runtime["config"])
                st = tray_state(service.status.get("asr") or "",
                                service.status.get("mic") or "",
                                bool(service.status.get("paused")))
                sig = (st, bool(service.status.get("paused")),
                       window_visible.is_set(), _floating_on.is_set(),
                       _click_through_on.is_set())
            except Exception as e:
                log.warning(f"[desktop] ⚠ 托盘状态读取失败: {e}")
                continue
            if sig == last:
                continue
            last = sig
            try:
                icon.icon = _tray_image(st)
                icon.title = _STATE_TITLES[st]
                icon.update_menu()
            except Exception as e:
                log.warning(f"[desktop] ⚠ 托盘刷新失败: {e}")

    st0 = tray_state(service.status.get("asr") or "",
                     service.status.get("mic") or "",
                     bool(service.status.get("paused")))
    icon = pystray.Icon(
        WINDOW_TITLE, _tray_image(st0), _STATE_TITLES[st0],
        _build_tray_menu(
            visible=lambda: window_visible.is_set(),
            floating=_floating_on.is_set,
            click_through=_click_through_on.is_set,
            paused=lambda: bool(service.status.get("paused")),
            hints=_hotkey_hints(),          # p3-5：附「（Ctrl+Alt+L）」等键位提示
            acts={"toggle_window": _toggle_window,
                  "toggle_floating": _toggle_floating,
                  "toggle_click_through": _toggle_click_through,
                  "toggle_pause": _toggle_pause,
                  "open_browser": _open_browser,
                  "quit": _quit}))

    # p3-5：主窗版式——优先上次退出时记忆的尺寸/位置（函数内部读盘，见其 docstring），
    # 不可用则默认 1280×800 居中。
    _geom = _window_geometry()
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/",
        min_size=WINDOW_MIN_SIZE, resizable=True,
        hidden=bool(minimized),   # p3-3：--minimized 开机自启不弹窗（见 run_desktop 注释）
        js_api=DesktopApi(),      # §3：设置页「选择文件夹…」的原生选择框
        **_geom,
    )

    # —— B1 悬浮字幕条（p1-7）：第二窗口，无边框 / 置顶 / 透明（WebView2 原生支持，
    # winforms.py:281-287「window transparency is supported only with EdgeChromium」）；
    # 位置：优先记忆值（p3-5），否则底部居中（_float_position）；初始显隐按 config；
    # 整窗可拖（easy_drag 经 JS 注入，customize.js:92-94）。主窗最小化不影响本窗
    # （独立 Form）——第二块屏场景。——
    window2 = webview.create_window(
        FLOAT_TITLE, f"http://127.0.0.1:{port}/float",
        width=FLOAT_SIZE[0], height=FLOAT_SIZE[1],
        frameless=True, easy_drag=True, on_top=True, transparent=True,
        resizable=False,
        hidden=not bool(service.runtime["config"].get("floating_bar")),
        **_float_position(service.load_config()),   # p3-5：悬浮条位置也记忆
    )
    # _floating_on = 「窗口当前可见」事实：实测非主窗的 hidden 参数不生效
    # （创建即可见，见 _start_poller 注释），故无条件置位；由 _start_poller
    # 按 config 收口（hide 失败则保持置位，轮询线程自动重试——自愈）
    _floating_on.set()

    def _on_float_closing():
        """p1-review 修复⑨：用户关闭悬浮窗（无边框窗未禁任务栏按钮/Alt+F4，
        winforms 无 WM_CLOSE 拦截）→ 等价 config.floating_bar=false（hide 保留
        窗口、托盘/设置页可再开），与主窗「X 藏托盘」同款哲学；返回 False 取消
        销毁——否则窗口对象不可复用（winforms show/hide 只认 instances[uid]），
        托盘却仍勾选、日志零提示的永久静默失效（评审员1 指出的问题）。"""
        if quit_requested.is_set():
            return None  # 真退出：放行销毁
        service.runtime["config"]["floating_bar"] = False
        with contextlib.suppress(Exception):
            service.save_config(service.runtime["config"])
        log.info("[desktop] 悬浮条被关闭——已写回 config.floating_bar=false"
                 "（托盘菜单/设置页可重新开启）")
        return False  # 取消销毁；隐藏由轮询线程按 config 执行

    window2.events.closing += _on_float_closing

    def _float_set_click_through(on: bool) -> bool:
        """点击穿透执行 + 状态同步（p1-review 修复⑥）：main 的
        /api/float/click-through 把请求转回本进程（ctypes 需进程内句柄；纯服务
        模式无此注册，端点 503 降级）。执行成功必须同步 _click_through_on——
        浮窗 📌 开启穿透后托盘勾选/菜单 want 要同步，否则第一次点菜单空转
        （而托盘正是穿透后唯一的解除入口）。"""
        ok = set_click_through(window2, bool(on))
        if ok:
            if on:
                _click_through_on.set()
            else:
                _click_through_on.clear()
            log.info(f"[desktop] 点击穿透{'开启' if on else '关闭'}"
                     "（浮窗/端点入口，托盘勾选已同步）")
        return ok

    service.float_control["set_click_through"] = _float_set_click_through

    def _on_closing():
        """X 按钮 → 隐藏到托盘。pywebview 6：closing handler 返回 False = 取消
        关闭（winforms.py:400-402）；Event.set 任一 handler 返回 False 即判取消
        （event.py:60-62）。托盘「退出」的 destroy 放行（返回 None 不拦）。"""
        if quit_requested.is_set():
            return None  # 真退出：放行
        _set_window_visible(False)  # 藏托盘走唯一写入口（可见性布尔同步）
        return False

    def _on_closed():
        if not quit_requested.is_set():
            log.warning("[desktop] ⚠ 窗口被直接销毁（closing 取消未生效？）——应用退出")

    window.events.closing += _on_closing
    window.events.closed += _on_closed

    icon.run_detached()  # win32: 后台线程跑消息循环（pystray/_win32.py:130-131）

    def _start_poller():
        """webview.start(func=) 回调（__init__.py:294-300 独立线程）：只启动托盘
        轮询线程。浮窗初始显隐的对齐交给轮询首个「就绪 tick」——本回调与子窗创建
        线程并发（__init__.py:281-294），此刻 window2 的 form 可能尚未创建，对它
        show/hide 会被 winforms 静默忽略（winforms 的 hide(uid) 找不到实例即
        return；真机冒烟实测踩中）。"""
        threading.Thread(target=_poll_tray_state, args=(icon, poller_stop),
                         name="tray-poller", daemon=True).start()

    try:
        webview.start(
            debug=os.environ.get("LECTURELENS_DEVTOOLS") == "1",
            func=_start_poller,
            **persistent_storage_kwargs())   # B15 评审修复①：关掉默认的私有模式（见该函数）
    finally:
        # p3-5：兜底再记一次版式——托盘「退出」不是唯一退出路径（异常退出 / 窗口被
        # 非常规销毁 / webview.start 自己返回）。窗口已销毁时 _read_geometry 返回空
        # 字典、_save_geometry 直接不写，不会把 None 覆盖掉已有记忆。
        _save_geometry(window, window2)
        poller_stop.set()
        _release_instance_lock()
        stop_server(server, th)
        with contextlib.suppress(Exception):
            icon.stop()
        _exit_deadline.set()          # 收尾完成：退出看门狗不再强制

    # 首期反馈⑤（Mac）：非守护线程（如 keyboard 的 darwin 监听）会让进程在
    # main 返回后继续挂着（窗口已关、托盘已停 = 用户眼里的"退不掉"）。如实记
    # 日志后强制退出，绝不留僵尸进程。
    leftover = [t.name for t in threading.enumerate()
                if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    if leftover:
        log.warning(f"[desktop] ⚠ 退出时仍有非守护线程 {leftover}——强制退出")
        with contextlib.suppress(Exception):
            logging.shutdown()
        os._exit(0)
    return 0


# —— --check：headless 自检（门禁）——

def run_check() -> int:
    """headless 自检，全过退出码 0、任一失败 1（进度行打 stdout 供门禁解析）：
    ① 桌面三依赖可导入 + 服务模块可导入（faster-whisper 全链） ② 托盘图标可
    生成 + pystray Icon 可构造（不 run） ③ tray_state 状态机矩阵 ④ 三态图标
    状态灯像素断言 ⑤ 菜单构建器动态 text/checked 求值 ⑥ p3-1 全局快捷键白名单与
    降级路径（纯逻辑，不装真钩子） ⑦ uvicorn 后台线程起
    服务（无 lifespan：不加载模型不开麦，秒级） ⑧ HTTP 探活 /api/status（200
    且 JSON 带 asr 键）与 /（index.html） ⑨ §3 js_api 文件夹选择桥（假 webview，
    不弹真对话框）。"""
    import importlib.metadata as md

    failures: list[str] = []
    holder: dict = {}

    def step(name: str, fn) -> bool:
        try:
            detail = fn() or ""
            print(f"[check] OK   {name}{(' — ' + detail) if detail else ''}")
            return True
        except Exception as e:
            failures.append(name)
            print(f"[check] FAIL {name}: {type(e).__name__}: {e}")
            return False

    def _deps() -> str:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
        import webview  # noqa: F401
        return (f"pywebview {md.version('pywebview')}, "
                f"pystray {md.version('pystray')}, pillow {md.version('pillow')}; "
                f"service app={type(service.app).__name__}")

    def _tray() -> str:
        import pystray
        from PIL import Image
        img = _tray_image("ok")
        assert isinstance(img, Image.Image) and img.size == (64, 64)
        assert img.getpixel((50, 50))[:3] == _STATE_COLORS["ok"], "状态灯像素色值不符"
        pystray.Icon("check", img, WINDOW_TITLE,
                     pystray.Menu(pystray.MenuItem("x", lambda *_: None)))
        return "64x64 RGBA + Icon 构造（不 run）"

    def _state() -> str:
        assert tray_state(asr="error: boom") == "error"
        assert tray_state(asr="ready", mic="error: gone") == "error"
        assert tray_state(asr="error: x", paused=True) == "error"  # error 优先
        assert tray_state(paused=True) == "paused"
        assert tray_state(asr="loading model (m)", mic="ok") == "ok"
        return "矩阵过：error(asr/mic) > paused > ok"

    def _icons() -> str:
        from PIL import Image
        for st, rgb in _STATE_COLORS.items():
            img = _tray_image(st)
            assert img.size == (64, 64), st
            assert img.getpixel((50, 50))[:3] == rgb, f"{st} 状态灯像素色值不符"
        return "3 态图标生成，状态灯像素色值逐一断言"

    def _menu() -> str:
        m = _build_tray_menu(
            visible=lambda: True, floating=lambda: False, paused=lambda: True,
            click_through=lambda: False,
            hints={"toggle_pause": "ctrl+alt+l", "toggle_float": "ctrl+alt+s"},
            acts={k: (lambda *a: None) for k in
                  ("toggle_window", "toggle_floating", "toggle_click_through",
                   "toggle_pause", "open_browser", "quit")})
        assert m.items[0].text == "隐藏主窗"   # 动态文本按 visible() 求值
        assert m.items[1].checked is False     # checked 按 floating() 求值
        assert m.items[1].text == "悬浮字幕条（CTRL+ALT+S）"   # p3-5：附键位提示
        assert m.items[2].checked is False     # p1-7：checked 按 click_through() 求值
        assert m.items[3].text == "继续字幕（CTRL+ALT+L）"     # 动态文本按 paused() 求值
        # 无键位提示时不得多出空括号（热键不可用时菜单照样要干净）
        m2 = _build_tray_menu(
            visible=lambda: True, floating=lambda: False, paused=lambda: False,
            click_through=lambda: False,
            acts={k: (lambda *a: None) for k in
                  ("toggle_window", "toggle_floating", "toggle_click_through",
                   "toggle_pause", "open_browser", "quit")})
        assert m2.items[1].text == "悬浮字幕条" and m2.items[3].text == "暂停字幕"
        return "菜单 7 描述符构建 + 动态 text/checked 求值正确（含 p3-5 键位提示）"

    def _hotkeys() -> str:
        """p3-1：全局快捷键的**纯逻辑**自检——键名白名单 + 降级路径。

        刻意用 enabled=False 构造管理器：只验状态机与校验，**不装真实全局钩子**
        （门禁不该在用户机器上注册热键）。真注册由真机冒烟覆盖。"""
        import hotkeys as hk
        assert hk.combo_problem("ctrl+alt+l") is None
        assert hk.combo_problem("l") is not None            # 无修饰键
        assert hk.combo_problem("ctrl+l+m") is not None     # 两个主键
        assert hk.combo_problem("ctrl+alt+zz") is not None   # 不在白名单
        assert hk.combo_problem("ctrl++l") is not None      # 空键名
        assert hk.combo_problem(None) is not None           # 类型错
        assert hk.normalize("Ctrl+Alt+L") == "ctrl+alt+l"    # 大小写归一
        assert hk.normalize("win+alt+1") == hk.normalize("windows+alt+1")  # 别名归一
        # 降级路径：关闭态 → active=False/reason=disabled，且不抛
        mgr = hk.HotkeyManager(actions={"toggle_pause": lambda: None,
                                        "toggle_float": lambda: None},
                               bindings=hk.DEFAULT_BINDINGS, enabled=False)
        st = mgr.start()
        assert st["active"] is False and st["reason"] == "disabled", st
        # 配置兜底：非法配置值回落默认（手改 config.json 不该让注册炸掉）
        fb = hk.bindings_from_config({"hotkey_pause": "!!bad!!", "hotkey_float": None})
        assert fb == hk.DEFAULT_BINDINGS, fb
        return (f"白名单/规范化/降级/配置兜底过：默认 "
                f"{hk.DEFAULT_BINDINGS['toggle_pause']} + "
                f"{hk.DEFAULT_BINDINGS['toggle_float']}")

    def _server() -> str:
        th, server, port = start_server()
        holder.update(th=th, server=server, port=port)
        host = (service.runtime.get("bind") or {}).get("host") or "127.0.0.1"
        return f"uvicorn listening on {host}:{port}"

    def _probe() -> str:
        import httpx
        base = f"http://127.0.0.1:{holder['port']}"
        deadline, last = time.time() + 20, None
        while True:
            try:
                r = httpx.get(f"{base}/api/status", timeout=2)
                if r.status_code == 200 and "asr" in r.json():
                    break
                last = f"/api/status -> {r.status_code}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            if time.time() > deadline:
                raise RuntimeError(f"/api/status 探活超时（last={last}）")
            time.sleep(0.25)
        idx = httpx.get(f"{base}/", timeout=5)
        if idx.status_code != 200:
            raise RuntimeError(f"/ -> {idx.status_code}")
        return (f"/api/status 200 (asr={r.json().get('asr')!r}), "
                f"/ 200 ({len(idx.content)} bytes)")

    def _float_page() -> str:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{holder['port']}/float", timeout=5)
        if r.status_code != 200:
            raise RuntimeError(f"/float -> {r.status_code}")
        if "btnThru" not in r.text or "btnHide" not in r.text:
            raise RuntimeError("/float 缺少关键元素（btnThru/btnHide）")
        return f"/float 200（{len(r.content)} bytes，含穿透/关闭按钮）"

    def _jsapi() -> str:
        """§3：js_api 桥（设置页「选择文件夹…」）的**纯逻辑**自检——用假 webview
        模块验「返回选中路径 / 取消回落 None / 窗口未就绪不炸」。真对话框不在门禁里
        弹（--check 不该在用户机器上冒出系统窗口），由真机手测覆盖。"""
        import sys as _sys
        import types

        class _FakeWin:
            def __init__(self, res):
                self.res = res

            def create_file_dialog(self, kind):
                return self.res

        fake = types.ModuleType("webview")
        fake.FOLDER_DIALOG = object()
        win = _FakeWin(["D:\\LectureLens-导出"])
        fake.windows = [win]
        old = _sys.modules.get("webview")
        _sys.modules["webview"] = fake
        try:
            api = DesktopApi()
            assert api.pick_export_dir() == "D:\\LectureLens-导出", "选中路径未回传"
            win.res = None                       # 用户取消
            assert api.pick_export_dir() is None, "取消应回 None"
            win.res = "D:\\x"
            fake.windows = []                    # 窗口未就绪
            assert api.pick_export_dir() is None, "无窗口应回 None 而不是抛"
        finally:
            if old is not None:
                _sys.modules["webview"] = old
            else:
                _sys.modules.pop("webview", None)
        return "选中/取消/无窗口 三态过（原生对话框走真机手测）"

    ok = all([
        step("deps import (webview/pystray/pillow/service)", _deps),
        step("tray icon generation", _tray),
        step("tray state machine", _state),
        step("tray icons x3 states", _icons),
        step("tray menu builder (dynamic text/checked)", _menu),
        step("hotkeys whitelist + degradation (p3-1)", _hotkeys),
        step("uvicorn service thread", _server),
        step("HTTP probe /api/status + /", _probe),
        step("float bar page /float", _float_page),
        step("js_api folder picker bridge (§3)", _jsapi),
    ])
    if holder:
        with contextlib.suppress(Exception):
            stop_server(holder["server"], holder["th"])
    print("[check] ALL OK" if ok else f"[check] FAILED: {failures}")
    return 0 if ok else 1


# —— CLI 分发 ——

def _split_argv(argv: list[str]) -> tuple[str, list[str]]:
    """分出桌面壳模式与转发旗标。--check/--server-only 是壳级旗标（store_true，
    无值参数，逐字扫描安全）；desktop 模式只额外接受 --glossary（术语表注入是
    README 主推能力，见 run_desktop 的 args 透传说明），其余旗标提示改用
    --server-only；--server-only 模式把其余旗标原样转发给 main.parse_args。"""
    mode, rest = "desktop", []
    for a in argv:
        if a == "--check":
            if mode == "server-only":
                raise ValueError("--check 与 --server-only 互斥")
            mode = "check"
        elif a == "--server-only":
            if mode == "check":
                raise ValueError("--check 与 --server-only 互斥")
            mode = "server-only"
        else:
            rest.append(a)
    return mode, rest


def _extract_desktop_flags(rest: list[str]) -> tuple[list[str], bool, list[str]]:
    """从桌面模式的剩余参数里摘出桌面壳自己的旗标，返回
    (glossary 列表, minimized, 未识别的其他旗标)。

    - `--glossary PATH` 与 `--glossary=PATH` 两种写法都收
      （p1-review 修复③：旧入口 bat 的 %* 直通 main.py 时术语表可用，改走 desktop
      后术语表注入永久失效——桌面模式必须保留这条能力）。
    - `--minimized`（p3-3）：启动即进托盘。它是**桌面模式专属**开关（纯服务模式
      没有窗口可最小化），故只在桌面分支解析；--server-only 分支要另行明确拒绝，
      否则会落到 main.parse_args 报一句 argparse 的 usage（用户看不懂）。"""
    glossary: list[str] = []
    bad: list[str] = []
    minimized = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--glossary" and i + 1 < len(rest):
            glossary.append(rest[i + 1])
            i += 2
        elif a.startswith("--glossary="):
            glossary.append(a.split("=", 1)[1])
            i += 1
        elif a == "--minimized":
            minimized = True
            i += 1
        else:
            bad.append(a)
            i += 1
    return glossary, minimized, bad


def main() -> int:
    # p1-review 修复⑩：--force-unlock——锁误判冲突/残留锁的人工挽回手段
    # （评审员1：判为冲突时用户只拿到一个 MessageBox，没有自救路径）
    if "--force-unlock" in sys.argv[1:]:
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--force-unlock"]
        if LOCK_PATH.exists():
            with contextlib.suppress(OSError):
                LOCK_PATH.unlink()
            print(f"[desktop] --force-unlock：已清除单实例锁 {LOCK_PATH.name}")
        else:
            print("[desktop] --force-unlock：无锁文件，继续启动")
    try:
        mode, rest = _split_argv(sys.argv[1:])
    except ValueError as e:
        print(f"[desktop] {e}", file=sys.stderr)
        return 2
    if mode == "check":
        if rest:
            print(f"[desktop] --check 不接受额外旗标: {rest}", file=sys.stderr)
            return 2
        return run_check()
    if mode == "server-only":
        # p3-3：--minimized 是桌面壳专属（纯服务模式没有窗口）——提前给一句人话，
        # 别让它落到 argparse 的 usage 里（那里只会说「unrecognized arguments」）
        if "--minimized" in rest:
            print("[desktop] --minimized 仅桌面模式可用（--server-only 无窗口可最小化）",
                  file=sys.stderr)
            return 2
        # 等价旧 main.py：剥掉 --server-only 后把命令行原样交给 main.main()
        # （它自己解析 sys.argv[1:]），日志/token/端口/浏览器行为零漂移
        sys.argv = [sys.argv[0]] + rest
        service.main()
        return 0
    glossary, minimized, bad = _extract_desktop_flags(rest)
    if bad:
        print(f"[desktop] 桌面模式不认识旗标: {bad}"
              "（要转发全部旗标请用 --server-only）", file=sys.stderr)
        return 2
    return run_desktop(glossary=glossary, minimized=minimized)


if __name__ == "__main__":
    sys.exit(main())
