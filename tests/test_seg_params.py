# -*- coding: utf-8 -*-
"""tests/test_seg_params.py — p2-5（B4 断句参数面板）：白名单 / 区间 / 组合校验 /
热重建触发 / 积压阈值 / 恢复默认（离线秒级）。

四层：
1) 默认值一致性锁：DEFAULT_CONFIG 的三个断句默认值必须与 capture.MicSegmenter
   构造签名逐字一致（两处默认值漂移会让面板显示的值与实际生效的值不同）；
2) 数值校验：类型（含 bool 排除）、区间、跨键组合（hard_cut_s > max_seg_s、
   backlog_keep < backlog_drop），且被拒不污染 runtime/文件；
3) 生效路径：改断句键 → 触发 apply_segmenter 重建；只改积压键 → **不**重建；
   config_backlog_thresholds 夹边界 + backlog_trim 纯函数行为；
4) POST /api/config/segmentation/reset 恢复默认 + 幂等 + 切换中 409。
"""
import inspect
import queue
from contextlib import asynccontextmanager

import httpx
import pytest

import main
from capture import MicSegmenter


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def seg_env(monkeypatch):
    """复位 config/reloading，并把 apply_segmenter 换成可计数的替身。"""
    calls: list = []
    monkeypatch.setitem(main.runtime, "config", dict(main.DEFAULT_CONFIG))
    monkeypatch.setitem(main.runtime, "reloading", False)
    monkeypatch.setattr(main, "apply_segmenter",
                        lambda: calls.append("rebuild"))
    return calls


# —— 1) 默认值一致性锁 ——

def test_defaults_match_capture_signature():
    """面板显示的默认值必须就是采集器实际用的默认值。

    这是本项最容易悄悄坏掉的地方：DEFAULT_CONFIG 里写 700、capture 里是 800，
    「恢复默认」之后面板说 700、实际按 800 分句，而且任何断言都不会响。"""
    sig = inspect.signature(MicSegmenter.__init__).parameters
    for key in main.SEGMENT_REBUILD_KEYS:
        assert key in sig, f"capture.MicSegmenter 不再接受 {key}——面板会传空参"
        assert main.DEFAULT_CONFIG[key] == sig[key].default, (
            f"{key} 默认值漂移：DEFAULT_CONFIG={main.DEFAULT_CONFIG[key]!r} "
            f"vs capture={sig[key].default!r}")


def test_new_keys_are_whitelisted():
    for key in main.SEGMENT_KEYS:
        assert key in main.ALLOWED_CONFIG_KEYS
        assert key in main.CONFIG_NUM_RANGES
        assert key in main.DEFAULT_CONFIG
    assert set(main.SEGMENT_KEYS) <= set(main.ALLOWED_CONFIG_KEYS)


def test_defaults_in_range_and_cross_ok():
    """内置默认值自身必须合法——否则「恢复默认」会写进一个被拒的状态。"""
    for key in main.SEGMENT_KEYS:
        assert main._config_number_problem(key, main.DEFAULT_CONFIG[key]) is None
    assert main._config_cross_problem(dict(main.DEFAULT_CONFIG)) is None


def test_config_example_documents_new_keys():
    import json
    from pathlib import Path
    ex = json.loads((Path(__file__).resolve().parent.parent / "config.example.json")
                    .read_text(encoding="utf-8"))
    for key in main.SEGMENT_KEYS:
        assert key in ex, f"config.example.json 缺 {key}"
        assert main._config_number_problem(key, ex[key]) is None


# —— 2) 数值与组合校验 ——

def test_number_problem_types_and_ranges():
    assert main._config_number_problem("silence_ms", True) is not None   # bool 排除
    assert main._config_number_problem("silence_ms", "700") is not None  # 字符串
    assert main._config_number_problem("silence_ms", 700.5) is not None  # 整型键收 float
    assert main._config_number_problem("silence_ms", 10) is not None     # 越下界
    assert main._config_number_problem("silence_ms", 99999) is not None  # 越上界
    assert main._config_number_problem("silence_ms", 700) is None
    assert main._config_number_problem("max_seg_s", 15) is None          # 浮点键收 int
    assert main._config_number_problem("max_seg_s", 100) is not None
    assert main._config_number_problem("backlog_keep", 2.5) is not None


def test_cross_problem_rejects_hardcut_not_above_softcap():
    """核心组合约束：hard_cut_s 必须 > max_seg_s。

    否则 capture 的「到软上限先标记溢出、改等静音对齐；仅溢出后到硬上限才强切」
    被抵消——溢出标记一置位下一块就满足硬切条件。两个数字各自都在合法区间内，
    只有这一层能拦。"""
    base = dict(main.DEFAULT_CONFIG)
    assert main._config_cross_problem({**base, "hard_cut_s": 15.0,
                                       "max_seg_s": 15.0}) is not None
    assert main._config_cross_problem({**base, "hard_cut_s": 10.0,
                                       "max_seg_s": 20.0}) is not None
    assert main._config_cross_problem({**base, "hard_cut_s": 18.0,
                                       "max_seg_s": 15.0}) is None


def test_cross_problem_rejects_keep_not_below_drop():
    base = dict(main.DEFAULT_CONFIG)
    assert main._config_cross_problem({**base, "backlog_keep": 5,
                                       "backlog_drop": 5}) is not None
    assert main._config_cross_problem({**base, "backlog_keep": 6,
                                       "backlog_drop": 5}) is not None
    assert main._config_cross_problem({**base, "backlog_keep": 2,
                                       "backlog_drop": 5}) is None


async def test_set_config_accepts_valid_and_persists(seg_env):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"silence_ms": 900, "max_seg_s": 10.0,
                                              "hard_cut_s": 14.0})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"silence_ms": 900, "max_seg_s": 10.0,
                                   "hard_cut_s": 14.0}
    assert main.runtime["config"]["silence_ms"] == 900
    assert seg_env == ["rebuild"]                       # 断句键 → 触发重建


async def test_set_config_rejects_out_of_range_without_pollution(seg_env):
    snapshot = dict(main.runtime["config"])
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"silence_ms": 10})
    assert r.status_code == 400
    assert "silence_ms" in r.json()["error"]
    assert main.runtime["config"] == snapshot
    assert not main.CONFIG_PATH.exists()
    assert seg_env == []                                # 被拒不重建


async def test_set_config_rejects_bad_cross_combo(seg_env):
    """跨键组合非法 → 400，且**两个键都不生效**（不能只写进一个）。"""
    snapshot = dict(main.runtime["config"])
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"max_seg_s": 20.0, "hard_cut_s": 10.0})
    assert r.status_code == 400
    assert "hard_cut_s" in r.json()["error"]
    assert main.runtime["config"] == snapshot
    assert seg_env == []


async def test_set_config_backlog_only_does_not_rebuild(seg_env):
    """只改积压阈值不必重建采集器（asr_loop 每轮现读），避免白丢在飞的那一段。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"backlog_drop": 8, "backlog_keep": 3})
    assert r.status_code == 200
    assert main.runtime["config"]["backlog_drop"] == 8
    assert seg_env == []


async def test_set_config_rejects_keep_ge_drop(seg_env):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"backlog_keep": 5, "backlog_drop": 5})
    assert r.status_code == 400
    assert "backlog_keep" in r.json()["error"]


# —— 3) 生效路径 ——

def test_segment_kwargs_only_carries_present_keys():
    cfg = dict(main.DEFAULT_CONFIG)
    # 2026-10 首期反馈②：默认软/硬上限 15/18 → 8/10（连续说话时字幕等太久）
    assert main.segment_kwargs(cfg) == {"silence_ms": 700, "max_seg_s": 8.0,
                                        "hard_cut_s": 10.0}
    bare = {k: v for k, v in cfg.items() if k not in main.SEGMENT_REBUILD_KEYS}
    assert main.segment_kwargs(bare) == {}      # 缺键 → 交给 capture 自己的默认值


def test_config_backlog_thresholds_falls_back_and_clamps():
    assert main.config_backlog_thresholds({}) == (main.BACKLOG_DROP_THRESHOLD,
                                                  main.BACKLOG_KEEP)
    assert main.config_backlog_thresholds({"backlog_drop": 4,
                                           "backlog_keep": 1}) == (4, 1)
    # 手改 config.json 绕过 API 的越界值：这里再夹一次，防 keep >= drop 空转
    drop, keep = main.config_backlog_thresholds({"backlog_drop": 1, "backlog_keep": 99})
    assert drop >= 2 and keep < drop
    drop2, keep2 = main.config_backlog_thresholds({"backlog_drop": 99999,
                                                   "backlog_keep": 99999})
    assert drop2 <= 200 and keep2 < drop2


def test_backlog_trim_drops_oldest_keeps_newest():
    q = queue.Queue()
    for i in range(10):
        q.put(i)
    dropped = main.backlog_trim(q, drop_th=5, keep_n=2)
    assert dropped == 8
    assert q.qsize() == 2
    assert [q.get_nowait() for _ in range(2)] == [8, 9]   # 留最新的


def test_backlog_trim_noop_below_threshold():
    q = queue.Queue()
    for i in range(3):
        q.put(i)
    assert main.backlog_trim(q, drop_th=5, keep_n=2) == 0
    assert q.qsize() == 3


def test_backlog_trim_empty_queue():
    assert main.backlog_trim(queue.Queue(), drop_th=1, keep_n=1) == 0


# —— 4) 恢复默认 ——

async def test_reset_restores_defaults(seg_env):
    main.runtime["config"].update({"silence_ms": 1200, "max_seg_s": 9.0,
                                   "hard_cut_s": 12.0, "backlog_drop": 9,
                                   "backlog_keep": 4})
    async with asgi_client() as c:
        r = await c.post("/api/config/segmentation/reset")
    assert r.status_code == 200
    body = r.json()
    assert set(body["changed"]) == set(main.SEGMENT_KEYS)
    for k in main.SEGMENT_KEYS:
        assert main.runtime["config"][k] == main.DEFAULT_CONFIG[k]
    assert body["values"] == {k: main.DEFAULT_CONFIG[k] for k in main.SEGMENT_KEYS}
    assert seg_env == ["rebuild"]        # 断句键变了 → 重建


async def test_reset_is_idempotent(seg_env):
    async with asgi_client() as c:
        r = await c.post("/api/config/segmentation/reset")
    assert r.status_code == 200
    assert r.json()["changed"] == {}
    assert seg_env == []                 # 已是默认值：不该白重建一次


async def test_reset_only_backlog_changed_does_not_rebuild(seg_env):
    main.runtime["config"]["backlog_drop"] = 9
    async with asgi_client() as c:
        r = await c.post("/api/config/segmentation/reset")
    assert r.status_code == 200
    assert main.runtime["config"]["backlog_drop"] == main.DEFAULT_CONFIG["backlog_drop"]
    assert seg_env == []


async def test_reset_conflicts_while_reloading(seg_env):
    main.runtime["reloading"] = True
    async with asgi_client() as c:
        r = await c.post("/api/config/segmentation/reset")
    assert r.status_code == 409


# —— 首期反馈②：旧默认值迁移（15/18 → 8/10，仅当恰好等于旧默认时） ——

def test_legacy_segmentation_defaults_migrate_once(tmp_path, monkeypatch):
    """旧版装好的 config.json 里是 700/15.0/18.0（默认值从未被调过）——
    load_config 应一次性迁移到 8/10 并写盘；用户明确改过的组合一律不动。"""
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"silence_ms": 700, "max_seg_s": 15.0,
                             "hard_cut_s": 18.0}), encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", p)
    cfg = main.load_config()
    assert cfg["max_seg_s"] == 8.0 and cfg["hard_cut_s"] == 10.0
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["max_seg_s"] == 8.0 and saved["hard_cut_s"] == 10.0, "迁移未写盘"

    # 用户自己调过的组合（哪怕接近）不迁移
    p.write_text(json.dumps({"silence_ms": 500, "max_seg_s": 12.0,
                             "hard_cut_s": 20.0}), encoding="utf-8")
    cfg2 = main.load_config()
    assert cfg2["silence_ms"] == 500 and cfg2["max_seg_s"] == 12.0
    assert cfg2["hard_cut_s"] == 20.0
