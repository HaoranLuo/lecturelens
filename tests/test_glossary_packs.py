# -*- coding: utf-8 -*-
"""tests/test_glossary_packs.py — B13 术语库包（docs/notes-batch.md B13 行）。

五层：
1) **包文件完整性巡检**——对 glossary_packs/ 下**每一个包**断言全部格式规则
   （60–150 条、零无法解析行、英文大小写不敏感去重、en 里无 `=`、字段/换行合法、
   带简介头、UTF-8 无 BOM）——起草内容是「AI 初版 + 朋友补正」，格式由这条测试钉住；
2) 纯函数层：块标记定位、并集去重、上限预检、课程名 → 文件名派生、双路径解析；
3) **装/卸往返以文件为事实来源**（不看响应自报，只读盘上那份 md 的真内容）；
4) **双标记语义**：EOF 追加的用户词条绝不被动/被吞（卸载只按两行标记切片）；
5) 安全：包名与文件名两侧都不接受越界输入；课程绑定越出 glossary/ → 拒绝。

全程 ROOT 指向 tmp_path（绝不碰仓库真实 glossary/）；PACKS_DIR 指向仓库真实包目录
（包是随代码发布的**只读输入**，测试要校验的就是它们本身）。
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import glossary as gl
import main
import packs

ROOT_REAL = Path(__file__).resolve().parent.parent
PACKS_REAL = ROOT_REAL / "glossary_packs"

#: 初版 8 包（B13：数学/心理学/工程/医疗/CS…）——只断言「都在」，将来增包不受影响
INITIAL_PACKS = ("数学", "心理学", "工程", "医疗", "计算机", "经济", "法律", "人工智能")

MIN_ENTRIES, MAX_ENTRIES = 60, 150


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def pack_env(tmp_path, monkeypatch):
    """ROOT → tmp_path（隔离真实 glossary/ 与 courses.json），PACKS_DIR → 真实包目录。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "PACKS_DIR", PACKS_REAL)
    monkeypatch.setitem(main.runtime, "translator", None)
    monkeypatch.setitem(main.runtime, "prompt", None)
    (tmp_path / "glossary").mkdir(parents=True, exist_ok=True)
    return tmp_path


async def _new_course(c, name, glossary=None):
    body = {"name": name}
    if glossary:
        body["glossary"] = glossary
    r = await c.post("/api/courses", json=body)
    assert r.status_code == 200, r.text
    return r.json()["course"]


# —— 1) 包文件完整性巡检（对每个包逐条断言） ——

def test_pack_files_exist_and_are_utf8_without_bom():
    files = packs.list_pack_files(PACKS_REAL)
    stems = {p.stem for p in files}
    assert set(INITIAL_PACKS) <= stems, f"初版 8 包必须都在，缺: {set(INITIAL_PACKS) - stems}"
    for p in files:
        assert p.parent == PACKS_REAL, "只认包目录直属的 .md（子目录不当成包）"
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} 带 BOM"
        raw.decode("utf-8")                    # 非法 UTF-8 直接抛
        assert b"# \xe2\x80\x94\xe2\x80\x94 \xe5\x8c\x85\xef\xbc\x9a" in raw, \
            f"{p.name} 缺「# —— 包：<名> ——」头"


@pytest.mark.parametrize("pack_file", packs.list_pack_files(PACKS_REAL),
                         ids=lambda p: p.stem)
def test_each_pack_obeys_every_format_rule(pack_file):
    """逐包跑 glossary.parse_lines（与翻译器同源的唯一解析器）并断言格式规则。"""
    text = pack_file.read_text(encoding="utf-8")
    lines = gl.parse_lines(text)

    # ① 60–150 条（B13「每包 50-150 条，宁精勿多」的落地口径）
    entries = [ln for ln in lines if ln["kind"] == "entry"]
    assert MIN_ENTRIES <= len(entries) <= MAX_ENTRIES, \
        f"{pack_file.name} 条目数 {len(entries)} 不在 {MIN_ENTRIES}–{MAX_ENTRIES}"

    # ② 非注释、非空行**全部**被解析成词条（零「无法解析行」——脏行会被翻译器静默跳过）
    bad = [ln["raw"] for ln in lines
           if ln["kind"] != "entry" and ln["raw"].strip() and not ln["raw"].strip().startswith("#")]
    assert bad == [], f"{pack_file.name} 有解析不了的行: {bad[:5]}"

    # ③ 英文侧大小写不敏感去重 + 无 `=` + 无换行 + 字段长度
    seen: dict = {}
    for ln in entries:
        key = ln["en"].lower()
        assert key not in seen, f"{pack_file.name} 英文重复（大小写不敏感）: {ln['en']}"
        seen[key] = True
        assert gl.entry_problem(ln["en"], ln["zh"]) is None, \
            f"{pack_file.name} 词条非法: {ln['en']!r} = {ln['zh']!r}"

    # ④ 头部元信息（列表页要显示简介；维护/版本供将来在线更新比对）
    meta = packs.pack_meta(text)
    assert meta.get("intro"), f"{pack_file.name} 缺「# 简介：」"
    assert meta.get("version"), f"{pack_file.name} 缺「# 版本：」"

    # ⑤ 条目数与 parse_pack 自报的一致（端口径：实数，不是文件里写死的数）
    rec = packs.parse_pack(pack_file)
    assert rec["count"] == len(entries) and rec["name"] == pack_file.stem
    assert len(rec["entries"]) == len(entries)


def test_pack_meta_ignores_comment_after_entries():
    """元信息只扫**首个词条之前**的注释——正文里若出现 `# 简介：` 不许被当成头部。"""
    meta = packs.pack_meta("# 简介：真头部\na = 甲\n# 简介：假头部\n")
    assert meta["intro"] == "真头部"
    assert packs.pack_meta("a = 甲\n简介：不是注释行\n").get("intro") is None


def test_packs_dir_dual_path(tmp_path):
    """目录解析：dev = ROOT/glossary_packs；frozen = _MEIPASS/glossary_packs（照 WEB 模式）。"""
    assert packs.packs_dir(tmp_path) == tmp_path / "glossary_packs"
    assert packs.packs_dir(tmp_path, "M:/meipass") == Path("M:/meipass") / "glossary_packs"
    # dev 口径：main.PACKS_DIR 就是仓库根下的 glossary_packs（Windows 现有路径逐字节不变）
    assert main.PACKS_DIR == PACKS_REAL


# —— 2) 纯函数：派生名 / 标记 / 去重 / 上限 ——

def test_course_glossary_name_sanitizes_and_avoids_collision(tmp_path):
    assert packs.course_glossary_name("数学 101") == "数学 101.md"
    assert packs.course_glossary_name("  a/b:c*  ") == "a-b-c-.md"        # Windows 非法字符
    assert packs.course_glossary_name("") == "术语表.md"                   # 空名兜底
    assert packs.course_glossary_name("..") == "术语表.md"                 # 只剩点 → 兜底
    assert packs.course_glossary_name("机器..学习") == "机器-学习.md"      # 消掉 ..（白名单拒它）
    assert len(packs.course_glossary_name("x" * 400)) <= 128              # 限长过白名单
    # 撞名追加后缀——**绝不覆盖**别人的文件（大小写按 Windows 口径）
    assert packs.course_glossary_name("数学", ["数学.md"]) == "数学-2.md"
    assert packs.course_glossary_name("数学", ["数学.md", "数学-2.md"]) == "数学-3.md"
    assert packs.course_glossary_name("CS", ["cs.md"]) == "CS-2.md"
    # 派生结果必须过 glossary 的安全白名单（这是写盘的唯一通道）
    for nm in ("数学 101", "a/b:c*", "", "..", "机器..学习"):
        assert gl.safe_glossary_path(tmp_path, packs.course_glossary_name(nm)) is not None, nm


def test_pack_name_problem_rejects_marker_breakers():
    """包名会写进注释标记行：换行会把一行拆两行、`#` 会变成注释头——都必须拒。"""
    assert packs.name_problem("数学") is None
    assert packs.name_problem(" 数学 ") is None
    assert packs.name_problem("x\ny") is not None
    assert packs.name_problem("x\ry") is not None
    assert packs.name_problem("#数学") is not None
    assert packs.name_problem("") is not None
    assert packs.name_problem(None) is not None
    assert packs.name_problem("x" * 61) is not None


def test_install_block_dedupes_case_insensitively_keeping_user_translation():
    lines = gl.parse_lines("# 我的表\nNeural Network = 我自己的译名\n")
    out, info, err = packs.install_block(
        lines, "小包", [("neural network", "神经网络"), ("brand new", "全新")])
    assert err is None and info["added"] == 1 and info["skipped"] == ["neural network"]
    assert info["overwritten"] == [] and info["dropped"] == []
    text = gl.render_lines(out)
    assert "Neural Network = 我自己的译名" in text    # 用户译名未被覆盖（原样那一行）
    assert "brand new = 全新" in text


def test_install_block_reports_overwritten_and_dropped_on_reinstall():
    """评审修复②：重新安装 = 块内条目**以包为准**重写——被改译/被移除的必须如实回报。

    块外的同名条目是受保护的（skipped），块内的不是：用户通过术语页改过的块内译名会被
    包的译名重置、包新版里删掉的词条会被带走。这不是错误（整块重写正是「重新安装」的
    定义），但绝不能静默——端点的 overwritten / dropped 就是给 UI 与用户看的。"""
    # 先装一版（alpha 一条）
    out, info, err = packs.install_block(gl.parse_lines(""), "小包", [("alpha", "阿尔法")])
    assert err is None and info["added"] == 1
    # 用户改译块内那条（走术语页的同一条行级路径）
    out, err = gl.update_entry(out, "alpha", "alpha", "我改的阿尔法")
    assert err is None
    # 再装新版：alpha 译名不同 + 新增 beta + 旧版块里有 gamma（新版没有）。
    # gamma 必须插在**结束标记之前**（块内）——add_entry 是 EOF 追加，会落在块外，
    # 那样重新安装本就不该动它（块外条目受保护，另有用例覆盖）。
    text = gl.render_lines(out)
    text = text.replace(packs.mark_end("小包"), "gamma = 伽马\n" + packs.mark_end("小包"))
    assert "gamma = 伽马" in text and text.index("gamma") < text.index(packs.mark_end("小包"))
    out2 = gl.parse_lines(text)
    out3, info2, err2 = packs.install_block(
        out2, "小包", [("alpha", "阿尔法"), ("beta", "贝塔")])
    assert err2 is None
    assert info2["overwritten"] == [{"en": "alpha", "was": "我改的阿尔法", "now": "阿尔法"}]
    assert info2["dropped"] == ["gamma"]
    # added = **写进新块的条目数**：旧块先被移除，故 alpha（重写）+ beta（新增）都算
    assert info2["added"] == 2
    text = gl.render_lines(out3)
    assert "alpha = 阿尔法" in text and "我改的阿尔法" not in text
    assert "gamma" not in text


def test_install_block_rejects_over_cap_and_keeps_lines_untouched():
    lines = [{"kind": "entry", "en": f"e{i}", "zh": f"z{i}", "raw": None, "indent": ""}
             for i in range(gl.MAX_ENTRIES - 1)]
    entries = [("new one", "新一"), ("new two", "新二")]
    out, info, err = packs.install_block(lines, "小包", entries)
    assert out is lines and info == {} and err
    assert "上限" in err and "未改动" in err
    # 边界：正好装满（existing + fresh == MAX）必须放行
    out2, info2, err2 = packs.install_block(lines, "小包", entries[:1])
    assert err2 is None and info2["added"] == 1
    assert gl.count_entries(out2) == gl.MAX_ENTRIES


def test_install_block_rejects_empty_pack():
    out, info, err = packs.install_block(gl.parse_lines(""), "空包", [])
    assert info == {} and err and "没有可安装的词条" in err


def test_uninstall_reports_missing_and_broken_markers_without_changing_lines():
    text = "# 头\na = 甲\n"
    lines = gl.parse_lines(text)
    out, removed, err = packs.uninstall_block(lines, "数学")
    assert out is lines and removed == 0 and err and "没有安装" in err
    broken = gl.parse_lines("# 头\na = 甲\n" + packs.mark_start("数学") + "\nb = 乙\n")
    out2, removed2, err2 = packs.uninstall_block(broken, "数学")
    assert out2 is broken and removed2 == 0
    assert err2 and "标记不完整" in err2 and "未改动文件" in err2
    # 只有结束标记（缺头）同样拒动
    tail_only = gl.parse_lines("# 头\na = 甲\n" + packs.mark_end("数学") + "\n")
    out3, _, err3 = packs.uninstall_block(tail_only, "数学")
    assert out3 is tail_only and err3 and "标记不完整" in err3


def test_installed_names_reads_start_markers():
    lines = gl.parse_lines(
        "# 头\n"
        + packs.mark_start("数学") + "\nx = 甲\n" + packs.mark_end("数学") + "\n"
        + packs.mark_start("医疗") + "\ny = 乙\n" + packs.mark_end("医疗") + "\n"
        + packs.mark_start("数学") + "\nz = 丙\n" + packs.mark_end("数学") + "\n")
    assert packs.installed_names(lines) == ["数学", "医疗"]      # 去重保序


# —— 3/4) 端点：装 / 读回 / 卸 往返，「文件是唯一事实来源」 ——

async def test_install_creates_course_glossary_and_binds_it(pack_env):
    """课程未绑定术语表 → 自动建 `glossary/<课程名>.md` + 写回绑定（复用课程端点口径）。"""
    async with asgi_client() as c:
        course = await _new_course(c, "微积分基础")
        assert course["glossary"] is None
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        path = pack_env / "glossary" / body["file"]
        assert path.is_file()
        assert body["created_file"] is True and body["rel"] == f"glossary/{body['file']}"
        # 表头是标准新建表头
        assert path.read_text(encoding="utf-8").startswith(gl.NEW_FILE_HEADER)
        # 课程表里绑定已落盘（文件为事实来源）
        import json as _json
        data = _json.loads((pack_env / "courses.json").read_text(encoding="utf-8"))
        assert data["courses"][0]["glossary"] == f"glossary/{body['file']}"
        # 课程视图的术语条数与文件名口径同源
        row = (await c.get("/api/courses")).json()["courses"][0]
        assert row["terms"] == body["count"] and row["glossary_missing"] is False
        # 包列表里显示「已装到」
        packs_body = (await c.get("/api/glossary/packs")).json()
        item = next(p for p in packs_body["packs"] if p["name"] == "数学")
        assert item["installed_in"] == [body["file"]]
        assert "entries" not in item                      # 列表不带全部词条（体积）
        assert item["count"] >= MIN_ENTRIES

        # —— 卸载：文件回到「只剩表头」，条目数 0 ——
        r2 = await c.post("/api/glossary/packs/uninstall",
                          json={"pack": "数学", "file": body["file"]})
        assert r2.status_code == 200, r2.text
        left = gl.read_file(path)
        assert gl.count_entries(left["lines"]) == 0
        assert packs.mark_start("数学") not in left["text"]      # 标记也一并清掉
        assert left["text"].startswith(gl.NEW_FILE_HEADER)
        item2 = next(p for p in (await c.get("/api/glossary/packs")).json()["packs"]
                     if p["name"] == "数学")
        assert item2["installed_in"] == []


async def test_uninstall_restores_file_byte_identical(pack_env):
    """装→卸往返后，文件内容**逐字节**等于装之前（注释/空行/行尾一个都没动）。

    必须用 write_bytes/read_bytes 写读原件：Path.write_text 在 Windows 上会把 `\\n`
    翻成 `\\r\\n`（glossary.py 抬头记过这个坑），拿它造「原件」会让比对失真。"""
    target = pack_env / "glossary" / "CS.md"
    async with asgi_client() as c:
        course = await _new_course(c, "CS", "glossary/CS.md")
        for original in ("# 我的表\n\nname = 名字\n# 尾注释\n",
                         "# CRLF 原件\r\nname = 名字\r\n"):
            target.write_bytes(original.encode("utf-8"))
            r = await c.post("/api/glossary/packs/install",
                             json={"pack": "计算机", "course": course["id"]})
            assert r.status_code == 200 and r.json()["added"] > 0
            installed = target.read_bytes()
            assert installed != original.encode("utf-8")           # 确实写进去了
            assert (b"\r\n" in installed) == (b"\r\n" in original.encode("utf-8")), \
                "安装不得改动原件的行尾风格"
            r2 = await c.post("/api/glossary/packs/uninstall",
                              json={"pack": "计算机", "file": "CS.md"})
            assert r2.status_code == 200
            assert target.read_bytes() == original.encode("utf-8"), \
                "卸载后必须逐字节回到装之前（注释/空行/行尾原样）"


async def test_user_entries_survive_install_and_uninstall(pack_env):
    """**双标记**的核心用例：EOF 追加的用户词条既不被动、也不被吞。"""
    target = pack_env / "glossary" / "数学.md"
    target.write_text(gl.NEW_FILE_HEADER, encoding="utf-8")
    async with asgi_client() as c:
        course = await _new_course(c, "数学", "glossary/数学.md")
        assert (await c.post("/api/glossary/packs/install",
                             json={"pack": "数学", "course": course["id"]})
                ).status_code == 200
        # 用户装完之后在术语页（EOF 追加路径）加自己的词条——它落在结束标记**之后**
        for en, zh in (("my own term", "我自己的词"), ("another one", "又一枚")):
            assert (await c.post("/api/glossary", json={"file": "数学.md", "op": "add",
                                                        "en": en, "zh": zh})
                    ).status_code == 200
        packed = gl.read_file(target)
        assert packs.installed_names(packed["lines"]) == ["数学"]
        n_after_install = gl.count_entries(packed["lines"])

        r = await c.post("/api/glossary/packs/uninstall",
                         json={"pack": "数学", "file": "数学.md"})
        assert r.status_code == 200, r.text
        after = gl.read_file(target)
        text = after["text"]
        assert "my own term = 我自己的词" in text and "another one = 又一枚" in text
        assert gl.count_entries(after["lines"]) == 2        # 只剩用户那两条
        # 移除行数 = 包词条 + 两行标记（精确到行，证明「只删块」而不是「删到文件尾」）
        pack_count = packs.find_pack(PACKS_REAL, "数学")["count"]
        assert r.json()["removed_lines"] == pack_count + 2
        assert n_after_install == pack_count + 2
        assert packs.installed_names(after["lines"]) == []


async def test_reinstall_replaces_block_instead_of_stacking(pack_env):
    """同包重复安装 = 先卸后装：块只有一份，且挪到 EOF（不会叠成两块）。"""
    target = pack_env / "glossary" / "数学.md"
    target.write_text(gl.NEW_FILE_HEADER + "user term = 用户词\n", encoding="utf-8")
    async with asgi_client() as c:
        course = await _new_course(c, "数学", "glossary/数学.md")
        first = (await c.post("/api/glossary/packs/install",
                              json={"pack": "数学", "course": course["id"]})).json()
        second = (await c.post("/api/glossary/packs/install",
                               json={"pack": "数学", "course": course["id"]})).json()
        assert second["added"] == first["added"], "重装应重新写全量（先卸后装）"
        lines = gl.read_file(target)["lines"]
        complete, dangling_s, dangling_e = packs.block_spans(lines, "数学")
        assert len(complete) == 1 and not dangling_s and not dangling_e
        assert gl.count_entries(lines) == second["count"] == first["count"]
        assert "user term = 用户词" in gl.render_lines(lines)     # 用户词条还在


async def test_dedup_is_reported_and_translation_not_overwritten(pack_env):
    """去重：用户已有优先——跳过数如实回报，用户译名一字不改。"""
    target = pack_env / "glossary" / "数学.md"
    target.write_text("# 我自己的表\nderivative = 我译的导数\n",
                      encoding="utf-8")
    async with asgi_client() as c:
        course = await _new_course(c, "数学", "glossary/数学.md")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] == ["derivative"] and body["skipped_count"] == 1
        assert body["added"] == body["count"] - 1
    text = target.read_text(encoding="utf-8")
    assert "derivative = 我译的导数" in text
    # 「没有任何第二条 derivative」要按**词条**数，不能按子串数（数学包里本来就有
    # partial derivative / antiderivative 这类包含 derivative 的词条）
    lines = gl.read_file(target)["lines"]
    assert sum(1 for ln in lines if ln["kind"] == "entry"
               and ln["en"].lower() == "derivative") == 1


async def test_over_cap_install_returns_400_and_file_untouched(pack_env):
    """上限预检：已有 + 新增 > MAX_ENTRIES → 400，且**文件逐字节不变**。"""
    target = pack_env / "glossary" / "数学.md"
    target.write_text(gl.NEW_FILE_HEADER
                      + "".join(f"filler{i} = 填充{i}\n"
                                for i in range(gl.MAX_ENTRIES - 10)),
                      encoding="utf-8")
    before = target.read_bytes()
    async with asgi_client() as c:
        course = await _new_course(c, "数学", "glossary/数学.md")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        assert r.status_code == 400, r.text
        err = r.json()["error"]
        assert "上限" in err and str(gl.MAX_ENTRIES) in err and "未改动" in err
    assert target.read_bytes() == before, "超限时一个字节都不许写"


async def test_install_without_course_is_rejected(pack_env):
    """没有课程可装（当前「未分类」且没给 course）→ 400 明确文案，不装进虚空。"""
    async with asgi_client() as c:
        r = await c.post("/api/glossary/packs/install", json={"pack": "数学"})
        assert r.status_code == 400
        assert "未分类" in r.json()["error"]
        assert not list((pack_env / "glossary").glob("*.md")), "拒绝时不得留下新文件"
        # 给了不存在的 course → 404
        assert (await c.post("/api/glossary/packs/install",
                             json={"pack": "数学", "course": "nope"})).status_code == 404
        # 未知包 → 404
        course = await _new_course(c, "数学")
        assert (await c.post("/api/glossary/packs/install",
                             json={"pack": "不存在的包", "course": course["id"]})
                ).status_code == 404
        # 未知字段 → 400（白名单纪律）
        assert (await c.post("/api/glossary/packs/install",
                             json={"pack": "数学", "course": course["id"], "x": 1})
                ).status_code == 400


async def test_installed_pack_installs_into_active_course_by_default(pack_env):
    """course 省略 = 当前课程（active）——「安装到这门课」正是这条路径。"""
    async with asgi_client() as c:
        course = await _new_course(c, "心理学课")
        assert (await c.post("/api/courses/active",
                             json={"active": course["id"]})).status_code == 200
        r = await c.post("/api/glossary/packs/install", json={"pack": "心理学"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["course"] == course["id"] and body["course_name"] == "心理学课"
        assert (pack_env / "glossary" / body["file"]).is_file()


async def test_two_packs_coexist_and_uninstall_leaves_the_other(pack_env):
    """两包并存：各占一个块，卸一个不许动另一个（也不许动用户词条）。

    并集去重的**已知副作用**在最后一段如实钉住：两包对同一英文词给出不同译名时
    （数学 derivative=导数 / 经济 derivative=衍生品），先装的那个胜出、后装的那条被
    跳过——所以卸载先装的那个会把它一起带走。端点必须把这份名单回报（`shared`），
    而不是假装没这回事。"""
    target = pack_env / "glossary" / "数学.md"
    target.write_text(gl.NEW_FILE_HEADER, encoding="utf-8")
    async with asgi_client() as c:
        course = await _new_course(c, "数学", "glossary/数学.md")
        for name in ("数学", "经济"):
            assert (await c.post("/api/glossary/packs/install",
                                 json={"pack": name, "course": course["id"]})
                    ).status_code == 200
        lines = gl.read_file(target)["lines"]
        assert packs.installed_names(lines) == ["数学", "经济"]
        r = await c.post("/api/glossary/packs/uninstall",
                         json={"pack": "数学", "file": "数学.md"})
        assert r.status_code == 200
        after = gl.read_file(target)
        assert packs.installed_names(after["lines"]) == ["经济"]
        econ = packs.find_pack(PACKS_REAL, "经济")
        math_pack = packs.find_pack(PACKS_REAL, "数学")
        overlap = {en.lower() for en, _ in math_pack["entries"]} & \
                  {en.lower() for en, _ in econ["entries"]}
        assert gl.count_entries(after["lines"]) == econ["count"] - len(overlap)
        # 重合名单如实回报（不自动补写——卸载只做卸载）
        assert r.json()["shared"] == ["derivative"] and r.json()["shared_count"] == 1


async def test_uninstall_missing_marker_is_409_and_file_untouched(pack_env):
    target = pack_env / "glossary" / "CS.md"
    target.write_text("# 我的表\na = 甲\n", encoding="utf-8")
    before = target.read_bytes()
    async with asgi_client() as c:
        r = await c.post("/api/glossary/packs/uninstall",
                         json={"pack": "数学", "file": "CS.md"})
        assert r.status_code == 409, r.text
        assert "没有安装" in r.json()["error"]
        # 标记不完整（手改坏）同样 409 且不动文件
        target.write_text("# 我的表\na = 甲\n" + packs.mark_start("数学") + "\nb = 乙\n",
                          encoding="utf-8")
        before2 = target.read_bytes()
        r2 = await c.post("/api/glossary/packs/uninstall",
                          json={"pack": "数学", "file": "CS.md"})
        assert r2.status_code == 409 and "标记不完整" in r2.json()["error"]
    assert target.read_bytes() == before2


# —— 5) 安全边界（包名 / 文件名 / 绑定路径） ——

async def test_pack_endpoints_reject_path_escape_and_marker_injection(pack_env):
    async with asgi_client() as c:
        course = await _new_course(c, "数学")
        # 文件名越界（与 POST /api/glossary 同一把闸）
        for bad in ("../config.json", "sub/x.md", "C:/Windows/win.ini", "x.txt", None):
            r = await c.post("/api/glossary/packs/uninstall",
                             json={"pack": "数学", "file": bad})
            assert r.status_code in (400, 404), (bad, r.text)
        # 包名注入标记行：换行 / #
        for bad in ("数学\n# —— 结束包：数学 ——", "#数学", "x" * 61, ""):
            r = await c.post("/api/glossary/packs/install",
                             json={"pack": bad, "course": course["id"]})
            assert r.status_code == 400, (bad, r.text)
    # 越界尝试后仓库里不该多出任何东西（只认 tmp_path 内的 glossary/）
    assert not (pack_env / "sub").exists()


async def test_course_name_with_separators_stays_inside_glossary_dir(pack_env):
    """课程名带 `/` `..` 也不许把文件写出 glossary/ 之外。"""
    async with asgi_client() as c:
        course = await _new_course(c, "恶意/../名字")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        assert r.status_code == 200, r.text
        path = pack_env / "glossary" / r.json()["file"]
        assert path.parent == (pack_env / "glossary").resolve()
        assert "恶意" in path.name and path.name.endswith(".md")
        assert not (pack_env / "名字.md").exists()
        assert not (pack_env / "glossary" / "名字.md").exists()
        assert not (pack_env / "恶意").exists()


async def test_course_bound_glossary_outside_glossary_dir_is_rejected(pack_env):
    """课程绑定指向 glossary/ 之外（courses.json 手改过）→ 400 + 不写盘。

    理由：glossary.write_file 的包含性校验会拒绝这种路径（防越界写），端点必须先给出
    能看懂的原因，而不是抛 500。"""
    async with asgi_client() as c:
        course = await _new_course(c, "别处", "notes/notes.md")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        assert r.status_code == 400, r.text
        assert "不在 glossary/ 下" in r.json()["error"]
    assert not (pack_env / "notes" / "notes.md").exists()


async def test_pack_install_through_terms_endpoint_counts_agree(pack_env):
    """跨端点一致性：装完之后 GET /api/glossary 的条目数 = 包条目数（同一解析器）。"""
    async with asgi_client() as c:
        course = await _new_course(c, "数学")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        fname = r.json()["file"]
        view = (await c.get("/api/glossary")).json()
        f = next(f for f in view["files"] if f["name"] == fname)
        assert f["count"] == r.json()["count"] == packs.find_pack(PACKS_REAL, "数学")["count"]
        assert f["entries"][0] == {"en": "derivative", "zh": "导数"}


# —— 评审修复回归（2026-10-14 独立评审 5 项）——
# 每一条都先复现过（见本次交付说明），这里把修好的行为钉死。


@pytest.fixture
def packs_dir_env(pack_env, monkeypatch):
    """把 PACKS_DIR 换成 tmp 里的一套包目录（可放坏包/超大包，不碰仓库真包）。"""
    d = pack_env / "glossary_packs"
    d.mkdir()
    monkeypatch.setattr(main, "PACKS_DIR", d)
    return d


def _write_pack(d: Path, name: str, body: str, encoding: str = "utf-8") -> Path:
    p = d / f"{name}.md"
    p.write_bytes(body.encode(encoding))
    return p


async def test_non_utf8_pack_does_not_break_list_or_install(packs_dir_env):
    """评审①：一个非 UTF-8（记事本另存 ANSI/GBK）的包只坏自己那张卡——不 500 整张列表，
    也不让**装任何包**都 500（find_pack 会解析到它）。"""
    good = _write_pack(packs_dir_env, "好包", "# —— 包：好包 ——\n# 简介：正常\n# 版本：v1\n"
                                       "alpha = 阿尔法\n")
    bad = _write_pack(packs_dir_env, "坏包", "# —— 包：坏包 ——\nb = 乙\n", encoding="gbk")
    # 逐文件容错：坏包带 error 文案、条目数 0，好包照常
    recs = {r["name"]: r for r in packs.list_packs(packs_dir_env)}
    assert recs["坏包"]["error"] and "UTF-8" in recs["坏包"]["error"]
    assert recs["坏包"]["count"] == 0 and recs["坏包"]["entries"] == []
    assert recs["好包"]["error"] is None and recs["好包"]["count"] == 1
    assert packs.find_pack(packs_dir_env, "坏包")["error"]
    async with asgi_client() as c:
        r = await c.get("/api/glossary/packs")                   # 关键：不再是 500
        assert r.status_code == 200, r.text
        items = {p["name"]: p for p in r.json()["packs"]}
        assert items["坏包"]["error"] and items["好包"]["count"] == 1
        course = await _new_course(c, "评审课")
        r_bad = await c.post("/api/glossary/packs/install",
                             json={"pack": "坏包", "course": course["id"]})
        assert r_bad.status_code == 400, r_bad.text              # 明确 400，不是 500
        assert "装不了「坏包」" in r_bad.json()["error"]
        # 坏包在目录里也不再妨碍装好包（旧实现这里会 500）
        r_ok = await c.post("/api/glossary/packs/install",
                            json={"pack": "好包", "course": course["id"]})
        assert r_ok.status_code == 200 and r_ok.json()["added"] == 1
        # 列表里「已装到」照常工作（坏包不参与）
        items2 = {p["name"]: p for p in (await c.get("/api/glossary/packs")).json()["packs"]}
        assert items2["好包"]["installed_in"] == [r_ok.json()["file"]]
    assert bad.read_bytes().decode("gbk").startswith("#")        # 坏包文件一字未动


async def test_reinstall_reports_overwritten_translations(pack_env):
    """评审②：重新安装会把块内条目重置为包的译名——响应必须如实回报，不再静默。"""
    async with asgi_client() as c:
        course = await _new_course(c, "数学")
        first = (await c.post("/api/glossary/packs/install",
                              json={"pack": "数学", "course": course["id"]})).json()
        fname = first["file"]
        assert first["overwritten_count"] == 0 and first["dropped_count"] == 0
        # 用户走术语页改块内某条的译名（正常路径，200）
        assert (await c.post("/api/glossary", json={"file": fname, "op": "update",
                                                    "old_en": "derivative",
                                                    "en": "derivative",
                                                    "zh": "我改的导数"})).status_code == 200
        assert "derivative = 我改的导数" in (pack_env / "glossary" / fname).read_text(
            encoding="utf-8")
        r = await c.post("/api/glossary/packs/install",
                         json={"pack": "数学", "course": course["id"]})
        body = r.json()
        assert r.status_code == 200
        assert body["overwritten"] == [{"en": "derivative", "was": "我改的导数", "now": "导数"}]
        assert body["overwritten_count"] == 1
        assert body["dropped"] == [] and body["dropped_count"] == 0
        assert body["added"] == body["count"]        # 重装后块内条数 = 包条数
    text = (pack_env / "glossary" / fname).read_text(encoding="utf-8")
    assert "derivative = 导数" in text and "我改的导数" not in text


async def test_install_failure_leaves_no_side_effects(pack_env, monkeypatch):
    """评审③：装不下（400）**不许**留下副作用——不建表、不写课程绑定、不广播。

    旧顺序是「先建表 + 落盘绑定 + 广播，再校验」，于是请求失败却改了课程表。"""
    big = [f"w{i} = 词{i}" for i in range(gl.MAX_ENTRIES + 5)]
    d = pack_env / "glossary_packs"
    d.mkdir(exist_ok=True)
    _write_pack(d, "大包",
                "# —— 包：大包 ——\n# 简介：超大\n# 版本：v1\n" + "\n".join(big) + "\n")
    # 再放一个正常包（照抄仓库真包内容）：证明失败之后同一门课照常能装
    _write_pack(d, "数学", (PACKS_REAL / "数学.md").read_text(encoding="utf-8"))
    monkeypatch.setattr(main, "PACKS_DIR", d)
    sent: list = []

    async def spy(payload):
        sent.append(payload)

    real = main.hub.broadcast
    monkeypatch.setattr(main.hub, "broadcast", spy)
    try:
        async with asgi_client() as c:
            course = await _new_course(c, "装不下的课")
            before = (pack_env / "courses.json").read_bytes()
            sent.clear()                 # 建课本身会广播一帧 courses——只看安装这一次
            r = await c.post("/api/glossary/packs/install",
                             json={"pack": "大包", "course": course["id"]})
            assert r.status_code == 400 and "上限" in r.json()["error"]
            # 课程表逐字节未变（绑定没被写）、glossary/ 下没多出文件、没有 courses 广播
            assert (pack_env / "courses.json").read_bytes() == before
            assert list((pack_env / "glossary").glob("*.md")) == []
            assert [m for m in sent if m.get("type") == "courses"] == []
            # 同一门课再装正常包 → 一切照常（失败没留下半截状态）
            r2 = await c.post("/api/glossary/packs/install",
                              json={"pack": "数学", "course": course["id"]})
            assert r2.status_code == 200 and r2.json()["created_file"] is True
            row = next(x for x in (await c.get("/api/courses")).json()["courses"]
                       if x["id"] == course["id"])
            assert row["glossary"] == f"glossary/{r2.json()['file']}"
    finally:
        monkeypatch.setattr(main.hub, "broadcast", real)


async def test_uninstall_target_follows_selected_file(pack_env):
    """评审④：同一包装进两门课后，「卸载」动的必须是**判据指的那份表**。

    旧实现：按钮按 GL.sel 启用、请求却打 installed_in[0]——在 B 表视图里点卸载卸的是 A。"""
    async with asgi_client() as c:
        a = await _new_course(c, "第一课")
        b = await _new_course(c, "第二课")
        for course in (a, b):
            assert (await c.post("/api/glossary/packs/install",
                                 json={"pack": "数学", "course": course["id"]})
                    ).status_code == 200
        items = {p["name"]: p for p in (await c.get("/api/glossary/packs")).json()["packs"]}
        installed = items["数学"]["installed_in"]
        assert len(installed) == 2                     # 两门课都装上了（多课是正常用法）
        # 前端判据（packUninstallTarget）：选中的表在 installed_in 里就用它，否则第一份
        for target, sel in ((installed[0], installed[0]), (installed[0], "别的表.md"),
                            (installed[1], installed[1])):
            assert (sel if sel in installed else installed[0]) == target
        html = (ROOT_REAL / "app" / "web" / "index.html").read_text(encoding="utf-8")
        assert "function packUninstallTarget(p) {" in html
        assert "installed.includes(GL.sel) ? GL.sel : (installed[0] || GL.sel)" in html
        fn = html.split("async function uninstallPack(p, btn) {", 1)[1].split("\n}", 1)[0]
        assert "const file = packUninstallTarget(p);" in fn     # 判据与动作同源
        # 端点侧：卸「选中的那份」不会碰另一份
        target = installed[1]
        r = await c.post("/api/glossary/packs/uninstall",
                         json={"pack": "数学", "file": target})
        assert r.status_code == 200 and r.json()["file"] == target
        left = {p["name"]: p for p in (await c.get("/api/glossary/packs")).json()["packs"]}
        assert left["数学"]["installed_in"] == [installed[0]]
