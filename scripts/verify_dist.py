# -*- coding: utf-8 -*-
"""verify_dist.py — D3：**在临时目录里跑真 exe** 的产物验证（dist 保持干净）。

为什么先拷贝再跑：① 直接跑 dist/ 会在产物里落下 config.json/logs/transcripts（首启
写盘），把一个「可分发产物」变成用过的目录；② 顺带验证**可搬移**——onedir 产物换任意
路径都能起（PyInstaller 的 _MEIPASS 相对 exe 解析，本脚本就是这条断言的执行者）。

检查项（每项一行 ✅/❌，任一失败 exit 1）：
  1. exe 文件版本（PowerShell 的 (Get-Item).VersionInfo.FileVersion）含 1.0.0；
  2. 拷贝内 LectureLens.exe --server-only --port 8798 起得来，/api/status 200 且
     status.model = 随包模型（默认取 build_exe.BUNDLE_MODEL，即 small）；
  3. /、/float、/privacy 三个页面全 200（桌面壳新页面没漏进包）；
  4. 用户数据落在**程序目录**（拷贝根）：config.json + logs/ 生成；而 _internal/
     （只读打包数据）里没有 config.json/.env/transcripts/logs；
  5. 结束清理拷贝目录，不留进程、不留临时文件。

安全边界（本脚本的零容忍面，注释即契约）：
  - **请求目标只有一种**：本机环回的被测进程。URL 由 HOST 常量 + 整数端口 + 本文件
    写死的路径白名单拼出，拼完仍过 `checked_url`（路径白名单 + 解析后 is_loopback），
    并**禁用重定向**；脚本不接受任何来自命令行/环境的 URL、host 或路径。只用标准库。
  - **可触路径只有两类**：① 构建产物 `ROOT/dist/LectureLens`（`checked_dist_path`
    要求解析后逐字相等）；② 系统临时目录 `tempfile.gettempdir()` 之下（
    `checked_tmp_path` 要求解析后确实在其下）。读/写/拷贝/删除/启动前都过这两道闸。
  - **启动被测程序的方式**：先 `os.chdir` 进**已验证**的拷贝目录，再以**字面量文件名**
    `LectureLens.exe`（不含任何路径分隔符）启动——命令行里不出现任何拼接出来的可执行
    路径，也没有 shell（`shell=False`，参数列表形式）。

用法: .venv/Scripts/python.exe scripts/verify_dist.py [--port 8798] [--model small]
"""
import argparse
import ipaddress
import json
import os
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
EXE_NAME = "LectureLens.exe"       # 字面量文件名（启动时用，见模块头安全边界）
API_STATUS = "/api/status"
PAGES = ("/", "/float", "/privacy")
ALLOWED_PATHS = (API_STATUS,) + PAGES
#: 用户数据/密钥清单**不另立一份**：与构建侧同一个事实源（build_exe），
#: 免得「清单改了一处、另一处还照抄旧的」（D3 评审2 正是这么抓到的）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_exe  # noqa: E402  同目录的构建脚本（只读它的常量与扫描函数）

#: 运行期应落在**程序目录**（拷贝根）的东西（一次 --server-only 首启就会生成）
RUNTIME_IN_ROOT = ("config.json", "logs", "transcripts")
#: 这些**不该**由一次首启产生（密钥/课程表/术语表/单实例锁）——出现即失败
ROOT_UNEXPECTED = (".env", "courses.json", "glossary", "desktop.lock")
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
    """只接受构建产物目录**之内**的路径（ROOT/dist/LectureLens 本身或其子项）。"""
    rp = Path(p).resolve()
    root = (ROOT / "dist" / "LectureLens").resolve()
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


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((HOST, port)) == 0


def exe_file_version(exe: Path) -> str:
    """PowerShell 读 Windows 文件版本（与资源管理器「详细信息」同源）。"""
    quoted = str(exe).replace("'", "''")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Item -LiteralPath '{quoted}').VersionInfo.FileVersion"],
        capture_output=True, text=True, timeout=90)
    return (out.stdout or "").strip()


def expected_model() -> str:
    """随包模型名 = 构建脚本的 BUNDLE_MODEL（单一事实源，见文件头的 import build_exe）。"""
    return build_exe.BUNDLE_MODEL


def main() -> int:
    ap = argparse.ArgumentParser(description="LectureLens 产物验证（拷贝到临时目录跑真 exe）")
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--model", default=None, help="期望的随包模型（默认 build_exe.BUNDLE_MODEL）")
    ap.add_argument("--timeout", type=float, default=240.0, help="等 /api/status 就绪的上限秒数")
    args = ap.parse_args()
    want_model = args.model or expected_model()

    src_dist = checked_dist_path(ROOT / "dist" / "LectureLens")
    src_exe = checked_dist_path(src_dist / EXE_NAME)
    if not src_exe.is_file():
        print(f"[verify] ✗ 找不到 {src_exe}（先跑 scripts/build_exe.py）")
        return 1
    if port_busy(args.port):
        print(f"[verify] ✗ 端口 {args.port} 已被占用（先停掉占用进程，或用 --port 换一个）")
        return 1

    # ① exe 文件版本（读 dist 原件；拷贝内的字节相同）
    ver = exe_file_version(src_exe)
    rec("exe 文件版本含 1.0.0", "1.0.0" in ver, f"VersionInfo.FileVersion={ver!r}")

    tmp_root = checked_tmp_path(Path(tempfile.mkdtemp(prefix="lecturelens-verify-")))
    copy = checked_tmp_path(tmp_root / "LectureLens")
    proc = None
    cleaned = False
    try:
        print(f"[verify] 拷贝产物 → {copy}（验证可搬移；dist 保持干净）…")
        shutil.copytree(checked_dist_path(src_dist), copy)

        # ② 起真 exe：chdir 进已验证的拷贝目录 + 字面量文件名（见模块头安全边界）。
        # 子进程的 stdout/stderr 走 DEVNULL——它自己会写 <copy>/logs/lecturelens.log，
        # 失败诊断读那份（startup_log_tail）。
        prev_cwd = os.getcwd()
        try:
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
                    if status.get("model") == want_model:
                        break
            except Exception as e:            # 还没起来：继续等（超时后统一报）
                why = f"{type(e).__name__}: {e}"
        st_ok = bool(status) and status.get("model") == want_model
        rec(f"/api/status 200 且 status.model={want_model}（随包模型）",
            st_ok, f"model={status.get('model') if status else None}"
                   f" · asr={status.get('asr') if status else None}"
                   f" · version={status.get('version') if status else None}"
                   + ("" if st_ok else f"（等待中最后错误: {why}）"))
        if status and str(status.get("asr", "")).startswith("error"):
            rec("asr 非错误态", False, f"asr={status.get('asr')}")

        # ③ 三个页面全 200
        for path in PAGES:
            try:
                code, body = get(args.port, path)
                rec(f"GET {path} 200", code == 200, f"{len(body)} bytes")
            except Exception as e:
                rec(f"GET {path} 200", False, f"{type(e).__name__}: {e}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()
        time.sleep(2)                          # 让 Windows 松开文件句柄
        # ④ 用户数据在程序目录、_internal 干净（**清理之前**断言）
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
        # 失败诊断：读拷贝内应用自己写的日志尾部（只读、路径已验证）
        if any(not ok for _n, ok, _t in RESULTS):
            try:
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
