# -*- coding: utf-8 -*-
# LectureLens — 课堂实时字幕翻译（MVP v0）
# 签证合规：MVP 免费自用+朋友，不收费不注册公司（Student visa 禁 self-employment）
# 商业化决策推迟至 Graduate visa 或其他合规架构；本警告永久保留于此。

## 一、这是什么
课堂实时字幕翻译：笔记本麦克风采集老师语音 → faster-whisper 实时英文识别
→ DeepSeek 翻译中文 → 浏览器大字幕（中英对照）→ 课后自动存双语笔记。
桌面应用先行；浏览器页面手机连同一 WiFi 也能看字幕。

## 二、结构
- app/capture.py    麦克风采集 + 能量 VAD 分句（自适应噪声底）
- app/asr.py        faster-whisper 封装（默认 CPU int8 small，GPU 可选）
- app/translate.py  DeepSeek 翻译 + 课程术语表注入
- app/main.py       FastAPI + WebSocket + 双语笔记落盘
- app/web/index.html 字幕页（断线自动重连）
- glossary/         课程术语表（每行 `EN term = 中文译名`，--glossary 传入）
- transcripts/      每节课双语笔记（gitignore：含课程内容）
- scripts/build.py  无——本项目无出版管线（区别于菜单项目）

## 三、运行
1. `.venv` 已建好；启动：双击 start_translate.bat 或
   `.venv\Scripts\python.exe app\main.py --glossary glossary/CS.md`
2. 浏览器打开 http://localhost:8787 （手机连同一 WiFi 访问 http://<电脑IP>:8787）
3. KCL SSO / DeepSeek key：DEEPSEEK_API_KEY 放环境变量或项目根 .env

## 四、红线（永久）
- ⚠️ Student visa 禁 self-employment：**不收费、不注册公司、不公开分发**
- 采集课堂音频仅限本人学习用途，笔记不外传
- 凭据（DeepSeek key）不进 git；transcripts/ 不进 git

## 五、已知限制（v0）
- VAD 是能量门限：背景噪声大时可能切分不准（后续换 Silero VAD）
- whisper 后端固定英文（language=en），中英混杂课堂会漏译中文段
- 无说话人分离：多人群聊场景字幕会混在一起
