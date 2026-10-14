# LectureLens 分发指南（D5）

> 读者 = **你本人**（发版时照做）。目标 = 把安装包可靠地送到朋友手上，并且让朋友
> 第一次就能装上、能用。定位见 README：**免费自用 + 朋友工具**（仓库公开但保留所有权利）。
>
> 本指南只写**已经存在的能力**。两条需要你心里有数的现实约束（贯穿全文）：
> **安装包没有代码签名**（首次运行必弹 SmartScreen）、**没有自动更新**
> （换新版靠手动覆盖安装）。这两条都不是缺陷，是本阶段刻意不做的范围，但
> **不能对朋友含糊**。
>
> 📌 **2026-10-14 变更**：仓库已转**公开**（保留所有权利，见 README「许可」）。
> Release 下载**无需登录、无需邀请 collaborator**——朋友（或其 AI 助手）拿到
> 链接就能自助安装（README「安装」章有可直接贴给 AI 的指令）。网盘/直传降为
> **镜像兜底**。

---

## 一、三条分发渠道对比

| 维度 | ① GitHub Release（已公开，正本） | ② 网盘转载（镜像） | ③ 直接传文件（兜底） |
|---|---|---|---|
| 谁能拿到 | **任何人**（链接即得，无需账号；朋友的 AI 也能自助下载） | 拿到分享链接的人 | 你发给谁谁才有 |
| 国内下载速度 | **常不稳**（GitHub 在国内波动大） | 快（选国内网盘） | 看传输方式 |
| 单附件上限 | **2 GiB**（GitHub 硬限制） | 各家不同，通常够 | 受渠道限制（微信/QQ 对超大文件不友好） |
| 版本历史 | **有**（每个 tag 一个版本，可回溯） | 弱（文件会被覆盖/过期） | 无 |
| 完整性校验 | **好**（附件 + `.sha256` 并排挂着） | 中（要你自己把 `.sha256` 一起放） | 弱 |
| 链接寿命 | 长期（只要仓库在） | **链接会过期**（免费网盘尤其） | 一次性 |

### 推荐组合（2026-10-14 起）

- **GitHub Release 是唯一"正本"与首选投递**：版本号、发布说明、SHA256 都齐；
  公开后朋友（或其 AI 助手）**拿链接即装**——把 README「安装」章那段指令贴给
  AI 即可全自动完成（下载 → 校验 → 静默安装 → 自检）。以后回溯"1.0.0 到底装的
  是哪个包"也只有这里查得到。
- **网盘当镜像/兜底**：朋友在国内直连 GitHub 拖 1.4 GiB 大概率极慢。
  把**安装包 + `.sha256` + 新手指引**三件一起转存网盘，把链接发给他备用。
- **直传（微信/QQ/移动硬盘）当兜底**：只有一两个朋友、或网盘也卡时用。
  注意微信对超大文件不友好，**优先用 U 盘或直接让朋友从你电脑上拷**。

> **本项目当前体积的实测结论**（v1.1.0）：安装包 **1.408 GiB（1,511,742,229 字节）**，
> 距 GitHub 的 2 GiB 单附件上限还有约 **0.59 GiB** 余量，所以**现在可以
> 直接挂 Release**，不必分卷。
> （v1.0.0 那轮是 1,511,627,489 字节——同一量级；下面是 turbo 的估算，不是实测。）
> ⚠️ 但余量不算大：如果哪天改用 `--bundle-model large-v3-turbo` 重打
> （随包模型从 small 的 ≈460 MB 换成 turbo 的 ≈1.6 GB，模型权重几乎压不动），
> 按当前压缩比**估算**包会涨到 ≈2.4 GiB、**超过 2 GiB 上限**（这是估算，不是实测
> ——发版前先看实际产物体积，别照抄）。真超了要么按 D6 预案做 7z 分卷，
> 要么只走网盘/直传。

---

## 二、发新版 SOP（照做即可）

### 2.0 一次性准备（**本机已就绪**，实测过）

- `gh` 已装并已登录：实测 `gh --version` → **gh version 2.92.0**
  （`C:\Program Files\GitHub CLI\gh.exe`）；`gh auth status` → 已登录
  **HaoranLuo**，token scopes 含 `repo`（发 Release 够用）。
- Inno Setup 6 已装（D4 装好，v6.7.3）。注意 **ISCC 不在 PATH 上**，
  下面一律用绝对路径调用。
- 仓库**已公开**（2026-10-14 起）：`gh repo view --json visibility` → `"PUBLIC"`。
  Release 下载无需登录——发完照 §2.3 的「匿名验证」跑一遍即可交出去。

### 2.1 构建

版本号**只有一个来源** = `app/branding.py` 的 `APP_VERSION`。改版本就改这一处，
exe 属性、安装包名、关于页显示全部跟着走。

```bat
:: ① 改版本号（唯一改动点）
::    app\branding.py  →  APP_VERSION = "1.1.0"

:: ② 打包（GPU 版：含 CUDA 运行库；约 3 GB 产物 / 十几分钟）
.venv\Scripts\python.exe scripts\build_exe.py

::    没有 N 卡的机器 / 想让包小一点：加 --no-gpu
::    .venv\Scripts\python.exe scripts\build_exe.py --no-gpu

:: ③ 产物门禁（秒级）：必需文件齐不齐、有没有混进 config.json/transcripts 之类
.venv\Scripts\python.exe scripts\build_exe.py --check-dist

:: ④ 编安装包（ISCC 不在 PATH，必须绝对路径；实测 405～484 秒，随机器负载浮动）
"%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" installer\lecturelens.iss
```

④ 的产出：`dist\installer\LectureLens-<版本>-setup.exe`（版本自动取自 exe 版本资源）。

### 2.2 生成 SHA256 + 实测（**别跳过这一步**）

```powershell
# ⑤ 生成校验文件（与安装包同目录、同前缀名 + .sha256）
#    用 PowerShell：这条命令已实测，产出格式与仓库里现有的 .sha256 逐字节一致。
$p    = "dist\installer\LectureLens-1.1.0-setup.exe"          # ← 改成实际版本
$h    = (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLower()
$name = Split-Path $p -Leaf
Set-Content -LiteralPath "$p.sha256" -Value "$h  $name" -Encoding ascii
Get-Content "$p.sha256"      # 应打印：<64位小写哈希>  LectureLens-1.1.0-setup.exe
```

```bat
:: ⑥ 只有「首次发版、手上没有真旧版包」时才需要合成一个旧版测试包
::    （不压缩，约 20 秒；用 .iss 里的 TEST_VERSION 钩子）。输出到 upgrade-test\ 子目录，
::    免得它被 verify_installer 当成"最新的正式包"误选。
::    ⚠️ 从 v1.1.0 起**不需要**这一步——直接用真实的 1.0.0 安装包（见 ⑦）。
"%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" /DTEST_VERSION=0.9.0 /O"dist\installer\upgrade-test" installer\lecturelens.iss
```

```bat
:: ⑦ 真装真卸实测（分钟级）：静默装→--check→造数据探针→卸载→断言 exe 删了但笔记
::    还在→重装→【装真旧版 → 在旧版下造探针 → 用新版覆盖它】→清理
::    ★ 优先用**真实**旧版安装包（比合成的测试包硬）：
.venv\Scripts\python.exe scripts\verify_installer.py --upgrade-from dist\installer\LectureLens-1.0.0-setup.exe
::    没有真旧版时才退回第 ⑥ 步的合成包：
::    .venv\Scripts\python.exe scripts\verify_installer.py --upgrade-from dist\installer\upgrade-test\LectureLens-0.9.0-setup.exe
```

⑦ 必须打出 **「全部 N 项通过」并 exit 0** 才算这一版可发（当前 N=32；脚本加了检查项
N 会变，认「全部通过 + exit 0」这个结论，别认数字）。它会自己校验
`.sha256` 是否存在且与安装包一致——**忘了第 ⑤ 步这里会当场红**。

> ⚠️ **`--upgrade-from` 不是可选项**。不传，第 ⑦ 步会**直接判失败**——因为
> 「覆盖安装不丢数据」这条承诺的真实路径是"旧版 → 新版"，只测同版本重装撑不起
> 这句话。第一次发版没有真正的旧版，就用上面第 ⑥ 步合成一个测试包。
> `/DTEST_VERSION` 是 `.iss` 里的**测试专用钩子**：发版命令**不带**它，版本仍严格
> 取自 exe 版本资源（`tests/test_verify_installer.py` 有断言钉住这一点）。
>
> 🔒 **旧版安装包是发版资产，打新包时不许删**：`dist\installer\LectureLens-1.0.0-setup.exe`
> 要**一直留着**——它是 v1.1.0 及以后每一轮跨版本升级实测的输入。ISCC 按
> `LectureLens-<版本>-setup.exe` 命名，打 1.1.0 不会覆盖 1.0.0；但仍请打完后
> `dir dist\installer` 确认两个包都在（v1.1.0 那轮的实测：1.0.0 的 SHA256 在重打后
> 逐字节未变 = `b3c9fada…`）。

### 2.3 打标签 + 建 Release

```bat
:: ⑦ 提交（写中文提交信息，见仓库惯例）+ 打标签
git add -A
git commit -m "release: v1.1.0"
git tag v1.1.0
git push origin main --tags

:: ⑧ 建 Release 并同时挂上两个附件
::    --verify-tag：标签没真的推到远端就中止（防手滑发了个空版本）
::    命令写成一行，避免 cmd 的 ^ 换行踩坑
gh release create v1.1.0 --verify-tag "dist\installer\LectureLens-1.1.0-setup.exe" "dist\installer\LectureLens-1.1.0-setup.exe.sha256" --title "LectureLens v1.1.0" --notes-file docs\release-notes-v1.1.0.md
```

（`--title` / `--notes-file` / `--verify-tag` 三个旗标均已用
`gh release create --help` 核对过存在。）

> 仓库已公开（2026-10-14）→ Release 对所有人可见可下载，**不再需要邀请 collaborator**。
> 发完做一次**匿名验证**（curl 默认不携带登录态；应看到 200 / 302→200，
> 不出现任何登录跳转）：
>
> ```bash
> curl -s  -o /dev/null -w "repo   HTTP %{http_code}\n" https://github.com/HaoranLuo/lecturelens
> curl -sIL -o /dev/null -w "setup  HTTP %{http_code}\n" \
>   "https://github.com/HaoranLuo/lecturelens/releases/latest/download/LectureLens-<版本>-setup.exe"
> curl -s https://api.github.com/repos/HaoranLuo/lecturelens/releases/latest | grep -o '"tag_name[^,]*'
> ```
>
> （2026-10-14 实测 v1.1.0：repo 200、setup 302→200、API 返回 `"tag_name": "v1.1.0"`。）

### 2.4 发布说明模板

复制下面这段，存成 `docs/release-notes-vX.Y.Z.md` 再喂给 `--notes-file`：

```markdown
## LectureLens vX.Y.Z

**这个版本给谁用**：自己的学习用 + 朋友装机（公开仓库 · 保留所有权利）。

### 装之前先看
- 本程序**没有代码签名**，首次运行 Windows 会拦一次：点「更多信息」→「仍要运行」。
- 第一次启动如果弹防火墙，想用「手机同屏」就点「允许访问」。

### 本版改了什么
- （逐条写人话，别写 commit message）

### 安装包
| 文件 | 大小 | SHA256 |
|---|---|---|
| `LectureLens-X.Y.Z-setup.exe` | 1.41 GiB | `3c0a6ffc…`（贴完整值） |

校验方法见仓库 `docs/distribution-guide.md` §五 Q3「怎么确认下载没坏」。

### 已知限制（如实说）
- 没有代码签名 → SmartScreen 会提示。
- 没有自动更新 → 换新版要**重新下载并覆盖安装**（笔记不会丢，见 FAQ）。
- 平台：Windows 64 位 + macOS（Apple Silicon，预构建包见 Release）；两者均未签名（SmartScreen／未公证提示，属预期）。
```

---

## 三、朋友首次使用指引

> 这一节的内容已经作为 **`新手指引.txt` 随包发出去**（装完就在安装目录里，
> 开始菜单也有一项）。下面是同一套流程的完整版，你发给朋友前自己过一遍。

1. **下载**：拿到 `LectureLens-1.1.0-setup.exe`（+ 可选的 `.sha256`）。
   ⚠️ 别在压缩包里直接双击——先解压到硬盘再装。
2. **过 SmartScreen**：双击后若弹蓝色「Windows 已保护你的电脑」→
   点「**更多信息**」→ 再点「**仍要运行**」。这是"没买数字签名证书"的正常现象，
   不是病毒。之后不再提示。
3. **安装**：一路下一步。默认装到 **`%LocalAppData%\LectureLens`**，
   **不需要管理员权限**（不会弹 UAC）。安装页有一个「创建桌面快捷方式」的勾选，
   **默认不勾**，想要就自己勾上；开始菜单里一定会有一项。
4. **过防火墙**（只在想用手机同屏时需要）：首次启动 Windows 可能问是否允许联网 →
   点「**允许访问**」（勾"专用网络"就够）。只用电脑自己翻译的话，点「取消」也不影响。
5. **首启三步向导**（自动弹出）：
   - **① 麦克风测试** —— 对着麦说句话，确认能收到声音；
   - **② DeepSeek key** —— 用来把英文翻成中文，**没有可以点「跳过」**
     （跳过 = 纯英文字幕模式，之后随时能在「设置 → ⚙ 系统 → 重开首启向导」补）；
   - **③ 试说一句** —— 说一句英文，验证"识别 → 翻译 → 上屏"整条链路。
6. **手机同屏**（可选）：字幕页工具条点「📱 手机同屏」→ 打开开关 →
   **重启一次程序**（监听地址要重启才切换）→ 用手机扫面板里的二维码。
   手机和电脑必须连**同一个 WiFi**；面板里有「连不上就按这三步排查」。
   ⚠️ 二维码/URL 里带着访问 token，**别把截图外传**；泄了就在设置页「📶 连接」
   重新生成 token（旧设备会立刻断开）。

---

## 四、可直接复制发给朋友的文案

> 链接公开后不用替换任何东西，整段发即可。两版任选：① 手装版；② 贴给 AI 版更省事
> （对方电脑上要有能执行命令的 AI 助手）。这段话刻意不提"签名证书多少钱"之类的内部细节。

① 手装版：

```text
给你个我自己在用的上课字幕工具，LectureLens，Windows 64 位，免费。

下载：https://github.com/HaoranLuo/lecturelens/releases/latest
（点最新那个 LectureLens-1.1.0-setup.exe 下载，约 1.41 GB；国内直连慢的话
 跟我说一声，我发你网盘镜像）

怎么装（三步）：
1. 双击 LectureLens-1.1.0-setup.exe。如果 Windows 弹蓝色的
   "Windows 已保护你的电脑"，点「更多信息」→「仍要运行」。
   这个程序我没买代码签名证书，所以 Windows 会先拦一下，不是病毒。
2. 一路下一步。装完第一次启动，如果防火墙弹窗问是否允许联网，
   想用手机扫码看字幕就点「允许访问」，不用就点「取消」。
3. 会弹一个三步向导：麦克风测试 → 填 DeepSeek 的 key（没有可以先跳过，
   跳过就是纯英文字幕）→ 说一句英文试试。

它怎么用：麦克风收教室的声音，本机识别成英文字幕，再把英文翻成中文。
音频全程不出你的电脑，只有识别出来的文字会发去 DeepSeek 翻译。
上网课（Zoom / 看视频）可以切「系统声音内录」，直接录电脑放出来的声音，
不受环境噪音影响。

想手机躺床上看字幕：同一个 WiFi 下点字幕页的「📱 手机同屏」，扫码即可。

装完看「关于」能看到版本号。有新版我会再发你，直接双击新安装包覆盖装就行，
你的笔记不会丢。
```

② 贴给 AI 版（对方电脑上有带执行权限的 AI 助手时，整段贴给它）：

```text
帮我在本机安装 LectureLens（Windows 课堂实时字幕工具）：
1. 取最新发布：https://github.com/HaoranLuo/lecturelens/releases/latest
2. 下载其中的 LectureLens-*-setup.exe（约 1.41 GB）和同名 .sha256 文件，
   用 SHA256 校验两者一致；
3. 静默安装：<安装包> /VERYSILENT /SUPPRESSMSGBOXES /NORESTART；
4. 装完运行 %LocalAppData%\LectureLens\LectureLens.exe --check，
   看到 [check] ALL OK 即成功；告诉我安装位置和版本号。
```

---

## 五、常见问题（FAQ）

### Q1. 双击没反应 / 弹「Windows 已保护你的电脑」
这是 **SmartScreen**，因为安装包没有代码签名（本阶段刻意不买，£100-400/年）。
点「**更多信息**」→「**仍要运行**」。
如果压根没看到这两个按钮，确认你是在**硬盘上的 .exe**（不是压缩包内预览），
文件名叫 `LectureLens-1.1.0-setup.exe`。

### Q2. 杀毒软件报毒
PyInstaller 打的包 + 无签名，确实常被启发式规则误报。处理办法：
把安装包加入杀软白名单后重装。如需自证清白，用下面的方法核对 SHA256 —— 只要哈希
与发布页一致，就说明文件在传输中没被篡改。

### Q3. 怎么确认下载没坏 / 没被人换过
对**你自己下的那个文件**算一次哈希，跟发布页/`.sha256` 里第一串比对。
PowerShell（推荐，Windows 自带）：

```powershell
cd "$env:USERPROFILE\Downloads"
(Get-FileHash -Algorithm SHA256 .\LectureLens-1.1.0-setup.exe).Hash.ToLower()
```

或用 `certutil`（实测可用）：

```bat
certutil -hashfile "%USERPROFILE%\Downloads\LectureLens-1.1.0-setup.exe" SHA256
```

把输出与 `.sha256` 文件里的第一串（64 位十六进制）逐字比对，**一致即完好**。
> 当前 v1.1.0 的值：`0b41d0a04b7f4fd93b25a96f8315f3ead00d4124a6d613cb792b6b192b2b3f14`
> （v1.0.0 那版是 `b3c9fada9e4791d15a15609ed6a632037f03ddc11bc0488dad33fbdc786cd881`，那个包仍留在 dist\installer 里当跨版本升级实测的输入。）
> 注意 PowerShell 默认输出大写、`certutil` 输出小写，比对时别看粗细——大小写
> 不影响一致性，但字母得对得上。
> 这个值随每次重打安装包都会变，**以同目录 `.sha256` 文件为准**（本文件这里只是
> 当前这一版的实际值）。

### Q4. 装不上 / 装到一半报错
- **是不是 32 位 Windows？** 本程序只有 64 位版（`ArchitecturesAllowed=x64compatible`）。
  32 位系统装不了。
- **磁盘空间**：装完约占 3 GB（含随包的「标准」档模型 ≈460 MB），
  安装过程还要额外临时空间，**留 6 GB 以上**再装。
- **没有管理员权限也能装**：本安装包是**每用户安装**（装到
  `%LocalAppData%\LectureLens`），**不弹 UAC**。如果它要你提权，说明拿到的不是
  这个包，别继续。
- 还是不行：把报错截图发我，安装日志可以这样留：
  `LectureLens-1.1.0-setup.exe /VERYSILENT /LOG="%TEMP%\ll-install.log"`，
  然后把 `%TEMP%\ll-install.log` 发我。

### Q5. 装了但**没声音 / 一直没字幕**
按顺序查：
1. **音源选错**：字幕页有音源下拉——「麦克风」= 教室现场收音；
   「系统声音内录」= WASAPI 环回，录 Zoom/视频课用。网课场景选成麦克风当然只有房间噪音。
2. **麦克风设备选错**：设置页「🎙 上课」里能换输入设备（下拉里带 ★ 的是系统默认）。
   换一个再试。
3. **Windows 麦克风权限**：「设置 → 隐私和安全性 → 麦克风」里确认允许桌面应用访问。
4. **模型还在下载**：首次用「高」「极致」档位要下模型（约 1.6 / 2.9 GB）。
   装完自带的是「标准」档（≈460 MB，开箱即用、离线可用），所以**刚装完不该卡在这**。
   —— 卡在"下载中"就去设置页看进度条。
5. 说话时字幕页有没有波形/字数变化？**有波形但没文字** = 识别环节的问题，
   **没波形** = 采集环节的问题（回到第 1、3 条）。

### Q6. 手机扫码连不上
面板里写明了三步，照顺序来：
1. **防火墙放行**：首次开启时弹的"是否允许 **LectureLens** 联网"必须选允许。
   当时点了取消 → 到「Windows 安全中心 → 防火墙和网络保护 → 允许应用通过防火墙」
   在列表里找到 **LectureLens** 这个条目，把**专用**和**公用**两个勾都补上。
   （装好的成品里**没有 python.exe**——`dist\LectureLens\` 下只有
   `LectureLens.exe`）。
2. **网络类型改「专用」**：校园网/公司网常被判成「公用」，默认拒绝入站 →
   在「防火墙和网络保护」里把当前网络改成**专用网络**。
3. **同一个 WiFi**：别一个连 WiFi、一个连热点或有线到别的网段。
   扫码前面板应显示「已开启」（显示「重启后生效」就重启一次程序）。
> 如实说明：**仅同一 WiFi 可用**。本程序不做公网映射、不做内网穿透，
> 所以出门在外、或朋友在不同网络下，**连不上是正常的**，不是 bug。

### Q7. **怎么更新到新版本**（重点：笔记不会丢）
**没有自动更新**。流程 = 下载新版安装包 → **直接双击覆盖安装**即可，
不需要先卸载。
> 数据保留是**实测过的**，不是口头承诺。`scripts/verify_installer.py` 真装真卸并断言：
> - 卸载后 `LectureLens.exe` 已删除，而 `transcripts\`（笔记）与 `config.json` 原样还在；
> - **跨版本覆盖升级**也是实测的（v1.1.0 那轮走的是**真实** `1.0.0 → 1.1.0`：先装旧版 → **在旧版下**造笔记 →
>   再用新版**不卸载直接覆盖**）→ 旧版下造的笔记依然原样、程序自检仍退 0；
>   这条路径**不传 `--upgrade-from` 会判失败**，防止"没测也敢说"；
> - 卸载器在**卸载前**、**卸载后**、以及"文件被占用需重启才能清完"那条路径上，
>   都会提示"你的笔记已保留"。

笔记、术语表、日志、设置都在安装目录（`%LocalAppData%\LectureLens`）里，
卸载程序**不会删**。想彻底清空，卸载完再手动删掉那个文件夹。

### Q8. 怎么看我装的是哪个版本
程序里点「关于」→ 显示 `vX.Y.Z`（取自 `/api/status` 的 version 键，与安装包同源）。

### Q9. 电脑有点老，跑得动吗
程序会检测硬件**保守推荐**档位（宁小勿大，随时可手动覆盖），四档：

| 档位 | 模型 | 大约体积 | 什么时候选 |
|---|---|---|---|
| 流畅 | base | ≈140 MB | 最快最省；老旧电脑也能流畅出字幕 |
| **标准** | small | ≈460 MB | 普通笔记本的平衡点，**开箱自带的就这档** |
| 高 | large-v3-turbo | ≈1.6 GB | 明显更准；有独显或新芯片推荐 |
| 极致 | large-v3 | ≈2.9 GB | 最强精度，要大显存，速度换质量 |

没有独立显卡也能用「标准」档（走 CPU）。想更准就去设置页升档，会联网下模型。

---

## 六、明确的能力边界（**别对朋友承诺这些**）

| 没有的东西 | 实情 |
|---|---|
| 代码签名 | **没有** → 首启必弹 SmartScreen。买证书 £100-400/年，商业化再上。 |
| 自动更新 | **没有**（Tauri 议题）→ 换新版靠手动覆盖安装。 |
| macOS 版 | **有预构建包（实验性）**：Apple Silicon（arm64），GitHub 云构建（`.github/workflows/build-macos.yml`）；未公证（首次打开需右键「打开」）；真机全量验收进行中；Intel Mac 不适用。 |
| 开源许可 | **没有**——仓库虽公开（2026-10-14 起）但**保留所有权利**：可下载使用，未授权复制/修改/再分发/商用（见 README「许可」）。 |
| 公网访问手机同屏 | **没有** → 仅同一 WiFi，不做穿透/映射。 |
| 云同步 / 账号 | **没有** → 笔记只在本机。 |
| 上架商店 | **没有** → 只走 Release / 网盘 / 直传。 |

## 七、本指南自身的验证状态（如实标注）

- **已实测**（本机，命令与输出都留有记录）：
  - 构建 → 编安装包（ISCC 实测 397 秒，exit 0）→ 生成 SHA256 → 与安装包核对一致；
  - `verify_installer.py` 真装真卸全流程：静默装 / `--check` / 造数据探针 / 静默卸载
    / 断言 exe 已删但笔记还在 / 重装 / **跨版本覆盖升级（实测 1.0.0 → 1.1.0）** / 清理。
    清理那四条**也是断言**（卸载器已退出、临时目录已删净、无残留进程、无新增 Inno
    临时目录），不是只打印一行日志。
- **未实测**（属你本人操作、或需要另一台机器 / GUI，我做不了）：
  - 网盘上传与分享链接；
  - **"干净 Windows、没装过 Python"的机器**上装一遍 —— D5 把这条列为已知未知，
    本机验证不能替代它。理论上安装包自带运行时（PyInstaller onedir 全量打包）
    不需要 Python，但**这条没在真无 Python 环境上验过**；
  - 朋友真机实测（真实用户）；
  - **安装/卸载向导的实际渲染效果**：自动化跑的是静默模式，静默卸载**不显示任何
    消息**，所以那四条卸载文案（含"笔记已保留"）只能验证"确实写进了脚本并覆盖了
    四条路径"，**屏幕上长什么样属 GUI 人工项**，没验过。
