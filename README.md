# LectureLens 课堂透镜 — 留学生课堂实时字幕翻译

> 免费自用 + 朋友工具（MVP v0）。**Student visa 禁 self-employment：不收费、不注册公司、不分发**，商业化决策推迟至毕业后。

## 一句话

上课时笔记本采集老师语音 → faster-whisper 实时英文识别 → DeepSeek 翻译中文 → 浏览器大字幕（上英下中）→ 课后自动存双语笔记。

## 快速开始

1. 项目根建 `.env`，写一行 `DEEPSEEK_API_KEY=sk-xxx`（没有 key 也能跑，只出英文字幕不翻译）
2. 双击 `start_translate.bat` —— 打开**桌面窗口**（原生窗口 + 系统托盘 + 悬浮字幕条，
   首次自动装依赖，whisper 模型约 250MB 首次自动下载）。关闭窗口 = 隐藏到托盘，
   真退出走托盘菜单；首次启动有 3 步向导（麦克风测试 → DeepSeek key → 试说一句）
3. 课程术语表：`start_translate.bat --glossary glossary/CS.md`（可多次）
4. 手机同 WiFi 看字幕（LAN 模式）：`start_translate.bat --server-only`，然后访问
   `http://<电脑IP>:8787`（桌面模式只监听本机 127.0.0.1 随机端口，不对外；LAN 访问需携带
   token，首启已打印、设置页「安全」卡可重新生成）
5. 对麦克风说英语，字幕出现即成功

## 结构

```
app/
  main.py         FastAPI + WebSocket 服务 + 双语笔记落盘
  capture.py      麦克风 16k 采集 + VAD 分句（默认 Silero，备选能量门限）
  asr.py          faster-whisper 封装（CPU int8 默认，GPU 可选）
  translate.py    DeepSeek 翻译 + 课程术语表注入（含 whisper initial_prompt）
  web/index.html  字幕页（断线自动重连 + 底部麦克风/翻译状态）
glossary/         课程术语表（每行 `EN term = 中文译名`）
scripts/
  smoke_test.py   全管线自检（无需麦克风：VAD + TTS→ASR + 翻译 + 服务）
transcripts/      每节课双语笔记（gitignore：含课程内容，不外传）
```

## 使用

```
python app/main.py --glossary glossary/CS.md        # 课堂模式
python app/main.py --gpu                             # 强制 CUDA（默认按 config.json 的 device=auto）
python app/main.py --model base                      # 临时换模型（CLI 覆盖 config.json）
python app/main.py --vad energy                      # 噪声底自适应的能量门限（默认 silero）
python app/main.py --list-devices                    # 列出麦克风设备（配 --device 用）
.venv/Scripts/python.exe scripts/smoke_test.py       # 不用麦克风的自检
.venv/Scripts/python.exe scripts/smoke_test.py --gpu # GPU 基准（RTF + 显存）
```

### 配置（config.json）

模型/设备/VAD 持久化在项目根 `config.json`，字幕页可热切换（无需重启）。首次部署从模板复制：`copy config.example.json config.json`（键集合以 app/main.py 的 `ALLOWED_CONFIG_KEYS` 白名单为准，非法值会被 /api/config 拒绝且不落盘）：

```json
{ "model": "large-v3-turbo", "device": "auto", "compute_type": null, "vad": "silero" }
```

- **GPU 本地顶配**（RTX 5060 8GB 实测）：large-v3-turbo / float16，7.5s 音频 0.35s 出结果（RTF 0.05），显存 2.2/8.0 GB。依赖：`nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（已装在 .venv）
- CLI 参数（--model/--gpu/--vad）只当次生效，不改文件；字幕页切换会写回 config.json

### 字幕页

- 顶部工具条（可折叠）：暂停/继续收音、模型热切换、VAD 电平条（黄线=触发门限）、字号调节、导出笔记、清屏、全屏
- 收起工具条即纯字幕沉浸模式；字号记忆在浏览器
- 上英文下中文，自动滚动；翻译到达稍晚于英文 1-3 秒（正常）
- **开课前看一眼电平条随说话跳动就说明收音正常**；导出 = 下载当前 `transcripts/*.md`
- 手机连同一 WiFi 开 `http://<电脑IP>:8787`（打不开多半是 Windows 防火墙拦 8787，首次运行允许"专用网络"）

手机访问：电脑和手机连同一 WiFi，手机浏览器开 `http://<电脑IP>:8787`。若手机打不开，多半是 Windows 防火墙拦了 8787（首次运行时允许"专用网络"即可）。

## 已知限制（v0.1）

- 单说话人：多人讨论字幕会混
- 只识别英文（中英混杂课堂会漏译中文段）
- 教室远场收音质量是最大变量——坐前排或用外接麦克风
- 能量 VAD（--vad energy）在极嘈杂环境仍不如 silero，仅作回退

## 路线图

- v1：术语表热更新 + 讲师口音自适应
- v2：对话模式（双向翻译）、浏览器插件
- 商业化：**毕业后再议**（签证合规）
