# -*- coding: utf-8 -*-
"""tests/test_hf_env.py — D2（分发工程：frozen 下载语义 + 镜像开关）回归。

钉死四件事（docs/notes-batch.md D 组；实现见 app/hfenv.py 模块头）：
(a) **frozen 不再强制离线**：旧行为 `if getattr(sys,"frozen",False): os.environ
    .setdefault("HF_HUB_OFFLINE","1")` 把打包版的 /api/models/download 钉死在
    local_files_only 分支（本地没缓存必然失败）。现在离线只认**显式**环境变量，
    frozen 影响不到下载端点；
(b) **模型解析次序** 随包 → HF 缓存里已完成的 snapshot → 按名联网（离线时给可读
    错误）：已下载的档位离线也能加载；
(c) frozen 且用户从未选过模型 → 默认**随包模型**（small 优先），不再硬吃
    DEFAULT_CONFIG 的 turbo；
(d) config 新键 `hf_mirror`（bool，默认 false）：白名单 + 枚举校验；为 true 时在
    HF 库**导入前**把 HF_ENDPOINT 设为唯一预设镜像 hf-mirror.com——**只认这一条
    预设值**，配置里的任何字符串都不会被当时 endpoint 用（无「按用户输入请求任意
    URL」的面）。

绝不联网、绝不碰真缓存：全部走 tmp_path 与打桩。
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import hfenv
import main
import models as models_mod


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def clean_state(monkeypatch):
    """复位模型/配置相关的模块级单例，测试互不污染。"""
    monkeypatch.setitem(main.runtime, "downloading", None)
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.status, "model_dl",
                        {"state": "idle", "name": None, "error": None, "freed": 0})
    monkeypatch.setitem(main.status, "load", None)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)   # 显式离线开关：默认关
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    return main.runtime


# ============================================================================
# (a) frozen 不再强制离线；下载端点只认显式开关
# ============================================================================

def test_frozen_no_longer_forces_hf_hub_offline():
    """源码级：旧的 frozen 全局离线块必须消失，且 hfenv.apply 要在 HF 导入**之前**。

    这是 D2 的根因修复：环境变量只有落在 `from asr import WhisperASR` 之前才有效
    （huggingface_hub 在 import 时把常量读走），而旧的 setdefault 正是丢掉了「打包版
    在线下载」这条能力。"""
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert 'setdefault("HF_HUB_OFFLINE"' not in src, \
        "frozen 不得再全局置 HF_HUB_OFFLINE（打包版要能下载新档位）"
    assert "hfenv.apply(CONFIG_PATH)" in src
    assert src.index("hfenv.apply(CONFIG_PATH)") < src.index("from asr import WhisperASR"), \
        "HF 环境必须在 huggingface_hub/faster_whisper 导入前落定"


def test_hf_offline_reads_env_only(monkeypatch):
    """显式离线开关 = 环境变量；frozen 与否都不影响它。"""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert main.hf_offline() is False
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    assert main.hf_offline() is True
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")     # 只有 "1" 算离线
    assert main.hf_offline() is False


async def test_download_endpoint_ignores_frozen_uses_explicit_offline(
        tmp_path, monkeypatch, clean_state):
    """下载端点的 local_files_only 只由环境开关决定——**frozen 不再参与**。

    修前实测口径：frozen 时 main.py 置 HF_HUB_OFFLINE=1 → offline=True →
    download_model(local_files_only=True) → 本地没缓存必抛。"""
    seen: list = []

    def fake_download(name, cache, on_progress, local_files_only=False,
                      backend="ctranslate2"):
        seen.append(local_files_only)
        return cache

    monkeypatch.setattr(models_mod, "download_model", fake_download)
    monkeypatch.setattr(models_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        # ① frozen + 未设环境开关 → 允许联网下载（旧行为在这里必为 True）
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        async with asgi_client() as c:
            r = await c.post("/api/models/download", json={"name": "tiny"})
            assert r.status_code == 200
            for _ in range(200):
                if clean_state["downloading"] is None:
                    break
                await asyncio.sleep(0.01)
        # ② 显式离线 → 透传 local_files_only=True
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        clean_state["config"] = dict(main.DEFAULT_CONFIG)
        async with asgi_client() as c:
            r2 = await c.post("/api/models/download", json={"name": "base"})
            assert r2.status_code == 200
            for _ in range(200):
                if clean_state["downloading"] is None:
                    break
                await asyncio.sleep(0.01)
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)

    assert seen == [False, True], f"local_files_only 透传序列不符: {seen}"


async def test_models_snapshot_exposes_facts(tmp_path, monkeypatch, clean_state):
    """/api/models 的 offline / mirror 是**事实**（进程内生效值），不是用户意图。"""
    monkeypatch.setattr(models_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    data = await asyncio.to_thread(main.models_snapshot)
    assert data["offline"] is False and data["mirror"] is False
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_ENDPOINT", hfenv.MIRROR_ENDPOINT)
    data2 = await asyncio.to_thread(main.models_snapshot)
    assert data2["offline"] is True and data2["mirror"] is True


# ============================================================================
# (b) 模型解析次序：随包 → HF 本地 snapshot → 按名
# ============================================================================

def test_snapshot_dir_requires_model_bin(tmp_path):
    """本地 snapshot 目录的判据 = 含 model.bin（半成品不算，与 is_cached 同口径）。"""
    entry = tmp_path / "models--Systran--faster-whisper-small"
    snap = entry / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert models_mod.snapshot_dir(entry) is None        # 只下了 config.json
    (snap / models_mod.REQUIRED_FILE).write_bytes(b"x")
    assert models_mod.snapshot_dir(entry) == snap
    assert models_mod.snapshot_revision(entry) == "rev1"  # 旧接口口径不变


def test_resolve_model_ref_order(tmp_path, monkeypatch):
    """① bundled（frozen 随包，永远离线可用）→ ② 本地缓存 snapshot → ③ None（按名）。

    ② 的路径必须是真的 HF 缓存布局：已下载的档位离线也能加载靠的就是它。"""
    # 开发态 + 缓存里没有 → 按名（None）
    monkeypatch.setattr(main, "bundled_model_path", lambda name: None)
    monkeypatch.setattr(models_mod, "cache_dir", lambda: tmp_path)
    assert main.resolve_model_ref("small") == (None, "hub")

    # ② 缓存里有完整 snapshot → 走本地目录
    snap = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "rev9"
    snap.mkdir(parents=True)
    (snap / models_mod.REQUIRED_FILE).write_bytes(b"x")
    assert main.resolve_model_ref("small") == (str(snap), "cache")

    # ① 随包优先于缓存（打包版随包模型永远离线可用）
    monkeypatch.setattr(main, "bundled_model_path",
                        lambda name: tmp_path / "bundled" / name)
    assert main.resolve_model_ref("small") == (str(tmp_path / "bundled" / "small"),
                                              "bundled")


def test_build_asr_hands_local_path_to_whisper(tmp_path, monkeypatch, clean_state):
    """build_asr 把解析结果当 model_path 交给 WhisperASR——本地命中就不联网。"""
    snap = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / models_mod.REQUIRED_FILE).write_bytes(b"x")
    monkeypatch.setattr(models_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "bundled_model_path", lambda name: None)
    captured: dict = {}

    class FakeASR:
        def __init__(self, model_size=None, device="auto", compute_type=None,
                     language="en", model_path=None):
            captured.update(model_size=model_size, model_path=model_path)
            self.model_size, self.device, self.compute_type = model_size, "cpu", "int8"

        def warmup(self):
            return None

    monkeypatch.setattr(main, "WhisperASR", FakeASR)
    clean_state["config"]["model"] = "small"
    main.build_asr()
    assert captured["model_path"] == str(snap)
    assert captured["model_size"] == "small"
    assert main.status["load"] is None            # 进度已收尾


def test_build_asr_offline_without_local_model_gives_readable_error(
        tmp_path, monkeypatch, clean_state):
    """本地没有 + 显式离线 → 错误文案里要有出路（不是 huggingface_hub 的原始英文）。"""
    monkeypatch.setattr(models_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "bundled_model_path", lambda name: None)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    class BoomASR:
        def __init__(self, **kw):
            raise OSError("Cannot find the requested files in the disk cache")

    monkeypatch.setattr(main, "WhisperASR", BoomASR)
    clean_state["config"]["model"] = "small"
    with pytest.raises(OSError):
        main.build_asr()
    assert "HF_HUB_OFFLINE=1" in main.status["asr"] and "离线开关" in main.status["asr"]


# ============================================================================
# (c) frozen 首启默认 = 随包模型
# ============================================================================

def test_put_bundled_default_prefers_small():
    assert main.pick_bundled_default([]) is None
    assert main.pick_bundled_default(["small"]) == "small"
    assert main.pick_bundled_default(["large-v3-turbo", "small"]) == "small"
    assert main.pick_bundled_default(["large-v3-turbo"]) == "large-v3-turbo"


def test_bundled_model_names_requires_frozen_and_model_bin(tmp_path, monkeypatch):
    """开发态恒空（行为零变化）；frozen 下只认含 model.bin 的目录。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    (tmp_path / "models" / "small").mkdir(parents=True)
    assert main.bundled_model_names() == []                 # 开发态：不认
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    try:
        assert main.bundled_model_names() == []             # 没有 model.bin
        (tmp_path / "models" / "small" / "model.bin").write_bytes(b"x")
        (tmp_path / "models" / "turbo").mkdir()             # 半成品目录
        assert main.bundled_model_names() == ["small"]
    finally:
        monkeypatch.delattr(main.sys, "frozen", raising=False)


def test_fresh_start_uses_bundled_model_when_frozen():
    """源码级：首启分支不再对 frozen 一律跳过，而是走 bundled → pick_bundled_default。"""
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "bundled_model_names() if getattr(sys, \"frozen\", False) else []" in src
    assert "pick_bundled_default(bundled)" in src
    assert "not getattr(sys, \"frozen\", False)\n            and not user_picked_model" not in src, \
        "旧行为（frozen 直接跳过 → 用 DEFAULT_CONFIG 的 turbo）必须已被替换"


# ============================================================================
# (d) hf_mirror：白名单键 + 预设镜像（绝不接受任意 URL）
# ============================================================================

def test_hf_mirror_key_is_whitelisted_bool():
    assert "hf_mirror" in main.ALLOWED_CONFIG_KEYS
    assert main.CONFIG_VALUE_CHOICES["hf_mirror"] == (False, True)
    assert main.DEFAULT_CONFIG["hf_mirror"] is False
    # 默认关 = 老用户零变化（不动 env，走官方 endpoint）


def test_hfenv_mirror_requested_only_true_bool(tmp_path):
    """只认真布尔 true；字符串/数字/坏 JSON/缺文件一律 false（手改文件也不放行）。"""
    p = tmp_path / "config.json"
    assert hfenv.mirror_requested(p) is False                 # 文件不存在
    p.write_text("{ broken json", encoding="utf-8")
    assert hfenv.mirror_requested(p) is False                 # 坏文件
    for bad in ('"true"', "1", "null"):
        p.write_text('{"hf_mirror": %s}' % bad, encoding="utf-8")
        assert hfenv.mirror_requested(p) is False, f"{bad} 不该被当成开启"
    p.write_text('{"hf_mirror": true}', encoding="utf-8")
    assert hfenv.mirror_requested(p) is True


def test_hfenv_apply_sets_preset_endpoint_only(tmp_path, monkeypatch):
    """apply(true) 只把 HF_ENDPOINT 设成**唯一预设镜像**；apply(false) 一律不动。

    安全口径：配置里的任何字符串都不会被当作 endpoint —— 「按用户输入请求 URL」的
    面根本不存在（本模块只读布尔键）。"""
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    off = tmp_path / "off.json"
    off.write_text('{"hf_mirror": false, "hf_endpoint": "http://evil.example"}',
                   encoding="utf-8")
    hfenv.apply(off)
    assert hfenv.MIRROR_ENDPOINT != "http://evil.example"
    assert "HF_ENDPOINT" not in os.environ, "开了 false 就不该动 env"

    on = tmp_path / "on.json"
    on.write_text('{"hf_mirror": true, "hf_endpoint": "http://evil.example"}',
                  encoding="utf-8")
    got = hfenv.apply(on)
    assert got["mirror"] is True
    assert got["endpoint"] == hfenv.MIRROR_ENDPOINT == "https://hf-mirror.com"
    assert "evil.example" not in got["endpoint"]


def test_hf_mirror_active_reads_env(monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert main.hf_mirror_active() is False
    monkeypatch.setenv("HF_ENDPOINT", hfenv.MIRROR_ENDPOINT)
    assert main.hf_mirror_active() is True


async def test_api_config_accepts_bool_rejects_non_bool(monkeypatch, clean_state):
    """POST /api/config：true/false 收；字符串/数字一律 400（白名单枚举校验）。"""
    async with asgi_client() as c:
        r_bad = await c.post("/api/config", json={"hf_mirror": "true"})
        assert r_bad.status_code == 400
        assert "hf_mirror" in r_bad.json()["error"]
        assert clean_state["config"]["hf_mirror"] is False   # 拒绝时零污染
        r_ok = await c.post("/api/config", json={"hf_mirror": True})
        assert r_ok.status_code == 200
        assert clean_state["config"]["hf_mirror"] is True
        r_get = await c.get("/api/config")
        assert r_get.json()["hf_mirror"] is True             # GET 回读一致（设置页数据源）
        r_off = await c.post("/api/config", json={"hf_mirror": False})
        assert r_off.status_code == 200
        assert clean_state["config"]["hf_mirror"] is False


# ============================================================================
# (d) 设置页开关的接线契约（前端结构 + 服务端真发出的页面）
# ============================================================================

def test_settings_page_wires_hf_mirror_toggle():
    """设置页「识别模型」卡里的镜像开关：控件 + 写入口 + 回填 + 生效口径文案。

    只传布尔（`hf_mirror: e.target.checked`）——镜像地址由后端常量决定，前端**不传
    任何 URL**（本仓库对「按用户输入请求地址」的零容忍面）。"""
    html = (Path(main.__file__).resolve().parent / "web" / "index.html").read_text(
        encoding="utf-8")
    assert 'id="setHfMirrorChk"' in html                       # 开关本体
    assert "hf_mirror: e.target.checked" in html               # 写入口（cfgSet 白名单键）
    assert "$('setHfMirrorChk').checked = !!SETCFG.hf_mirror;" in html   # 打开设置页回填
    assert 'id="setHfMirrorState"' in html                     # 事实行（区别于用户意图）
    assert "hf-mirror.com" in html and "重启应用后生效" in html  # 口径：不许写立即生效
    assert "改完要重启应用才换源" in html                        # 卡片文案常驻的生效口径
    assert "MODELS.mirror" in html                             # 事实来自 /api/models


async def test_served_page_contains_mirror_toggle():
    """服务端**实际发出**的 index.html 也带这个开关（防「源码里有、送出去的没有」）。"""
    async with asgi_client() as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert 'id="setHfMirrorChk"' in r.text
