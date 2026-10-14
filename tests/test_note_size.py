# -*- coding: utf-8 -*-
"""tests/test_note_size.py — B4 笔记体量徽章（离线秒级）。

交付面（docs/notes-batch.md B4 行「笔记行显示"体量"（段数/字数/时长小徽章）」）：
笔记列表每行加一枚体量徽章，文案 `N 段 · 约 M 字`。数据源是 `notes.parse` 汇总、
`notes.index` 下发的 `words` = 该篇 **EN+ZH 字符数**；段数沿用已有的 `segments`
（与预览页头部的「N 段 · 已译 M」同一个数）。

三层：① notes.parse/index 纯函数；② /api/notes/list 端点透传；③ index.html
结构/令牌断言（本仓没有 JS 测试运行器，前端条目一律静态断言——与
tests/test_theme_tokens.py 的口径一致；真浏览器里的字号量级与排版在报告里取证）。
"""
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

import main
import notes as notes_mod

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

NOTE = ("# 双语课堂笔记 20261013_0146\n\n"
        "> LectureLens 自动生成（EN 原文 / ZH 翻译）\n"
        "> 课程：未分类\n\n---\n\n"
        "- `10:00:01` All right.\n  - `#1` 好的。\n"
        "- `10:00:05` The rest go.\n  - `#2` 其余的继续。\n")
#: 上面两段的体量（= EN 字符 + ZH 字符；中文按字、英文按字符）
WANT_WORDS = (len("All right.") + len("好的。")
              + len("The rest go.") + len("其余的继续。"))


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def write_note(name: str, text: str = NOTE) -> Path:
    d = main.TRANSCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------- 1) 纯函数：words 口径 ----------------

def test_parse_words_is_en_plus_zh_chars():
    got = notes_mod.parse(NOTE)
    assert got["segments"] == 2
    assert got["words"] == WANT_WORDS == 31


def test_parse_words_ignores_unpaired_zh_leftovers():
    """只数**进 pairs 的行**：老格式里 ZH 行多于 EN 时，多余的不会出现在正文里，
    也就不能算进体量（否则徽章数字与预览看到的内容对不上）。"""
    text = ("- `t1` EN ONE\n  - 中文一\n"
            "- `t2` EN TWO\n  - 中文二\n  - 中文三\n")     # 3 条 ZH、2 条 EN
    got = notes_mod.parse(text)
    assert got["segments"] == 2 and [p["zh"] for p in got["pairs"]] == ["中文一", "中文二"]
    assert got["words"] == len("EN ONE") + len("中文一") + len("EN TWO") + len("中文二")
    assert got["words"] != len("EN ONE") + len("中文一") + len("EN TWO") + len("中文二") + len("中文三")


def test_parse_words_empty_note_is_zero():
    assert notes_mod.parse("")["words"] == 0


def test_index_reports_words_and_segments_together():
    """列表数据源：每条同时有段数与体量，两条不同大小的笔记各算各的。"""
    big = write_note("lecture_20260101_0900.md")
    write_note("lecture_20260202_1000.md", NOTE + "- `t3` Extra line here.\n")
    got = notes_mod.index(main.TRANSCRIPTS)
    by_name = {n["name"]: n for n in got["notes"]}
    small = by_name[big.name]
    assert small["segments"] == 2 and small["words"] == WANT_WORDS
    assert by_name["lecture_20260202_1000.md"]["segments"] == 3


async def test_list_endpoint_exposes_words():
    write_note("lecture_20260101_0900.md")
    async with asgi_client() as c:
        body = (await c.get("/api/notes/list")).json()
    assert body["ok"] and body["total"] == 1
    assert body["notes"][0]["words"] == WANT_WORDS
    assert body["notes"][0]["segments"] == 2


# ---------------- 2) index.html：行内徽章（静态断言） ----------------

def _row_body() -> str:
    return HTML.split("function noteRow(n) {", 1)[1].split("\n}", 1)[0]


def test_row_appends_size_badge_first():
    """体量徽章挂在状态徽章之前（它是信息，摘要/卡组是状态）。"""
    body = _row_body()
    assert "f.append(nbSize(n), nbFlag('摘要', n.has_summary), nbFlag('卡组', n.has_deck));" in body


def test_size_badge_copy_and_helpers():
    """文案 `N 段 · 约 M 字`（与预览页「N 段 · 已译 M」同族），空笔记退化成只报段数。"""
    size = HTML.split("function nbSize(n) {", 1)[1].split("\n}", 1)[0]
    assert "`${seg} 段 · ${nbWordsPhrase(w)}`" in size
    assert "w > 0 ?" in size                      # 无正文 → 不写「约 0 字」
    fmt = HTML.split("function nbWordsPhrase(w) {", 1)[1].split("\n}", 1)[0]
    assert "Math.round(w / 100) * 100" in fmt     # 四位数取整到百（"约"要站得住）
    assert "`约 ${(r / 10000).toFixed(1)} 万字`" in fmt   # 上万给一位小数（"2.4 万字"）
    assert "`约 ${Math.round(r / 10000)} 万字`" in fmt    # 十万+ 不再带小数位
    assert "`约 ${r} 字`" in fmt
    assert "className = 'nbFlag size'" in size    # 复用 .nbFlag 系


def test_size_badge_style_uses_tokens():
    """spec §六：徽章 = --surface-2 底 + --text-2 字（中性），不走 .nbFlag 的 ok 绿
    （绿在别处是"已生成"的语义，体量不是状态）。"""
    assert ".nbFlag.size { background:var(--surface-2); color:var(--text-2); }" in HTML
    css = HTML.split("</style>", 1)[0]
    rule = css.split(".nbFlag.size {", 1)[1].split("}", 1)[0]
    assert "var(--" in rule and "#" not in rule and "rgb" not in rule


def test_row_top_wraps_so_badges_never_overflow():
    """380px 列表里「日期 + 课程 + 三枚徽章」塞不进一行（真浏览器实测：内容 257px >
    可用 219px），故行顶必须可换行——徽章整组落到第二行右对齐，比溢出裁字好，
    顺带把课程名从 16px 的「…」腾回可读宽度。"""
    top = HTML.split(".nbRowTop {", 1)[1].split("}", 1)[0]
    assert "display:flex" in top and "flex-wrap:wrap" in top
