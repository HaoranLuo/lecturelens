# -*- coding: utf-8 -*-
"""tests/test_courses.py — p2-1 课程管理：存储/校验/CRUD/笔记归属（离线秒级）。

覆盖三层：
1) courses.py 纯函数（load/save 容错、白名单校验、glossary 路径包含性、笔记头解析）；
2) /api/courses* 端点（经 httpx ASGITransport，环回对端 → 条目19 鉴权豁免）；
3) 笔记归属（Hub.start_transcript 落课程头 / retag_course 的 empty↔marked 分流 /
   改名回溯 rename_in_notes）。

路径全部经 conftest 重定向到 tmp_path（CONFIG_PATH / TRANSCRIPTS / COURSES_PATH），
另有会话级 repo_courses_guard 兜底断言仓库真实 courses.json 未被碰。
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import courses as courses_mod
import main

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


def block(marker: str, stop: str) -> str:
    """取 [marker, stop) 之间的片段（本仓既有的 split 夹取手法，先断言 marker 存在）。"""
    assert marker in HTML, f"index.html 缺少 {marker}"
    return HTML.split(marker, 1)[1].split(stop, 1)[0]


@pytest.fixture
def cpath(tmp_path, monkeypatch):
    """课程表路径（conftest 已重定向；此处返回便于直接读写断言）。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)   # glossary 包含性判定以 ROOT 为界
    return main.COURSES_PATH


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# —— 1) 存储层 ——

def test_load_missing_file_is_empty(cpath):
    data = courses_mod.load(cpath)
    assert data == {"active": None, "courses": []}


def test_save_load_roundtrip(cpath):
    data = {"active": "cs101", "courses": [
        {"id": "cs101", "name": "计算机科学导论", "color": "#7fa6d9",
         "glossary": "glossary/CS.md"}]}
    courses_mod.save(cpath, data)
    assert courses_mod.load(cpath) == data


def test_save_is_atomic_no_tmp_leftover(cpath):
    """原子替换：落盘后不得残留 .tmp（否则说明 replace 没走成）。"""
    courses_mod.save(cpath, {"active": None, "courses": []})
    assert cpath.is_file()
    assert not cpath.with_name(cpath.name + ".tmp").exists()


def test_load_tolerates_corrupt_file(cpath):
    """手改坏的 courses.json 不得让服务起不来：退化为空表。"""
    cpath.write_text("{ this is not json", encoding="utf-8")
    assert courses_mod.load(cpath) == {"active": None, "courses": []}


def test_load_sanitizes_junk_entries(cpath):
    """逐条净化：缺 id / 缺 name / 非对象一律丢弃，合法项保留。"""
    cpath.write_text(json.dumps({
        "active": "ghost",   # 指向不存在的课程 → 归 null
        "courses": [
            {"id": "ok", "name": "  信号 与 系统  ", "color": "NOPE"},
            {"id": "", "name": "无 id"},
            {"name": "无 id 字段"},
            "不是对象",
            {"id": "noname"},
        ]}), encoding="utf-8")
    data = courses_mod.load(cpath)
    assert [c["id"] for c in data["courses"]] == ["ok"]
    assert data["courses"][0]["name"] == "信号 与 系统"          # 空白压成单空格
    assert data["courses"][0]["color"] == courses_mod.default_color()  # 非法色回落
    assert data["active"] is None                                # 悬空 active 归 null


def test_load_dedupes_ids_and_caps_count(cpath):
    many = [{"id": "dup", "name": f"课{i}"} for i in range(courses_mod.MAX_COURSES + 10)]
    cpath.write_text(json.dumps({"active": None, "courses": many}), encoding="utf-8")
    data = courses_mod.load(cpath)
    assert len(data["courses"]) == 1                 # 重复 id 只留第一条
    assert len(courses_mod.load(cpath)["courses"]) <= courses_mod.MAX_COURSES


# —— 2) 校验纯函数 ——

def test_validation_rejects_unknown_key(tmp_path):
    fields, err = courses_mod.validation_error({"name": "A", "evil": 1}, tmp_path)
    assert fields == {} and "未知课程字段" in err


def test_validation_name_rules(tmp_path):
    assert courses_mod.validation_error({"name": "  "}, tmp_path)[1]
    assert courses_mod.validation_error({"name": "x" * 61}, tmp_path)[1]
    assert courses_mod.validation_error({"name": 123}, tmp_path)[1]
    assert courses_mod.validation_error({"name": " 计算 机 "}, tmp_path)[0]["name"] == "计算 机"


def test_validation_color_rules(tmp_path):
    assert courses_mod.validation_error({"color": "red"}, tmp_path)[1]
    assert courses_mod.validation_error({"color": "#12345"}, tmp_path)[1]
    # 合法色规范化成小写
    assert courses_mod.validation_error({"color": "#AABBCC"}, tmp_path)[0]["color"] == "#aabbcc"


def test_glossary_path_traversal_rejected(tmp_path):
    """核心安全断言：glossary 路径不得逃出项目根（该文件会被读进 system prompt）。"""
    for bad in ("../secret.md", "..\\secret.md", "glossary/../../outside.md",
                "C:/Windows/win.ini", "/etc/passwd"):
        assert courses_mod.glossary_problem(tmp_path, bad), f"应拒绝: {bad}"
    # 合法的相对路径放行
    assert courses_mod.glossary_problem(tmp_path, "glossary/CS.md") is None
    assert courses_mod.glossary_problem(tmp_path, "") is None
    assert courses_mod.glossary_problem(tmp_path, None) is None


def test_glossary_must_be_markdown(tmp_path):
    assert courses_mod.glossary_problem(tmp_path, "glossary/CS.txt")
    assert courses_mod.glossary_problem(tmp_path, "notes.exe")


def test_glossary_absolute_path_inside_root_allowed(tmp_path):
    inside = tmp_path / "glossary" / "CS.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("attention = 注意力\n", encoding="utf-8")
    assert courses_mod.glossary_problem(tmp_path, str(inside)) is None


def test_resolve_glossary_makes_absolute(tmp_path):
    got = courses_mod.resolve_glossary(tmp_path, "glossary/CS.md")
    assert got == str((tmp_path / "glossary" / "CS.md").resolve())
    assert courses_mod.resolve_glossary(tmp_path, None) is None
    assert courses_mod.resolve_glossary(tmp_path, "  ") is None


def test_new_id_slug_and_dedup():
    assert courses_mod.new_id("CS101", set()) == "cs101"
    assert courses_mod.new_id("CS101", {"cs101"}) == "cs101-2"
    assert courses_mod.new_id("CS101", {"cs101", "cs101-2"}) == "cs101-3"
    # 纯中文名无法派生 slug → 随机 id（非空且不与既有冲突）
    rid = courses_mod.new_id("计算机科学导论", set())
    assert rid and rid not in {"计算机科学导论"}


# —— 3) 笔记归属 ——

def test_note_header_records_course():
    h = main.note_header("20261013_1000", "计算机科学导论")
    assert courses_mod.note_course(h) == "计算机科学导论"
    assert "LectureLens 自动生成" in h


def test_note_header_uncategorized_default():
    assert courses_mod.note_course(main.note_header("20261013_1000", None)) \
        == courses_mod.UNCATEGORIZED


def test_start_transcript_stamps_course(tmp_path):
    hub = main.Hub()
    p = hub.start_transcript("线性代数")
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) == "线性代数"
    assert hub.stamp == main.datetime.datetime.now().strftime("%Y%m%d_%H%M")


def test_retag_empty_note_rewrites_header():
    """开课前选课：笔记还只有头 → 直接改写（否则头里永远写着「未分类」）。"""
    hub = main.Hub()
    hub.start_transcript(None)
    assert hub.retag_course("信号与系统") == "empty"
    assert courses_mod.note_course(hub.transcript_path.read_text(encoding="utf-8")) \
        == "信号与系统"


def test_retag_note_with_body_appends_marker():
    """已开课再切课：改写头会让前半段归属说谎 → 只追加切换标记，原归属不变。"""
    hub = main.Hub()
    hub.start_transcript("微积分")
    hub.append("10:00:01", "hello world", "你好世界", 1)
    assert hub.retag_course("线性代数") == "marked"
    text = hub.transcript_path.read_text(encoding="utf-8")
    assert courses_mod.note_course(text) == "微积分"          # 主归属保持开课课程
    assert "> 课程：线性代数" in text                          # 切换事实留在正文
    assert "hello world" in text                              # 正文零丢失


def test_retag_same_course_is_noop():
    hub = main.Hub()
    hub.start_transcript("微积分")
    assert hub.retag_course("微积分") == "none"


def test_retag_without_transcript_is_noop():
    assert main.Hub().retag_course("任意") == "none"


def test_rename_in_notes_backfills():
    """课程改名 → 历史笔记头同步改名（按名统计才不漏历史笔记）。"""
    hub = main.Hub()
    p = hub.start_transcript("旧名")
    hub.append("10:00:01", "content", "", 1)
    assert courses_mod.rename_in_notes(main.TRANSCRIPTS, "旧名", "新名") == 1
    assert courses_mod.note_course(p.read_text(encoding="utf-8")) == "新名"
    assert "content" in p.read_text(encoding="utf-8")


def test_count_notes_groups_by_course():
    hub = main.Hub()
    hub.start_transcript("甲")
    hub.append("10:00:01", "a", "", 1)
    # 注：start_transcript 的文件名是**分钟粒度**（lecture_YYYYMMDD_HHMM.md，既有行为），
    # 同一分钟内再起一个 Hub 会覆盖同名文件——故第二篇直接另写不同名文件，
    # 本用例只专注验证「按课程名分组统计」这一条。
    other = main.TRANSCRIPTS / "lecture_20200101_0000.md"
    other.write_text(main.note_header("20200101_0000", "乙"), encoding="utf-8")
    counts = courses_mod.count_notes(main.TRANSCRIPTS)
    assert counts.get("甲") == 1 and counts.get("乙") == 1


def test_count_notes_missing_dir_is_empty(tmp_path):
    assert courses_mod.count_notes(tmp_path / "nope") == {}


# —— 4) API：CRUD 白名单 + 当前课程 ——

async def test_create_list_and_defaults(cpath):
    async with asgi_client() as c:
        r = await c.post("/api/courses", json={"name": "计算机科学导论"})
        assert r.status_code == 200
        course = r.json()["course"]
        assert course["name"] == "计算机科学导论"
        assert course["color"] == courses_mod.default_color()
        assert course["glossary"] is None

        r2 = await c.get("/api/courses")
        body = r2.json()
        assert body["ok"] and len(body["courses"]) == 1
        assert body["active"] is None
        assert body["active_name"] == courses_mod.UNCATEGORIZED   # 未选 = 未分类
        assert body["courses"][0]["notes"] == 0
        assert body["palette"]


async def test_create_rejects_unknown_key_and_bad_values(cpath):
    async with asgi_client() as c:
        assert (await c.post("/api/courses",
                             json={"name": "A", "bogus": 1})).status_code == 400
        assert (await c.post("/api/courses", json={"color": "#fff"})).status_code == 400
        assert (await c.post("/api/courses", json={"name": "  "})).status_code == 400
        # 未知键被拒后不得留下任何状态
        assert (await c.get("/api/courses")).json()["courses"] == []


async def test_create_rejects_duplicate_name(cpath):
    async with asgi_client() as c:
        assert (await c.post("/api/courses", json={"name": "A"})).status_code == 200
        assert (await c.post("/api/courses", json={"name": "A"})).status_code == 400
        assert len((await c.get("/api/courses")).json()["courses"]) == 1


async def test_create_rejects_traversal_glossary(cpath):
    async with asgi_client() as c:
        r = await c.post("/api/courses",
                         json={"name": "A", "glossary": "../evil.md"})
        assert r.status_code == 400
        assert ".." in r.json()["error"]


async def test_update_and_404(cpath):
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "A"})).json()["course"]["id"]
        r = await c.put(f"/api/courses/{cid}", json={"color": "#AABBCC"})
        assert r.status_code == 200
        assert r.json()["course"]["color"] == "#aabbcc"
        assert (await c.put("/api/courses/nope", json={"name": "B"})).status_code == 404
        # 改成已存在的名字被拒
        await c.post("/api/courses", json={"name": "B"})
        assert (await c.put(f"/api/courses/{cid}",
                            json={"name": "B"})).status_code == 400


async def test_update_unknown_field_rejected(cpath):
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "A"})).json()["course"]["id"]
        r = await c.put(f"/api/courses/{cid}", json={"nope": 1})
        assert r.status_code == 400


async def test_active_course_selection_and_validation(cpath):
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "A"})).json()["course"]["id"]
        r = await c.post("/api/courses/active", json={"active": cid})
        assert r.status_code == 200
        assert r.json()["active_name"] == "A"
        assert main.status["course"] == "A"          # status 供前端展示
        # 未分类 = null
        assert (await c.post("/api/courses/active", json={"active": None})).status_code == 200
        assert (await c.get("/api/courses")).json()["active"] is None
        # 不存在的 id / 非法类型 / 未知字段
        assert (await c.post("/api/courses/active", json={"active": "ghost"})).status_code == 404
        assert (await c.post("/api/courses/active", json={"active": 7})).status_code == 400
        assert (await c.post("/api/courses/active", json={"foo": 1})).status_code == 400


async def test_active_course_activates_glossary(cpath, tmp_path):
    """选中带术语表的课程 → 该表接进翻译侧清单（+ whisper 侧 prompt 同步）。"""
    g = tmp_path / "glossary" / "CS.md"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("attention = 注意力\n", encoding="utf-8")

    class FakeTranslator:
        """替身：只需 set_glossary_files 的语义（真翻译器见 test_glossary_reload）。"""
        def __init__(self):
            self.glossary_files = []
            self.entries = [("attention", "注意力")]

        def set_glossary_files(self, files):
            if list(files) == self.glossary_files:
                return False
            self.glossary_files = list(files)
            return True

    tr = FakeTranslator()
    main.runtime["translator"] = tr
    main.runtime["cli_glossary"] = []
    try:
        async with asgi_client() as c:
            cid = (await c.post("/api/courses",
                                json={"name": "CS", "glossary": "glossary/CS.md"})
                   ).json()["course"]["id"]
            r = await c.post("/api/courses/active", json={"active": cid})
            assert r.json()["glossary_reloaded"] is True
            assert tr.glossary_files == [str(g.resolve())]
            assert main.runtime["prompt"]                 # whisper 侧 prompt 已同步
    finally:
        main.runtime["translator"] = None
        main.runtime["cli_glossary"] = []
        main.runtime["prompt"] = None


async def test_delete_course_and_active_reset(cpath):
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "A"})).json()["course"]["id"]
        await c.post("/api/courses/active", json={"active": cid})
        r = await c.delete(f"/api/courses/{cid}")
        assert r.status_code == 200
        assert r.json()["was_active"] is True
        body = (await c.get("/api/courses")).json()
        assert body["courses"] == [] and body["active"] is None
        assert (await c.delete(f"/api/courses/{cid}")).status_code == 404


async def test_notes_counted_per_course_in_api(cpath):
    """端到端：开课带课程名 → /api/courses 的 notes 计数反映归属。"""
    hub = main.Hub()
    hub.start_transcript("甲课")
    hub.append("10:00:01", "hello", "", 1)
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "甲课"})
        body = (await c.get("/api/courses")).json()
        assert body["courses"][0]["notes"] == 1


async def test_malformed_body_rejected(cpath):
    async with asgi_client() as c:
        r = await c.post("/api/courses", content=b"not json",
                         headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        r2 = await c.post("/api/courses", json=[1, 2, 3])
        assert r2.status_code == 400


def test_repo_courses_json_untouched(repo_courses_guard):
    """本文件所有测试跑完，仓库真实 courses.json 未被创建/改写。"""
    repo_courses_guard()


# —— 5) B14：课程管理重做（统计字段 / 删课回溯 / 设置页结构） ——

async def test_courses_view_reports_terms_and_missing_glossary(cpath, tmp_path):
    """B14：每行要显示「N 篇笔记 · M 条术语」→ /api/courses 带出 terms。

    口径与术语页同源（glossary 解析器）；**绑了但文件不在** → glossary_missing，
    好让 UI 如实说「术语表未找到」而不是谎报 0 条。"""
    g = tmp_path / "glossary" / "CS.md"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("# 注释行不算\nattention = 注意力\n\nbatch size = 批大小\n残留行\n",
                 encoding="utf-8")
    gone = tmp_path / "glossary" / "Gone.md"
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "CS", "glossary": "glossary/CS.md"})
        await c.post("/api/courses", json={"name": "无表课"})
        await c.post("/api/courses", json={"name": "缺表课", "glossary": "glossary/Gone.md"})
        body = (await c.get("/api/courses")).json()
    rows = {r["name"]: r for r in body["courses"]}
    assert rows["CS"]["terms"] == 2 and rows["CS"]["glossary_missing"] is False
    assert rows["无表课"]["terms"] == 0 and rows["无表课"]["glossary_missing"] is False
    assert rows["缺表课"]["terms"] == 0 and rows["缺表课"]["glossary_missing"] is True
    assert rows["缺表课"]["glossary"] == "glossary/Gone.md"   # 绑定关系仍在（只是文件没了）
    assert all(isinstance(r["notes"], int) for r in body["courses"])


async def test_courses_view_never_reads_outside_root(cpath, tmp_path, monkeypatch):
    """安全：courses.json 可手改（sanitize 只去空白、不校验路径），统计读盘**必须**
    先过 courses.glossary_problem 的根锚定校验——项目根之外的 .md 一个字节都不许读。"""
    outside = tmp_path.parent / "outside-b14.md"
    outside.write_text("secret = 机密\n", encoding="utf-8")
    cpath.write_text(json.dumps({"active": None, "courses": [
        {"id": "evil", "name": "外挂", "color": "#93a1b0",
         "glossary": str(outside)}]}), encoding="utf-8")
    async with asgi_client() as c:
        row = (await c.get("/api/courses")).json()["courses"][0]
    assert row["terms"] == 0 and row["glossary_missing"] is False   # 未读盘、也没崩


async def test_delete_course_retags_historical_notes(cpath):
    """B14：删课程 = 该课**历史笔记**的归属行一并改「未分类」（设置页确认框的那句承诺）。

    旧行为只重标「当前课程」那一篇：其余笔记在库里永远挂着已不存在的课名（课程筛选
    栏还会冒出幽灵项），而确认框已经告诉用户「N 篇笔记将归入未分类」。"""
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    crlf = (main.note_header("20261013_1000", "甲课").replace("\n", "\r\n")
            + "- `10:00:01` hello\r\n  - `#1` 你好\r\n")
    p = main.TRANSCRIPTS / "lecture_20261013_1000.md"
    p.write_bytes(crlf.encode("utf-8"))
    other = main.TRANSCRIPTS / "lecture_20261013_1100.md"
    other.write_text(main.note_header("20261013_1100", "乙课") + "- `10:01` x\n",
                     encoding="utf-8")
    async with asgi_client() as c:
        cid = (await c.post("/api/courses", json={"name": "甲课"})).json()["course"]["id"]
        await c.post("/api/courses", json={"name": "乙课"})
        r = await c.delete(f"/api/courses/{cid}")
        body = r.json()
        assert r.status_code == 200
        assert body["retagged"] == 1 and body["was_active"] is False
        assert body["uncategorized"] == courses_mod.UNCATEGORIZED
        assert (await c.get("/api/courses")).json()["courses"][0]["notes"] == 1  # 乙课
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    assert courses_mod.note_course(text) == courses_mod.UNCATEGORIZED
    assert "hello" in text and "你好" in text and "LectureLens 自动生成" in text
    assert b"\r\r\n" not in raw and raw.count(b"\n") == raw.count(b"\r\n")   # 行尾保真
    assert courses_mod.note_course(other.read_text(encoding="utf-8")) == "乙课"  # 别的课不动


def test_rename_in_notes_keeps_lf_files_lf(tmp_path):
    """逐字节保真：LF 笔记不该因为改课程名被整体翻成 CRLF（旧实现 write_text 会翻）。"""
    d = tmp_path / "transcripts"
    d.mkdir()
    p = d / "lecture_20261013_1000.md"
    p.write_bytes((main.note_header("20261013_1000", "旧名")
                   + "- `10:00:01` x\n").encode("utf-8"))
    before_nl = p.read_bytes().count(b"\n")
    assert courses_mod.rename_in_notes(d, "旧名", "新名") == 1
    raw = p.read_bytes()
    assert b"\r" not in raw and raw.count(b"\n") == before_nl
    assert courses_mod.note_course(raw.decode("utf-8")) == "新名"


async def test_delete_live_course_keeps_body_and_reassigns(cpath):
    """删的正是**当前课程**：记录中那篇按既有语义走（empty 改头 / marked 追加标记），
    随后头部残留的旧课名被统一收口到「未分类」——正文与配对一字不丢。"""
    hub = main.Hub()
    saved = (hub.transcript_path, hub.stamp)
    try:
        hub.start_transcript("微积分")
        hub.append("10:00:01", "derivative", "导数", 1)
        async with asgi_client() as c:
            cid = (await c.post("/api/courses", json={"name": "微积分"})).json()["course"]["id"]
            await c.post("/api/courses/active", json={"active": cid})
            r = await c.delete(f"/api/courses/{cid}")
            assert r.status_code == 200 and r.json()["was_active"] is True
            assert r.json()["retagged"] == 1                 # 记录中那篇也被收口
        text = hub.transcript_path.read_text(encoding="utf-8")
        assert courses_mod.note_course(text) == courses_mod.UNCATEGORIZED
        assert "derivative" in text and "导数" in text
        assert main.status["course"] == courses_mod.UNCATEGORIZED
    finally:
        hub.transcript_path, hub.stamp = saved


# —— 7) B5：记住上次课程（回归断言；机制本就成立，不新增功能） ——

async def test_active_course_survives_restart(cpath):
    """B5 回归：`active` 写盘 → **重启读回**（启动时按 courses.json 选出当前课程，
    新笔记头直接挂上它）。「记住上次课程」的全部机制就是这条链路：active 落在
    courses.json 里跨重启持久，启动处读回来 —— 前端不另存任何"上次选了谁"的状态。

    既有用例覆盖到哪一步（如实说明，避免重复劳动）：storage 层 save/load 往返
    （test_save_load_roundtrip）与端点 POST→GET 往返（test_active_course_selection_*）
    都有了；**缺的正是"重启应用"这一步**（load → find(active) → status.course +
    新笔记头），故这里补上，并把启动处那三行静态钉住（删了这条回归才有意义）。"""
    async with asgi_client() as c:
        await c.post("/api/courses", json={"name": "数学"})
        cid = (await c.post("/api/courses", json={"name": "计算机科学导论"})
               ).json()["course"]["id"]
        await c.post("/api/courses/active", json={"active": cid})
    on_disk = json.loads(cpath.read_text(encoding="utf-8"))
    assert on_disk["active"] == cid              # 跨重启的唯一凭据：models 落盘
    hub = main.Hub()
    saved_hub = (hub.transcript_path, hub.stamp)
    saved_course = main.status.get("course")
    try:
        # 「重启」= 复刻 lifespan 的启动三行（main.py:4111-4116）
        data = courses_mod.load(main.COURSES_PATH)
        cur = courses_mod.find(data, data.get("active")) if data.get("active") else None
        assert cur is not None and cur["name"] == "计算机科学导论"
        main.status["course"] = cur["name"] if cur else courses_mod.UNCATEGORIZED
        p = hub.start_transcript(cur["name"] if cur else None)
        assert main.status["course"] == "计算机科学导论"
        assert courses_mod.note_course(p.read_text(encoding="utf-8")) \
            == "计算机科学导论"                   # 开机即挂上上次那门课
    finally:
        hub.transcript_path, hub.stamp = saved_hub
        main.status["course"] = saved_course
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'status["course"] = _ccur["name"] if _ccur else courses_mod.UNCATEGORIZED' in src
    assert 'hub.start_transcript(_ccur["name"] if _ccur else None)' in src


# —— 6) B14：设置页课程卡的结构断言（重做后 = 纯课程管理） ——

def test_settings_course_card_is_management_only():
    """B14 职责拆分：卡里**没有**「设为当前课程」的任何入口（那是字幕页下拉与
    笔记库左侧课程列表的活）；卡只做管理。"""
    card = block("<h2>课程管理</h2>", "<h2>识别模型</h2>")
    assert 'id="courseRows"' in card
    assert "/api/courses/active" not in card and "setActiveCourse" not in card
    for anchor in ("改名", "换色", "绑定术语表", "删除", "查看笔记"):
        assert anchor in card, anchor
    # 建课入口仍在（管理职责的一部分）
    assert 'id="courseNewName"' in card and "$('courseAdd').onclick" in HTML
    # 卡内 JS（renderCourseCard → createCourse 之间）同样不许出现选课调用
    js = block("function renderCourseCard() {", "async function createCourse(name, glossary) {")
    assert "setActiveCourse" not in js and "/api/courses/active" not in js
    # 选择职责在**别处**且在：侧栏课程条目点一下 = 设为当前课程
    side = block("function renderCourseSidebar() {", "async function setActiveCourse(id) {")
    assert "b.onclick = () => setActiveCourse(id);" in side


def test_course_row_shows_stats_and_jumps_to_notes():
    """每行 = 色点 + 课程名 + 统计 + 术语表绑定 + 查看笔记 + 删除；点行跳笔记库筛该课。"""
    row = block("function courseRow(c) {", "\n}\n")
    assert 'row.className = \'cRow cGo\'' in row or "cRow cGo" in row
    assert "cSw" in row and "cSwDot" in row and "saveCourse(c.id, { color: col })" in row
    assert "saveCourse(c.id, { name: name.value })" in row                 # 行内改名
    assert "saveCourse(c.id, { glossary: gl.value })" in row               # 绑定术语表
    assert "篇笔记 · " in row and "条术语" in row and "术语表未找到" in row   # 统计（含缺表口径）
    assert "'查看笔记'" in row and "openCourseNotes(c.name)" in row
    assert "row.append(sw, name, stat, gl, go, del)" in row
    # 点行跳转 + 控件区不触发（否则点改名输入框会把人踢到笔记库）
    assert "row.onclick" in row and "closest('input, button')" in row
    assert "del.onclick = () => delCourse(c)" in row


def test_course_row_jump_uses_notes_filter_contract():
    """跳转契约：先设 NB.course（笔记库课程筛选栏的同一口径），再 openWorkbench('notes')
    ——它内含 refreshNotes()，两条入口（已在笔记库 / 不在）都覆盖。"""
    fn = block("function openCourseNotes(name) {", "\n}\n")
    assert "NB.course = name;" in fn
    assert "openWorkbench('notes')" in fn
    assert fn.index("NB.course = name;") < fn.index("openWorkbench('notes')")
    assert "showToast" in fn                          # 有反馈（B 组原则）
    # 笔记库侧读的确实是 NB.course
    assert "if (NB.course) p.set('course', NB.course);" in HTML


def test_delete_confirm_promises_uncategorized_and_server_delivers():
    """确认框写「N 篇笔记将归入未分类」+「笔记文件保留」——这句承诺由服务端兑现
    （DELETE → courses.rename_in_notes），前端文案与后端行为必须成对出现。"""
    d = block("async function delCourse(c) {", "\n}",)
    assert "c.notes" in d
    assert "篇笔记将归入「" in d and "笔记文件保留，不会删除" in d
    assert "setActiveCourse" not in d                 # 删课不动「当前课程」选择入口
    assert "r.retagged" in d                          # 回执如实报几篇被改归属
    # 服务端那半句（同一个承诺的另一半）
    assert "courses_mod.rename_in_notes(TRANSCRIPTS, course[\"name\"]" in \
        (ROOT / "app" / "main.py").read_text(encoding="utf-8")
