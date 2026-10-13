# -*- coding: utf-8 -*-
"""tests/test_review_fixes.py — 独立评审（评审1/评审2）发现的问题逐条回归。

覆盖：
- R1-2 courses.json：坏文件时**拒绝写回**（旧实现 load 静默退化成空表 → save 把整张
  课程表抹掉；实测「截断文件后 POST 一个新课」返回 200 且原课程全消失）；
- R1-3 导出不得堵塞事件循环（旧实测 fmt=pdf 处理 661ms、同期事件循环空档 661.7ms）；
- R2-5 模型切换进行中禁删模型（旧实现可 200 真删掉正在加载的目标）；
- R2-6 下载与切模型**双向**互斥 + status['load'] 进度条归属令牌。

R1-1（SRT 锚点）/ R1-4（字体缺失契约）在 test_exporters.py；
R2-8（术语表保形）在 test_glossary_api.py；R2-7/R2-9 为纯前端，真浏览器人工核验。"""
import asyncio
import json
import secrets
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import models as models_mod
import notes as notes_mod


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# —— R1-2：坏课程表拒绝写回 ——

VALID_COURSES = {"active": "jia", "courses": [
    {"id": "jia", "name": "甲", "color": "#93a1b0", "glossary": None}]}


async def test_corrupt_courses_refuses_write_and_leaves_file_intact(tmp_path):
    """核心断言：文件坏了 → 写端点 409，且**文件一个字节都没动**。

    旧实现走的是 load(坏文件)→空表→改→save，把空表落盘 → 整张课程表被静默抹掉。"""
    main.COURSES_PATH.write_text(json.dumps(VALID_COURSES, ensure_ascii=False),
                                 encoding="utf-8")
    good = main.COURSES_PATH.read_bytes()
    main.COURSES_PATH.write_text('{"courses": [{"id": "jia"', encoding="utf-8")
    broken = main.COURSES_PATH.read_bytes()
    assert broken != good

    async with asgi_client() as c:
        r1 = await c.post("/api/courses", json={"name": "乙"})
        assert r1.status_code == 409, "坏课程表下的新增必须被拒绝"
        assert "损坏" in r1.json()["error"] and "拒绝" in r1.json()["error"]
        assert main.COURSES_PATH.read_bytes() == broken, "坏文件被覆盖了！"

        r2 = await c.post("/api/courses/active", json={"active": None})
        assert r2.status_code == 409, "取消选课同样会触发写回，必须一起拦"
        assert main.COURSES_PATH.read_bytes() == broken

        r3 = await c.put("/api/courses/jia", json={"name": "甲改"})
        assert r3.status_code == 409
        r4 = await c.delete("/api/courses/jia")
        assert r4.status_code == 409
        assert main.COURSES_PATH.read_bytes() == broken


async def test_corrupt_courses_read_reports_error_not_silent_empty(tmp_path):
    """读侧也要显式报错——否则 UI 只显示「0 门课」，用户以为课程被删了。"""
    main.COURSES_PATH.write_text("not json at all", encoding="utf-8")
    async with asgi_client() as c:
        r = await c.get("/api/courses")
    body = r.json()
    assert r.status_code == 200
    assert body["courses"] == []
    assert "error" in body and "损坏" in body["error"]
    assert "detail" in body


async def test_missing_courses_file_is_still_normal(tmp_path):
    """真·首次运行（文件不存在）必须照常可写——别把「没有文件」也当成损坏。"""
    assert not main.COURSES_PATH.exists()
    async with asgi_client() as c:
        r = await c.post("/api/courses", json={"name": "甲"})
    assert r.status_code == 200
    assert main.COURSES_PATH.is_file()
    async with asgi_client() as c:
        assert (await c.get("/api/courses")).json()["courses"][0]["name"] == "甲"


# —— R1-3：导出不堵事件循环 ——

def _big_note(n_pairs: int = 600) -> Path:
    main.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    lines = ["# 双语课堂笔记 20260101_0900", "",
             "> LectureLens 自动生成（EN 原文 / ZH 翻译）", "", "---", ""]
    for i in range(1, n_pairs + 1):
        lines.append(f"- `10:{i % 60:02d}:00` Sentence number {i} with some English words.")
        lines.append(f"  - `#{i}` 第 {i} 句，带一点中文。")
    p = main.TRANSCRIPTS / "lecture_20260101_0900.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
async def test_export_does_not_block_event_loop(fmt):
    """导出期间事件循环必须仍在转（评审1 修）。

    做法：并发跑一个 10ms 心跳，导出请求结束后数心跳次数。旧实现（同步内联）在
    整段导出期间心跳**几乎为 0**；修复后（to_thread）应持续跳动。"""
    _big_note()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    try:
        async with asgi_client() as c:
            r = await c.get("/api/notes/export",
                            params={"name": "lecture_20260101_0900.md", "fmt": fmt})
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and len(r.content) > 1000
    # 导出期间的心跳数应当与「导出耗时 / 10ms」同量级；旧实现会是 0~1。
    # 取一个宽松下界（5），既不受机器快慢影响，又能确实区分「内联」与「线程」。
    assert ticks >= 5, (f"{fmt} 导出期间事件循环只跳了 {ticks} 次"
                        f"（耗时 {elapsed:.3f}s）——说明仍在协程里同步跑")


# —— R2-5 / R2-6：模型并发守卫 ——

@pytest.fixture
def fake_cache(tmp_path, monkeypatch):
    d = tmp_path / "hub"
    d.mkdir()
    monkeypatch.setattr(models_mod, "cache_dir", lambda: d)
    monkeypatch.setitem(main.runtime, "downloading", None)
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setitem(main.runtime, "asr", None)
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    return d


def _make_cached(cache: Path, name: str, nbytes: int = 4096) -> Path:
    entry = models_mod.entry_dir(cache, name)
    snap = entry / "snapshots" / "rev1"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / models_mod.REQUIRED_FILE).write_bytes(b"x" * nbytes)
    return entry


async def test_delete_blocked_while_model_switch_in_progress(fake_cache):
    """评审2：切换是「先构建、成功才合并 config」，构建期间 config/asr 都还指着旧
    模型——旧实现此时 DELETE 目标模型会 200 并真删。"""
    entry = _make_cached(fake_cache, "tiny")
    main.runtime["config"]["model"] = "large-v3-turbo"   # 当前是别的模型
    main.runtime["reloading"] = True                     # 正在切到 tiny
    async with asgi_client() as c:
        r = await c.delete("/api/models/tiny")
    assert r.status_code == 409
    assert "切换进行中" in r.json()["error"]
    assert entry.is_dir(), "切换进行中把目标模型删了"


async def test_delete_allowed_once_switch_finished(fake_cache):
    entry = _make_cached(fake_cache, "tiny")
    main.runtime["config"]["model"] = "base"
    main.runtime["reloading"] = False
    async with asgi_client() as c:
        r = await c.delete("/api/models/tiny")
    assert r.status_code == 200 and not entry.exists()


async def test_model_switch_blocked_while_downloading(fake_cache, monkeypatch):
    """评审2：反向也要拦——旧实现下载中 POST {model:'tiny'} 会 200 并真的开建。"""
    built = []

    def _build(overrides=None):
        built.append(overrides)

    monkeypatch.setattr(main, "build_asr", _build)
    main.runtime["downloading"] = "small"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "tiny"})
    assert r.status_code == 409
    assert "正在下载" in r.json()["error"]
    assert built == [], "下载中不该触发模型构建"
    assert main.runtime["config"]["model"] != "tiny"


async def test_instant_keys_still_allowed_while_downloading(fake_cache, monkeypatch):
    """只拦模型键：下载中改 vad/lang 这类即时键不该受影响。"""
    monkeypatch.setattr(main, "apply_segmenter", lambda: None)
    main.runtime["downloading"] = "small"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lang": "zh"})
    assert r.status_code == 200
    assert main.runtime["config"]["lang"] == "zh"


def test_load_progress_owner_token_isolates_writers():
    """评审2：旧实现按 `'模型' in stage` 判进度条归属，而 build_asr 的文案
    「加载模型 X（首次运行需下载）」同样含「模型」——下载任务收尾会把切换中的进度
    误清成 None。改用显式令牌后互不干扰。"""
    saved = dict(main.status.get("load") or {})
    try:
        main.set_load_progress(12, "加载模型 tiny（首次运行需下载）", "asr")
        main.clear_load_progress("dl:tiny")          # 下载任务收尾：不是它的进度
        assert main.status["load"] is not None, "asr 的进度被下载任务误清了"
        assert main.status["load"]["pct"] == 12
        main.clear_load_progress("asr")              # 所有者自己清
        assert main.status["load"] is None
    finally:
        main.status["load"] = saved or None
        main._load_owner["token"] = None


def test_build_asr_progress_uses_token(monkeypatch):
    """build_asr 的三处进度写入都要走令牌 API（否则归属判定形同虚设）。"""
    src = (Path(main.__file__)).read_text(encoding="utf-8")
    assert 'set_load_progress(12,' in src and 'set_load_progress(65,' in src
    assert src.count('clear_load_progress("asr")') == 2      # 成功路径 + 异常路径
    assert 'status["load"] = {"pct": 12' not in src          # 旧写法已清除


# ============================================================================
# 第二轮独立评审（评审员1 / 评审员2）——p3 阶段的发现逐条回归。
# 每条用例的 docstring 写明修前实测与影响；纯前端行为另有真浏览器核验。
# ============================================================================

WEB = Path(main.__file__).resolve().parent / "web"
PROJ = Path(main.__file__).resolve().parent.parent


def test_r1_autostart_frozen_entry_can_parse_minimized():
    """R1（自启·打包版）回归：打包入口与自启命令必须是**同一个能解析 --minimized
    的入口**。

    修前实跑取证：`python -c "sys.argv=['main.py','--minimized']; main.parse_args()"`
    → `main.py: error: unrecognized arguments: --minimized`，SystemExit 2。而自启项
    写的正是 `"<exe>" --minimized`，且 enable() 的回读校验只比字符串，于是打包版
    开机即退、设置页却照常显示「已启用」。现在打包入口改为 app/desktop.py（与 dev
    分支指向同一桌面壳），本用例把「入口」与「旗标」两件事焊在一起。"""
    import autostart
    import desktop
    src = (PROJ / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    assert 'str(ROOT / "app" / "desktop.py"),' in src, \
        "打包入口必须是桌面壳（自启命令要它来解析 --minimized）"
    assert 'str(ROOT / "app" / "main.py"),' not in src, \
        "main.py 入口不认 --minimized，不能再作为打包入口"
    # 自启命令 dev 分支指向 desktop；且该入口确实解析 --minimized
    assert autostart.command_parts(PROJ)[1].endswith(
        str(Path("app") / "desktop.py"))
    _g, minimized, bad = desktop._extract_desktop_flags(["--minimized"])
    assert minimized is True and bad == []


def test_r1_geometry_save_bases_on_disk_config(tmp_path, monkeypatch):
    """R1（窗口记忆）回归：落盘以**磁盘现值**为基底，只覆盖几何键。

    修前：`_save_geometry` 整份落盘 `runtime['config']`，而它初值是 DEFAULT_CONFIG；
    asr_loop 尚未载入磁盘配置时退出，就会把 config.json 静默重置成默认值（实测清掉
    auth_token / model / lang）。这里把「磁盘有非默认值 + runtime 是默认值」的恶劣
    前提直接摆在台面上。"""
    import desktop
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    main.CONFIG_PATH.write_text(json.dumps(
        {"model": "small", "lang": "zh", "auth_token": "TOKEN-STUB"}), encoding="utf-8")
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))

    class W:
        x, y, width, height = 0, 0, 1400, 900
    desktop._save_geometry(W(), None)

    after = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert after["auth_token"] == "TOKEN-STUB"
    assert after["model"] == "small" and after["lang"] == "zh"
    assert after["window_w"] == 1400


def test_r2_privacy_discloses_lan_plaintext_path():
    """R2（隐私页）回归：LAN 明文出网路径必须披露。

    §7 起事实换了：监听地址由 config.lan_enabled **在启动时**决定——false（默认）
    只听 127.0.0.1，true 才听 0.0.0.0。常量在 app/lan.py，两个入口（main.py 的
    uvicorn.run / desktop.py 的 uvicorn.Config）都经 lan.lan_bind 取地址。四处
    （常量、两个入口、隐私页文案）交叉核对——文案与代码事实不一致时这里会红。"""
    lm = (PROJ / "app" / "lan.py").read_text(encoding="utf-8")
    assert 'LAN_ALL_HOST = "0.0.0.0"' in lm, "LAN 对外监听常量变了？同步改文案"
    assert 'LAN_LOOPBACK_HOST = "127.0.0.1"' in lm, "关闭态监听常量变了？同步改文案"
    m = Path(main.__file__).read_text(encoding="utf-8")
    assert "uvicorn.run(app, host=bind_host, port=bind_port" in m, \
        "后端 LAN 监听事实变了？同步改文案"
    d = (PROJ / "app" / "desktop.py").read_text(encoding="utf-8")
    assert "lan_mod.lan_bind(" in d, "桌面模式不再跟随 lan_enabled？同步改文案"

    s = (WEB / "privacy.html").read_text(encoding="utf-8")
    assert "0.0.0.0" in s and "127.0.0.1" in s, "隐私页未说明两种模式各自的监听范围"
    assert "明文" in s, "隐私页未披露 LAN 传输是明文 HTTP"
    assert "token" in s, "隐私页未说明 LAN 访问需持 token"
    assert "lan_enabled" in s, "§7 起 LAN 是开关驱动（默认关），隐私页要说清怎么开"


def test_r2_redline_copy_matches_readme_authority():
    """R2（红线声明）回归——2026-10-13 用户拍板后**重钉**：权威口径 = README.md:3
    新版（当前免费自用+朋友含装机、不收费不注册公司不公开分发；商业化含出售，
    推迟至毕业后）。三处文案必须一致；不得再用「永久」把推迟裁决写成既成事实。
    旧口径的「待裁决」断言已随拍板作废——历史出处见 docs/upgrade-plan.md §3.3-1。"""
    readme = "".join((PROJ / "README.md").read_text(encoding="utf-8").split())
    assert "不收费、不注册公司、不公开分发" in readme      # README 2026-10-13 原文
    assert "含朋友装机" in readme
    assert "推迟至毕业后" in readme

    for name in ("index.html", "privacy.html"):
        flat = "".join((WEB / name).read_text(encoding="utf-8").split())
        assert "不收费、不注册公司、不公开分发" in flat, \
            f"{name}: 红线本体与 README 新口径不一致"
        assert "含朋友装机" in flat, f"{name}: 未写明「含朋友装机」的边界"
        assert "推迟至毕业后" in flat, f"{name}: 漏了「推迟至毕业后」"
        assert "合规红线（永久）" not in flat, f"{name}: 不得用「永久」定格"


def test_r2_modals_are_keyboard_reachable():
    """R2（关于弹窗）回归：真 Chrome 实测修前——打开后焦点不进入弹窗（activeElement
    仍是 #btnAbout）、Escape 不关闭、全文档 role/aria 计数为 0、Tab 能走到遮罩后面
    的按钮（4 个停靠点）。这里锁住修复的结构要件；行为已在真浏览器逐项复核。"""
    s = (WEB / "index.html").read_text(encoding="utf-8")
    assert "$('btnAbout').onclick = () => openModal(aboutMask);" in s
    assert "openModal($('wizardMask'))" in s
    assert "setAttribute('role', 'dialog')" in s
    assert "setAttribute('aria-modal', 'true')" in s
    assert "setAttribute('inert', '')" in s
    assert "e.key !== 'Escape'" in s


def test_r2_float_page_focus_is_visible():
    """R2（悬浮条）回归：修前实测——Tab 能进 #btnThru/#btnHide，但 .actions 的
    opacity 恒为 0（只有 #bar:hover 显形），且全文件 0 处 focus 规则，焦点落在看不见
    的控件上。这里锁住「键盘焦点显形 + gold 焦点环」两条结构要件。"""
    s = (WEB / "float.html").read_text(encoding="utf-8")
    assert "#bar:focus-within .actions" in s, "键盘聚焦时按钮不显形"
    assert "focus-visible" in s, "没有可见焦点环"
    style = s.split("<style>")[1].split("</style>")[0]
    assert "var(--gold)" in style, "焦点环未用品牌金令牌"


# ============================================================================
# 阶段 C 评审（2026-10-13）：评审员1① / 评审员2①④ —— 逐条回归
# 取证口径见各自的 docstring（均基于实测复现的事实，不是推测）。
# ============================================================================

#: 运行期生成的假 token（源码不写可用凭据字面量，Mimosa 约束）
_REVIEW_TOKEN = secrets.token_urlsafe(16)


@pytest.fixture
def lan_state(monkeypatch):
    """LAN/鉴权相关用例的公共前置：复位 runtime + 注入测试 token + 清空 WS 集合。"""
    saved_runtime = dict(main.runtime)
    saved_cfg = dict(main.runtime["config"])
    saved_clients = set(main.hub.clients)
    saved_lan_clients = set(main.hub.lan_clients)
    main.runtime["config"].clear()
    main.runtime["config"].update(main.DEFAULT_CONFIG)
    main.runtime["config"]["auth_token"] = _REVIEW_TOKEN
    main.runtime["reloading"] = False
    main.runtime["bind"] = None
    main.hub.clients.clear()
    main.hub.lan_clients.clear()
    yield main
    main.runtime.clear()
    main.runtime.update(saved_runtime)
    main.runtime["config"].clear()
    main.runtime["config"].update(saved_cfg)
    main.hub.clients.clear()
    main.hub.clients.update(saved_clients)
    main.hub.lan_clients.clear()
    main.hub.lan_clients.update(saved_lan_clients)


# ---------------- 评审1①：Host/Origin 守卫（DNS-rebinding） ----------------

@pytest.mark.parametrize("host_header,ok", [
    ("127.0.0.1:8787", True), ("127.0.0.1", True), ("[::1]:8787", True),
    ("::1", True), ("192.168.1.5:8787", True),         # 直连地址（手机/本机 IP）
    ("localhost:8787", True), ("localhost", True),
    ("testserver", True), ("test", True),              # 单标签名（测试客户端、本机名）
    ("evil.example", False), ("evil.example:8787", False),
    ("attacker.com", False), ("x.1.2.3.4.nip.io", False),  # rebinding 的典型载体
    ("", False), (None, False), ("[", False), ("evil.example.", False),
])
def test_c1_host_allowlist_matrix(host_header, ok):
    """评审1① 的判据本体：只有 IP 字面量 / 单标签名 / 本机名可信。

    实测事实（评审脚本 probe_lan2.py）：`Host: evil.example` + `Origin:
    http://evil.example` 的环回请求曾拿到 200 且响应里的 token 与 config 逐字相等。
    rebinding 必然用**攻击者控的域名**，而那是多标签名——所以这条规则拦的就是它。"""
    assert main.host_is_allowed(host_header) is ok


def test_c1_host_allowlist_env_escape_hatch(monkeypatch):
    """逃生口：LECTURELENS_ALLOW_HOSTS 里显式列出的名字（hosts 别名/隧道域名）放行。"""
    assert main.host_is_allowed("mybox.tailnet.ts.net") is False
    monkeypatch.setenv("LECTURELENS_ALLOW_HOSTS", "MyBox.Tailnet.ts.net, other.local")
    assert main.host_is_allowed("mybox.tailnet.ts.net") is True
    assert main.host_is_allowed("other.local:8787") is True
    assert main.host_is_allowed("evil.example") is False


def test_c1_host_name_parsing():
    assert main.host_name("127.0.0.1:8787") == "127.0.0.1"
    assert main.host_name("[::1]:8787") == "::1"
    assert main.host_name("LOCALHOST") == "localhost"
    assert main.host_name("") == "" and main.host_name(None) == ""
    assert main.host_name("[") == ""


@pytest.mark.parametrize("origin,host_header,ok", [
    (None, "127.0.0.1:8787", True),                      # 无 Origin = 同源导航/非浏览器
    ("http://127.0.0.1:8787", "127.0.0.1:8787", True),   # 本机页面自己的 POST
    ("http://localhost:8787", "localhost:8787", True),
    ("http://127.0.0.1:8787", "127.0.0.1:9999", False),  # 同主机不同端口 = 不同源
    ("http://evil.example", "127.0.0.1:8787", False),    # 跨站页面借浏览器的手
    ("https://evil.example:8787", "127.0.0.1:8787", False),
    ("null", "127.0.0.1:8787", False),                   # 沙箱 iframe / 本地文件
    ("not-a-url", "127.0.0.1:8787", False),
])
def test_c1_origin_same_site_matrix(origin, host_header, ok):
    """Origin 判定：缺省放行；有 Origin 必须与请求 Host 同源（端口也算）。"""
    assert main.origin_ok(origin, host_header) is ok


@asynccontextmanager
async def _asgi_at(base: str, peer=("127.0.0.1", 123)):
    """本文件公共 asgi_client 只认固定 base_url；Host 守卫用例需要指定 Host
    （httpx 用 URL 的 host 填 Host 头），故这里给个能带 base 的版本。"""
    transport = httpx.ASGITransport(app=main.app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url=base) as c:
        yield c


async def test_c1_rebinding_shaped_request_is_rejected(lan_state):
    """评审1① 的端到端回归（与评审取证同形）：环回 + `Host: evil.example` +
    `Origin: http://evil.example` 必须**不再**是 200，响应里也不许出现 token。

    两层防线都验：① 全局中间件直接 400；② 端点自己的 loopback_only_ok 判据
    （即使中间件被摘掉也拦得住）——后者用 Request 直接调端点函数验。"""
    lan_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    evil = {"Host": "evil.example", "Origin": "http://evil.example"}
    async with asgi_client() as c:
        r = await c.get("/api/lan-qr", headers=evil)
    assert r.status_code == 400, f"rebinding 形态仍被放行：HTTP {r.status_code}"
    assert "Host" in r.json()["error"]
    assert _REVIEW_TOKEN not in r.text and "#t=" not in r.text

    # ② 端点层判据（不经中间件）：直接构造 Request，必须 403 且不吐 token
    req = _make_request("/api/lan-qr", peer="127.0.0.1", host="evil.example",
                        origin="http://evil.example")
    resp = await main.api_lan_qr(req)
    assert resp.status_code == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["ok"] is False and "仅本机" in body["error"]
    assert _REVIEW_TOKEN not in resp.body.decode("utf-8")


def _make_request(path: str, peer: str = "127.0.0.1", host: str = "127.0.0.1:8787",
                  origin: str | None = None, method: str = "GET"):
    """手搓一个最小 Request（直接调端点函数用：绕开 ASGI 中间件，验端点自己的判据）。"""
    from starlette.requests import Request
    headers = [(b"host", host.encode("latin-1"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    scope = {"type": "http", "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": headers,
             "client": (peer, 54321), "server": ("127.0.0.1", 8787), "scheme": "http"}
    return Request(scope)


async def test_c1_same_site_requests_still_pass(lan_state):
    """别误伤：本机页面自己的请求（Host=localhost / 127.0.0.1，无跨源 Origin）照旧 200。"""
    lan_state.runtime["config"]["lan_enabled"] = True
    main.set_bind("0.0.0.0", 8787)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(main.lan_mod, "primary_lan_ip", lambda: "192.168.1.23")
    try:
        async with _asgi_at("http://127.0.0.1:8787") as c:
            r = await c.get("/api/lan-qr")
            assert r.status_code == 200 and r.json()["url"].endswith(_REVIEW_TOKEN)
            # 本机页面的 POST 带 Origin=同源 → 仍放行
            r2 = await c.post("/api/config", json={"lan_enabled": True},
                              headers={"Origin": "http://127.0.0.1:8787"})
            assert r2.status_code == 200
            # 同一 Host 但跨源 Origin → 拒
            r3 = await c.post("/api/config", json={"lan_enabled": True},
                              headers={"Origin": "http://evil.example"})
            assert r3.status_code == 400
        async with _asgi_at("http://localhost:8787") as c:
            assert (await c.get("/api/lan-qr")).status_code == 200
    finally:
        monkeypatch.undo()


async def test_c1_export_open_also_guarded(lan_state):
    """同款判据守着 /api/export/open（评审指出的第二个 loopback-only 面：rebinding 页面
    可反复在本机弹文件夹窗口并读出 export_dir 路径）。恶意 Host → 400；合法 Host +
    未设 export_dir → 仍是原来的 409（行为不变）。"""
    async with _asgi_at("http://127.0.0.1:8787") as c:
        r_evil = await c.post("/api/export/open", headers={"Host": "evil.example"})
        assert r_evil.status_code == 400, f"恶意 Host 未被拒：HTTP {r_evil.status_code}"
        r_ok = await c.post("/api/export/open")
        assert r_ok.status_code == 409 and "未设置导出位置" in r_ok.json()["error"]


# ---------------- 评审2①：设备数只数非环回对端 ----------------

def test_c2_ws_clients_excludes_local_windows(lan_state):
    """评审2① 的判据：status.ws_clients 数的是**非环回**的 WS（真手机），不含本机窗口。

    取证：本机实测（desktop.start_server + 裸 socket 握手）无页面 0 → 开一条 WS 1 →
    关闭 0；而桌面窗口自己就常年连着 WS（悬浮条再 +1），旧实现下这个数字恒 ≥1。"""
    class _Fake:
        def __init__(self, host):
            self.client = types.SimpleNamespace(host=host)

    assert main.is_loopback_peer("127.0.0.1") and not main.is_loopback_peer("192.168.1.50")
    # 判据就是 ws_endpoint 里用的那一条（对端 → 是否计入设备数）
    lan_peer = lambda h: not main.is_loopback_peer(h)          # noqa: E731
    assert lan_peer("127.0.0.1") is False
    assert lan_peer("::1") is False
    assert lan_peer("192.168.1.50") is True
    assert lan_peer("10.131.37.5") is True
    assert main.hub.lan_clients is not main.hub.clients, "设备数集合与总连接集合必须分开"
    fake = _Fake("127.0.0.1")
    assert not lan_peer(fake.client.host)


# ---------------- 评审2④a：文案不得与新事实矛盾 ----------------

def test_c2_frontend_copy_matches_lan_facts():
    """评审2④a：关于弹窗里「用 --server-only 时服务监听 0.0.0.0」是 §7 之前的旧口径
    （现在 --server-only 默认也只监听 127.0.0.1，要显式开开关）。护栏从 privacy.html
    扩到全仓前端文案：出现 0.0.0.0 的句子必须同时出现开关键/手机同屏，否则就是漏改的旧话。
    """
    for name in ("index.html", "privacy.html"):
        text = (WEB / name).read_text(encoding="utf-8")
        flat = "".join(text.split())
        if "0.0.0.0" in flat:
            assert "lan_enabled" in flat or "手机同屏" in flat, \
                f"{name}: 提到 0.0.0.0 却没提开关（旧口径漏改？）"
        assert "用--server-only时服务监听0.0.0.0" not in flat, \
            f"{name}: 仍是旧口径（--server-only 默认不再对外）"
    # 关于弹窗那一句的具体形状（评审点名的位置）
    idx = (WEB / "index.html").read_text(encoding="utf-8")
    assert "手机同屏会对外提供明文 HTTP" in idx
    assert "config" in idx.split("手机同屏会对外提供明文 HTTP", 1)[1][:220]


def test_c2_rotate_copy_promises_only_what_we_do():
    """评审2④b 的文案面：两处「旧设备失效」必须说清是**断开旧连接**（我们真做了 kick），
    而不是含糊的「随即失效」。"""
    idx = (WEB / "index.html").read_text(encoding="utf-8")
    assert "旧设备随即失效" not in idx
    priv = (WEB / "privacy.html").read_text(encoding="utf-8")
    assert "旧设备立刻失效" not in priv
    assert "立即断开" in idx or "立即断开" in priv


# ---------------- 评审2④b：token 轮换必须断开在线连接 ----------------

def test_c2_token_rotate_disconnects_live_ws(lan_state):
    """评审2④b 端到端（真 WebSocket，TestClient 无 lifespan 不起模型）：轮换前连上的
    会话，轮换后必须被服务端断开，且旧 token 不能再建新连接。

    修前实测（评审）：轮换后 ws_clients 仍=1、旧 socket recv 超时（没被关闭）——
    即「截图外泄、对方当下正开着页面」时旧设备继续收字幕。"""
    from fastapi import WebSocketDisconnect
    from starlette.testclient import TestClient

    client = TestClient(main.app)
    with client.websocket_connect(f"/ws?token={_REVIEW_TOKEN}") as ws:
        # TestClient 的对端是 "testclient"（非环回）→ 计入设备数
        assert client.get(f"/api/status?token={_REVIEW_TOKEN}").json()["ws_clients"] == 1
        rot = client.post(f"/api/security/token/regenerate?token={_REVIEW_TOKEN}")
        assert rot.status_code == 200
        assert rot.json()["disconnected"] >= 1, "轮换没有断开在线连接"
        new_token = rot.json()["token"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()          # 服务端已 close → 旧连接读到断开
        # 设备数回落；旧 token 再也建不了连接，新 token 可以
        assert client.get(f"/api/status?token={new_token}").json()["ws_clients"] == 0
        assert client.get(f"/api/status?token={_REVIEW_TOKEN}").status_code == 403
        with client.websocket_connect(f"/ws?token={new_token}"):
            pass
