# LectureLens 课堂透镜 — 留学生课堂实时字幕翻译

> 免费自用 + 朋友工具（MVP v0）。**Student visa 禁 self-employment：不收费、不注册公司、不分发**，商业化决策推迟至毕业后。

## 一句话

上课时笔记本采集老师语音 → faster-whisper 实时英文识别 → DeepSeek 翻译中文 → 浏览器大字幕（上英下中）→ 课后自动存双语笔记。

## 快速开始

1. 项目根建 `.env`，写一行 `DEEPSEEK_API_KEY=sk-xxx`（没有 key 也能跑，只出英文字幕不翻译）
2. 双击 `start_translate.bat`（首次自动装依赖，whisper 模型约 250MB 首次自动下载）
3. 浏览器打开 `http://localhost:8787` —— 手机连同一 WiFi 访问 `http://<电脑IP>:8787` 也能看字幕
4. 对麦克风说英语，字幕出现即成功

## 结构

```
app/
  main.py         FastAPI + WebSocket 服务 + 双语笔记落盘
  capture.py      麦克风 16k 采集 + 能量 VAD 分句（自适应噪声底）
  asr.py          faster-whisper 封装（CPU int8 默认，GPU 可选）
  translate.py    DeepSeek 翻译 + 课程术语表注入
  web/index.html  字幕页（断线自动重连）
glossary/         课程术语表（每行 `EN term = 中文译名`）
transcripts/      每节课双语笔记（gitignore：含课程内容，不外传）
```

## 使用

```
python app/main.py --glossary glossary/CS.md        # 课堂模式
python app/main.py --model base                      # 更快（牺牲准确率）
```

字幕页：上英文下中文，自动滚动；翻译到达稍晚于英文 1-3 秒（正常）。课后笔记在 `transcripts/`，双语对照。

## 已知限制（v0）

- 能量 VAD：嘈杂环境切分可能不准（v1 换 Silero VAD）
- 只识别英文（中英混杂课堂会漏译中文段）
- 教室远场收音质量是最大变量——坐前排或用外接麦克风
- 无说话人分离：多人讨论字幕会混

## 路线图

- v1：Silero VAD + 术语表热更新 + 讲师口音自适应
- v2：对话模式（双向翻译）、浏览器插件
- 商业化：**毕业后再议**（签证合规）
