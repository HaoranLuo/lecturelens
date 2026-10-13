# -*- coding: utf-8 -*-
"""branding.py — 品牌视觉单一事实源（p1-3：应用图标 + 关于页）。

职责：
- APP_VERSION：应用版本号唯一来源（/api/status 的 version 键、关于页消费）；
- draw_app_icon：品牌应用图标的矢量绘制（深蓝黑圆角底 + 金色「镜头 + 字幕条」
  意象，色值一律取 docs/design-spec.md §2 令牌），窗口/托盘/打包共用同一设计；
- write_ico：多尺寸 .ico 合成（16..256 逐尺寸矢量重绘而非缩放糊边，经 PIL
  IcoImagePlugin 的 append_images 精确按尺寸取用，IcoImagePlugin.py:65-72）。

直接运行生成产物：python app/branding.py → assets/icon.ico（7 尺寸）+ assets/icon_<N>.png
尺寸族 16/24/32/48/64/128/256（p3-4 起；旧版只出 icon_256.png 一张）。
打包消费：scripts/build_exe.py 的 --icon assets/icon.ico（exe/任务栏图标）；
托盘消费：desktop.py 的 _tray_image → draw_app_icon(64, state)。
"""
from pathlib import Path

# 版本口径（推断）：升级计划 v0.5（条目 17/18）已落地，P1「壳与颜值」进行中；
# README 无权威版本号（仅有「已知限制（v0.1）」标题）——0.6.0-dev 为下一阶段语义。
APP_VERSION = "0.6.0-dev"

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)  # Windows .ico 惯例尺寸族
#: PNG 尺寸族（p3-4）：16/24 供托盘与小控件、32/48 供列表与任务栏、64/128 供
#: 文档与商店、256 供高 DPI 与大图（p1-3 已在用的那张）。
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)

# 托盘状态灯色值（design-spec §2 --ok/--warn/--err；desktop.py 消费同一来源）
STATE_COLORS = {
    "ok": (95, 208, 138),      # --ok   #5FD08A 运行绿
    "paused": (232, 180, 74),  # --warn #E8B44A 暂停黄
    "error": (255, 107, 107),  # --err  #FF6B6B 错误红
}

# —— 64 基准坐标系里的设计令牌（design-spec.md §2）——
_TEXT_1 = (232, 238, 245)       # --text-1 #E8EEF5 主文字（仅用于推导镜头高光）
_GOLD = (255, 217, 122, 255)    # --gold   #FFD97A 品牌金（镜头环/字幕条）
_SURFACE = (18, 22, 29, 255)    # --surface   #12161D 底板
_SURFACE_2 = (23, 28, 36, 255)  # --surface-2 #171C24 镜头玻璃
_BORDER = (35, 43, 53, 255)     # --border  #232B35 描边
_SS = 4                         # 超采样倍率：PIL 无原生抗锯齿，4x 绘制后缩小


def _mix(a, b, t):
    """线性混色（t=0 → a，t=1 → b）。用于从令牌推导衍生色，而不是新发明色值。"""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# 镜头高光：--gold 向 --text-1 偏 45% 的暖白。**唯一非令牌直接取值**——设计令牌表
# 管的是界面，图标里的「玻璃反光」必须比金色更亮才读得出是反光；此值仍由两个令牌
# 推导得出（不是凭空挑的颜色），改令牌它会跟着变。
_GLARE = (*_mix(_GOLD[:3], _TEXT_1, 0.45), 235)
# 光圈叶片暗示（仅大尺寸）：金色压到低透明度，在玻璃上留一圈若有若无的环
_BLADE = (*_GOLD[:3], 64)

# —— 64 空间坐标（p3-4 精修后的构图）——
# 纵向版式：镜头在上、**两行字幕条**在下（长条 = 英文行，短条 = 中文行）。
# 旧版只有一根横条，整体读作「眼睛 + 嘴」；两根长短条才读得出「字幕」。
_BASE_BOX = (3, 3, 61, 61)          # 底板（r14 圆角方底）
_LENS_RING_OUT = (19, 10, 45, 36)   # 镜头外圈（圆心 (32,23) r13，金）
_LENS_RING_IN = (23, 14, 41, 32)    # 玻璃（r9，surface-2）→ 环宽 4
_LENS_BLADE = (26, 17, 38, 29)      # 光圈叶片环（r6，低透明金；仅大尺寸）
_LENS_DOT = (29, 20, 36, 27)        # 光圈点（r3.5，金）——比旧版小半点，弱化「瞳孔」观感
_LENS_GLARE_BOX = (21, 12, 43, 34)  # 高光弧所在环带（r11 中径；仅大尺寸）
_GLARE_FROM, _GLARE_TO = 186, 248   # 左上象限（PIL 角度：0=3 点钟，顺时针增大）
_BAR1 = (11, 42, 41, 48)            # 字幕条①：长条（英文字幕行）
_BAR2 = (11, 51, 29, 56)            # 字幕条②：短条（中文字幕行）
_BAR1_SOLO = (12, 43, 40, 50)       # 小尺寸单条变体（<24px：两条会糊成灰渣）
_LIGHT_HALO = (40, 40, 60, 60)      # 状态灯光晕 r10（同色 8% α≈20）
_LIGHT_DOT = (44, 44, 56, 56)       # 状态灯实心 r6，圆心 (50,50)

#: 细节阈值：小于此尺寸不再画高光/叶片环（亚像素细节只会把图形搅浑）
_DETAIL_MIN = 32
#: 小于此尺寸只用一根字幕条（两根条在 16/24px 下会糊成一条灰带）
_TWO_BAR_MIN = 24


def draw_app_icon(size: int = 64, state: str | None = None):
    """绘制品牌应用图标，返回 PIL RGBA Image（size × size）。

    构图（p3-4 精修）：深蓝黑圆角方底 + 金色镜头（环 + 光圈点 + 高光）+ 两行金色
    字幕条。色值一律取 design-spec §2 令牌（高光由令牌混色推导，见 _GLARE）。

    **按尺寸分级绘制**（不是缩放糊边，这正是「逐尺寸矢量重绘」的意义）：
    - ≥32px：加镜头高光弧与光圈叶片环（大尺寸需要细节才像镜头）；
    - ≥24px：画两根字幕条（长 = 英文行，短 = 中文行）；
    - <24px：只画一根加粗字幕条、不要高光——16px 下两根细条与高光弧都会糊成噪点。

    state：None = 应用图标（无状态灯，供 ico/打包/文档）；'ok'/'paused'/'error'
    = 托盘图标，右下加状态灯（design-spec §六「8px 圆点 + 同色 8% 光晕」）。
    **状态灯的几何与色值不得改动**：desktop.py --check 对 (50,50) 像素做精确断言。
    4x 超采样后 LANCZOS 缩小（PIL 原生无抗锯齿）。"""
    from PIL import Image, ImageDraw
    k = (size * _SS) / 64.0  # 64 空间 → 超采样画布的缩放系数

    def sc(box):
        return tuple(round(v * k) for v in box)

    detailed = size >= _DETAIL_MIN
    two_bars = size >= _TWO_BAR_MIN

    img = Image.new("RGBA", (size * _SS, size * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(sc(_BASE_BOX), radius=round(14 * k),
                        fill=_SURFACE, outline=_BORDER, width=max(1, round(2 * k)))
    d.ellipse(sc(_LENS_RING_OUT), fill=_GOLD)                      # 镜头外圈（实心金）
    d.ellipse(sc(_LENS_RING_IN), fill=_SURFACE_2)                  # 玻璃（比底板亮一档）
    if detailed:
        # 叶片环：低透明金描边，给玻璃一点纵深（大尺寸才看得见）
        d.ellipse(sc(_LENS_BLADE), outline=_BLADE, width=max(1, round(1.2 * k)))
    d.ellipse(sc(_LENS_DOT), fill=_GOLD)                           # 光圈点
    if detailed:
        # 高光弧：沿环带（中径 r11）画左上象限的一段圆弧——这是让圆形读作
        # 「镜头」而不是「靶心/眼睛」的关键一笔
        d.arc(sc(_LENS_GLARE_BOX), start=_GLARE_FROM, end=_GLARE_TO,
              fill=_GLARE, width=max(1, round(2.2 * k)))
    if two_bars:   # 含 detailed 档（_DETAIL_MIN 32 > _TWO_BAR_MIN 24，故此分支已覆盖）
        d.rounded_rectangle(sc(_BAR1), radius=round(3 * k), fill=_GOLD)
        d.rounded_rectangle(sc(_BAR2), radius=round(2.5 * k), fill=_GOLD)
    else:
        d.rounded_rectangle(sc(_BAR1_SOLO), radius=round(3.5 * k), fill=_GOLD)
    if state is not None:
        rgb = STATE_COLORS[state]
        d.ellipse(sc(_LIGHT_HALO), fill=(*rgb, 20))                # 同色 8% 光晕
        d.ellipse(sc(_LIGHT_DOT), fill=(*rgb, 255))                # 实心灯
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path) -> Path:
    """生成多尺寸 .ico：ICON_SIZES 逐尺寸矢量重绘（小尺寸不糊），经
    IcoImagePlugin._save 的 append_images 按精确尺寸取帧
    （IcoImagePlugin.py:65-72 实源核实）。返回写入路径。"""
    from PIL import Image  # noqa: F401  确保注册 ICO 插件
    base = draw_app_icon(ICON_SIZES[-1])
    smaller = [draw_app_icon(s) for s in ICON_SIZES[:-1]]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base.save(path, format="ICO",
              sizes=[(s, s) for s in ICON_SIZES],
              append_images=smaller)
    return path


def write_pngs(out_dir, sizes=None) -> list:
    """生成 PNG 尺寸族（p3-4：ask 要求「多尺寸 ico/png」——旧版只出一张 256）。
    每张都走 draw_app_icon 逐尺寸重绘（不是把 256 缩下来），故 16/32px 也清晰。
    文件名 icon_<size>.png（icon_256.png 沿用 p1-3 既有命名，不破坏引用）。
    返回写入的路径列表。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for s in (sizes or PNG_SIZES):
        p = out / f"icon_{s}.png"
        draw_app_icon(s).save(p)
        written.append(p)
    return written


if __name__ == "__main__":
    assets = Path(__file__).resolve().parent.parent / "assets"
    ico_path = write_ico(assets / "icon.ico")
    pngs = write_pngs(assets)
    print(f"[branding] {ico_path}（sizes={ICON_SIZES}）")
    print(f"[branding] PNG 尺寸族 → {', '.join(p.name for p in pngs)}")
    print(f"[branding] version={APP_VERSION}  共 {1 + len(pngs)} 个产物")
