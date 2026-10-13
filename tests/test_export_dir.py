# -*- coding: utf-8 -*-
"""tests/test_export_dir.py — §3 导出位置自选 + 导出文件名（notes-batch）。

验收口径（docs/notes-batch.md §3）：
- config 新键 `export_dir`：白名单 + 专项校验（**绝对路径 / 可创建 / 可写 /
  非系统关键目录**）；null = 取消自选（回浏览器下载）；
- 设置后导出**直接落盘**（响应 JSON {ok, path}），未设置 = 维持浏览器下载（响应
  形态与旧行为一致）；文件名 = `{课程}_{日期}.{ext}`，课程为空维持原名；
- 文件名 sanitize（禁 `\\ / : * ? " < > |` 与系统保留名）；
- 桌面壳 js_api 的原生文件夹选择框（仅桌面版；浏览器/手机退化为手动输入）；
- `/api/export/open`（打开文件夹）**仅本机**可用、且不接受任何参数。

HTML 页面不在本文件断言（前端交互走真机手测清单）；此处覆盖全部纯逻辑与端点契约。
"""
import inspect
import json
import os
import secrets
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import desktop
import exportdir as ed
import main
import notes as notes_mod

NOTE = "lecture_20260101_0900.md"
COURSE = "计算机科学导论"


def write_note(name=NOTE, course=COURSE, en="Hello world.", zh="你好世界。",
               stamp="20260101_0900"):
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    lines = [f"# 双语课堂笔记 {stamp}", "",
             "> LectureLens 自动生成（EN 原文 / ZH 翻译）"]
    if course:
        lines.append(f"> 课程：{course}")
    lines += ["", "---", "", f"- `01:00:00` {en}"]
    if zh:
        lines.append(f"  - `#1` {zh}")
    p = main.TRANSCRIPTS / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def app_state(monkeypatch):
    """复位 runtime['config']（模块级可变状态）+ 打桩采集器重建（同 test_auth 口径）。

    teardown 要**逐层还原**：`dict(runtime)` 是浅拷贝——`saved['config']` 与
    `runtime['config']` 是同一个 dict 对象，只 update(saved) 等于把测试期间写进去的
    值留在原地（实测：本文件设过的 export_dir 泄漏进 test_exporters，那边两个导出
    用例拿到 JSON 而不是文件流）。故 config 内容另行深一层快照还原。"""
    monkeypatch.setattr(main, "apply_segmenter", lambda: None)
    monkeypatch.delenv("LECTURELENS_AUTH_NO_LOCAL_EXEMPT", raising=False)
    saved = dict(main.runtime)
    saved_cfg = dict(main.runtime["config"])
    main.runtime["config"].clear()
    main.runtime["config"].update(main.DEFAULT_CONFIG)
    main.runtime["reloading"] = False
    yield main
    main.runtime.clear()
    main.runtime.update(saved)
    main.runtime["config"].clear()
    main.runtime["config"].update(saved_cfg)


@asynccontextmanager
async def asgi_client(peer=("127.0.0.1", 123)):
    transport = httpx.ASGITransport(app=main.app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------- 纯函数：文件名 sanitize / {课程}_{日期} ----------------

def test_clean_component_replaces_forbidden_and_trims():
    assert ed.clean_component("AI/ML: 导论?") == "AI-ML- 导论"
    assert ed.clean_component('a<b>c|d"e*f') == "a-b-c-d-e-f"
    assert ed.clean_component("  数学  ") == "数学"          # 首尾空白
    assert ed.clean_component("名字. ") == "名字"            # 尾部点/空格（Windows 会静默裁掉）
    assert ed.clean_component("") == ""
    assert ed.clean_component(None) == ""
    assert ed.clean_component("***") == ""                   # 全是非法字符 → 空（调用方回落原名）


def test_clean_component_blocks_reserved_device_names():
    for raw in ("con", "CON", "prn", "aux", "nul", "com1", "LPT9"):
        assert ed.clean_component(raw) == "_" + raw.lower() or \
            ed.clean_component(raw).startswith("_"), raw
    assert ed.clean_component("console") == "console"        # 只是前缀相同，不是保留名


def test_clean_component_length_cap():
    assert len(ed.clean_component("长" * 200)) == ed.COMPONENT_MAX


def test_clean_component_strips_invisible_chars():
    """评审④：零宽/行分隔符不能让两个「看起来一样」的文件名并存；与标签侧共用
    notes.INVISIBLE_CHARS 一张表。"""
    import notes as notes_mod
    assert ed.clean_component("数学\u200b分析") == "数学-分析"
    assert ed.clean_component("a\u2028b") == "a-b"        # 行分隔符（会被某些工具当换行）
    assert "\u200b" not in ed.clean_component("x\u200by")
    for ch in notes_mod.INVISIBLE_CHARS:
        assert ch not in ed.clean_component(f"a{ch}b")


def test_name_problem_rejects_invisible_chars():
    """改名侧（§4）同口径：肉眼看一样的名字最难查，直接拒绝。"""
    import notes as notes_mod
    assert notes_mod.name_problem("lecture_数学\u200b分析.md") is not None
    assert notes_mod.name_problem("lecture_a\u2028b.md") is not None
    assert notes_mod.name_problem("lecture_正常名字.md") is None


def test_export_filename_course_and_date():
    p = Path(NOTE)
    assert ed.export_filename(p, COURSE, "pdf") == f"{COURSE}_20260101.pdf"
    assert ed.export_filename(p, "数学/分析", "md") == "数学-分析_20260101.md"


@pytest.mark.parametrize("course", [None, "", "   ", "未分类"])
def test_export_filename_keeps_original_when_course_empty(course):
    """课程为空维持原名（含「未分类」= 未选课：notes-batch §0 的既有口径）。"""
    assert ed.export_filename(Path(NOTE), course, "docx") == "lecture_20260101_0900.docx"


def test_export_filename_renamed_note_uses_head_stamp():
    """改名后的笔记（文件名没有时间戳）用笔记头的时间戳。"""
    name = ed.export_filename(Path("lecture_第3讲-神经网络.md"), "数学", "srt",
                              stamp="20260202_1400")
    assert name == "数学_20260202.srt"


def test_export_filename_falls_back_to_mtime_then_today(tmp_path):
    """没有文件名戳、也没有笔记头戳 → 用文件 mtime；连 mtime 都读不到 → 今天。"""
    import datetime
    f = tmp_path / "lecture_随手记.md"
    f.write_text("x", encoding="utf-8")
    want_mtime = datetime.date.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d")
    assert ed.export_filename(f, "数学", "md") == f"数学_{want_mtime}.md"
    assert ed.export_filename(f, "数学", "md",
                              today=datetime.date(2026, 3, 4)) == f"数学_{want_mtime}.md"
    assert ed.date_token(tmp_path / "不存在.md", None,
                         today=datetime.date(2026, 3, 4)) == "20260304"


def test_export_filename_extension_normalised():
    assert ed.export_filename(Path(NOTE), "数学", ".PDF") == "数学_20260101.pdf"
    assert ed.export_filename(Path(NOTE), "数学", "") == "数学_20260101.md"


# ---------------- 纯函数：export_dir 校验 ----------------

@pytest.mark.parametrize("raw", ["exports", "exports/notes", "./out", "C:relative",
                                 "", "   ", 123, True, None, ["D:\\x"]])
def test_path_problem_rejects_non_absolute_and_non_string(raw):
    assert ed.path_problem(raw) is not None


def test_path_problem_rejects_bad_segments():
    assert "路径段不得含" in ed.path_problem("C:" + os.sep + "a<b")
    assert ".." in ed.path_problem("D:" + os.sep + "exports" + os.sep + ".." + os.sep + "x")
    long = "D:" + os.sep + "x" * (ed.EXPORT_DIR_MAX + 10)
    assert "太长" in ed.path_problem(long)


@pytest.mark.parametrize("raw", [
    "\\\\?\\C:\\Windows\\Temp",        # 扩展路径前缀：绕过保留名/规范化的经典写法
    "\\\\.\\C:\\Windows\\Temp",        # 设备路径前缀
    "\\\\.\\PhysicalDrive0",           # 设备路径（盘设备）
    "\\\\?\\UNC\\server\\share\\x",    # 扩展形式的 UNC
    "//?/C:/Windows/Temp",             # 正斜杠写法
    "\\\\server\\share\\folder",       # 明文 UNC（评审：校验阶段就会发 SMB 探测）
    "//server/share/folder",
])
def test_path_problem_rejects_device_prefixes_and_unc(raw):
    """评审①（已实证）：`\\\\?\\C:\\Windows\\Temp` 曾能整条绕过系统目录黑名单并**真的写盘成功**。

    `Path('\\\\?\\C:\\Windows\\Temp').resolve()` 原样返回、`parents` 里没有 `C:\\Windows`
    ——黑名单比对随之失效。现在前缀一律拒绝；UNC 也拒绝（设置它就等于向该主机发起一次
    带当前用户凭据的 SMB 连接，而 /api/config 无 Origin 校验、环回豁免）。"""
    problem = ed.path_problem(raw)
    assert problem is not None, f"{raw} 必须被拒"
    assert "export_dir" in problem
    if "server" in raw or "share" in raw:
        assert "UNC" in problem or "盘符" in problem


def test_extended_prefix_strip_is_what_makes_the_blacklist_work():
    r"""机制说明（评审①的根因）：resolve() 对超长路径也会加 `\\?\`——比对前必须剥掉。"""
    assert ed._strip_extended(Path("\\\\?\\C:\\Windows\\Temp")) == Path("C:\\Windows\\Temp")
    assert ed._strip_extended(Path("\\\\?\\UNC\\srv\\share\\x")) == Path("\\\\srv\\share\\x")
    assert ed._strip_extended(Path("\\\\?\\C:\\x")) == Path("C:\\x")
    assert ed._strip_extended(Path("D:\\normal")) == Path("D:\\normal")   # 无前缀不动


def test_path_length_problem_reports_clearly(tmp_path):
    """评审④：最坏组合（长目录 + 长笔记名）要给出**可行动**文案，而不是裸 OSError。"""
    assert ed.path_length_problem(tmp_path, "数学_20260101.md") is None
    long_name = "长" * (ed.MAX_FULL_PATH)
    problem = ed.path_length_problem(tmp_path, long_name)
    assert problem is not None and "路径太长" in problem and "重命名" in problem
    # 预算算式自洽：目录上限 + 最坏文件名 + 临时名余量 不得超 260
    worst = ed.EXPORT_DIR_MAX + 1 + (100 - 3) + 1 + 4 + ed._TMP_EXTRA
    assert worst <= 260, f"长度预算算错: {worst}"


def test_path_problem_rejects_system_dirs(tmp_path, monkeypatch):
    """系统关键目录：**只禁本身**的卷根 + 连子目录一起禁的系统目录。

    用 monkeypatch 造一套假系统目录（不依赖真 C:\\Windows），口径 portable。"""
    fake_root = tmp_path / "fakeroot"
    fake_win = fake_root / "Windows"
    fake_win.mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(fake_win))
    monkeypatch.setenv("windir", str(fake_win))
    monkeypatch.setenv("SystemDrive", str(fake_root))
    monkeypatch.setenv("ProgramFiles", str(fake_root / "Program Files"))
    assert ed.path_problem(str(fake_win)) is not None                 # 系统目录本身
    assert ed.path_problem(str(fake_win / "Temp")) is not None        # 其子目录
    assert ed.path_problem(str(fake_root)) is not None                # 盘根本身
    assert ed.path_problem(str(fake_root / "LectureLens-导出")) is None  # 盘根下的普通目录


def test_path_problem_allows_normal_absolute_dir(tmp_path):
    assert ed.path_problem(str(tmp_path)) is None
    assert ed.path_problem(str(tmp_path / "导出" / "2026")) is None    # 还不存在也可（可创建）


# ---------------- 纯函数：可创建/可写实证、落盘、打开文件夹 ----------------

def test_ensure_writable_creates_nested_dirs_and_cleans_probe(tmp_path):
    d = tmp_path / "a" / "b" / "c"
    assert ed.ensure_writable(d) is None
    assert d.is_dir()
    left = [p.name for p in d.iterdir()]
    assert left == [], f"写探针必须删干净（现在用随机名 mkstemp）: {left}"


def test_probe_never_clobbers_a_user_file(tmp_path):
    """评审④（破坏性）：探针曾用固定名且写前不检查存在——用户目录里恰有同名文件
    （例如上次异常残留被改名归档）会被覆盖后删掉。现在走 mkstemp（独占 + 随机名）。"""
    victim = tmp_path / ".lecturelens-write-probe.tmp"      # 旧固定名
    victim.write_text("USER-DATA", encoding="utf-8")
    assert ed.ensure_writable(tmp_path) is None
    assert victim.is_file() and victim.read_text(encoding="utf-8") == "USER-DATA"


def test_ensure_writable_reports_unwritable_path(tmp_path):
    """目标位置是个**文件**（不是目录）→ 明确文案，且不猜、不覆盖。"""
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    problem = ed.ensure_writable(f)
    assert problem and "导出目录" in problem


def test_ensure_writable_mkdir_failure_is_reported(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    problem = ed.ensure_writable(tmp_path / "x")
    assert problem and "无法创建" in problem


def test_ensure_writable_probe_failure_is_reported(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise PermissionError("read-only")

    # 探针走 os.write(fd, ...)——打桩它才等于「探针写失败」
    monkeypatch.setattr(os, "write", boom)
    problem = ed.ensure_writable(tmp_path)
    assert problem and "不可写" in problem
    assert [p.name for p in tmp_path.iterdir()] == [], "失败的探针不能留下垃圾"


def test_dir_problem_is_syntax_plus_writability(tmp_path, monkeypatch):
    assert ed.dir_problem(str(tmp_path)) is None
    assert ed.dir_problem("relative") is not None
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(OSError("x")))
    assert ed.dir_problem(str(tmp_path / "y")) is not None


def test_unique_path_never_overwrites(tmp_path):
    (tmp_path / "数学_20260101.pdf").write_bytes(b"a")
    assert ed.unique_path(tmp_path, "数学_20260101.pdf").name == "数学_20260101-2.pdf"
    (tmp_path / "数学_20260101-2.pdf").write_bytes(b"b")
    assert ed.unique_path(tmp_path, "数学_20260101.pdf").name == "数学_20260101-3.pdf"
    assert ed.unique_path(tmp_path, "无扩展名").name == "无扩展名"        # 没有扩展名也安全


def test_write_export_is_atomic_and_leaves_no_tmp(tmp_path):
    out = ed.write_export(tmp_path, "笔记_20260101.md", b"# hello")
    assert out.read_bytes() == b"# hello"
    assert [p.name for p in tmp_path.iterdir()] == ["笔记_20260101.md"], "不得留 .tmp 残留"
    out2 = ed.write_export(tmp_path, "笔记_20260101.md", b"# second")
    assert out2.name == "笔记_20260101-2.md" and out.read_bytes() == b"# hello"


def test_write_export_creates_missing_dir(tmp_path):
    d = tmp_path / "新目录"
    assert ed.write_export(d, "x.md", b"1").is_file()


def test_reserve_path_is_atomic(tmp_path):
    """评审②：`unique_path` 是「先查后写」，并发下两个调用会拿到同一个名字；reserve_path
    用 O_CREAT|O_EXCL 让「查」与「占」成为一步（跨进程也成立）。"""
    a = ed.reserve_path(tmp_path, "数学_20260101.pdf")
    b = ed.reserve_path(tmp_path, "数学_20260101.pdf")
    assert {a.name, b.name} == {"数学_20260101.pdf", "数学_20260101-2.pdf"}
    assert a.is_file() and b.is_file()          # 都是空占位（不是同一份）


def test_write_export_concurrent_same_name_never_mixes(tmp_path):
    """评审②（已实证）：并发导出同一文件名曾共用一个 `<名>.<pid>.tmp`，产出两份内容的
    **混合体**（PDF/SRT 就是坏文件），且两次返回同一个路径。

    这里 8 线程各写 64KB 不同内容 → 必须是 8 个互不相同的文件、每份内容**逐字节等于**
    某一个请求（不得混合），且不留临时文件。旧实现会在这条上失败。"""
    import threading

    payloads = [bytes([65 + i]) * (64 * 1024) for i in range(8)]
    results: list = []
    errors: list = []

    def worker(body: bytes) -> None:
        try:
            results.append((ed.write_export(tmp_path, "数学_20260101.pdf", body), body))
        except Exception as e:                    # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len({p for p, _ in results}) == 8, "并发导出必须各占一个不同的落点"
    for path, body in results:
        assert path.read_bytes() == body, f"{path.name} 的内容被别的请求写花了"
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        sorted(p.name for p, _ in results), ".tmp / 占位文件不得残留"


def test_open_folder_missing_dir(tmp_path):
    assert ed.open_folder(tmp_path / "nope") is not None


def test_open_folder_calls_startfile(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)
    assert ed.open_folder(tmp_path) is None
    assert calls == [str(tmp_path)]
    monkeypatch.delattr(os, "startfile", raising=False)
    assert "Windows" in (ed.open_folder(tmp_path) or ""), "非 Windows 要给明确文案"


# ---------------- /api/config：export_dir 写入 ----------------

def test_export_dir_in_whitelist_and_default_none(app_state):
    assert "export_dir" in main.ALLOWED_CONFIG_KEYS          # 硬规矩⑦
    assert main.DEFAULT_CONFIG["export_dir"] is None


async def test_config_set_export_dir_persists(app_state, tmp_path):
    d = tmp_path / "导出"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"export_dir": str(d)})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "changed": {"export_dir": str(d)}}
    assert main.runtime["config"]["export_dir"] == str(d)
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["export_dir"] == str(d)


async def test_config_export_dir_creates_dir(app_state, tmp_path):
    """「可创建」是写进配置的**前提**：设置成功时目录已经在那儿了。"""
    d = tmp_path / "a" / "b"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"export_dir": str(d)})
    assert r.status_code == 200 and d.is_dir()


@pytest.mark.parametrize("value", [
    "exports/relative",          # 非绝对路径
    123,                         # 类型错
    True,                        # bool 不是路径
    "",                          # 空串（取消请用 null）
    "D:" + os.sep + "a<b",       # 非法字符
])
async def test_config_export_dir_invalid_400_no_pollution(app_state, value):
    snapshot = dict(main.runtime["config"])
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"export_dir": value})
    assert r.status_code == 400, f"{value!r} 应被拒: {r.text}"
    body = r.json()
    assert body["ok"] is False and "export_dir" in body["error"]
    assert main.runtime["config"] == snapshot
    assert not main.CONFIG_PATH.exists()          # 零污染（整体校验先行）


async def test_config_export_dir_system_dir_400(app_state, tmp_path, monkeypatch):
    fake_win = tmp_path / "fakeroot" / "Windows"
    fake_win.mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(fake_win))
    monkeypatch.setenv("windir", str(fake_win))
    monkeypatch.setenv("SystemDrive", str(tmp_path / "fakeroot"))
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"export_dir": str(fake_win / "Temp")})
    assert r.status_code == 400 and "系统关键目录" in r.json()["error"]
    assert main.runtime["config"]["export_dir"] is None


async def test_config_export_dir_unwritable_400(app_state, tmp_path):
    """目标是个文件（建不出目录）→ 400，且配置零污染（实证校验在赋值之前）。"""
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"export_dir": str(f)})
    assert r.status_code == 400, r.text
    assert "导出目录" in r.json()["error"]           # 明确文案（哪一步不通过看得见）
    assert main.runtime["config"]["export_dir"] is None
    assert not main.CONFIG_PATH.exists()            # 零污染：没写进 runtime，也没落盘


async def test_config_export_dir_null_clears(app_state, tmp_path):
    async with asgi_client() as c:
        r1 = await c.post("/api/config", json={"export_dir": str(tmp_path)})
        r2 = await c.post("/api/config", json={"export_dir": None})
        r3 = await c.post("/api/config", json={"export_dir": None})   # 已经是 null = no-op
        st = await c.get("/api/config")
    assert r1.status_code == 200
    assert r2.json() == {"ok": True, "changed": {"export_dir": None}}
    assert r3.json() == {"ok": True, "changed": {}}
    assert main.runtime["config"]["export_dir"] is None
    assert st.json()["export_dir"] is None


# ---------------- 导出端点：落盘 vs 浏览器下载 ----------------

async def test_export_to_dir_returns_json_and_writes_file(app_state, tmp_path):
    write_note()
    dest = tmp_path / "导出"
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(dest)})
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "md"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["fmt"] == "md"
    assert j["name"] == f"{COURSE}_20260101.md"          # {课程}_{日期}.{ext}
    assert Path(j["path"]) == dest / j["name"]
    assert j["dir"] == str(dest) and j["bytes"] > 0
    assert "Hello world." in Path(j["path"]).read_text(encoding="utf-8")


async def test_export_to_dir_path_too_long_is_400_with_clear_message(app_state, tmp_path,
                                                                   monkeypatch):
    """评审④：路径超限要 400 + 可行动文案（换目录/重命名），而不是裸 OSError 500。"""
    write_note()
    monkeypatch.setattr(ed, "MAX_FULL_PATH", 40)
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(tmp_path)})
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "md"})
    assert r.status_code == 400, r.text
    assert "路径太长" in r.json()["error"]
    assert not list(tmp_path.glob("*.md")), "超限时不该留下任何文件"


async def test_export_to_dir_second_export_does_not_overwrite(app_state, tmp_path):
    write_note()
    dest = tmp_path / "导出"
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(dest)})
        r1 = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "srt"})
        write_note(en="Second line.")
        r2 = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "srt"})
    n1, n2 = r1.json()["name"], r2.json()["name"]
    assert n1 == f"{COURSE}_20260101.srt" and n2 == f"{COURSE}_20260101-2.srt"
    assert "Hello world." in (dest / n1).read_text(encoding="utf-8-sig")
    assert "Second line." in (dest / n2).read_text(encoding="utf-8-sig")


async def test_export_to_dir_pdf_real_bytes(app_state, tmp_path):
    write_note()
    dest = tmp_path / "导出"
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(dest)})
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "pdf"})
    assert r.status_code == 200, r.text
    out = Path(r.json()["path"])
    assert out.suffix == ".pdf" and out.read_bytes()[:5] == b"%PDF-"


async def test_export_to_dir_recreates_deleted_dir(app_state, tmp_path):
    """导出位置被删/挪走后导出**仍可用**（自动补建），而不是 500。"""
    write_note()
    dest = tmp_path / "导出"
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(dest)})
        import shutil
        shutil.rmtree(dest)
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "md"})
    assert r.status_code == 200 and Path(r.json()["path"]).is_file()


async def test_export_download_mode_unchanged_when_unset(app_state):
    """未设置 export_dir = 维持浏览器下载：响应体是**文件本身**（不是 JSON），
    带 attachment 头，状态码 200——旧行为零变化。"""
    write_note()
    async with asgi_client() as c:
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "md"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "双语课堂笔记" in r.text
    async with asgi_client() as c:
        r2 = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "srt"})
    assert "attachment" in r2.headers.get("content-disposition", "")


async def test_export_download_filename_uses_course_and_date(app_state):
    """§3④ 文件名两种模式一致：中文课程名走 RFC 5987（filename* + ASCII 兜底）。"""
    write_note()
    async with asgi_client() as c:
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "pdf"})
    cd = r.headers["content-disposition"]
    assert "filename*=utf-8''" in cd
    assert COURSE in __import__("urllib.parse", fromlist=["unquote"]).unquote(cd)


async def test_export_download_filename_stays_original_for_uncategorized(app_state):
    write_note(course=None)
    async with asgi_client() as c:
        r = await c.get("/api/notes/export", params={"name": NOTE, "fmt": "pdf"})
    assert 'filename="lecture_20260101_0900.pdf"' in r.headers["content-disposition"]


def test_export_disposition_ascii_and_utf8():
    assert main.export_disposition("lecture_a.pdf") == 'attachment; filename="lecture_a.pdf"'
    got = main.export_disposition("数学_20260101.pdf")
    assert got.startswith('attachment; filename="___20260101.pdf"; filename*=utf-8\'\'%E6%95%B0')
    # 引号/反斜杠这类会破坏头结构的字符不进 ASCII 段（只替换，不裸奔进头里）
    quoted = main.export_disposition('a"b\\c.md')
    ascii_part = quoted.split("filename*=")[0]
    assert ascii_part == 'attachment; filename="a_b_c.md"; '
    assert quoted.count('"') == 2


# ---------------- /api/export/open：打开文件夹（仅本机） ----------------

async def test_export_open_loopback_calls_startfile(app_state, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)
    async with asgi_client() as c:
        await c.post("/api/config", json={"export_dir": str(tmp_path)})
        r = await c.post("/api/export/open")
    assert r.status_code == 200 and r.json() == {"ok": True, "path": str(tmp_path)}
    assert calls == [str(tmp_path)]


async def test_export_open_rejects_non_loopback(app_state, tmp_path, monkeypatch):
    """**仅本机**：LAN/手机侧不能远程在别人电脑上弹窗口。

    鉴权中间件的回退开关（auth_enabled=false）会全放行，故端点必须自己判对端——
    这里带合法 token 走非环回对端，验证拦的是**端点自己**那一层。"""
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)
    token = secrets.token_urlsafe(12)
    main.runtime["config"]["auth_token"] = token
    main.runtime["config"]["export_dir"] = str(tmp_path)
    async with asgi_client(peer=("192.168.1.5", 1234)) as c:
        r = await c.post("/api/export/open", headers={"X-LectureLens-Token": token})
    assert r.status_code == 403 and "仅本机" in r.json()["error"]
    assert calls == []


async def test_export_open_unset_409(app_state, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    async with asgi_client() as c:
        r = await c.post("/api/export/open")
    assert r.status_code == 409 and "未设置" in r.json()["error"]


async def test_export_open_missing_dir_409(app_state, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    gone = tmp_path / "gone"
    main.runtime["config"]["export_dir"] = str(gone)
    async with asgi_client() as c:
        r = await c.post("/api/export/open")
    assert r.status_code == 409 and "不存在" in r.json()["error"]


async def test_export_open_rejects_unknown_fields(app_state, tmp_path):
    """端点不接受任何参数（否则就是「用参数打开任意路径」的注入面）。"""
    main.runtime["config"]["export_dir"] = str(tmp_path)
    async with asgi_client() as c:
        r = await c.post("/api/export/open", json={"path": "C:" + os.sep + "Windows"})
    assert r.status_code == 400 and "未知字段" in r.json()["error"]


# ---------------- 桌面壳 js_api（原生文件夹选择框） ----------------

def _fake_webview(res, windows=True):
    class _FakeWin:
        def __init__(self):
            self.res, self.kinds = res, []

        def create_file_dialog(self, kind):
            self.kinds.append(kind)
            return self.res

    mod = types.ModuleType("webview")
    mod.FOLDER_DIALOG = object()
    win = _FakeWin()
    mod.windows = [win] if windows else []
    return mod, win


def test_desktop_api_pick_export_dir_states(monkeypatch):
    fake, win = _fake_webview(["D:\\LectureLens-导出"])
    monkeypatch.setitem(sys.modules, "webview", fake)
    api = desktop.DesktopApi()
    assert api.pick_export_dir() == "D:\\LectureLens-导出"
    assert win.kinds == [fake.FOLDER_DIALOG]

    win.res = None
    assert api.pick_export_dir() is None           # 取消
    win.res = ["E:\\x"]
    fake.windows = []
    assert api.pick_export_dir() is None           # 窗口未就绪：给 None 而不是抛


def test_desktop_api_survives_webview_errors(monkeypatch):
    class _Boom:
        @property
        def windows(self):
            raise RuntimeError("no gui")

    mod = types.ModuleType("webview")
    mod.FOLDER_DIALOG = object()
    mod.windows = _Boom()
    monkeypatch.setitem(sys.modules, "webview", mod)
    assert desktop.DesktopApi().pick_export_dir() is None


def test_desktop_window_wires_js_api():
    """主窗必须真的把 DesktopApi 挂上（否则页面上「选择文件夹…」永远是退化的）。"""
    src = inspect.getsource(desktop.run_desktop)
    assert "js_api=DesktopApi()" in src


def test_desktop_api_exposes_only_empty_arg_methods():
    """js_api 面纪律：只暴露零参数、无副作用的公有方法（不接受前端传路径）。"""
    public = [n for n, _ in inspect.getmembers(desktop.DesktopApi,
                                              predicate=inspect.isfunction)
              if not n.startswith("_")]
    assert public == ["pick_export_dir"]
    sig = inspect.signature(desktop.DesktopApi.pick_export_dir)
    assert list(sig.parameters) == ["self"]


# ---------------- 前端页面：导出卡与 toast 动作按钮的存在性 ----------------

def test_index_html_has_export_card_and_toast_action():
    html = (main.WEB / "index.html").read_text(encoding="utf-8")
    for anchor in ("setExportState", "btnExportPick", "btnExportOpen", "btnExportClear",
                   "setExportInput", "desktopApi()", "toastAct",
                   "/api/export/open", "pywebviewready"):
        assert anchor in html, f"index.html 缺少 {anchor}"


def test_notes_module_untouched_by_export_naming():
    """导出文件名是**导出侧**的事：不得改笔记文件本身（笔记名归 §4 重命名管）。"""
    p = write_note()
    before = p.read_bytes()
    ed.export_filename(p, COURSE, "pdf")
    assert p.read_bytes() == before
    assert notes_mod.is_note_file(p)
