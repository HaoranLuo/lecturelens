# -*- coding: utf-8 -*-
"""verify_dist.py — D3：**在临时目录里跑真产物**的验证（dist 保持干净）。

为什么先拷贝再跑：① 直接跑 dist/ 会在产物里落下 config.json/logs/transcripts（首启
写盘），把一个「可分发产物」变成用过的目录；② 顺带验证**可搬移**——Windows 的
onedir 产物换任意路径都能起、macOS 的 .app 包搬家仍能起（PyInstaller 的 _MEIPASS
相对 exe 解析，本脚本就是这条断言的执行者）。

检查项（每项一行 ✅/❌，任一失败 exit 1；**平台分支**见各条批注）：
  1. 产物版本号含 `expected_version()`（= branding.APP_VERSION，**不在此写死**）
     —— Windows 读 exe 文件版本（PowerShell，与资源管理器同源）；
     macOS 读 .app 的 CFBundleShortVersionString（`plutil -p Contents/Info.plist`）；
  2. 拷贝内 `--server-only --port 8798` 起得来，/api/status 200。模型断言按平台：
     Windows = 必须等于随包模型（build_exe.BUNDLE_MODEL，默认 small）；
     **macOS 不随包模型**（走 HF 缓存/下载，见 build_macos.py 文件头）→ 只断言
     status.model 已上报（非空），且 asr 状态**不做硬断言**（第一次跑要下载，慢是正常的，
     把它记成 ❌ 会让「没下过模型的干净 Mac」永远验不过；状态值照样打印出来）；
  3. /、/float、/privacy 三个页面全 200（桌面壳新页面没漏进包）；
  4. **用户数据落在包外** —— Windows：拷贝根（config.json + logs/ + transcripts/）；
     macOS：`~/Library/Application Support/LectureLens`（app/main.py 的 _frozen_root
     darwin 分支；.app 是签名覆盖的只读包体，数据写进去会破坏签名）。两侧都断言
     产物内部没有 config.json/.env/transcripts/logs（Windows 看 _internal，
     macOS 看 Contents——同一份禁用清单，见 build_exe.FORBIDDEN_DIST_ENTRIES）；
  5. 结束清理拷贝目录，不留进程、不留临时文件（macOS 的用户数据目录**绝不删**——
     那是真用户数据，本脚本只读断言）。

安全边界（本脚本的零容忍面，注释即契约）：
  - **请求目标只有一种**：本机环回的被测进程。URL 由 HOST 常量 + 整数端口 + 本文件
    写死的路径白名单拼出，拼完仍过 `checked_url`（路径白名单 + 解析后 is_loopback），
    并**禁用重定向**；脚本不接受任何来自命令行/环境的 URL、host 或路径。只用标准库。
  - **可触路径只有两类**：① 构建产物 `ROOT/dist/{LectureLens | LectureLens.app}`
    （`checked_dist_path` 要求解析后逐字相等或其下）；② 系统临时目录
    `tempfile.gettempdir()` 之下（`checked_tmp_path` 要求解析后确实在其下）。
    读/写/拷贝/删除/启动前都过这两道闸。
  - **启动被测程序的方式**：先 `os.chdir` 进**已验证**的拷贝目录（macOS 用拷贝的父目录），
    再以**本文件里写死的字面量**启动——Windows `LectureLens.exe`（不含路径分隔符）、
    macOS `LectureLens.app/Contents/MacOS/LectureLens`（字面量常量，不是运行时拼接）。
    没有 shell（`shell=False`，参数列表形式）。

用法: .venv/Scripts/python.exe scripts/verify_dist.py [--port 8798] [--model small]
      macOS: .venv/bin/python scripts/verify_dist.py [--port 8798]
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"                 # 环回：被测进程只听本机（fresh config 无 lan_enabled）
IS_MAC = sys.platform == "darwin"
EXE_NAME = "LectureLens.exe"       # Windows：字面量文件名（启动时用，见模块头安全边界）
#: macOS：.app 包名与包内可执行文件名（先定义成部分再拼，**不让两处各写一遍**）
MAC_APP_NAME = "LectureLens.app"
MAC_BINARY_NAME = "LectureLens"
#: ⚠ 语义 =「相对 .app 的**父目录**」的启动用相对路径（Popen 前会 chdir 到拷贝的父目录）。
#: **不要**把它拼到 .app 根上（那会把 .app 名拼两遍）——要「.app 内的可执行文件绝对路径」
#: 请用 binary_path()。
MAC_BINARY_REL = f"{MAC_APP_NAME}/Contents/MacOS/{MAC_BINARY_NAME}"
#: 产物根（Windows = onedir 目录；macOS = .app 包）
DIST_ROOT = ROOT / "dist" / (MAC_APP_NAME if IS_MAC else "LectureLens")
API_STATUS = "/api/status"
PACKS_API = "/api/glossary/packs"   # B13：随包术语库包列表（只读 GET，白名单内）
PAGES = ("/", "/float", "/privacy")
ALLOWED_PATHS = (API_STATUS, PACKS_API) + PAGES
#: 用户数据/密钥清单**不另立一份**：与构建侧同一个事实源（build_exe），
#: 免得「清单改了一处、另一处还照抄旧的」（D3 评审2 正是这么抓到的）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_exe    # noqa: E402  同目录的构建脚本（只读它的常量与扫描函数）
import build_macos  # noqa: E402  macOS 侧：数据目录口径与产物布局（只读常量）

#: 运行期应落在**程序目录**（拷贝根）的东西（一次 --server-only 首启就会生成）
#: ——Windows 口径；macOS 见 MAC_DATA_DIR（数据在包外的用户目录）。
RUNTIME_IN_ROOT = ("config.json", "logs", "transcripts")
#: 这些**不该**由一次首启产生（密钥/课程表/术语表/单实例锁）——出现即失败
ROOT_UNEXPECTED = (".env", "courses.json", "glossary", "desktop.lock")
#: macOS 的用户数据目录（与 app/main.py 的 _frozen_root('darwin') 同一处；test 有断言钉住）
MAC_DATA_DIR = Path.home() / build_macos.MAC_DATA_DIR_REL
RESULTS: list = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止重定向：被测进程若 302 到别处，本脚本**不跟**（返回 None = 拒绝处理）。"""

    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else ""))


def checked_url(port: int, path: str) -> str:
    """构造并校验请求 URL：仅 http + 仅环回 + 仅白名单路径（见模块头安全边界）。"""
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"非法端口: {port!r}")
    if path not in ALLOWED_PATHS:
        raise ValueError(f"非法路径（不在白名单）: {path!r}")
    if not ipaddress.ip_address(HOST).is_loopback:
        raise ValueError(f"非环回目标被拒: {HOST}")
    return f"http://{HOST}:{port}{path}"


def get(port: int, path: str, timeout: float = 5.0):
    target = checked_url(port, path)
    with _OPENER.open(target, timeout=timeout) as r:
        if not ipaddress.ip_address(r.url.split("/")[2].split(":")[0]).is_loopback:
            raise ValueError(f"响应 URL 非环回（拒收）: {r.url}")
        return r.status, r.read()


def checked_dist_path(p: Path) -> Path:
    """只接受构建产物目录**之内**的路径（DIST_ROOT 本身或其子项）。

    Windows = ROOT/dist/LectureLens；macOS = ROOT/dist/LectureLens.app（见 DIST_ROOT）。"""
    rp = Path(p).resolve()
    root = DIST_ROOT.resolve()
    if rp == root or root in rp.parents:
        return rp
    raise SystemExit(f"[verify] ✗ 只接受构建产物目录之内的路径 {root}（收到 {rp}）")


def checked_tmp_path(p: Path) -> Path:
    """只接受系统临时目录**之下**的路径（防误删/穿越；本脚本不碰仓库与用户数据）。"""
    rp = Path(p).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if rp != tmp and tmp in rp.parents:
        return rp
    raise SystemExit(f"[verify] ✗ 拒绝操作临时目录之外的路径: {rp}")


def checked_data_path(p: Path) -> Path:
    """只接受 macOS 用户数据目录（MAC_DATA_DIR）**之内**的路径。

    用途只有一处：失败诊断里读日志尾部（只读）。**本脚本绝不删改用户数据**——
    那是真用户数据（一学期的笔记），清理段只删临时目录里的拷贝（见 ⑤）。"""
    rp = Path(p).resolve()
    base = MAC_DATA_DIR.resolve()
    if rp == base or base in rp.parents:
        return rp
    raise SystemExit(f"[verify] ✗ 拒绝用户数据目录之外的路径: {rp}")


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((HOST, port)) == 0


def binary_path(dist_root: Path | str, mac: bool | None = None) -> Path:
    """产物根 → 其中**可执行文件**的路径（纯函数，平台可注入以便在 Windows 上断言 macOS 分支）。

    - Windows：`<onedir 目录>/LectureLens.exe`
    - macOS  ：`<.app 包**本身**>/Contents/MacOS/LectureLens`

    与 MAC_BINARY_REL 的区别是「相对谁」——那一个是相对 .app 的**父目录**（启动用），
    本函数收的是 .app **自身**。评审实测过的坑：把 MAC_BINARY_REL 拼到 .app 根上 →
    `…/LectureLens.app/LectureLens.app/Contents/MacOS/LectureLens`，`main()` 在任何
    检查项之前就 exit 1（macOS 上整条脚本不可用）。故这里只提供**一个**推导入口，
    并有 tests/test_macos_build.py 的「落点 + is_file」断言钉住。"""
    d = Path(dist_root)
    if IS_MAC if mac is None else mac:
        return d / "Contents" / "MacOS" / MAC_BINARY_NAME
    return d / EXE_NAME


def mac_bundle_plist(target: Path | str) -> Path:
    """定位 .app 的 Contents/Info.plist。target 可以是 .app 本身、包内任意文件
    （如 Contents/MacOS/LectureLens）——统一解析到最近的 .app 祖先。"""
    p = Path(target).resolve()
    for cand in (p, *p.parents):
        if cand.suffix == ".app" and (cand / "Contents" / "Info.plist").is_file():
            return cand / "Contents" / "Info.plist"
    raise SystemExit(f"[verify] ✗ 找不到 .app/Contents/Info.plist（从 {p} 往上找）")


def parse_plutil_version(text: str) -> str:
    """从 `plutil -p` 的输出里取 CFBundleShortVersionString（纯函数，可单测）。

    plutil -p 打印的是 `"键" => 值` 形式（字符串带引号）；取不到 → 空串（调用方记 ❌
    并把原始输出打出来，不猜一个版本号）。"""
    m = re.search(r'"CFBundleShortVersionString"\s*=>\s*"([^"]*)"', text or "")
    return m.group(1) if m else ""


def mac_bundle_version(target: Path | str) -> str:
    """macOS 读产物版本：`plutil -p` 打印 Info.plist，取 CFBundleShortVersionString。

    为什么不用 defaults：plutil 是系统自带、对任意路径都 read-only，且不碰用户的
    defaults 域（本脚本的只读纪律）。"""
    plist = mac_bundle_plist(target)
    out = subprocess.run(["plutil", "-p", str(plist)],
                         capture_output=True, text=True, timeout=90)
    return parse_plutil_version((out.stdout or "") + (out.stderr or ""))


def exe_file_version(exe: Path) -> str:
    """产物版本号（平台分支）：
    - Windows：PowerShell 读 exe 文件版本资源（与资源管理器「详细信息」同源）；
    - macOS：plutil -p 读 .app 的 CFBundleShortVersionString（见 mac_bundle_version）。"""
    if IS_MAC:
        return mac_bundle_version(exe)
    quoted = str(exe).replace("'", "''")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Item -LiteralPath '{quoted}').VersionInfo.FileVersion"],
        capture_output=True, text=True, timeout=90)
    return (out.stdout or "").strip()


def expected_model() -> str:
    """随包模型名 = 构建脚本的 BUNDLE_MODEL（单一事实源，见文件头的 import build_exe）。"""
    return build_exe.BUNDLE_MODEL


def expected_version() -> str:
    """期望的产物版本 = branding.APP_VERSION（**单一事实源**，见 build_exe.app_version）。

    为什么必须派生而不是写死字面量：版本号是发版时**唯一要改的那一处**（分发出指南
    §一：'改版本就改这一处'），而本脚本原先写的是 `"1.0.0" in ver` —— 每次发版都得
    记得回来改这条探针，忘一次门禁就假红（升级 1.1.0 时它就红了）；若反过来把字面量
    改松成 `any(...)` 又会变成假绿。派生之后它与 exe 版本资源、安装包文件名同源。

    判定用「**包含**」而不是相等：Windows 的 FileVersion 是**四段**固定形式
    （APP_VERSION='1.1.0' 经 build_exe.version_quad 补成 '1.1.0.0'，实测
    GetVersionNumbersString/VersionInfo.FileVersion 读回 '1.1.0.0'），
    macOS 侧 CFBundleShortVersionString 是三段形式——「包含」让两个平台同一条口径。"""
    return build_exe.app_version()


def main() -> int:
    ap = argparse.ArgumentParser(description="LectureLens 产物验证（拷贝到临时目录跑真 exe）")
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--model", default=None, help="期望的随包模型（默认 build_exe.BUNDLE_MODEL）")
    ap.add_argument("--timeout", type=float, default=240.0, help="等 /api/status 就绪的上限秒数")
    args = ap.parse_args()
    want_model = args.model or expected_model()

    src_dist = checked_dist_path(DIST_ROOT)
    # 可执行文件路径**只经 binary_path 推导**（评审修复：旧写法把 .app 名拼了两遍，
    # macOS 上会在任何检查项之前 exit 1；见 binary_path 的 docstring）
    src_exe = checked_dist_path(binary_path(src_dist))
    if not src_exe.is_file():
        hint = ("先跑 scripts/build_macos.py" if IS_MAC else "先跑 scripts/build_exe.py")
        print(f"[verify] ✗ 找不到 {src_exe}（{hint}）")
        return 1
    if port_busy(args.port):
        print(f"[verify] ✗ 端口 {args.port} 已被占用（先停掉占用进程，或用 --port 换一个）")
        return 1

    # ① 产物版本（读 dist 原件；拷贝内的字节相同）
    ver = exe_file_version(src_exe)
    want_ver = expected_version()
    rec(f"产物版本含 {want_ver}"
        f"（{'CFBundleShortVersionString' if IS_MAC else 'FileVersion'}）",
        want_ver in ver, f"读到 {ver!r}")

    tmp_root = checked_tmp_path(Path(tempfile.mkdtemp(prefix="lecturelens-verify-")))
    # 拷贝时保留 .app 目录名（macOS 的包形态本身就是断言对象）；Windows 与旧版逐字相同
    copy = checked_tmp_path(tmp_root / src_dist.name)
    proc = None
    cleaned = False
    try:
        print(f"[verify] 拷贝产物 → {copy}（验证可搬移；dist 保持干净）…")
        # macOS：symlinks=True 保住包内交叉软链（Resources ↔ Frameworks）——按内容拷贝
        # 会破坏 ad-hoc 签名对软链条目的封印（arm64 拒绝执行签名已损坏的二进制）；
        # Windows 侧没有软链，保持默认行为逐字不变。
        shutil.copytree(checked_dist_path(src_dist), copy,
                        symlinks=IS_MAC)

        # ② 起真产物：chdir 进已验证的拷贝目录（macOS 用拷贝的父目录）+ **本文件里写死的
        # 字面量**命令（见模块头安全边界：两条 Popen 各自内联，命令行里不出现任何运行时
        # 拼接出来的可执行路径）。子进程的 stdout/stderr 走 DEVNULL——它自己会写日志
        # （Windows 在拷贝根的 logs/、macOS 在用户数据目录的 logs/）。
        prev_cwd = os.getcwd()
        try:
            if IS_MAC:
                os.chdir(str(tmp_root))
                proc = subprocess.Popen([MAC_BINARY_REL, "--server-only", "--port", str(args.port)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            else:
                os.chdir(str(copy))
                proc = subprocess.Popen([EXE_NAME, "--server-only", "--port", str(args.port)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        finally:
            os.chdir(prev_cwd)
        status, why = None, ""
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                code, body = get(args.port, API_STATUS)
                if code == 200:
                    status = json.loads(body)
                    if status.get("model") == want_model or (IS_MAC and status.get("model")):
                        break
            except Exception as e:            # 还没起来：继续等（超时后统一报）
                why = f"{type(e).__name__}: {e}"
        # 模型断言分平台：Windows = 必须是随包模型（离线开箱可用的承诺）；
        # macOS 不随包模型（走 HF 缓存/下载，见 build_macos.py 文件头）→ 只验「已上报模型名」。
        if IS_MAC:
            st_ok = bool(status) and bool(status.get("model"))
            label = "/api/status 200 且 status.model 已上报（macOS 不随包模型）"
        else:
            st_ok = bool(status) and status.get("model") == want_model
            label = f"/api/status 200 且 status.model={want_model}（随包模型）"
        rec(label, st_ok,
            f"model={status.get('model') if status else None}"
            f" · asr={status.get('asr') if status else None}"
            f" · version={status.get('version') if status else None}"
            + ("" if st_ok else f"（等待中最后错误: {why}）"))
        if status and str(status.get("asr", "")).startswith("error"):
            if IS_MAC:
                # 干净 Mac 上「第一次要联网下载模型」是正常路径——把它记成 ❌ 会让这条
                # 检查永远红。如实打印状态，不假装通过、也不误报失败。
                print(f"  ℹ asr 状态: {status.get('asr')}"
                      "（macOS 未随包模型：首次运行需下载或从 HF 缓存加载，"
                      "本项在 macOS 上不做硬断言）")
            else:
                rec("asr 非错误态", False, f"asr={status.get('asr')}")

        # ③ 三个页面全 200
        for path in PAGES:
            try:
                code, body = get(args.port, path)
                rec(f"GET {path} 200", code == 200, f"{len(body)} bytes")
            except Exception as e:
                rec(f"GET {path} 200", False, f"{type(e).__name__}: {e}")

        # ③b B13：随包术语库包在**打包版里真能读出来**。产物侧「文件在不在」由
        # build_exe.assert_dist_tree 断言；这里补的是运行期那一段——目录常量若写错
        # （比如 frozen 分支没指向 _MEIPASS），文件在包却列不出来，且只有跑起来才看得见。
        try:
            code, body = get(args.port, PACKS_API)
            data = json.loads(body) if code == 200 else {}
            items = data.get("packs") or []
            ok = code == 200 and bool(items) and all(
                p.get("name") and isinstance(p.get("count"), int) and p["count"] > 0
                for p in items)
            rec("B13 /api/glossary/packs 随包可读（frozen 双路径）", ok,
                f"HTTP {code} · {len(items)} 个包"
                + (f" · 首包 {items[0].get('name')}/{items[0].get('count')} 条"
                   if items else "（⚠ 空列表：_internal/glossary_packs 没被读到？）"))
        except Exception as e:
            rec("B13 /api/glossary/packs 随包可读（frozen 双路径）", False,
                f"{type(e).__name__}: {e}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()
        time.sleep(2)                          # 让 Windows 松开文件句柄
        # ④ 用户数据落在**包外**（**清理之前**断言）。平台口径不同：
        #    Windows = 程序目录（拷贝根，config.json/logs/transcripts 随 exe 走）；
        #    macOS   = ~/Library/Application Support/LectureLens（app/main.py 的
        #    _frozen_root darwin 分支）——.app 是签名覆盖的只读包体，数据写进去会破坏
        #    签名，也让「换新 .app」变成「数据一起丢」。这里只**读**用户数据目录，
        #    绝不删改（清理段只删临时拷贝，见 ⑤）。
        if IS_MAC:
            data_artifacts = {n: (MAC_DATA_DIR / n).exists() for n in RUNTIME_IN_ROOT}
            rec(f"用户数据落在用户目录（~/{build_macos.MAC_DATA_DIR_REL}）",
                all(data_artifacts.values()),
                ", ".join(f"{k}={v!s}" for k, v in data_artifacts.items())
                + "（只读断言；本脚本不删改用户数据）")
            inside = {n: (copy / n).exists() for n in RUNTIME_IN_ROOT}
            rec(".app 包内无用户数据（数据面在包外）", not any(inside.values()),
                f"包内命中: {[k for k, v in inside.items() if v]}" if any(inside.values())
                else "干净")
            # macOS 的「只读打包数据」= Contents（等价于 Windows 的 _internal）：
            # 同一份禁用清单、同一个扫描函数（build_exe.forbidden_hits）
            contents = checked_tmp_path(copy / "Contents")
            inner_hits = build_exe.forbidden_hits(contents)
            rec("Contents 内无用户数据/密钥（深扫任意层级）", not inner_hits,
                f"命中: {inner_hits[:6]}" if inner_hits else "干净")
        else:
            repo_artifacts = {n: (copy / n).exists() for n in RUNTIME_IN_ROOT}
            rec("用户数据落在程序目录（config.json + logs/ + transcripts/）",
                all(repo_artifacts.values()),
                ", ".join(f"{k}={v!s}" for k, v in repo_artifacts.items()))
            # D3 评审修复②：_internal（只读打包数据）**深扫任意层级**——清单与扫描函数都
            # 直接用 build_exe 的（单一事实源），不再另抄一份（旧版抄漏 glossary/courses.json
            # 且只看顶层存在性）。
            internal = checked_tmp_path(copy / "_internal")
            inner_hits = build_exe.forbidden_hits(internal)
            rec("_internal 内无用户数据/密钥（深扫任意层级）", not inner_hits,
                f"命中: {inner_hits[:6]}" if inner_hits else "干净")
            unexpected = [n for n in ROOT_UNEXPECTED if (copy / n).exists()]
            rec("拷贝根无非首启产物（.env/课程表/术语表/锁）", not unexpected,
                f"命中: {unexpected}" if unexpected else "干净")
        # 失败诊断：读应用自己写的日志尾部（只读、路径已验证；macOS 的日志在用户数据目录，
        # 那份走 checked_data_path——它不在临时目录下，不能过 checked_tmp_path）
        if any(not ok for _n, ok, _t in RESULTS):
            try:
                if IS_MAC:
                    app_log = checked_data_path(MAC_DATA_DIR / "logs" / "lecturelens.log")
                else:
                    app_log = checked_tmp_path(copy / "logs" / "lecturelens.log")
                tail = app_log.read_text(encoding="utf-8", errors="replace").splitlines()
                print("  ---- 应用日志尾部 ----")
                for line in tail[-12:]:
                    print("   " + line)
            except Exception as e:
                print(f"  ---- 应用日志读不到: {type(e).__name__}: {e} ----")
        # ⑤ 清理（只删临时目录里的拷贝）
        for _ in range(5):
            shutil.rmtree(checked_tmp_path(tmp_root), ignore_errors=True)
            if not tmp_root.exists():
                cleaned = True
                break
            time.sleep(2)
        rec("临时目录已清理", cleaned,
            "已删除" if cleaned else f"残留（删除失败）: {tmp_root}")

    print("\n========== 结果汇总 ==========")
    for name, ok, note in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else ""))
    fails = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"总计 {len(RESULTS)} 项，失败 {fails} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
