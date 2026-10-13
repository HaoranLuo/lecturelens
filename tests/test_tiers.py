# -*- coding: utf-8 -*-
"""tests/test_tiers.py — 模型档位化：档位表 / 推荐阈值 / 检测 / 端点（离线秒级）。

四层：
1) 档位表完整性：四档、四档模型都在既有白名单与仓库映射里（档位不加旁路）；
2) recommend() 阈值矩阵——用**合成硬件画像**，不依赖本机真实显卡（换机器也能绿）；
3) detect_hardware() 真实本机跑一遍：只断言「不抛异常、键齐全」，不断言值；
4) /api/tiers 端点（ASGI 直调；cache_dir 指 tmp，绝不碰真缓存）与
   user_picked_model()（CONFIG_PATH 指 tmp）。
"""
import json
from contextlib import asynccontextmanager

import httpx
import pytest

import main
import models as models_mod
import tiers as tiers_mod


# —— 1) 档位表完整性 ——

def test_tier_table_integrity():
    assert len(tiers_mod.TIERS) == 4
    names = [t["name"] for t in tiers_mod.TIERS]
    ids = [t["id"] for t in tiers_mod.TIERS]
    assert names == ["流畅", "标准", "高", "极致"]      # 展示顺序：弱→强
    assert len(set(ids)) == 4 and len(set(names)) == 4
    for t in tiers_mod.TIERS:
        assert t["model"] in main.MODEL_CHOICES          # 走既有白名单，不加旁路
        assert t["model"] in models_mod.MODEL_REPOS      # 可经模型管理器下载
        assert t["size_mb"] > 0 and t["desc"]


def test_tier_of_lookup():
    assert tiers_mod.tier_of("large-v3-turbo")["name"] == "高"
    assert tiers_mod.tier_of("base")["name"] == "流畅"
    # 不在档位表 → None（前端按原名显示「自定义」，不硬塞进某档）
    assert tiers_mod.tier_of("medium") is None
    assert tiers_mod.tier_of("tiny") is None
    assert tiers_mod.tier_of(None) is None


# —— 2) 推荐阈值矩阵（合成画像） ——

@pytest.mark.parametrize("hw,expected", [
    # 大显存 → 极致
    ({"gpu": "RTX 5090", "vram_gb": 24.0, "ram_gb": 64.0, "cpu": 16, "apple": False}, "max"),
    ({"gpu": "RTX 4090", "vram_gb": 10.0, "ram_gb": 32.0, "cpu": 16, "apple": False}, "max"),
    # 中显存 → 高（含本机 8GB 场景）
    ({"gpu": "RTX 5060 Laptop GPU", "vram_gb": 8.0, "ram_gb": 31.3, "cpu": 32, "apple": False}, "high"),
    ({"gpu": "GTX 1650", "vram_gb": 4.0, "ram_gb": 16.0, "cpu": 8, "apple": False}, "high"),
    # 有卡但显存读数不可用 → 保守回落到标准
    ({"gpu": "some-gpu", "vram_gb": None, "ram_gb": 16.0, "cpu": 8, "apple": False}, "balanced"),
    # 纯 CPU：内存够 → 标准；不够 → 流畅
    ({"gpu": None, "vram_gb": None, "ram_gb": 16.0, "cpu": 8, "apple": False}, "balanced"),
    ({"gpu": None, "vram_gb": None, "ram_gb": 8.0, "cpu": 8, "apple": False}, "balanced"),
    ({"gpu": None, "vram_gb": None, "ram_gb": 4.0, "cpu": 4, "apple": False}, "fast"),
    # Apple：大内存 → 高；小内存 → 标准
    ({"gpu": None, "vram_gb": None, "ram_gb": 16.0, "cpu": 10, "apple": True}, "high"),
    ({"gpu": None, "vram_gb": None, "ram_gb": 8.0, "cpu": 8, "apple": True}, "balanced"),
])
def test_recommend_thresholds(hw, expected):
    rec = tiers_mod.recommend(hw)
    assert rec["tier"]["id"] == expected
    assert rec["reason"]                                 # 推荐必须带理由（UI 展示）
    assert rec["hardware"] is hw


def test_recommend_is_conservative_no_overreach():
    """保守口径显式断言：8GB 显卡推「高」不推「极致」；阈值以下不越档。"""
    rec = tiers_mod.recommend({"gpu": "RTX 5060", "vram_gb": 8.0,
                               "ram_gb": 32.0, "cpu": 16, "apple": False})
    assert rec["tier"]["model"] == "large-v3-turbo"
    rec = tiers_mod.recommend({"gpu": None, "vram_gb": None,
                               "ram_gb": 0.0, "cpu": 2, "apple": False})
    assert rec["tier"]["model"] == "base"                # 读不到内存 → 最省档


# —— 3) 真实检测：只验形状不验值 ——

def test_detect_hardware_runs_and_shapes():
    hw = tiers_mod.detect_hardware()                     # 绝不抛（检测失败只降级）
    assert set(hw) == {"gpu", "vram_gb", "ram_gb", "cpu", "apple"}
    assert isinstance(hw["cpu"], int) and hw["cpu"] >= 0
    assert isinstance(hw["ram_gb"], float)
    assert isinstance(hw["apple"], bool)
    rec = tiers_mod.recommend(hw)                        # 真实画像也能给出档位
    assert rec["tier"]["model"] in main.MODEL_CHOICES


# —— 4) user_picked_model / /api/tiers 端点 ——

def test_user_picked_model(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", p)
    assert main.user_picked_model() is False             # 文件不存在（首装）
    p.write_text(json.dumps({"auth_token": "x"}), encoding="utf-8")
    assert main.user_picked_model() is False             # 首启只写 token → 未选过
    p.write_text(json.dumps({"auth_token": "x", "model": "small"}), encoding="utf-8")
    assert main.user_picked_model() is True
    p.write_text("{ broken json", encoding="utf-8")
    assert main.user_picked_model() is True              # 损坏按「选过」保守处理，不覆盖


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_api_tiers_endpoint(monkeypatch, tmp_path):
    """/api/tiers：四档 + 单一推荐 + 缓存状态；打桩检测、cache 指 tmp（确定性）。"""
    d = tmp_path / "hub"
    d.mkdir()
    monkeypatch.setattr(models_mod, "cache_dir", lambda: d)
    fake_hw = {"gpu": "RTX 5060 Laptop GPU", "vram_gb": 8.0,
               "ram_gb": 32.0, "cpu": 16, "apple": False}
    monkeypatch.setattr(tiers_mod, "detect_hardware", lambda: fake_hw)
    monkeypatch.setitem(main.runtime, "config",
                        dict(main.DEFAULT_CONFIG, model="large-v3-turbo"))
    monkeypatch.setitem(main.runtime, "downloading", None)
    async with asgi_client() as c:
        r = await c.get("/api/tiers")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and len(data["tiers"]) == 4
    assert data["hardware"]["gpu"] == "RTX 5060 Laptop GPU"
    assert data["recommended_model"] == "large-v3-turbo"     # 8GB → 高
    rec = [t for t in data["tiers"] if t["recommended"]]
    assert len(rec) == 1 and rec[0]["name"] == "高"
    act = [t for t in data["tiers"] if t["active"]]
    assert len(act) == 1 and act[0]["model"] == "large-v3-turbo"
    for t in data["tiers"]:
        assert t["est_h"]                                    # 「需下载 ≈ X」文案数据
        assert t["cached"] is False and t["partial"] is False  # tmp 缓存为空
        assert t["downloading"] is False
