# -*- coding: utf-8 -*-
"""notes.py — p2-2 笔记库：transcripts/*.md 的解析、索引与检索（纯函数，可单测）。

笔记文件格式（由 main.Hub 落笔，见 main.note_header / Hub.append）：

    # 双语课堂笔记 20261013_0146        ← 时间戳

    > LectureLens 自动生成（EN 原文 / ZH 翻译）
    > 课程：计算机科学导论              ← p2-1 起（老笔记无此行 → 归「未分类」）

    ---

    - `01:46:42` All right.             ← EN 行（无 seg 标号）
      - `#1` 好的。                      ← ZH 行，带 #seg 锚点

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

#: 笔记文件名（也是本模块唯一接受的 name 形态）
NOTE_RE = re.compile(r"^lecture_(\d{8})_(\d{4})\.md$")
NOTE_GLOB = "lecture_*.md"
#: 正文行格式（与 main.Hub.append 的落笔格式成对，改动需同步）
EN_RE = re.compile(r"^- `([^`]+)` (.*)$")
ZH_RE = re.compile(r"^  - `#(\d+)` (.*)$")
ZH_LOOSE_RE = re.compile(r"^  - (?!#)(.*)$")   # 老格式：ZH 行没有 #seg 标号
TITLE_PREFIX = "# 双语课堂笔记 "

MAX_QUERY = 200        # 检索词长度上限（超长直接截断，避免无意义的超长匹配）
FIRST_SENT_MAX = 120   # 首句摘要截断长度


def safe_note_path(dir_path: Path, name) -> Path | None:
    """把请求里的笔记名解析成**目录内**的真实路径；非法/越界 → None。

    安全要害：`name` 直接来自 HTTP 查询参数，必须同时挡住
    ① 路径分隔符与 `..`（`../../.env` 之类）；
    ② 绝对路径（`C:/Windows/win.ini`）；
    ③ 不匹配 `lecture_YYYYMMDD_HHMM.md` 的文件名（config.json / 日志等一律读不到）；
    ④ 解析后父目录不等于 transcripts 目录（符号链接/等价路径兜底）。
    四道都过才返回路径——这是唯一允许读笔记文件的入口，端点层必须走它。"""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or len(n) > 128:
        return None
    if "/" in n or "\\" in n or ".." in n:
        return None
    if not NOTE_RE.match(n):
        return None
    d = Path(dir_path)
    cand = (d / n).resolve()
    if cand.parent != d.resolve():
        return None
    return cand


def is_note_file(p: Path) -> bool:
    """是否是「笔记正文」文件（排除 .summary.md 等派生物）。"""
    return bool(NOTE_RE.match(p.name))


def parse(text: str) -> dict:
    """解析笔记正文 → {stamp, course, pairs, segments, paired, has_zh}。

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
          course: str | None = None, limit: int | None = None) -> dict:
    """列出笔记索引（时间倒序）→ {"notes": [...], "total": int, "matched": int}。

    query：全文检索（课程名 + 文件名 + 全部 EN/ZH 正文，含中文子串）。
    course：按课程名精确过滤（侧栏课程列表下钻用）。
    单文件解析失败只跳过该篇——索引是展示信息，不该因一篇坏文件整体 500。"""
    d = Path(dir_path)
    notes: list[dict] = []
    if not d.is_dir():
        return {"notes": [], "total": 0, "matched": 0}
    q = (query or "").strip().casefold()[:MAX_QUERY]
    want_course = (course or "").strip()
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
            blob = "\n".join([name, coursename] + [
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
            "first_en": en,
            "first_zh": zh,
            "segments": parsed["segments"],
            "translated": parsed["paired"],
            "has_summary": (d / f"{stem}.summary.md").is_file(),
            "has_deck": (d / f"{stem}.apkg").is_file(),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    total = len(notes)
    notes.sort(key=lambda n: (n["name"]), reverse=True)   # 文件名含时间戳 → 天然按时间倒序
    if limit and limit > 0:
        notes = notes[:limit]
    return {"notes": notes, "total": total, "matched": total}
