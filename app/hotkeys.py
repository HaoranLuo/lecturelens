# -*- coding: utf-8 -*-
"""hotkeys.py — 全局快捷键（P3 打磨阶段，docs/design-borrowing.md B12「快捷键体系」）。

职责边界（**本模块不 import main**，避免循环依赖；动作由调用方以 callable 注入）：
- 键名白名单 + 组合规范化：纯函数，**不需要 keyboard 库即可用**——降级态下
  设置页仍要能校验输入、展示当前绑定；
- HotkeyManager：按配置注册 / 重绑 / 注销全局热键。**任何失败都不抛出**，一律
  以 state() 如实上报（active / reason），由上层优雅降级为「仅托盘操作」。

设计取舍（每条都有代价，写清以便日后回看）：
- **全有或全无**：任一动作注册失败 → 注销已注册的、整体置 active=False。
  半可用状态（两个热键只生效一个）在界面上无法自证，用户会以为「按了没反应
  是 bug」；宁可明确降级并把原因写进设置页。
- **suppress=False**：不吞键。全局钩子吞掉按键会改变其它应用的正常输入，而
  快捷键只是「顺手多一条路」，不值得为它付这个代价。
- **回调全 try/except**：keyboard 库在监听线程里调用回调，未捕获异常会打断该
  线程后续分发（一个 bug 导致全部热键失灵，且现场只剩一条 traceback）。
- **不 import 时不留副作用**：`import hotkeys` 不碰 keyboard 库、不建线程——
  headless 门禁（desktop.py --check）与降级路径共用同一份校验逻辑。

降级口径（ask：import/注册失败时优雅降级为仅托盘操作并在设置注明）：
  reason = "disabled"            → 用户在设置页关了热键（配置项）
         | "import: ..."         → 库导入失败（未安装/权限/环境异常）
         | "register: ..."       → 注册失败（键被其它程序钩子占用等）
         | "starting" / "stopped"→ 尚未启动 / 已注销（生命周期边界）
**macOS**：import/register 失败时 reason 追加一句「需辅助功能授权」（见
accessibility_hint——没有该授权时键盘监听收不到事件，这是 Mac 上最常见的成因）；
同一句话也随 state()["hint"] 下发，让设置页在热键**可用时**也讲清楚要先授权。
非 darwin 平台这段提示恒为空串，文案与之前逐字一致。
"""
import logging
import sys
import threading

log = logging.getLogger("hotkeys")

# —— 键名白名单（ask：可在设置页改键，白名单校验键名）——
# 修饰键别名 → 规范名。keyboard 库内部把 win/meta/cmd/command 归一到同一个键
# （实测 keyboard.parse_hotkey('cmd+alt+l') == parse_hotkey('command+alt+l') ==
# parse_hotkey('windows+alt+l')），这里跟它对齐：用户写 win/super/meta/cmd/command
# 都能存成同一份规范形。**规范名按平台取**：
#   - darwin：command（库在 Darwin 上把 windows/win/cmd/command 全部归一到
#     'command'，见 keyboard/_canonical_names.py 的 platform-specific overrides；
#     设置页显示 command 才是用户键盘上真有的那个键）；
#   - 其它：windows（库的默认归一目标）。
def _meta_key_name() -> str:
    """meta/⌘ 键的规范名（按**调用时**平台取；模块常量见下方快照）。"""
    return "command" if sys.platform == "darwin" else "windows"


def modifier_aliases() -> dict:
    """修饰键别名 → 规范名（按调用时平台现算——单测才能在 Windows 上验 darwin 分支）。"""
    meta = _meta_key_name()
    return {"ctrl": "ctrl", "control": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "win": meta, "windows": meta, "super": meta, "meta": meta,
            "cmd": meta, "command": meta}


def modifier_order() -> tuple:
    """规范输出顺序固定：同一组合的两种写法必须产出同一个字符串，否则
    「改键后去重/冲突比较」会因书写顺序不同而漏判。"""
    return ("ctrl", "alt", "shift", _meta_key_name())


#: 导入时快照（与本平台一致；对外引用请用它们，_parse 内部走上面的函数）
MODIFIER_ALIASES: dict = modifier_aliases()
MODIFIER_ORDER: tuple = modifier_order()
# 主键白名单：字母 / 数字 / F1-F12。刻意收窄——keyboard 库对逗号、加号、
# 空格等有「用名字不用符号」的额外规矩（add_hotkey 文档：'comma'/'plus'/
# 'space'），那些键留给后续按需扩，不在这里一次放进来承担解析歧义。
MAIN_KEYS = frozenset(
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"f{i}" for i in range(1, 13)]
)

# —— 动作 ↔ 配置键 ——
ACTION_CONFIG_KEY: dict = {"toggle_pause": "hotkey_pause",   # Ctrl+Alt+L
                           "toggle_float": "hotkey_float"}   # Ctrl+Alt+S
HOTKEY_CONFIG_KEYS: tuple = tuple(ACTION_CONFIG_KEY.values())
DEFAULT_BINDINGS: dict = {"toggle_pause": "ctrl+alt+l", "toggle_float": "ctrl+alt+s"}
# 动作的中文名：单一事实源在模块里，设置页/日志都读它（避免两处中文漂移）
BINDING_LABELS: dict = {"toggle_pause": "暂停/恢复字幕",
                        "toggle_float": "悬浮字幕条显隐"}

#: macOS 上全局键盘钩子要「辅助功能」授权（TCC），否则键盘库的监听收不到事件；
#: 这句同时是**降级 reason 的后缀**与**设置页说明**（state()["hint"]）的单一事实源。
MAC_ACCESSIBILITY_HINT = ("macOS 需在 系统设置 → 隐私与安全性 → 辅助功能 "
                          "里授权 LectureLens（或运行它的终端/Python）后重试")


def accessibility_hint() -> str:
    """本平台的授权提示；**非 darwin 一律空串**（Windows/Linux 文案逐字不变）。"""
    return MAC_ACCESSIBILITY_HINT if sys.platform == "darwin" else ""


def _with_accessibility_hint(reason: str) -> str:
    """降级原因 + 本平台授权提示（非 darwin 原样返回——Windows 原因文案逐字不变）。
    设置页状态行会把 reason 原样显示，这是「按了没反应」在 Mac 上最常见的成因：
    没有辅助功能授权时 keyboard 的监听收不到任何按键。"""
    hint = accessibility_hint()
    return f"{reason}（{hint}）" if hint else reason


def whitelist_hint() -> str:
    """白名单的可读描述（错误文案与设置页提示共用，避免两处措辞不一致）。"""
    return ("修饰键 " + "/".join(modifier_order())
            + " 至少一个 + 主键一个（a-z / 0-9 / f1-f12）")


#: 导入时快照（同上；错误文案统一用它，保证同一进程内措辞一致）
WHITELIST_HINT = whitelist_hint()


def _parse(combo) -> tuple:
    """组合串 → (规范形, 错误文案)。规范形非 None 即合法。

    纯函数：不依赖 keyboard 库——降级态下设置页仍要能校验与回显。"""
    if not isinstance(combo, str):
        return None, f"须为字符串（收到 {type(combo).__name__}）"
    raw = combo.strip()
    if not raw:
        return None, "不能为空"
    tokens = [t.strip() for t in raw.lower().split("+")]
    if any(not t for t in tokens):
        return None, "有空的键名（如 'ctrl++l' 或末尾多一个 +）"
    aliases = modifier_aliases()          # 按调用时平台取（darwin: command）
    order = modifier_order()
    mods: list = []
    mains: list = []
    for t in tokens:
        if t in aliases:
            m = aliases[t]
            if m in mods:
                return None, f"重复的修饰键 {m}"
            mods.append(m)
        elif t in MAIN_KEYS:
            if mains:
                return None, f"只能有一个主键（收到 {mains[0]} 与 {t}）"
            mains.append(t)
        else:
            return None, f"未知键名 {t!r}（{WHITELIST_HINT}）"
    if not mains:
        return None, f"缺少主键（{WHITELIST_HINT}）"
    if not mods:
        # 单键热键会让打字时处处误触发（本应用是常驻后台的），必须挡在配置层
        return None, f"至少需要一个修饰键（{WHITELIST_HINT}）"
    return "+".join([m for m in order if m in mods] + mains), None


def normalize(combo) -> str | None:
    """返回规范形（小写、修饰键固定顺序、别名归一）；不合法返回 None。"""
    return _parse(combo)[0]


def combo_problem(combo) -> str | None:
    """校验组合串。返回错误文案；None = 合法（与 _config_number_problem 同款契约）。"""
    return _parse(combo)[1]


def bindings_from_config(cfg: dict) -> dict:
    """从配置取各动作的绑定，**逐个兜底到默认值**。

    配置层（/api/config 与 config.example.json）已做白名单校验，这里是第二道：
    手改 config.json 能绕过 API，而一个手滑写成 "ctrl+" 的绑定若原样喂给
    keyboard.add_hotkey 会在启动时抛错——那会把「热键不可用」升级成
    「服务起不来」，与降级口径相悖。"""
    out: dict = {}
    for action, key in ACTION_CONFIG_KEY.items():
        combo = normalize(cfg.get(key))
        if combo is None:
            if cfg.get(key) is not None:
                log.warning(f"[hotkeys] ⚠ 配置 {key}={cfg.get(key)!r} 非法，"
                            f"回落到默认 {DEFAULT_BINDINGS[action]}")
            combo = DEFAULT_BINDINGS[action]
        out[action] = combo
    return out


def idle_state() -> dict:
    """未启动时的 state 形状（main.status 初始化用）。
    UI 读的是同一份形状，避免「启动前字段缺失 → 前端 undefined」这类闪烁。
    hint = 本平台的授权说明（macOS 才非空，见 accessibility_hint）。"""
    return {"enabled": True, "active": False, "reason": "starting",
            "bindings": dict(DEFAULT_BINDINGS), "labels": dict(BINDING_LABELS),
            "hint": accessibility_hint()}


class HotkeyManager:
    """全局热键注册器。线程安全（锁保护注册/注销），**不抛异常**。

    actions: {动作名: 无参 callable}——由调用方注入（main 里是 toggle_pause /
    toggle_float 的闭包），本模块不认识 main 的任何状态。
    """

    def __init__(self, actions: dict, bindings: dict | None = None,
                 enabled: bool = True):
        self._actions = dict(actions or {})
        self._bindings = dict(bindings or DEFAULT_BINDINGS)
        self._enabled = bool(enabled)
        self._handles: dict = {}
        self._active = False
        self._reason: str | None = "starting"
        self._lock = threading.RLock()

    # —— 内部 ——

    def _unregister_all(self) -> None:
        """注销已注册的热键。逐个 try：一个注销失败不能连累其余（否则重绑时
        会留下孤儿钩子——旧键位仍然生效，用户看到「改了还在」）。"""
        for action, handle in list(self._handles.items()):
            try:
                handle()  # add_hotkey 的返回值即「移除函数」（库 __init__.py:615-616）
            except Exception as e:
                log.debug(f"[hotkeys] {action} 注销失败（忽略）: {e}")
        self._handles.clear()

    def _wrap(self, action: str, fn):
        """包一层异常兜底：库在监听线程里回调，异常会打断后续分发。"""
        def _callback():
            try:
                fn()
            except Exception as e:
                log.warning(f"[hotkeys] ⚠ {action} 回调异常: {type(e).__name__}: {e}")
        return _callback

    def _describe(self) -> str:
        return ", ".join(f"{BINDING_LABELS.get(a, a)}={c}"
                         for a, c in self._bindings.items())

    # —— 对外 ——

    def configure(self, *, bindings: dict | None = None,
                  enabled: bool | None = None) -> dict:
        """更新绑定/开关（不注册）。改完由调用方调 start() 让新配置生效。"""
        with self._lock:
            if bindings is not None:
                self._bindings = dict(bindings)
            if enabled is not None:
                self._enabled = bool(enabled)
            return self.state()

    def start(self) -> dict:
        """（重新）注册全部热键。幂等：重复调用先注销旧的。

        返回 state()。**任何失败都只体现在 state 里**，绝不抛出——热键是锦上
        添花，不能因为它让服务起不来（生命线）。"""
        with self._lock:
            self._unregister_all()
            if not self._enabled:
                self._active, self._reason = False, "disabled"
                log.info("[hotkeys] 全局快捷键已在配置中关闭（仅托盘菜单可用）")
                return self.state()
            try:
                import keyboard
            except Exception as e:
                self._active = False
                self._reason = _with_accessibility_hint(
                    f"import: {type(e).__name__}: {e}")
                log.warning(f"[hotkeys] ⚠ 全局快捷键不可用（import keyboard 失败）"
                            f"——已降级为仅托盘操作: {self._reason}")
                return self.state()
            for action, combo in self._bindings.items():
                fn = self._actions.get(action)
                if fn is None:
                    continue
                try:
                    self._handles[action] = keyboard.add_hotkey(
                        combo, self._wrap(action, fn),
                        suppress=False,        # 不吞键（见模块 docstring）
                        trigger_on_release=False)
                except Exception as e:
                    # 全有或全无：清掉已注册的，整体降级
                    self._unregister_all()
                    self._active = False
                    self._reason = _with_accessibility_hint(
                        f"register: {action}={combo}: "
                        f"{type(e).__name__}: {e}")
                    log.warning(f"[hotkeys] ⚠ 全局快捷键注册失败"
                                f"——已降级为仅托盘操作: {self._reason}")
                    return self.state()
            self._active, self._reason = True, None
            log.info(f"[hotkeys] ✅ 全局快捷键已注册: {self._describe()}")
            return self.state()

    def stop(self) -> dict:
        """注销全部热键（服务收尾）。同样不抛出。"""
        with self._lock:
            self._unregister_all()
            if self._active:
                log.info("[hotkeys] 全局快捷键已注销")
            self._active = False
            self._reason = "stopped"
            return self.state()

    def state(self) -> dict:
        """给 status / 设置页的快照（JSON 可序列化）。
        hint = 本平台授权说明（macOS 非空；其它平台空串 → 前端不显示，文案不变）。"""
        with self._lock:
            return {"enabled": self._enabled, "active": self._active,
                    "reason": self._reason, "bindings": dict(self._bindings),
                    "labels": dict(BINDING_LABELS), "hint": accessibility_hint()}
