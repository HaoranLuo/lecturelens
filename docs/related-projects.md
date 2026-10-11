# 相关项目调研（GitHub · 2026-10-10）

> 搜索摘要级（🟡），star/活跃度未逐一进仓库核实。用途：抄架构，不重复造轮子。

## 直接同类（流式 whisper 字幕）

| 项目 | 是什么 | 抄什么 |
|---|---|---|
| [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming) | 查理斯大学 ÚFAL 出品，低延迟流式 whisper（Local Agreement 策略分块） | ⭐ v1 升级首选：替换我们的能量 VAD 分句，延迟和质量都更好 |
| [KoljaB/RealtimeSTT](https://github.com/KoljaB/RealtimeSTT) | faster-whisper + VAD 的实时 STT 库 | 同栈（faster-whisper），VAD 实现可参考 |
| [davabase/whisper_real_time](https://github.com/davabase/whisper_real_time) | 最简实时转写 demo | 入门对照 |

## 部分相关

| 项目 | 关系 |
|---|---|
| [m-bain/whisperX](https://github.com/m-bain/whisperX) | 批式+词级时间戳——课后笔记精校路线可用 |
| [HyperWhisper](https://github.com/hyperwhisper/app) | Deepgram API 桌面实时字幕——云端 ASR 路线参考 |
| HLS 直播流实时转写+18 语翻译（HN 2024 案例） | 和我们场景最接近的已验证案例（2.5 万用户规模） |

## 结论

"whisper 流式识别"项目很多，但**识别+翻译+术语表+双语笔记**整套面向留学生的组合尚无同定位项目——我们的差异化在这个组合，不在单点技术。
