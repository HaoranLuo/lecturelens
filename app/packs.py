# -*- coding: utf-8 -*-
"""packs.py — B13 术语库包：`glossary_packs/*.md`（可分发的术语表包）→ 课程术语表。

一个**包**就是一份与 `glossary/*.md` 同格式的 md（每行 `EN = 中文`，`#` 头注释写
元信息：类别/简介/维护/版本）。本模块负责四件事，全部是**行级纯函数**（读盘/写盘
走 glossary 模块——同一份解析器、同一套原子写，绝不另立第二套格式）：

1. 枚举：`list_packs` / `parse_pack` / `find_pack`（条目数用 glossary.count_entries
   实数，不是文件里写死的数）；
2. 安装：`install_block` —— 整块并入，**并集去重**（用户已有优先）+ 上限预检；
3. 卸载：`uninstall_block` —— 按**双标记**切片整块移除；
4. 命名：`course_glossary_name` —— 课程名 → 合法裸文件名（课程未绑定术语表时建表用）。

**双标记是硬要求**（块形状）：

    # —— 来自包：数学 ——
    derivative = 导数
    …
    # —— 结束包：数学 ——

为什么不能只有起始标记：写入走 **EOF 追加**（与 glossary.add_entry 同款——绝不重排
用户既有行），于是块尾之后随时会追加用户自加的词条。只有起始标记的话，「卸载」分不清
「包自带的最后一条」与「用户后加的第一条」，一刀切下去就会连用户的词条一起吃掉。
双标记也不怕缺：起始在、结束不在（用户手改坏）→ 如实报错、**一个字节都不动**。

去重口径 = `glossary.find_index`（大小写不敏感）。**用户已有优先**：包内同名条目跳过，
已有词条的译名绝不被覆盖。副作用要知情：装 A 之后装 B，B 里与 A 重名的条目会被跳过
（挂在 A 的块里）；此后卸载 A 会连带去掉那条——此时重装 B 即可补回。
"""
import logging
from pathlib import Path

import glossary

log = logging.getLogger("packs")

#: 包目录名（相对仓库根；frozen 下位于 _MEIPASS，见 packs_dir）
PACKS_DIR = "glossary_packs"

#: 安装块的双标记（**格式即契约**：卸载与「已装状态」都按这两行切片/识别）
MARK_START = "# —— 来自包：{} ——"
MARK_END = "# —— 结束包：{} ——"
_MARK_START_HEAD = "# —— 来自包："
_MARK_TAIL = " ——"

#: 包名长度上限（要能安全地写进注释行；够长够用，且挡住恶意超长输入）
PACK_NAME_MAX = 60

#: 包文件头注释区的标签 → 记录字段（中英文冒号都认）
META_LABELS = (("类别", "category"), ("简介", "intro"),
               ("维护", "maintainer"), ("版本", "version"))

#: Windows 文件名非法字符（课程名是用户输入，可能带 `/` `:` 等；派生文件名时替换成 `-`）
_ILLEGAL_CHARS = '<>:"/\\|?*'


def packs_dir(root, meipass=None) -> Path:
    """包目录：dev = `<root>/glossary_packs`；frozen = `<_MEIPASS>/glossary_packs`。

    照 main.WEB 的双路径模式：打包版把包当**只读数据**塞进 _MEIPASS
    （--add-data glossary_packs;glossary_packs），开发态直接从仓库根读。
    `meipass=None`（默认）即开发态口径——调用方按 sys.frozen 显式传。"""
    base = Path(meipass) if meipass else Path(root)
    return base / PACKS_DIR


def list_pack_files(dir_path) -> list[Path]:
    """包目录**直属**的 .md（按文件名排序）。目录不存在 → 空表。

    只收直属文件（`*.md` 一层）——子目录（含扫描工具留下的点目录）一律不看。"""
    d = Path(dir_path)
    try:
        if not d.is_dir():
            return []
        return sorted((p for p in d.glob("*.md") if p.is_file()), key=lambda p: p.name)
    except OSError as e:
        log.debug(f"[packs] 包目录读取失败 {d}: {e}")
        return []


def pack_meta(text: str) -> dict:
    """从文件头注释区解析元信息 → {"category","intro","maintainer","version"}（缺则缺）。

    只扫**首个词条行之前**的注释：包正文里若有人写 `# 简介：…` 也不会被误当成头部。"""
    out: dict = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("#"):
            if s:                       # 头注释区结束（第一条词条）
                break
            continue
        body = s.lstrip("#").strip()
        for label, key in META_LABELS:
            for colon in ("：", ":"):
                prefix = label + colon
                if body.startswith(prefix):
                    out[key] = body[len(prefix):].strip()
    return out


def parse_pack(path) -> dict:
    """读一个包 → 记录（含词条）。**条目数 = glossary.count_entries 实数**。

    **逐文件容错**（评审修复）：单个包读不出来，只让**这一张卡**显示成坏包，绝不把整个
    包列表连它的接口一起带崩。为什么这条是硬要求：
    - 打包版的包目录在 dist 下、**用户可写**（`_internal/glossary_packs/`），而包文件本身
      就是「改完发回整个文件即可作为新包分享」的载体——用记事本另存为 ANSI/GBK 是最常见
      的操作失误；
    - `glossary.read_file` 只 catch OSError，而 UnicodeDecodeError 是 **ValueError**，
      会一路穿透到 GET /api/glossary/packs 与 POST install（实测：一个 GBK 包让整张卡
      片 500，且**装任何包**都 500——find_pack 会先解析到它）。
    与同文件 packs_view 里对 glossary/ 文件的逐文件容错（「单个文件坏不该让端点 500」）
    同一条口径：坏包在记录里带 `error` 文案（前端照原样显示），条目数 0、不可安装。"""
    p = Path(path)
    rec = {"file": p.name, "name": p.stem, "category": "", "intro": "",
           "maintainer": "", "version": "", "count": 0, "entries": [], "error": None}
    try:
        data = glossary.read_file(p)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        rec["error"] = (f"这个包读不出来（{type(e).__name__}）：文件不是 UTF-8 编码或已损坏"
                        f"——用编辑器另存为 UTF-8 后重试")
        log.warning(f"[packs] ⚠ 读不了这个包 {p.name}: {type(e).__name__}: {e}")
        return rec
    meta = pack_meta(data["text"])
    entries = [(ln["en"], ln["zh"]) for ln in data["lines"] if ln["kind"] == "entry"]
    rec.update({
        "category": meta.get("category", ""),
        "intro": meta.get("intro", ""),
        "maintainer": meta.get("maintainer", ""),
        "version": meta.get("version", ""),
        "count": len(entries),
        "entries": entries,
    })
    return rec


def list_packs(dir_path) -> list[dict]:
    """目录下全部包的记录（含 entries；端点按需裁掉再返回给前端）。"""
    return [parse_pack(p) for p in list_pack_files(dir_path)]


def find_pack(dir_path, name) -> dict | None:
    """按包名（= 文件名主干）精确查找；不存在 → None。"""
    n = name.strip() if isinstance(name, str) else None
    if not n:
        return None
    for p in list_pack_files(dir_path):
        if p.stem == n:
            return parse_pack(p)
    return None


def name_problem(name) -> str | None:
    """包名校验（返回值 = 错误文案，None = 合法）。包名会写进注释标记行，故必须干净：
    不含换行（会把一行拆成两行、破坏块结构）、不含 `#`（会被解析成注释头）。"""
    if not isinstance(name, str):
        return "包名必须是字符串"
    n = name.strip()
    if not n:
        return "包名不能为空"
    if len(n) > PACK_NAME_MAX:
        return f"包名不得超过 {PACK_NAME_MAX} 字符"
    if "\n" in n or "\r" in n:
        return "包名不得含换行"
    if "#" in n:
        return "包名不得含 #（会破坏包标记行）"
    return None


def mark_start(name: str) -> str:
    return MARK_START.format(name)


def mark_end(name: str) -> str:
    return MARK_END.format(name)


def _is_mark(line: dict, text: str) -> bool:
    return line.get("kind") == "raw" and line.get("raw", "").strip() == text


def block_spans(lines: list[dict], name: str) -> tuple[list, int, int]:
    """定位该包在行列表里的块 → (完整块区间列表, 悬空起始标记数, 悬空结束标记数)。

    区间是**闭区间** (start, end)，两端都是标记行本身。悬空的起始标记（有头无尾，
    或被手改掉尾）单独计数——调用方据此**拒绝动手**，而不是猜块边界。"""
    s, e = mark_start(name), mark_end(name)
    complete: list = []
    dangling_s = dangling_e = 0
    i = 0
    while i < len(lines):
        if _is_mark(lines[i], s):
            j = i + 1
            while j < len(lines) and not _is_mark(lines[j], e):
                j += 1
            if j < len(lines):
                complete.append((i, j))
                i = j + 1
                continue
            dangling_s += 1
            i += 1
            continue
        if _is_mark(lines[i], e):
            dangling_e += 1
        i += 1
    return complete, dangling_s, dangling_e


def installed_names(lines: list[dict]) -> list[str]:
    """这份术语表里**装了哪些包**（按起始标记识别，去重保序）。UI 的「已装」状态用它。"""
    out: list[str] = []
    for ln in lines or []:
        if not _is_mark_head(ln):
            continue
        nm = _mark_head_name(ln["raw"].strip())
        if nm and nm not in out:
            out.append(nm)
    return out


def _is_mark_head(line: dict) -> bool:
    return (line.get("kind") == "raw"
            and isinstance(line.get("raw"), str)
            and line["raw"].strip().startswith(_MARK_START_HEAD)
            and line["raw"].strip().endswith(_MARK_TAIL)
            and len(line["raw"].strip()) > len(_MARK_START_HEAD) + len(_MARK_TAIL))


def _mark_head_name(text: str) -> str:
    return text[len(_MARK_START_HEAD):-len(_MARK_TAIL)]


def install_block(lines: list[dict], name: str, entries: list
                  ) -> tuple[list[dict], dict, str | None]:
    """把包整块并入行列表 → (新行列表, info, 错误文案)；info 见下。

    语义（B13 硬要求）：
    - **先卸后装**：同包旧块先整块移除（重复安装/换版本不会叠成两块）；
    - **去重以用户已有为准**（find_index 大小写不敏感）：同名条目跳过、译名不覆盖；
    - **上限预检**：结果条数 > glossary.MAX_ENTRIES → 直接拒绝（一个字节都不写回）；
    - 追加位置 = **EOF**（绝不重排用户既有行），块用双标记框起；
    - 词条合法性过 glossary.entry_problem（包文件是可信输入，但校验便宜、防手改的包）。

    info 里的三个数**必须回给用户**（评审修复）：块内条目在重新安装时是**以包为准**重写的
    ——用户通过术语页改过的块内译名会被重置、包新版本里删掉的词条会被带走。这些都不是
    错误（整块重写正是「重新安装」的定义），但不能静默：
    - `added`：本次**写进新块的条目数**（旧块先被移除 → 同包重装时它等于包内条数减去
      与**块外**条目重合的部分，不是「文件里净增的条数」）；
    - `skipped`：[en]，块**外**已有同名条目而被跳过的（用户译名不动，含大小写不敏感）；
    - `overwritten`：[{en, was, now}]，块内被改译的条目——包的译名覆盖了用户的；
    - `dropped`：[en]，旧块里有、而本版包里没有的条目（重新安装会从文件里移除它们）。

    错误文案非 None 时第一项是**原样的 lines**（调用方据此判定「未改动」）；info 为 {}。"""
    problem = name_problem(name)
    if problem:
        return lines, {}, problem
    if not entries:
        return lines, {}, f"包「{name}」里没有可安装的词条（文件为空或格式坏了）"
    complete, _, _ = block_spans(lines, name)      # 旧块内容：用于回报被覆盖/被移除的词条
    old_block = [ln for a, b in complete for ln in lines[a + 1:b] if ln["kind"] == "entry"]
    base, dropped_rows, err = _drop_blocks(lines, name)
    if err:
        return lines, {}, err
    fresh: list[dict] = []
    skipped: list[str] = []
    seen: set = set()
    pack_zh: dict = {}
    for en, zh in entries:
        bad = glossary.entry_problem(en, zh)
        if bad:
            return lines, {}, f"包「{name}」内词条非法（{en!r}）: {bad}"
        key = en.strip().lower()
        pack_zh.setdefault(key, zh.strip())
        if key in seen or glossary.find_index(base, en) is not None:
            skipped.append(en.strip())              # 用户已有 / 包内重复 → 不覆盖
            continue
        seen.add(key)
        fresh.append({"kind": "entry", "en": en.strip(), "zh": zh.strip(),
                      "raw": None, "indent": ""})
    existing = glossary.count_entries(base)
    total = existing + len(fresh)
    if total > glossary.MAX_ENTRIES:
        return lines, {}, (
            f"装不下：「{name}」需新增 {len(fresh)} 条，该术语表已有 {existing} 条，"
            f"合计 {total} 条超过上限 {glossary.MAX_ENTRIES} 条——文件未改动。"
            f"请先精简该术语表（或卸载其它包）后重试")
    overwritten: list[dict] = []
    gone: list[str] = []
    for ln in old_block:
        key = ln["en"].strip().lower()
        if key in pack_zh:
            if pack_zh[key] != ln["zh"].strip():
                overwritten.append({"en": ln["en"], "was": ln["zh"], "now": pack_zh[key]})
        elif ln["en"] not in gone:
            gone.append(ln["en"])
    info = {"added": len(fresh), "skipped": skipped,
            "overwritten": overwritten, "dropped": gone}
    if not fresh:
        # 全部命中去重（或包为空——上面已挡）：只把旧块卸掉，不写空块
        log.info(f"[packs] 「{name}」无新增词条（跳过 {len(skipped)} 条重复），"
                 f"仅移除旧块（{dropped_rows} 行）")
        return base, info, None
    block = ([{"kind": "raw", "raw": mark_start(name)}] + fresh
             + [{"kind": "raw", "raw": mark_end(name)}])
    log.info(f"[packs] 「{name}」安装块：新增 {len(fresh)} 条、跳过 {len(skipped)} 条重复"
             f"（旧块移除 {dropped_rows} 行；覆盖译名 {len(overwritten)} 条、"
             f"移除旧词条 {len(gone)} 条）")
    return base + block, info, None


def _drop_blocks(lines: list[dict], name: str) -> tuple[list[dict], int, str | None]:
    """移除该包的全部完整块 → (新行列表, 移除行数, 错误文案)。

    标记不完整（有头无尾 / 有尾无头）→ 报错且**原样返回**：宁可让用户手工看一眼，
    也不猜边界把不该删的行删掉（这条是「卸载不吞用户词条」的最后一道保险）。"""
    complete, dangling_s, dangling_e = block_spans(lines, name)
    if dangling_s or dangling_e:
        return lines, 0, _broken_mark_text(name, dangling_s, dangling_e)
    if not complete:
        return lines, 0, None
    drop: set = set()
    for a, b in complete:
        drop.update(range(a, b + 1))
    return [ln for i, ln in enumerate(lines) if i not in drop], len(drop), None


def _broken_mark_text(name: str, dangling_s: int, dangling_e: int) -> str:
    return (f"「{name}」的包标记不完整（起始标记 {dangling_s} 处 / 结束标记 {dangling_e} 处"
            f"没有配对——可能被手工改过）：为避免删错内容，本次**未改动文件**；"
            f"请在术语表里手工删掉该块后重试")


def uninstall_block(lines: list[dict], name: str) -> tuple[list[dict], int, str | None]:
    """按双标记切片整块移除 → (新行列表, 移除行数, 错误文案)。

    - 未安装（没有该包的标记）→ 错误文案（调用方给 409；**不动文件**）；
    - 标记不完整 → 错误文案（**不动文件**）；
    - 块外的用户词条、注释、空行一律原样保留。"""
    problem = name_problem(name)
    if problem:
        return lines, 0, problem
    complete, dangling_s, dangling_e = block_spans(lines, name)
    if dangling_s or dangling_e:
        return lines, 0, _broken_mark_text(name, dangling_s, dangling_e)
    if not complete:
        return lines, 0, f"「{name}」没有安装在这份术语表里（找不到包标记）"
    out, dropped, err = _drop_blocks(lines, name)
    if err:
        return lines, 0, err
    log.info(f"[packs] 「{name}」卸载块：移除 {dropped} 行")
    return out, dropped, None


def shared_entries(lines: list[dict], name: str, other_packs: dict) -> list[str]:
    """该包块内的词条里，**同时还由别的已装包提供**的那些 en（大小写不敏感去重）。

    这是「并集去重」的已知副作用，必须如实回报而不是假装没有：装 A 之后装 B，B 里与
    A 重名的条目会被**跳过**（挂在 A 的块里，译名以先装的为准——各包对同一个词给出
    不同译名时，这一点尤其要紧，例如 derivative=导数（数学）/衍生品（经济））；
    此后卸载 A 会把这些词一并带走。端点据此回一个 `shared` 名单，UI 提示「如需保留
    请重装该包」——不自动补写（卸载就该只做卸载这一件事，不偷偷改用户没要求动的地方）。

    `other_packs` = {其它包名: 该包的 en 列表}（调用方只传**这份文件里仍装着**且包目录
    里还在的包）。"""
    complete, _, _ = block_spans(lines, name)
    if not complete or not other_packs:
        return []
    keep: set = set()
    for ens in other_packs.values():
        for en in ens:
            keep.add(str(en).strip().lower())
    out: list[str] = []
    for a, b in complete:
        for ln in lines[a + 1:b]:
            if ln.get("kind") != "entry":
                continue
            if ln["en"].strip().lower() in keep and ln["en"] not in out:
                out.append(ln["en"])
    return out


def course_glossary_name(course_name, existing=()) -> str:
    """由课程名派生术语表**裸文件名**（「数学 101」→ `数学 101.md`）。

    课程名是用户输入（≤60 字符、任意语言、可能带 `/` `:` `*` 等），文件名却要过
    glossary.safe_glossary_path 的四道闸，故此处：Windows 非法字符与控制字符 → `-`、
    连续空白压成单个空格、剥掉首尾空白与点、`..` 消掉（safe_glossary_path 拒 `..`）、
    限长 100（给 `.md` 与后缀留余量，白名单上限 128）。

    `existing` = glossary/ 现有文件名集合：**撞名就追加 -2/-3…**（大小写按 Windows
    口径比对）——绝不因为「建表」覆盖或改写别人的文件。"""
    raw = str(course_name or "")
    safe = "".join("-" if (ch in _ILLEGAL_CHARS or ord(ch) < 32) else ch for ch in raw)
    safe = " ".join(safe.split()).strip(" .").replace("..", "-").strip(" .")
    if not safe:
        safe = "术语表"
    safe = safe[:100]
    have = {str(x).lower() for x in (existing or ())}
    name = f"{safe}.md"
    if name.lower() not in have:
        return name
    n = 2
    while f"{safe}-{n}.md".lower() in have:
        n += 1
    return f"{safe}-{n}.md"
