# -*- coding: utf-8 -*-
"""tests/test_fade_lines.py — p2-7（B2）历史行渐隐：配置键 / 枚举校验 / status 布线。

渐隐本身是纯前端视觉（CSS 类 + 轮询 status），后端只承担两件事：
① 把它做成一个 **0/1/2 枚举**的白名单键（硬规矩⑦）；
② 把它放进 status，让字幕页 2s 轮询就能拿到、并且**重启后从持久化配置回填**
   （否则用户在设置页选的值「重启就丢」——这是本项最容易漏的一处）。
本文件锁这两件事；CSS/类切换由真浏览器人工核验（见交付说明）。"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

import main


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def fade_env(monkeypatch):
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setitem(main.status, "fade_lines", main.DEFAULT_CONFIG["fade_lines"])
    return main.runtime


# —— ① 白名单与取值 ——

def test_key_is_whitelisted_everywhere():
    assert "fade_lines" in main.ALLOWED_CONFIG_KEYS
    assert main.CONFIG_VALUE_CHOICES["fade_lines"] == (0, 1, 2)
    assert main.DEFAULT_CONFIG["fade_lines"] == 1


def test_default_is_a_valid_choice():
    """默认值必须落在自己的枚举里——否则「恢复出厂」会直接写出一个非法值。"""
    assert main.DEFAULT_CONFIG["fade_lines"] in main.CONFIG_VALUE_CHOICES["fade_lines"]


def test_config_example_documents_it():
    from pathlib import Path
    ex = json.loads((Path(__file__).resolve().parent.parent / "config.example.json")
                    .read_text(encoding="utf-8"))
    assert ex["fade_lines"] in main.CONFIG_VALUE_CHOICES["fade_lines"]


@pytest.mark.parametrize("value", [0, 1, 2])
async def test_accepts_each_choice(fade_env, value):
    fade_env["config"]["fade_lines"] = 9      # 先置一个不同的值，确保真的写入
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"fade_lines": value})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"fade_lines": value}
    assert fade_env["config"]["fade_lines"] == value
    assert main.status["fade_lines"] == value     # 立即进 status（字幕页下一轮就采用）


@pytest.mark.parametrize("value", [3, -1, 99, "1", 1.5, None])
async def test_rejects_non_choice_without_pollution(fade_env, value):
    snapshot = dict(fade_env["config"])
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"fade_lines": value})
    if value is None:
        assert r.status_code == 200           # None = 未提供 → no-op（全局约定）
    else:
        assert r.status_code == 400, f"{value!r} 应被拒绝: {r.text}"
        assert "fade_lines" in r.json()["error"]
    assert fade_env["config"] == snapshot


async def test_zero_is_not_swallowed_by_noop_check(fade_env):
    """**0 是这个键最容易被吃掉的取值**：一路上的 `if not v` / `v == ""` 判空都可能
    把 0 当成「没提供」。这里专门把 1→0 的切换走一遍。"""
    fade_env["config"]["fade_lines"] = 1
    assert fade_env["config"]["fade_lines"] != 0
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"fade_lines": 0})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"fade_lines": 0}      # 真的被当成一次变更
    assert fade_env["config"]["fade_lines"] == 0
    assert main.status["fade_lines"] == 0


async def test_same_value_is_noop(fade_env):
    fade_env["config"]["fade_lines"] = 2
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"fade_lines": 2})
    assert r.status_code == 200 and r.json()["changed"] == {}


async def test_persisted_to_config_json(fade_env):
    async with asgi_client() as c:
        await c.post("/api/config", json={"fade_lines": 2})
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["fade_lines"] == 2
    assert main.load_config()["fade_lines"] == 2      # 重启后读得回来


# —— ② status 布线（含重启回填） ——

def test_status_has_default_at_import():
    assert main.status["fade_lines"] == main.DEFAULT_CONFIG["fade_lines"]


class _FakeSegmenter:
    def __init__(self, q_out, device=None, vad="silero", stream_q=None, **kw):
        self.error, self.thread, self.last_feed_ts = None, None, 0.0

    def start(self): pass

    def stop(self): pass

    def snapshot(self): return {}


def _loop_args():
    return SimpleNamespace(model=None, gpu=False, compute_type=None, vad=None,
                           streaming=False, lang=None, api_key="", glossary=[],
                           source=None, device=None)


async def test_asr_loop_backfills_status_from_persisted_config(monkeypatch):
    """启动时把持久化配置里的 fade_lines 回填进 status。

    少了这一步，用户在设置页选「渐隐 2 行」重启后会静默回到默认 1——表现是
    「设置没保存」，但 config.json 里其实写着 2。"""
    saved = {k: main.runtime.get(k) for k in
             ("asr", "segmenter", "seg_q", "scheduler", "translator", "prompt")}
    saved_paused = main.status["paused"]
    saved_fade = main.status["fade_lines"]
    main.CONFIG_PATH.write_text(json.dumps({"fade_lines": 2}), encoding="utf-8")
    main.status["fade_lines"] = 1                      # 故意先置成默认值
    # asr_loop 会调 hub.start_transcript() 把**全局** main.hub 指向新笔记文件；
    # 不隔离的话「无笔记 → 404」这类用例会因为在后面跑而拿到 409/200（实测踩过）。
    monkeypatch.setattr(main, "hub", main.Hub())
    monkeypatch.setattr(main, "MicSegmenter", _FakeSegmenter)
    monkeypatch.setattr(main, "ARGS", SimpleNamespace(device=None))
    monkeypatch.setattr(main, "load_api_key", lambda *a, **k: "")

    def _build(overrides=None):
        main.status["asr"] = "ready"
    monkeypatch.setattr(main, "build_asr", _build)

    async def _noop():
        return
    monkeypatch.setattr(main, "status_loop", _noop)
    main.status["paused"] = False

    task = asyncio.create_task(main.asr_loop(_loop_args()))
    observed = None
    try:
        for _ in range(200):
            if main.status["fade_lines"] == 2:
                break
            await asyncio.sleep(0.02)
        # 必须在 finally 里恢复状态**之前**取值——恢复后就看不到被测行为了
        observed = main.status["fade_lines"]
    finally:
        main.status["paused"] = True
        task.cancel()
        if main.runtime.get("seg_q") is not None:
            main.runtime["seg_q"].put(None)            # 哨兵：解除 to_thread(seg_q.get)
        try:
            await task
        except asyncio.CancelledError:
            pass
        for k, v in saved.items():
            main.runtime[k] = v
        main.status["paused"] = saved_paused
        main.status["fade_lines"] = saved_fade

    assert observed == 2, \
        "启动没有从 config.json 回填 fade_lines —— 用户选择会在重启后静默丢失"
