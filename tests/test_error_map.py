# -*- coding: utf-8 -*-
"""tests/test_error_map.py — 前端错误反馈面静态回归（升级计划条目 8 / §2.2-8）。

如实说明（§2.1 纪律）：浏览器行为不进自动化——本文件对 index.html 做**结构断言**：
解析 ERROR_MAP（保持 JSON 兼容的数组）验证关键词覆盖与中文文案，并锁定三条布线：
① config ok:false → 回滚下拉 + toast 提示；② modelBusy finally 兜底复位；
③ 状态轮询统一错误横幅 + 「切小模型重试」按钮。
映射表命中/兜底/回滚的**行为验证**已在真实浏览器（chrome-devtools evaluate_script
对真实服务页面）执行过，记录于实施报告；后续回归以本文件 + 手动清单承担。"""
import json
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent
        / "app" / "web" / "index.html").read_text(encoding="utf-8")


def _extract_error_map() -> list[list[str]]:
    """从 index.html 提取 ERROR_MAP 并按 JSON 解析（数组内禁注释/单引号，保持可解析）。"""
    m = re.search(r"const ERROR_MAP = \[(.*?)\];", HTML, re.S)
    assert m, "index.html 缺少 ERROR_MAP 定义"
    return json.loads(f"[{m.group(1)}]")


def test_error_map_covers_plan_keywords():
    """条目8⑤ 点名的关键词：cuda/cublas/下载失败/显存（+ cudnn/portaudio 麦克风）。"""
    entries = dict(_extract_error_map())
    for key in ("cuda", "cublas", "cudnn", "memory", "download", "portaudio"):
        assert key in entries, f"ERROR_MAP 缺少关键词 {key}"


def test_error_map_specific_before_generic():
    """cublas/cudnn 必须排在 cuda 之前：错误串常同时含多个词，specific 优先命中。"""
    keys = [k for k, _ in _extract_error_map()]
    assert keys.index("cublas") < keys.index("cuda")
    assert keys.index("cudnn") < keys.index("cuda")


def test_error_map_messages_chinese_nonempty():
    for key, msg in _extract_error_map():
        assert msg.strip(), f"关键词 {key} 文案为空"
        assert any("\u4e00" <= c <= "\u9fff" for c in msg), f"{key} 文案应含中文: {msg}"


def test_error_map_fallback_exists():
    """兜底：英文堆栈截断展示不吞错；后端中文错误原样透传。"""
    assert "ERROR_FALLBACK_PREFIX" in HTML and "服务错误：" in HTML
    # 字符类 /[\u4e00-\u9fff]/.test(s) —— 中文错误直接返回原文（不走兜底前缀）
    assert "/[\\u4e00-\\u9fff]/.test(s)) return s;" in HTML


def test_config_fail_rolls_back_and_prompts():
    """ask 核心：/api/config ok:false → syncModel() 回滚下拉 + showToast 提示。"""
    seg = re.search(r"if \(!r \|\| r\.ok === false\)(.*?)return;", HTML, re.S)
    assert seg, "onchange 缺少 ok:false 分支"
    branch = seg.group(1)
    assert "syncModel()" in branch, "ok:false 分支必须回滚下拉"
    assert "showToast(" in branch and "friendlyError(" in branch, \
        "ok:false 分支必须经 friendlyError 后 toast 提示"


def test_model_busy_finally_reset():
    """条目8①：onchange 用 accepted + finally 保证失败/异常路径必复位（不卡暂停键）。"""
    assert "let accepted = false;" in HTML
    assert "if (!accepted) modelBusy = false;" in HTML


def test_status_errors_unified_banner():
    """统一展示：轮询按 asr > mic > translate 优先级驱动横幅，含切小模型重试按钮。"""
    assert "showErrorBanner(friendlyError(asrTxt), true)" in HTML
    assert "showErrorBanner(friendlyError(mic), false)" in HTML
    assert "showErrorBanner(friendlyError(status_.translate), false)" in HTML
    assert "hideErrorBanner()" in HTML
    assert 'id="btnRetrySmall"' in HTML
    assert re.search(r"btnRetrySmall.*?model.*?base.*?device.*?cpu", HTML, re.S), \
        "切小模型重试应 POST CPU base"


def test_caption_styles_untouched():
    """保护清单第3条抽查：字幕排版参数（金黄 #FFD97A / 900px 限宽 / 字号 localStorage）未动。"""
    assert "#FFD97A" in HTML
    assert "max-width:900px" in HTML
    assert "ll_font" in HTML
