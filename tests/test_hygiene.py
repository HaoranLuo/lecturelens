# -*- coding: utf-8 -*-
"""tests/test_hygiene.py — 工程卫生断言（升级计划条目 10 / §2.2-10，本地跑）。

锁定四件事：① 根目录无 nul 残留；② .gitignore 覆盖 config.json 与 nul；
③ config.example.json 键集合 == ALLOWED_CONFIG_KEYS（随常量演进不腐烂，
且示例值必须能通过白名单校验）；④ translate.py 无硬编码绝对路径。"""
import json
import re
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parent.parent


def test_no_nul_file():
    # Windows 坑：DOS 保留设备名解析让 Path("nul").exists() 恒为 True——
    # 必须查目录枚举（真实残留文件才会出现在 iterdir 里）
    names = {p.name for p in ROOT.iterdir()}
    assert "nul" not in names, "根目录不应有 nul 残留文件（条目10①）"


def test_gitignore_covers_config_and_nul():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^config\.json$", gi, re.M), ".gitignore 应包含 config.json（条目10②）"
    assert re.search(r"^nul$", gi, re.M), ".gitignore 应包含 nul（条目10②）"


def test_config_example_matches_allowed_keys():
    """config.example.json 键集合 == ALLOWED_CONFIG_KEYS，且示例值全过白名单（条目10③）。"""
    p = ROOT / "config.example.json"
    assert p.is_file(), "缺少 config.example.json（首次部署模板）"
    example = json.loads(p.read_text(encoding="utf-8"))
    assert set(example.keys()) == set(main.ALLOWED_CONFIG_KEYS), \
        f"example 键集合偏离白名单: {sorted(example)} vs {sorted(main.ALLOWED_CONFIG_KEYS)}"
    for k, v in example.items():
        if v is None:
            continue
        if k in main.CONFIG_NUM_RANGES:
            # p2-5：数值键不在 CONFIG_VALUE_CHOICES（那是枚举表），改查区间
            assert main._config_number_problem(k, v) is None, \
                f"example 的 {k}={v!r} 不满足数值区间/类型校验"
            continue
        if k in main.hotkeys_mod.HOTKEY_CONFIG_KEYS:
            # p3-1：热键组合串同样不是枚举表（任意修饰键×主键的组合），改查键名
            # 白名单——与 /api/config 走同一个 _hotkey_problem，示例值必须过同一道闸
            assert main._hotkey_problem(k, v) is None, \
                f"example 的 {k}={v!r} 不满足热键键名白名单校验"
            continue
        if k == "tags_presets":
            # B3：常用标签是**列表**键，同样不在 CONFIG_VALUE_CHOICES（枚举表放不下列表，
            # 下面那行 `v in CONFIG_VALUE_CHOICES[k]` 会对它 KeyError）——查专项校验，
            # 与 POST /api/config 同一个函数，示例值必须过同一道闸。
            assert main._tags_presets_problem(v) is None, \
                f"example 的 {k}={v!r} 不满足常用标签校验"
            continue
        assert v in main.CONFIG_VALUE_CHOICES[k], f"example 的 {k}={v!r} 不在白名单内"


def test_translate_no_hardcoded_abs_path():
    src = (ROOT / "app" / "translate.py").read_text(encoding="utf-8")
    assert "D:\\projects" not in src and "D:/projects" not in src, \
        "translate.py 不得硬编码绝对路径（条目10⑤；.env 路径由调用方传入）"
