# -*- coding: utf-8 -*-
"""tests/test_glossary_terms.py — p3-2 术语高亮：GET /api/glossary/terms + 修订号。

只锁**后端**能保证的两件事（前端高亮与缓存失效在真浏览器核验，见交付说明）：
① `/api/glossary/terms` 返回**当前生效**词条（来源 = translator.entries，与翻译/
   识别两侧 prompt 同源），形状稳定、去重、无 translator 时为空且带 translate_ready；
② rev 是内容指纹——同内容必稳定（前端可安全缓存）、任一变化必不同（缓存必失效）；
   并且 `/api/status` 里的 glossary_rev 与端点给出的是**同一个值**（前端就是靠这个
   比对来丢缓存的，两处算法一旦分叉，失效就静默失灵）。

全程 ROOT → tmp_path：绝不碰仓库真实 glossary/CS.md（test_glossary_api 同款隔离）。"""
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import main
from translate import DeepSeekTranslator

SAMPLE = ("# 课程术语表 — 示例\n"
          "neural network = 神经网络\n"
          "attention mechanism = 注意力机制\n"
          "transformer = Transformer\n")


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def terms_env(tmp_path, monkeypatch):
    """ROOT → tmp_path（隔离真实 glossary/），translator 缺省 None。"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setitem(main.runtime, "translator", None)
    monkeypatch.setitem(main.runtime, "prompt", None)
    (tmp_path / "glossary").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "glossary" / "CS.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def _translator_for(path: Path) -> DeepSeekTranslator:
    return DeepSeekTranslator("test-key", glossary_files=[str(path)])


# —— ① 端点形状与来源 ——

async def test_terms_endpoint_without_translator(terms_env):
    """未配 key = 术语表不参与任何一侧 → 空表 + translate_ready=false（UI 据此解释
    「为什么没高亮」，而不是让用户以为功能坏了）。"""
    async with asgi_client() as c:
        r = await c.get("/api/glossary/terms")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["terms"] == [] and d["count"] == 0
    assert d["files"] == [] and d["translate_ready"] is False
    assert isinstance(d["rev"], str) and d["rev"]


async def test_terms_endpoint_returns_active_entries(terms_env, monkeypatch):
    monkeypatch.setitem(main.runtime, "translator", _translator_for(terms_env))
    async with asgi_client() as c:
        d = (await c.get("/api/glossary/terms")).json()
    assert d["count"] == 3 and d["translate_ready"] is True
    assert d["files"] == ["CS.md"]                     # 裸文件名（不泄露绝对路径）
    assert d["terms"] == [{"en": "neural network", "zh": "神经网络"},
                          {"en": "attention mechanism", "zh": "注意力机制"},
                          {"en": "transformer", "zh": "Transformer"}]


async def test_terms_are_what_translation_actually_uses(terms_env, monkeypatch):
    """来源必须是 translator.entries——高亮的意义是「这个词正是引擎在保的词」。
    构造的 translator 与端点应逐条一致（含顺序）。"""
    tr = _translator_for(terms_env)
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        d = (await c.get("/api/glossary/terms")).json()
    assert [t["en"] for t in d["terms"]] == [en for en, _ in tr.entries]
    assert [t["zh"] for t in d["terms"]] == [zh for _, zh in tr.entries]


async def test_terms_deduped_by_en_case_insensitive(terms_env, monkeypatch):
    """同一 EN 出现两次（大小写不同）只出一份——否则前端会拿到互相切碎的正则。"""
    tr = _translator_for(terms_env)
    tr.entries = [("Neural Network", "神经网络"), ("neural network", "神经网")]
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        d = (await c.get("/api/glossary/terms")).json()
    assert d["count"] == 1 and d["terms"] == [{"en": "Neural Network", "zh": "神经网络"}]


async def test_terms_skips_blank_fields(terms_env, monkeypatch):
    tr = _translator_for(terms_env)
    tr.entries = [("", "空"), ("  ", "空白"), ("ok", "")]
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        d = (await c.get("/api/glossary/terms")).json()
    assert d["terms"] == [] and d["count"] == 0


# —— ② rev：内容指纹（前端缓存的唯一判据） ——

async def test_rev_stable_for_same_content(terms_env, monkeypatch):
    monkeypatch.setitem(main.runtime, "translator", _translator_for(terms_env))
    async with asgi_client() as c:
        a = (await c.get("/api/glossary/terms")).json()["rev"]
        b = (await c.get("/api/glossary/terms")).json()["rev"]
    assert a == b, "内容没变 rev 必须稳定——否则前端每次轮询都会白重拉"


async def test_rev_changes_on_term_change(terms_env, monkeypatch):
    tr = _translator_for(terms_env)
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        before = (await c.get("/api/glossary/terms")).json()["rev"]
        tr.entries = tr.entries + [("overfitting", "过拟合")]
        after = (await c.get("/api/glossary/terms")).json()["rev"]
    assert before != after, "词条变了 rev 必须变——否则前端缓存永不失效"


async def test_rev_changes_on_file_switch(terms_env, monkeypatch):
    """文件清单也算指纹的一部分（换课程 = 换文件集合）。"""
    tr = _translator_for(terms_env)
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        before = (await c.get("/api/glossary/terms")).json()["rev"]
        tr.glossary_files = [str(terms_env), str(terms_env)]   # 清单变化
        after = (await c.get("/api/glossary/terms")).json()["rev"]
    assert before != after


async def test_status_exposes_same_rev(terms_env, monkeypatch):
    """前端靠 /api/status 的 glossary_rev 判失效——两处必须是同一个值。
    算法一旦分叉（一处含 files、一处不含），失效会静默失灵且没有任何报错。"""
    monkeypatch.setitem(main.runtime, "translator", _translator_for(terms_env))
    async with asgi_client() as c:
        terms_rev = (await c.get("/api/glossary/terms")).json()["rev"]
        st = (await c.get("/api/status")).json()
    assert st["glossary_rev"] == terms_rev
    assert main.status["glossary_rev"] == terms_rev     # 也写回共享 status


# —— ③ 与既有端点共存 ——

async def test_existing_glossary_endpoint_unaffected(terms_env, monkeypatch):
    """/api/glossary 的既有契约不变（新增 /api/glossary/terms 不遮蔽它）。"""
    monkeypatch.setitem(main.runtime, "translator", _translator_for(terms_env))
    async with asgi_client() as c:
        r = await c.get("/api/glossary")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["active_count"] == 1
    assert [f["name"] for f in d["files"]] == ["CS.md"]
    assert d["files"][0]["entries"][0] == {"en": "neural network", "zh": "神经网络"}


async def test_terms_reflect_write_through(terms_env, monkeypatch):
    """端到端（后端侧）：写词条 → 热重载 → terms 里立刻出现，rev 跟着变。
    前端高亮据此在 ≤2s 轮询内自动失效（真浏览器已核验）。"""
    tr = _translator_for(terms_env)
    monkeypatch.setitem(main.runtime, "translator", tr)
    async with asgi_client() as c:
        before = (await c.get("/api/glossary/terms")).json()
        w = await c.post("/api/glossary", json={"file": "CS.md", "op": "add",
                                                "en": "C++", "zh": "C加加"})
        assert w.status_code == 200 and w.json()["reloaded"] is True
        after = (await c.get("/api/glossary/terms")).json()
    assert after["count"] == before["count"] + 1
    assert {"en": "C++", "zh": "C加加"} in after["terms"]
    assert after["rev"] != before["rev"]
