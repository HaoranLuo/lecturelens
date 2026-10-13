# -*- coding: utf-8 -*-
"""tests/test_applog.py — 结构化日志落盘单测（升级计划条目 3）。

覆盖：utf-8 文件落盘、轮转参数（5MB×5）、脱敏过滤器（Bearer/API key → ***）、
幂等、file=False 控制台模式不落盘、uvicorn 配置纳入 root（propagate）。
§2.2-3 的服务级验收（logs/lecturelens.log 含 model ready / config 已更新 /
笔记文件 关键字 + 假 key 不出现）由真实服务冒烟承担；本文件在单测层先锁
格式与脱敏不变量。"""
import logging
from logging.handlers import RotatingFileHandler

import pytest

import applog
from applog import BACKUP_COUNT, MAX_BYTES, setup_logging, uvicorn_log_config


@pytest.fixture
def fresh_logging(tmp_path):
    """隔离全局 logging 状态：保存 root handlers/levels，测试后精确还原
    并关闭测试期间新建的文件句柄（Windows 下不关会锁住 tmp 文件）。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    names = ("httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access", "main")
    saved = {n: logging.getLogger(n).level for n in names}
    applog._configured = False
    yield tmp_path
    for h in root.handlers[:]:
        if h not in saved_handlers:
            h.close()
            root.removeHandler(h)
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)
    applog._configured = False


def _file_handler() -> RotatingFileHandler:
    fhs = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(fhs) == 1, f"应有且仅有一个 RotatingFileHandler，实际 {len(fhs)}"
    return fhs[0]


def test_setup_creates_utf8_file_with_structured_line(fresh_logging):
    setup_logging(fresh_logging / "logs")
    logging.getLogger("main").info("模型加载中：中文与emoji📝")
    _file_handler().flush()
    p = fresh_logging / "logs" / "lecturelens.log"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")  # 显式 utf-8 读回（GBK 控制台环境不乱码）
    assert "模型加载中：中文与emoji📝" in text
    assert "INFO [main]" in text  # 结构化：级别 + logger 名


def test_rotation_params(fresh_logging):
    setup_logging(fresh_logging / "logs")
    fh = _file_handler()
    assert fh.maxBytes == MAX_BYTES == 5 * 1024 * 1024
    assert fh.backupCount == BACKUP_COUNT == 5
    assert fh.encoding == "utf-8"
    assert fh.baseFilename.endswith("lecturelens.log")


def test_redaction_hides_secrets(fresh_logging):
    """脱敏不变量：假 key 无论以 Bearer/k=v/env 三种写法出现都不得落盘（§2.2-3）。"""
    fake = "sk-fake-abcdef123456"
    setup_logging(fresh_logging / "logs")
    logging.getLogger("main").warning(
        f"Authorization: Bearer {fake} / DEEPSEEK_API_KEY={fake} / env DEEPSEEK_API_KEY: {fake}")
    _file_handler().flush()
    text = (fresh_logging / "logs" / "lecturelens.log").read_text(encoding="utf-8")
    assert fake not in text, "假 key 泄漏进日志"
    assert text.count("***") == 3  # 三种写法都被改写


def test_redaction_scrubs_traceback(fresh_logging):
    """评审1：exc_info 的 traceback 原文不经 getMessage()——formatException 输出
    曾绕过消息级脱敏直落文件。修复后栈内密钥必须被改写，且栈本身保留。"""
    fake = "sk-tb-secret-1234567890"
    setup_logging(fresh_logging / "logs")
    try:
        raise RuntimeError(f"Authorization: Bearer {fake} leaked in frame")
    except RuntimeError:
        logging.getLogger("main").exception("带异常栈的失败")
    _file_handler().flush()
    text = (fresh_logging / "logs" / "lecturelens.log").read_text(encoding="utf-8")
    assert fake not in text, "traceback 里的假 key 泄漏进日志"
    assert "Traceback (most recent call last)" in text, "异常栈本身必须保留（只脱敏密钥）"
    assert "Bearer ***" in text
    assert "RuntimeError: Authorization" in text  # 消息本体保留（脱敏后）


def test_setup_is_idempotent(fresh_logging):
    setup_logging(fresh_logging / "logs")
    setup_logging(fresh_logging / "logs")
    assert len([h for h in logging.getLogger().handlers
                if isinstance(h, RotatingFileHandler)]) == 1


def test_console_only_mode_writes_no_file(fresh_logging):
    setup_logging(fresh_logging / "logs", file=False)  # smoke_test 用法
    logging.getLogger("main").info("hello")
    assert not (fresh_logging / "logs").exists()  # 不建目录、不写文件
    assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in logging.getLogger().handlers)


def test_uvicorn_log_config_propagates_to_root():
    cfg = uvicorn_log_config()
    assert cfg["disable_existing_loggers"] is False  # 否则 dictConfig 禁用模块 logger
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert "handlers" not in cfg["loggers"][name]  # 无自带 handler → propagate 到 root
        assert cfg["loggers"][name]["level"] == "WARNING"  # INFO 行维持现状被抑制


def test_third_party_httpx_silenced_to_warning(fresh_logging):
    setup_logging(fresh_logging / "logs")
    # httpx 每请求一条 INFO 会改变控制台现状（条目3：控制台行为不变）；
    # httpx2/httpcore2 是本机同装的 httpx 分支，faster-whisper 拉模型在用
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level == logging.WARNING, name
