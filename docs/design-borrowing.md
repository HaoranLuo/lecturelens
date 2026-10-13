# 设计与功能借鉴清单（GitHub 调研）

> 核验方式：2026-10-13 经 GitHub API 实时核验仓库存在性、星数、描述（非凭印象）；
> 三个深潜项目的功能清单来自其 README 原文。
> 用途：为 LectureLens 桌面软件化（P1-P4 路线，见 §五）提供设计参考与功能候选。

## 一、同行项目深潜（做的事情和我们几乎一样）

### 1. begin0808/LiveCaption — 99 ⭐
https://github.com/begin0808/LiveCaption
浏览器标签页音频 → 实时双语字幕（Silero VAD + SenseVoice + DeepSeek/Ollama）。
技术选型与我们高度重合，产品形态是"浏览器扩展 + 本地 Python 后端"。
**深挖到的设计点：**
- 毛玻璃（glassmorphism）半透明悬浮字幕条：可拖拽、双击复位、鼠标点击穿透（不挡视频控制）
- 可选历史行：当前句上方保留 0-2 行半透明缩小前文
- 翻译引擎降级链：DeepSeek → Ollama（qwen2.5:3b，约 2GB）→ 免费 Google 兜底
- 细粒度可调参数全部开放为设置：断句静音时长（含"0.8s 修字头丢失"补丁）、最大句长、历史行数
- 全离线 Windows 一体包（模型 + 启动器 bat 打包成 zip）
- 教训：SenseVoice 不支持欧洲语言 → 我们 faster-whisper 选型在英文课上占优

### 2. Zackriya-Solutions/meetily — 31,662 ⭐
https://github.com/Zackriya-Solutions/meetily
隐私优先本地会议助手：实时转写（Parakeet/Whisper）+ AI 摘要 + 纪要编辑器，Tauri + Next.js。
**深挖到的设计点：**
- Home 会议 hub（列表管理）+ 摘要视图 + **可编辑纪要编辑器**（AI 摘要后人工修订再导出）
- 导入已有音频文件重转写（可换模型/换语言）
- 麦克风 + 系统声音混音（ducking 压低背景声、防削波）
- 摘要引擎可选：Ollama 本地 / Claude / Groq / 自定义 OpenAI 兼容端点
- PRO 功能思路可借鉴：自定义摘要模板、PDF/DOCX/Markdown 导出、与会议内容对话、自动会议检测

### 3. abb128/LiveCaptions — 1,803 ⭐
https://github.com/abb128/LiveCaptions
Linux 桌面实时字幕（C 语言）。桌面字幕窗交互的先行者，窗口行为细节值得对照。

## 二、工作台美学标杆（照着微调我们的工作台）

| 项目 | ⭐ | 抄什么 |
|---|---|---|
| [open-webui/open-webui](https://github.com/open-webui/open-webui) | 154,928 | 暗色工作台/设置页排版、模型管理器（下载进度/删除/默认选中）的成熟范式 |
| [lobehub/lobehub](https://github.com/lobehub/lobehub) | 83,179 | 品牌件（logo/吉祥物/微交互）、列表卡片密度、流畅过渡 |
| [nextai-translator/nextai-translator](https://github.com/nextai-translator/nextai-translator) | 24,995 | 划词交互、快捷键体系、Tauri 桌面翻译应用的界面密度 |
| [pot-app/pot-desktop](https://github.com/pot-app/pot-desktop) | 19,383 | 同上；多服务配置的组织方式 |

## 三、功能模式库

| 项目 | ⭐ | 抄什么 |
|---|---|---|
| [immersive-translate/immersive-translate](https://github.com/immersive-translate/immersive-translate) | 19,319 | 双语"保留上下文"对照排版；术语库（glossary）产品化面板；**Trust Center 式隐私透明页**；安装→一键翻译 onboarding。注意：该项目非开源，只参考 UX 不看代码 |
| [chidiwilliams/buzz](https://github.com/chidiwilliams/buzz) | 21,956 | 模型管理器（下载/删除/进度）、导出格式 TXT/SRT/VTT |
| [thewh1teagle/vibe](https://github.com/thewh1teagle/vibe) | 7,762 | 同上；"Transcribe on your own!"的本地优先叙事 |

## 四、可借鉴项总表（B1-B12）

| # | 借鉴项 | 来源 | 落地方式 | 建议阶段 |
|---|---|---|---|---|
| B1 | 毛玻璃悬浮字幕条（拖拽/穿透/复位） | LiveCaption | pywebview 无边框置顶小窗，字幕可钉在任意应用上 | P1 |
| B2 | 历史行渐隐（当前句上方 0-2 行半透明前文） | LiveCaption | 字幕区前端改造，与 1000 条历史回看共存 | P2 |
| B3 | 翻译引擎降级链（DeepSeek→本地 Ollama→兜底） | LiveCaption | translate.py 加 provider 链；教室断网也能出字幕 | P4 |
| B4 | 断句参数面板（静音时长/最大句长/历史行数） | LiveCaption | capture.py 常量提升为 config 白名单键 + 设置页"高级"区 | P2 |
| B5 | 可编辑 AI 摘要（生成→人工修订→导出） | Meetily | 摘要页加编辑态，存 *.summary.edited.md | P4 |
| B6 | 导入音频重转写（换模型/换语言） | Meetily | 队列化离线转写端点，复用现有管线 | P4 |
| B7 | 麦克风+系统声音混音（ducking） | Meetily | 音频前端重构，两路合成后再进 VAD | P4/后续 |
| B8 | 术语库管理 UI（增删改查/分组） | immersive-translate | glossary/*.md 的表格化编辑页，保存即热重载（引擎已支持） | P2 |
| B9 | 隐私透明页（数据去向一页说清） | immersive-translate | 设置→"数据去向"：音频不出电脑/文字发 DeepSeek/笔记位置 | P1 |
| B10 | 多格式导出（SRT/VTT/TXT） | Buzz / Vibe | 时间戳已有（词级时间戳在流式管线），生成 SRT 成本低 | P2 |
| B11 | 工作台视觉对照实现 | Open WebUI / lobehub | P1 实施时对照其暗色排版微调密度与间距 | P1 |
| B12 | 桌面翻译工具的交互密度/快捷键体系 | nextai-translator / pot | P1 壳的窗口行为 + P3 快捷键参考 | P1/P3 |

## 五、路线对照（P1-P4 定义，含本次并入项）

> 源自产品化建议（三步走），本清单并入后为四阶段。均为免费自用+朋友，不碰签证红线。

- **P1 壳与颜值**（≈5-6 人日）：pywebview 原生窗口 + 托盘 + 应用图标 + 设计规范（docs/design-spec.md）落地 + 首启向导 + 设置页 + **B1 悬浮字幕条 + B9 隐私透明页**
- **P2 产品化**（≈5-6 人日）：笔记中心（浏览/搜索/双栏/PDF-Word 导出）+ 课程管理 + 模型管理器 + **B2 历史行渐隐 + B4 断句面板 + B8 术语库 UI + B10 SRT 导出**
- **P3 打磨**（≈3 人日）：全局快捷键 + 术语高亮 + 开机自启 + 品牌件（logo/图标）+ **B12 交互细节**
- **P4 学习闭环**（≈5-6 人日，新增）：**B5 可编辑摘要 + B6 导入重转写 + B3 翻译降级链 + B7 混音**

## 六、已核对未采用的（留档防重复调研）

- SimulStreaming：需 ≥10GB 显存（本机 8GB），且 torch 栈无法替换 faster-whisper——v0.3 已裁定不用
- braden-w/whispering：已迁移至 EpicenterHQ/epicenter，方向变为通用 dictation，不再对口
- Tauri：单开发者 + 无 Rust 工具链，维护成本不划算（PyInstaller + pywebview 覆盖同等需求）
