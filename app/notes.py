# -*- coding: utf-8 -*-
"""notes.py — p2-2 笔记库：transcripts/*.md 的解析、索引与检索（纯函数，可单测）。

笔记文件格式（由 main.Hub 落笔，见 main.note_header / Hub.append）：

    # 双语课堂笔记 20261013_0146        ← 时间戳

    > LectureLens 自动生成（EN 原文 / ZH 翻译）
    > 课程：计算机科学导论              ← p2-1 起（老笔记无此行 → 归「未分类」）

    ---

    - `01:46:42` All right.             ← EN 行（无 seg 标号）
      - `#1` 好的。                      ← ZH 行，带 #seg 锚点

归属改写的两条路（都只动头部那一行，正文零改动）：课中切课 = Hub.retag_course
**追加切换标记**（保留"当时切过"的事实）；课后更正 = 本模块 set_course **整篇改写**
（定稿口径，端点 §2 /api/notes/course）。两套语义不可互相顶替。

**EN↔ZH 配对规则（本模块的核心假设，已用真实数据核验）**：EN 行按落盘顺序即
seg 1..N（main 的 asr_loop 串行 append，seg 自增），ZH 行自带 `#seg`，故
「第 i 条 EN（1 起）」↔「`#i` 的 ZH」。核验：对 transcripts/ 下 57 篇实测，
所有含 ZH 的笔记 seg 均为连续的 1..N 且与 EN 条数相等（详见测试用例
test_real_transcripts_pair_by_seg 的口径说明）。

不用「缩进父子关系」配对：main.Hub.append 的缩进只是排版，ZH 迟到时可能落在
别的 EN 之下（main.py 注释已说明需靠 `#seg` 锚定），按缩进配对会错配。

老笔记可能整篇没有 ZH（未配 DEEPSEEK_API_KEY 的纯英文场次，实测存在 2 篇），
此时 zh=None —— 预览端显示为空栏而非报错。
"""
import re
from pathlib import Path

from courses import COURSE_HEADER_PREFIX, UNCATEGORIZED, note_course

#: 笔记文件名（原生的时间戳形态；新建笔记走它。§4 起**改名后的笔记**不再匹配它）
NOTE_RE = re.compile(r"^lecture_(\d{8})_(\d{4})\.md$")
NOTE_GLOB = "lecture_*.md"
#: 笔记名形态（§4 起放宽：`lecture_` + 任意安全字符 + `.md`）。新建笔记仍是时间戳名，
#: 但**重命名**后可以是任何安全名（如 `lecture_第3讲-神经网络.md`）——两种都必须能被
#: 索引/读写，否则「改名」等于把笔记从库里弄丢。派生物（.summary.md/.bak.md）另行排除。
NOTE_NAME_RE = re.compile(r"^lecture_.+\.md$")
#: 笔记名长度上限（含前缀与扩展名；Windows 整条路径上限 260 的余量足够）
NOTE_NAME_MAX = 100
#: §6：AI 摘要文件名后缀（`lecture_X` → `lecture_X.summary.md`）。**单一来源**：
#: 列表徽章（index）、main 的读写端点（summary_path）、summarize 的落盘三处共用，
#: 否则「写的地方」与「读的地方」迟早在改名/搬运时对不上。
SUMMARY_SUFFIX = ".summary.md"
#: §6：摘要的**覆盖前备份**后缀（`lecture_X.summary.bak.md`）。重新生成摘要前把
#: 人工修订过的那一版存这里，覆盖不再静默丢内容。
SUMMARY_BACKUP_SUFFIX = ".summary.bak.md"
#: 派生物后缀（都不是笔记正文：AI 摘要 / 人工修订备份）
DERIVED_SUFFIXES = (SUMMARY_SUFFIX, ".bak.md")
#: 回收站目录名（transcripts/ 下的子目录）。索引与课程统计都用**非递归** glob，
#: 天然看不到它——「排除 .trash」是结构保证，不靠各处记得过滤。
TRASH_DIR = ".trash"
#: 派生产物的后缀（删除/恢复/改名时跟着笔记一起走，避免留下孤儿文件）
SIDECAR_SUFFIXES = (SUMMARY_SUFFIX, SUMMARY_BACKUP_SUFFIX, ".times.json", ".apkg")
#: Windows 保留设备名（不区分大小写；扩展名前的部分也算）
RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})
#: 文件名里一律禁止的字符（含路径分隔符）
BAD_NAME_CHARS = set('\\/:*?"<>|')
#: 上者的展示形态（错误文案给人看，别拿 set 拼字符串）
BAD_NAME_SHOW = '\\ / : * ? " < > |'
#: 正文行格式（与 main.Hub.append 的落笔格式成对，改动需同步）
EN_RE = re.compile(r"^- `([^`]+)` (.*)$")
ZH_RE = re.compile(r"^  - `#(\d+)` (.*)$")
ZH_LOOSE_RE = re.compile(r"^  - (?!#)(.*)$")   # 老格式：ZH 行没有 #seg 标号
TITLE_PREFIX = "# 双语课堂笔记 "
#: 自动生成说明行（note_header 落笔的第二行）——「插入课程行」时优先挂在它下面
AUTOGEN_PREFIX = "> LectureLens 自动生成"
#: §8 标签行前缀（与课程行相邻；写成 `> 标签：#重点 #考试`）
TAGS_HEADER_PREFIX = "> 标签："
#: 标签行解析时的宽松前缀（手改过的笔记可能写成半角冒号）
TAGS_HEADER_LOOSE = "> 标签"
TAG_MAX_LEN = 16        # 单个标签长度上限（字符数）
TAG_MAX_COUNT = 20      # 每篇笔记的标签数上限
#: 标签里额外要挡的不可见字符（str.isspace() 覆盖不到的零宽/格式类）
TAG_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff"
#: **文件名与标签共用的唯一一份「看不见但会改变含义」字符表**（评审④）：
#: 零宽族（U+200B..）之外还含行/段分隔符 U+2028/U+2029——后者在部分编辑器/工具里
#: 会被当换行（标签侧 M3 实测过它写坏笔记头）。导出侧 clean_component 把它替换成
#: `-`；改名侧 name_problem 直接拒绝（两个名字肉眼看一样、实际不同 = 最坏的误导）。
INVISIBLE_CHARS = TAG_INVISIBLE + "\u2028\u2029"
#: 标签里一律禁止的字符（'#' 是分隔符）
TAG_BAD_CHARS = "#" + TAG_INVISIBLE
#: 头部区块行数：course 行**读写同窗口**（note_course 只扫前 12 行，改写也只看这 12 行）
HEAD_SCAN = 12

MAX_QUERY = 200        # 检索词长度上限（超长直接截断，避免无意义的超长匹配）
FIRST_SENT_MAX = 120   # 首句摘要截断长度


def set_course(text: str, course: str) -> tuple[str, str]:
    """§2：把笔记头的课程行改成 course → (新文本, 动作)。动作 = replaced / inserted。

    与课中切课（main.Hub.retag_course 追加「切换标记」）**语义不同**：这是课后
    更正归属的**定稿口径**——整篇归属改写，不新增标记行。

    规则（与解析侧 courses.note_course 严格同窗口，避免"改的行读不到、读的行没改"）：
    - 在**头部区块**（前 HEAD_SCAN 行）找首个 `> 课程：` 行 → 原行替换，行尾符号
      按原样保留（\\r\\n 的笔记不会被悄悄转成 \\n）；
    - 头部没有该行（p2-1 之前的老笔记 / 被手改掉）→ 插进头部区块：优先紧跟
      「> LectureLens 自动生成…」行，其次标题行之后，再兜底文首。用文件自身的主
      行尾符号，不引入混排；
    - 正文里的同名行（历史切换标记）一律不动——那是"当时确实切过课"的事实记录。

    纯函数、不吃 IO：写盘与原子性由端点负责（main.note_course_write）。"""
    course = course or UNCATEGORIZED      # 兜底：绝不让空名写成 "None" 那样的字面量
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[:HEAD_SCAN]):
        if line.startswith(COURSE_HEADER_PREFIX):
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"{COURSE_HEADER_PREFIX}{course}{eol}"
            return "".join(lines), "replaced"
    idx = 0
    for i, line in enumerate(lines[:HEAD_SCAN]):
        if line.startswith(AUTOGEN_PREFIX):
            idx = i + 1
            break
    else:
        for i, line in enumerate(lines[:HEAD_SCAN]):
            if line.startswith(TITLE_PREFIX):
                idx = i + 1
                while idx < HEAD_SCAN and idx < len(lines) and not lines[idx].strip():
                    idx += 1        # 跳过标题后的空行：插在正文之前，不跟标题粘成一坨
                break
    eol = "\r\n" if "\r\n" in text else "\n"
    lines.insert(idx, f"{COURSE_HEADER_PREFIX}{course}{eol}")
    return "".join(lines), "inserted"


def safe_note_path(dir_path: Path, name) -> Path | None:
    """把请求里的笔记名解析成**目录内**的真实路径；非法/越界 → None。

    安全要害：`name` 直接来自 HTTP 查询参数，必须同时挡住
    ① 路径分隔符与 `..`（`../../.env` 之类）——**回收站里的笔记也因此打不开**：
       它的名字只能写成 `.trash/…`，第一步就被拒（"回收站内容只走回收站接口"）；
    ② 绝对路径（`C:/Windows/win.ini`）；
    ③ 不匹配笔记名形态的文件（config.json / 日志 / `.summary.md` 等一律读不到）；
    ④ 解析后父目录不等于 transcripts 目录（符号链接/等价路径兜底）。
    四道都过才返回路径——这是唯一允许读写笔记文件的入口，端点层必须走它。"""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or len(n) > 128:
        return None
    if "/" in n or "\\" in n or ".." in n:
        return None
    if not is_note_file(Path(n)):
        return None
    d = Path(dir_path)
    cand = (d / n).resolve()
    if cand.parent != d.resolve():
        return None
    return cand


def is_note_file(p: Path) -> bool:
    """是否是「笔记正文」文件（排除 .summary.md / .bak.md 等派生物）。

    §4 起判据是 `lecture_*.md`（改名后的语义名也算）而不是时间戳正则——否则
    「重命名」会把笔记从索引里弄丢。派生物仍排除（它们与正文同前缀同目录）。"""
    n = p.name
    return bool(NOTE_NAME_RE.match(n)) and not n.endswith(DERIVED_SUFFIXES)


def name_problem(raw) -> str | None:
    """校验一个**新**笔记名（重命名目标）→ 错误文案；None = 合法。

    规则（§4 口径：保留 .md、禁路径分隔/系统保留名/长度限）：
    - 非字符串/空/超长（含前缀与扩展名 ≤ NOTE_NAME_MAX）；
    - 含 `\\ / : * ? " < > |` 或控制字符；含 `..`；
    - 含**不可见字符**（零宽族/行分隔符，见 INVISIBLE_CHARS）——两个名字肉眼看一样、
      实际不同名是最坏的一种误导（与标签侧同一张表，评审④）；
    - 以点或空格开头/结尾（Windows 上会被静默裁掉，造成"名字对不上"的鬼故事）；
    - 主体是 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）；
    - 派生物名（`.summary.md` / `.bak.md`）——那是产物位，别被用户名字占用。"""
    if not isinstance(raw, str):
        return "新文件名必须是字符串"
    n = raw.strip()
    if not n:
        return "新文件名不能为空"
    if len(n) > NOTE_NAME_MAX:
        return f"新文件名太长（上限 {NOTE_NAME_MAX} 字符，含前缀与 .md）"
    if any(ch in BAD_NAME_CHARS for ch in n) or any(ord(ch) < 32 for ch in n):
        return f'新文件名含非法字符（禁 {BAD_NAME_SHOW} 与控制字符）'
    if any(ch in INVISIBLE_CHARS for ch in n):
        return "新文件名含不可见字符（零宽/行分隔符）——请删掉它再提交"
    if ".." in n:
        return "新文件名不得包含 .."
    if n.startswith(".") or n.endswith(" ") or n.endswith("."):
        return "新文件名不得以点或空格开头/结尾"
    if n.endswith(DERIVED_SUFFIXES):
        return f"新文件名不得使用派生物后缀（{list(DERIVED_SUFFIXES)}）"
    stem = n.split(".", 1)[0].strip().lower()
    if stem in RESERVED_STEMS:
        return f"新文件名不能是系统保留名: {n}（CON/PRN/AUX/NUL/COM1-9/LPT1-9）"
    return None


def normalize_note_name(raw) -> tuple[str | None, str | None]:
    """把用户输入的新名规范成合法笔记名 → (名字, 错误文案)。

    - 缺 `.md` → 补上（"保留 .md"：扩展名归位，不要求用户记着敲）；
    - 缺 `lecture_` 前缀 → 补上（库里的键就是它：没有前缀就不在索引范围内）；
    - 其余按 name_problem 逐条校验，任一不过 → 原样报错（**不猜测用户意图**）。"""
    if not isinstance(raw, str):
        return None, "新文件名必须是字符串"
    n = raw.strip().rstrip(". ")              # Windows 会静默裁掉尾部的点/空格
    if not n:
        return None, "新文件名不能为空"
    if not n.lower().endswith(".md"):
        n += ".md"
    else:
        n = n[:-3] + ".md"                      # 统一小写扩展名（.MD → .md）
    if not n.startswith("lecture_"):
        n = "lecture_" + n
    problem = name_problem(n)
    if problem:
        return None, problem
    if not is_note_file(Path(n)):
        return None, f"新文件名不合法（须形如 lecture_<名字>.md）: {n}"
    return n, None


def trash_path(dir_path: Path, name: str) -> Path | None:
    """回收站里某个笔记名对应的路径（只算路径，不碰盘）。名字不合法 → None。"""
    if name_problem(name):
        return None
    return Path(dir_path) / TRASH_DIR / name


def sidecar_paths(p: Path) -> list[str]:
    """一篇笔记的派生产物名（同 stem + 各后缀）——删除/恢复/改名时按它搬运。"""
    return [p.stem + suffix for suffix in SIDECAR_SUFFIXES]


class MoveConflict(Exception):
    """目标位置已有同名文件（正文或**派生文件**）——移动整体拒绝，绝不覆盖。

    评审 M2 实测的两条静默覆盖路径都在这里收口：① 回收站里已有 `lecture_A.md`
    时再删一篇同名笔记 → 旧的那篇（连摘要）被直接 replace 掉；② 恢复时目标目录
    没有 `lecture_A.md` 但有 `lecture_A.summary.md` → 那份摘要被覆盖（而端点
    docstring 写着「绝不覆盖」）。"""

    def __init__(self, names: list[str]):
        self.names = list(names)
        super().__init__("已有同名文件: " + ", ".join(self.names))


def move_note_with_sidecars(p: Path, dest_dir: Path) -> list[str]:
    """把一篇笔记**连同派生物**挪到 dest_dir（同盘 rename）→ 实际移动的文件名列表。

    为什么带上派生物：笔记删进回收站而摘要/时间轴留在原处，就成孤儿了（回收站里
    恢复不出来、列表里又没有入口）；改名同理（`X.summary.md` 不跟着改，摘要徽章
    就丢了）。dest_dir 不存在时创建（.trash）。

    **先整体预检、再逐个搬**：目标里已存在同名文件（正文或任一派生文件）就抛
    MoveConflict 且一个都不动——半执行会把正文和摘要拆到两个目录，比直接报错糟。
    （逐个 rename 期间仍可能因 IO 失败半途而废：Windows 没有目录级事务，这一点
    由调用方把已移动名单如实回报；覆盖风险则已被预检消除。）"""
    dest = Path(dest_dir)
    names = [p.name] + [n for n in sidecar_paths(p) if p.with_name(n).is_file()]
    conflicts = [n for n in names if (dest / n).exists()]
    if conflicts:
        raise MoveConflict(conflicts)
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in names:
        src = p if name == p.name else p.with_name(name)
        src.replace(dest / name)
        moved.append(name)
    return moved


def normalize_tag(raw) -> str | None:
    """§8 单个标签规范化 → 合法标签；非法 → None。

    规则（计划口径）：≤ TAG_MAX_LEN 字符；**禁一切空白与控制字符**（含全角空格、
    不换行空格 \\xa0、段/行分隔符 \\u2028/\\u2029、C0/C1 控制符与零宽字符）+ 禁 `#`。
    宽松处：允许用户带 `#` 提交（`#重点` 与 `重点` 等价，去掉开头的一个或多个 `#`），
    首尾空白也去掉——"想得到的最自然输入"都该能用，而不是让人记住格式。

    为什么要按 isspace() 收紧（评审 M3 实测）：只拦 '\\n\\r\\t' 时 `a\\u2028b` 能过关，
    而 str.splitlines()/文本编辑器都在 \\u2028 处断行——标签被当成**裸行**写进笔记头，
    文件格式当场被用户输入破坏，且 POST 回显（提交值）与随后的 GET（解析值）自相矛盾。
    这里与 name_problem 的控制字符口径对齐（那边也是 ord<32 一票否决）。"""
    if not isinstance(raw, str):
        return None
    t = raw.strip().lstrip("#").strip()
    if not t or len(t) > TAG_MAX_LEN:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ch in TAG_BAD_CHARS for ch in t):
        return None
    return t


def normalize_tags(raw) -> tuple[list[str] | None, str | None]:
    """§8 标签数组规范化 → (去重后的标签表, 错误文案)。

    - 非数组 / 超上限（TAG_MAX_COUNT）→ 错误（**不静默截断**：悄悄丢掉第 21 个标签，
      用户会以为存上了）；
    - 单个标签非法 → 错误并点名（哪一条、为什么）；
    - 同名去重（保序）——写回是**全量替换**，重不重要靠配置而非重复项。"""
    if not isinstance(raw, list):
        return None, "tags 必须是数组"
    if len(raw) > TAG_MAX_COUNT:
        return None, f"一篇最多 {TAG_MAX_COUNT} 个标签（收到 {len(raw)}）"
    out: list[str] = []
    for one in raw:
        tag = normalize_tag(one)
        if tag is None:
            return None, (f"非法标签: {one!r}（≤{TAG_MAX_LEN} 字符；"
                          f"禁一切空白/控制字符与 #）")
        if tag not in out:            # 同名去重（保序：用户排的顺序有意义）
            out.append(tag)
    return out, None


def _tags_line_index(lines: list[str]) -> int:
    """头部区块里标签行的下标；没有 → -1（宽松匹配 `> 标签` 前缀，半角冒号也认）。"""
    for i, line in enumerate(lines[:HEAD_SCAN]):
        if line.startswith(TAGS_HEADER_LOOSE):
            return i
    return -1


def note_tags(text: str) -> list[str]:
    """§8 从笔记头块解析标签 → 标签表（**容错**：行缺失/未知格式都给空表或忽略）。

    口径：只认头部区块（与课程行同窗口）；行内以 `#` 起始、空白分隔的词才算标签；
    非 `#` 开头的词（`> 标签: 重点` 这类未知格式）忽略不计；非法词（超长/含空格）
    跳过；同名去重。存文件不存 sidecar：可移植、随编辑/移动/重命名存活。"""
    lines = text.splitlines()[:HEAD_SCAN]
    i = -1
    for k, line in enumerate(lines):
        if line.startswith(TAGS_HEADER_LOOSE):
            i = k
            break
    if i < 0:
        return []
    body = lines[i]
    if body.startswith(TAGS_HEADER_PREFIX):
        body = body[len(TAGS_HEADER_PREFIX):]
    elif ":" in body:                       # 半角冒号变体：`> 标签: #a`
        body = body.split(":", 1)[1]
    else:                                   # 没有冒号 = 未知格式，整行忽略
        return []
    out: list[str] = []
    for token in body.split():
        if not token.startswith("#"):
            continue                        # 未知格式（无 #）忽略，不猜
        tag = normalize_tag(token)
        if tag and tag not in out:
            out.append(tag)
    return out


def tags_line(tags: list[str]) -> str:
    """标签行的规范写法（不含行尾符号）。"""
    return TAGS_HEADER_PREFIX + " ".join(f"#{t}" for t in tags)


def _insert_index_after(lines: list[str], prefixes: tuple[str, ...]) -> int:
    """头部区块内「第一个命中 prefixes 的行」之后的位置；都没命中 → -1。"""
    for i, line in enumerate(lines[:HEAD_SCAN]):
        if any(line.startswith(p) for p in prefixes):
            return i + 1
    return -1


def set_tags(text: str, tags: list[str]) -> tuple[str, str]:
    """§8 写回标签行 → (新文本, 动作)。动作 = replaced / inserted / removed / none。

    规则：
    - 已有标签行 → 原行替换（行尾符号按原样保留）；tags 为空 → **删掉该行**（而不是
      留一行空标签）；
    - 没有标签行且 tags 非空 → 插到**课程行下方**（计划口径）；老笔记没有课程行时
      依次退到「自动生成行」之后、标题行之后、文首——插点永远落在头部区块内，
      与解析/改写同窗口；
    - 纯函数、不吃 IO：写盘与原子性由端点负责（main.note_tags_write）。

    注意：只动**头部那一行**。正文里恰好写成 `> 标签：#x` 的引用行不受影响（解析也
    只看头部窗口，读写同口径）。"""
    tags = list(tags)
    lines = text.splitlines(keepends=True)
    i = _tags_line_index(lines)
    if not tags:
        if i < 0:
            return text, "none"
        del lines[i]
        return "".join(lines), "removed"
    eol = "\r\n" if (i >= 0 and lines[i].endswith("\r\n")) else ("\n" if "\r\n" not in text
                                                                else "\r\n")
    new_line = tags_line(tags) + eol
    if i >= 0:
        if lines[i].rstrip("\r\n") == new_line.rstrip("\r\n"):
            return text, "none"          # 幂等：内容没变就不动文件（mtime 也不变）
        lines[i] = new_line
        return "".join(lines), "replaced"
    # 插点优先级：课程行下方（计划口径）→ 自动生成行之后 → 标题行之后 → 文首。
    # 注意要**按优先级逐个试**：一次性扫「命中任一前缀的第一行」会先撞上标题行，
    # 结果标签行跑到课程行上面去（实测踩过）。
    idx = -1
    for prefix in (COURSE_HEADER_PREFIX, AUTOGEN_PREFIX, TITLE_PREFIX):
        idx = _insert_index_after(lines, (prefix,))
        if idx >= 0:
            break
    lines.insert(idx if idx >= 0 else 0, new_line)
    return "".join(lines), "inserted"


def tag_counts(notes: list[dict]) -> list[dict]:
    """§8 全局标签清单 + 计数（筛选栏用）→ [{tag, count}]，按计数降序、同数按名。

    计数口径 = 传进来的这批笔记（调用方传"课程/搜索筛过、**未按标签筛**"的那批）：
    这样下拉里的数字正好是"选它能看到几篇"，与筛选结果自洽。"""
    counts: dict[str, int] = {}
    for n in notes:
        for t in n.get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": c}
            for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def parse(text: str) -> dict:
    """解析笔记正文 → {stamp, course, tags, pairs, segments, paired, has_zh}。

    pairs 为按 seg 升序的 [{"seg","ts","en","zh"}]；zh 可能为 None（该段未译
    或整篇纯英文）。解析是纯函数、不吃 IO，便于对任意文本单测。"""
    stamp = None
    ens: list[tuple[str, str]] = []      # 按落盘顺序 = seg 1..N
    zhs: dict[int, str] = {}
    loose_zh: list[str] = []
    for line in text.splitlines():
        if stamp is None and line.startswith(TITLE_PREFIX):
            stamp = line[len(TITLE_PREFIX):].strip()
            continue
        m = EN_RE.match(line)
        if m:
            ens.append((m.group(1), m.group(2).rstrip()))
            continue
        m = ZH_RE.match(line)
        if m:
            zhs[int(m.group(1))] = m.group(2).rstrip()
            continue
        m = ZH_LOOSE_RE.match(line)
        if m and m.group(1).strip():
            loose_zh.append(m.group(1).rstrip())
    # 无 #seg 的老格式 ZH：按顺序补给「还没有 ZH 的 EN」（点对点，不改变有标号的配对）
    if loose_zh:
        for i in range(1, len(ens) + 1):
            if not loose_zh:
                break
            if i not in zhs:
                zhs[i] = loose_zh.pop(0)
    pairs = [{"seg": i, "ts": ts, "en": en, "zh": zhs.get(i)}
             for i, (ts, en) in enumerate(ens, start=1)]
    return {
        "stamp": stamp,
        "course": note_course(text) if text else None,
        "tags": note_tags(text) if text else [],
        "pairs": pairs,
        "segments": len(pairs),
        "paired": sum(1 for p in pairs if p["zh"]),
        "has_zh": bool(zhs),
    }


def first_sentence(pairs: list[dict]) -> tuple[str, str]:
    """首句摘要：(en, zh)。取第一条非空 EN 及其配对 ZH，各截断到 FIRST_SENT_MAX。

    EN 优先（英文原文是笔记的主键）；ZH 仅作副标题，缺失时为空串。"""
    def cut(s: str) -> str:
        s = " ".join((s or "").split())
        return s[:FIRST_SENT_MAX] + ("…" if len(s) > FIRST_SENT_MAX else "")
    for p in pairs:
        if p["en"].strip():
            return cut(p["en"]), cut(p["zh"] or "")
    for p in pairs:                      # 极端：只有 ZH 没有 EN
        if (p["zh"] or "").strip():
            return "", cut(p["zh"])
    return "", ""


def display_date(stamp: str | None, name: str) -> str:
    """展示用日期。优先文件名里的 8+4 位时间戳，回退头部戳，再回退文件名原样。"""
    m = NOTE_RE.match(name)
    if m:
        d, t = m.group(1), m.group(2)
        return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:]}"
    if stamp and len(stamp) >= 13 and stamp[:8].isdigit():
        d, t = stamp[:8], stamp[9:13] if len(stamp) > 9 else stamp[8:]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:]}"
    return stamp or name


def _matches(query: str, blob: str) -> bool:
    """检索匹配：大小写不敏感的子串匹配。

    **中文靠子串匹配而非分词**——中文没有空格，`in` 天然支持任意中文片段
    （「神经网络」能命中「其余的神经网络继续运行」），不需要分词器；
    英文侧用 casefold 保证 `Attention`/`attention` 都能命中。"""
    return query in blob


def index(dir_path: Path, query: str | None = None,
          course: str | None = None, tag: str | None = None,
          limit: int | None = None) -> dict:
    """列出笔记索引（时间倒序）→ {"notes": [...], "tags": [...], "total", "matched"}。

    query：全文检索（课程名 + 标签名 + 文件名 + 全部 EN/ZH 正文，含中文子串）。
    course：按课程名精确过滤（侧栏课程列表下钻用）。
    tag：§8 按标签精确过滤（与 course/query **叠加**；空 = 不筛）。
    返回口径：total = 过 course/query 的篇数（**未**按标签筛）；matched = course/
    query/tag 全过、limit 之前的篇数；tags = 全局标签清单 + 计数（facet 口径见
    tag_counts，只受 course/query 影响——"选它能看到几篇"才和筛选结果自洽）。
    单文件解析失败只跳过该篇——索引是展示信息，不该因一篇坏文件整体 500。"""
    d = Path(dir_path)
    notes: list[dict] = []
    if not d.is_dir():
        return {"notes": [], "total": 0, "matched": 0, "tags": []}
    q = (query or "").strip().casefold()[:MAX_QUERY]
    want_course = (course or "").strip()
    want_tag = normalize_tag(tag) if tag else None
    for p in d.glob(NOTE_GLOB):
        if not is_note_file(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
            st = p.stat()
        except Exception:
            continue
        parsed = parse(text)
        name = p.name
        coursename = parsed["course"] or UNCATEGORIZED
        if want_course and coursename != want_course:
            continue
        if q:
            blob = "\n".join([name, coursename] + (parsed["tags"] or []) + [
                f"{x['en']}\n{x['zh'] or ''}" for x in parsed["pairs"]]).casefold()
            if not _matches(q, blob):
                continue
        en, zh = first_sentence(parsed["pairs"])
        stem = p.stem
        notes.append({
            "name": name,
            "date": display_date(parsed["stamp"], name),
            "stamp": parsed["stamp"],
            "course": coursename,
            "tags": list(parsed["tags"] or []),
            "first_en": en,
            "first_zh": zh,
            "segments": parsed["segments"],
            "translated": parsed["paired"],
            "has_summary": (d / f"{stem}{SUMMARY_SUFFIX}").is_file(),
            "has_deck": (d / f"{stem}.apkg").is_file(),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    total = len(notes)
    counts = tag_counts(notes)              # 计数在按 tag 过滤**之前**算（facet 口径）
    if want_tag:
        notes = [n for n in notes if want_tag in (n["tags"] or [])]
    # 排序：优先按**头部时间戳**（= 开课时间），无戳才回落文件名。§4 起笔记可以
    # 重命名成语义名（`lecture_第3讲.md`），若仍按文件名排，改个名就会在列表里
    # 跳到别的年份去——按开课时间排才稳。
    notes.sort(key=lambda n: (n.get("stamp") or n["name"], n["name"]), reverse=True)
    if limit and limit > 0:
        notes = notes[:limit]
    return {"notes": notes, "total": total, "matched": len(notes),
            "tag": want_tag or "", "tag_counts": counts}
