# -*- coding: utf-8 -*-
"""hfenv.py — D2（分发工程）：HuggingFace 环境变量的**唯一落点**。

为什么单独一个模块：`huggingface_hub` 在 **import 时**就把 `HF_HUB_OFFLINE` /
`HF_ENDPOINT` 读成模块级常量（constants.py），之后再改 os.environ 不再影响它。
而 config.json 的 `hf_mirror` 键要真的起作用，就必须在 `import faster_whisper`
（→ huggingface_hub）**之前**落定。本模块**只依赖标准库**（绝不 import
huggingface_hub / faster_whisper），故可安全地在 main.py 的第三方导入之前调用。

两个环境变量的口径（D2 = docs/notes-batch.md D 组「hf-mirror 预设开关」）：
- `HF_HUB_OFFLINE`：**不再由 frozen 自动置位**。旧行为（条目20）在 frozen 时无条件
  置 1，等于把打包版的 `/api/models/download` 钉死在「只查本地缓存」分支上（必然
  失败）。现在：随包模型以**本地路径**加载、本就不需要联网；其余档位允许在线下载。
  用户/开发显式设 `HF_HUB_OFFLINE=1` 仍然被尊重 = 「显式离线开关」（main.hf_offline
  读它）。本模块**从不写**这个变量。
- `HF_ENDPOINT`：config.hf_mirror=true → 指向**唯一预设镜像**（MIRROR_ENDPOINT，
  国内网络加速）；false → 一律不动（保持官方默认，或系统环境里已有的值）。
  镜像地址是**源码里的常量**：本模块不读配置/请求里的任何 endpoint 字符串，不存在
  「按用户输入去请求任意 URL」的面（Mimosa 安全口径：仅允许 http/https、拒绝任意
  目标——这里连用户可控的 URL 参数都没有）。
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("hfenv")

#: 唯一允许的镜像 endpoint（D 组口径：只认预设镜像，拒绝任意 URL）
MIRROR_ENDPOINT = "https://hf-mirror.com"
#: HuggingFace 官方默认（仅用于日志/展示对比，不主动写进 env）
OFFICIAL_ENDPOINT = "https://huggingface.co"
OFFLINE_ENV = "HF_HUB_OFFLINE"
ENDPOINT_ENV = "HF_ENDPOINT"


def mirror_requested(config_path) -> bool:
    """config.json 里 `hf_mirror` 是否为**真布尔 true**。

    缺键 / 文件不存在 / JSON 坏 / 手改成 "true"、1 这类值 → 一律 False。只认
    `is True`：API 侧已有白名单枚举校验，这里是「有人直接改文件」的最后一道——
    镜像开关是**行为开关**，宁可关着也不接受模糊取值。
    """
    try:
        p = Path(config_path)
        if not p.is_file():
            return False
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[hfenv] 读 config.json 失败（按未开镜像处理）: {e}")
        return False
    return isinstance(raw, dict) and raw.get("hf_mirror") is True


def apply(config_path) -> dict:
    """按 config.json 把 HF 相关环境变量落定（**必须在 HF 导入前调用**）。

    返回实际生效状态 {"mirror": bool, "endpoint": str|None, "offline": bool}：
    调用点（main.py）把它存成模块级常量，等日志装配好之后再如实打一行启动信息。
    """
    mirror = mirror_requested(config_path)
    if mirror:
        os.environ[ENDPOINT_ENV] = MIRROR_ENDPOINT
    endpoint = os.environ.get(ENDPOINT_ENV) or None
    return {"mirror": mirror, "endpoint": endpoint,
            "offline": os.environ.get(OFFLINE_ENV) == "1"}
