# -*- coding: utf-8 -*-
"""glossary.py — p2-6（B8）术语库管理：glossary/*.md 的读取 / 新增 / 改写 / 删除。

文件格式必须与 translate.load_glossary_entries 逐字兼容（那是唯一消费方）：
- `#` 开头 = 注释行；空行也保留。**改动时注释与空行一律原样保留**（含顺序），
  否则「改一个词条」会把用户手写的说明整段抹掉。
- 词条行：`EN = 中文`（解析器也接受 Tab 分隔，但本模块写入一律用 ` = `，统一格式）。
- 解析器会跳过无法解析的行；本模块同样把它们当「原样保留行」，绝不丢。

**安全边界（比读路径更严——这是写操作）**：对外只收**裸文件名**（`file: "CS.md"`），
服务端拼到 glossary/ 目录内并做包含性校验。只接受 glossary/ **直属**的 .md：
不接受子目录、不接受绝对路径、不接受 `..`。越界的写入能改任意文件，故这条白名单
是本模块最要紧的一行。

**为什么写盘后不能只靠 mtime 触发热重载**：translate.reload_if_changed 按
st_mtime_ns 判变化，而本机文件系统会把快速重写的时间戳折叠成同一个（实测 300 次
连写里 266 次 st_mtime_ns 完全相同）——「保存即生效」若只靠 mtime 会随机失效。
故写成功后由端点显式调 DeepSeekTranslator.reload_now()（见 translate.py）。
"""
import logging
from pathlib import Path

log = logging.getLogger("glossary")

#: 术语表所在目录名（相对项目根）
GLOSSARY_DIR = "glossary"
#: 单文件词条数上限 / 单字段长度上限（防手滑粘贴整篇文档把 prompt 撑爆）
MAX_ENTRIES = 500
MAX_FIELD = 200

#: 新建文件时的默认表头（与既有 glossary/CS.md 的风格一致）
NEW_FILE_HEADER = (
    "# 课程术语表（每行 `EN term = 中文译名`）\n"
    "# 本文件由 LectureLens 术语页维护；`#` 开头的行是注释，不会被解析\n"
)


def safe_glossary_path(root: Path, name) -> Path | None:
    """把裸文件名解析为 glossary/ 内的真实路径；非法 → None。

    四道闸：① 必须是字符串、非空、无路径分隔符、无 `..`；② 必须以 .md 结尾；
    ③ 解析后父目录必须**就是** glossary/ 目录（符号链接/等价路径兜底）；
    ④ glossary/ 目录须位于 root 内（防 root 本身被指歪）。"""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or len(n) > 128:
        return None
    if "/" in n or "\\" in n or ".." in n:
        return None
    if not n.lower().endswith(".md"):
        return None
    gdir = (Path(root) / GLOSSARY_DIR).resolve()
    cand = (gdir / n).resolve()
    if cand.parent != gdir:
        return None
    return cand


def list_files(root: Path) -> list[Path]:
    """glossary/ 下的全部 .md（按文件名排序）。目录不存在 → 空表。"""
    gdir = Path(root) / GLOSSARY_DIR
    try:
        if not gdir.is_dir():
            return []
        return sorted(p for p in gdir.glob("*.md") if p.is_file())
    except OSError:
        return []


# —— 行模型：entry 可改，raw（注释/空行/无法解析行）原样保留 ——

def parse_lines(text: str) -> list[dict]:
    """文本 → 行列表：[{"kind":"entry","en","zh","raw","indent"} | {"kind":"raw","raw"}]。

    判「是词条」的条件与 load_glossary_entries 完全一致（有 `=` 或 Tab、
    两侧非空），保证「本模块认得的行」与「翻译器认得的行」是同一批。

    entry 额外带两个**保形**字段（评审2 修）：
    - `raw`：该行原文。未被改动的词条回写时**逐字吐回**，于是「改一条不动其余」
      成立——旧实现对**每一条**词条都重写成 `EN = 中文`，于是一份含 `EN\\t中文`
      的术语表只要改一条，其余 Tab 行也会被顺手改成 ` = `（内容没变但整篇 diff）。
    - `indent`：行首空白。词条被改写时保留它——旧实现 strip() 后回写会把缩进吃掉
      （如围栏示例里的 `  a = b` 变成顶格 `a = b`）。"""
    out: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out.append({"kind": "raw", "raw": line})
            continue
        if "=" in s:
            en, _, zh = s.partition("=")
        elif "\t" in s:
            en, _, zh = s.partition("\t")
        else:
            out.append({"kind": "raw", "raw": line})
            continue
        en, zh = en.strip(), zh.strip()
        if not en or not zh:
            out.append({"kind": "raw", "raw": line})
            continue
        out.append({"kind": "entry", "en": en, "zh": zh, "raw": line,
                    "indent": line[:len(line) - len(line.lstrip())]})
    return out


def entry_line(ln: dict) -> str:
    """一条词条的行文本：未被改过就原样吐回，改过则按 `indent + EN = 中文` 重写。"""
    if ln.get("raw") is not None:
        return ln["raw"]
    return f"{ln.get('indent', '')}{ln['en']} = {ln['zh']}"


def render_lines(lines: list[dict], eol: str = "\n") -> str:
    """行列表 → 文本。未改动的词条与 raw 行**逐字吐回**；只有被改过/新增的词条
    才按 `EN = 中文` 规范化。

    eol 由调用方按原文件的换行风格传入（CRLF 文件不会被悄悄改成 LF）。"""
    out = [entry_line(ln) if ln["kind"] == "entry" else ln["raw"] for ln in lines]
    return eol.join(out) + eol


def count_entries(lines: list[dict]) -> int:
    return sum(1 for ln in lines if ln["kind"] == "entry")


def entry_problem(en, zh) -> str | None:
    """词条校验。返回错误文案；None = 合法。

    重点挡两类「写进去就消失」的输入：
    - en 以 `#` 开头 → 重载时被当注释行跳过；
    - en 含 `=` → partition 会从第一个 `=` 断开，词条面目全非。"""
    if not isinstance(en, str) or not isinstance(zh, str):
        return "en/zh 必须是字符串"
    en, zh = en.strip(), zh.strip()
    if not en or not zh:
        return "en 与 zh 都不能为空"
    if len(en) > MAX_FIELD or len(zh) > MAX_FIELD:
        return f"单个字段不得超过 {MAX_FIELD} 字符"
    if "\n" in en or "\r" in en or "\n" in zh or "\r" in zh:
        return "字段内不得含换行"
    if en.startswith("#"):
        return "en 不得以 # 开头（会被当成注释行，词条会消失）"
    if "=" in en:
        return "en 不得含 =（会破坏 `EN = 中文` 的解析）"
    return None


def find_index(lines: list[dict], en: str) -> int | None:
    """按 en 找词条下标（大小写不敏感，取第一条）。"""
    key = (en or "").strip().lower()
    for i, ln in enumerate(lines):
        if ln["kind"] == "entry" and ln["en"].lower() == key:
            return i
    return None


def add_entry(lines: list[dict], en: str, zh: str) -> tuple[list[dict], str | None]:
    """追加一条词条（放在文件末尾，注释与既有顺序不动）。"""
    problem = entry_problem(en, zh)
    if problem:
        return lines, problem
    en, zh = en.strip(), zh.strip()
    if find_index(lines, en) is not None:
        return lines, f"词条已存在: {en}（如需改译名请用「更新」）"
    if count_entries(lines) >= MAX_ENTRIES:
        return lines, f"词条数已达上限 {MAX_ENTRIES}"
    # 新增行没有原文（raw=None）→ 按标准形状 `EN = 中文` 写出
    return lines + [{"kind": "entry", "en": en, "zh": zh, "raw": None,
                     "indent": ""}], None


def update_entry(lines: list[dict], old_en: str, en: str, zh: str
                 ) -> tuple[list[dict], str | None]:
    """改一条词条（按 old_en 定位）。改名后与既有词条重名 → 拒绝。"""
    problem = entry_problem(en, zh)
    if problem:
        return lines, problem
    idx = find_index(lines, old_en)
    if idx is None:
        return lines, f"词条不存在: {old_en}"
    en, zh = en.strip(), zh.strip()
    dup = find_index(lines, en)
    if dup is not None and dup != idx:
        return lines, f"词条已存在: {en}"
    out = list(lines)
    # 改过的词条丢掉 raw（触发规范化重写），但**保留原缩进**，不把行首空白吃掉
    out[idx] = {"kind": "entry", "en": en, "zh": zh, "raw": None,
                "indent": lines[idx].get("indent", "")}
    return out, None


def delete_entry(lines: list[dict], en: str) -> tuple[list[dict], str | None]:
    """删一条词条（按 en 定位）。注释与其余词条位置不变。"""
    idx = find_index(lines, en)
    if idx is None:
        return lines, f"词条不存在: {en}"
    return [ln for i, ln in enumerate(lines) if i != idx], None


def read_file(path: Path) -> dict:
    """读一个术语表 → {"text","lines","eol","entries"}。文件不存在 → 空表结构。

    **必须 newline="" 开**：Path.read_text 走通用换行，`\\r\\n` 会被就地翻成 `\\n`，
    于是「原文到底是不是 CRLF」再也看不出来——本模块要靠它决定回写时用哪种行尾。
    """
    p = Path(path)
    text = ""
    try:
        if p.is_file():
            with open(p, "r", encoding="utf-8", newline="") as f:
                text = f.read()
    except OSError:
        text = ""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = parse_lines(text)
    return {"text": text, "lines": lines, "eol": eol,
            "entries": [{"en": ln["en"], "zh": ln["zh"]}
                        for ln in lines if ln["kind"] == "entry"]}


def write_file(path: Path, lines: list[dict], eol: str = "\n") -> None:
    """原子写（先写同目录 .tmp 再 replace）：避免写一半崩溃留下半截术语表。

    **必须 newline="" 写**：Path.write_text 在 Windows 上会把 `\\n` 自动翻成
    `\\r\\n`（os.linesep 转换），于是「只改一个词条」会把整份 LF 文件的每一行都
    变成 CRLF——内容是没变，但 git diff 全红、用户看到的是「什么都没改却整篇变了」
    （实测踩过：原文件 436B 的 LF，改一次回来变 448B 的 CRLF）。
    newline="" 关掉转换，行尾完全由 eol 参数决定。"""
    p = Path(path).resolve()
    # 写前包含性校验（安全扫描要求 + 防御纵深）：端点层已用 safe_glossary_path()
    # 做根锚定白名单，这里在真正的写盘点再兜底一次——只允许 glossary/ 直属的 .md，
    # 即使将来某个调用方漏验，也无法借这里越界写任意文件。
    if p.suffix.lower() != ".md" or p.parent.name != GLOSSARY_DIR:
        raise ValueError(f"拒绝写入 glossary/ 之外的路径: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(render_lines(lines, eol), encoding="utf-8", newline="")
    tmp.replace(p)
