# -*- coding: utf-8 -*-
# LectureLens — 课堂实时字幕翻译
# 签证合规（2026-10-13 用户拍板）：当前免费自用+朋友使用（含朋友装机），
# 不收费、不注册公司、不公开分发（Student visa 禁 self-employment）；
# 未来可能商业化（含出售），决策推迟至毕业后按签证与法律要求评估。

## 一、这是什么
课堂实时字幕翻译：笔记本麦克风采集老师语音 → faster-whisper 实时英文识别
→ DeepSeek 翻译中文 → 浏览器大字幕（中英对照）→ 课后自动存双语笔记。
桌面应用先行；浏览器页面手机连同一 WiFi 也能看字幕。

## 二、结构
- app/capture.py    麦克风采集 + VAD 分句（默认 silero 流式，--vad energy 回退）
- app/asr.py        faster-whisper 封装（auto=自动上 CUDA；GPU float16 / CPU int8）
- app/tiers.py      模型档位化（硬件检测 → 保守推荐；四档=流畅/标准/高/极致）
- app/translate.py  DeepSeek 翻译 + 课程术语表注入（含 whisper initial_prompt）
- app/summarize.py  课后 AI 笔记摘要（map-reduce 分块并行 → 概览/要点/术语/存疑 md）
                    + §6 覆盖前自动备份为 *.summary.bak.md（人工修订版不被静默冲掉）
- app/ankideck.py   Anki 复习卡生成（摘要 → LLM 生成+二次过滤 → genanki .apkg）
- app/main.py       FastAPI + WebSocket + config.json + 模型热切换/暂停/导出端点
                    （+ §2 笔记改归属 /api/notes/course(|/batch)：改写笔记头课程行；
                     + §4 笔记管理 /api/notes/{raw,rename,delete,trash*}：软删除进
                     transcripts/.trash/、编辑走正文指纹、重命名规范化；
                     + §8 标签 /api/notes/tags：改笔记头 `> 标签：` 行，全量替换；
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
                    关键目录；`{课程}_{日期}.{ext}` + sanitize；同名避让 -2；原子落盘）
- app/notes.py      笔记解析（EN↔ZH 配对）+ safe_note_path + §2 set_course 定稿改写
                    + §4 名字校验/回收站/派生文件搬运 + §8 标签解析/规范化/写回
- app/web/index.html 字幕页（工具条：暂停/选课程/换模型/电平条/字号/导出/清屏/全屏）
- config.json       模型/设备/VAD 持久化（字幕页热切换会写回）
- glossary/         课程术语表（每行 `EN term = 中文译名`，--glossary 传入）
- scripts/smoke_test.py 全管线自检（无需麦克风；--gpu 基准 / --server 在线 / --translate）
- transcripts/      每节课双语笔记（gitignore：含课程内容）

## 三、运行
1. `.venv` 已建好（含 nvidia-cublas/cudnn cu12，全在 D 盘）；启动：双击 start_translate.bat
   （桌面窗口 + 托盘 + 悬浮字幕条，`app/desktop.py`；`--glossary` 可透传，其余旗标走
   `--server-only`），或 `.venv\Scripts\python.exe app\desktop.py --glossary glossary/CS.md`
2. 桌面模式无需浏览器（原生窗口）。手机同屏（§7）：字幕页/设置页打开「手机同屏」开关
   （config `lan_enabled`）→ **重启**（监听地址只在启动时读，进程内换不了）→ 扫面板二维码；
   开启后服务监听 0.0.0.0（8787 被占自动换端口，实际值见日志/面板/`/api/lan-qr`），
   关闭态桌面与 --server-only 都只听 127.0.0.1；`--server-only` 后浏览器仍是
   http://localhost:8787（本机不受开关影响）
3. KCL SSO / DeepSeek key：DEEPSEEK_API_KEY 放环境变量或项目根 .env
4. 自检：`.venv\Scripts\python.exe scripts\smoke_test.py`（--gpu/--server/--translate 可选）

## 三·一、GPU 本地顶配（RTX 5060 8GB 实测）
- config.json 默认 large-v3-turbo + device=auto → RTX 5060 上 float16，RTF 0.05，显存 2.2/8GB
- Windows 坑：ctranslate2 首次推理经 PATH 搜 cuBLAS DLL，asr.py 的 _setup_cuda_dlls 已双保险
  （add_dll_directory + 进程 PATH 前插），换机器装了 nvidia-*cu12 即可
- C 盘零占用原则：venv/模型缓存全在 D 盘（HF_HOME=D:\01_Environments\model-cache）
- 档位化（2026-10-13）：首装未选过模型时按硬件自动选档（判定=config.json 有无
  model 键，见 main.user_picked_model；frozen 打包版不参与）；检测只信 nvidia-smi
  （WMI 大显存读数不准）、阈值宁小勿大、可随时手动覆盖

## 四、红线（2026-10-13 用户拍板口径）
- 当前阶段：**免费自用 + 朋友使用（含朋友装机）**；不收费、不注册公司、不公开分发
  （「不分发」仅指不公开分发——Student visa 禁 self-employment）
- 未来计划：可能商业化（含对外发布或出售）；决策**推迟至毕业后**，届时按签证与法律要求评估
- 采集课堂音频仅限本人学习用途，笔记不外传
- 凭据（DeepSeek key）不进 git；transcripts/ 不进 git

## 五、已知限制（v0）
- VAD 是能量门限：背景噪声大时可能切分不准（后续换 Silero VAD）
- whisper 后端固定英文（language=en），中英混杂课堂会漏译中文段
- 无说话人分离：多人群聊场景字幕会混在一起
