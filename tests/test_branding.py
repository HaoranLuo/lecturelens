# -*- coding: utf-8 -*-
"""tests/test_branding.py — p3-4 品牌件：图标绘制 / 尺寸分级 / 多尺寸产物。

锁三件事：
① **状态灯几何与色值不许漂移**：desktop.py --check 对 64px 图标 (50,50) 像素做精确
   断言（托盘三态靠它），本文件把同一契约钉在单元测试里——改图标时先在这里现形，
   而不是等门禁或托盘变色才发现；
② **尺寸分级真的生效**：≥24px 画两根字幕条、<24px 只画一根加粗；≥32px 才有镜头
   高光。这不是「缩放糊边」的细节，而是 ask「多尺寸 ico/png」的实质——分级失效
   意味着 16px 糊成灰渣；
③ .ico 里确实是 7 个不同尺寸的**真帧**（不是一张 256 缩出来的），PNG 尺寸族齐全。
"""
import sys
from pathlib import Path

import pytest
from PIL import Image

import branding

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def _is_gold(px, tol=40):
    r, g, b = px[:3]
    return abs(r - 255) <= tol and abs(g - 217) <= tol and abs(b - 122) <= tol


def _lower_half_gold_bands(img):
    """下半区里「含金色的连续行」段数 = 画了几根字幕条。

    注意必须按**行带**统计而不是单行游程：两根字幕条位于不同行，任何单独一行
    都只会横穿其中一根（早期版本按单行游程写，恒得 1，是个假测试）。"""
    y0 = round(42 * img.height / 64)
    bands, prev = 0, False
    for y in range(y0, img.height):
        has = any(_is_gold(img.getpixel((x, y))) for x in range(img.width))
        if has and not prev:
            bands += 1
        prev = has
    return bands


def _max_visible_blue(img):
    """可见像素（alpha≥128）的最大蓝通道值。

    这是「有没有画高光」的判据：金色 b=122，高光（金向 --text-1 偏 45%）b=177，
    两者在蓝通道上分得很开；而红/绿通道两者太近，且 LANCZOS 的过冲会让个别
    像素比原色更亮，用它当判据会误判。"""
    return max(img.getpixel((x, y))[2]
               for y in range(img.height) for x in range(img.width)
               if img.getpixel((x, y))[3] >= 128)


# —— ① 状态灯契约（与 desktop.py --check 同口径） ——

@pytest.mark.parametrize("state", sorted(branding.STATE_COLORS))
def test_status_light_pixel_exact(state):
    """托盘三态的 (50,50) 必须是该状态的精确色值——desktop --check 的断言锚点。"""
    img = branding.draw_app_icon(64, state)
    assert img.size == (64, 64)
    assert img.getpixel((50, 50))[:3] == branding.STATE_COLORS[state]


def test_no_status_light_without_state():
    """不带 state = 应用图标：右下角是底板色，不能混进状态灯。"""
    img = branding.draw_app_icon(64)
    assert img.getpixel((50, 50))[:3] == branding._SURFACE[:3]


def test_state_colors_match_design_tokens():
    """状态灯色值 = design-spec §2 的 --ok/--warn/--err（改令牌必须同时改这里）。"""
    assert branding.STATE_COLORS == {"ok": (95, 208, 138),
                                     "paused": (232, 180, 74),
                                     "error": (255, 107, 107)}


# —— ② 尺寸分级 ——

def test_two_caption_bars_at_large_sizes():
    """>=24px：下半区有**两段**含金色的行带（长 = 英文字幕行，短 = 中文字幕行）。
    单行游程恒为 1（两根条在不同行），故必须按行带统计。"""
    for size in (24, 32, 48, 64, 128, 256):
        bands = _lower_half_gold_bands(branding.draw_app_icon(size))
        assert bands == 2, f"{size}px 应有两根字幕条，实测 {bands} 段金色行带"


def test_single_bar_below_24px():
    """<24px：只画一根（两根细条在 16px 下会糊成一条灰带）。"""
    bands = _lower_half_gold_bands(branding.draw_app_icon(16))
    assert bands == 1, f"16px 应只有一根字幕条，实测 {bands} 段金色行带"


def test_glare_only_at_detailed_sizes():
    """高光弧只在 >=32px 出现（判据：可见像素的蓝通道上限）。

    金色 b=122、高光 b=177；实测 16/24px 上限 135（无高光，135 是边缘抗锯齿），
    32px 起 167~193（高光在）。这正是「按尺寸分级绘制」该有的形状。"""
    assert _max_visible_blue(branding.draw_app_icon(64)) > 150, "64px 应有镜头高光"
    assert _max_visible_blue(branding.draw_app_icon(32)) > 150, "32px 起进入细节档"
    assert _max_visible_blue(branding.draw_app_icon(24)) <= 150, "24px 不该有高光"
    assert _max_visible_blue(branding.draw_app_icon(16)) <= 150, \
        "16px 不该有高光（亚像素细节只会把图形搅浑）"


def test_glare_color_is_derived_from_tokens():
    """高光由 --gold 与 --text-1 混色推导（不是凭空挑的颜色，改令牌会跟着变）。"""
    assert branding._GLARE[:3] == branding._mix(branding._GOLD[:3],
                                               branding._TEXT_1, 0.45)
    assert sum(branding._GLARE[:3]) > sum(branding._GOLD[:3])


def test_all_icon_sizes_render_square_rgba():
    for size in branding.ICON_SIZES:
        img = branding.draw_app_icon(size)
        assert img.size == (size, size) and img.mode == "RGBA"


# —— ③ 多尺寸产物 ——

def test_write_ico_has_real_frames(tmp_path):
    """ico 内 7 个尺寸都是真帧（逐尺寸矢量重绘），不是一张 256 缩小糊边。"""
    p = branding.write_ico(tmp_path / "icon.ico")
    assert p.is_file() and p.stat().st_size > 1000
    im = Image.open(p)
    assert sorted(im.info.get("sizes", [])) == [(s, s) for s in branding.ICON_SIZES]
    for s in branding.ICON_SIZES:
        im.size = (s, s)
        im.load()
        assert im.size == (s, s), f"ico 内 {s}px 帧尺寸不对"
        assert im.mode == "RGBA"


def test_write_pngs_full_family(tmp_path):
    written = branding.write_pngs(tmp_path)
    names = sorted(p.name for p in written)
    assert names == sorted(f"icon_{s}.png" for s in branding.PNG_SIZES)
    for p in written:
        with Image.open(p) as im:
            want = int(p.stem.split("_")[1])
            assert im.size == (want, want), f"{p.name} 尺寸不对"
            assert im.mode == "RGBA"


def test_png_family_includes_p1_3_name(tmp_path):
    """icon_256.png 沿用 p1-3 既有命名——换名会静默打断已有引用。"""
    names = [p.name for p in branding.write_pngs(tmp_path)]
    assert "icon_256.png" in names


def test_repo_assets_are_current():
    """仓库 assets/ 里的产物与当前代码一致（改了图标忘记重跑生成 = 打包进旧图）。"""
    root = Path(__file__).resolve().parent.parent
    ico = root / "assets" / "icon.ico"
    assert ico.is_file(), "缺少 assets/icon.ico（python app/branding.py 生成）"
    assert sorted(Image.open(ico).info.get("sizes", [])) == \
        [(s, s) for s in branding.ICON_SIZES], \
        "assets/icon.ico 的尺寸族与 branding.ICON_SIZES 不一致——请重跑 python app/branding.py"
    for s in branding.PNG_SIZES:
        p = root / "assets" / f"icon_{s}.png"
        assert p.is_file(), f"缺少 assets/{p.name}——请重跑 python app/branding.py"
        with Image.open(p) as im:
            assert im.size == (s, s)
