# -*- coding: utf-8 -*-
"""courses.py — p2-1 课程管理：courses.json 存储 + 白名单校验 + 笔记归属。

数据模型（courses.json，与 config.json 同放仓库根）：

    {"active": "<course id> | null",
     "courses": [{"id": str, "name": str, "color": "#rrggbb", "glossary": "glossary/CS.md" | null}]}

- `glossary` 为**相对仓库根**的 .md 术语表路径；选中课程时该表喂给翻译侧
  （DeepSeekTranslator.glossary_files）。与既有 `--glossary` CLI 参数同格式，
  但存储值统一按仓库根解析（不依赖进程 CWD），读盘后转绝对路径再交给翻译器。
- `active` = 当前上课课程 id；**null = 「未分类」**（默认态，不选课程照常上课）。
- **笔记归属**：开课时把课程名写进笔记头 `> 课程：X`（见 COURSE_HEADER_PREFIX，
  由 main.Hub.start_transcript 落笔）。课程列表的笔记数由扫描 transcripts/*.md
  头部统计——不引入第二份 sidecar 索引文件，避免「笔记头/索引」双写不一致。

校验纪律对齐 main.ALLOWED_CONFIG_KEYS：未知键一律拒绝；逐键校验取值；**glossary
路径必须落在仓库根之内**（防 `../` 穿越把任意文件读进翻译 prompt）。

视觉口径（docs/design-spec.md）：课程颜色只用于侧栏 8px 圆点做身份标识，绝不
大面积铺底（§一-1/§一-2）；选中态仍走 --gold-dim/--gold（§六）。spec §二 的
语义令牌（--ok/--warn/--err）表达「状态」而非「身份」，故课程调色板另立一组
低饱和色，见 main 的 COURSE_PALETTE——这是对 spec 的**扩展**，不与之竞争。
"""
import json
import re
import secrets
from pathlib import Path

#: 笔记头里的课程标记行前缀（Hub.start_transcript 落笔 / 本模块解析统计，单一常量）
COURSE_HEADER_PREFIX = "> 课程："
#: 未选课程时的默认归属名（active=null 即此值）
UNCATEGORIZED = "未分类"

#: 单课程可用字段白名单（对齐 ALLOWED_CONFIG_KEYS 的纪律：未知键 400）
COURSE_ALLOWED_KEYS = ("name", "color", "glossary")

NAME_MAX = 60
MAX_COURSES = 50

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_color() -> str:
    """新建课程的默认色 = --text-2（中性灰蓝），避免抢金色的品牌/选中语义。"""
    return "#93a1b0"


def normalize_color(value) -> str | None:
    """把颜色规范化为小写 #rrggbb；非法返回 None（调用方据此报 400）。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v.lower() if _HEX_RE.match(v) else None


def normalize_name(value) -> str | None:
    """课程名规范化：去首尾空白、内部连续空白压成单空格；空/超长/非字符串 → None。"""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name or len(name) > NAME_MAX:
        return None
    return name


def glossary_problem(root: Path, rel) -> str | None:
    """校验术语表路径。返回错误文案；None = 合法或为空（空 = 不挂术语表）。

    安全要点：先拒字面 `..` 段（错误信息更直白），再用 resolve() + 包含性判定
    兜底（符号链接/等价路径都归一化到真实位置）——**目录外的文件一律读不进来**，
    因为该路径最终会被读进 system prompt。"""
    if rel is None:
        return None
    if not isinstance(rel, str):
        return "术语表路径必须是字符串"
    s = rel.strip()
    if not s:
        return None
    if ".." in Path(s).parts:
        return f"术语表路径不得包含 .. : {s}"
    cand = Path(s)
    cand = cand.resolve() if cand.is_absolute() else (Path(root) / s).resolve()
    root_r = Path(root).resolve()
    if cand != root_r and root_r not in cand.parents:
        return f"术语表必须位于项目目录内: {s}"
    if cand.suffix.lower() != ".md":
        return f"术语表必须是 .md 文件: {s}"
    return None


def resolve_glossary(root: Path, rel) -> str | None:
    """把存储的相对路径转为绝对路径字符串（喂给翻译器用）；空 → None。"""
    if not isinstance(rel, str) or not rel.strip():
        return None
    p = Path(rel.strip())
    return str(p if p.is_absolute() else (Path(root) / p))


def sanitize_course(raw) -> dict | None:
    """把一条（可能被手改坏的）课程记录规范化；不可救 → None（丢弃）。

    文件是用户可手改的输入，load() 必须把它当不可信数据处理——字段缺失/类型
    错误只丢该条，不炸整个列表。"""
    if not isinstance(raw, dict):
        return None
    cid = raw.get("id")
    name = normalize_name(raw.get("name"))
    if not isinstance(cid, str) or not cid.strip() or name is None:
        return None
    return {
        "id": cid.strip(),
        "name": name,
        "color": normalize_color(raw.get("color")) or default_color(),
        "glossary": (raw.get("glossary").strip()
                     if isinstance(raw.get("glossary"), str) and raw["glossary"].strip()
                     else None),
    }


def _sanitize(raw: dict) -> dict:
    """把已解析的顶层对象规范成 {"active","courses"}（逐条净化 + 上限 + 悬空 active 归 null）。"""
    data: dict = {"active": None, "courses": []}
    courses = []
    seen: set = set()
    for item in raw.get("courses") or []:
        c = sanitize_course(item)
        if c and c["id"] not in seen:   # 重复 id 只留第一条（id 是主键）
            seen.add(c["id"])
            courses.append(c)
        if len(courses) >= MAX_COURSES:
            break
    data["courses"] = courses
    active = raw.get("active")
    data["active"] = active if isinstance(active, str) and active in seen else None
    return data


def load_checked(path: Path) -> tuple[dict, str | None]:
    """读 courses.json → (数据, 错误)。**写端点必须用这个**。

    与 load() 的区别：文件**存在但读不出来**（IO 错、非法 JSON、顶层不是对象）时
    返回明确错误，而不是静默当成空表。

    为什么这条是硬要求（评审1 实测复现）：写端点的模式是 load → 改 → save。
    若坏文件被静默当成空表，save 就会把这份空表**落盘覆盖**——实测把 courses.json
    截断成非法 JSON 后 POST 一个新课，返回 200 且原课程全部消失；甚至 POST
    /api/courses/active {active:null}（取消选课）就能把整张表抹成 0 门课，且无任何
    错误提示。触发条件不需要手改文件：Windows 上编辑器/杀软瞬时占用导致 read_text
    抛错即可。"""
    p = Path(path)
    if not p.is_file():
        return {"active": None, "courses": []}, None      # 真·首次运行：空表合法
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return ({"active": None, "courses": []},
                f"课程表已存在但无法解析（拒绝写回以免覆盖）: {type(e).__name__}: {e}")
    if not isinstance(raw, dict):
        return ({"active": None, "courses": []},
                "课程表格式错误（顶层不是 JSON 对象），拒绝写回以免覆盖")
    return _sanitize(raw), None


def load(path: Path) -> dict:
    """读 courses.json → {"active": id|None, "courses": [...]}（**只读场景**用）。

    解析失败退化为空表——但任何**写**路径必须改用 load_checked()，否则会把这份
    空表写回、静默抹掉整张课程表（见 load_checked 的说明）。"""
    return load_checked(path)[0]


def save(path: Path, data: dict) -> None:
    """原子落盘：先写同目录 .tmp 再 os.replace，避免写一半被断电/崩溃截断。"""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"active": data.get("active"),
                               "courses": data.get("courses") or []},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def new_id(name: str, existing: set) -> str:
    """由课程名派生可读 id（纯 ASCII 才可派生，中文名回落随机）；冲突追加 -2/-3…"""
    slug = "-".join(filter(None, (
        "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "-"
                for ch in name.lower()).split("-"))))[:32].strip("-")
    base = slug or ("c" + secrets.token_hex(3))
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def find(data: dict, cid: str) -> dict | None:
    """按 id 取课程；不存在 → None。"""
    for c in data.get("courses") or []:
        if c.get("id") == cid:
            return c
    return None


def validation_error(payload, root: Path) -> tuple[dict, str | None]:
    """校验一条课程写入载荷（创建/更新共用）。

    返回 (规范化结果, 错误文案)。语义：
    - 未知键（不在 COURSE_ALLOWED_KEYS）→ 错误（白名单纪律，绝不静默忽略）；
    - 缺省键 = 不改（更新）或取默认（创建），由调用方按 `present` 判断；
    - name 必须合法（创建时必填，由调用方保证 present）。"""
    if not isinstance(payload, dict):
        return {}, "请求体必须是 JSON 对象"
    for k in payload:
        if k not in COURSE_ALLOWED_KEYS:
            return {}, (f"未知课程字段: {k}（允许: {list(COURSE_ALLOWED_KEYS)}）")
    out: dict = {}
    if "name" in payload:
        name = normalize_name(payload.get("name"))
        if name is None:
            return {}, f"非法课程名（须为 1–{NAME_MAX} 字符的非空字符串）"
        out["name"] = name
    if "color" in payload:
        color = normalize_color(payload.get("color"))
        if color is None:
            return {}, f"非法颜色: {payload.get('color')!r}（须为 #rrggbb 十六进制）"
        out["color"] = color
    if "glossary" in payload:
        g = payload.get("glossary")
        problem = glossary_problem(root, g)
        if problem:
            return {}, problem
        out["glossary"] = (g.strip() if isinstance(g, str) and g.strip() else None)
    return out, None


def note_course(text: str) -> str | None:
    """从笔记文本头部解析课程名；无标记 → None。

    只扫前若干行（头部区域），避免正文里恰好出现同前缀的引用行被误认。"""
    for line in text.splitlines()[:12]:
        if line.startswith(COURSE_HEADER_PREFIX):
            return line[len(COURSE_HEADER_PREFIX):].strip() or None
    return None


def rename_in_notes(dir_path: Path, old: str, new: str) -> int:
    """课程改名时同步已写笔记头里的课程名；返回改动文件数。

    必要性：笔记归属按**课程名**记录，改名后不回溯就会让历史笔记永远挂在旧名下
    （按名统计漏掉它们）。只替换头部那**一行**（note_course 命中的那行），正文与
    课程切换标记一律不动；行尾符号（\\n / \\r\\n）按原样保留。"""
    n = 0
    d = Path(dir_path)
    if not d.is_dir() or not old or not new or old == new:
        return 0
    for f in d.glob("lecture_*.md"):
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if note_course(text) != old:
            continue
        lines = text.splitlines(keepends=True)
        hit = None
        for i, line in enumerate(lines[:12]):
            body = line[len(COURSE_HEADER_PREFIX):] if line.startswith(
                COURSE_HEADER_PREFIX) else None
            if body is not None and body.strip() == old:
                hit = i
                break
        if hit is None:
            continue
        eol = "\r\n" if lines[hit].endswith("\r\n") else "\n"
        lines[hit] = f"{COURSE_HEADER_PREFIX}{new}{eol}"
        try:
            f.write_text("".join(lines), encoding="utf-8")
            n += 1
        except Exception:
            continue
    return n


def count_notes(dir_path: Path) -> dict:
    """统计 transcripts 目录下每个课程名的笔记数 → {课程名: 条数}。

    口径与笔记头解析同源（单一事实来源 = 文件本身）。目录不存在/单文件读失败
    一律静默跳过——统计是展示信息，不该让课程接口 500。"""
    counts: dict = {}
    d = Path(dir_path)
    if not d.is_dir():
        return counts
    for f in d.glob("lecture_*.md"):
        try:
            name = note_course(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts
