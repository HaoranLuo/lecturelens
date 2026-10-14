# -*- coding: utf-8 -*-
"""system_info.py — B8：系统级**只读事实**（当前只有供电状态）。

为什么单开一个模块而不塞进 tiers.py：tiers 管的是「识别硬件 → 推荐档位」（选型，
结果要进推荐理由），而这里每次问的都是**此刻**的状态（插电还是电池供电），会被
/api/status 每轮现算。同类查询（休眠/锁屏之类）将来加在这里，不动档位逻辑。

口径（对用户的承诺是「拿不准就不说话」）：
- **只在 Windows 上读**：`kernel32!GetSystemPowerStatus`（ctypes，零新依赖；照
  tiers._ram_gb 的模板：定义结构体 + byref 调用，任何异常都吞掉返回 None）；
- 非 Windows（含 Mac）一律 None——电池提醒只在 Windows 上生效；Mac 的电源 API 是
  另一套（IOKit/pmset），届时按 `sys.platform` 加分支，函数签名与语义不变；
- ACLineStatus 只认 1（插电）/ 0（电池），255（未知）与调用失败一律 None：
  **说不准就不提醒**，绝不把"不知道"当成"在用电池"（误报比不报烦人）。
"""
import ctypes
import sys

#: GetSystemPowerStatus.SYSTEM_POWER_STATUS.ACLineStatus 的取值
AC_ONLINE = 1       # 交流电（插电）
AC_OFFLINE = 0      # 电池供电
AC_UNKNOWN = 255    # 未知（读不到/不支持的机器）


def on_battery() -> "bool | None":
    """当前是否**电池供电** → True / False；读不到或非 Windows → None。

    绝不抛异常、绝不阻塞（一次本地 syscall，µs 级）——它挂在 /api/status 上，
    出错只降级成"不提醒"，不该影响任何请求。"""
    if sys.platform != "win32":
        return None
    try:
        class _PowerStatus(ctypes.Structure):
            # SYSTEM_POWER_STATUS：4 个 BYTE + 2 个 DWORD（顺序/宽度固定，勿改）
            _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                        ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]

        st = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None                       # 调用失败（极罕见）：不猜
        if st.ACLineStatus == AC_OFFLINE:
            return True
        if st.ACLineStatus == AC_ONLINE:
            return False
        return None                           # 255 / 其它：未知 → 不提醒
    except Exception:
        return None
