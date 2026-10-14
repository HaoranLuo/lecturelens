# LectureLens 课堂透镜 — 留学生课堂实时字幕翻译

> 免费自用 + 朋友工具。**当前阶段（Student visa 禁 self-employment）：免费供本人学习使用，并免费分享给朋友使用（含朋友装机）；不收费、不注册公司**。仓库自 2026-10-14 起**公开**（便于朋友及其 AI 助手直接从 GitHub 安装），但**保留所有权利——未授予开源许可**（见文末「许可」）。未来可能商业化（含对外发布或出售），相关决策**推迟至毕业后**，届时按签证与法律要求评估。

## 一句话

上课时笔记本采集老师语音 → faster-whisper 实时英文识别 → DeepSeek 翻译中文 → 浏览器大字幕（上英下中）→ 课后自动存双语笔记。

## 安装（给朋友 / 新电脑）

> Windows 64 位 · 安装包约 1.41 GB · 不需要管理员权限 · 不需要 GitHub 账号。
> **把下面这段话整段贴给你的 AI 助手**（带执行权限的：ZCode / Claude Code /
> Cursor 等），它会自动完成"下载 → 校验 → 安装 → 自检"：

```text
帮我在本机安装 LectureLens（Windows 课堂实时字幕工具）：
1. 取最新发布：https://github.com/HaoranLuo/lecturelens/releases/latest
   （或调 API：https://api.github.com/repos/HaoranLuo/lecturelens/releases/latest
    的 assets 里取 browser_download_url）
2. 下载其中的 LectureLens-*-setup.exe（约 1.41 GB）和同名 .sha256 文件，
   用 SHA256 校验两者一致；
3. 静默安装：<安装包> /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
   （每用户安装，不需要管理员权限）；
4. 装完运行 %LocalAppData%\LectureLens\LectureLens.exe --check，
   看到 [check] ALL OK 即安装成功；
5. 告诉我安装位置与版本号。
```

不想用 AI 就手装：下载同一安装包，双击 → 若弹蓝色「Windows 已保护你的电脑」→
点「更多信息」→ 再点「仍要运行」（未购买代码签名证书，属正常提示）→ 一路下一步 →
首启三步向导（麦克风测试 → DeepSeek key → 试说一句）。

- **升级 = 覆盖安装**：直接装新版即可，笔记/设置/课程/术语表全部保留（跨版本升级有真实实测）
- 完整性校验值以 Release 页面的 `.sha256` 为准（当前 v1.1.0：`0b41d0a0…b2b3f14`，以文件为准）
- **macOS**：目前**没有**现成安装包（.app 需要在 Mac 上构建，见 [`docs/mac-guide.md`](docs/mac-guide.md)）

## 快速开始

> **平台**：Windows 与 macOS 都支持。下面凡带「Windows」标注的步骤是 Windows 专属；
> macOS 的对应做法就地写在括号里（启动方式、路径、--gpu、防火墙、字体等）。

1. 项目根建 `.env`，写一行 `DEEPSEEK_API_KEY=sk-xxx`（没有 key 也能跑，只出英文字幕不翻译）
2. **Windows**：双击 `start_translate.bat` —— 打开**桌面窗口**（原生窗口 + 系统托盘 + 悬浮字幕条，
   首次自动装依赖，whisper 模型约 250MB 首次自动下载）。关闭窗口 = 隐藏到托盘，
   真退出走托盘菜单；首次启动有 3 步向导（麦克风测试 → DeepSeek key → 试说一句）
   **macOS**：开发态跑 `.venv/bin/python app/desktop.py`（无 .bat）；打包版双击
   `dist/LectureLens.app`（见「打包」一节）。窗口/托盘/悬浮条行为一致
3. 课程术语表：**Windows** `start_translate.bat --glossary glossary/CS.md`；
   **macOS** `.venv/bin/python app/desktop.py --glossary glossary/CS.md`（可多次）
4. 手机同 WiFi 看字幕（📱 手机同屏）：在字幕页/设置页打开「手机同屏」开关（config
   `lan_enabled`）→ **重启 LectureLens**（监听地址只有重启才换）→ 字幕页面板里扫码
   （或访问 `http://<电脑IP>:8787`）。开关默认关：桌面模式与 --server-only 一律只监听
   127.0.0.1，不对外；开启后服务监听 0.0.0.0（8787 被占则自动换端口，以面板/日志为准），
   LAN 访问需携带 token（首启已打印、设置页「安全」卡可重新生成）
5. 对麦克风说英语，字幕出现即成功

## 结构

```
app/
  main.py         FastAPI + WebSocket 服务 + 双语笔记落盘
  capture.py      麦克风 16k 采集 + VAD 分句（默认 Silero，备选能量门限）
  asr.py          faster-whisper 封装（CPU int8 默认，GPU 可选；macOS arm64 走 MLX）
  translate.py    DeepSeek 翻译 + 课程术语表注入（含 whisper initial_prompt）
  web/index.html  字幕页（断线自动重连 + 底部麦克风/翻译状态）
glossary/         课程术语表（每行 `EN term = 中文译名`）
scripts/
  smoke_test.py   全管线自检（无需麦克风：VAD + TTS→ASR + 翻译 + 服务）
  build_exe.py    Windows 打包（PyInstaller onedir → dist/LectureLens/LectureLens.exe）
  build_macos.py  macOS 打包（PyInstaller onedir + .app → dist/LectureLens.app，含图标/
                  Info.plist/ad-hoc 签名）——**只在 macOS 上跑**，Windows 上直接拒绝
transcripts/      每节课双语笔记（gitignore：含课程内容，不外传）
```

**数据放在哪（桌面版）**：**Windows** = 安装目录（exe 旁边：`config.json` / `transcripts/` /
`logs/` / `glossary/`）；**macOS** = `~/Library/Application Support/LectureLens`（.app 是
签名覆盖的只读包体，用户数据写进去会破坏签名、升级换 .app 也会把数据一起换掉）。
两侧的「设置页 → ⚙ 系统 → 数据去向」都写着实际位置。

## 使用

```
# 以下命令 Windows 的 python 路径是 .venv/Scripts/python.exe、macOS 是 .venv/bin/python
python app/main.py --glossary glossary/CS.md        # 课堂模式
python app/main.py --gpu                             # 强制 CUDA（仅 Windows/NVIDIA；macOS 的
                                                     #  加速后端是 MLX，自动启用，见 asr.py）
python app/main.py --model base                      # 临时换模型（CLI 覆盖 config.json）
python app/main.py --vad energy                      # 噪声底自适应的能量门限（默认 silero）
python app/main.py --list-devices                    # 列出麦克风设备（配 --device 用）
.venv/Scripts/python.exe scripts/smoke_test.py       # 不用麦克风的自检（macOS: .venv/bin/python）
.venv/Scripts/python.exe scripts/smoke_test.py --gpu # GPU 基准（RTF + 显存；仅 Windows，
                                                     #  非 Windows 会如实打印「该段仅 Windows」跳过）
```

### 配置（config.json）

模型/设备/VAD 持久化在项目根 `config.json`，字幕页可热切换（无需重启）。首次部署从模板复制：`copy config.example.json config.json`（键集合以 app/main.py 的 `ALLOWED_CONFIG_KEYS` 白名单为准，非法值会被 /api/config 拒绝且不落盘）：

```json
{ "model": "large-v3-turbo", "device": "auto", "compute_type": null, "vad": "silero" }
```

- **GPU 本地顶配（Windows / NVIDIA）**（RTX 5060 8GB 实测）：large-v3-turbo / float16，7.5s 音频 0.35s 出结果（RTF 0.05），显存 2.2/8.0 GB。依赖：`nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（已装在 .venv）。**macOS** 上是另一条路：Apple Silicon 自动用 MLX（`mlx-whisper`，见 `app/asr.py`），Intel Mac / 没装 mlx-whisper 时回落 CPU，行为与 Windows 的 CPU 档一致
- CLI 参数（--model/--gpu/--vad）只当次生效，不改文件；字幕页切换会写回 config.json
- **模型档位**：首次启动自动检测硬件并推荐一档——流畅(base)/标准(small)/高(large-v3-turbo)/极致(large-v3)，界面只显示档位名（原名收在设置页「高级」）；设置页可一键「下载并使用」任一档（140MB–2.9GB，首次需联网）。用户手动选过之后永远以用户为准
- **模型放在哪（磁盘占用别踩坑）**：**随包模型**在**安装目录**里（`models/<档位>`，离线可用、不占额外空间——打包版首启默认用的就是它；**macOS 打包版不随包模型**，走下面的 HF 缓存/在线下载）；**点「下载并使用」的更高档位**则落到 HuggingFace 缓存的默认位置，**Windows** 上是 `C:\Users\<用户名>\.cache\huggingface\hub`、**macOS** 上是 `~/.cache/huggingface/hub`（实测：开发机缓存在 D 盘只是因为设了 `HF_HOME`，**朋友机器没有这个环境变量**——「C 盘零占用」只对开发机成立）。设置页「识别模型」卡常显实际路径与占用（`缓存目录：… · 共占用 …`）；想换盘就在**启动前**设环境变量 `HF_HOME`（**Windows**：`set HF_HOME=D:\ll-models`；**macOS**：`export HF_HOME=~/ll-models`，或写进 LaunchAgent/shell 配置），缓存位置随它走
- **国内网络下载慢**：设置页「识别模型」卡的「模型下载走 hf-mirror.com 镜像」开关（只此一个预设镜像，不接受自定义地址）；**改完要重启应用**才换源（HF 的 endpoint 在库导入时读取）
- **export_dir**（设置页「导出」卡）：导出目录的绝对路径（或 `null` = 浏览器下载）。
  写入时实测校验「可创建 + 可写」并拒绝系统关键目录（**Windows**：系统盘根、
  `%SystemRoot%`、`Program Files`、`ProgramData` 等；**macOS 等 POSIX**：`/`、
  `/etc`、`/usr`、`/bin`、`/sbin`、`/boot`、`/sys`、`/proc`、`/dev`——`/System`、
  `/Library` 这类只读/需提权的目录则会在「可创建 + 可写」那一步被拒），
  非法值 400 且配置零污染
- **上次那门课会被记住（B5）**：选过的课程写进 `courses.json` 的 `active` 跨重启保留；
  启动时读回来挂到当前课程（字幕页工具条下拉显示的、以及自动新建的那篇笔记头里的
  课程名）。**不额外维护前端状态**——这就是"打开软件还是上次那门课"的全部机制
  （回归断言见 `tests/test_courses.py::test_active_course_survives_restart`）
- **设置页五分区（B12）**：设置按用途分五格标签页——🎙 上课（识别模型/音源/识别语言/高级·分句调优/流式字幕/DeepSeek key）、
  📝 笔记（课程管理/下课归档/导出）、📶 连接（手机同屏/安全）、🎨 外观（字幕显示/悬浮字幕条）、
  ⚙ 系统（全局快捷键/开机自启/数据去向）。**记住上次那格**（浏览器本地存储，键 `ll_settings_tab`），
  手机同屏面板的「去设置」可**直达 📶 连接**。分区只影响设置页的可见性，不改任何 config 键

### 字幕页

- 顶部工具条（可折叠）：暂停/继续收音、**下课**（见下）、**课程选择**（见下）、模型热切换、VAD 电平条（黄线=触发门限）、字号调节、导出笔记、清屏、全屏
- **下课一键归档（§5）**：工具条「🎓 下课」按顺序做完三件事，结果**逐条**列在面板里
  （单步失败不中断后续）：① 暂停收音 ② 生成课后 AI 摘要（已有摘要则跳过；没有 DeepSeek
  key 则跳过并说明；正在生成则等它完成）③ 导出这篇笔记（Markdown；设了导出位置就落盘到
  那里，否则浏览器下载）。二次确认默认开（会写清楚三步做什么），设置页「下课归档」卡
  可关掉；还没上过课（没有当前笔记）时只提示、不空转。
- **选课程**（上课态工具条「课程」下拉）：列出全部课程 + 「未分类」+「＋ 新建课程…」（内联输入，回车即创建并选中）。选中写入 `courses.json` 的 active 并落到当前笔记归属——**还没开始记录 = 改写笔记头的课程行；已在记录 = 追加一行切换标记**（笔记主归属仍是开课那门课，不让前半段说谎）。与侧栏同源：另一窗口改课、或服务端 `status.course` 变化，这里都会跟着刷新（WS 即时 + 轮询兜底）。**选课只在「上课的地方」**（本下拉 + 笔记库左侧课程列表）；设置页那张卡是纯**课程管理**（B14）：每行 = 色点 + 课程名 + 统计「N 篇笔记 · M 条术语」（术语数 = 读该课绑定术语表的真文件），行内改名 / 换色 / 绑定术语表 / 删除，**点行（或「查看笔记」）跳到笔记库并筛出该课**；删除课程会把它的笔记归属改回「未分类」（确认框写明几篇笔记；笔记文件一律保留）
- 收起工具条即纯字幕沉浸模式；字号记忆在浏览器
- 上英文下中文，自动滚动；翻译到达稍晚于英文 1-3 秒（正常）
- **音源可切换（设置页 🎙 上课 → 音源）**：`mic`（麦克风，默认）或 `loopback`（系统声音内录）。
  **Windows** 走 WASAPI 环回（默认扬声器，免装驱动）；**macOS** 需要先装免费的虚拟声卡
  **BlackHole**（`brew install blackhole-2ch`）并在「音频 MIDI 设置」把系统输出指到它——
  没装时内录会明确报错并给出这段指引（不会静默录到空音）；设备可在设置页的音源设备下拉里选
  （/api/devices 列出的就是它）
- **开课前看一眼电平条随说话跳动就说明收音正常**；导出 = 下载当前 `transcripts/*.md`
- 手机连同一 WiFi 看字幕：先在字幕页/设置页打开「📱 手机同屏」（默认关，开启后**重启**生效），面板里扫码即可（也可手输 `http://<电脑IP>:8787`）；打不开多半是**防火墙**拦了入站——**Windows**：首次运行允许"专用网络"；**macOS**：系统设置 → 网络 → 防火墙里允许 Python/LectureLens 接受传入连接；两侧都要确认同一 WiFi

手机访问（手机同屏）：电脑和手机连同一 WiFi，在字幕页/设置页打开「📱 手机同屏」→ 重启 LectureLens → 扫面板里的二维码（URL 形如 `http://<电脑IP>:8787/#t=<token>`，token 在首启已打印、设置页「安全」卡可重新生成；重新生成会把已在线的旧设备立即断开）。服务只在开关打开时监听 0.0.0.0；若手机打不开，先看**防火墙**是否放行入站（**Windows**："专用网络"；**macOS**：系统设置 → 网络 → 防火墙里允许），再确认电脑与手机在同一网段——面板上的状态行与"已连接 N 台设备"（只数**手机/局域网设备**，本机自己的窗口不计）会如实告诉你卡在哪一步。

访问地址只认直连形式：**IP（127.0.0.1 / 本机局域网 IP）、localhost、单标签主机名**。用别的域名访问（hosts 别名、DDNS、隧道域名）会被 400 拒绝——那是 DNS-rebinding 拿本机服务当跳板的唯一载体；确有需要时用环境变量 `LECTURELENS_ALLOW_HOSTS=名字1,名字2` 显式放行。

### 笔记库（工作台）

- 入口：上课态工具条「📚 笔记库」（或侧栏）。列表 = `transcripts/` 真文件（日期 | 课程 | 首句 | 摘要/卡组徽章），点条目看 EN|ZH 对照；搜索支持中文子串，可按课程筛选
- **课后改归属（归错能改）**：列表行的「移动到…」或预览页头部的「移动到…」把**整篇**改到另一门课——直接**改写笔记头的课程行**（定稿口径），与课上切课的"追加切换标记"是两套语义，互不顶替；列表行首勾选可多选**批量移动**；弹窗里点「＋ 新建」可当场建课并移入
- **管理（删除 / 编辑 / 重命名）**：预览页头部「编辑 / 重命名 / 删除」，列表行也有「删除」；行首勾选可**批量删除**
  - **删除 = 软删除**：连摘要/时间轴一起搬进 `transcripts/.trash/`，列表与搜索立即看不到；右上「🗑 回收站」里可**恢复 / 彻底删除 / 清空**（彻底删除才不可逆）
  - **编辑**保存 md 原文；保存走**正文指纹**——页面打开后若有新字幕写进来，保存会被拒绝（409，防覆盖新内容），重开一次即可
  - **重命名**只改文件名：自动补 `lecture_` 前缀与 `.md`，禁路径分隔与系统保留名；内容零改动，摘要/时间轴跟着改名
- **全选套装（A1）**：工具条上的三态全选框（空 / 半选 / 全选）+「反选」按钮；笔记库视图里按 **Ctrl+A** 等价于全选。选择只作用于**当前筛选后的可见项**（看不见的勾不上）；**切换检索/课程/标签筛选 = 清空选择**（防「看不见的选中」被下一次批量删除带走，批量条里有这条提示）。批量条显示「已选 N 篇」+ 移动到…/删除——删除确认写明篇数并提示可恢复
- **批量打标签（A2）**：笔记库勾选多篇 → 批量条「🏷 打标签」→ 小面板里点选一组标签（预设 chips 来自服务端）+ 自定义输入 + 方向 **添加 / 移除** → 一次应用到所选。服务端逐篇读现状再合并（幂等：已有的不重复加、没有的不报错），**整单先校验再动手**（有一篇不存在/标签非法 → 整单 400、一篇都不动），写入阶段某篇失败（如加上会超过每篇 20 个）只记那一篇并如实汇总「N 篇成功、M 篇失败」；选择被清空（切筛选/移动/删除/应用完成）时面板自动收起
- **批量导出 zip（A3）**：勾选多篇 → 批量条「导出所选」→ 服务端打成**一个 zip**（标准库 `zipfile`，零新依赖）。设了导出位置就落盘 `<导出目录>/笔记导出_日期.zip`（同名自动 `-2`，绝不覆盖；toast 给「打开文件夹」），没设 = 浏览器下载（文件名同口径，中文名走 RFC 5987）。包内条目名与单篇导出一致（`课程_日期.md`），**同课同日重名自动避让 `-2`**（否则解压时后者盖前者、静默少文件）；整单先校验：有一篇不存在 / 超过 50 篇 / 格式不是 md 或 srt → 400 且**一个包都不产出**。导出是只读动作，**不清空选择**（可以接着换个格式再导一次）
- **标签**：预览页头部的标签 chips、编辑面板的 chips（预设 **重点 / 考试 / 没听懂 / 待复习** + 自定义输入）点一下即存；上课时字幕页工具条「🏷 标签」可一键给**当前笔记**打标（"没听懂"当场标记）。笔记库筛选栏可按标签过滤（与课程筛选、全文检索**叠加**，检索也命中标签名）。标签存在**笔记文件头块**的一行 `> 标签：#重点 #考试`——存文件不存 sidecar，随编辑/移动/重命名存活，git 友好；规则：每标签 ≤16 字符、禁空格/`#`/换行、每篇 ≤20 个
- 预览页头部：摘要 / 卡组 / **编辑 / 重命名 / 删除 / 移动到…** / 导出（Markdown / SRT / PDF / Word）
- **可编辑摘要（§6）**：摘要（`lecture_*.summary.md`）在预览页顶部直接显示，「编辑摘要」
  可人工修订并**就地写回**（保存走正文指纹：页面开着期间摘要被重新生成过会拒绝覆盖）；
  再次点「摘要」会先确认「将覆盖你的修改」，且**覆盖前自动**把当前这版备份为
  `lecture_*.summary.bak.md`（最近一版永远在备份里；备份不进笔记列表，随笔记
  删除/改名/搬运一起走）。
- **导出位置自选（§3）**：设置页「导出」卡选一个文件夹（桌面版走系统原生文件夹选择框；
  浏览器/手机版手动填绝对路径）→ 之后「导出」**直接写进那个文件夹**，不再弹浏览器下载，
  toast 里给「打开」按钮；没设置 = 维持浏览器下载。文件名统一 **`课程_日期.扩展名`**
  （如 `数学_20261013.pdf`；没有课程归属的笔记维持原名），同名文件**不覆盖**（自动加 `-2`）。
  **有课程归属的笔记再按课程分文件夹（B1）**：落点是 `<导出目录>/<课程名>/`（如
  `机器学习/机器学习_20261013.md`）——"对应的笔记本"的物理版，Obsidian/同步盘直接可用；
  课程名为空 /「未分类」/ 清洗后为空 → 仍落导出根目录。目录名与文件名用**同一条**
  清洗规则（路径分隔与非法字符一律替换，绝不越出你选的导出目录）。
  导出目录只接受**本机绝对路径**：**Windows** 上 `\\主机\共享` 这类 UNC 与 `\\?\` / `\\.\`
  扩展前缀一律拒绝（前者会让服务在你选目录时就去连那台主机、后续把笔记写到共享上；后者能绕过
  系统目录黑名单）——要写网络位置请先映射成盘符（`Z:\导出`）；**macOS** 上写 `/Volumes/…` 或
  `~/…` 即可（网络卷先挂载成本地路径）。
- **导出用的中文字体按平台取**：**Windows** = 微软雅黑（`C:\Windows\Fonts\msyh.ttc`）；
  **macOS** = PingFang / Hiragino Sans GB（系统自带，找不到时会在系统字体目录里扫描，
  仍找不到则报错并提示改 `exporters.FONT_REGULAR`）。PDF/Word 的中文渲染依赖系统字体，
  与界面字体无关

## 打包（分发给朋友用）

| 平台 | 脚本 | 产物 | 说明 |
|---|---|---|---|
| **Windows** | `.venv/Scripts/python.exe scripts/build_exe.py` | `dist/LectureLens/LectureLens.exe`（onedir） | 默认带 GPU 运行库 + 随包 small 模型（≈460MB，离线开箱可用）；`--no-gpu` 出纯 CPU 版；`.exe` 的 `--check` 是无窗口自检 |
| **macOS** | `.venv/bin/python scripts/build_macos.py` | `dist/LectureLens.app` | PyInstaller onedir + `--windowed`；**不随包模型**（首启按档位下载/走 HF 缓存）；构建后自动注入 Info.plist（版本 + 麦克风用途说明）并做 ad-hoc 签名 |

- macOS 的图标：脚本会在 `assets/icon.icns` 缺失时用 `iconutil` 由 PNG 生成；
  手工生成命令见 `scripts/build_macos.py` 文件头（需要 16–512px 各档，本机素材到 256px）
- macOS 的 ad-hoc 签名（`codesign --force --deep -s -`）只够**本机**用：拷给另一台 Mac
  会被 Gatekeeper 拦，首次要右键 →「打开」（或 `xattr -dr com.apple.quarantine <app>`）。
  公开分发需要 Apple 开发者证书 + 公证——属于商业化项（红线外，推迟到毕业后评估）
- macOS 版的数据在 `~/Library/Application Support/LectureLens`（见「数据放在哪」）；
  Windows 版的数据在安装目录。两边都跑一次 `--check` 再交给朋友
- **Mac 上的完整操作口径（前置依赖/系统授权/构建/签名/真机验收清单/已知限制）见
  [`docs/mac-guide.md`](docs/mac-guide.md)** —— 本 README 只写跨平台总览

## 已知限制（v0.1）

- 单说话人：多人讨论字幕会混
- 只识别英文（中英混杂课堂会漏译中文段）
- 教室远场收音质量是最大变量——坐前排或用外接麦克风
- 能量 VAD（--vad energy）在极嘈杂环境仍不如 silero，仅作回退
- **写端点的 CSRF 面**：`/api/*` 只挂 LAN token 鉴权，没有 Origin/Referer 校验，而本机
  （127.0.0.1）对端被豁免——恶意页面/DNS-rebinding 理论上可代本机发起写请求。本批已把
  「把导出位置改成 UNC 共享、借导出把笔记外发并触发 SMB 认证」这条新增路径堵死（只收
  本机盘符）；给全部写端点加 Origin 校验是后续项，在那之前请勿在浏览器里开着不可信页面
  的同时运行本服务

## 路线图

- v1：术语表热更新 + 讲师口音自适应
- v2：对话模式（双向翻译）、浏览器插件
- 商业化（含出售）：**推迟至毕业后评估**（签证合规优先；口径与关于页/隐私透明页一致）

## 许可

**保留所有权利（All rights reserved）**。仓库公开（2026-10-14 起）仅为了分发与安装便利：
你可以下载并按原样使用本软件（含给朋友装机），但**未授予**复制、修改、再分发、
商业使用或将其作为服务提供的许可。课堂音频仅限本人学习用途，笔记不外传。
