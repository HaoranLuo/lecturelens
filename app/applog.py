# -*- coding: utf-8 -*-
"""applog.py — 结构化日志落盘（升级计划条目 3）。

设计（docs/upgrade-plan.md §1 条目 3）：
- RotatingFileHandler(logs/lecturelens.log, maxBytes=5MB, backupCount=5,
  encoding='utf-8')——显式 utf-8（本机 cmd 是 GBK 代码页，不显式必乱码）；
- console handler 写 sys.stdout、格式 %(message)s——模块消息文本保留原 print
  的「[xxx]」前缀，控制台行为与改造前逐字一致；
- 模块 logger（main/capture/translate/asr）propagate 到 root，统一经这两个
  handler 出口；uvicorn 通过 main.UVICORN_LOG_CONFIG 的 dictConfig（
  disable_existing_loggers=False + 自带 handler 清空 + propagate 到 root）
  纳入同一落盘，其 INFO 行维持现状被 WARNING 级别抑制；
- 脱敏：RedactFilter 在 handler 层把 Bearer token 与 DEEPSEEK_API_KEY 赋值
  改写为 ***——API key 任何情况不落日志（key 只从 env/.env 读）；
- 重复调用幂等（_configured 门闩），smoke_test 以 file=False 复用同一控制台
  行为而不写文件。
"""
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "lecturelens.log"
MAX_BYTES = 5 * 1024 * 1024   # 5MB，与 backupCount=5 合计磁盘占用封顶 5MB×6
BACKUP_COUNT = 5

_FILE_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# 脱敏：Authorization Bearer token、DEEPSEEK_API_KEY 键值对（env 行 / .env 行 / k=v 文本）
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_APIKEY_RE = re.compile(r"(DEEPSEEK_API_KEY\s*[=:]\s*)\S+")

_configured = False


def _scrub(text: str) -> str:
    """对任意文本做密钥改写（Bearer token / DEEPSEEK_API_KEY 赋值 → ***）。"""
    scrubbed = _BEARER_RE.sub(r"\1***", text)
    return _APIKEY_RE.sub(r"\1***", scrubbed)


class RedactFilter(logging.Filter):
    """把记录里的密钥改写为 ***，再放行（防 API key 意外进日志）。
    评审1：traceback 原文不经 getMessage()——exc_info=True 时 formatException
    的输出会绕过消息级脱敏直落文件。因此先格式化 traceback、脱敏后写入
    exc_text 并清空 exc_info（防 formatter 二次 formatException 绕过）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            try:
                tb = logging.Formatter().formatException(record.exc_info)
            except Exception:
                tb = ""
            record.exc_text = _scrub(tb)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _scrub(record.exc_text)
        msg = record.getMessage()
        scrubbed = _scrub(msg)
        if scrubbed != msg:
            record.msg, record.args = scrubbed, None
        return True


def setup_logging(logs_dir: Path | None, file: bool = True) -> None:
    """配置 root logger：utf-8 轮转文件 + stdout 控制台。幂等。

    file=False 时只挂控制台（smoke_test 复用同一控制台行为，不产生日志文件）。"""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if file and logs_dir is not None:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(logs_dir / LOG_FILE_NAME,
                                 maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                 encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
        fh.addFilter(RedactFilter())
        root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)  # print 原走 stdout，控制台行为不变
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    ch.addFilter(RedactFilter())
    root.addHandler(ch)

    # 第三方库降噪：httpx 每请求一条 INFO 会改变控制台现状，且重试已由
    # translate 侧自行打点——压到 WARNING，只留异常。httpx2/httpcore2 是本机
    # 同装的 httpx 分支（faster-whisper 拉模型在用），同样处理（v02-3 实测发现）
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True


def uvicorn_log_config() -> dict:
    """给 uvicorn.run(log_config=...) 的配置：清空 uvicorn 自带 handler、
    propagate 到 root（同一落盘 + 同一控制台）；INFO 行维持现状被 WARNING
    抑制（与原 uvicorn.run(log_level='warning') 的控制台表现一致）。
    disable_existing_loggers 必须显式 False，否则 dictConfig 会把已创建的
    main/capture/translate/asr 模块 logger 一并禁用。"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {},
        "handlers": {},
        "loggers": {
            "uvicorn": {"level": "WARNING"},
            "uvicorn.error": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
        },
    }
