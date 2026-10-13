# -*- coding: utf-8 -*-
"""tests/test_glossary_api.py — p2-6（B8）术语库：文件读写 / 增删改 / 端点 / 热重载。

三层：
1) glossary.py 纯函数（行模型解析与渲染、注释与顺序保留、词条校验、增删改）；
2) 安全边界（**写操作**：只收裸文件名，只能落到 glossary/ 直属 .md）；
3) /api/glossary 的 GET/POST 与「保存即热重载」——含一条**mtime 无关**的回归：
   本机文件系统会把快速重写的时间戳折叠成同一个（实测 300 次里 266 次相同），
   若重载只靠 reload_if_changed 就会随机不生效，这正是 reload_now 存在的理由。

全程 ROOT 指向 tmp_path：绝不碰仓库真实 glossary/CS.md。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import glossary as gl
import main
from translate import DeepSeekTranslator


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


SAMPLE = ("# 课程术语表 — 示例\n"
          "# 第二行注释\n"
          "neural network = 神经网络\n"
          "attention = 注意力\n"
          "\n"
          "gradient descent = 梯度下降\n")


@pytest.fixture
def gl_env(tmp_path, monkeypatch):
    """ROOT → tmp_path（隔离真实 glossary/），并复位调优相关单例状态。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setitem(main.runtime, "translator", None)
    monkeypatch.setitem(main.runtime, "prompt", None)
    (tmp_path / "glossary").mkdir(parents=True, exist_ok=True)
    (tmp_path / "glossary" / "CS.md").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


# —— 1) 行模型 ——

def test_parse_lines_keeps_comments_and_blanks():
    lines = gl.parse_lines(SAMPLE)
    kinds = [ln["kind"] for ln in lines]
    assert kinds == ["raw", "raw", "entry", "entry", "raw", "entry"]
    assert lines[0]["raw"].startswith("#")
    assert lines[4]["raw"] == ""                      # 空行也保留
    assert lines[2]["kind"] == "entry"
    assert (lines[2]["en"], lines[2]["zh"]) == ("neural network", "神经网络")
    assert lines[2]["raw"] == "neural network = 神经网络"   # 原文留底（未改就逐字吐回）
    assert lines[2]["indent"] == ""


def test_parse_lines_matches_translator_parser():
    """本模块认得的词条必须与 translate.load_glossary_entries 完全一致——
    两边口径分叉会出现「界面里看得到、翻译器读不到」的幽灵词条。"""
    from translate import load_glossary_entries
    import tempfile
    p = Path(tempfile.mkdtemp()) / "T.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mine = [(ln["en"], ln["zh"]) for ln in gl.parse_lines(SAMPLE)
            if ln["kind"] == "entry"]
    assert load_glossary_entries([str(p)]) == mine


def test_render_round_trip_preserves_everything():
    lines = gl.parse_lines(SAMPLE)
    assert gl.render_lines(lines) == SAMPLE            # 注释/空行/顺序一字不差


def test_render_preserves_crlf():
    text = "# c\r\na = 甲\r\nb = 乙\r\n"
    out = gl.render_lines(gl.parse_lines(text), "\r\n")
    assert "\r\n" in out and "\n\n" not in out.replace("\r\n", "")


def test_parse_lines_tolerates_unparsable_lines():
    """解析器跳过的行（无分隔符/缺一侧）本模块也不许丢——原样保留。"""
    text = "# c\n脏行没有等号\n只有等号 =\n= 只有右侧\nx = y\n"
    lines = gl.parse_lines(text)
    assert [ln["kind"] for ln in lines] == ["raw", "raw", "raw", "raw", "entry"]
    assert gl.render_lines(lines) == text


# —— 2) 词条校验 ——

def test_entry_problem_rejects_format_breakers():
    """两类「写进去就消失」的输入：en 以 # 开头（变注释）、en 含 =（解析错位）。"""
    assert gl.entry_problem("#x", "甲") is not None
    assert gl.entry_problem("a = b", "甲") is not None
    assert gl.entry_problem("a\nb", "甲") is not None
    assert gl.entry_problem("a", "甲\n乙") is not None
    assert gl.entry_problem("", "甲") is not None
    assert gl.entry_problem("a", "  ") is not None
    assert gl.entry_problem("a" * 201, "甲") is not None
    assert gl.entry_problem(None, "甲") is not None
    assert gl.entry_problem("attention", "注意力") is None
    assert gl.entry_problem("a=b 的中文", "含 = 是允许的") is not None  # en 侧仍禁 =


def test_entry_problem_allows_equal_in_zh():
    """zh 里含 = 不会破坏解析（partition 只切第一个 =）——但 en 侧必须干净。"""
    lines, err = gl.add_entry([], "x", "a = b")
    assert err is None and lines[0]["zh"] == "a = b"


# —— 3) 增删改 ——

def test_add_appends_and_preserves_layout():
    lines = gl.parse_lines(SAMPLE)
    out, err = gl.add_entry(lines, "overfitting", "过拟合")
    assert err is None
    assert gl.count_entries(out) == 4
    text = gl.render_lines(out)
    assert "overfitting = 过拟合" in text
    assert text.startswith("# 课程术语表 — 示例\n# 第二行注释\n")   # 表头未动


def test_add_rejects_duplicate_case_insensitively():
    out, err = gl.add_entry(gl.parse_lines(SAMPLE), "Neural Network", "神经网络")
    assert out is not None and err and "已存在" in err


def test_add_enforces_cap():
    lines = [{"kind": "entry", "en": f"e{i}", "zh": f"z{i}"}
             for i in range(gl.MAX_ENTRIES)]
    out, err = gl.add_entry(lines, "one more", "再多一个")
    assert err and "上限" in err


def test_update_renames_in_place():
    lines = gl.parse_lines(SAMPLE)
    out, err = gl.update_entry(lines, "attention", "attention mechanism", "注意力机制")
    assert err is None
    assert [ln["kind"] for ln in out] == [ln["kind"] for ln in lines]   # 位置不变
    assert gl.find_index(out, "attention mechanism") == 3
    assert gl.find_index(out, "attention") is None


def test_update_rejects_collision_and_missing():
    lines = gl.parse_lines(SAMPLE)
    _, err = gl.update_entry(lines, "attention", "neural network", "撞名")
    assert err and "已存在" in err
    _, err2 = gl.update_entry(lines, "不存在", "x", "y")
    assert err2 and "不存在" in err2


def test_update_own_name_is_allowed():
    """只改译名（en 没动）不该被当成撞名。"""
    out, err = gl.update_entry(gl.parse_lines(SAMPLE), "attention", "attention", "注意力!")
    assert err is None and gl.find_index(out, "attention") is not None


def test_delete_removes_only_target():
    out, err = gl.delete_entry(gl.parse_lines(SAMPLE), "attention")
    assert err is None
    assert gl.count_entries(out) == 2
    assert "# 课程术语表 — 示例" in gl.render_lines(out)
    assert "gradient descent = 梯度下降" in gl.render_lines(out)


def test_edit_one_entry_leaves_others_byte_identical():
    """评审2 修：改一条**不该动到其余行**。

    旧实现把**每一条**词条都重写成 `EN = 中文`，于是含 Tab 分隔行的术语表只要改一条，
    其余 Tab 行也被顺手改成 ` = `——内容没变但整篇 diff 红了。"""
    text = ("# 头\n"
            "alpha = 甲\n"
            "beta\t乙\n"          # Tab 分隔：未被改动时应原样保留
            "  gamma = 丙\n")      # 带缩进
    lines = gl.parse_lines(text)
    out, err = gl.update_entry(lines, "alpha", "alpha", "甲甲")
    assert err is None
    rendered = gl.render_lines(out)
    assert "alpha = 甲甲" in rendered            # 改动的那条被重写
    assert "beta\t乙" in rendered                # 其余 Tab 行**逐字**保留
    assert "  gamma = 丙" in rendered            # 缩进也保留


def test_rewritten_entry_keeps_indent():
    """被改写的词条要保留行首缩进——旧实现 strip() 后回写会把缩进吃掉。"""
    lines = gl.parse_lines("  a = 甲\n")
    out, err = gl.update_entry(lines, "a", "a", "甲甲")
    assert err is None
    assert gl.render_lines(out) == "  a = 甲甲\n"


def test_new_entry_uses_canonical_shape():
    out, err = gl.add_entry(gl.parse_lines("# c\n"), "a", "甲")
    assert err is None
    assert gl.render_lines(out) == "# c\na = 甲\n"


def test_delete_missing_reports_error():
    _, err = gl.delete_entry(gl.parse_lines(SAMPLE), "nope")
    assert err and "不存在" in err


def test_read_write_file_round_trip(tmp_path):
    p = tmp_path / "glossary" / "T.md"
    gl.write_file(p, gl.parse_lines(SAMPLE))
    # **逐字节**比对：只比 read_file 的返回值会漏掉行尾被翻（read_text 走通用换行，
    # 会把 CRLF 还原成 LF，于是「文件其实变成了 CRLF」在字符串比对上完全看不出来）
    assert p.read_bytes() == SAMPLE.encode("utf-8")
    got = gl.read_file(p)
    assert got["text"] == SAMPLE and got["eol"] == "\n"
    assert len(got["entries"]) == 3
    assert not p.with_name(p.name + ".tmp").exists()      # 原子写不留 .tmp


def test_write_file_does_not_translate_line_endings(tmp_path):
    """回归：Windows 的 Path.write_text 会把 `\\n` 自动翻成 `\\r\\n`，于是「只改一个
    词条」会把整份 LF 文件的每一行都变成 CRLF——内容没变，但 git diff 全红，用户
    看到的是「什么都没改却整篇变了」。术语表是手改且进 git 的文件，必须原样。
    实测踩过：原 436B LF 的文件，改一次回来变 448B CRLF（12 行 × 多一个 \\r）。"""
    p = tmp_path / "glossary" / "T.md"
    gl.write_file(p, gl.parse_lines("a = 甲\nb = 乙\n"))
    assert p.read_bytes() == "a = 甲\nb = 乙\n".encode("utf-8")
    assert b"\r\n" not in p.read_bytes()
    # 反过来：CRLF 的原件也必须原样保留 CRLF（eol 参数说了算）
    gl.write_file(p, gl.parse_lines("a = 甲\r\nb = 乙\r\n"), "\r\n")
    assert p.read_bytes() == "a = 甲\r\nb = 乙\r\n".encode("utf-8")


def test_read_file_detects_crlf_eol(tmp_path):
    """read_file 的 eol 检测必须看得到真实的 CRLF——Path.read_text 的通用换行会把它
    抹平，那样 write_file 就再也无法保留原行尾。"""
    p = tmp_path / "glossary" / "T.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes("# c\r\na = 甲\r\n".encode("utf-8"))
    assert gl.read_file(p)["eol"] == "\r\n"
    p.write_bytes("# c\na = 甲\n".encode("utf-8"))
    assert gl.read_file(p)["eol"] == "\n"


# —— 4) 安全边界（写操作，比读更严） ——

def test_safe_glossary_path_rejects_escape_and_foreign(tmp_path):
    for bad in ("../config.json", "..\\config.json", "sub/CS.md", "sub\\CS.md",
                "C:/Windows/win.ini", "/etc/passwd", "CS.txt", "CS.md.bak",
                ".env", "", None, 123, "x" * 200, "glossary/CS.md"):
        assert gl.safe_glossary_path(tmp_path, bad) is None, f"应拒绝 {bad!r}"


def test_safe_glossary_path_accepts_bare_name(tmp_path):
    got = gl.safe_glossary_path(tmp_path, "CS.md")
    assert got == (tmp_path / "glossary" / "CS.md").resolve()
    assert gl.safe_glossary_path(tmp_path, " cs101.MD ") is not None   # 大小写/空白宽容


def test_write_file_rejects_non_glossary_path(tmp_path):
    """写前包含性校验（防御纵深）：非 glossary/ 直属路径必须直接拒绝、且不留副作用。"""
    import pytest
    with pytest.raises(ValueError):
        gl.write_file(tmp_path / "T.md", gl.parse_lines("a = 甲\n"))
    with pytest.raises(ValueError):
        gl.write_file(tmp_path / "sub" / "T.md", gl.parse_lines("a = 甲\n"))
    assert not (tmp_path / "T.md").exists()
    assert not (tmp_path / "sub").exists()   # 被拒绝的路径不得建目录


# —— 5) 端点 ——

async def test_get_lists_files_entries_and_active_state(gl_env):
    async with asgi_client() as c:
        r = await c.get("/api/glossary")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["dir"] == "glossary/"
    assert len(body["files"]) == 1
    f = body["files"][0]
    assert f["name"] == "CS.md" and f["rel"] == "glossary/CS.md"
    assert f["count"] == 3 and len(f["entries"]) == 3
    assert f["entries"][0] == {"en": "neural network", "zh": "神经网络"}
    assert f["active"] is False                # 未挂到翻译器
    assert body["translate_ready"] is False
    assert body["limits"]["max_entries"] == gl.MAX_ENTRIES


async def test_post_add_writes_file_and_preserves_comments(gl_env):
    async with asgi_client() as c:
        r = await c.post("/api/glossary", json={"file": "CS.md", "op": "add",
                                                "en": "overfitting", "zh": "过拟合"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 4 and body["reloaded"] is False   # 未挂翻译器
    text = (gl_env / "glossary" / "CS.md").read_text(encoding="utf-8")
    assert "overfitting = 过拟合" in text
    assert text.startswith("# 课程术语表 — 示例")             # 注释在
    assert "# 第二行注释" in text


async def test_post_update_and_delete(gl_env):
    async with asgi_client() as c:
        r = await c.post("/api/glossary", json={"file": "CS.md", "op": "update",
                                                "old_en": "attention",
                                                "en": "attention mechanism",
                                                "zh": "注意力机制"})
        assert r.status_code == 200 and r.json()["count"] == 3
        r2 = await c.post("/api/glossary", json={"file": "CS.md", "op": "delete",
                                                 "en": "attention mechanism"})
        assert r2.status_code == 200 and r2.json()["count"] == 2
    text = (gl_env / "glossary" / "CS.md").read_text(encoding="utf-8")
    assert "attention" not in text
    assert "neural network = 神经网络" in text


async def test_post_rejects_bad_input(gl_env):
    async with asgi_client() as c:
        assert (await c.post("/api/glossary", json={"file": "CS.md",
                                                    "op": "nope"})).status_code == 400
        assert (await c.post("/api/glossary", json={"file": "../config.json",
                                                    "op": "add", "en": "a",
                                                    "zh": "甲"})).status_code == 400
        assert (await c.post("/api/glossary", json={"file": "CS.md", "op": "add",
                                                    "en": "#c", "zh": "甲"})).status_code == 400
        assert (await c.post("/api/glossary", json={"file": "CS.md", "op": "add",
                                                    "en": "neural network",
                                                    "zh": "重复"})).status_code == 400
        assert (await c.post("/api/glossary", json={"file": "NOPE.md", "op": "add",
                                                    "en": "a", "zh": "甲"})).status_code == 404


async def test_post_create_file(gl_env):
    async with asgi_client() as c:
        r = await c.post("/api/glossary", json={"file": "CS101.md", "op": "create"})
        assert r.status_code == 200 and r.json()["count"] == 0
        assert (gl_env / "glossary" / "CS101.md").is_file()
        # 新建的带表头注释
        assert (gl_env / "glossary" / "CS101.md").read_text(
            encoding="utf-8").startswith("#")
        r2 = await c.post("/api/glossary", json={"file": "CS101.md", "op": "create"})
        assert r2.status_code == 409
        # 新建出来的文件立刻出现在列表里（按文件名升序：'.'(0x2E) < '1'(0x31)，
        # 故 "CS.md" 排在 "CS101.md" 之前——这是字符串序，不是自然序）
        names = [f["name"] for f in (await c.get("/api/glossary")).json()["files"]]
        assert names == ["CS.md", "CS101.md"]


# —— 6) 保存即热重载 ——

def _translator_for(p: Path) -> DeepSeekTranslator:
    return DeepSeekTranslator("test-key", glossary_files=[str(p)])


async def test_write_through_active_file_hot_reloads_both_sides(gl_env, monkeypatch):
    """核心：写的是**正在生效**的文件 → entries 与 whisper 侧 prompt 同步刷新。"""
    p = gl_env / "glossary" / "CS.md"
    tr = _translator_for(p)
    monkeypatch.setitem(main.runtime, "translator", tr)
    assert len(tr.entries) == 3
    async with asgi_client() as c:
        r = await c.post("/api/glossary", json={"file": "CS.md", "op": "add",
                                                "en": "overfitting", "zh": "过拟合"})
        assert r.status_code == 200
        assert r.json()["reloaded"] is True            # 在生效清单里 → 重载
        f = (await c.get("/api/glossary")).json()["files"][0]
        assert f["active"] is True
    assert ("overfitting", "过拟合") in tr.entries      # 翻译侧 entries
    assert "overfitting = 过拟合" in tr.system          # 翻译侧 system prompt
    # whisper 侧 prompt 是**英文术语串**（glossary_prompt = EN 分号连接，不含译名），
    # 故这里断言英文术语到齐，而不是 `EN = 中文` 那种形状。
    assert main.runtime["prompt"] == \
        "neural network; attention; gradient descent; overfitting"


async def test_write_through_inactive_file_does_not_touch_prompt(gl_env, monkeypatch):
    """编辑别的课程的表 → 不重载、不动当前 prompt（否则会把当前课的术语顶掉）。"""
    active = gl_env / "glossary" / "CS.md"
    other = gl_env / "glossary" / "OTHER.md"
    other.write_text("foo = 福\n", encoding="utf-8")
    tr = _translator_for(active)
    monkeypatch.setitem(main.runtime, "translator", tr)
    before = tr.system
    async with asgi_client() as c:
        r = await c.post("/api/glossary", json={"file": "OTHER.md", "op": "add",
                                                "en": "bar", "zh": "巴"})
        assert r.status_code == 200 and r.json()["reloaded"] is False
    assert tr.system == before
    assert main.runtime["prompt"] is None


def test_reload_now_beats_mtime_collapse(gl_env):
    """回归：**mtime 被折叠**时 reload_if_changed 会漏掉变更，reload_now 不会。

    本机实测 300 次连续重写里 266 次 st_mtime_ns 完全相同——若术语页「保存即生效」
    只依赖 mtime 比对，就会出现「改了词条但翻译没变」。这里把 mtime 冻结回原值来
    确定性复现该场景。"""
    p = gl_env / "glossary" / "CS.md"
    tr = _translator_for(p)
    mtime = p.stat().st_mtime_ns
    p.write_text("neural network = 神经网络\nbrand new = 全新\n", encoding="utf-8")
    os.utime(p, ns=(p.stat().st_atime_ns, mtime))          # 冻结 mtime = 变更被隐藏
    assert p.stat().st_mtime_ns == mtime
    assert tr.reload_if_changed() is False                 # 只靠 mtime：漏掉
    assert len(tr.entries) == 3
    tr.reload_now()                                        # 显式重载：不依赖 mtime
    assert tr.entries == [("neural network", "神经网络"), ("brand new", "全新")]


def test_reload_now_updates_mtimes_snapshot(gl_env):
    """reload_now 之后 _mtimes 必须同步刷新，否则紧接着的 reload_if_changed
    会误判「变了」而多刷一次（并可能把清理掉的状态又刷回来）。"""
    p = gl_env / "glossary" / "CS.md"
    tr = _translator_for(p)
    p.write_text("x = 甲\n", encoding="utf-8")
    tr.reload_now()
    assert tr.reload_if_changed() is False
