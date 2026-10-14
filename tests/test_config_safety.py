# -*- coding: utf-8 -*-
"""tests/test_config_safety.py — /api/config 配置安全单测（升级计划条目 4）。

验收口径（§2.2-4）：
- 非法值 → HTTP 400，runtime['config'] / config.json / status 全部不碰；
- 构建失败 → 两者仍是旧值（save-after-build，runtime 与文件零污染）；
- reloading 竞态守卫；
- 全键白名单：未知键 400；lang 为条目 13 预留键（仅校验+持久化，不生效）。

ASGI 直调；CONFIG_PATH 由 conftest 重定向 tmp_path；apply_segmenter 打桩；
build_asr 打桩为可编程 fake，模拟真实成功契约「构建成功才把 overrides 合并进
runtime['config']」（真实合并逻辑在 main.build_asr，另有 _resolve_build_cfg 纯函数单测）。"""
import asyncio
import json
import queue
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

import main
from main import ALLOWED_CONFIG_KEYS, DEFAULT_CONFIG

REAL_BUILD_ASR = main.build_asr  # 模块导入时捕获原函数（app_state fixture 会打桩 main.build_asr）
REAL_APPLY_SEGMENTER = main.apply_segmenter  # 同上（app_state fixture 会打桩）


@pytest.fixture
def app_state(monkeypatch):
    """打桩重型副作用 + 复位模块级可变状态，保证测试互不污染。"""
    monkeypatch.setattr(main, "apply_segmenter", lambda: None)
    ns = SimpleNamespace(build_calls=[], build_error=None)

    def fake_build(overrides=None):
        ns.build_calls.append(dict(overrides or {}))
        if ns.build_error is not None:
            main.status["asr"] = f"error: {ns.build_error}"
            raise RuntimeError(ns.build_error)
        # 模拟真实 build_asr 的成功契约：构建成功才合并 overrides（条目4④）
        main.runtime["config"].update(overrides or {})
        main.status["asr"] = "ready"

    monkeypatch.setattr(main, "build_asr", fake_build)

    main.runtime["config"].clear()
    main.runtime["config"].update(DEFAULT_CONFIG)
    main.runtime["reloading"] = False
    main.runtime["asr"] = None
    main.runtime["segmenter"] = None
    main.runtime["seg_q"] = None
    main.status["asr"] = "init"
    return ns


@asynccontextmanager
async def asgi_client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _wait_reload_done():
    """_reload 是独立 asyncio 任务：让出事件循环直到收尾（上限 ~2s 防挂死）。"""
    for _ in range(200):
        if not main.runtime["reloading"]:
            return
        await asyncio.sleep(0.01)


# ---------------- 白名单与整体校验（先验后赋值） ----------------

async def test_illegal_model_400_no_side_effects(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "gpt-5"})
    assert r.status_code == 400
    assert "model" in r.json()["error"] and "gpt-5" in r.json()["error"]
    assert main.runtime["config"]["model"] == DEFAULT_CONFIG["model"]
    assert not main.CONFIG_PATH.exists()  # 非法值不得写坏 config.json


async def test_illegal_device_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"device": "tpu"})
    assert r.status_code == 400
    assert main.runtime["config"]["device"] == DEFAULT_CONFIG["device"]
    assert not main.CONFIG_PATH.exists()


async def test_illegal_vad_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"vad": "webrtc"})
    assert r.status_code == 400
    assert not main.CONFIG_PATH.exists()


async def test_illegal_compute_type_400(app_state):
    """v02-2 时 compute_type 完全不校验（任意值直通）——条目4 白名单补齐。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"compute_type": "fp16"})
    assert r.status_code == 400
    assert "compute_type" in r.json()["error"]
    assert main.runtime["config"]["compute_type"] == DEFAULT_CONFIG["compute_type"]


async def test_unknown_key_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"foo": 1})
    assert r.status_code == 400
    assert "未知配置键" in r.json()["error"]
    assert not main.CONFIG_PATH.exists()


async def test_mixed_valid_invalid_mutates_nothing(app_state):
    """整体校验核心场景：合法键在前、非法键在后 → 任何状态不得被部分污染
    （v02-2 时逐键短路会在 runtime 留下合法键的新值）。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "tiny", "vad": "webrtc"})
    assert r.status_code == 400
    assert main.runtime["config"]["model"] == DEFAULT_CONFIG["model"]  # 未被 tiny 污染
    assert main.runtime["config"]["vad"] == DEFAULT_CONFIG["vad"]
    assert not main.CONFIG_PATH.exists()


async def test_non_dict_body_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json=[1, 2])
    assert r.status_code == 400
    assert "JSON 对象" in r.json()["error"]


async def test_noop_change_saves_nothing(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {}}
        r2 = await c.post("/api/config", json={"model": DEFAULT_CONFIG["model"]})
    assert r2.json() == {"ok": True, "changed": {}}
    assert not main.CONFIG_PATH.exists()  # 无变更不落盘


async def test_reloading_guard(app_state):
    """模型切换进行中的第二次请求必须被拒（守卫在入口原子完成：检查+置位无 await）。"""
    main.runtime["reloading"] = True
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "tiny"})
    assert r.json()["ok"] is False
    assert main.runtime["config"]["model"] == DEFAULT_CONFIG["model"]


async def test_allowed_keys_constant(app_state):
    """锁白名单键集合：防止后续演进意外收窄/扩大（条目19 全键核对的基础）。
    p1-5：+first_run_done（首启向导标记）；p1-7：+floating_bar（B1 悬浮条显隐）；
    p2-5：+5 个断句/积压调优键（B4 断句参数面板）；p3-1：+3 个全局快捷键键
    （B12：开关 + 两个键位）；p3-5：+6 个窗口版式记忆键（B12：主窗 4 + 悬浮条 2）；
    §3：+export_dir（导出位置自选：绝对路径字符串或 null，走 exportdir.dir_problem）；
    §5：+class_end_confirm（下课归档的二次确认开关，布尔枚举）；
    §7：+lan_enabled（手机同屏开关，布尔枚举；写盘即存，换绑监听地址要重启）；
    D2：+hf_mirror（模型下载镜像开关，布尔枚举；写盘即存，HF endpoint 要重启才换）。"""
    assert set(ALLOWED_CONFIG_KEYS) == {"model", "device", "compute_type", "vad",
                                        "lang", "streaming", "source", "input_device",
                                        "translate_model", "auth_enabled",
                                        "first_run_done", "floating_bar",
                                        # p2-5（B4）：数值区间校验，见 CONFIG_NUM_RANGES
                                        "silence_ms", "max_seg_s", "hard_cut_s",
                                        "backlog_drop", "backlog_keep",
                                        "fade_lines",   # p2-7（B2）：0/1/2 枚举
                                        # p3-1（B12）：键名字符串，走 _hotkey_problem
                                        "hotkeys_enabled", "hotkey_pause",
                                        "hotkey_float",
                                        # p3-5（B12）：窗口版式记忆（整数区间校验；
                                        # 坐标允许为负——多显示器主屏左侧为负坐标）
                                        "window_w", "window_h", "window_x",
                                        "window_y", "float_x", "float_y",
                                        # §3：路径字符串（或 null），走
                                        # exportdir.dir_problem 专项校验
                                        "export_dir",
                                        # §5：下课归档二次确认（布尔枚举，即时键）
                                        "class_end_confirm",
                                        # §7：手机同屏（布尔枚举；非即时键——
                                        # 换绑监听地址要重启，见 main.lan_bind_state）
                                        "lan_enabled",
                                        # D2：模型下载镜像（布尔枚举；非即时键——
                                        # HF_ENDPOINT 在库导入时读入，重启应用才生效）
                                        "hf_mirror"}


# ---------------- streaming 预留键（条目11 灰度，重启服务后生效） ----------------

async def test_streaming_accepts_bool_and_persists(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"streaming": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": {"streaming": True}}
    assert main.runtime["config"]["streaming"] is True
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["streaming"] is True


async def test_streaming_invalid_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"streaming": "yes"})
    assert r.status_code == 400
    assert "streaming" in r.json()["error"]
    assert main.runtime["config"]["streaming"] is False  # DEFAULT_CONFIG 值未被污染
    assert not main.CONFIG_PATH.exists()


# ---------------- lang（条目13：热切换生效，不再只是预留键） ----------------

async def test_lang_persists_and_hot_swaps_asr_instance(app_state):
    """POST lang=zh：持久化 + runtime['asr'].language 即时热改（条目13①，无需重建
    模型）+ auto 检测历史重置（防旧语言短段沿用污染新模式）。"""
    fake_asr = SimpleNamespace(language="en", detected_lang="en")
    main.runtime["asr"] = fake_asr
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lang": "zh"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"lang": "zh"}}
        assert fake_asr.language == "zh"          # 热生效：实例属性已改
        assert fake_asr.detected_lang is None     # 检测历史重置
        assert main.status["lang"] == "zh"
        saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["lang"] == "zh"
        r2 = await c.post("/api/config", json={"lang": "auto"})
    assert fake_asr.language == "auto" and fake_asr.detected_lang is None
    assert main.status["lang"] == "auto"


async def test_lang_extended_code_ja_persists(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lang": "ja"})
    assert r.status_code == 200
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["lang"] == "ja"


async def test_lang_invalid_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"lang": "xx"})
    assert r.status_code == 400
    assert "lang" in r.json()["error"]
    assert main.runtime["config"]["lang"] == DEFAULT_CONFIG["lang"]  # 零污染
    assert not main.CONFIG_PATH.exists()


def test_build_asr_reads_config_lang(app_state, monkeypatch):
    """条目13②：build_asr 从 cfg 读 lang（重启后持久化生效的关键）；legacy config
    无 lang 键时回退 en。

    D2(b)：model_path 现在是 resolve_model_ref 的产物（随包 → 本地缓存 snapshot →
    按名 None），故这里把解析器打桩成「都没命中」，专测 lang 与 None 透传；三档解析
    次序本身在 tests/test_hf_env.py 里单独钉。"""
    captured = {}

    class FakeASR:
        def __init__(self, model_size=None, device=None, compute_type=None,
                     language=None, cpu_threads=4, model_path=None):
            captured["language"] = language
            captured["model_path"] = model_path
            self.model_size = model_size
            self.device = "cpu"
            self.compute_type = "int8"

        def warmup(self):
            pass

    monkeypatch.setattr(main, "WhisperASR", FakeASR)
    monkeypatch.setattr(main, "resolve_model_ref", lambda name: (None, "hub"))
    main.runtime["config"]["lang"] = "zh"
    REAL_BUILD_ASR()
    assert captured["language"] == "zh"
    assert captured["model_path"] is None  # D2：本地都没有 → 交给 faster-whisper 按名取
    assert main.status["asr"] == "ready"
    main.runtime["config"].pop("lang", None)  # 模拟 legacy config.json（无 lang 键）
    REAL_BUILD_ASR()
    assert captured["language"] == "en"


# ---------------- source 音源（条目14：白名单校验 + 热切换） ----------------

async def test_source_invalid_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"source": "spdif"})
    assert r.status_code == 400
    assert "source" in r.json()["error"] and "spdif" in r.json()["error"]
    assert main.runtime["config"]["source"] == DEFAULT_CONFIG["source"]  # 零污染
    assert not main.CONFIG_PATH.exists()


async def test_source_hot_switch_persists_and_rebuilds(app_state, monkeypatch):
    """POST source=loopback：持久化 + 走即时键路径（apply_segmenter 热重建，
    与 vad 同款语义）；服务不重启。"""
    calls = []
    monkeypatch.setattr(main, "apply_segmenter", lambda: calls.append(1))
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"source": "loopback"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"source": "loopback"}}
        r2 = await c.post("/api/config", json={"source": "mic"})
    assert r2.json() == {"ok": True, "changed": {"source": "mic"}}
    assert len(calls) == 2  # 两次切换各触发一次重建
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["source"] == "mic"
    assert main.runtime["config"]["source"] == "mic"


def test_apply_segmenter_reads_source_and_input_device(app_state, monkeypatch):
    """条目14 关键修正：apply_segmenter 按 source 选型（loopback→LoopbackSegmenter，
    强制 silero 在类内保证）；device 改读 config 的 input_device（计划点名的
    main.py:150 硬编码修正），无该键回退 ARGS.device。"""
    from types import SimpleNamespace
    captured: dict = {}
    stopped: list = []

    class _OldSeg:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class FakeMic:
        # **kw：p2-5 起 apply_segmenter 还会传断句参数（silence_ms/max_seg_s/
        # hard_cut_s）——真 MicSegmenter 的签名就收这三个（capture.py 构造参数）
        def __init__(self, q, device=None, vad=None, stream_q=None, **kw):
            captured.update(kind="mic", device=device, vad=vad, seg=kw)

        def start(self):
            pass

        def stop(self):
            stopped.append("mic")

    class FakeLoop:
        # 真 LoopbackSegmenter(out_queue, **kw) 把 **kw 透传给 MicSegmenter
        def __init__(self, q, stream_q=None, **kw):
            captured.update(kind="loopback", seg=kw)

        def start(self):
            pass

        def stop(self):
            stopped.append("loopback")

    monkeypatch.setattr(main, "MicSegmenter", FakeMic)
    monkeypatch.setattr(main, "LoopbackSegmenter", FakeLoop)
    monkeypatch.setattr(main, "ARGS", SimpleNamespace(device=7))  # CLI --device 回退值
    main.runtime["seg_q"] = object()  # 占位（fake 不消费）
    old = _OldSeg()
    main.runtime["segmenter"] = old  # 验证旧分句器被 stop

    main.runtime["config"]["source"] = "loopback"
    REAL_APPLY_SEGMENTER()
    assert captured["kind"] == "loopback"
    assert main.status["source"] == "loopback"

    main.runtime["config"]["source"] = "mic"
    main.runtime["config"]["input_device"] = 3
    REAL_APPLY_SEGMENTER()
    assert captured["kind"] == "mic"
    assert captured["device"] == 3           # config 的 input_device 生效
    assert captured["vad"] == DEFAULT_CONFIG["vad"]
    # p2-5：断句参数必须从 config 贯通到构造参数（三个键逐一对上）
    assert captured["seg"] == {k: DEFAULT_CONFIG[k] for k in main.SEGMENT_REBUILD_KEYS}

    main.runtime["config"]["silence_ms"] = 1200
    main.runtime["config"]["max_seg_s"] = 9.0
    main.runtime["config"]["hard_cut_s"] = 12.0
    REAL_APPLY_SEGMENTER()
    assert captured["seg"] == {"silence_ms": 1200, "max_seg_s": 9.0,
                               "hard_cut_s": 12.0}

    main.runtime["config"].pop("input_device")  # legacy config 无该键 → 回退 ARGS.device
    REAL_APPLY_SEGMENTER()
    assert captured["device"] == 7           # 回退到 CLI --device
    # 每次重建都 stop 旧分句器：loopback→mic 停了 loopback；p2-5 那次改断句参数
    # 又重建一次（停 mic）；最后换成 CLI --device 再重建一次（停 mic）。
    assert stopped == ["loopback", "mic", "mic"]
    assert len(stopped) == 3
    assert old.stopped                       # 首个旧分句器也被优雅停止
    assert main.runtime["segmenter"] is not old


# ---------------- input_device 麦克风设备（条目15：白名单 + 存在性校验 + null 重置） ----------------

def _stub_sd_devices(monkeypatch, devices=None, default=0):
    """打桩 sounddevice（main 内延迟 import，patch 模块属性即可全局生效）。
    默认 4 台：0/1 无输入通道（模拟输出设备），2/3 是真输入设备。"""
    import sounddevice as sd
    devs = devices if devices is not None else [
        {"name": "Speakers (输出)", "max_input_channels": 0},
        {"name": "HDMI (输出)", "max_input_channels": 0},
        {"name": "Mic A", "max_input_channels": 2},
        {"name": "Mic B", "max_input_channels": 1},
    ]
    monkeypatch.setattr(sd, "query_devices", lambda *a, **k: devs)
    monkeypatch.setattr(sd, "default", SimpleNamespace(device=(default, 1)))
    return devs


async def test_input_device_valid_persists_and_rebuilds(app_state, monkeypatch):
    """§2.2-15：apply_segmenter 消费 config 的 input_device（v04-14 已修的
    main.py:150）+ POST 持久化 + 每次变更触发一次热重建。"""
    _stub_sd_devices(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "apply_segmenter", lambda: calls.append(1))
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": 3})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"input_device": 3}}
        r2 = await c.post("/api/config", json={"input_device": 2})
    assert r2.json() == {"ok": True, "changed": {"input_device": 2}}
    assert len(calls) == 2
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["input_device"] == 2
    assert main.runtime["config"]["input_device"] == 2


async def test_input_device_output_device_400(app_state, monkeypatch):
    """§2.2-15「非法索引 400」：索引指向无输入通道的设备（扬声器/显示器 HDMI）。"""
    _stub_sd_devices(monkeypatch)
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": 0})
    assert r.status_code == 400
    assert "没有输入通道" in r.json()["error"]
    assert main.runtime["config"]["input_device"] is None  # 零污染
    assert not main.CONFIG_PATH.exists()


async def test_input_device_out_of_range_400(app_state, monkeypatch):
    _stub_sd_devices(monkeypatch)
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": 99})
    assert r.status_code == 400
    assert not main.CONFIG_PATH.exists()


@pytest.mark.parametrize("bad", [-1, "mic", True, 1.5])
async def test_input_device_type_400(app_state, bad):
    """类型校验：负数/字符串/bool（int 子类必须排除）/浮点 → 400 零污染。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": bad})
    assert r.status_code == 400
    assert "input_device" in r.json()["error"]
    assert main.runtime["config"]["input_device"] is None
    assert not main.CONFIG_PATH.exists()


async def test_input_device_null_resets_to_system_default(app_state, monkeypatch):
    """null = 重置为跟随系统默认（input_device 特有的重置语义——其他键 null 仍是
    no-op 占位）；重复 null 与现值相同 = no-op 不触发重建。"""
    _stub_sd_devices(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "apply_segmenter", lambda: calls.append(1))
    async with asgi_client() as c:
        await c.post("/api/config", json={"input_device": 3})
        r = await c.post("/api/config", json={"input_device": None})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"input_device": None}}
        r2 = await c.post("/api/config", json={"input_device": None})
    assert r2.json() == {"ok": True, "changed": {}}   # 已是 null → no-op
    assert len(calls) == 2
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["input_device"] is None


async def test_input_device_enum_failure_allows(app_state, monkeypatch):
    """离线环境（无音频 API 枚举失败）放行存在性校验——采集层 watchdog 兜底。"""
    import sounddevice as sd

    def boom(*a, **k):
        raise OSError("PortAudio not available")

    monkeypatch.setattr(sd, "query_devices", boom)
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": 3})
    assert r.status_code == 200
    assert main.runtime["config"]["input_device"] == 3


async def test_input_device_rebuild_failure_keeps_old_segmenter(app_state, monkeypatch):
    """条目15④风险条款：重建失败保留旧采集器——apply_segmenter 构造期抛出时
    runtime['segmenter'] 不动，响应为干净错误（不是裸 500 崩感）。"""
    _stub_sd_devices(monkeypatch)

    def failing_rebuild():
        raise RuntimeError("silero 加载失败")

    monkeypatch.setattr(main, "apply_segmenter", failing_rebuild)
    old = object()
    main.runtime["segmenter"] = old
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"input_device": 3})
    assert r.status_code == 500
    assert r.json()["ok"] is False
    assert "重建失败" in r.json()["error"] and "保留" in r.json()["error"]
    assert main.runtime["segmenter"] is old  # 旧采集器原封不动
    assert main.runtime["reloading"] is False  # 守卫已释放


# ---------------- translate_model（条目16：白名单 + 热切换） ----------------

async def test_translate_model_invalid_400(app_state):
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"translate_model": "gpt-4o"})
    assert r.status_code == 400
    assert "translate_model" in r.json()["error"]
    assert main.runtime["config"]["translate_model"] == DEFAULT_CONFIG["translate_model"]
    assert not main.CONFIG_PATH.exists()


async def test_translate_model_persists_and_hot_swaps(app_state, monkeypatch):
    """POST translate_model=deepseek-reasoner：持久化 + translator.model 即时热改
    （payload 每次请求读 self.model，改属性即生效，无需重建 client）。"""
    from types import SimpleNamespace as _NS
    fake_tr = _NS(model="deepseek-chat", glossary_files=[])
    main.runtime["translator"] = fake_tr
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"translate_model": "deepseek-reasoner"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"translate_model": "deepseek-reasoner"}}
        assert fake_tr.model == "deepseek-reasoner"   # 热生效
        saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["translate_model"] == "deepseek-reasoner"
        r2 = await c.post("/api/config", json={"translate_model": "deepseek-chat"})
    assert fake_tr.model == "deepseek-chat"
    main.runtime["translator"] = None  # 还原单例


# ---------------- /api/devices（条目15①：过滤输入设备列表） ----------------

async def test_api_devices_filtered_with_default_flag(app_state, monkeypatch):
    _stub_sd_devices(monkeypatch, default=2)
    main.runtime["config"]["input_device"] = 3
    async with asgi_client() as c:
        r = await c.get("/api/devices")
    assert r.status_code == 200
    j = r.json()
    assert [d["index"] for d in j["devices"]] == [2, 3]     # 无输入通道的 0/1 被过滤
    assert j["devices"][0]["name"] == "Mic A"
    assert j["devices"][0]["default"] is True                # default 指向 index 2
    assert j["devices"][1]["default"] is False
    assert j["devices"][1]["inputs"] == 1
    assert j["current"] == 3                                 # config 现值透传
    assert "error" not in j


async def test_api_devices_enum_failure_graceful(app_state, monkeypatch):
    """枚举失败不 500：devices=[] + error 字段（字幕页照常工作，仅下拉为空）。"""
    import sounddevice as sd

    def boom(*a, **k):
        raise OSError("no audio backend")

    monkeypatch.setattr(sd, "query_devices", boom)
    async with asgi_client() as c:
        r = await c.get("/api/devices")
    assert r.status_code == 200
    j = r.json()
    assert j["devices"] == []
    assert "error" in j and "no audio backend" in j["error"]
    assert j["current"] is None


async def test_asr_loop_cli_device_priority(app_state, monkeypatch):
    """条目15③：CLI --device 最高优先级——asr_loop 把它写入 cfg['input_device']
    （数字串归一为索引；非数字串按设备名透传），真 apply_segmenter 消费 config
    把 device 传给 MicSegmenter。"""
    captured = {}

    class FakeSeg:
        # **kw：p2-5 起还会传断句参数（真 MicSegmenter 收这三个，见 capture.py）
        def __init__(self, q, device=None, vad=None, stream_q=None, **kw):
            captured["device"] = device

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return {}

    async def _noop_status_loop():
        return

    monkeypatch.setattr(main, "MicSegmenter", FakeSeg)
    monkeypatch.setattr(main, "LoopbackSegmenter", FakeSeg)
    monkeypatch.setattr(main, "ARGS", SimpleNamespace(device=None))
    monkeypatch.setattr(main, "status_loop", _noop_status_loop)
    monkeypatch.setattr(main, "load_api_key", lambda *a, **k: "")
    # app_state fixture 已把 apply_segmenter 桩成 no-op——恢复真身（模块导入时捕获），
    # 验证 config → 构造参数的贯通；build_asr 维持 fixture 的桩
    monkeypatch.setattr(main, "apply_segmenter", REAL_APPLY_SEGMENTER)
    main.runtime["seg_q"] = queue.Queue()
    main.CONFIG_PATH.write_text(json.dumps({"model": "tiny"}), encoding="utf-8")

    async def _run(device):
        captured.clear()
        args = SimpleNamespace(model=None, gpu=False, compute_type=None, vad=None,
                               streaming=False, lang=None, api_key="", glossary=[],
                               source=None, device=device)
        task = asyncio.create_task(main.asr_loop(args))
        for _ in range(100):
            if "device" in captured:
                break
            await asyncio.sleep(0.02)
        main.status["paused"] = True   # 停在暂停分支，取消不卡执行器线程
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        main.status["paused"] = False
        return captured.get("device")

    dev = await _run("3")
    assert dev == 3                       # CLI 数字串归一为索引
    assert main.runtime["config"]["input_device"] == 3
    dev2 = await _run("USB Microphone")   # 非数字串按设备名透传（sounddevice 按名查找）
    assert dev2 == "USB Microphone"
    main.hub.transcript_path = None


# ---------------- 即时键与模型键（save-after-build） ----------------

async def test_legal_vad_persists(app_state, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="main"):
        async with asgi_client() as c:
            r = await c.post("/api/config", json={"vad": "energy"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": {"vad": "energy"}}
    assert "config 已更新" in caplog.text  # 条目3验收关键字（§2.2-3），即时路径仍打
    assert main.runtime["config"]["vad"] == "energy"
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["vad"] == "energy"


async def test_model_change_save_after_build_success(app_state):
    """构建成功 → overrides 传入 build_asr → 合并进 runtime → 才持久化。"""
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "tiny"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "changed": {"model": "tiny"}}
        await _wait_reload_done()
    assert main.runtime["reloading"] is False
    assert app_state.build_calls == [{"model": "tiny"}]
    assert main.runtime["config"]["model"] == "tiny"
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["model"] == "tiny"


async def test_model_change_failure_no_pollution(app_state):
    """§2.2-4 核心：构建失败 → runtime['config'] 与 config.json 均为旧值。"""
    app_state.build_error = "boom: out of memory"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"model": "tiny"})
        assert r.status_code == 200  # 受理成功；失败在异步构建阶段
        await _wait_reload_done()
    assert main.runtime["reloading"] is False
    assert app_state.build_calls == [{"model": "tiny"}]
    assert main.runtime["config"]["model"] == DEFAULT_CONFIG["model"]  # 零污染
    assert not main.CONFIG_PATH.exists()  # 从未落盘
    assert "error: boom" in main.status["asr"]


async def test_instant_key_persists_even_when_model_build_fails(app_state):
    """同请求混合即时键+模型键、构建失败：vad 照常生效并落盘，模型键零污染
    （条目5 评审 P5 的断链修复方向：即时路径不得被构建失败连坐）。"""
    app_state.build_error = "boom"
    async with asgi_client() as c:
        r = await c.post("/api/config", json={"vad": "energy", "model": "tiny"})
        assert r.status_code == 200
        await _wait_reload_done()
    saved = json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["vad"] == "energy"
    assert saved["model"] == DEFAULT_CONFIG["model"]
    assert main.runtime["config"]["vad"] == "energy"
    assert main.runtime["config"]["model"] == DEFAULT_CONFIG["model"]


# ---------------- build_asr 纯函数 ----------------

def test_resolve_build_cfg_does_not_mutate_runtime(app_state):
    main.runtime["config"]["model"] = "large-v3-turbo"
    main.runtime["config"]["streaming"] = True
    merged = main._resolve_build_cfg({"model": "tiny", "vad": "energy"})
    assert merged == {"model": "tiny", "device": "auto", "compute_type": None,
                      "vad": "energy", "streaming": True, "lang": "en",
                      "source": "mic", "input_device": None,
                      "translate_model": "deepseek-chat",
                      "auth_enabled": True, "auth_token": None,  # 条目14/15/16/19 新增键
                      "first_run_done": False,   # p1-5 新增键
                      "floating_bar": False,     # p1-7 新增键
                      # p2-5（B4）新增键：断句参数与积压阈值
                      "silence_ms": 700, "max_seg_s": 15.0, "hard_cut_s": 18.0,
                      "backlog_drop": 5, "backlog_keep": 2,
                      "fade_lines": 1,   # p2-7（B2）新增键
                      # p3-1（B12）新增键：全局快捷键开关与两个键位
                      "hotkeys_enabled": True, "hotkey_pause": "ctrl+alt+l",
                      "hotkey_float": "ctrl+alt+s",
                      # p3-5（B12）新增键：窗口版式记忆（None = 还没记忆过）
                      "window_w": None, "window_h": None,
                      "window_x": None, "window_y": None,
                      "float_x": None, "float_y": None,
                      "export_dir": None,   # §3 新增键：导出位置（未设置）
                      "class_end_confirm": True,   # §5 新增键：下课归档二次确认
                      "lan_enabled": False,  # §7 新增键：手机同屏（默认关＝只听 127.0.0.1）
                      # D2 新增键：模型下载镜像（默认关 = 走官方 huggingface.co）
                      "hf_mirror": False}
    assert main.runtime["config"]["model"] == "large-v3-turbo"  # 原配置不动
    assert main.runtime["config"]["streaming"] is True
    assert main._resolve_build_cfg() == dict(main.runtime["config"])  # 无 overrides = 原样


def test_resolve_build_cfg_empty_overrides(app_state):
    assert main._resolve_build_cfg({}) == dict(main.runtime["config"])


# ---------------- 评审1：并发竞态回归（TOCTOU / 启动构建不持守卫） ----------------

async def test_concurrent_model_posts_spawn_single_build(app_state, monkeypatch):
    """评审1 TOCTOU 回归：守卫检查与置位之间的 await 让出点（req.json /
    apply_segmenter）曾让两个并发模型 POST 都通过守卫、各自 spawn _reload
    （两个 WhisperModel 并发加载，后完成者覆盖 runtime['asr']）。修复后预约
    在任何 await 之前原子完成 → 恰好一个请求被受理、恰好一次构建。"""
    import threading
    release = threading.Event()
    calls: list = []

    def blocking_build(overrides=None):
        calls.append(dict(overrides or {}))
        release.wait(timeout=5)  # 模拟秒级模型加载，撑开并发窗口
        main.runtime["config"].update(overrides or {})
        main.status["asr"] = "ready"

    monkeypatch.setattr(main, "build_asr", blocking_build)
    async with asgi_client() as c:
        r1, r2 = await asyncio.gather(
            c.post("/api/config", json={"model": "base"}),
            c.post("/api/config", json={"model": "tiny"}),
        )
    j1, j2 = r1.json(), r2.json()
    accepted = [j for j in (j1, j2) if j["ok"] is True]
    rejected = [j for j in (j1, j2) if j["ok"] is False]
    assert len(accepted) == 1, f"并发窗口内应恰好受理一个请求: {j1} / {j2}"
    assert len(rejected) == 1 and "切换进行中" in rejected[0]["error"]
    release.set()
    await _wait_reload_done()
    assert main.runtime["reloading"] is False
    assert len(calls) == 1, f"并发窗口内不得出现两次构建: {calls}"
    assert calls[0]["model"] == accepted[0]["changed"]["model"]


async def test_startup_build_holds_reloading_guard(app_state, monkeypatch):
    """评审1 场景2 回归：asr_loop 的启动初始构建（首次下载可达分钟级）必须持
    reloading 守卫——期间模型键 POST 应被拒，而非与启动构建并发（两个
    build_asr 后完成者覆盖 runtime['asr']）。"""
    import threading
    release = threading.Event()
    calls: list = []

    def blocking_build(overrides=None):
        calls.append(dict(overrides or {}))
        release.wait(timeout=5)  # 模拟首次运行的分钟级下载
        main.runtime["asr"] = object()
        main.status["asr"] = "ready"

    async def _noop_status_loop():
        return

    monkeypatch.setattr(main, "build_asr", blocking_build)
    monkeypatch.setattr(main, "load_api_key", lambda *a, **k: "")  # 跳过 translator
    monkeypatch.setattr(main, "status_loop", _noop_status_loop)    # 防悬空常驻任务
    # apply_segmenter 已由 fixture 打桩

    args = SimpleNamespace(model="base", gpu=False, compute_type="int8",
                           vad="energy", streaming=False, lang=None,
                           api_key="", glossary=[],
                           source=None, device=None)  # 条目14/15 CLI 占位
    task = asyncio.create_task(main.asr_loop(args))
    try:
        # 等初始构建进入阻塞段：此刻守卫必须已被启动路径置位
        t0 = time.monotonic()
        while not (calls and main.runtime["reloading"]):
            assert time.monotonic() - t0 < 3.0, "启动构建未持 reloading 守卫"
            await asyncio.sleep(0.02)
        async with asgi_client() as c:
            r = await c.post("/api/config", json={"model": "tiny"})
        assert r.json()["ok"] is False and "切换进行中" in r.json()["error"]
        assert len(calls) == 1, "启动构建进行中不得并发第二次构建"
    finally:
        main.status["paused"] = True   # 让主循环停在暂停分支的 sleep，取消时不卡执行器线程
        release.set()
        main.hub.transcript_path = None  # 还原单例状态（测试写到 tmp）
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert main.runtime["reloading"] is False
