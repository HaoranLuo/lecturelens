# -*- coding: utf-8 -*-
"""tests/test_hub.py — 广播中心 Hub 离线单测（升级计划条目 2；v02-9 扩展 ui_metrics）。

笔记落盘路径已被 conftest 重定向到 tmp_path；广播用假 WebSocket 客户端只验
json 序列化与死连接剔除。真实 WebSocket 行为（握手/断连）由 smoke_test [5]
的服务子进程段覆盖。条目9④：ui_metrics 上行解析 = apply_ui_metrics 纯函数
单测 + TestClient websocket 布线单测（不进 lifespan，避免拉起真模型）。"""
import json
import secrets
import time

import main


class FakeWS:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(data)


def test_start_transcript_writes_header():
    hub = main.Hub()
    p = hub.start_transcript()
    assert p.is_file()
    assert "双语课堂笔记" in p.read_text(encoding="utf-8")
    assert hub.transcript_path == p


def test_append_noop_without_transcript():
    hub = main.Hub()
    hub.append("12:00:01", "hello", "", None)  # 未 start_transcript：静默跳过，不抛


def test_append_en_line():
    hub = main.Hub()
    p = hub.start_transcript()
    hub.append("12:00:01", "hello world", "", None)
    assert "- `12:00:01` hello world\n" in p.read_text(encoding="utf-8")


def test_append_zh_line_with_seg_anchor():
    """晚到的中文行必须带 `#seg` 锚定父 EN 行（main.py append 的配对错乱修复）。"""
    hub = main.Hub()
    p = hub.start_transcript()
    hub.append("12:00:02", "", "你好世界", 3)
    hub.append("12:00:03", "", "无锚行", None)
    text = p.read_text(encoding="utf-8")
    assert "  - `#3` 你好世界\n" in text
    assert "  - 无锚行\n" in text


async def test_broadcast_sends_json_and_prunes_dead_clients():
    hub = main.Hub()
    good, bad = FakeWS(), FakeWS(fail=True)
    hub.clients.update([good, bad])
    await hub.broadcast({"en": "hi", "seg": 1})
    assert [json.loads(d) for d in good.sent] == [{"en": "hi", "seg": 1}]
    assert good in hub.clients   # 活连接保留
    assert bad not in hub.clients  # 发送失败的连接被剔除


async def test_broadcast_with_no_clients_is_noop():
    hub = main.Hub()
    await hub.broadcast({"en": "hi"})  # 空客户端集：不应抛异常


# ---------------- 条目9④：ui_metrics 上行（zh_dropped 并入 /api/status） ----------------

def test_apply_ui_metrics_accepts():
    main.status["ui_zh_dropped"] = 0
    try:
        assert main.apply_ui_metrics({"type": "ui_metrics", "zh_dropped": 3}) is True
        assert main.status["ui_zh_dropped"] == 3
        assert main.apply_ui_metrics({"type": "ui_metrics", "zh_dropped": 0}) is True
        assert main.status["ui_zh_dropped"] == 0  # 合法清零
    finally:
        main.status["ui_zh_dropped"] = 0


def test_apply_ui_metrics_rejects():
    main.status["ui_zh_dropped"] = 0
    bad_payloads = (
        None, "x", 42, [],                                 # 非 dict / 类型不符
        {"type": "other", "zh_dropped": 1},                # type 不对
        {"type": "ui_metrics"},                            # 缺 zh_dropped
        {"type": "ui_metrics", "zh_dropped": -1},          # 负数
        {"type": "ui_metrics", "zh_dropped": 1.5},         # float
        {"type": "ui_metrics", "zh_dropped": "3"},         # str
        {"type": "ui_metrics", "zh_dropped": True},        # bool 是 int 子类，必须排除
    )
    try:
        for bad in bad_payloads:
            assert main.apply_ui_metrics(bad) is False, f"应拒绝: {bad!r}"
        assert main.status["ui_zh_dropped"] == 0, "被拒绝的上行不得改写 status"
    finally:
        main.status["ui_zh_dropped"] = 0


def _poll(pred, timeout: float = 2.0) -> bool:
    """TestClient 的 WS 上行在 portal 线程异步处理：轮询到生效或超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_ws_ui_metrics_end_to_end():
    """布线验证：/ws 收到 ui_metrics → status 更新；非 JSON/非法值不炸不改写。
    不用 `with TestClient(...)`（会跑 lifespan 拉起 asr_loop 真模型）。
    条目19：/ws 走 token 鉴权——TestClient 对端（testclient）非环回，须带 token
    （运行期生成并还原现场，不污染后续测试模块的 runtime['config']）。"""
    from fastapi.testclient import TestClient

    saved = main.status["ui_zh_dropped"]
    saved_cfg = dict(main.runtime["config"])
    tok = secrets.token_urlsafe(16)
    main.runtime["config"]["auth_token"] = tok
    try:
        client = TestClient(main.app)
        with client.websocket_connect(f"/ws?token={tok}") as ws:
            ws.send_text("ping")                                            # 非 JSON 忽略
            ws.send_text(json.dumps({"type": "ui_metrics", "zh_dropped": 3}))
            ws.send_text("not json")                                        # 非 JSON 忽略
            ws.send_text(json.dumps({"type": "ui_metrics", "zh_dropped": -1}))  # 非法忽略
            ws.send_text(json.dumps({"type": "ui_metrics", "zh_dropped": 7}))
            assert _poll(lambda: main.status["ui_zh_dropped"] == 7), \
                f"ui_metrics 未生效: {main.status['ui_zh_dropped']}"
        # with 退出 = 客户端断开：handler 走 WebSocketDisconnect 分支正常收尾即通过
    finally:
        main.status["ui_zh_dropped"] = saved
        main.runtime["config"].clear()
        main.runtime["config"].update(saved_cfg)
