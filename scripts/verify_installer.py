# -*- coding: utf-8 -*-
"""verify_installer.py — D4：安装包「装 → 用 → 造数据 → 卸 → 覆盖升级」全流程实测。

为什么必须真装真卸：安装包的行为（装到哪、装出什么、卸载删什么）**只有真跑一遍**
才知道。本脚本全程只在 `%TEMP%` 的自建子目录里折腾，**绝不碰**用户真实的
`%LocalAppData%\\LectureLens`（下面 `checked_install_dir` 把这条钉死）。

检查项（每项一行 ✅/❌，任一失败 exit 1）：
  1. 安装包体检：存在、非空、文件名含版本号、有 .sha256 校验文件且**核对通过**；
  2. 静默安装（/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=<临时目录>）退 0，
     且装出 REQUIRED_INSTALLED 全部关键产物（**派生自 build_exe.required_dist_files**，
     见该常量的注释：含 float/privacy 两页与 B13 术语库包抽查 + 随包「新手指引.txt」），
     并读回装好的 `LectureLens.exe` 的 FileVersion 断言 == 目标版本号；
  3. 装好的 `LectureLens.exe --check` 退 0（桌面壳 headless 自检，见 app/desktop.py）；
  4. 在**安装目录**造四类数据探针（见 PROBES）：transcripts/lecture_<stamp>.md
     （**真实笔记命名法**）、glossary/、logs/、config.json —— 覆盖面 = .iss 对用户
     承诺保留的那四类；
  5. 静默卸载（unins*.exe /VERYSILENT）→ 断言 **exe 已删**（程序文件清干净）
     但 **四类探针原样还在**（D2 数据保留口径，[UninstallDelete] 故意缺席）；
  6. 重新静默安装 → 探针仍在、exe 回来、--check 再退 0；
  7. **跨版本覆盖升级**：装「旧版」→ **在旧版下**造探针 → 装目标版**覆盖**它 →
     断言 ① 装好的 exe 版本 == **新版号**（否则"新文件没落地"照样全绿）、
     ② REQUIRED_INSTALLED 在覆盖后**重跑一遍**（1.1.0 的卖点就是随包新文件）、
     ③ 探针原样、④ --check 仍退 0。**必须传 `--upgrade-from <旧版安装包>`**；
     不传就判失败——评审指出：原先拿同一个安装包原地再装一遍只是"同版本重装"，
     撑不起「换新版覆盖安装不丢数据」这句话（那本该是这条最该验的路径）；
  8. 清理**也是断言**（评审指出原先只 print，rmtree 失败不影响结论）：卸载器已退出、
     临时目录已删净、**无残留进程**（tasklist）、无新增 Inno 临时目录残留。

安全边界（本脚本的零容忍面，注释即契约）：
  - **可写路径只有一类**：`tempfile.gettempdir()` 之下的自建子目录。安装、造探针、
    删目录前都过 `checked_tmp_path`；卸载只调安装目录里的 `unins*.exe`。
  - **安装目标目录双重设防**：必须是临时目录之下，且**显式不等于**
    `%LocalAppData%\\LectureLens`——即使参数传错也伤不到用户的真数据。
  - **被测程序只从已校验的安装目录里按字面量文件名启动**（`LectureLens.exe`，
    不含路径分隔符）：先 `os.chdir` 进该目录再启动，**不是**传 `cwd=` ——
    Windows 的 CreateProcess 解析可执行文件名时看的是调用进程的当前目录，
    传 `cwd=` 会 WinError 2（见 `exe_check` docstring）。`shell=False`，
    参数列表形式；本脚本不接受任何外部 URL。
  - **安装包只从 `dist/installer/` 取**（`checked_installer_path`），不搜全盘。
  - 除标准库外**只多 import 同目录的 `build_exe`**（读它的产物清单/版本四段口径，
    见 REQUIRED_INSTALLED 的注释——两份清单各写一份正是评审②抓到的漂移源）。
    `installed_exe_version` 用系统自带 PowerShell 读版本资源，不引入新依赖。

用法:
    .venv/Scripts/python.exe scripts/verify_installer.py
    .venv/Scripts/python.exe scripts/verify_installer.py --keep     # 失败现场留档
    :: 完整的发布门禁（含跨版本覆盖升级实测）——**优先用真实旧版包**：
    .venv/Scripts/python.exe scripts/verify_installer.py ^
        --upgrade-from dist\\installer\\LectureLens-1.0.0-setup.exe
    :: 没有真旧版（首次发版）时才合成一个（.iss 的 TEST_VERSION 钩子；输出到
    :: upgrade-test\\ 子目录，免得被 find_setup_exe 当成"最新的正式包"误选）：
    "%LocalAppData%\\Programs\\Inno Setup 6\\ISCC.exe" /DTEST_VERSION=0.9.0 ^
        /O"dist\\installer\\upgrade-test" installer\\lecturelens.iss
    .venv/Scripts/python.exe scripts/verify_installer.py ^
        --upgrade-from dist\\installer\\upgrade-test\\LectureLens-0.9.0-setup.exe
（旧版测试包用 TEST_VERSION 钩子生成：0.9.0 + 不压缩，只为省掉 LZMA 时间。）
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = ROOT / "dist" / "installer"
EXE_NAME = "LectureLens.exe"                 # 字面量文件名（启动时用，见文件头安全边界）
GUIDE_NAME = "新手指引.txt"                   # 随包新手指引（D4 要求）
UNINSTALLER_GLOB = "unins*.exe"

#: 构建侧脚本（同目录）：产物清单 / 版本四段口径**单一事实源**（只读它的常量与纯函数）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_exe    # noqa: E402

#: 装完必须具备的关键产物（相对安装目录）。
#: **派生自 build_exe.required_dist_files()，不在这里另抄一份**——评审指出：原先本处
#: 是 4 项独立清单（且只含 _internal/web/index.html），弱于构建侧的 7 项（漏了
#: float.html / privacy.html 两个页面与 B13 术语库包抽查），两份清单迟早漂移；而
#: 「新文件没落地」恰恰是跨版本升级最该抓到的事（1.1.0 的卖点就在这些新文件上）。
#: 新手指引.txt 不在 dist/ 里（由 .iss 从 installer/ 单独挂上去），故单独补一项。
REQUIRED_INSTALLED = (*build_exe.required_dist_files(build_exe.BUNDLE_MODEL), GUIDE_NAME)
#: 数据探针（相对安装目录）：运行期产物，卸载**必须保留**。
#: 覆盖面 = installer/lecturelens.iss 对用户承诺保留的那四类（transcripts / glossary /
#: logs / config.json）。评审指出原先只探 transcripts+config.json 两项，**术语表与日志
#: 无人验**——其中 glossary/ 是用户自己攒的、不可再生，最该验。
#: 笔记探针名**刻意用真实命名法**（main.py 的运行期笔记是 transcripts/lecture_<stamp>.md）：
#: 若探针叫「探针笔记.md」这种真实命名法里不存在的名字，一条
#: `[UninstallDelete] …transcripts\lecture_*.md` 式通配就能删光真实笔记而探针完好
#: ——探针必须站在真实数据的名字空间里才有判别力。
PROBE_NOTE = "transcripts/lecture_20261014_probe.md"
PROBE_CONFIG = "config.json"
PROBE_GLOSSARY = "glossary/探针术语.md"
PROBE_LOG = "logs/lecture_verify_probe.log"
PROBE_MARKER = "lecturelens-installer-verify"
#: 探针相对路径 → 内容模板（`{marker}` → PROBE_MARKER）。写与验共用这一份，不许两处各写。
#: 占位符用 **str.replace 而不是 str.format**：config.json 探针本身就是 JSON，满是大括号，
#: format 会把它们当占位符（实测 KeyError: '\n  "_verify_probe"'）。
PROBES = {
    PROBE_NOTE: "# 探针笔记\n\n这是 verify_installer.py 写入的数据保留探针（{marker}）。\n",
    PROBE_CONFIG: ('{\n  "_verify_probe": "{marker}",\n'
                   '  "model": "small",\n  "first_run_done": true\n}\n'),
    PROBE_GLOSSARY: "# 探针术语表\n\nattention = 注意力（{marker}）\n",
    PROBE_LOG: "probe log line（{marker}）\n",
}
#: 清理断言要查的"本程序相关"镜像名（残留进程检查，见 leftover_processes）
PROCESS_NAMES = (EXE_NAME, "unins000.exe")

#: 卸载等待上限（秒）：unins*.exe 会把自己复制到 temp 再重启干活，
#: 外层进程立刻返回 → 只能轮询"效果"（exe 消失），不能只看退出码。
UNINSTALL_WAIT_S = 180.0
RESULTS: list = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else ""))


def checked_tmp_path(p: Path) -> Path:
    """只接受系统临时目录**之下**的路径（防误删/穿越；本脚本不碰仓库与用户真数据）。"""
    rp = Path(p).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if rp != tmp and tmp in rp.parents:
        return rp
    raise SystemExit(f"[verify] ✗ 拒绝操作临时目录之外的路径: {rp}")


def checked_installer_path(p: Path) -> Path:
    """只接受 dist/installer 之内的安装包路径（不搜全盘、不接受任意路径）。"""
    rp = Path(p).resolve()
    root = INSTALLER_DIR.resolve()
    if rp == root or root in rp.parents:
        return rp
    raise SystemExit(f"[verify] ✗ 只接受 dist/installer 之内的安装包 {root}（收到 {rp}）")


def checked_install_dir(p: Path) -> Path:
    """安装目标目录：必须在临时目录之下，且**必须不是**用户真实的每用户安装位置。

    第二条是防呆的重头——D2 的默认安装位置就是 %LocalAppData%\\LectureLens，
    万一参数写错，这里直接拦下，绝不去动用户的真笔记。
    """
    rp = checked_tmp_path(p)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        real = (Path(local) / "LectureLens").resolve()
        if rp == real or real in rp.parents:
            raise SystemExit(f"[verify] ✗ 拒绝在用户真实安装位置做实验: {rp}")
    return rp


def find_setup_exe(explicit: str | None) -> Path | None:
    """定位安装包：显式给定优先，否则取 dist/installer 下最新一个 *-setup.exe。"""
    if explicit:
        p = checked_installer_path(Path(explicit))
        return p if p.is_file() else None
    if not INSTALLER_DIR.is_dir():
        return None
    cands = sorted(INSTALLER_DIR.glob("LectureLens-*-setup.exe"),
                   key=lambda q: q.stat().st_mtime, reverse=True)
    return checked_installer_path(cands[0]) if cands else None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_quiet(argv: list, timeout: float, cwd: Path | None = None):
    """跑一个 Windows 程序并等它结束。shell=False + 列表参数：
    命令行里不出现拼接出来的可执行路径，也不经 shell（见文件头安全边界）。

    `errors="replace"` 是必需的，不是保险起见：Inno 的 setup/unins 往控制台写的是
    **系统 OEM 代码页**（本机 cp936）而非 UTF-8，`text=True` 的严格解码会在
    subprocess 的 reader 线程里抛 UnicodeDecodeError，线程一死 `r.stdout` 就是空的
    —— --check 的诊断尾巴会被静默吞掉（第一版实测如此）。输出只用于排错，
    宽容解码即可。
    """
    return subprocess.run(argv, capture_output=True, text=True, errors="replace",
                          timeout=timeout, cwd=str(cwd) if cwd else None)


def wait_for(pred, timeout: float, what: str, interval: float = 0.5) -> bool:
    """轮询等待某个"效果"出现（卸载器会重启成子进程，只能看效果不能看退出码）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(interval)
    print(f"    [wait] ⚠ 等待「{what}」超时（{timeout:.0f}s）")
    return False


def find_uninstaller(install_dir: Path) -> Path | None:
    hits = sorted(install_dir.glob(UNINSTALLER_GLOB))
    return hits[0] if hits else None


def inno_temp_dirs() -> set:
    """%TEMP% 下 Inno 的临时解压目录（安装/卸载时创建，正常收尾会自动清掉）。

    只用于**运行前后对比**：并发任务可能也在跑安装包测试，所以只关心「本次新增
    且没被清掉」的那些，而不是"%TEMP% 里一个都不许有"（那会误报别人的残留）。
    """
    tmp = Path(tempfile.gettempdir())
    out = set()
    for pat in ("is-*.tmp", "_iu*.tmp"):
        out |= {p.name for p in tmp.glob(pat)}
    return out


def leftover_processes(names: tuple = PROCESS_NAMES) -> list:
    """按镜像名查残留进程（用 Windows 自带的 tasklist，不引入 psutil 之类新依赖）。

    判定方式刻意避开本地化文案：`/NH /FO CSV` 在**没有匹配**时不会输出带引号的
    镜像名，而有匹配时第一个字段就是 `"名字"`。中英文系统的"没有运行的任务匹配
    指定标准"提示都不含引号镜像名，故用「输出里有没有 'name'」判定跨语言安全。

    `names` 可传（默认 PROCESS_NAMES），便于测试做阳性/阴性对照。
    """
    found = []
    for name in names:
        r = run_quiet(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
                      timeout=60.0)
        if f'"{name}"' in (r.stdout or ""):
            found.append(name)
    return found


def version_from_setup(p: Path) -> str:
    """从 LectureLens-<版本>-setup.exe 里抠出版本号（抠不到就回退整名，便于报错）。"""
    m = re.search(r"-(\d+\.\d+(?:\.\d+)*)-setup\.exe$", p.name)
    return m.group(1) if m else p.name


def silent_install(setup: Path, install_dir: Path, log: Path) -> int:
    """按 D4 口径静默安装到指定目录；/LOG 留档便于失败时定位。"""
    argv = [str(setup),
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            f"/DIR={install_dir}",
            f"/LOG={log}"]
    r = run_quiet(argv, timeout=1800.0)
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").splitlines()[-15:])
        print(f"    [install] 退出码 {r.returncode}\n{tail}")
    return r.returncode


def silent_uninstall(install_dir: Path) -> int:
    """静默卸载。返回外层进程退出码（效果另看 wait_for）。"""
    un = find_uninstaller(install_dir)
    if un is None:
        return -1
    r = run_quiet([str(un), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                  timeout=600.0, cwd=install_dir)
    return r.returncode


def exe_check(install_dir: Path, timeout: float = 420.0):
    """跑装好的 LectureLens.exe --check（桌面壳 headless 自检）。

    启动方式与 scripts/verify_dist.py 同款：**先 os.chdir 进已校验的安装目录，
    再以字面量文件名启动**。

    为什么不能传 `cwd=` 了事：Windows 的 CreateProcess 在解析**可执行文件名**
    时用的是「调用进程」的当前目录，而不是 lpCurrentDirectory——传 cwd= 会直接
    WinError 2 找不到文件（本脚本第一版就是这么挂的，实测已复现）。
    chdir 到已过 checked_install_dir 的目录，既让裸文件名能解析，又保持
    "命令行里不出现拼接出来的可执行路径"这条口径。
    """
    prev = Path.cwd()
    try:
        os.chdir(install_dir)
        return run_quiet([EXE_NAME, "--check"], timeout=timeout)
    finally:
        os.chdir(prev)


def write_probes(install_dir: Path) -> dict:
    """在安装目录造数据探针，返回 {相对路径: 写入内容}（供 probes_intact 逐字节复核）。

    覆盖面见 PROBES 的注释（= .iss 承诺保留的那四类）；返回内容而不是让调用方各自
    再读一遍，是为了「写的」和「验的」永远是同一份字符串。
    """
    written = {}
    for rel, tpl in PROBES.items():
        p = install_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        text = tpl.replace("{marker}", PROBE_MARKER)
        p.write_text(text, encoding="utf-8")
        written[rel] = text
    return written


def probes_intact(install_dir: Path, want: dict) -> tuple:
    """探针是否原样（存在 + 内容逐字节一致）。返回 (ok, 说明)。

    `want` = write_probes() 的返回值（多探针；见 PROBES）。"""
    missing = [rel for rel in want if not (install_dir / rel).is_file()]
    if missing:
        return False, f"探针丢失: {missing}"
    for rel, text in want.items():
        if (install_dir / rel).read_text(encoding="utf-8") != text:
            return False, f"{rel} 内容被改动"
    return True, ""


def missing_installed(install_dir: Path) -> list:
    """REQUIRED_INSTALLED 里缺哪些（相对安装目录的路径列表）。"""
    return [rel for rel in REQUIRED_INSTALLED if not (install_dir / rel).is_file()]


def installed_exe_version(install_dir: Path) -> str:
    """读**安装目录里** exe 的 FileVersion（PowerShell VersionInfo，与资源管理器同源）。

    为什么必须有这一步（评审②）：跨版本升级那一步原先只断言「exe 在位 + 探针还在 +
    --check 退 0」——若新版根本没覆盖上去（例如 1.1.0 的新文件没落地），那一轮照样
    28/28 全绿。要证明「新版真装上了」，只能把装好的二进制的版本读回来看。
    """
    quoted = str(install_dir / EXE_NAME).replace("'", "''")
    r = run_quiet(["powershell", "-NoProfile", "-Command",
                   f"(Get-Item -LiteralPath '{quoted}').VersionInfo.FileVersion"],
                  timeout=120.0)
    return (r.stdout or "").strip()


def version_matches(installed: str, expected: str) -> bool:
    """版本号比较走 build_exe.version_quad（四段补零）：'1.1.0.0' == '1.1.0'。

    两边都经同一个解析器，避免这边自己写一套「三段/四段」的比较逻辑。
    解析失败（version_quad 会 SystemExit）一律判不等，不猜。
    """
    try:
        return build_exe.version_quad(installed) == build_exe.version_quad(expected)
    except SystemExit:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="LectureLens 安装包实测（装→用→卸→覆盖升级，全程在 %TEMP%）")
    ap.add_argument("--setup-exe", default=None,
                    help="指定安装包（默认取 dist/installer 下最新的 *-setup.exe）")
    ap.add_argument("--upgrade-from", default=None,
                    help="**旧版**安装包路径：先装它、造探针，再把目标版装上去——"
                         "实测真正的「跨版本覆盖升级」（评审要求）。不给这项时第 ⑦ 步"
                         "退化为同版本原地重装，脚本会**如实标注跨版本路径未实测**")
    ap.add_argument("--keep", action="store_true",
                    help="跑完保留临时目录（失败现场留档用；默认清理）")
    ap.add_argument("--skip-uninstall-wait", action="store_true",
                    help="不轮询等卸载完成（调试用；正式验收不要加）")
    args = ap.parse_args()

    print("[verify-installer] LectureLens 安装包实测\n")

    # —— ① 安装包体检 ——————————————————————————————————————————
    setup = find_setup_exe(args.setup_exe)
    if setup is None or not setup.is_file():
        print(f"[verify] ✗ 找不到安装包（先跑："
              f'"{os.environ.get("LOCALAPPDATA")}\\Programs\\Inno Setup 6\\ISCC.exe" '
              f"installer\\lecturelens.iss）")
        return 1
    size = setup.stat().st_size
    rec("安装包存在且非空", size > 0, f"{setup.name}（{size / 2**30:.2f} GiB）")
    m = re.search(r"-(\d+\.\d+\.\d+)-setup\.exe$", setup.name)
    rec("安装包文件名含版本号", bool(m), m.group(1) if m else setup.name)

    # .sha256 校验：**安装包与其校验文件必须成对**，且内容对得上（D6 口径）
    sha_path = setup.with_name(setup.name + ".sha256")
    if not sha_path.is_file():
        rec("SHA256 校验文件存在", False, f"缺 {sha_path.name}")
    else:
        rec("SHA256 校验文件存在", True, sha_path.name)
        want = (sha_path.read_text(encoding="utf-8").split() or [""])[0].lower()
        got = sha256_file(setup).lower()
        rec("SHA256 内容与安装包一致", want == got,
            f"{got[:16]}…" if want == got else f"文件写的是 {want[:16]}…，实测 {got[:16]}…")

    # —— 旧版安装包（跨版本升级实测用）：路径同样只认 dist/installer 之内 ————
    upgrade_from = None
    if args.upgrade_from:
        upgrade_from = checked_installer_path(Path(args.upgrade_from))
        if not upgrade_from.is_file():
            print(f"[verify] ✗ --upgrade-from 指向的旧版安装包不存在: {upgrade_from}")
            return 1
        rec("旧版安装包就位", True,
            f"{upgrade_from.name}（{version_from_setup(upgrade_from)}）")

    # —— 临时工作区（自建子目录，进去前先过闸） ————————————————
    work = checked_tmp_path(Path(tempfile.mkdtemp(prefix="ll-installer-verify-")))
    install_dir = checked_install_dir(work / "installdir")
    log = checked_tmp_path(work / "install.log")
    # Inno 临时目录快照：清理断言只比"本次新增"，不替并发任务背锅
    tmp_before = inno_temp_dirs()
    print(f"\n[verify-installer] 工作区: {work}")

    rc_all = 0
    try:
        # —— ② 静默安装到临时目录 ————————————————————————————————
        print("\n② 静默安装到临时目录")
        rc = silent_install(setup, install_dir, log)
        rec("静默安装退出码 0", rc == 0, f"退出码 {rc}")
        if rc != 0 or not (install_dir / EXE_NAME).is_file():
            rec("装出 LectureLens.exe", False, f"未找到 {install_dir / EXE_NAME}")
            return 1
        rec("装出 LectureLens.exe", True, EXE_NAME)
        missing = missing_installed(install_dir)
        rec("关键产物齐全", not missing,
            f"{len(REQUIRED_INSTALLED) - len(missing)}/{len(REQUIRED_INSTALLED)}"
            + (f"，缺 {missing}" if missing else ""))
        # 装出来的必须是**目标版本**（评审②）：清单齐了但版本是旧的，同样是错的包。
        got_v = installed_exe_version(install_dir)
        rec(f"装出的 exe 版本 == {version_from_setup(setup)}",
            version_matches(got_v, version_from_setup(setup)), f"读到 {got_v!r}")

        # —— ③ 装好的程序能过 --check ————————————————————————————
        print("\n③ 装好的程序自检（LectureLens.exe --check）")
        r = exe_check(install_dir)
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-1:] or [""]
        rec("--check 退出码 0", r.returncode == 0, f"退出码 {r.returncode}｜{tail[0][:90]}")

        # —— ④ 造数据探针 ————————————————————————————————————————
        print("\n④ 在安装目录造数据探针")
        want_probes = write_probes(install_dir)
        rec("探针已写入", True, " + ".join(PROBES))

        # —— ⑤ 静默卸载：程序文件删掉，数据留下 ————————————————————
        print("\n⑤ 静默卸载（断言：exe 已删 / 探针仍在）")
        un = find_uninstaller(install_dir)
        if un is None:
            rec("找到卸载器", False, f"{install_dir} 下无 {UNINSTALLER_GLOB}")
            return 1
        rec("找到卸载器", True, un.name)
        urc = silent_uninstall(install_dir)
        if not args.skip_uninstall_wait:
            wait_for(lambda: not (install_dir / EXE_NAME).is_file(),
                     UNINSTALL_WAIT_S, "安装的 exe 被删除")
        rec("卸载后 exe 已删除", not (install_dir / EXE_NAME).is_file(),
            f"卸载器退出码 {urc}")
        ok, why = probes_intact(install_dir, want_probes)
        rec("卸载后探针仍在（数据保留）", ok, why or "四类探针（笔记/术语表/日志/配置）原样")
        # 程序文件目录也该清掉（否则是卸载不全，不是"保留数据"）
        leftover_internal = (install_dir / "_internal").exists()
        rec("程序文件目录已清理", not leftover_internal,
            "" if not leftover_internal else "_internal/ 仍在")

        # —— ⑥ 重新静默安装（覆盖升级） ————————————————————————————
        print("\n⑥ 重新静默安装（覆盖升级）")
        rc2 = silent_install(setup, install_dir, log)
        rec("重装退出码 0", rc2 == 0, f"退出码 {rc2}")
        rec("重装后 exe 回来", (install_dir / EXE_NAME).is_file())
        ok, why = probes_intact(install_dir, want_probes)
        rec("重装后探针仍在（覆盖不丢数据）", ok, why or "四类探针原样")
        try:
            r2 = exe_check(install_dir)
            rec("重装后 --check 仍退 0", r2.returncode == 0, f"退出码 {r2.returncode}")
        except subprocess.TimeoutExpired:
            rec("重装后 --check 仍退 0", False, "--check 超时")

        # —— ⑦ 真实覆盖升级（**跨版本**） ————————————————————————————
        # ⑥ 是"卸了再装"（同版本）；⑦ 是**不卸直接装**——朋友拿到新版双击即升级，
        # 是最容易丢数据的路径。评审指出：原来第 ⑦ 步拿**同一个**安装包原地再装一遍，
        # 那是"同版本重装"，而分发指南却把它写成"换新版"的实测口径——于是这里改成
        # 必须用**两个不同版本**：--upgrade-from 给旧版 → 装旧版 → 在旧版下造探针 →
        # 装目标版**覆盖**它。没给 --upgrade-from 就如实**判失败**（不再默默放过），
        # 免得"覆盖升级已实测"这句话又变成没测也敢说。
        if upgrade_from is not None:
            old_v, new_v = version_from_setup(upgrade_from), version_from_setup(setup)
            print(f"\n⑦ 跨版本覆盖升级（{old_v} → {new_v}，不卸载直接装）")
            # 先从"干净"起步：把当前装的卸掉，再装旧版
            silent_uninstall(install_dir)
            if not args.skip_uninstall_wait:
                wait_for(lambda: find_uninstaller(install_dir) is None,
                         UNINSTALL_WAIT_S, "旧安装归还")
            rc_old = silent_install(upgrade_from, install_dir, log)
            rec(f"旧版 {old_v} 安装退 0", rc_old == 0, f"退出码 {rc_old}")
            rec(f"旧版 {old_v} 装出 exe", (install_dir / EXE_NAME).is_file())
            # 旧版自身也读一次版本：确认「升级起点」真的是旧版（而不是上一步残留）
            old_got = installed_exe_version(install_dir)
            rec(f"旧版 {old_v} 装出的确实不是新版",
                not version_matches(old_got, new_v), f"读到 {old_got!r}")
            # **在旧版下**造探针（这才是"升级前的用户数据"）
            up_probes = write_probes(install_dir)
            rc_new = silent_install(setup, install_dir, log)
            rec(f"新版 {new_v} 覆盖安装退 0", rc_new == 0, f"退出码 {rc_new}")
            rec("覆盖后 exe 在位", (install_dir / EXE_NAME).is_file())
            # —— 评审②要求的两条「新版真装上了」的证据 ——
            # ① 装好的二进制版本 == 新版号（只断言 exe 在位的话，新文件没落地也照样绿）
            new_got = installed_exe_version(install_dir)
            rec(f"覆盖后 exe 版本 == {new_v}", version_matches(new_got, new_v),
                f"读到 {new_got!r}")
            # ② ② 的关键产物清单**在覆盖后重跑一遍**——1.1.0 的卖点正是随包新文件
            #    （_internal/glossary_packs/*.md、float.html、privacy.html），
            #    它们没落地是本步最该抓到的事。清单与构建侧同源（见 REQUIRED_INSTALLED）。
            up_missing = missing_installed(install_dir)
            rec("覆盖后关键产物齐全（含 float/privacy/术语库包）", not up_missing,
                f"{len(REQUIRED_INSTALLED) - len(up_missing)}/{len(REQUIRED_INSTALLED)}"
                + (f"，缺 {up_missing}" if up_missing else ""))
            ok, why = probes_intact(install_dir, up_probes)
            rec(f"跨版本升级（{old_v}→{new_v}）后探针仍在", ok,
                why or "旧版下造的四类探针原样")
            try:
                r3 = exe_check(install_dir)
                rec("跨版本升级后 --check 退 0", r3.returncode == 0, f"退出码 {r3.returncode}")
            except subprocess.TimeoutExpired:
                rec("跨版本升级后 --check 退 0", False, "--check 超时")
        else:
            print("\n⑦ 覆盖安装（同版本原地重装）")
            rc3 = silent_install(setup, install_dir, log)
            rec("覆盖安装退出码 0", rc3 == 0, f"退出码 {rc3}")
            ok, why = probes_intact(install_dir, want_probes)
            rec("覆盖安装后探针仍在", ok, why or "四类探针原样")
            try:
                r3 = exe_check(install_dir)
                rec("覆盖安装后 --check 退 0", r3.returncode == 0, f"退出码 {r3.returncode}")
            except subprocess.TimeoutExpired:
                rec("覆盖安装后 --check 退 0", False, "--check 超时")
            rec("跨版本覆盖升级已实测", False,
                "未传 --upgrade-from → 本次只验了**同版本**重装，跨版本路径未实测；"
                "请按 docs/distribution-guide.md §二 生成旧版测试包后带 --upgrade-from 重跑")

    finally:
        # —— 清理：先卸载，再删净临时目录。**这几条也是断言，不是日志** ————————
        # 评审指出：原先这段一个 rec() 都没有，rmtree(ignore_errors=True) 失败只 print
        # 一行 ⚠、退出码与「全部通过」都不受影响；也完全没有进程残留检查。于是
        # docstring 第 7 条「清理…不留残留」与指南第 ⑥ 步都成了空头承诺。
        # 现在四条都进 RESULTS，任一条不干净都会让整轮判失败。
        print("\n[verify-installer] 清理")
        try:
            if find_uninstaller(install_dir) is not None:
                silent_uninstall(install_dir)
                if not args.skip_uninstall_wait:
                    wait_for(lambda: find_uninstaller(install_dir) is None,
                             UNINSTALL_WAIT_S, "卸载器归还")
            rec("清理：卸载器已退出", find_uninstaller(install_dir) is None)
        except Exception as e:  # 清理失败不掩盖主结论，但要说出来
            rec("清理：卸载器已退出", False, f"卸载时异常: {e}")

        if args.keep:
            print(f"    [cleanup] --keep：保留 {work}（故不断言目录已删）")
        else:
            shutil.rmtree(work, ignore_errors=True)
            rec("清理：临时目录已删净", not work.exists(),
                "" if not work.exists() else f"{work} 仍有残留（rmtree 失败）")

        procs = leftover_processes()
        rec("清理：无残留进程", not procs,
            "" if not procs else f"仍在运行: {procs}")

        # Inno 的卸载器会把自己复制到 %TEMP%\is-<id>-uninstall.tmp\ 下再运行，收尾时
        # **不保证**删掉那个目录——实测 /VERYSILENT 卸载后确实会留下一个（本断言第一
        # 次跑就是被它判红的：残留 ['is-5F4T7NM75W-uninstall.tmp']）。实测还确认了
        # **不要指望它自清**：等 15 秒它也不会动，所以这里不等，直接由本脚本收干净
        # （把断言放宽成"允许残留"是错的，那等于把评测口径改松）。
        # 只收「本次新增」且「仍在系统临时目录之下」的：tmp_before 差集 +
        # checked_tmp_path 双重限定，并发任务或系统原有的目录一律不碰。
        time.sleep(1.0)              # 让卸载器把手里的句柄放掉（随后的 wait_for 已等它退出）
        for name in sorted(inno_temp_dirs() - tmp_before):
            try:
                shutil.rmtree(checked_tmp_path(Path(tempfile.gettempdir()) / name),
                              ignore_errors=True)
            except SystemExit:      # 闸门拦下（理论上不会）→ 宁可留下也不越界删
                pass
        still = sorted(inno_temp_dirs() - tmp_before)
        rec("清理：无新增 Inno 临时目录残留", not still,
            "" if not still else f"残留 {still}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 68)
    if failed:
        print(f"[verify-installer] ✗ 失败 {len(failed)}/{len(RESULTS)} 项：")
        for n in failed:
            print(f"    · {n}")
        return 1
    print(f"[verify-installer] ✅ 全部 {len(RESULTS)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
