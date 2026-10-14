# -*- coding: utf-8 -*-
# LectureLens — 课堂实时字幕翻译
# 签证合规（2026-10-13 拍板）：当前免费自用+朋友使用（含朋友装机），
# 不收费、不注册公司（Student visa 禁 self-employment）；
# 2026-10-14 拍板：仓库转公开（保留所有权利、未授予开源许可）——用于
# 朋友及其 AI 助手从 GitHub 一键安装；权威口径见 README.md「许可」章。
# 未来可能商业化（含出售），决策推迟至毕业后按签证与法律要求评估。

## 一、这是什么
课堂实时字幕翻译：笔记本麦克风采集老师语音 → faster-whisper 实时英文识别
→ DeepSeek 翻译中文 → 浏览器大字幕（中英对照）→ 课后自动存双语笔记。
桌面应用先行；浏览器页面手机连同一 WiFi 也能看字幕。

## 二、结构
- app/capture.py    麦克风采集 + VAD 分句（默认 silero 流式，--vad energy 回退）
- app/asr.py        faster-whisper 封装（auto=自动上 CUDA；GPU float16 / CPU int8）
                    Mac 适配第二批起：Apple Silicon（arm64）且装了 mlx-whisper 时
                    auto 解析为 **mlx**（权重表 models.MLX_MODEL_REPOS，mlx-community），
                    否则回落 CPU；★ 上游事实：mlx_whisper 没有 vad_filter、beam search
                    未实现，_transcribe_mlx 自己消化这两处（见该函数注释）
- app/tiers.py      模型档位化（硬件检测 → 保守推荐；四档=流畅/标准/高/极致）
- app/translate.py  DeepSeek 翻译 + 课程术语表注入（含 whisper initial_prompt）
- app/summarize.py  课后 AI 笔记摘要（map-reduce 分块并行 → 概览/要点/术语/存疑 md）
                    + §6 覆盖前自动备份为 *.summary.bak.md（人工修订版不被静默冲掉）
- app/ankideck.py   Anki 复习卡生成（摘要 → LLM 生成+二次过滤 → genanki .apkg）
- app/main.py       FastAPI + WebSocket + config.json + 模型热切换/暂停/导出端点
                    ⚠ 数据根 ROOT：frozen 时 **Windows = exe 目录**、
                    **macOS = ~/Library/Application Support/LectureLens**
                    （`_frozen_root()`；.app 是签名覆盖的只读包体——config.json/
                    transcripts/logs/glossary/courses/.env/desktop.lock 全随 ROOT，
                    WEB 与 PACKS_DIR 仍走 _MEIPASS 只读）
                    （+ §2 笔记改归属 /api/notes/course(|/batch)：改写笔记头课程行；
                     + §4 笔记管理 /api/notes/{raw,rename,delete,trash*}：软删除进
                     transcripts/.trash/、编辑走正文指纹、重命名规范化；
                     + §8 标签 /api/notes/tags：改笔记头 `> 标签：` 行，全量替换；
                     + A2 批量打标签 /api/notes/tags/batch {files,tags,mode:add|remove}：
                     整单校验 + 逐篇合并（幂等）+ 失败明细 + 单次广播；
                     + A3 批量导出 /api/notes/export/batch {files,fmt:md|srt}：打包 zip
                     （落 export_dir 或临时文件下载；条目名 {课程}_{日期} 自动避让）；
                     + §3 导出位置 export_dir（落盘 JSON {ok,path}）+ /api/export/open：
                     仅本机打开导出目录；
                     + §6 可编辑摘要 /api/notes/summary（GET/PUT）：摘要原文读写、正文
                     指纹防覆盖、重新生成前自动备份 *.summary.bak.md；
                     + §5 下课一键归档（前端 endClass 编排：暂停→摘要→导出，逐步列示；
                     开关 config.class_end_confirm）
                     + §7 手机同屏：config.lan_enabled（默认 false）决定启动绑定
                     0.0.0.0（优先 8787，被占换端口）/ 127.0.0.1；GET /api/lan-qr 仅
                     环回可读（配对 URL + SVG 二维码，含 token）；status.ws_clients
                     = **非环回**对端的 WS 数（本机窗口不计，评审2①）；
                     HostGuardMiddleware：/api/* 与 /ws 只认 IP/localhost/单标签名/
                     本机名（LECTURELENS_ALLOW_HOSTS 可加白名单），挡 DNS-rebinding
                     （评审1①）；token 轮换会断开所有在线 WS（评审2④b）
- app/lan.py         §7 手机同屏的纯逻辑/系统探测：绑定决策（0.0.0.0 ↔ 127.0.0.1）、
                    端口占用回落（真 bind 探测）、主网卡地址（UDP connect 探测，不发包）、
                    配对 URL（`#t=<token>` 走 hash 段）与 token 解析、qrcode 出 SVG
- app/exportdir.py  §3 导出位置与文件名（export_dir 校验：绝对路径/可建/可写/非系统
                    关键目录；`{课程}_{日期}.{ext}` + sanitize；B1 课程子目录
                    `<导出目录>/<课程名>/`（空课程仍落根）；同名避让 -2；原子落盘）
- app/notes.py      笔记解析（EN↔ZH 配对）+ safe_note_path + §2 set_course 定稿改写
                    + §4 名字校验/回收站/派生文件搬运 + §8 标签解析/规范化/写回
- app/web/index.html 字幕页（工具条：暂停/选课程/换模型/电平条/字号/导出/清屏/全屏）
                    + 工作台：笔记库（A1 三态全选/Ctrl+A、A2 批量打标签、A3 批量导出 zip）、
                    术语库、设置页五分区（B12：卡片挂 data-set-tab + setTabs 标签条，
                    记住上次分区 ll_settings_tab，openWorkbench('settings',{tab}) 深链）
- config.json       模型/设备/VAD 持久化（字幕页热切换会写回）
- glossary/         课程术语表（每行 `EN term = 中文译名`，--glossary 传入）
- scripts/smoke_test.py 全管线自检（无需麦克风；--gpu 基准 / --server 在线 / --translate）
                    ★ 平台分支：TTS 合成 **Windows=SAPI(PowerShell)**、**macOS=say+afconvert**；
                    [3] GPU 基准（nvidia-smi/CUDA）**仅 Windows**（非 Windows 如实打印跳过，
                    不计入结果）；[8] 打包契约 **Windows=build_exe**、**macOS=build_macos**
                    （PyInstaller 的 win32 版本资源模块在非 Windows 导不进来）
- scripts/build_exe.py   Windows 打包（PyInstaller onedir；随包 small；GPU DLL 可选）
- scripts/build_macos.py macOS 打包（onedir + .app；Info.plist 版本/麦克风用途说明；
                    iconutil 生成 .icns；ad-hoc 签名 codesign -s -；**只在 darwin 上跑**，
                    纯函数部分在 Windows 上可被 tests/test_macos_build.py 断言）
- scripts/verify_dist.py 产物验证（拷贝到临时目录跑真产物）：版本读法 Windows=PowerShell、
                    macOS=plutil -p；数据落点断言 Windows=拷贝根、macOS=用户数据目录
- transcripts/      每节课双语笔记（gitignore：含课程内容）

## 三、运行
1. `.venv` 已建好（含 nvidia-cublas/cudnn cu12，全在 D 盘）；启动：双击 start_translate.bat
   （桌面窗口 + 托盘 + 悬浮字幕条，`app/desktop.py`；`--glossary` 可透传，其余旗标走
   `--server-only`），或 `.venv\Scripts\python.exe app\desktop.py --glossary glossary/CS.md`
   ★ 平台：`start_translate.bat` 与 `.venv\Scripts\...` 都是 **Windows 口径**；macOS 用
   `.venv/bin/python app/desktop.py --glossary glossary/CS.md`（或 `open dist/LectureLens.app`）
2. 桌面模式无需浏览器（原生窗口）。手机同屏（§7）：字幕页/设置页打开「手机同屏」开关
   （config `lan_enabled`）→ **重启**（监听地址只在启动时读，进程内换不了）→ 扫面板二维码；
   开启后服务监听 0.0.0.0（8787 被占自动换端口，实际值见日志/面板/`/api/lan-qr`），
   关闭态桌面与 --server-only 都只听 127.0.0.1；`--server-only` 后浏览器仍是
   http://localhost:8787（本机不受开关影响）
3. KCL SSO / DeepSeek key：DEEPSEEK_API_KEY 放环境变量或项目根 .env
4. 自检：`.venv\Scripts\python.exe scripts\smoke_test.py`（--gpu/--server/--translate 可选）

## 三·一、GPU 本地顶配（RTX 5060 8GB 实测）
> ⚠ 本节整节是 **Windows / NVIDIA** 口径。macOS 走另一条加速路（Apple Silicon 用
> mlx-whisper，见 app/asr.py；Intel Mac 回落 CPU），`--gpu`/nvidia-smi/GPU DLL 那些
> 在 macOS 上都不适用；数据面也从「安装目录」换成 `~/Library/Application Support/LectureLens`。

- config.json 默认 large-v3-turbo + device=auto → RTX 5060 上 float16，RTF 0.05，显存 2.2/8GB
- Windows 坑：ctranslate2 首次推理经 PATH 搜 cuBLAS DLL，asr.py 的 _setup_cuda_dlls 已双保险
  （add_dll_directory + 进程 PATH 前插），换机器装了 nvidia-*cu12 即可
- C 盘零占用原则（**仅开发机**）：venv/模型缓存全在 D 盘（HF_HOME=D:\01_Environments\model-cache）。
  ⚠ 分发形态下不成立：朋友机器没有 HF_HOME，**档位下载**默认落
  `C:\Users\<user>\.cache\huggingface\hub`（随包模型在安装目录、不占额外空间；
  **macOS 上没有随包模型**，缓存落在 `~/.cache/huggingface/hub`，换盘用 `export HF_HOME=…`）；
  设置页模型卡常显实际路径，README「模型放在哪」一节是给用户的口径
- 档位化（2026-10-13）：首装未选过模型时按硬件自动选档（判定=config.json 有无
  model 键，见 main.user_picked_model；**D2 起打包版改为默认选随包模型**——
  main.pick_bundled_default，small 优先；旧口径「frozen 不参与」已废）；检测只信 nvidia-smi
  （WMI 大显存读数不准）、阈值宁小勿大、可随时手动覆盖
- D2（分发工程）：frozen **不再**强制离线（旧 `HF_HUB_OFFLINE=1` 让打包版永远不能下载
  新档位）；离线只认显式环境变量；模型解析次序=随包 → HF 缓存已下载 snapshot → 按名联网
  （main.resolve_model_ref）；config 键 `hf_mirror`（true → 导入前把 HF_ENDPOINT 设为
  **唯一预设镜像** hf-mirror.com，重启生效，见 app/hfenv.py）
- **D4 安装包必须实测的四条（评审2 点名，D3 时仓库内尚无安装器 → 无法核验）**：
  （⚠ Windows 安装器（Inno）口径；macOS 的分发形态是 .app + ad-hoc 签名，
  没有安装器——「覆盖安装不丢数据」在 macOS 上等价于「数据在包外」，见 scripts/build_macos.py）
  ① 安装器/安装后目录内不得有敏感物（把 build_exe.forbidden_hits 对**解包目录**再跑一遍，
     清单=config.json/courses.json/.env/transcripts/logs/glossary/desktop.lock/`*.log`）；
  ② 装到 `%LocalAppData%\LectureLens` 后该目录**仍可写**（用户数据跟着 exe 目录走，
     main.ROOT=exe 所在目录——这是数据布局的前提，必须真装出来验）；
  ③ **卸载必须保留数据**：Inno 卸载器默认会连安装目录一起删 → 必须
     `[UninstallDelete]` 精细控制 + 卸载页写明「你的笔记已保留」，且**实测**卸载后
     transcripts/config.json/glossary 仍在（否则第一个朋友卸载就丢笔记）；
  ④ 覆盖安装不丢数据 + 新手指引含：SmartScreen「更多信息→仍要运行」、防火墙首次弹窗、
     **模型缓存落点与 HF_HOME 换盘**（README「模型放在哪」同口径）、首启向导、扫码配对

## 四、红线（2026-10-13 拍板；2026-10-14 修订）
- 当前阶段：**免费自用 + 朋友使用（含朋友装机）**；不收费、不注册公司
  （Student visa 禁 self-employment）
- **仓库公开**（2026-10-14 拍板）：便于朋友及其 AI 助手从 GitHub 一键安装；
  但**保留所有权利、未授予开源许可**——可下载使用，未授权复制/修改/再分发/商用
  （权威口径 = README.md「许可」章；关于页/隐私页同步）
- 未来计划：可能商业化（含对外发布或出售）；决策**推迟至毕业后**，届时按签证与法律要求评估
- 采集课堂音频仅限本人学习用途，笔记不外传
- 凭据（DeepSeek key）不进 git；transcripts/ 不进 git

## 五、已知限制（v0）
- VAD 是能量门限：背景噪声大时可能切分不准（后续换 Silero VAD）
- whisper 后端固定英文（language=en），中英混杂课堂会漏译中文段
- 无说话人分离：多人群聊场景字幕会混在一起
