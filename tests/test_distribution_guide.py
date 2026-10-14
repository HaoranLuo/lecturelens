# -*- coding: utf-8 -*-
"""tests/test_distribution_guide.py — D5 分发指南的**事实一致性**回归。

指南是给"未来的自己"看的操作手册，它最容易坏的方式不是写错字，而是**悄悄说谎**：
代码里加了自动更新、仓库改成公开、安装包换了个名字/哈希，而文档还停在旧口径
——朋友照着做就会踩空。所以本文件测的不是文笔，是**文档与事实的对齐**：

  - 结构：三条渠道对比 / 发版 SOP / 首次使用 / 可复制文案 / FAQ 五块都在；
  - 诚实边界：私有可见性、未签名、无自动更新 三条必须如实写明（需求原话
    「只写真实能力……不要承诺不存在的能力」）；
  - 代码事实对齐：**仓库里确实没有任何自动更新机制**（否则本测试红，提醒去改文档）；
  - 产物事实对齐：文档里贴的安装包文件名与 SHA256 必须与 dist/installer 下的真产物
    一致（发版时改一处忘另一处 = 朋友校验哈希时对不上，会以为包被篡改）。

产物不存在时（dist/ 被清过）后两条**跳过**而不是失败——文档本身仍应可读。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "distribution-guide.md"


def _text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_guide_exists_and_is_utf8_without_bom():
    """规则③：新文件 UTF-8；且不带 BOM（仓库一律以 encoding="utf-8" 读文件）。"""
    assert GUIDE.is_file(), f"缺 {GUIDE.relative_to(ROOT)}"
    raw = GUIDE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "分发指南不得带 BOM"
    raw.decode("utf-8")          # 解不开即失败


def test_guide_covers_the_five_required_parts():
    """需求点名的五块内容都得有（缺哪块都是没写完）。"""
    t = _text()
    # ① 三条渠道对比
    for ch in ("GitHub Release", "网盘", "直接传文件"):
        assert ch in t, f"渠道对比缺「{ch}」"
    # ② 发新版 SOP（含 gh release create 与 SHA256 步骤）
    assert "gh release create" in t, "SOP 缺 gh release create"
    assert "--notes-file" in t and "SHA256" in t
    # ③ 朋友首次使用指引
    for step in ("SmartScreen", "仍要运行", "防火墙", "向导", "扫码"):
        assert step in t, f"首次使用指引缺「{step}」"
    # ④ 可直接复制发给朋友的文案（用代码块承载）
    assert "```text" in t, "缺可复制的朋友文案块"
    # ⑤ 常见问题
    assert "常见问题" in t or "FAQ" in t
    for q in ("装不上", "没声音", "手机扫码连不上", "怎么更新"):
        assert q in t, f"FAQ 缺「{q}」问题"


def test_guide_states_the_honest_limits():
    """需求原话：诚实边界**如实写明，不要承诺**。2026-10-14 起仓库公开——
    边界口径 = 公开且保留所有权利（未授予开源许可）、未签名、无自动更新。"""
    t = _text()
    assert "已公开" in t, "未写明仓库已公开（2026-10-14 新口径）"
    assert "保留所有权利" in t, "未写明「保留所有权利」（未授予开源许可）"
    assert "代码签名" in t, "未写明没有代码签名"
    assert re.search(r"没有自动更新|无自动更新|没有\s*自动更新", t), "未写明没有自动更新"
    # 2 GiB 单附件上限是渠道对比的关键事实，不能漏
    assert "2 GiB" in t or "2GiB" in t, "未写明 GitHub 单附件 2 GiB 上限"


def test_no_auto_update_mechanism_exists_in_code():
    """**文档↔代码事实对齐**：指南说「没有自动更新」，代码里就必须真没有。

    哪天有人加了自动更新，本测试变红——那是提醒去改文档，
    而不是让文档继续对朋友说谎。
    """
    hits = []
    for p in sorted((ROOT / "app").rglob("*.py")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for kw in ("auto_update", "autoUpdate", "check_for_update",
                   "checkForUpdate", "updater_url", "自动更新"):
            if kw in txt:
                hits.append(f"{p.relative_to(ROOT)}:{kw}")
    assert not hits, ("代码里出现了自动更新机制，请同步修订 docs/distribution-guide.md"
                      f"（它现在写着「没有自动更新」）：{hits}")


def test_guide_reflects_public_repo_policy():
    """口径纪律（2026-10-14 用户拍板变更）：仓库已转公开（便于朋友/AI 从 GitHub
    一键安装），但**保留所有权利**——指南必须写清新口径，且不再出现私有仓库时代
    的 collaborator 邀请流程。"""
    t = _text()
    assert "保留所有权利" in t, "指南未写 2026-10-14 新口径「保留所有权利」"
    assert "已公开" in t, "指南未反映仓库已公开"
    assert "gh api -X PUT repos" not in t, "不应再写 collaborator 邀请命令（仓库已公开）"


def _built_installer():
    cands = sorted((ROOT / "dist" / "installer").glob("LectureLens-*-setup.exe"),
                   key=lambda q: q.stat().st_mtime)
    return cands[-1] if cands else None


def test_guide_facts_match_the_built_installer():
    """文档里贴的安装包名 / SHA256 / 体积必须与真产物一致（发版防漂移）。"""
    setup = _built_installer()
    if setup is None:
        pytest.skip("dist/installer 下暂无安装包（未构建），跳过产物对齐检查")
    t = _text()
    assert setup.name in t, f"文档未提到当前安装包名 {setup.name}"

    sha = setup.with_name(setup.name + ".sha256")
    if sha.is_file():
        want = sha.read_text(encoding="utf-8").split()[0].lower()
        assert want in t.lower(), (
            f"文档里的 SHA256 与 {sha.name} 不一致（发版时只改了一处）")

    size = setup.stat().st_size
    assert f"{size:,}" in t or f"{size / 2**30:.3f} GiB" in t, (
        f"文档里的体积与真产物不符（实际 {size:,} 字节 / {size / 2**30:.3f} GiB）")


def test_guide_stays_under_the_github_attachment_limit_advice():
    """渠道建议不能自相矛盾：既要提醒 2 GiB 上限，也要给出超限预案。"""
    t = _text()
    assert "分卷" in t or "7z" in t, "顶破 2 GiB 时没给预案（分卷 / 只走网盘）"
