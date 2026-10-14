# -*- coding: utf-8 -*-
"""tests/test_backup_zip.py — B10 一键打包备份（离线秒级）。

交付面（docs/notes-batch.md B10 行「一键打包备份：全部笔记打成 zip 落到导出目录
（换电脑/保险用）」+ 本条的 ask）：
- `POST /api/notes/export/batch` 加**显式**全库模式 `{all: true, fmt}`：服务端枚举
  transcripts/ 下全部 `lecture_*.md`（与 notes.index 同款非递归 glob + is_note_file，
  故 `.trash/` 与 `*.summary.md`/`*.bak.md` 等派生物**结构上**进不来）；
- 全库模式**绕开 NOTE_ZIP_MAX=50**（那是"一次挑几篇"的选包上限，整库备份要一篇不落）；
  缺 `files` 且没给 `all` 的既有 400 语义、以及显式列表的 50 篇上限**一字未变**；
- zip 名 `LectureLens备份_YYYYMMDD.zip`（落盘/下载两种模式同名口径）；
- 产物复用既有 `_build`/`exportdir.write_export`（落盘原子抢占 + 同名 -2）/下载响应。

三层：① 纯函数（文件名、全库枚举）；② 端点契约（60 篇过上限、.trash/派生物不进包、
空库 400、显式列表仍 50 封顶、all 必须布尔且不与 files 同时给）；③ index.html 静态
断言（本仓无 JS 测试运行器，前端条目一律静态断言）。
"""
import io
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

COURSE = "计算机科学导论"


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def write_note(name: str, stamp: str = "20260101_0900", course: str = COURSE) -> Path:
    """在（被 conftest 重定向到 tmp_path 的）TRANSCRIPTS 下落一篇最小笔记。"""
    d = main.TRANSCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# 双语课堂笔记 {stamp}", "",
             "> LectureLens 自动生成（EN 原文 / ZH 翻译）"]
    if course:
        lines.append(f"> 课程：{course}")
    lines += ["", "---", "", "- `01:00:00` hello world", "  - `#1` 你好世界"]
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_many(n: int) -> list[str]:
    """造 n 篇笔记（名字互不相同；stamp 同一天 → 条目名靠 dedupe 避让，正好一并覆盖）。"""
    return [write_note(f"lecture_b{i:03d}.md").name for i in range(n)]


def zip_entries(path_or_bytes) -> list[str]:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        with zipfile.ZipFile(io.BytesIO(bytes(path_or_bytes))) as zf:
            return zf.namelist()
    with zipfile.ZipFile(path_or_bytes) as zf:
        return zf.namelist()


# ---------------- 1) 纯函数 ----------------

def test_backup_zip_name_is_pinned():
    """名字就是"这是整库快照"的标签，与「笔记导出_」分开（纯函数，today 可注入）。"""
    assert main.backup_zip_name(date(2026, 10, 14)) == "LectureLens备份_20261014.zip"
    assert main.backup_zip_name().startswith("LectureLens备份_")
    assert main.zip_export_name(date(2026, 10, 14)) == "笔记导出_20261014.zip"   # 既有口径没动


def test_all_note_paths_excludes_trash_and_derived():
    """枚举口径 = notes.index 同一套：非递归 glob + is_note_file。

    `.trash/` 是子目录（非递归天然看不到）、`*.summary.md` / `*.bak.md` 是派生物
    （is_note_file 排除）——两条都不靠调用方记得过滤。"""
    write_note("lecture_20260101_0900.md")
    write_note("lecture_20260101_1000.md")
    write_note("lecture_20260101_0900.summary.md")                 # 派生物：不算
    write_note("lecture_20260101_0900.summary.bak.md")             # 派生物：不算
    write_note("lecture_20260101_0900.apkg")                       # 非 .md：不算
    trash = main.TRANSCRIPTS / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    write_note("lecture_20260101_0800.md")
    (trash / "lecture_20260101_0800.md").write_text(
        (main.TRANSCRIPTS / "lecture_20260101_0800.md").read_text(encoding="utf-8"),
        encoding="utf-8")
    got = [p.name for p in main.all_note_paths()]
    assert got == ["lecture_20260101_0800.md",                      # 排序确定（按名）
                   "lecture_20260101_0900.md", "lecture_20260101_1000.md"]


def test_all_note_paths_missing_dir_is_empty():
    assert main.all_note_paths() == []          # transcripts/ 不存在 → 空表（不抛）


# ---------------- 2) 端点：all 模式 ----------------

async def test_all_mode_passes_the_50_cap_and_packs_everything(monkeypatch, tmp_path):
    """**核心**：60 篇 > NOTE_ZIP_MAX=50 → all 模式照打整包（条数、条目、落盘名都对）。"""
    names = write_many(60)
    assert len(names) > main.NOTE_ZIP_MAX
    dest = tmp_path / "导出"
    monkeypatch.setattr(main, "export_target_dir", lambda: dest)
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"all": True, "fmt": "md"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["all"] is True and j["count"] == 60
    assert j["name"] == main.backup_zip_name()          # LectureLens备份_YYYYMMDD.zip
    out = Path(j["path"])
    assert out.parent == dest and out.is_file()
    entries = zip_entries(out)
    assert len(entries) == 60                           # 全部都在（一篇不少）
    assert len(set(entries)) == 60                      # 同名自动 -2，不互相覆盖


async def test_all_mode_skips_trash_and_derived_entries(monkeypatch, tmp_path):
    """回收站与派生物**不进包**（换电脑时不该把"已删的"和"摘要壳"一起带走）。"""
    write_note("lecture_20260101_0900.md")
    write_note("lecture_20260101_0900.summary.md")
    trash = main.TRANSCRIPTS / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "lecture_20260101_0700.md").write_text("# 双语课堂笔记 20260101_0700\n",
                                                    encoding="utf-8")
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path / "导出")
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"all": True})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    entries = zip_entries(Path(r.json()["path"]))
    assert len(entries) == 1
    assert all("summary" not in n and "0700" not in n for n in entries)


async def test_all_mode_empty_library_is_400(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path)
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"all": True})
    assert r.status_code == 400
    assert "还没有笔记" in r.json()["error"]


async def test_all_mode_download_uses_backup_name(monkeypatch):
    """未设导出位置 → 走浏览器下载，文件名同样带「LectureLens备份」前缀，包可解。"""
    write_many(3)
    monkeypatch.setattr(main, "export_target_dir", lambda: None)
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"all": True, "fmt": "md"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "filename*=utf-8''" in cd
    assert unquote(cd.split("filename*=utf-8''", 1)[1]).startswith("LectureLens备份_")
    assert len(zip_entries(r.content)) == 3


async def test_all_rejects_non_boolean_and_mixed_with_files(monkeypatch, tmp_path):
    """all 必须是布尔；给了 all:true 就不许再给 files（意图不明 → 明确 400，不猜）。"""
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path)
    names = write_many(2)
    async with asgi_client() as c:
        bad = await c.post("/api/notes/export/batch", json={"all": "yes"})
        mixed = await c.post("/api/notes/export/batch",
                             json={"all": True, "files": names})
        unknown = await c.post("/api/notes/export/batch",
                               json={"all": True, "nope": 1})
    assert bad.status_code == 400 and "非法 all" in bad.json()["error"]
    assert mixed.status_code == 400 and "不能同时给" in mixed.json()["error"]
    assert unknown.status_code == 400 and "未知字段" in unknown.json()["error"]


# ---------------- 3) 既有 A3 语义：一字未变 ----------------

async def test_explicit_files_still_capped_at_50(monkeypatch, tmp_path):
    """50 篇上限**仍然只作用于显式 files**：51 篇显式请求照旧 400（all 模式才是例外）。"""
    names = write_many(51)
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path / "导出")
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"files": names})
    assert r.status_code == 400
    assert str(main.NOTE_ZIP_MAX) in r.json()["error"] and "最多打包" in r.json()["error"]


async def test_missing_files_without_all_still_400(monkeypatch, tmp_path):
    """缺 files 且没给 all → 既有 400（客户端忘了带 all **不会**静默变成全库备份）。"""
    write_many(2)
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path)
    async with asgi_client() as c:
        r = await c.post("/api/notes/export/batch", json={"fmt": "md"})
        explicit = await c.post("/api/notes/export/batch",
                                json={"files": [], "fmt": "md"})
    assert r.status_code == 400 and "缺少 files" in r.json()["error"]
    assert explicit.status_code == 400 and "不能为空" in explicit.json()["error"]


async def test_unknown_field_and_bad_fmt_still_400(monkeypatch, tmp_path):
    write_many(1)
    monkeypatch.setattr(main, "export_target_dir", lambda: tmp_path)
    async with asgi_client() as c:
        f = await c.post("/api/notes/export/batch", json={"all": True, "fmt": "pdf"})
        u = await c.post("/api/notes/export/batch", json={"all": True, "x": 1})
    assert f.status_code == 400 and "只支持 md / srt" in f.json()["error"]
    assert u.status_code == 400 and "未知字段" in u.json()["error"]


# ---------------- 4) index.html：设置页「导出」卡的备份入口（静态断言） ----------------

def _export_card() -> str:
    """§3 导出卡原文：从它的标题到下一张卡（`<div class="card"`）——不能用 `</div>`
    夹取（卡里有多个 .row div，会在第一个就截断）。"""
    return HTML.split('<h2>导出 <span class="stateline" style="display:inline">§3', 1)[1] \
               .split('<div class="card"', 1)[0]


def test_backup_button_lives_in_the_export_card():
    card = _export_card()
    assert 'id="btnExportBackup"' in card
    assert "备份全部笔记" in card
    assert "$('btnExportBackup').onclick = () => exportAllBackup();" in HTML
    assert 'id="btnExportOpen"' in card          # 既有按钮一个没动


def test_backup_sends_all_true_and_mirrors_the_two_modes():
    fn = HTML.split("async function exportAllBackup() {", 1)[1].split("\n}", 1)[0]
    assert "JSON.stringify({ all: true, fmt: 'md' })" in fn
    assert "'/api/notes/export/batch'" in fn
    assert "if (backupBusy) return;" in fn                       # 防连点
    assert "已备份全部 ${j.count} 篇 → ${j.path}" in fn          # 落盘模式
    assert "{ label: '打开文件夹', fn: openExportDir }" in fn    # 复用既有打开动作
    assert "exportFilename(r, 'LectureLens备份', 'zip')" in fn   # 下载模式的文件名兜底
    assert "已备份全部笔记（${a.download}" in fn


def test_export_card_copy_states_the_backup_scope():
    """文案如实写明范围：整库、不受 50 篇限制、不含回收站与派生物（别让用户以为
    "备份"连摘要一起带走了）。"""
    card = _export_card()
    assert "备份全部笔记（B10）" in card
    assert "LectureLens备份_日期.zip" in card
    assert "不受「一次最多 50 篇」的选包上限约束" in card
    assert "不含回收站" in card
