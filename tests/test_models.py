# -*- coding: utf-8 -*-
"""tests/test_models.py — p2-4 模型管理器：缓存状态 / 下载 / 删除（离线秒级）。

三层：
1) models.py 纯函数（短名→仓库映射、体积统计与去重、可用性判据、展示格式化）；
2) 下载编排（snapshot_download 打桩：进度回调序列、local_files_only 透传、
   查不到总体积时的降级）；
3) /api/models 三个端点（白名单 400 / 并发 409 / 使用中禁删 409 / 真删）。

**绝不联网、绝不动真缓存**：所有用例把 models.cache_dir 指向 tmp_path，
下载用替身（唯一一次真实 snapshot_download 调用由人工集成验证，见交付说明）。"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
import models as models_mod


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def fake_cache(tmp_path, monkeypatch):
    """把缓存根指向 tmp_path：绝不碰 D:\\...\\huggingface 真缓存。"""
    d = tmp_path / "hub"
    d.mkdir()
    monkeypatch.setattr(models_mod, "cache_dir", lambda: d)
    return d


@pytest.fixture
def clean_state(monkeypatch):
    """复位模型下载相关的模块级单例状态，测试互不污染。"""
    monkeypatch.setitem(main.runtime, "downloading", None)
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setitem(main.runtime, "asr", None)
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.status, "model_dl",
                        {"state": "idle", "name": None, "error": None, "freed": 0})
    monkeypatch.setitem(main.status, "load", None)
    return main.runtime


def make_cached(cache: Path, name: str, nbytes: int = 1000, complete: bool = True):
    """造一个缓存条目；complete=False 表示只下了 config.json 的半成品。"""
    entry = models_mod.entry_dir(cache, name)
    snap = entry / "snapshots" / "rev1"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    if complete:
        (snap / models_mod.REQUIRED_FILE).write_bytes(b"x" * nbytes)
    return entry


# —— 1) 纯函数 ——

def test_model_repos_match_library_map():
    """钉住短名→仓库映射与 faster-whisper 内部表一致，防止两边悄悄漂移。"""
    from faster_whisper.utils import _MODELS
    for short, repo in models_mod.MODEL_REPOS.items():
        assert _MODELS[short] == repo, f"{short} 映射与库不一致"


def test_planned_models_match_config_choices():
    """管理器的模型清单必须与 config 白名单/字幕页下拉同源，不能各报各的。"""
    assert tuple(models_mod.PLANNED_MODELS) == tuple(main.MODEL_CHOICES)


def test_human_size():
    assert models_mod.human_size(0) == "0 MB"
    assert models_mod.human_size(999) == "999 B"
    assert models_mod.human_size(1024) == "1.0 KB"
    assert models_mod.human_size(78_200_000) == "74.6 MB"
    assert models_mod.human_size(1_621_700_000) == "1.5 GB"
    assert models_mod.human_size(None) == "—"      # 脏数据不炸


def test_entry_dir_uses_hf_layout(fake_cache):
    assert models_mod.entry_dir(fake_cache, "large-v3-turbo").name == \
        "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    assert models_mod.entry_dir(fake_cache, "tiny").name == \
        "models--Systran--faster-whisper-tiny"


def test_dir_size_sums_files_and_ignores_missing(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "a.bin").write_bytes(b"a" * 100)
    (d / "sub").mkdir()
    (d / "sub" / "b.bin").write_bytes(b"b" * 50)
    assert models_mod.dir_size(d) == 150
    assert models_mod.dir_size(tmp_path / "nope") == 0


def test_dir_size_dedupes_symlinked_files(tmp_path):
    """HF 在支持符号链接的平台上把 snapshots 做成指向 blobs 的链接——不去重
    会把同一份文件算两遍。Windows 未开开发者模式时建不了链接，那就跳过。"""
    d = tmp_path / "repo"
    blobs = d / "blobs"
    snap = d / "snapshots" / "rev"
    blobs.mkdir(parents=True)
    snap.mkdir(parents=True)
    real = blobs / "abcd"
    real.write_bytes(b"z" * 4096)
    try:
        (snap / "model.bin").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("本机不支持创建符号链接（Windows 需开发者模式/管理员）")
    assert models_mod.dir_size(d) == 4096          # 只算一次


def test_is_cached_requires_model_bin(fake_cache):
    """判据核心：只看目录存在会把「只下了 config.json 的半成品」误报成可用，
    而缺 model.bin 时 WhisperModel 根本起不来。"""
    assert models_mod.is_cached(models_mod.entry_dir(fake_cache, "tiny")) is False
    make_cached(fake_cache, "tiny", complete=False)
    assert models_mod.is_cached(models_mod.entry_dir(fake_cache, "tiny")) is False
    make_cached(fake_cache, "base", complete=True)
    assert models_mod.is_cached(models_mod.entry_dir(fake_cache, "base")) is True


def test_model_info_states(fake_cache):
    info = models_mod.model_info(fake_cache, "tiny")
    assert info["cached"] is False and info["present"] is False and info["size"] == 0
    make_cached(fake_cache, "tiny", complete=False)
    info = models_mod.model_info(fake_cache, "tiny")
    assert info["cached"] is False and info["present"] is True and info["partial"] is True
    make_cached(fake_cache, "tiny", nbytes=500)
    info = models_mod.model_info(fake_cache, "tiny")
    assert info["cached"] is True and info["partial"] is False
    assert info["size"] > 500 and info["repo"] == "Systran/faster-whisper-tiny"


def test_list_models_totals_and_active(fake_cache):
    make_cached(fake_cache, "tiny", nbytes=1000)
    make_cached(fake_cache, "base", nbytes=2000)
    data = models_mod.list_models(fake_cache, active="base")
    assert len(data["models"]) == len(models_mod.PLANNED_MODELS)
    assert data["cached_count"] == 2
    assert data["total_size"] == sum(m["size"] for m in data["models"])
    assert [m["name"] for m in data["models"] if m["active"]] == ["base"]
    assert data["cache_dir"] == str(fake_cache)


def test_remove_model_deletes_entry(fake_cache):
    entry = make_cached(fake_cache, "tiny", nbytes=4096)
    freed, err = models_mod.remove_model(fake_cache, "tiny")
    assert err is None and freed > 4096 and not entry.exists()
    # 只删目标条目，别的仓库不受影响
    other = make_cached(fake_cache, "base")
    models_mod.remove_model(fake_cache, "tiny")
    assert other.exists()


def test_remove_model_missing_reports_error(fake_cache):
    freed, err = models_mod.remove_model(fake_cache, "tiny")
    assert freed == 0 and err and "未下载" in err


def test_repo_total_bytes_degrades_to_zero(monkeypatch):
    """查不到体积绝不能拒绝下载（本地已有缓存时下载本就不需要联网）。"""
    import huggingface_hub

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("offline")

    monkeypatch.setattr(huggingface_hub, "HfApi", Boom)
    assert models_mod._repo_total_bytes("Systran/faster-whisper-tiny") == 0


# —— 2) 下载编排（打桩，不联网） ——

def test_download_model_reports_progress_and_passes_cache(fake_cache, monkeypatch):
    import huggingface_hub

    calls = {}

    def fake_snapshot(repo_id=None, cache_dir=None, tqdm_class=None,
                      local_files_only=False, **kw):
        calls.update(repo_id=repo_id, cache_dir=cache_dir,
                     local_files_only=local_files_only)
        bar = tqdm_class(total=100, desc="model.bin")
        bar.update(60)
        bar.update(40)
        bar.close()
        return str(fake_cache / "models--Systran--faster-whisper-tiny" / "snapshots" / "r")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(models_mod, "_repo_total_bytes", lambda repo: 100)

    seen = []
    got = models_mod.download_model("tiny", fake_cache, lambda p, s: seen.append((p, s)))

    assert calls["repo_id"] == "Systran/faster-whisper-tiny"   # 只用白名单里的仓库
    assert calls["cache_dir"] == str(fake_cache)
    assert calls["local_files_only"] is False
    assert got.name == "r"
    assert seen[0][0] == 3 and "查询模型信息" in seen[0][1]
    assert seen[-1][0] == 95 and "校验完成" in seen[-1][1]
    pcts = [p for p, _ in seen]
    assert pcts == sorted(pcts), "进度必须单调不减"
    assert max(pcts) <= 95
    # 100/100 字节 → 应推进到 90%
    assert 90 in pcts


def test_download_model_local_files_only(fake_cache, monkeypatch):
    import huggingface_hub
    seen = {}

    def fake_snapshot(repo_id=None, cache_dir=None, tqdm_class=None,
                      local_files_only=False, **kw):
        seen["lfo"] = local_files_only
        return str(fake_cache)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(models_mod, "_repo_total_bytes",
                        lambda repo: pytest.fail("离线模式不该查体积"))
    stages = []
    models_mod.download_model("tiny", fake_cache, lambda p, s: stages.append(s),
                              local_files_only=True)
    assert seen["lfo"] is True
    assert any("离线模式" in s for s in stages)


def test_download_model_progress_survives_callback_error(fake_cache, monkeypatch):
    """进度回调永不许可影响下载本身：回调炸了也要把模型下完。"""
    import huggingface_hub

    def fake_snapshot(repo_id=None, cache_dir=None, tqdm_class=None,
                      local_files_only=False, **kw):
        bar = tqdm_class(total=10, desc="x")
        bar.update(10)
        bar.close()
        return str(fake_cache)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(models_mod, "_repo_total_bytes", lambda repo: 10)

    def boom(p, s):
        raise RuntimeError("callback exploded")

    assert models_mod.download_model("tiny", fake_cache, boom) == fake_cache


# —— 3) 端点 ——

async def test_get_models_shape(fake_cache, clean_state):
    make_cached(fake_cache, "tiny", nbytes=1024)
    clean_state["config"]["model"] = "tiny"
    async with asgi_client() as c:
        r = await c.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["active"] == "tiny"
    assert body["cache_dir"] == str(fake_cache)
    assert body["downloading"] is None
    assert "total_size_h" in body and "dl" in body
    names = [m["name"] for m in body["models"]]
    assert names == list(models_mod.PLANNED_MODELS)
    tiny = body["models"][0]
    assert tiny["cached"] is True and tiny["active"] is True and tiny["size_h"]
    assert tiny["bundled"] is False                 # 开发模式无随包模型
    base = body["models"][1]
    assert base["cached"] is False and base["size"] == 0


async def test_download_unknown_model_400(fake_cache, clean_state):
    async with asgi_client() as c:
        r = await c.post("/api/models/download", json={"name": "../../etc/passwd"})
        assert r.status_code == 400
        assert "未知模型" in r.json()["error"]
        r2 = await c.post("/api/models/download", json={})
        assert r2.status_code == 400


async def test_download_already_cached_short_circuits(fake_cache, clean_state, monkeypatch):
    make_cached(fake_cache, "tiny", nbytes=10)
    monkeypatch.setattr(models_mod, "download_model",
                        lambda *a, **k: pytest.fail("已缓存不该再下载"))
    async with asgi_client() as c:
        r = await c.post("/api/models/download", json={"name": "tiny"})
    assert r.status_code == 200
    body = r.json()
    assert body["already"] is True and body["model"]["cached"] is True


async def test_download_conflicts_409(fake_cache, clean_state):
    async with asgi_client() as c:
        clean_state["downloading"] = "medium"
        r = await c.post("/api/models/download", json={"name": "tiny"})
        assert r.status_code == 409 and "正在下载" in r.json()["error"]
        clean_state["downloading"] = None
        clean_state["reloading"] = True
        r2 = await c.post("/api/models/download", json={"name": "tiny"})
        assert r2.status_code == 409 and "切换进行中" in r2.json()["error"]


async def test_download_runs_and_sets_status(fake_cache, clean_state, monkeypatch):
    """受理即返回 → 后台跑完 → status['load'] 进度 + model_dl 终态 done。"""
    def fake_download(name, cache, on_progress, local_files_only=False):
        on_progress(10, "下载模型 tiny")
        on_progress(90, "下载模型 tiny")
        make_cached(cache, name, nbytes=2048)          # 落盘，供终态统计
        return cache

    monkeypatch.setattr(models_mod, "download_model", fake_download)
    async with asgi_client() as c:
        r = await c.post("/api/models/download", json={"name": "tiny"})
        assert r.status_code == 200
        assert r.json()["started"] is True
        for _ in range(200):
            if clean_state["downloading"] is None:
                break
            await __import__("asyncio").sleep(0.01)
    assert main.status["model_dl"]["state"] == "done"
    assert clean_state["downloading"] is None
    assert main.status["load"] is None                  # 自己的进度收尾已清


async def test_download_failure_surfaces_error(fake_cache, clean_state, monkeypatch):
    """下载失败必须留下可见错误（进度条会清空，错误不能跟着没）。"""
    def boom(name, cache, on_progress, local_files_only=False):
        raise OSError("disk full")

    monkeypatch.setattr(models_mod, "download_model", boom)
    async with asgi_client() as c:
        await c.post("/api/models/download", json={"name": "tiny"})
        for _ in range(200):
            if clean_state["downloading"] is None:
                break
            await __import__("asyncio").sleep(0.01)
    assert main.status["model_dl"]["state"] == "error"
    assert "disk full" in main.status["model_dl"]["error"]
    assert main.status["load"] is None


async def test_delete_unknown_and_active_and_loaded(fake_cache, clean_state):
    make_cached(fake_cache, "tiny", nbytes=100)
    async with asgi_client() as c:
        assert (await c.delete("/api/models/nope")).status_code == 400
        # 使用中（config 当前模型）→ 409
        clean_state["config"]["model"] = "tiny"
        r = await c.delete("/api/models/tiny")
        assert r.status_code == 409 and "使用中" in r.json()["error"]
        assert models_mod.entry_dir(fake_cache, "tiny").is_dir()   # 没删掉
        # config 已切走但识别器还加载着它（切换失败态）→ 也要拦住
        clean_state["config"]["model"] = "base"
        clean_state["asr"] = type("A", (), {"model_size": "tiny"})()
        r2 = await c.delete("/api/models/tiny")
        assert r2.status_code == 409 and "识别器正在加载" in r2.json()["error"]
        assert models_mod.entry_dir(fake_cache, "tiny").is_dir()


async def test_delete_ok_frees_space(fake_cache, clean_state):
    make_cached(fake_cache, "tiny", nbytes=4096)
    clean_state["config"]["model"] = "base"
    async with asgi_client() as c:
        r = await c.delete("/api/models/tiny")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["deleted"] == "tiny"
    assert body["freed"] > 4096 and body["freed_h"]
    assert not models_mod.entry_dir(fake_cache, "tiny").exists()


async def test_delete_while_downloading_409(fake_cache, clean_state):
    make_cached(fake_cache, "tiny", nbytes=10)
    clean_state["config"]["model"] = "base"
    clean_state["downloading"] = "tiny"
    async with asgi_client() as c:
        r = await c.delete("/api/models/tiny")
    assert r.status_code == 409 and "正在下载" in r.json()["error"]


async def test_delete_missing_cache_409(fake_cache, clean_state):
    clean_state["config"]["model"] = "base"
    async with asgi_client() as c:
        r = await c.delete("/api/models/tiny")
    assert r.status_code == 409 and "未下载" in r.json()["error"]
