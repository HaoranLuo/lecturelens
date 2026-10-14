# Mac 构建与实测指南

> 面向：把 LectureLens 跑在 Mac 上的人（开发机是 Windows，所以这份文档是 Mac 侧的唯一操作口径）。
> 范围：**Apple Silicon（arm64）为首选**；Intel Mac 能跑（走 CPU 推理），但没有 MLX 加速。
> 文档里的路径/ID/命令都与代码里的唯一事实源对齐（`tests/test_mac_guards.py` 有断言钉住，
> 改代码忘改文档会立刻红）。

---

## 0. 一分钟速览

| 要做的事 | 命令 |
|---|---|
| 跑起来（开发态） | `.venv/bin/python app/desktop.py` |
| 无窗口自检（门禁） | `.venv/bin/python app/desktop.py --check` → 退出码 0 |
| 全管线自检 | `.venv/bin/python scripts/smoke_test.py` |
| 打包 .app | `.venv/bin/python scripts/build_macos.py` |
| 验证产物 | `.venv/bin/python scripts/verify_dist.py` |
| 数据在哪 | `~/Library/Application Support/LectureLens`（日志在同目录 `logs/lecturelens.log`） |

---

## 1. 前置

1. **Xcode Command Line Tools**（PyInstaller 收集二进制、`codesign` 签名的前置）
   ```bash
   xcode-select --install
   xcode-select -p          # 应打印 /Library/Developer/CommandLineTools
   ```
2. **Homebrew**（装 Python 与 BlackHole 用；官网 brew.sh 有安装命令）
   ```bash
   brew --version
   ```
3. **Python 3.13**
   ```bash
   brew install python@3.13
   python3.13 --version
   ```
   或者用 python.org 的官方安装包（任选其一，本项目不挑来源，只要求 ≥3.11 且能建 venv）。
4. **BlackHole**（只有要用「音源 = 系统声音内录」时才必需，麦克风模式不需要）
   ```bash
   brew install blackhole-2ch            # 装完设备名就是 “BlackHole 2ch”
   ```
   装好后到「音频 MIDI 设置」把系统输出指到 BlackHole（或用「多输出设备」同时听+录）。
   应用侧的判定：设置页「音源设备」下拉里能看到 BlackHole（`/api/devices` 列出的就是它），
   不填则自动按名字找 `BlackHole 2ch`。

---

## 2. 首次配置（系统授权）

macOS 的权限是**按 App 身份**发的，所以分两种情况——这也是 Mac 上最容易卡住的一步：

| 运行方式 | 授权对象 | 在哪授权 |
|---|---|---|
| 开发态（`.venv/bin/python app/desktop.py`） | **终端 App**（Terminal / iTerm） | 系统设置 → 隐私与安全性 → **麦克风** / **辅助功能** |
| 打包版（`LectureLens.app`） | **LectureLens**（`com.lecturelens.app`） | 同上，列表里会出现 LectureLens |

- **麦克风**：首次采集时系统弹窗 → 允许；被拒过就手动去上面那两处勾上（改完要重启应用）。
  打包版的弹窗文案来自 `Contents/Info.plist` 的 `NSMicrophoneUsageDescription`（由
  `scripts/build_macos.py` 注入，中文）。
- **辅助功能**：全局快捷键（Ctrl+Alt+L 暂停/继续、Ctrl+Alt+S 悬浮字幕条）需要它——
  没有这项授权，热键会**静默收不到按键**（应用会在设置页状态行如实写明原因）。
- 内录（BlackHole）**不需要**额外授权：它是普通音频输入设备。
- 不需要屏幕录制/输入监控权限。

---

## 3. 开发态运行

```bash
cd <项目目录>                                   # 例：~/projects/lecture-translate
python3.13 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt        # 运行依赖
.venv/bin/python -m pip install -r requirements-dev.txt     # 跑测试/打包才需要
```

依赖的平台差异（`requirements.txt` 里用 marker 写死，不用手工挑）：

- `pyaudiowpatch` → **只在 Windows 装**（WASAPI 环回；macOS 的内录走 BlackHole，见 §1.4）
- `mlx-whisper` → **只在 macOS 装**（Apple Silicon 的加速后端；Intel Mac 装了也用不上，
  应用会自动回落 CPU。想确认：`.venv/bin/python -c "import mlx_whisper"`）

启动与自检：

```bash
.venv/bin/python app/desktop.py                # 桌面窗口 + 系统托盘 + 悬浮字幕条
.venv/bin/python app/desktop.py --minimized    # 启动即进托盘（开机自启项用的就是这个）
.venv/bin/python app/desktop.py --check        # 无窗口自检：全过打印 [check] ALL OK、退 0
.venv/bin/python app/desktop.py --server-only  # 纯服务模式（浏览器访问 127.0.0.1:8787）
.venv/bin/python scripts/smoke_test.py         # 全管线自检（TTS 走 macOS 的 say + afconvert）
.venv/bin/python -m pytest tests/ -q           # 单测（含全部 Mac 守卫用例）
```

**数据落在哪（重要）**：macOS 上**不在** .app/项目目录里，而是

```
~/Library/Application Support/LectureLens/
  config.json  courses.json  transcripts/  logs/  glossary/  .env  webview_profile/
```

只读资源（网页、术语库包）随包在 `Contents/Resources` 下，两者互不混。
换机器/备份/清理都只认上面这个目录。

---

## 4. 构建 .app 全流程

```bash
.venv/bin/python scripts/build_macos.py                 # 构建 + 注入 Info.plist + ad-hoc 签名 + 产物断言
.venv/bin/python scripts/build_macos.py --check-dist    # 只对既有 dist/LectureLens.app 跑产物断言
.venv/bin/python scripts/build_macos.py --no-sign       # 跳过签名（纯调试用）
```

脚本做的事（顺序即代码顺序）：

1. **图标**：`assets/icon.icns` 不存在时，用 `iconutil` 由 `assets/*.png` 现场生成；
   生成失败只降级成 PyInstaller 默认图标（不拦构建）。手工生成命令：

   ```bash
   mkdir -p /tmp/ll.iconset
   cp assets/icon_16.png  /tmp/ll.iconset/icon_16x16.png
   cp assets/icon_32.png  /tmp/ll.iconset/icon_16x16@2x.png
   cp assets/icon_32.png  /tmp/ll.iconset/icon_32x32.png
   cp assets/icon_64.png  /tmp/ll.iconset/icon_32x32@2x.png
   cp assets/icon_128.png /tmp/ll.iconset/icon_128x128.png
   cp assets/icon_256.png /tmp/ll.iconset/icon_128x128@2x.png
   cp assets/icon_256.png /tmp/ll.iconset/icon_256x256.png
   iconutil -c icns /tmp/ll.iconset -o assets/icon.icns
   ```

2. **PyInstaller**（`--onedir --windowed`，入口 = `app/desktop.py`，`--osx-bundle-identifier
   com.lecturelens.app`；`--add-data` 用 `:` 分隔）。
3. **Info.plist 注入**：`CFBundleShortVersionString` / `CFBundleVersion` = `branding.APP_VERSION`，
   以及 `NSMicrophoneUsageDescription`（麦克风授权弹窗的文案）。
4. **ad-hoc 签名**：`codesign --force --deep -s - dist/LectureLens.app`。
5. **产物断言**：`Contents/MacOS/LectureLens`、三个页面、术语库包齐备；且 `Contents/` 里
   **不得**出现 config.json / transcripts / .env 等用户数据（出现即构建失败）。

产物结构（`dist/LectureLens.app`）：

```
Contents/
  MacOS/LectureLens                 可执行（--check / --server-only / --minimized 都在这儿）
  Frameworks/…                      Python 运行时 + 依赖（= sys._MEIPASS）
  Resources/web/…                   三个页面（只读）
  Resources/glossary_packs/…        术语库包（只读）
  Info.plist                        标识/版本/麦克风用途说明
```

验证产物（拷贝到临时目录跑真产物，顺便验证可搬移；只读断言用户数据目录，不删改）：

```bash
.venv/bin/python scripts/verify_dist.py
```

> Windows 侧的打包是另一条链：`scripts/build_exe.py`（PyInstaller onedir → `dist/LectureLens/LectureLens.exe`，
> 随包 small 模型、可选 GPU DLL）。两个脚本各自的产物断言互不干扰，别拿 Windows 的 dist 去跑本脚本。

**模型不随包**（与 Windows 版不同）：首次运行按档位从 HuggingFace 下载（默认落
`~/.cache/huggingface/hub`）。国内慢就在设置页打开「模型下载走 hf-mirror.com 镜像」并**重启应用**；
想换盘就在启动前 `export HF_HOME=~/ll-models`（写进 shell 配置或 LaunchAgent）。

---

## 5. Gatekeeper：把 .app 拷到别的 Mac 上打开

| 来源 | 现象 | 处理 |
|---|---|---|
| 本机构建、本机打开 | 直接能开 | 什么都不用做 |
| 网盘 / U 盘 / AirDrop 传过去 | 「无法打开，因为 Apple 无法检查它是否包含恶意软件」 | 右键（或按住 Control 点击）→ **打开** → 再点「打开」；之后这台机器就记住了 |
| 上面那步没出现、提示「已损坏」 | 多半是隔离属性惹的祸，**不是真损坏** | `xattr -l /Applications/LectureLens.app` 看一眼；确认是 `com.apple.quarantine` 就执行 `xattr -dr com.apple.quarantine /Applications/LectureLens.app` |

原因：这个包是 **ad-hoc 签名**（`codesign -s -`），没有 Apple 开发者证书、也没有公证（notarization）。
本机自用足够；**公开分发要真证书 + 公证**——那属于商业化项，按红线推迟到毕业后评估。

---

## 6. 真机验收清单（逐项打勾）

> 门槛：前 2 项必须全绿才算「能用」；其余是 Mac 专属能力，逐项确认一遍再交给朋友。

- [ ] **1. `--check` 退 0**
  `.venv/bin/python app/desktop.py --check`（打包版：`dist/LectureLens.app/Contents/MacOS/LectureLens --check`）
  → 末行 `[check] ALL OK`；10 步全 OK（依赖导入/托盘图标/状态机/菜单/热键白名单/服务线程/HTTP 探活//float/js_api 桥）。
- [ ] **2. MLX 推理能跑 + RTF 记一笔**
  启动后看字幕页工具条徽章：`device` 应是 **MLX**（不是 CPU）；说话出字幕。
  用 `.venv/bin/python scripts/smoke_test.py` 的 ASR 段（整句 RTF）与流式段（partial 首字时间）
  各记一个数，和 Windows/CPU 口径对比留档。设置页「识别模型」卡会显示 `MLX · <档位> · float16`。
- [ ] **3. BlackHole 内录端到端**
  装好 BlackHole 并把系统输出指过去 → 设置页「音源」选**系统声音内录**、音源设备选 BlackHole →
  播一段有声内容（视频/音乐）→ 字幕应出现（不需要对着麦克风说话）。日志里会打
  `[loopback] 内录设备: BlackHole 2ch @ 48000Hz × 2ch → downmix → 16000Hz（silero VAD）`。
- [ ] **4. launchd 开机自启**
  设置页 ⚙ 系统 →「开机自动启动」打开 → 检查 `~/Library/LaunchAgents/com.lecturelens.plist`
  存在（`plutil -p` 能看到 `RunAtLoad => 1`、`KeepAlive => 0`）→ 关掉应用 → 注销再登录（或
  `launchctl kickstart gui/$(id -u)/com.lecturelens`）→ 托盘图标出现（主窗不弹）。
  再关掉开关 → plist 消失、`launchctl list | grep lecturelens` 为空。
- [ ] **5. 托盘**
  左键/双击切换主窗显隐；菜单项：悬浮字幕条（勾选态跟随）、点击穿透（macOS 上是降级项）、
  暂停/继续字幕、打开字幕页、退出。**退出必须是真退出**（进程消失，不被 launchd 拉起）。
  图标右下状态灯随状态变色（错误红 > 暂停黄 > 运行绿）。
- [ ] **6. 悬浮字幕条：置顶 + 拖拽 + 第二块屏**
  设置页 🎨 外观打开悬浮条 → 拖动它（整窗可拖）→ 切到别的 App，它应仍在最前；
  在第二块屏上摆放并拖动 → 退出应用再启动，位置应被记住（第二块屏拔掉后回默认位、不消失）。
  ⚠ macOS 上**点击穿透恒为降级**（`📌` 会提示不可用、浮窗保持可关闭）——见 §7。
- [ ] **7. 手机同屏扫码**
  设置页 📶 连接打开「手机同屏」→ **重启应用** → 字幕页工具条「📱 手机同屏」面板出现二维码 →
  同一 WiFi 的手机扫码 → 手机上看到字幕（首次要在手机上粘贴一次 token，页面会记住）。
  状态行显示「已连接 N 台设备」（只数局域网设备，本机窗口不计）。
- [ ] **8. PDF / Word 中文导出**
  笔记库 → 选一篇 → 导出 PDF：中文不是豆腐块（字体来自系统 PingFang / Hiragino Sans GB）。
  若报「PDF 中文字体缺失」，说明那台机器上没有这两个字体文件（macOS 15+ 可能把系统字体
  收进了 asset bundle）——按报错文案把 `exporters.FONT_REGULAR` 指向本机任意中文字体即可。
- [ ] **9. 关掉应用后无残留**
  `ps aux | grep -i lecturelens` 无残留；`~/Library/LaunchAgents` 里没有意外多出来的项；
  用户数据目录仍是数据（不随 .app 删除而消失）。

---

## 7. 已知限制（Mac 侧，如实交代）

- **点击穿透仅 Windows**：macOS 上没有 `WS_EX_TRANSPARENT` 这套窗口样式，`📌` 会明确降级
  （浮窗保持可点击可关闭）；托盘菜单的同类项同样不生效。
- **多屏位置记忆不设防**：macOS 侧拿不到 `GetSystemMetrics` 的虚拟屏包围盒（pywebview 的
  screen API 要等 start() 之后才有值），所以记忆的窗口/悬浮条坐标**原样采信**——拔掉第二块屏后
  可能落在屏外，由窗口管理器兜底；把主窗拖回可见区域即可（再次退出时会被记住）。
- **内录要 BlackHole**：Windows 的 WASAPI 环回是系统自带，macOS 必须装虚拟声卡（§1.4）。
- **MLX 后端没有 beam search**：`mlx_whisper` 未实现，流式路径回落贪心解码（日志会说明一次）；
  识别质量与 Windows/CUDA 的 `beam=5` 略有差别，这是上游能力差异，不是本项目的取舍。
- **未签名 / 未公证**：换机器首次打开要右键「打开」或清 `com.apple.quarantine`（§5）。
- **没有安装器、没有自动更新**：分发形态就是 `.app` 一个包（删掉即卸载，数据在
  `~/Library/Application Support/LectureLens` 不跟着删）。自动更新是**明确不做**的项。
- **权限按 App 身份**：开发态与打包版的授权对象不同（§2）；打包版改了 `Info.plist` 里的
  bundle id 就等于换了个身份，用户要重新授权。

---

## 8. 反馈方式（把这几样发回）

1. **日志**：`~/Library/Application Support/LectureLens/logs/lecturelens.log`
   （开发态在 `<项目目录>/logs/lecturelens.log`）。⚠ 日志里可能有课程名/片段，先自己扫一眼。
2. **复现步骤**：点了什么 → 期望什么 → 实际什么（能截图更好）。
3. **环境**：`sw_vers`（系统版本）+ `uname -m`（arm64/x86_64）+ 是否装了 BlackHole +
   设置页 ⚙ 系统 的「数据去向」显示的实际路径。
4. 若是**打包版**问题，附上 `dist/LectureLens.app/Contents/Info.plist` 里的
   `CFBundleShortVersionString`（版本号）与 `--check` 的完整输出。

---

## 9. 相关文档

- `README.md` — 功能总览与「数据放在哪」
- `scripts/build_macos.py` 文件头 — .app 布局与图标/签名的逐条说明
- `CLAUDE.md` — 结构与平台口径（含 Windows 侧事实的平台标注）
- `docs/distribution-guide.md` — Windows 安装包分发流程（Mac 侧用本文档）
