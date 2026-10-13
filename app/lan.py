# -*- coding: utf-8 -*-
"""lan.py — §7 手机扫码同屏：绑定决策 / 主网卡探测 / 配对二维码。

三件事各自独立可测，本模块**不碰鉴权与 HTTP**——「仅本机可读」的判据在
main.is_loopback_peer（端点自己再判一次 socket 对端，见 /api/lan-qr）：

1. **绑定决策**（lan_bind / pick_port）：lan_enabled=true → 监听 0.0.0.0（优先
   8787，被占则换一个空闲端口）；false → 只听 127.0.0.1。监听地址在 uvicorn
   启动那一刻定死、进程内不可热改，所以改键**必须重启才生效**——端点的
   restart_required 与面板文案都以此为准（不许写「立即生效」）。
2. **主网卡地址**（primary_lan_ip）：UDP connect 探测。UDP 的 connect 只查路由表、
   给本地端选一个源地址，**不发一个包**（不是对外请求、不需要外网可达，离线也成立）。
3. **配对二维码**（qr_svg）：qrcode 的 SvgPathImage 出 SVG 字符串——纯 Python，
   不需要 Pillow（Pillow 只在 qrcode 的 pil/all extra 里）。延迟导入：缺依赖时
   端点回明确错误，而不是让整个服务起不来。

端口事实源：URL 里的端口必须是**内核真正绑上的那个**（8787 被占时会换）——调用方
把实际值记进 main.runtime['bind']（main.set_bind / desktop.start_server），端点
再优先以请求 scope['server'][1] 为准（同一事实的两个来源，永远取当下的那个）。
"""
import ipaddress
import io
import logging
import socket
from urllib.parse import unquote

log = logging.getLogger("lan")

LAN_ALL_HOST = "0.0.0.0"        # 「所有网卡」= 手机同屏开启时的监听地址
LAN_LOOPBACK_HOST = "127.0.0.1"  # 关闭态 = 现状语义（只有本机能连）
LAN_PREFERRED_PORT = 8787        # 手机同屏的首选端口（README 里的老地址保持有效）

#: UDP 探测目标：只要有一个能选路就够。三个都试是为了「内网/断网/只有 IPv6 出口」
#: 这些环境下也能拿到主网卡地址；connect 不发包，所以这不是「访问外部服务」。
#: 端口取 53（DNS）：任何网络设备都放行 UDP 53，且不需要真的通。
PROBE_TARGETS = (("8.8.8.8", 53), ("1.1.1.1", 53), ("223.5.5.5", 53))


# —— 1. 绑定决策（纯逻辑 + 真 bind 探测） ——

def lan_bind(cfg: dict, preferred_port: int | None) -> tuple[str, int]:
    """config → (监听地址, 首选端口)。

    lan_enabled=true → 0.0.0.0（手机同屏）；此时 preferred_port 为 None/0 用首选
    8787（对外地址稳定，README 里的老地址继续有效）。false → 127.0.0.1（现状语义：
    不对外），端口**原样透传**——0 表示让内核挑一个空闲端口（桌面关闭态就是这样，
    窗口只走 127.0.0.1，不必固定端口）。

    **只在启动时读一次**：uvicorn 的监听地址在进程生命周期内是固定的，改键后必须
    重启服务才换绑（/api/lan-qr 会如实回 restart_required=true）。"""
    if bool((cfg or {}).get("lan_enabled")):
        return LAN_ALL_HOST, int(preferred_port or LAN_PREFERRED_PORT)
    return LAN_LOOPBACK_HOST, (LAN_PREFERRED_PORT if preferred_port is None
                               else int(preferred_port))


def is_lan_host(host: str | None) -> bool:
    """监听地址是否「对外」（非环回）。0.0.0.0 = 所有网卡 = 对外；127.0.0.1 = 仅本机。
    解析不了（None/空串/域名/异常）→ False：宁可显示成「没开」，也不谎报「已开」。"""
    if not host:
        return False
    try:
        return not ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def can_bind(host: str, port: int) -> bool:
    """能否独占绑定 (host, port)。**故意不设 SO_REUSEADDR**：Windows 下两个都设了
    SO_REUSEADDR 的 socket 可以绑同一端口（后绑的把流量"抢"过去），探测就失去意义；
    不设则是独占语义（已被占 → bind 失败），这正是要问的问题。返回前立即关闭——
    既没 listen 也没连过，不留 TIME_WAIT，端口能被 uvicorn 立刻重新绑上。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, int(port)))
        return True
    except OSError:
        return False


def free_port(host: str) -> int:
    """让内核挑一个空闲端口号（bind 0 → getsockname）。同样 bind 完即关。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def pick_port(host: str, preferred: int) -> int:
    """「优先 preferred，占用则实际端口」（§7）的落地。preferred<=0 = 交给内核（返回 0）。

    返回的是**端口号**而非已绑好的 socket：探测与 uvicorn 真正 bind 之间有微秒级
    窗口，万一那一步被抢走，uvicorn 会自己报 "address already in use" 并退出——
    宁可响亮地失败（日志里看得见），也不静默换一个用户不知道的端口。"""
    preferred = int(preferred)
    if preferred <= 0 or can_bind(host, preferred):
        return preferred
    alt = free_port(host)
    log.warning(f"[lan] ⚠ 端口 {preferred} 已被占用 → 改用 {alt}"
                f"（配对 URL 里的端口以此为准）")
    return alt


# —— 2. 主网卡地址（UDP connect 探测，不发包） ——

def ip_usable(ip: str | None) -> bool:
    """这个地址能不能写进配对 URL。纯函数，供单测。

    放行：合法 IPv4、非环回、非未指定(0.0.0.0)、非链路本地(169.254.x.x——APIPA
    自配地址，正是「网线没插好/WiFi 没拿到 DHCP」的表现，写进二维码手机永远连不上)、
    非组播/保留。**私有段与公网段都放行**：校园网/eduroam 可能直接给公网地址，
    那恰恰是手机要连的那个地址。（本模块不发任何请求，"只允许 http/https + 拒绝
    私有地址"那套 SSRF 纪律管的是按用户输入去请求 URL，与这里无关。）"""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    if addr.version != 4:
        return False
    return not (addr.is_loopback or addr.is_unspecified or addr.is_link_local
                or addr.is_multicast or addr.is_reserved)


def primary_lan_ip(targets=PROBE_TARGETS) -> str | None:
    """探测主网卡地址；拿不到回 None（调用方如实显示「没探测到局域网地址」）。

    UDP connect 的语义：只查路由表并给本地端选一个源地址，**一个包都不发**。
    拿到的是「默认路由走的那块网卡」的地址——手机要连的正是它（而不是 VPN/
    虚拟网卡那一堆别的地址）。探测失败（无路由/无网卡）不抛异常。"""
    for ip, port in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.3)
                s.connect((ip, int(port)))
                cand = s.getsockname()[0]
        except OSError as e:
            log.debug(f"[lan] UDP 探测 {ip}:{port} 失败: {e}")
            continue
        if ip_usable(cand):
            return str(cand)
        log.debug(f"[lan] 探测到不可用地址 {cand}（环回/链路本地），继续试下一个目标")
    return None


# —— 3. 配对 URL 与二维码 ——

def lan_url(ip: str, port: int, token: str | None) -> str:
    """配对 URL：`http://<LAN-IP>:<port>/#t=<token>`。

    token 放 **hash 片段**（`#t=`）而不是查询串：hash 不会随请求发给服务端、不进
    服务端访问日志、也不进 Referer；手机端页面加载时读 location.hash → 存
    localStorage → 立刻清掉 hash（§7 前端口径），此后免输。

    不 percent-encode：token 由 secrets.token_urlsafe 生成（字母数字 + `-`/`_`），
    本身就是 URL 安全字符；编码过的值存进 localStorage 反而与真 token 对不上。"""
    base = f"http://{ip}:{int(port)}/"
    return f"{base}#t={token}" if token else base


def token_from_hash(url_or_fragment: str | None) -> str:
    """从配对 URL / URL 片段里解出 token（`#t=…`）；没有 → `""`。

    **与前端同一口径**（§7②）：二维码 URL 把 token 放在 hash 段（浏览器绝不发 hash，
    故它不长在服务端日志/Referer 里），页面加载时读 `location.hash` 的 `t=` 存进
    localStorage 再清 hash。前端那份实现在 app/web/index.html（正则
    `/(?:^#|&)t=([^&]+)/`），本函数是 Python 侧的同款实现——smoke 用它把**服务端刚
    给出的 URL** 里的 token 取出来再拿去鉴权，等于把「手机上扫到的码真能用」这件事
    里唯一可自动化的部分钉住（真机扫码本身是手动清单项）。

    接受的形态（与前端一致，宽松是为容错、不是鼓励）：
    `http://ip:port/#t=x`、`#t=x`、`t=x`、`#a=1&t=x`、含百分号编码的 `t=%78`
    （token 恒为 URL 安全字符，解码只是防御性兜底）。取**第一个** `t=`。"""
    s = str(url_or_fragment or "")
    if "#" in s:
        s = s.split("#", 1)[1]          # 整条 URL → 只留片段
    for part in s.split("&"):
        if part.startswith("t="):
            raw = part[2:]
            try:
                return unquote(raw)
            except Exception:           # pragma: no cover — unquote 实际不抛
                return raw
    return ""


def qr_svg(data: str) -> str:
    """把 data 编成 SVG 二维码字符串（含 <svg>/<path>，带 viewBox，可随 CSS 缩放）。

    用 SvgPathImage（纯 Python 拼字符串）——不经过 Pillow。纠错级 M（约 15% 容错，
    手机对着屏幕扫的常规选择）；border=2 是二维码规范要求的最小静默区（少了会扫不出）。
    缺依赖时抛 RuntimeError：端点据此回一句人话（并照常给 URL），而不是 500 裸栈。"""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except Exception as e:  # pragma: no cover — 依赖已在 requirements.txt 钉住
        raise RuntimeError(f"二维码依赖不可用（缺 qrcode，装法："
                           f"pip install qrcode）: {e}") from e
    qr = qrcode.QRCode(border=2, box_size=10,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
