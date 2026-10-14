# -*- coding: utf-8 -*-
"""tests/test_pending_notes.py — B6 未归档轻提醒（离线秒级）。

交付面（docs/notes-batch.md B6 行「未归档轻提醒」+ 本条的 ask）：
- 判据：**近 2 天**（今天 + 昨天，按笔记 stamp 的日期前缀与本地当天比）存在
  `has_summary=false` 的笔记；数据源 `/api/notes/list`（stamp/has_summary 现成）；
- 形态：顶栏 chips 区一条**可点**轻提示「N 篇新笔记未归档 · 点此处理」→ 跳笔记库
  （无「未归档」筛选 → 兜底：跳列表 + toast 列出篇名）；本会话点过即收起（不落盘）；
- 口径边界：「未导出」本轮**不做**（本仓没有导出记录），文案只承诺摘要维度。

三层：① `notes.recent_unarchived` / `pending_payload` 纯函数（**today 可注入**，
跨日期边界逐个钉住）；② `/api/notes/list` 的 pending 段（与筛选无关）；③ index.html
结构断言（本仓没有 JS 测试运行器，前端条目一律静态断言，与其余条目同口径）。
"""
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx

import main
import notes as notes_mod

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def item(name: str, stamp, segments: int = 3, has_summary: bool = False) -> dict:
    """索引条目的最小替身（只有 B6 用得到的那几个键）。

    非字符串戳（老代码/手改文件都可能出现）也要能构造出来——display_date 只保证
    接受 str|None，这里不为它做替身兜底。"""
    return {"name": name, "stamp": stamp, "segments": segments,
            "has_summary": has_summary,
            "date": notes_mod.display_date(stamp, name) if isinstance(stamp, str) else ""}


def stamp_of(d: date, hhmm: str = "0900") -> str:
    return f"{d:%Y%m%d}_{hhmm}"


# ---------------- 1) 纯函数：近 N 天判定（跨日期边界） ----------------

def test_recent_unarchived_today_and_yesterday_only():
    """「近 2 天」= 今天 + 昨天：前天起一律不算（边界就钉在这里）。"""
    today = date(2026, 10, 14)
    rows = [item("a.md", stamp_of(today)),                       # 今天
            item("b.md", stamp_of(today - timedelta(days=1))),    # 昨天
            item("c.md", stamp_of(today - timedelta(days=2))),    # 前天 → 不算
            item("d.md", stamp_of(today - timedelta(days=9)))]    # 更早 → 不算
    got = [n["name"] for n in notes_mod.recent_unarchived(rows, today=today)]
    assert got == ["a.md", "b.md"]


def test_recent_unarchived_month_and_year_boundary():
    """跨月/跨年边界（判据是真实日期差，不是字符串前缀）。"""
    for today, yes, no in ((date(2026, 3, 1), "20260228_2359", "20260227_2359"),
                           (date(2026, 1, 1), "20251231_0800", "20251230_0800")):
        rows = [item("y.md", yes), item("n.md", no)]
        assert [x["name"] for x in notes_mod.recent_unarchived(rows, today=today)] == ["y.md"]


def test_recent_unarchived_skips_summarized_and_empty_note():
    """有摘要的（已归档）与**没有正文**的（空壳，无从归档）都不算提醒。"""
    today = date(2026, 10, 14)
    rows = [item("done.md", stamp_of(today), has_summary=True),
            item("empty.md", stamp_of(today), segments=0),
            item("todo.md", stamp_of(today))]
    assert [n["name"] for n in notes_mod.recent_unarchived(rows, today=today)] == ["todo.md"]


def test_recent_unarchived_skips_future_and_unusable_stamp():
    """未来戳（时钟回拨/手改）、缺戳、假日期（20260230）都不算——宁可漏报不误报。"""
    today = date(2026, 10, 14)
    rows = [item("future.md", stamp_of(today + timedelta(days=1))),
            item("nostamp.md", None),
            item("badcal.md", "20260230_0900"),
            item("short.md", "2026"),
            item("notstr.md", 20261014),
            item("ok.md", stamp_of(today))]
    assert [n["name"] for n in notes_mod.recent_unarchived(rows, today=today)] == ["ok.md"]


def test_recent_unarchived_sorted_newest_first():
    today = date(2026, 10, 14)
    rows = [item("y.md", stamp_of(today - timedelta(days=1))),
            item("t1.md", f"{today:%Y%m%d}_0900"),
            item("t2.md", f"{today:%Y%m%d}_1800")]
    assert [n["name"] for n in notes_mod.recent_unarchived(rows, today=today)] \
        == ["t2.md", "t1.md", "y.md"]


def test_pending_payload_shape_and_default_window():
    today = date(2026, 10, 14)
    p = notes_mod.pending_payload([item("a.md", stamp_of(today))], today=today)
    assert p == {"count": 1, "days": notes_mod.RECENT_DAYS,
                 "notes": [{"name": "a.md", "date": "2026-10-14 09:00"}]}
    assert notes_mod.RECENT_DAYS == 2
    assert notes_mod.pending_payload([], today=today) == {
        "count": 0, "days": 2, "notes": []}


# ---------------- 2) 端点：/api/notes/list 的 pending 段 ----------------

def write_note(name: str, stamp: date, body_seg: bool = True) -> Path:
    d = main.TRANSCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# 双语课堂笔记 {stamp_of(stamp)}", "",
             "> LectureLens 自动生成（EN 原文 / ZH 翻译）", "> 课程：数学", "", "---", ""]
    if body_seg:
        lines += ["- `10:00:01` hello", "  - `#1` 你好"]
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


async def test_list_endpoint_reports_recent_unarchived():
    today = date.today()
    write_note("lecture_a.md", today)                                # 未归档 → 计入
    write_note("lecture_b.md", today - timedelta(days=5))            # 太旧 → 不计
    write_note("lecture_c.md", today, body_seg=False)                # 空壳 → 不计
    p_done = write_note("lecture_d.md", today)
    (main.TRANSCRIPTS / (p_done.stem + notes_mod.SUMMARY_SUFFIX)).write_text(
        "s", encoding="utf-8")                                      # 有摘要 → 不计
    async with asgi_client() as c:
        body = (await c.get("/api/notes/list")).json()
    assert body["ok"]
    pend = body["pending"]
    assert pend["count"] == 1 and pend["days"] == 2
    assert [x["name"] for x in pend["notes"]] == ["lecture_a.md"]
    assert pend["notes"][0]["date"].startswith(f"{today:%Y-%m}-")


async def test_pending_is_independent_of_filters():
    """换个筛选词/课程不该改变"库里有几篇没归档"——它不是列表 facet。"""
    today = date.today()
    write_note("lecture_a.md", today)
    async with asgi_client() as c:
        base = (await c.get("/api/notes/list")).json()["pending"]["count"]
        for qs in ({"q": "不存在的词"}, {"course": "别的课"}, {"tag": "没有这个标签"}):
            got = (await c.get("/api/notes/list", params=qs)).json()
            assert got["pending"]["count"] == base == 1, qs


async def test_list_without_transcripts_dir_still_has_pending_shape():
    async with asgi_client() as c:
        body = (await c.get("/api/notes/list")).json()
    assert body["pending"] == {"count": 0, "days": 2, "notes": []}


# ---------------- 3) index.html：顶栏可点 chips（静态断言） ----------------

def _chip_span() -> str:
    return HTML.split('<span class="chips">', 1)[1].split("</header>", 1)[0]


def test_pending_chip_is_a_button_in_header_chips():
    """chips 区里唯一的**可点**徽章：button + .chip.btn + 默认 hidden（零变化）。"""
    chips = _chip_span()
    assert '<button class="chip btn" id="chipPending" hidden' in chips
    assert "chipDevice" in chips and "chipTranslate" in chips   # 原有状态徽章一个没动
    rule = HTML.split(".chip.btn {", 1)[1].split("}", 1)[0]
    assert "cursor:pointer" in rule
    hover = HTML.split(".chip.btn:hover {", 1)[1].split("}", 1)[0]
    assert "var(--text-1)" in hover and "color-mix(in srgb, var(--gold) 50%" in hover
    assert "#" not in hover and "rgb(" not in hover             # §一-4：色值只走令牌


def test_pending_chip_copy_count_and_current_note_exclusion():
    """文案「N 篇新笔记未归档 · 点此处理」；正在记录的那篇不算（否则每次开课都提醒）。"""
    body = HTML.split("function renderPendingChip(data) {", 1)[1].split("\n}", 1)[0]
    assert "`${n} 篇新笔记未归档 · 点此处理`" in body
    assert "n.name !== cur" in body and "el.hidden = n === 0 || pendingMuted" in body
    assert "const cur = (data && data.current) || null;" in body


def test_pending_chip_click_goes_to_notes_and_mutes():
    """点击 → 笔记库 + toast 列篇名；本会话点过即收起（pendingMuted，不落盘）。

    评审1③ 修复：点击还必须**清掉三条筛选**——未归档计数是整库口径，列表是筛选后的，
    不清就会出现"toast 说有 3 篇、列表里只有 1 篇别的课"。"""
    click = HTML.split("$('chipPending').onclick = () => {", 1)[1].split("};", 1)[0]
    assert "pendingMuted = true" in click
    assert "openWorkbench('notes')" in click
    assert "showToast(pendingToastText(hadFilter)" in click
    for clear in ("NB.query = ''", "NB.course = ''", "NB.tag = ''",
                  "$('nbSearch').value = ''", "$('nbCourseFilter').value = ''",
                  "$('nbTagFilter').value = ''"):
        assert clear in click, f"点击应清筛选：{clear}"
    assert "localStorage" not in click                          # 「不落盘」= 不碰存储
    toast = HTML.split("function pendingToastText(cleared) {", 1)[1].split("\n}", 1)[0]
    assert "未归档：" in toast and "摘要" in toast
    assert "已清掉筛选" in toast and "cleared" in toast          # 有筛选时才提这一句
    assert "导出" not in toast                                   # 只承诺摘要维度（无导出记录）


def test_pending_chip_reads_the_list_endpoint_and_is_refreshed_on_events():
    """数据源 = /api/notes/list；四个触点各刷一次（启动 / 笔记事件 / 列表刷新 / 下课收尾）。"""
    body = HTML.split("async function refreshPending() {", 1)[1].split("\n}", 1)[0]
    assert "'/api/notes/list'" in body and "renderPendingChip(r)" in body
    assert HTML.count("refreshPending();") >= 3          # 启动引导 + WS notes + 下课收尾
    assert "refreshPending();        // B6：启动即判" in HTML
    assert "renderPendingChip(r);" in HTML               # refreshNotes 复用同一份响应
