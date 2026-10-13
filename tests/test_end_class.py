# -*- coding: utf-8 -*-
"""tests/test_end_class.py — §5 下课一键归档（notes-batch）。

验收口径（docs/notes-batch.md §5）：
- 字幕页工具条「下课」按钮（醒目 = 只此一个金色主按钮）→ 顺序执行：暂停收音 →
  生成摘要 → 导出（§3 到 export_dir；未设置则浏览器下载）；
- 每步成功/失败**逐条**进汇总面板，**单步失败不中断后续**；
- 边界：无笔记（还没上过课）/ 无 API key（摘要跳过并说明）/ 摘要已在跑（等它完成、
  不是本篇的在跑则跳过不排队）；
- config 新键 class_end_confirm（二次确认可关）：白名单 + 布尔枚举校验。

浏览器交互（confirm/面板渲染）不进自动化：这里覆盖 ① 配置键的校验与持久化、
② 流程每一步在**真 ASGI 服务**上的契约（前端就是按这些响应判成败的）、
③ index.html 的结构断言。真机浏览器行为见实施报告与 smoke 的真服务段。
"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import notes as notes_mod
import summarize

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def end_env(monkeypatch):
    """复位「下课」流程会碰到的模块级状态（hub 指针 / 摘要守卫 / translator / 暂停位
    / runtime['config'] 内容）。

    两条都是踩过的坑：
    ① config 必须**逐层**快照还原——`dict(runtime)` 是浅拷贝，`saved['config']` 与
       `runtime['config']` 是同一个 dict，不还原就漏进别的模块（test_exporters 的
       导出用例会拿到 JSON 而不是文件流）；
    ② status / hub 上的字段不在 runtime 里，得单独存——漏掉 `hub.transcript_path`
       会让 test_notes 的「无笔记 → 404」用例拿到上一篇笔记（顺序相关失败）。"""
    monkeypatch.setattr(main, "apply_segmenter", lambda: None)
    saved = dict(main.runtime)
    saved_cfg = dict(main.runtime["config"])
    saved_summary = main.status["summary"]
    saved_paused = main.status["paused"]
    saved_hub = (main.hub.transcript_path, main.hub.stamp, main.hub.note_seg,
                 main.hub.t0_audio, list(main.hub.times))
    main.runtime["config"].clear()
    main.runtime["config"].update(main.DEFAULT_CONFIG)
    main.status["summary"] = {"state": "idle", "done": 0, "total": 0,
                              "failed": 0, "path": None, "error": None}
    main.runtime["summarizing"] = False
    main.runtime["summary_task"] = None
    main.hub.transcript_path = None
    main.runtime["translator"] = None
    main.status["paused"] = False
    yield main
    main.runtime.clear()
    main.runtime.update(saved)
    main.runtime["config"].clear()
    main.runtime["config"].update(saved_cfg)
    main.status["summary"] = saved_summary
    main.status["paused"] = saved_paused
    (main.hub.transcript_path, main.hub.stamp, main.hub.note_seg,
     main.hub.t0_audio, main.hub.times) = saved_hub


class FakeTranslator:
    """与 test_summarize.FakeTranslator 同形（只要 chat_with_retry + model）。"""

    def __init__(self):
        self.model = "deepseek-chat"

    async def chat_with_retry(self, payload: dict) -> str:
        system = payload["messages"][0]["content"]
        if system == summarize.MAP_SYSTEM:
            return "### 要点\n- 一块要点"
        if system == summarize.REDUCE_SYSTEM:
            return "## 概览\n下课归档摘要\n"
        raise AssertionError("未知 system prompt")


async def _wait(pred, timeout: float = 3.0) -> bool:
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def start_note(en="hello there"):
    """起一篇"正在记录"的笔记（= 「下课」要归档的那篇）→ 笔记路径。"""
    hub = main.Hub()
    p = hub.start_transcript()
    hub.append("12:00:01", en, "", None)
    main.hub.transcript_path = p
    return p


# ---------------- config 新键：class_end_confirm ----------------

def test_class_end_confirm_in_whitelist_and_default_true(end_env):
    assert "class_end_confirm" in main.ALLOWED_CONFIG_KEYS      # 硬规矩⑦
    assert main.DEFAULT_CONFIG["class_end_confirm"] is True     # 默认要弹确认
    assert main.CONFIG_VALUE_CHOICES["class_end_confirm"] == (False, True)


async def test_class_end_confirm_roundtrip_and_invalid(end_env):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"class_end_confirm": False})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "changed": {"class_end_confirm": False}}
        saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["class_end_confirm"] is False
        bad = await c.post("/api/config", json={"class_end_confirm": "yes"})
        assert bad.status_code == 400 and "class_end_confirm" in bad.json()["error"]
        back = await c.post("/api/config", json={"class_end_confirm": True})
        assert back.status_code == 200
    assert main.runtime["config"]["class_end_confirm"] is True


# ---------------- 流程契约（前端按这些响应判每步成败） ----------------

async def test_end_class_flow_pause_summarize_export(end_env, tmp_path):
    """三步全成功的满链：暂停 → 摘要（无旧摘要 → 生成）→ 导出（落盘 export_dir）。

    这正是前端 endClass() 依次发的请求与判定依据（响应形状即契约）。"""
    note = start_note()
    main.runtime["translator"] = FakeTranslator()
    dest = tmp_path / "导出"
    async with asgi_client() as c:
        # ① 暂停（endClass 第 1 步；判据 = ok + paused）
        rp = await c.post("/api/pause")
        assert rp.status_code == 200 and rp.json() == {"ok": True, "paused": True}
        # 前两件：当前笔记（无 current = 还没上过课，前端会直接提示不空转）
        lst = (await c.get("/api/notes/list")).json()
        assert lst["current"] == note.name
        # ② 摘要：先看有没有（有 → 跳过）
        s0 = (await c.get("/api/notes/summary",
                          params={"name": note.name})).json()
        assert s0["ok"] is True and s0["exists"] is False
        rs = await c.post("/api/notes/summarize",
                          json={"name": note.name})
        assert rs.status_code == 200 and rs.json()["ok"] is True
        assert rs.json()["overwrite"] is False          # 前端据它决定文案
        task = main.runtime["summary_task"]
        assert await _wait(lambda: main.status["summary"]["state"] == "done")
        await task
        # ③ 导出：设了 export_dir → 落盘 JSON {ok, path}
        assert (await c.post("/api/config",
                             json={"export_dir": str(dest)})).status_code == 200
        re_ = await c.get("/api/notes/export", params={"fmt": "md", "name": ""})
        assert re_.status_code == 200 and re_.json()["ok"] is True
        out = Path(re_.json()["path"])
    assert out.is_file() and out.parent == dest
    assert "下课归档摘要" not in out.read_text(encoding="utf-8")   # 导出的是笔记，不是摘要
    assert (note.parent / (note.stem + ".summary.md")).is_file()    # 摘要在笔记旁


async def test_end_class_without_key_skips_summary(end_env):
    """无 API key（translator=None）→ 摘要步骤 409，文案含「翻译未启用」——
    前端据此判为「跳过」（warn）而不是失败（err）。"""
    start_note()
    async with asgi_client() as c:
        r = await c.post("/api/notes/summarize")
    assert r.status_code == 409
    assert "翻译未启用" in r.json()["error"]
    assert main.runtime["summarizing"] is False      # 没有留下半拉状态


async def test_end_class_existing_summary_reports_skip(end_env):
    """已有摘要 → 跳过的判据（GET exists=true），且**不会**被这次归档重生成。"""
    note = start_note()
    main.runtime["translator"] = FakeTranslator()
    summary = note.with_name(note.stem + ".summary.md")
    summary.write_text("## 概览\n人工修订版，别动\n", encoding="utf-8")
    async with asgi_client() as c:
        s = (await c.get("/api/notes/summary", params={"name": note.name})).json()
        # 已有 → 前端在这一步直接标「跳过」，不发 POST；这里验证判据本身
        assert s["exists"] is True and s["has_backup"] is False
    assert summary.read_text(encoding="utf-8") == "## 概览\n人工修订版，别动\n"
    assert not summary.with_name(note.stem + ".summary.bak.md").exists()


async def test_end_class_no_note_means_nothing_to_archive(end_env):
    """还没上过课：/api/notes/list 的 current 为 null → 前端明确提示、一步不跑。"""
    main.hub.transcript_path = None
    async with asgi_client() as c:
        lst = (await c.get("/api/notes/list")).json()
    assert lst["ok"] is True and lst["current"] is None


async def test_end_class_running_summary_path_is_identifiable(end_env):
    """摘要已在跑：status.summary.path 能判定「是不是这篇」——前端据此决定
    等完成（同一篇）还是跳过不排队（另一篇）。"""
    note = start_note()
    main.status["summary"] = {"state": "running", "done": 1, "total": 4, "failed": 0,
                              "path": str(note.with_name(note.stem + ".summary.md")),
                              "error": None, "backup": None}
    other = "lecture_20260101_0900.md"
    async with asgi_client() as c:
        s = (await c.get("/api/status")).json()
    p = s["summary"]["path"].replace("\\", "/")
    assert p.endswith("/" + note.stem + ".summary.md")
    assert not p.endswith("/" + other.replace(".md", "") + ".summary.md")
    assert s["summary"]["state"] == "running" and s["summary"]["total"] == 4


async def test_end_class_export_failure_is_reportable(end_env, monkeypatch):
    """导出失败要给**可判定**的错误（前端把它标成 ✗ 并继续/收尾，不假成功）。"""
    start_note()
    def boom(*a, **kw):
        raise RuntimeError("磁盘爆炸")
    monkeypatch.setattr(main.exporters, "build_pdf", boom)
    async with asgi_client() as c:
        r = await c.get("/api/notes/export", params={"fmt": "pdf", "name": ""})
    assert r.status_code == 500 and r.json()["ok"] is False
    assert "磁盘爆炸" in r.json()["error"]


# ---------------- 前端结构断言 ----------------

def test_toolbar_has_end_class_button_as_only_primary():
    toolbar = HTML.split('<div id="toolbar">', 1)[1].split("</div>", 1)[0]
    assert 'id="btnEndClass"' in toolbar
    assert "🎓 下课" in toolbar
    assert 'class="primary"' in toolbar.split('id="btnEndClass"')[1][:80]
    assert "$('btnEndClass').onclick = () => endClass()" in HTML
    # 醒目 = 金色主按钮样式**真的命中**：选择器必须比 `#toolbar button` 更具体
    # （实测只写 #btnEndClass 会被 id+元素那条盖掉，背景仍是 surface-2）
    assert "#toolbar button#btnEndClass { background:var(--gold)" in HTML


def test_end_class_flow_wiring():
    for anchor in ("async function endClass(", "async function endClassSummary(",
                   "async function waitEndSummary(", "END_SUMMARY_WAIT_MS",
                   "END_STEPS", "'/api/pause'", "'/api/notes/summarize'",
                   "exportNote(current, 'md')", "class_end_confirm",
                   "翻译未启用", "进行中", "另一篇笔记的摘要正在生成",
                   "已有摘要，跳过", "还没有笔记（还没上过课）"):
        assert anchor in HTML, f"index.html 缺少 {anchor}"


def test_end_panel_is_persistent_and_in_masks():
    for anchor in ("endMask", "endSteps", "endSub", "endDone", "endOpenFolder",
                   "setEndStep", "openEndPanel"):
        assert anchor in HTML, f"index.html 缺少 {anchor}"
    masks = HTML.split("const MASKS = ", 1)[1].split("];", 1)[0]
    assert "endMask" in masks and "sumMask" in masks
    # 面板按钮接线（关闭/打开导出文件夹）
    assert "$('endDone').onclick = () => closeModal($('endMask'))" in HTML
    assert "$('endOpenFolder').onclick = () => openExportDir()" in HTML


def test_settings_card_toggles_confirm():
    assert 'id="setEndConfirmChk"' in HTML
    assert "$('setEndConfirmChk').onchange" in HTML
    assert "class_end_confirm: e.target.checked" in HTML
    assert "$('setEndConfirmChk').checked = SETCFG.class_end_confirm !== false" in HTML


def test_end_class_reuses_export_center():
    """③ 导出必须复用 §3 的导出中心（落盘/下载同一实现），不另写一套。"""
    seg = HTML.split("async function endClass(", 1)[1].split("async function", 1)[0]
    assert "exportNote(" in seg and "downloadNote(" not in seg


def test_end_class_does_not_block_on_summary_failure():
    """单步失败不中断：摘要步骤之后**仍然**执行导出（顺序在代码里可见）。"""
    seg = HTML.split("async function endClass(", 1)[1].split("\nasync function", 1)[0]
    assert seg.index("endClassSummary(current)") < seg.index("exportNote(current, 'md')")


def _end_class_body() -> str:
    """endClass 的函数体（到下一个顶层函数定义为止）。"""
    return HTML.split("async function endClass(", 1)[1].split("\nasync function", 1)[0]


# ---------------- 评审2 修复的回归钉子 ----------------

def test_guard_is_set_synchronously_before_any_await():
    """评审2 ①：守卫此前在 await 之后才置位——「点下课 → 弹确认框」之间（手机/LAN 上一次
    慢往返内）连点两下会并行跑两套流程（两次暂停、两次导出，面板文字互相覆盖）。"""
    seg = _end_class_body()
    assert seg.index("endRunning = true") < seg.index("await jfetch('/api/config')")
    assert seg.index("endRunning = true") < seg.index("confirm(")


def test_every_step_survives_transport_failure():
    """评审2 ①：jfetch 抛异常（fetch 被拒/响应体不是 JSON）时，此前会整条穿出 endClass
    ——①②停在进行中、③根本不跑，与「单步失败不中断后续」矛盾。现在每步都有就地兜底。"""
    body = _end_class_body()
    for anchor in ("'✗ 暂停失败：' + e", "✗ 摘要步骤异常：", "✗ 导出异常："):
        assert anchor in body or anchor in HTML, f"缺少 {anchor}"
    summary_fn = HTML.split("async function endClassSummary(", 1)[1].split(
        "\nasync function", 1)[0]
    assert "try {" in summary_fn and "catch (e)" in summary_fn


def test_summary_wait_is_identity_checked():
    """评审2 ②：waitEndSummary 只看全局 state 会把「别的笔记刚跑完」算到这一步头上
    （实测假报「✓ 摘要已生成」而本篇根本没有摘要）——必须只认本篇的 path。"""
    seg = HTML.split("async function waitEndSummary(", 1)[1].split("\nasync function", 1)[0]
    assert "wantName" in seg and "endsWith('/' + wantName)" in seg
    assert "waitEndSummary(want)" in HTML


def test_export_is_anchored_to_the_summarized_note():
    """评审2 ③：第③步此前用 name='' 让服务端重新解析「当前笔记」——摘要等待分钟级，
    期间删掉当前笔记会立刻接力一篇新笔记（§4），于是导出的是空笔记却标 ✓。"""
    seg = _end_class_body()
    assert "exportNote(current, 'md')" in seg
    assert "exportNote('', 'md')" not in seg


def test_summary_toast_is_the_visible_fallback():
    """评审2 ④：面板一关（或刷新）结果就再也看不到——docs 承诺的「汇总 toast」必须发。"""
    assert "function endClassSummaryToast" in HTML
    assert "endClassSummaryToast();" in _end_class_body()          # finally 里发
    toast_fn = HTML.split("function endClassSummaryToast()", 1)[1].split("\n}", 1)[0]
    assert "showToast(" in toast_fn and "END_STEPS.map" in toast_fn


def test_export_waits_for_note_body_to_settle():
    """评审2 补充核对：/api/pause 只置标志位，最多 2 个在飞翻译仍可能 append 进正文——
    秒级读盘会让 md 少最后 1~2 条中文。导出前等 size+mtime_us 连续两次一致。"""
    assert "async function waitNoteStable(" in HTML
    body = HTML.split("async function waitNoteStable(", 1)[1].split("\nasync function", 1)[0]
    assert "/api/notes/raw?name=" in body and "mtime_us" in body
    assert "await waitNoteStable(current);" in _end_class_body()


def test_end_panel_says_reload_drops_pending_steps():
    """编排在前端：刷新/关标签页会丢掉还没跑的步骤（②可能③必然）——如实写在面板抬头，
    别让用户以为「归档了一半」也成立。"""
    seg = _end_class_body()
    assert "刷新/关标签页会丢掉还没跑的步骤" in seg


def test_summary_backup_suffix_matches_notes_sidecar():
    """备份文件名口径：summarize 的 BACKUP_SUFFIX 与 notes 的派生物表同源。"""
    assert notes_mod.SUMMARY_BACKUP_SUFFIX == ".summary.bak.md"
    assert summarize.backup_path(Path("lecture_x.summary.md")).name == \
        "lecture_x" + notes_mod.SUMMARY_BACKUP_SUFFIX
