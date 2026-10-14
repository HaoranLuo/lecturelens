# -*- coding: utf-8 -*-
"""exportdir.py — §3 导出位置自选 + 导出文件名规范（纯函数 + 极薄 IO 辅助）。

职责（只做「文件落到哪、叫什么」，不掺 HTTP 与配置读写——那在 main.py）：
- path_problem：config.export_dir 取值的**纯语法**校验（绝对路径 / 长度 / 非法字符 /
  `..` 段 / 系统关键目录），**不碰盘**；
- ensure_writable：落地可用性的**实证**校验（建目录 + 写探针），是唯一会产生
  文件系统副作用的一步；
- clean_component / export_filename：`{课程}_{日期}.{ext}` 与文件名 sanitize；
- write_export：原子落盘 + 同名避让（`-2/-3…`，绝不静默覆盖）；
- open_folder：os.startfile 打开目录（仅 Windows；「仅本机」放行由调用方负责）。

两条纪律（与 notes/courses 同款）：
① 校验与副作用**分开**——`/api/config` 在任何状态改动之前跑完校验，`path_problem`
   绝不建目录，建目录只发生在「整体校验已通过、马上要落盘」的那一刻；
② 拒绝而不是猜测——非法值一律给**点名文案**（`非法 export_dir: …`），不静默回退。
"""
import contextlib
import datetime
import contextlib
import datetime
import os
import re
import tempfile
from pathlib import Path

from courses import UNCATEGORIZED
from notes import INVISIBLE_CHARS

#: export_dir 字符串长度上限。**算式要按最坏情况**（评审④）：落盘最坏 =
#: 目录 + "\" + 笔记原名(notes.NOTE_NAME_MAX=100，其中留 97 给 stem) + "." + 扩展名(4)
#: ≈ 目录 + 102；临时文件名比它更长（"." + 名 + 随机 8 + ".tmp" ≈ +14）→ 共 +117；
#: 再加同名避让后缀（-999，+4）。128 + 117 + 4 = 249 ≤ 260 ✓（旧值 160 会算到 281，
#: 命中时只会以裸 OSError 收场）。
EXPORT_DIR_MAX = 128
#: 整条路径的安全上限（Windows 经典 260 留余量）；由 path_length_problem 用。
MAX_FULL_PATH = 250
#: 临时文件比最终名多占的字符数（mkstemp 的 "." + 随机段 + ".tmp"）
_TMP_EXTRA = 14
#: 文件名里课程名组件的长度上限
COMPONENT_MAX = 60
#: 文件名一律禁止的字符（与 notes.BAD_NAME_CHARS 同口径）
BAD_FILE_CHARS = set('\\/:*?"<>|')
#: 上者的展示形态（错误文案给人看，别拿 set 拼字符串——顺序还不稳定）
BAD_FILE_SHOW = '\\ / : * ? " < > |'
#: Windows 保留设备名（不区分大小写；扩展名前的部分也算）
RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})
#: 日期 token 形态 = YYYYMMDD（与 lecture_YYYYMMDD_HHMM.md 的文件名口径一致）
DATE_RE = re.compile(r"^\d{4}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
#: 写探针文件名的前缀（**随机段由 mkstemp 生成**：固定名会在用户目录里恰有同名文件时
#: 把它覆盖后删掉——评审④，破坏性）
PROBE_PREFIX = ".lecturelens-write-probe-"
#: 合法首段形态：盘符（`D:\`）；POSIX 根（开发/测试环境）单独判
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]$")
#: UNC 形态首段（`\\host\share\`）——能识别出来，但**本项目一律拒绝**（见 path_problem）
_UNC_RE = re.compile(r"^\\\\[^\\/]+\\[^\\/]+[\\/]?$")
#: Windows 扩展/设备路径前缀（`\\?\`、`\\.\`）——它们绕过 Win32 路径规范化
_EXT_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")


def _strip_extended(p: Path) -> Path:
    """剥掉扩展路径前缀 → 普通路径（剥不动就原样返回）。

    `Path.resolve()` 对超长路径会自动加 `\\?\\`，而**带该前缀的路径与普通路径不相等**：
    `Path('\\\\?\\C:\\Windows\\Temp').parents` 里没有 `C:\\Windows`（本轮实测），于是
    「禁用目录」比对会静默失效——比黑名单的字符级绕过更隐蔽（那条已在 path_problem 拦）。"""
    s = str(p)
    for pre in ("\\\\?\\UNC\\", "//?/UNC/"):
        if s.startswith(pre):
            return Path("\\\\" + s[len(pre):])
    for pre in _EXT_PREFIXES:
        if s.startswith(pre):
            return Path(s[len(pre):])
    return p


def course_is_empty(course) -> bool:
    """「课程为空」的判据（§3：课程为空则维持原名）。

    两种情况都算空：① 笔记头没有课程行（p2-1 之前的老笔记，解析为 None/空白）；
    ② 归属是「未分类」——未选课就是没有课程归属（notes-batch §0 的既有口径：
    「未选课 = 未分类」）。用 UNCATEGORIZED 字面量当文件名只会把同一天的多篇
    未分类笔记挤成同一个名字（还得靠 -2/-3 后缀区分），反而不如原名里带时分。"""
    if not isinstance(course, str):
        return True
    c = course.strip()
    return (not c) or c == UNCATEGORIZED


def clean_component(raw: str | None) -> str:
    """把一个名字组件（课程名）清洗成 Windows 文件名可用的形态；不可救 → 空串。

    规则：禁 `\\ / : * ? " < > |`、控制字符与**不可见字符**（零宽族 + 行/段分隔符，
    与标签侧共用 notes.INVISIBLE_CHARS 一张表；替换成 `-`，不静默丢弃信息）→ 连续
    `-` 压成一个 → 去掉首尾的 `-`/空格/点（Windows 会**静默裁掉**尾部的点与空格，
    留着就会让「界面显示的名字」与「盘上的名字」对不上）→ 截断到 COMPONENT_MAX
    → 空或系统保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）加前缀 `_`（`con.pdf` 在
    Windows 上根本建不出来）。"""
    if not isinstance(raw, str):
        return ""
    out: list[str] = []
    for ch in raw.strip():
        if (ch in BAD_FILE_CHARS or ch in INVISIBLE_CHARS
                or ord(ch) < 32 or ord(ch) == 127):
            out.append("-")
        else:
            out.append(ch)
    s = re.sub(r"-{2,}", "-", "".join(out)).strip(" -.")
    s = s[:COMPONENT_MAX].strip(" -.")
    if not s:
        return ""
    if s.split(".", 1)[0].lower() in RESERVED_STEMS:
        s = "_" + s
    return s


def date_token(p: Path, stamp: str | None = None,
               today: datetime.date | None = None) -> str:
    """导出文件名里的 {日期} → `YYYYMMDD`（来源优先级与「这节课哪天上的」对齐）。

    ① 文件名里的时间戳（`lecture_YYYYMMDD_HHMM.md`，最可靠）；② 笔记头的时间戳
    （改名后的笔记：文件名没了时间戳、头里还在）；③ 文件 mtime（头也被手改掉）；
    ④ 今天（兜底——宁可日期不准，也不让文件名叫不出来）。"""
    m = re.match(r"^lecture_(\d{8})_\d{4}", Path(p).name)
    if m:
        return m.group(1)
    if isinstance(stamp, str):
        s = stamp.strip()
        if len(s) >= 8 and s[:8].isdigit():
            return s[:8]
    try:
        mtime = Path(p).stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y%m%d")
    except Exception:
        return (today or datetime.date.today()).strftime("%Y%m%d")


def export_filename(p: Path, course, ext: str, stamp: str | None = None,
                    today: datetime.date | None = None) -> str:
    """§3 导出文件名 = `{课程}_{日期}.{ext}`；课程为空 → 维持原名（只换扩展名）。

    纯函数（只读 p 的 stat 拿 mtime，不写任何东西）。清洗后的课程名若变成空串
    （如课程名整条都是非法字符），同样回落原名——宁可叫不出花名，也不能生成
    一个只有日期、看不出是哪门课的文件名。`today` 仅用于测试注入日期兜底。"""
    e = str(ext or "").strip().lstrip(".").lower() or "md"
    comp = clean_component(course) if not course_is_empty(course) else ""
    stem = f"{comp}_{date_token(p, stamp, today)}" if comp else p.stem
    return f"{stem}.{e}"


def _forbidden_dirs() -> tuple[list[Path], list[Path]]:
    """系统关键目录 → (只禁**本身**, 连**子目录**一起禁)。

    取环境变量/卷根而非硬编码盘符——本机装在 D 盘、朋友机器在 C 盘都能正确判定。

    两级是必需的：系统盘根（`C:\\`）只禁根本身——把「根的所有子目录也禁掉」写成
    父链判定，会让 C 盘上的**任何**目录（含 %TEMP%、桌面、下载）全被判非法，实测
    踩过。而 `C:\\Windows` / `Program Files` / `ProgramData` 这类是真不该往里写
    文件的地方，连子目录一起禁。

    **不含** USERPROFILE（`C:\\Users\\<user>`）：桌面/文档/下载都长在它下面，
    禁掉等于把最常用的导出位置全判成非法。"""
    subtree: list[Path] = []
    exact: list[Path] = []
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramData"):
        val = os.environ.get(var)
        if val and val.strip():
            subtree.append(Path(val.strip()))
    drive = os.environ.get("SystemDrive")
    if drive and drive.strip():
        exact.append(Path(drive.strip().rstrip("\\/") + os.sep))
    if os.name != "nt":                    # 开发/测试环境（POSIX）
        exact.append(Path(os.sep))
        subtree += [Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/boot",
                                      "/sys", "/proc", "/dev")]
    return exact, subtree


def path_problem(raw) -> str | None:
    """export_dir 取值的**纯语法**校验（不建目录、不写文件）→ 错误文案；None = 通过。

    规则：非空字符串；长度 ≤ EXPORT_DIR_MAX；**本机绝对路径**（`D:\\导出`；POSIX 上
    `/…`）；不含控制字符与 `\\ / : * ? " < > |`（盘符后的冒号是路径语法的一部分，
    按段判定）；不含 `..` 段；首段必须是盘符（拒绝 `\\\\?\\` / `\\\\.\\` 扩展前缀与
    `\\\\主机\\共享` UNC）；不得落在系统关键目录里（根只禁本身、系统目录连子目录一起禁）。

    两条**评审实证过**的硬要求：
    ① `\\\\?\\C:\\Windows\\Temp` 这种扩展前缀早期能整条绕过禁用目录黑名单（首段被
       「盘符/UNC」判定跳过、`?` 恰在被跳过的那一段，且 `resolve()` 对它原样返回——
       `parents` 里没有 `C:\\Windows`），而 Windows 照常做真实文件访问、写盘真的成功。
       现在：显式拒绝这类前缀，且对 `resolve()` 的结果也剥一次前缀（超长路径会自动带上它）。
    ② UNC（`\\\\主机\\共享`）：校验阶段就会向该主机发起一次**带当前用户凭据**的 SMB
       连接（实测错误文案为 `WinError 53 找不到网络路径`——即已经探测过），而
       `/api/config` 无 Origin/CSRF 校验、环回对端又被豁免，恶意页面可借此把导出位置
       改成攻击者共享，此后每次导出（含 §5 自动归档）都会把笔记写到那里。故**一律拒绝**：
       要用网络位置请先映射成盘符（`Z:\\导出`），本应用只写本机盘符。"""
    if not isinstance(raw, str):
        return f"非法 export_dir: {raw!r}（须为绝对路径字符串，或 null 取消）"
    s = raw.strip()
    if not s:
        return "export_dir 不能为空字符串（取消自选请传 null）"
    if len(s) > EXPORT_DIR_MAX:
        return f"export_dir 太长（上限 {EXPORT_DIR_MAX} 字符）: {s[:40]}…"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return f"export_dir 含控制字符: {s!r}"
    for pre in _EXT_PREFIXES:
        if s.startswith(pre):
            return (f"非法 export_dir: {s}（不支持 Windows 扩展/设备路径前缀 "
                    f"{pre!r}——它会绕过系统目录与保留名检查；请写普通绝对路径，"
                    f"如 D:\\LectureLens-导出）")
    p = Path(s)
    if not p.is_absolute():
        return f"非法 export_dir: {s}（须为绝对路径，如 D:\\LectureLens-导出）"
    parts = p.parts
    first = parts[0] if parts else ""
    if _DRIVE_RE.match(first) or first == "/":
        body = parts[1:]
    elif _UNC_RE.match(first) or s.startswith(("\\\\", "//")):
        return (f"非法 export_dir: {s}（不支持 UNC 网络共享路径——本应用只写本机盘符；"
                f"要用网络位置请先映射成盘符，如 Z:\\导出）")
    else:
        return (f"非法 export_dir: {s}（须形如 D:\\导出 的本地绝对路径，"
                f"不接受 \\\\?\\ 前缀或 \\\\主机\\共享）")
    bad = [seg for seg in body
           if seg in ("..", ".") or any(ch in BAD_FILE_CHARS for ch in seg)]
    if bad:
        return (f"非法 export_dir: {s}（路径段不得含 {BAD_FILE_SHOW} 中的字符，"
                f"也不得含 . / ..）: {bad[0]!r}")
    try:
        cand = _strip_extended(p.resolve())
    except Exception as e:                 # resolve 失败（如超长/权限）也不放过
        return f"非法 export_dir: {s}（无法解析路径 {type(e).__name__}: {e}）"
    exact, subtree = _forbidden_dirs()
    forb = []
    for d in exact:
        with contextlib.suppress(Exception):
            forb.append((_strip_extended(d.resolve()), False))
    for d in subtree:
        with contextlib.suppress(Exception):
            forb.append((_strip_extended(d.resolve()), True))
    for dr, with_children in forb:
        if cand == dr or (with_children and dr in cand.parents):
            return f"不能把导出位置设在系统关键目录: {s}（{dr}）"
    return None


def ensure_writable(dir_path, mkdir: bool = True) -> str | None:
    """建目录 + 写探针 → 错误文案；None = 目录确实存在且可写。

    为什么不用 `os.access(W_OK)`：Windows 上它对目录基本恒真（ACL 不参与判定），
    「可写」只能**实测**——写一个探针文件再删掉。

    探针走 `mkstemp`（**随机名 + 独占创建**），不再用固定名：固定名「写前不检查存在」
    会把用户目录里恰好同名的文件覆盖后删掉（评审④ 破坏性；比如上一次异常残留被用户
    改名归档）；mkstemp 既不碰别人的文件，又照样证明「能在这里新建文件」= 可写。
    `mkdir=False` 用于「只验证已存在的目录」（打开文件夹前的复检不建目录）。"""
    d = Path(dir_path)
    if mkdir:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"导出目录无法创建: {d}（{type(e).__name__}: {e}）"
    if not d.is_dir():
        return f"导出目录不存在或不是文件夹: {d}"
    probe: Path | None = None
    fd = -1
    err: str | None = None
    try:
        fd, name = tempfile.mkstemp(dir=str(d), prefix=PROBE_PREFIX, suffix=".tmp")
        probe = Path(name)
        os.write(fd, b"lecturelens")
    except Exception as e:
        err = f"导出目录不可写: {d}（{type(e).__name__}: {e}）"
    finally:
        # 探针的句柄与文件**两条路径都要收干净**：Windows 上「文件还开着」时 unlink
        # 会 PermissionError，于是失败路径会把一个空的探针文件留在用户目录里
        # （自测实测踩到：打桩 os.write 失败 → 残留 .lecturelens-write-probe-*.tmp）。
        if fd >= 0:
            with contextlib.suppress(Exception):
                os.close(fd)
        if probe is not None:
            with contextlib.suppress(Exception):
                probe.unlink()
    return err


def dir_problem(raw) -> str | None:
    """§3 专项校验（与 input_device / hotkey 同款纪律）= 语法校验 + 落地实证。"""
    problem = path_problem(raw)
    if problem:
        return problem
    return ensure_writable(raw)


def path_length_problem(dir_path, filename: str) -> str | None:
    """最终落盘路径（含临时名余量）是否超 Windows 经典 260 → 明确文案；None = 没问题。

    为什么要在写之前算：超限时 `open()`/`replace()` 抛的是裸 OSError（连错误码都
    不好认），而这里能给出**可行动**的话（换个更短的导出目录 / 重命名这篇笔记）。
    算式含临时文件名（比最终名长 `_TMP_EXTRA` 个字符）与同名避让后缀的余量。"""
    d = Path(dir_path)
    need = len(str(d)) + 1 + len(filename) + _TMP_EXTRA
    if need > MAX_FULL_PATH:
        return (f"导出路径太长（约 {need} 字符 > 上限 {MAX_FULL_PATH}）："
                f"{d}\\{filename}——请换一个更短的导出目录，或重命名这篇笔记")
    return None


def _candidate_name(filename: str, n: int) -> str:
    """第 n 个候选文件名（1 = 原名；2 起把 `-2/-3…` 插在扩展名之前）。"""
    if n <= 1:
        return filename
    stem, dot, ext = filename.rpartition(".")
    if not dot:                            # 没有扩展名的极端情况：后缀加在末尾
        return f"{filename}-{n}"
    return f"{stem}-{n}.{ext}"


def dedupe_names(name: str, used: set) -> str:
    """把 name 登记进 used 并返回**不重名**的版本（重名追加 `-2/-3…`）→ A3 打包用。

    为什么需要：打包 zip 时多个笔记可能算出同一个条目名——同一门课同一天的
    两节课都会得到 `{课程}_{日期}.md`。zip 里出现两个同名条目，解压时是"后者盖
    前者"（各家解压器行为还不一致），用户会静默少文件。规则与落盘避让共用
    `_candidate_name`，两种模式不会打架。"""
    if name not in used:
        used.add(name)
        return name
    for n in range(2, 1000):
        cand = _candidate_name(name, n)
        if cand not in used:
            used.add(cand)
            return cand
    raise OSError(f"同名文件过多: {name}")


def unique_path(dir_path, filename: str) -> Path:
    """（**只算名、不占位**）目标目录下第一个不存在的落点；落盘请用 reserve_path。

    保留它是因为「展示用/纯逻辑单测」需要不带副作用的候选名计算；**落盘路径不能
    用它**——「先查后写」在并发下两个调用会拿到同一个名字（评审② 实测：连点两下/
    两个标签页/桌面与手机同屏同时导出，会写同一个临时文件、产出两份内容的混合体，
    PDF/SRT 就是坏文件）。"""
    d = Path(dir_path)
    for n in range(1, 1000):
        cand = d / _candidate_name(filename, n)
        if not cand.exists():
            return cand
    raise OSError(f"同名文件过多: {filename}")


def reserve_path(dir_path, filename: str) -> Path:
    """**原子抢占**一个不覆盖任何现有文件的落点（O_CREAT|O_EXCL）→ 空文件占位路径。

    「查」与「占」是一步：并发导出不会拿到同一个落点（OS 级互斥，跨进程也成立——
    两个安装实例指向同一导出目录也不会互踩）。抢占失败就依次试 `-2/-3…`，与
    unique_path 同一套后缀规则。"""
    d = Path(dir_path)
    for n in range(1, 1000):
        cand = d / _candidate_name(filename, n)
        try:
            fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return cand
    raise OSError(f"同名文件过多: {filename}")


def write_export(dir_path, filename: str, body: bytes) -> Path:
    """把导出产物写进导出目录 → 真实落点（同名避让 + 并发安全，见 reserve_path）。

    写盘：先原子抢占目标名（得到空占位文件），再在**本次独占的** mkstemp 临时文件里
    写正文，最后 `os.replace` 覆盖掉自己那个占位——替换是原子的，断电/崩溃只会留下
    空占位或完整文件，不会留下半截 PDF；每次调用的临时文件各不相同（不再用 pid 命名），
    并发导出互不干扰。任何一步失败：临时文件与占位文件都清掉，目录里不留垃圾。"""
    d = Path(dir_path)
    d.mkdir(parents=True, exist_ok=True)
    out = reserve_path(d, filename)
    fd = -1
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(d), prefix=f".{out.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        os.write(fd, body)
        os.close(fd)
        fd = -1
        tmp.replace(out)
    except Exception:
        if fd >= 0:
            with contextlib.suppress(Exception):
                os.close(fd)
        if tmp is not None:
            with contextlib.suppress(Exception):
                tmp.unlink()
        with contextlib.suppress(Exception):
            out.unlink()               # 占位空文件也不留
        raise
    return out


def open_folder(dir_path) -> str | None:
    """在文件管理器里打开该目录（os.startfile，仅 Windows）→ 错误文案；None = 已打开。

    **调用方必须自己保证「仅本机」**（main 的 /api/export/open 只放行环回对端）：
    这个函数会在**运行服务的那台机器**上弹窗口，被 LAN 侧触发就是在别人电脑上开窗。
    路径只来自调用方传的 config 值，本函数不接受「打开任意路径」的请求面。"""
    d = Path(dir_path)
    if not d.is_dir():
        return f"导出目录不存在: {d}"
    startfile = getattr(os, "startfile", None)
    if startfile is None:                  # 非 Windows（开发/测试环境）
        return "当前系统不支持在文件管理器中打开文件夹（该按钮仅 Windows 可用）"
    try:
        startfile(str(d))
    except Exception as e:
        return f"打开文件夹失败: {type(e).__name__}: {e}"
    return None
