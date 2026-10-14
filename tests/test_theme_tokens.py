# -*- coding: utf-8 -*-
"""tests/test_theme_tokens.py — B15：深浅主题（离线秒级，令牌/结构层）。

交付面（docs/notes-batch.md B15 行 + 本条的 ask）：
- 三页各自一套令牌（`app/web/index.html` / `privacy.html` / `float.html`）；**深色 = 现状值
  逐字保留**（默认零变化），浅色 = 白底蓝调（DeepSeek 风）；
- 切换机制：localStorage 键 `ll_theme` ∈ dark | light | auto（默认 dark），
  落成 `<html data-theme="…">`；auto 走 `matchMedia('(prefers-color-scheme: dark)')`
  并**监听系统切换**；脚本在 `<head>` 里同步执行（防首帧闪烁）；
- **悬浮字幕条钉死深色**（`float.html` 不响应主题，其 :root 固定深色）；
- 设置页 🎨外观 有「主题」三选，复用 `.tagChip.on` 选中态（gold-dim/gold，§六）。

本文件只做**静态解析**断言（本仓没有 JS 测试运行器，与其余前端条目同一口径）：
真浏览器里的点选/记忆/系统切换行为在实施报告与真机手测里取证。三条硬口径：
① 每页深/浅两套令牌**键集合完全一致**（只加在深色块里的新令牌会在浅色下静默继承
   深色值 = 看不见的回归，故用键集合锁死）；
② `:root`/浅色块**之外零裸色值**（hex 与 rgb()/rgba()/hsl()；CSS 注释里的说明文字
   不算——注释不设色）。float 的钉死深色值全部收进它自己的 :root 令牌；
③ 深色令牌值**逐字**钉住（`DARK_INDEX` 等字面量表）：默认主题不许被这次改动碰到。

⚠️ float.html 是 ②① 的**明示例外**（ask ④「悬浮条钉死深色」）：它没有浅色块，
所以①对它不适用；②照旧适用（--bar-bg 令牌）。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"

PAGES = ("index.html", "privacy.html", "float.html")
#: 会读 ll_theme、有浅色块的页（float 钉死深色，见模块 docstring 的例外说明）
THEMED_PAGES = ("index.html", "privacy.html")


def style_of(name: str) -> str:
    """取 <style>…</style> 原文（含注释）。"""
    return (WEB / name).read_text(encoding="utf-8").split("<style>", 1)[1].split("</style>", 1)[0]


def head_scripts(name: str) -> str:
    """取 </head> 之前的全部内容（引导脚本必须在这里面，否则会闪屏）。"""
    return (WEB / name).read_text(encoding="utf-8").split("</head>", 1)[0]


def token_block(css: str, selector: str) -> dict[str, str]:
    """解析 `selector { --k:v; … }` → {token: 值}。selector 用 re.escape 精确匹配。"""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, f"没有找到令牌块：{selector}"
    return {k.strip(): v.strip()
            for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", m.group(1))}


def dark_of(name: str) -> dict[str, str]:
    return token_block(style_of(name), ":root")


def light_of(name: str) -> dict[str, str]:
    return token_block(style_of(name), 'html[data-theme="light"]')


def strip_token_blocks(css: str) -> str:
    """去掉 :root 与浅色块（令牌块是**唯一**允许出现色值字面量的地方）。"""
    css = re.sub(r":root\s*\{.*?\}", "", css, flags=re.S)
    return re.sub(r'html\[data-theme="light"\]\s*\{.*?\}', "", css, flags=re.S)


def strip_css_comments(css: str) -> str:
    """去掉 /* … */（注释是文档，不设色——「零裸色值」约束的是声明）。

    ⚠️ 评审2 指出的口径缺口（已修）：原先的扫描只看**第一块** `<style>`，`<script>`、
    内联 `style=` 属性、以及将来任何第二块 `<style>` 全在网外，颜色关键字也完全不查。
    现在改成扫**整页**（下节的 test_no_raw_colors_outside_token_blocks），注释与令牌块
    仍是唯二的豁免——它们不是声明。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def page_without_tokens_and_comments(name: str) -> str:
    """整页原文 − CSS 注释 − 令牌块（含 <script>、内联 style=、全部 <style> 块）。"""
    return strip_token_blocks(strip_css_comments((WEB / name).read_text(encoding="utf-8")))


#: 允许出现在声明里的**颜色关键字**（其余关键字都算「写死的色值」）：
#:   transparent = 透明（与主题无关）；currentColor = 显式「跟随文字色」（spec §六 图标口径）；
#:   inherit/none/initial = 不是色值本身，是 CSS 关键字。
COLOR_KEYWORDS_ALLOWED = ("transparent", "currentColor", "inherit", "none", "initial")
#: CSS 颜色关键字全表（评审2 的阴性对照：`color:white` 这类此前完全抓不到；
#: 含 light*/dark* 与全部 CSS3 扩展名——`lightgray` 这种带前缀的写法同样在网内）
NAMED_COLORS = tuple("""
    aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
    blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk
    crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgrey darkgreen darkkhaki
    darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
    dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
    gold goldenrod gray grey green greenyellow honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgrey lightgreen lightpink lightsalmon lightseagreen
    lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
    magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
    mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
    mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
    palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
    powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
    seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
    steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
    yellowgreen
""".split())


def raw_color_hits(text: str) -> list[str]:
    """写死色值扫描器（唯一真相）：hex / rgb()·rgba()·hsl()·hsla() / 颜色关键字。

    关键字按**出现语境**匹配，避免误伤普通词：
      · CSS 声明：`color: white;`（分隔符允许行尾——用 `$` 兜）；
      · JS 赋值：`el.style.color = 'red'`（**只认上色属性**，否则 `class="gold"` 这种
        HTML 类名会被误报 —— 首跑就踩到了，故收紧）；
      · JS 对象字面量：`{ background: 'red' }`。
    """
    names = "|".join(NAMED_COLORS)
    allowed = [k.lower() for k in COLOR_KEYWORDS_ALLOWED]
    hits = [f"hex {m}" for m in re.findall(r"#[0-9a-fA-F]{3,8}\b", text)]
    hits += [f"fn {m}" for m in re.findall(r"\b(?:rgb|rgba|hsl|hsla)\(", text)]
    for m in re.finditer(r":\s*(%s)\s*(?:[;}\"'\s]|$)" % names, text, re.I | re.M):
        if m.group(1).lower() not in allowed:
            hits.append(f"css 关键字 {m.group(1)}")
    js_prop = r"(?:color|backgroundColor|background|fill|stroke|borderColor|outlineColor)"
    for m in re.finditer(r"\b%s\s*=\s*['\"](%s)['\"]" % (js_prop, names), text, re.I):
        if m.group(1).lower() not in allowed:
            hits.append(f"js 关键字 {m.group(1)}")
    for m in re.finditer(r"\b%s\s*:\s*['\"](%s)['\"]" % (js_prop, names), text, re.I):
        if m.group(1).lower() not in allowed:
            hits.append(f"js 对象色值 {m.group(1)}")
    return hits


# ---------------- 1) 深色 = 现状值**逐字**保留（默认主题零变化的硬证据） ----------------
#: index.html 的深色令牌（B15 之前的 :root 原值；本批只**新增** --zh/--on-gold/
#: --shadow/--mask/--mask-strong 与评审修复的 --dot-w/--dot-mix，既有键一个都不许动）
DARK_INDEX = {
    "--bg": "#0B0E13", "--surface": "#12161D", "--surface-2": "#171C24",
    "--border": "#232B35", "--border-strong": "#2E3844",
    "--text-1": "#E8EEF5", "--text-2": "#93A1B0", "--text-3": "#5C6B7A",
    "--gold": "#FFD97A", "--gold-dim": "rgba(255,217,122,.14)",
    "--ok": "#5FD08A", "--warn": "#E8B44A", "--err": "#FF6B6B", "--partial": "#7C8B99",
    "--dot-w": "100%", "--dot-mix": "#0B0E13",
    "--radius": "12px", "--qr-bg": "#FFFFFF",
    "--cap-en": "24px", "--cap-zh": "34px",
    "--ui-title": "16px", "--ui-body": "14px", "--ui-small": "12px",
}
DARK_PRIVACY = {
    "--bg": "#0B0E13", "--surface": "#12161D", "--surface-2": "#171C24",
    "--border": "#232B35", "--text-1": "#E8EEF5", "--text-2": "#93A1B0",
    "--gold": "#FFD97A", "--ok": "#5FD08A",
}
DARK_FLOAT = {
    "--surface": "#12161D", "--surface-2": "#171C24", "--border": "#232B35",
    "--border-strong": "#2E3844", "--text-1": "#E8EEF5", "--text-2": "#93A1B0",
    "--text-3": "#5C6B7A", "--gold": "#FFD97A", "--err": "#FF6B6B",
}


@pytest.mark.parametrize("name,want", [
    ("index.html", DARK_INDEX), ("privacy.html", DARK_PRIVACY), ("float.html", DARK_FLOAT)])
def test_dark_tokens_are_verbatim(name, want):
    got = dark_of(name)
    for k, v in want.items():
        assert got.get(k) == v, f"{name} 的深色 {k} 被改动了：{got.get(k)!r} != {v!r}"


def test_new_tokens_default_to_pixel_identical_dark_values():
    """新增令牌的深色值必须**等于它们替换掉的字面量**（否则深色就不是「零变化」）：
    --on-gold=#1A1408（§六 主按钮深字）、--zh=#FFD97A（中文字幕原金色）、
    --shadow=0 1px 2px rgba(0,0,0,.4)（§四 投影上限）、--mask/.55 与 --mask-strong/.88。"""
    d = dark_of("index.html")
    assert d["--on-gold"] == "#1A1408"
    assert d["--zh"] == d["--gold"] == "#FFD97A"
    assert d["--shadow"] == "0 1px 2px rgba(0,0,0,.4)"
    assert d["--mask"] == "rgba(0,0,0,.55)"
    assert d["--mask-strong"] == "rgba(4,6,10,.88)"


# ---------------- 2) 两套令牌：键集合完全一致 + 浅色块存在且可达 ----------------

@pytest.mark.parametrize("name", THEMED_PAGES)
def test_light_block_exists_and_is_referenced_by_data_theme(name):
    css = style_of(name)
    assert 'html[data-theme="light"]' in css, f"{name} 没有浅色块"
    # 选择器必须真的可达：引导脚本往 <html> 上挂 data-theme
    assert "document.documentElement.dataset.theme" in head_scripts(name), \
        f"{name} 的 data-theme 没有人写（浅色块是死代码）"


@pytest.mark.parametrize("name", THEMED_PAGES)
def test_dark_and_light_token_key_sets_are_identical(name):
    dark, light = dark_of(name), light_of(name)
    assert light, f"{name} 的浅色块是空的"
    assert set(dark) == set(light), (
        f"{name} 深浅键集合不一致（只加在深色块里的令牌会在浅色下静默继承深色值）："
        f"缺 {sorted(set(dark) - set(light))} / 多 {sorted(set(light) - set(dark))}")


@pytest.mark.parametrize("name", THEMED_PAGES)
def test_light_actually_differs_from_dark_on_colors(name):
    """浅色块不能是拷贝：底色/文字/强调必须换成白底蓝调那套（防「复制粘贴忘记改」）。"""
    dark, light = dark_of(name), light_of(name)
    changed = {k for k in dark if dark[k] != light[k]}
    for k in ("--bg", "--surface", "--text-1", "--gold"):
        assert k in changed, f"{name} 浅色块的 {k} 与深色相同（没换值）"
    assert light["--gold"] != dark["--gold"]


def test_theme_agnostic_tokens_keep_identical_values():
    """字号档/圆角/二维码底与主题无关：浅色块照列同名键，值必须**原样**（不是新配色）。"""
    dark, light = dark_of("index.html"), light_of("index.html")
    for k in ("--cap-en", "--cap-zh", "--ui-title", "--ui-body", "--ui-small",
              "--radius", "--qr-bg"):
        assert dark[k] == light[k], f"{k} 与主题无关，浅色下不该变"


# ---------------- 3) 零裸色值：令牌块之外只有 var()/color-mix() ----------------

@pytest.mark.parametrize("name", PAGES)
def test_no_raw_colors_outside_token_blocks(name):
    """**整页**扫描（评审2 修复）：<style> 全部块 + <script> + 内联 style= 属性一起查。

    口径缺口是评审2 实测出来的：旧版只 `.split('<style>')[1]`，于是 `<script>` 里写
    `el.style.color='#f00'`、任何一个第二块 `<style>`、内联 style=、以及 `color:white`
    这类颜色关键字都不会响。现在：hex（3/4/6/8 位）、rgb()/rgba()/hsl()/hsla()、
    颜色关键字（transparent / currentColor 白名单除外，它们与主题无关或本就主题感知）
    全在网内，扫描范围是**整个文件**。
    """
    hits = raw_color_hits(page_without_tokens_and_comments(name))
    assert not hits, (f"{name} 令牌块之外还有写死的色值：{hits}"
                      f"（§一-4 颜色一律走变量；新令牌记得两套一起加）")


def test_scanner_catches_what_the_reviewer_injected():
    """扫描器的**阳性对照**（评审2 用同一套正则做过）：这些写法必须被抓到；
    并给出**阴性对照**：transparent / currentColor 是合法写法，不许误报。"""
    assert raw_color_hits("color:#123456") and raw_color_hits("color:#fff")
    assert raw_color_hits("border:1px solid rgba(0,0,0,.3)")
    assert raw_color_hits("background:rgb(0 0 0 / 50%)")
    assert raw_color_hits("fill:hsl(210 50% 40%)")
    assert raw_color_hits("outline:#11223344")
    assert raw_color_hits("color:white"), "颜色关键字必须入网（评审2 的阴性对照）"
    assert raw_color_hits("el.style.color = 'red';")
    assert raw_color_hits("d.style.background = 'lightgray'")
    for ok in ("background:transparent", "color:currentColor", "border:none",
               "border:1px solid transparent", "color:inherit",
               'class="gold"',                       # HTML 类名不是色值（首跑误报，已收紧）
               "h1 .gold { color:var(--gold); }"):
        assert raw_color_hits(ok) == [], f"{ok} 不该被误报（与主题无关的合法写法）"


def test_index_hardcoded_colors_all_went_through_tokens():
    """逐条对账（ask ③ 的勘察清单）：7 处金色主按钮深字 + 10 处阴影/遮罩。

    这 17 处是本条开工前 index.html 里**全部**的裸色值；现在它们必须是 var(--…)
    （字面量只剩令牌定义那一份，所以全文件里 #1A1408 只许出现一次）。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.count("#1A1408") == 1, "金色主按钮的深字应走 --on-gold 令牌（只剩 :root 里那一份）"
    assert "--on-gold:#1A1408;" in html
    assert html.count("color:var(--on-gold);") == 7, "7 处主按钮深字（#toolbar#btnEndClass/" \
        "wizStep.cur/wizardBox.primary/tierBadge/dlgFoot.primary/nbPH.primary/card.primary）"
    assert html.count("box-shadow:var(--shadow);") == 6, "6 处 0 1px 2px 投影"
    assert html.count("background:var(--mask);") == 3, "3 处 55% 遮罩"
    assert html.count("background:var(--mask-strong);") == 1, "1 处向导 88% 遮罩"


def test_float_pinned_dark_uses_its_own_token():
    """ask ④：悬浮条**不响应主题**——没有浅色块、不读 ll_theme，半透明深底走 --bar-bg。

    判据取「不碰 localStorage / 不写 data-theme」而不是「文件里没有 ll_theme 这串字」：
    说明用的注释里可以提到它（反而是好事），但代码路径不许有。
    """
    html = strip_css_comments((WEB / "float.html").read_text(encoding="utf-8"))
    assert 'data-theme="light"' not in html, "悬浮字幕条必须钉死深色（不许有浅色块）"
    assert "localStorage" not in html, "悬浮字幕条不该读主题偏好（它不随主题变）"
    assert "dataset.theme" not in html
    full = (WEB / "float.html").read_text(encoding="utf-8")
    assert "--bar-bg:rgba(18, 22, 29, .82);" in full, "半透明深底应提为 --bar-bg 令牌"
    assert "background:var(--bar-bg);" in full
    assert "rgba(18, 22, 29, .82)" not in strip_token_blocks(style_of("float.html")), \
        "#bar 仍写着裸 rgba"


# ---------------- 4) 切换机制：ll_theme ∈ dark|light|auto（默认 dark）+ 系统监听 ----------------

@pytest.mark.parametrize("name", THEMED_PAGES)
def test_ll_theme_bootstrap_in_head_before_body(name):
    head = head_scripts(name)
    assert "ll_theme" in head, f"{name} 没有读 ll_theme"
    for mode in ("dark", "light", "auto"):
        assert f"'{mode}'" in head, f"{name} 的引导脚本没有处理 {mode}"
    assert "localStorage.getItem(KEY)" in head
    assert "try" in head and "catch" in head, \
        f"{name}：隐私模式下 localStorage 会抛，必须兜住（回落 dark）"
    # 防闪烁：脚本必须在 </head> 之前（首帧之前执行）
    assert "<script>" in head, f"{name} 的引导脚本不在 head 里（会先闪一帧深色）"


def test_only_the_caption_page_offers_the_switch():
    """写入端只有字幕页（它是唯一有设置界面的页）；privacy 只读不写——
    「在哪切」和「在哪生效」要分得清，免得以后有人在隐私页也塞一套控件。"""
    assert "localStorage.setItem" in head_scripts("index.html")
    assert "localStorage.setItem" not in head_scripts("privacy.html")
    assert "window.LL_THEME" in head_scripts("index.html")     # 设置页要用它写


@pytest.mark.parametrize("name", THEMED_PAGES)
def test_auto_mode_follows_system_and_listens(name):
    head = head_scripts(name)
    assert "matchMedia('(prefers-color-scheme: dark)')" in head
    assert "addEventListener('change'" in head, "系统切换浅色/深色时页面要跟着变"
    assert "addListener" in head, "老 WebView2 只有 addListener（兜底路径）"
    assert "MQ.matches" in head, "auto 必须按系统当前值落成实色 dark/light"


def test_default_is_dark_and_nothing_is_preapplied():
    """默认深色零变化：静态 HTML 的 <html> 标签上不得预挂 data-theme（挂上就等于替用户选了）。"""
    for name in PAGES:
        html = (WEB / name).read_text(encoding="utf-8")
        html_tag = re.search(r"<html\b[^>]*>", html).group(0)
        assert "data-theme" not in html_tag, f"{name} 的 <html> 预挂了 data-theme：{html_tag}"
    head = head_scripts("index.html")
    assert "var cur = 'dark';" in head, "引导脚本的兜底值必须是 dark"
    assert "return MODES.indexOf(v) >= 0 ? v : 'dark';" in head, "非法取值必须回落 dark"


# ---------------- 5) 设置页 🎨外观 的「主题」三选 ----------------

def test_settings_has_theme_radiogroup_with_three_modes():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    card = html.split('<div class="card" data-set-tab="look">', 1)[1].split(
        "</div>\n\n    <div", 1)[0]
    assert "<h2>主题" in card, "外观分区第一张卡应是「主题」"
    assert 'id="themeRow"' in card and 'role="radiogroup"' in card
    for mode in ("dark", "light", "auto"):
        assert f'id="theme{mode.capitalize()}"' in card, mode
        assert f'data-mode="{mode}"' in card, mode
    assert card.count('role="radio"') == 3 and card.count('aria-checked="false"') == 3
    assert "悬浮字幕条始终是深色" in card, "要让用户知道悬浮条不跟着变（否则像坏了）"


def test_theme_control_reuses_gold_dim_selected_state():
    """选中态复用 .tagChip.on（gold-dim 底 + gold 字，§六）；且要还原 chip 形状——
    .card button 的优先级高于 .tagChip，不写 #themeRow 前缀会被拉成方按钮。"""
    css = style_of("index.html")
    block = css.split("/* —— B15：设置页「主题」三选", 1)[1].split("/*", 1)[0]
    assert "#themeRow .tagChip {" in block and "#themeRow .tagChip.on {" in block
    assert "var(--gold-dim)" in block and "color:var(--gold)" in block
    assert "animation" not in block and "transition" not in block, "§5：不加动效"
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}", block), "主题控件不得写死色值（§一-4）"


def test_theme_wiring_reads_and_writes_ll_theme():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "const THEME_MODES = ['dark', 'light', 'auto'];" in html
    setter = html.split("function setThemeMode(mode) {", 1)[1].split("\n}", 1)[0]
    assert "window.LL_THEME.set(mode)" in setter, "点选后必须真的写入并生效"
    assert "renderThemeRow();" in setter, "点选后选中态要跟着变"
    render = html.split("function renderThemeRow() {", 1)[1].split("\n}", 1)[0]
    assert "aria-checked" in render, "读屏用户要知道选中哪格"
    assert "window.LL_THEME.get()" in render
    boot = html.split("// —— 启动引导（**必须放在全部 let/const 声明之后**）——", 1)[1]
    assert "renderThemeRow();" in boot and "setThemeMode(_m)" in boot, "启动没接线"


def test_theme_control_does_not_touch_config():
    """主题是**每台设备**的显示偏好（手机各记各的），不写 config.json：
    这条守住「服务端零改动」——B15 不该往白名单/接口里加任何键。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    seg = html.split("// —— B15：主题三选", 1)[1].split("function openWorkbench", 1)[0]
    assert "/api/config" not in seg
    assert "localStorage.setItem(KEY, cur)" in head_scripts("index.html")   # 只落本地


# ---------------- 6) 浅色可读性：白底对比度 ≥ 4.5:1（WCAG AA，正常字号） ----------------

def _srgb(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def over(fg: str, alpha: float, bg: str) -> str:
    """把 fg 以 alpha 叠在 bg 上（算选中态那种半透明底的等效实色）。"""
    f = [int(fg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02X%02X%02X" % tuple(round(alpha * x + (1 - alpha) * y) for x, y in zip(f, b))


def mix(fg: str, weight: float, bg: str) -> str:
    """color-mix(in srgb, fg <weight>, bg) 的等效实色（两色都不透明时同 over 的算法）。"""
    return over(fg, weight, bg)


def test_light_theme_contrast_meets_aa():
    """浅色下这些组合都要 ≥4.5:1（ask ① 点名「字幕中文色…保证白底对比度 ≥4.5:1」）。

    数值口径 = WCAG 相对亮度对比度；选中态底是 rgba，先按 alpha 叠到 --surface-2 上
    再算（半透明底的真实观感）。这也是把强调色从用户点名的 #4D6BFE（白字 4.33 /
    选中态 3.39，均不达 AA）定成同族更深一档 #2B4BF2 的依据。
    """
    lt = light_of("index.html")
    surface, s2, bg = lt["--surface"], lt["--surface-2"], lt["--bg"]
    assert contrast(lt["--zh"], surface) >= 4.5, "中文字幕（白底）"
    assert contrast(lt["--zh"], bg) >= 4.5, "中文字幕（页面底）"
    assert contrast(lt["--text-1"], surface) >= 4.5, "主文字"
    assert contrast(lt["--text-2"], surface) >= 4.5, "次级文字"
    assert contrast(lt["--on-gold"], lt["--gold"]) >= 4.5, "主按钮（蓝底白字）"
    assert contrast(lt["--gold"], surface) >= 4.5, "强调色文字"
    dim = re.search(r"rgba\(([\d.]+),([\d.]+),([\d.]+),\s*([\d.]+)\)", lt["--gold-dim"])
    assert dim, "--gold-dim 应为 rgba（选中态底）"
    tint = over("#%02X%02X%02X" % tuple(int(x) for x in dim.groups()[:3]),
                float(dim.group(4)), s2)
    assert contrast(lt["--gold"], tint) >= 4.5, f"选中态（gold-dim 底）={contrast(lt['--gold'], tint):.2f}"


def test_light_text_3_is_readable_as_body_text():
    """评审2 修复：--text-3 在浅色下是**说明/元信息/空状态**的正文字色（28 处 11-16px），
    初版 #8A94A4 只有 3.06（白底）/ 2.76（--surface-2），比它替掉的深色基线（3.31）还糊。
    现取 #66707E：三种底全过 4.5（5.02 / 4.72 / 4.52），并在这里钉住。"""
    lt = light_of("index.html")
    for base_key in ("--surface", "--surface-2", "--bg"):
        got = contrast(lt["--text-3"], lt[base_key])
        assert got >= 4.5, f"--text-3 在 {base_key} 上只有 {got:.2f}:1（浅色下当正文用）"


def _tint_of(lt: dict, key: str, base: str) -> str:
    """控件真实底：自己 14% 半透明色压在 base 上（spec §六 徽章警示态 + 各处 banner/toast）。"""
    return over(lt[key], 0.14, base)


def test_light_status_colors_meet_aa_on_tinted_bg():
    """评审① 修复：ok/warn/err 的**实际落点**不是 --surface，而是「自己的 14% 半透明底」
    （.chip.warn / #pausedBanner / #errBanner / #toast[.ok] / .mState / .nbFlag 都是
    color-mix(同色 14%)，底再压在 body/卡片上）。旧断言只量 `contrast(色, --surface)`
    → 恰好绕开这个回归：深色同控件 5.9~8.0:1，浅色初版掉到 3.7~4.1:1。

    现在按**合成底**逐个断言（三种真实背景 × 有/无 14% 底），并钉住 ≥4.5。"""
    lt = light_of("index.html")
    for key in ("--ok", "--warn", "--err"):
        for base_key in ("--surface", "--surface-2", "--bg"):
            base = lt[base_key]
            got = contrast(lt[key], _tint_of(lt, key, base))
            assert got >= 4.5, (f"{key} 压在自己的 14% 底（叠在 {base_key} 上）只有 "
                                f"{got:.2f}:1 —— 这正是评审实测的 3.7~4.1 回归")
            plain = contrast(lt[key], base)
            assert plain >= 4.5, f"{key} 在 {base_key} 上只有 {plain:.2f}:1（无底用法同上）"


def test_wizard_done_step_ok_text_is_readable():
    """评审① 附带边界：.wizStep.done 是 12px 的 ok 字压在向导卡（--surface-2）上，无 14% 底。"""
    lt = light_of("index.html")
    got = contrast(lt["--ok"], lt["--surface-2"])
    assert got >= 4.5, f".wizStep.done 的 ok 字在 --surface-2 上只有 {got:.2f}:1"


def test_privacy_light_colors_readable():
    lt = light_of("privacy.html")
    assert contrast(lt["--text-1"], lt["--surface"]) >= 4.5
    assert contrast(lt["--text-2"], lt["--surface"]) >= 4.5
    assert contrast(lt["--gold"], lt["--surface"]) >= 4.5     # 链接字色（白底）


def test_dark_caption_and_status_still_readable():
    """反向守卫：深色（默认）的字幕/状态色不许在这一批里被调坏。"""
    d = dark_of("index.html")
    assert contrast(d["--zh"], d["--surface"]) >= 4.5
    assert contrast(d["--text-1"], d["--surface"]) >= 4.5
    assert contrast(d["--text-2"], d["--surface"]) >= 4.5


# ---------------- 6b) 课程身份色圆点（评审2 修复） ----------------

def test_course_dots_color_goes_through_a_token():
    """三处动态上色点（侧栏 .cdot、设置页换色钮 .cSwDot、移动到弹窗 .cdot）**不得**
    再直接写 `style.background`——色值一律写进 --dot-c，由 CSS 按主题混（§一-4）。
    否则浅色下那 8 个低饱和课程色在白底上只有 2.1~2.9:1，且对任何令牌扫描都隐形。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    # ⚠️ 选择器必须带 `.card` 前缀（特异性）：换色钮是 `.card` 里的 <button>，`.card button`
    # （0,1,1）会盖掉 `.cSwDot`（0,1,0）——真浏览器实测过（换色钮恒为 --surface-2），
    # 静态断言看不出这种输赢，所以这里把**修好后的形状**钉死。
    assert ".cdot, .card .cSwDot { background:color-mix(" \
           "in srgb, var(--dot-c) var(--dot-w), var(--dot-mix)); }" in html, \
        "圆点/色块的唯一上色口不见了（或丢了 .card 前缀 → 会被 .card button 盖掉）"
    assert html.count("d.style.setProperty('--dot-c', color);") == 2   # 侧栏 + 移动到弹窗
    assert "b.style.setProperty('--dot-c', col);" in html              # 设置页换色钮
    assert "style.background = color" not in html and "style.background = col" not in html


def test_light_course_dots_meet_non_text_contrast():
    """WCAG 1.4.11（非文本对比 ≥3:1）：圆点/色块「颜色即内容」（.cSwDot 更是「点哪个就是
    选哪个色」的控件），浅色下必须能被看见。色值来自服务端 COURSE_PALETTE（8 个低饱和色，
    深色底上 5.96~8.57:1），浅色初版在白底上 2.12~2.87:1 —— 混深后 4.10~5.22:1。

    数值口径 = --dot-w / --dot-mix 两个令牌的实际混合（不是「应该差不多」）。"""
    import main   # 调色板单一事实源（main.COURSE_PALETTE；同 test_hygiene 的导入口径）
    lt = light_of("index.html")
    assert lt["--dot-w"].endswith("%")
    w = float(lt["--dot-w"].rstrip("%")) / 100
    assert 0 < w <= 1
    for color in main.COURSE_PALETTE:
        got_color = mix(color, w, lt["--dot-mix"])
        for base_key in ("--surface", "--surface-2", "--bg"):
            got = contrast(got_color, lt[base_key])
            assert got >= 3.0, (f"课程色 {color} 浅色混后 {got_color} 在 {base_key} 上只有 "
                                f"{got:.2f}:1（< 非文本 3:1）")


def test_dark_course_dots_are_untouched():
    """深色恒等：--dot-w=100% 时 color-mix 结果就是课程色本身（默认主题零变化）。"""
    d = dark_of("index.html")
    assert d["--dot-w"] == "100%"
    import main
    for color in main.COURSE_PALETTE:
        assert mix(color, 1.0, d["--dot-mix"]).lower() == color.lower()


# ---------------- 7) 组件侧零改动：字幕中文改吃 --zh，但其余一律仍吃 --gold ----------------

def test_caption_zh_uses_zh_token():
    """"字幕中文色浅色下用深蓝"落地方式：中文字幕的 3 个出口全部改吃 --zh
    （.zh 正文 / 笔记库中英对照的「中」列 / 向导里「听到你说」的译文）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    zh_rule = html.split(".zh { font-size:var(--cap-zh);", 1)[1].split("}", 1)[0]
    assert "color:var(--zh)" in zh_rule
    assert ".nbCell.zh { color:var(--zh); }" in html
    assert "zhEl.style.color = err ? 'var(--text-2)' : 'var(--zh)';" in html


def test_brand_and_selection_still_use_gold():
    """§一-2：金色仍是品牌/选中/主按钮的唯一强调色——本批只把「中文字幕」与
    「主按钮深字」拆成独立令牌，其余用法一个都没换走（浅色下由 --gold 自己变蓝）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.count("var(--gold)") >= 50, "金色强调用法不该被这一批改掉"
    assert "#toolbar button#btnEndClass { background:var(--gold); color:var(--on-gold);" in html
    assert ".tagChip.on { background:var(--gold-dim); color:var(--gold);" in html
    assert "outline:2px solid color-mix(in srgb, var(--gold) 50%, transparent);" in html


def test_mask_tokens_are_used_at_the_three_modal_families():
    """遮罩令牌落到三族弹窗（关于 55% / 移动到 55% / 笔记管理 55% + 向导 88%）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "#aboutMask" in html and "#moveMask" in html and ".dlgMask" in html
    for sel in ("#aboutMask", "#moveMask", ".dlgMask"):
        assert "background:var(--mask);" in html.split(sel, 1)[1][:160], sel
    assert "background:var(--mask-strong);" in html.split("#wizardMask", 1)[1][:200]


# ---------------- 8) 偏好**真的存得下来**：桌面壳的持久化与 origin 稳定性 ----------------
# 评审①③ 的工具链实读/实跑：desktop.py start_server() 原先 port=0（内核随机端口），
# 窗口载入 http://127.0.0.1:<port>/，而 localStorage 按 origin（含端口）隔离；再加上
# webview.start() 默认 private_mode=True（webview/__init__.py:198「cookies and local
# storage are not preserved」）→ 主题（以及 p1 字号、B12 分区、LAN 配对记忆）每次开应用
# 都归零。这里把两条修法都钉住：默认端口 = 桌面首选端口；start 时关私有模式 + 给
# ROOT 下的持久 profile。

def test_desktop_window_origin_is_stable_across_launches():
    """端口默认值不许再是 0（随机）：端口属于 origin，随机端口 = 前端偏好每次失忆。"""
    import desktop
    assert desktop.DESKTOP_PREFERRED_PORT == desktop.lan_mod.LAN_PREFERRED_PORT, \
        "桌面窗口端口应与 --server-only/手机同屏同号（开关同屏也不换 origin）"
    import inspect
    default = inspect.signature(desktop.start_server).parameters["port"].default
    assert default == desktop.DESKTOP_PREFERRED_PORT, \
        "start_server 的默认端口必须稳定（0 = 内核随机 → 每次启动换 origin）"
    # 被占时的降级仍在（「宁可降级也不让窗口起不来」）：仍是 lan.pick_port 那条路
    src = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    assert "want = lan_mod.pick_port(host, want)" in src


def test_desktop_turns_off_pywebview_private_mode(tmp_path, monkeypatch):
    """private_mode 默认 True = localStorage 关窗即蒸发；桌面壳必须显式关掉并给 storage_path。"""
    import desktop
    monkeypatch.setattr(desktop, "WEBVIEW_PROFILE_DIR", tmp_path / "webview_profile")
    kw = desktop.persistent_storage_kwargs()
    assert kw == {"private_mode": False,
                  "storage_path": str(tmp_path / "webview_profile")}, kw
    assert (tmp_path / "webview_profile").is_dir(), "profile 目录要先建出来"
    src = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    assert "**persistent_storage_kwargs())" in src, "webview.start 没接上这组参数"


def test_desktop_storage_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    """profile 不可写时必须**回落默认（私有模式）**而不是抛异常：pywebview 的
    __set_storage_path 在不可写时直接抛 WebViewException（webview/__init__.py:487-498），
    那会让整个窗口起不来——「偏好记不住」可以降级，「应用打不开」不行。"""
    import desktop
    monkeypatch.setattr(desktop, "WEBVIEW_PROFILE_DIR", tmp_path / "nope" / "deep")
    monkeypatch.setattr(desktop.os, "access", lambda *a, **k: False)
    assert desktop.persistent_storage_kwargs() == {}


def test_webview_profile_is_registered_as_user_data():
    """profile 里有 localStorage（含扫码配对缓存的 LAN token）→ 必须进 D3 用户数据清单，
    也不能进 git（同 config.json 口径）。"""
    import build_exe
    assert "webview_profile" in build_exe.FORBIDDEN_DIST_ENTRIES
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^webview_profile/?$", gi, re.M), ".gitignore 缺 webview_profile/"
    src = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    assert 'WEBVIEW_PROFILE_DIR = (service.ROOT / "webview_profile")' in src, \
        "profile 必须锚在 ROOT（与 config.json/transcripts/logs 同一口径）"
