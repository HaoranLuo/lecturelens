# -*- coding: utf-8 -*-
"""tests/test_config_isolation.py — 条目 2 验收（docs/upgrade-plan.md §2.2-2）：

「触发 save_config 的测试不写真实 config.json」。
1) 直接调 save_config（CONFIG_PATH 已被 conftest 重定向）→ 临时文件落盘、
   仓库文件未动；
2) 会话级守卫 repo_config_guard（conftest 定义）在 teardown 再验一次仓库
   config.json 内容 + mtime_ns 未变——覆盖任何测试顺序下的累积效果。"""
import json
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parent.parent


def test_save_config_redirects_to_tmp():
    cfg = dict(main.DEFAULT_CONFIG, vad="energy")
    main.save_config(cfg)
    assert main.CONFIG_PATH.is_file()
    assert main.CONFIG_PATH != ROOT / "config.json"
    assert json.loads(main.CONFIG_PATH.read_text(encoding="utf-8"))["vad"] == "energy"


def test_repo_config_json_untouched(repo_config_guard):
    """仓库 config.json（若存在）必须仍是合法 JSON 且未被本会话改写。"""
    p = ROOT / "config.json"
    if p.is_file():
        json.loads(p.read_text(encoding="utf-8"))
    repo_config_guard()
