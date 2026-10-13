# LectureLens 升级计划（终稿，v0.2 → v1.0）

> **依据**：后端审计、界面审计、流式字幕调研、生态调研四份输入 + 草案 + 技术评审、实用评审两份意见，经本次对仓库逐条代码复核后整合（2026-10-12，main 工作区）。
> 两位评审 verdict 均为 needs-revision。**38 条意见逐条处理：33 条采纳、3 条采信（确认类）、2 条部分采纳并转待裁决**，逐条对照见文末附录「评审意见处理」。
> 本次为规划整合：**未修改任何代码，未运行 smoke_test / pytest / 未启动服务**（与草案第 7 节声明口径一致，见 §2.5）。

---

## 0. 调查结论

### 0.1 本次核验（我实际读过 / 跑过的）

**逐行实读**：`app/main.py`（402 行）、`app/capture.py`（221 行）、`app/asr.py`（102 行）、`app/translate.py`（96 行）、`app/web/index.html`（274 行）、`scripts/smoke_test.py`（318 行）、`start_translate.bat`、`requirements.txt`、`.gitignore`、`config.json`、README.md 关键行。

**实际执行的检查**：

| 检查 | 命令 | 结果 |
|---|---|---|
| 根目录卫生 | `ls -la`；`[ -d tests ]` | `nul` 文件存在（115 字节）；两份 `.backup_*.tar.gz` 被 `.gitignore` 第 7 行 `.backup_*` 覆盖（草案 4.8 修正属实）；**无 tests/ 目录** |
| venv 依赖 | `.venv/Scripts/python.exe -m pip list` | faster-whisper 1.2.1、sounddevice 0.5.6、ctranslate2 4.8.2、httpx 0.28.1、numpy 2.5.4；**无 scipy / genanki / PyAudioWPatch**（后三者为计划内新增依赖） |
| loopback 前提 | `.venv/Scripts/python.exe -c "import sounddevice as sd, inspect; print(inspect.signature(sd.WasapiSettings.__init__))"` | `(self, exclusive=False, auto_convert=False, explicit_sample_format=False)` —— **无 loopback 参数**，条目 14 选 PyAudioWPatch 的前提成立 |
| README 口径 | `grep` 关键行 | :3「免费自用 + 朋友工具…不分发」、:51「RTF 0.05 · 显存 2.2/8.0 GB」、:59「开课前看一眼电平条」、:67「中英混杂课堂会漏译」、:68「教室远场收音是最大变量」均属实 |

**关键代码事实（两评审与草案结论一致，本次逐条复核命中）**：

- **串行互锁**：`main.py:328-352`——每段 `await` 转写 → 广播 EN → `await` 翻译（`translate.py:62` 单次超时 20.0s）才取下一段；`seg_q` 为无界 `queue.Queue`（`main.py:282`）。
- **麦克风线程无保护**：`capture.py:196-206` 的 `run()` 完全无 try/except；`start()` 起的是 daemon 线程（`main.py:208-210`），异常即静默死亡，UI 零提示。
- **modelBusy 真 bug**：`index.html:213-216` 的 fetch 无 try/catch → 失败后 `modelBusy` 永真 → `index.html:240` 暂停键永久禁用（chipDevice 文案会被 2s 轮询覆盖恢复，真正卡死的是暂停键）。
- **配置污染双路径**：`main.py:178-182` 无校验先赋值 `runtime['config']`；`main.py:185` `save_config` 先于异步构建——非法 model 会永久写坏 config.json，**且「finally 持久化」修不掉运行时污染**（赋值已发生）。
- **笔记现状是每条 open→write→close 即时落盘**（`main.py:94-98`），崩溃耐久性已经良好——草案「2s 缓冲解决崩溃丢尾部」的动机不成立（技术评审修正，已吸收）。
- **音频采集时间戳**在 seg_q 元组首元素（`capture.py:191` `put((time.time(), audio))`），`main.py:328` `_, audio = ...` 丢弃；payload 的 `ts`（`main.py:342`）只是广播时刻墙钟字符串——延迟指标必须以采集时间戳贯穿管线。
- **流式改造的音频出口缺口属实（阻断级）**：原始音频块只在 MicSegmenter 内部消费（`capture.py:193-194` → `201-206`），seg_q 只出定稿段（`capture.py:191`）；`asr.py:87-88` 硬编码 `beam_size=1 / vad_filter=True`；普通 `threading.Lock` 表达不了「定稿优先」。
- **真正的初始化炸弹是 `hub.start_transcript()`（`main.py:293`，在 try 之外）**：磁盘满/权限错误会炸掉整个 asr_loop 任务（`main.py:387` 创建，无 done_callback）；而 `apply_segmenter` 几乎不抛——make_vad 的 silero 失败会回退 energy（`capture.py:118-125`），EnergyVAD 纯 numpy 构造不抛，InputStream 构造发生在 `seg.start()` 的线程内。
- **鉴权落地将破坏现有 smoke_test**：`smoke_test.py:191-204` 是无 token 裸 socket WS 握手；`smoke_test.py:273` 用 `proc.terminate()` 杀服务（Windows = TerminateProcess，不走 finally/atexit）——任何「close 时 flush」的设想都不可依赖。
- **LAN 暴露今天就存在**：服务绑 0.0.0.0（`main.py:397`）+ 手机同 WiFi 场景（README.md:60），与「是否分发给朋友」无关——这是 token 鉴权（条目 19）保留的独立理由。
- 硬编码三处：bat `cd /d D:\projects\lecture-translate`（bat:5，bat:2-3 注释自证 cmd 按 GBK 解析）、`translate.py:88-90` 硬编码绝对路径密钥兜底、`main.py:150` `device=ARGS.device` 与 `main.py:133` `language=ARGS.lang`（config 化必须改这两行）。

### 0.2 硬约束与总原则（修订版）

- **单开发者课余开发**：1 人日 = 一个完整晚间或周末半天；实测节奏约 2.5–3.5 人日/周。阶段按里程碑切片（§3.1），**每个里程碑结束都是可用状态**，可随时停下使用。
- **零预算、本地方案**：LLM 继续用 DeepSeek（调研口径 2026-10 定价下笔记/复习卡每节课 ≈ $0.01–0.05；来源：https://api-docs.deepseek.com/quick_start/pricing ）。
- **依赖口径（修正草案的自相矛盾）**：原「只新增 genanki 1 个依赖」与条目 14（PyAudioWPatch）/15 本就冲突，修正为——**v0.2/v0.3 零新增；v0.4 新增 PyAudioWPatch + scipy（仅用 `scipy.signal.resample_poly` 一个函数）；v0.5 新增 genanki**。共 3 项，分属 3 个阶段，无隐蔽依赖。
- **不推翻现有管线**：faster-whisper GPU + silero VAD 分句 + DeepSeek 翻译三件套原样保留。
- **红线**：免费自用，不收费、不注册公司、不公开分发（README.md:3）。「给朋友装用」与「不分发」的边界**待用户裁决**（§3.3-1），裁决前条目 20/21 不启动。
- **每项固定四维**：调查 → 设计 → 实现 → 审查 + 检验；检验一律衔接 `scripts/smoke_test.py` 扩展或 pytest（§2）。

### 0.3 架构保护清单（升级中不可回退，表述修订）

1. **capture.py 分句状态机逻辑**（0.4s preroll `capture.py:146,162`、500ms 最短语音 `capture.py:177-183`、15s 上限 `capture.py:164-165`、能量 VAD 10 分位噪声底 + 滞回 `capture.py:47-60`、silero→energy 回退 `capture.py:118-125`）——**精确表述（技术评审指出原表述过宽）：状态机逻辑不动；采集循环（`capture.py:201-206`）允许增加「可选扇出」一行**（条目 11 的唯一触碰点，默认关闭时行为零变化）。
2. **「EN 即时广播、ZH 到货补发同一 payload」**（`main.py:343-352` + `index.html:155-163` patchZh）——并发化后语义不变。
3. **字幕排版参数**（暗底 + 中文金黄 #FFD97A、上英下中、zh 32px/600、900px 限宽、18–44px 字号 + localStorage，`index.html:8-9,64,68,129-137`）。
4. **模型热切换原子替换语义**（build_asr 成功才替换 `main.py:136`；reloading 防并发 `main.py:190-191`；加载打点 `main.py:130-141`）。
5. **smoke_test 五段式自检纪律**（`scripts/smoke_test.py:1-9,207-287`，`record()`/exit-code/无残留进程检查）——所有验收挂在这套骨架上扩展。

### 0.4 路线总览

| 阶段 | 主题 | 条目 | 人日 | 排序理由 |
|---|---|---|---|---|
| **v0.2** | 可靠性加固：丢字幕不可能静默发生 | 1–10 | ≈14（m1: 1–4 / m2: 5–7 / m3: 8–10）+ 真实课堂终检（0 成本里程碑） | 收音死、配置写坏、翻译静默丢、UI 卡死都是上课当天踩的故障 |
| **v0.3** | 流式 partial 字幕（LocalAgreement-2） | 11–13 | ≈8（技术评审重排口径） | 顺序问题见 §3.3-2 待裁决，默认流式先行 |
| **v0.4** | 音源与上下文扩展 | 14–16 | ≈5.5 | Zoom 内录 + 翻译上下文一致性 |
| **v0.5** | 课程学习套件 | 17–18 | ≈5 | 依赖 v0.2 笔记重构 + 自用反馈 |
| **v1.0** | 分发与安全打磨（**待裁决**） | 19–21 | ≈6（19: 1.5 + 20: 4.5；21 可选 +4） | 19 因 LAN 暴露独立成立；20/21 挂裁决 |
| **合计** | | **21 项** | ≈38.5（不含可选条目 21 ≈ 34.5） | |

依赖关系：条目 1（麦克风守护）先行；条目 2（测试骨架）与条目 1 同周补齐（条目 1 的测试随骨架落地）；条目 3（日志）先于条目 5（重构期排障直接受益，两评审一致）；条目 5 依赖 3、4；条目 6 依赖 5（translate_loop）；条目 11 依赖 1、5；条目 12 依赖 9、11；条目 13/15 依赖 4（校验框架）；条目 16 依赖 5；条目 17/18 依赖 5（笔记单写者）；条目 19 依赖 4；条目 20 依赖 19。

---

## 1. 升级项分阶段详表

### v0.2 —— 可靠性加固（条目 1–10，≈14 人日）

#### 条目 1：麦克风线程守护——异常捕获、自动重开与「听不到声音」诊断（2 人日）

- **动机**（实用评审裁定为 v0.2 第一顺位）：延迟 2–5 秒尚可容忍听课，**收音死是整节课字幕全灭**。`capture.py:196-206` 的 `run()` 无任何 try/except，麦克风被 Teams/Zoom 独占时线程静默死亡，seg_q 永远为空、HTTP 照常、电平表冻结、UI 零提示——README.md:59 目前只能靠「开课前看一眼电平条」人工发现。
- **设计**：`run()` 改外层守护循环：InputStream 构造/回调异常 → logging 记录 → 指数退避重开（1s/2s/4s…上限 30s）；连续 5 次失败 → `status['mic']='error: 可能被其他应用占用'` 并熔断停止重试。新增无信号诊断：5s 滑窗 RMS 峰值持续 60s < 1e-5 → `status['mic']='no_signal?'`（区分「没人说话」与「设备没送数据」，只提示不动作）。`status_loop`（`main.py:241-264`）把 mic 状态一并送出，前端红横幅（衔接条目 8）。
- **风险与回退**：设备热插拔抖动——退避上限 + 5 连败熔断即回退机制；no_signal 误报——阈值保守且仅提示。
- **审查**：重开不泄漏旧 stream 句柄（with 语句）、单线程内重开无线程泄漏。

#### 条目 2：pytest 测试骨架（原条目 7，CI 推迟）（1.5 人日）

- **动机**：仓库无 tests/ 目录（本次实测）；smoke_test 依赖本机 Windows SAPI TTS、GPU、真实网络与 API 余额（`smoke_test.py:85-102,170-187`）；分句状态机与硬件解耦可纯测（`capture.py:157-191`）却无单测；后续大量重构需要回归网。
- **设计**：① `requirements-dev.txt`（pytest、pytest-asyncio）；② `tests/` 全套清单见 §2.3；③ **conftest 级全局隔离（技术评审 missing 项）**：CONFIG_PATH 注入 tmp_path 的 fixture 全局生效——否则条目 4/5/6 的测试一旦触发 `save_config`（`main.py:47-48`）会写坏真实 config.json；随机种子固定（沿用 `smoke_test.py:40` 的 rng(42) 惯例）；④ smoke_test 加 `--vad-only` 参数（只跑 [1] 段）。**GitHub Actions 推迟至 v1.0 真分发前（实用评审：单人私有仓库 CI 是持续维护成本无对等收益），卫生断言等全部由本地 pytest 承担。**
- **风险与回退**：无；CI 若未来启用，windows-latest + pytest + `--vad-only` 的设计已在 §2.3 预留。

#### 条目 3：结构化日志落盘（原条目 5，提前到并发重构之前）（1 人日）

- **动机**：全部日志 print 到 stdout（`main.py:199,280,292,333,355` 等），无落盘无轮转，课后无法回答「那节课为什么没字幕」；`main.py:249-250,262-263` 的 nvidia-smi / VAD snapshot 异常全部静默 `pass`。
- **设计**：标准库 `logging`：`RotatingFileHandler(logs/lecturelens.log, maxBytes=5MB, backupCount=5, **encoding='utf-8'**)` + console handler（技术评审：本机 cmd 是 GBK 代码页，bat:2-3 注释自证——不显式 utf-8 中文必乱码；**uvicorn logger 纳入同一落盘配置**）。全部 print 替换为 logger；两处 `except: pass` 降级为 debug/warning；关键事件打点：启动/配置变更/模型加载起止/每段转写耗时/翻译失败与重试/麦克风重开。**脱敏**：Authorization 头与 API key 任何情况不落日志（key 只从 env/.env 读，`translate.py:83-95`）。
- **风险与回退**：磁盘占用 5MB×5 封顶；无回退必要。

#### 条目 4：配置写入安全——先验后赋值 + 先验后存（从原条目 16 拆出提前）（0.5 人日）

- **动机**（两评审一致，实用评审裁定为数据损坏 bug 不应排队到 v1.0）：① `main.py:178-182` 对 body 无校验先赋值 `runtime['config']`——非法 model 名立刻污染运行时，后续 `/api/config` 的 diff 基线已坏；② `main.py:185` `save_config` 先于异步构建——非法值**永久写进 config.json**，下次启动加载失败。两者是同一条链上的数据损坏 bug。
- **设计**：① 引入 `ALLOWED_CONFIG_KEYS` 常量 + 每键白名单校验（model ∈ {tiny,base,small,medium,large-v3,large-v3-turbo}、device ∈ {auto,cpu,cuda}、compute_type ∈ {None,int8,float16,int8_float16}、vad ∈ {silero,energy}；lang/source/input_device 等随条目 13/14/15 扩充；**本计划自增键 pipeline.serial、streaming.enabled、translate.model、auth.enabled 一律纳入**——技术评审 missing 项）；② POST `/api/config` **先整体校验再动任何状态**：非法值 400 且 runtime['config']、config.json、status 全部不碰；③ 即时键（vad 等）校验通过 → 赋值 → 保存 → apply_segmenter（现状语义）；④ 模型键（model/compute_type）改 **save-after-build**：以 overrides 构建，成功才合并进 `runtime['config']` 并持久化，失败仅置 `status['asr']=error`——runtime 与文件零污染；⑤ reloading 期间语义保持 `main.py:190-191`。
- **风险与回退**：无（纯收紧）；白名单过窄时先放宽常量。

#### 条目 5：编排层并发解耦——拆队 + 有界队列 + 积压可视化 + 暂停语义（3 人日）

- **动机**：`main.py:328-352` 串行互锁（见 §0.1），DeepSeek 往返阻塞下一句转写；`main.py:282` 队列无界、UI 无提示。
- **设计（吸收四条评审修正）**：
  - 拆 `asr_loop`（seg_q.get → transcribe → 广播 EN → put）与 `translate_loop`（get → await translate → 补 zh → 二次广播 + 笔记）两个 asyncio 任务 + `translate_q(maxsize=8)`；队列满**丢最旧**并置 `status['backlog']`，UI 黄条「翻译落后 N 段」。
  - **（技术评审）translate_q 用 `asyncio.Queue` 而非 queue.Queue+to_thread**——两端都在事件循环内，`main.py:328` 的 `seg_q.get` 现状已付一次 to_thread 往返，新队列不复制该模式。
  - **（技术评审，动机反转）笔记写入不做 2s 批量缓冲**：现状每条 open→write→close 即时落盘（`main.py:94-98`），崩溃耐久性已经良好，批量缓冲反而把丢失窗口扩大到 2s，且 Windows `proc.terminate()`=TerminateProcess 不走 finally/atexit（`smoke_test.py:273` 即如此杀服务）。改为**单写者有序窗口**：pending dict 按 seg 排序，seg 连续即写、**写即 flush**；ZH 到货晚于窗口（>2 段或超 10s）→ 写 EN 行 + `> ⚠ seg:N 翻译缺失` 标记行，后到 ZH 走条目 6 的补译小节（笔记保持 append-only）。解决的是并发化引入的**乱序**，不是崩溃。
  - **（实用评审，断链修复）丢弃路径必须写缺失标记**：队列满丢最旧的段，其 EN 已广播、笔记有 EN 行——若只靠「翻译返回 None」的标记扫描，backfill 扫不到被丢弃段，该段中文永久不可达。丢弃时同步写 ⚠ 标记行。
  - **（技术评审，暂停语义补全）**：暂停（`main.py:314-322` 现只排空 seg_q）扩展为——同时排空 translate_q 中未翻译段（丢弃并写缺失标记，保证「暂停不回放」）；**已在飞的单次翻译放行完成并正常广播+写笔记**（它属于暂停前已出现的句子，是自然收尾而非回放；资源已花，不浪费）。
  - `config.json` 加 `pipeline.serial`（默认 false）一键回退旧行为。
- **风险与回退**：ZH 补发乱序——patchZh 按 seg 定位天然支持；回退 = `pipeline.serial=true`。
- **审查**：对照保护清单第 2 条逐行核对「EN 先出、ZH 补发」未变；两任务对 status/hub 的访问均在事件循环单线程内，无新增锁需求。

#### 条目 6：翻译失败重试、占位补偿与课后补译（1.5 人日）

- **动机**：`translate.py:77-78` raise 后被 `main.py:354-355` 捕获仅 print——该句中文永久丢失。
- **设计（吸收技术评审超时冲突修正）**：重试改 **deadline 预算制**——总预算 10s，单次尝试超时 = min(5s, 剩余预算)，退避 1s/2s 计入预算；429 优先读 `Retry-After`；最多 3 次尝试。最坏情况 ≈10s（草案「3 次×20s+5s=65s」与 10s 上限矛盾，且 65s 卡在 maxsize=8 的队列即全管线停摆——已修）。最终失败返回 None → `payload['zh_fail']=true` → 前端灰色「翻译暂缺」角标 + ⚠ 标记行 + `status['translate_misses']`。新增 `scripts/backfill_translations.py`：课后扫描 ⚠ 标记（含条目 5 的丢弃标记）重译回填「补译」小节。**测试 seam（技术评审）**：`DeepSeekTranslator` 构造函数加 `transport=None` 注入口（现状 `translate.py:60-63` 自建 AsyncClient，MockTransport 无法注入）。API key 继续只从 env/.env 读。
- **约束**：重试只覆盖网络/429/5xx，4xx 参数错误不重试。
- **风险与回退**：重试加剧积压——有界队列 + 预算制兜底；重试次数配置化，置 0 即旧行为。

#### 条目 7：asr_loop 初始化加固 + asyncio watchdog（原条目 4，动机改写）（1 人日）

- **动机（实用评审改写）**：`apply_segmenter`（`main.py:283`）实际几乎不会抛异常——make_vad 有 energy 回退（`capture.py:118-125`）、EnergyVAD 纯 numpy 构造不抛、InputStream 构造在 seg 线程内。**真正在 try 之外的炸弹是 `main.py:293` 的 `hub.start_transcript()`**——磁盘满/权限错误会炸掉整个 asr_loop 任务且无 watchdog，FastAPI 照常、status 停旧值。
- **设计**：① `start_transcript` 移入 try（含 `TRANSCRIPTS.mkdir` 的 parents/错误处理），失败 → `status['pipeline']='fatal: ...'`；② asr_loop 主体包顶层 try/except，意外异常 → logging.critical + fatal 标记；③ `main.py:387` 的 task 挂 `add_done_callback`：区分 `CancelledError`（正常关停），其余 → `status['pipeline']='dead'` + 前端黄条「字幕引擎已停止，请重启服务」；④ fatal 后 /api/config 修复成功要清 fatal 标志。
- **检验见 §2.2-7**；回退无必要（纯加固）。

#### 条目 8：前端错误反馈面修复（含 modelBusy 真 bug）（2 人日）

- **动机**：全部核对属实（§0.1）：① modelBusy 真 bug（`index.html:213-216,240`）；② WS 断线仅圆点变灰 + 2s 静默重连（`index.html:169-172`）；③ `post()` catch 返回 null、调用处 `index.html:190` 不判空；④ 导出直接 `location.href`（`index.html:191`），无笔记时整页跳到 404 JSON（`main.py:225`）；⑤ `index.html:244` 渲染后端原始 error。
- **设计**：① onchange 包 try/catch + finally 复位 modelBusy；② WS `onclose` → 红横幅 + 指数退避重连（2s→10s 封顶），`onopen` 移除；③ post 失败 → toast；④ 导出改 fetch → 404 toast「笔记还未生成」/ 200 blob 下载；⑤ 错误中英映射表（cuda/cublas/下载失败/显存 → 一句中文 + 「切小模型重试」按钮）；⑥ 手机端 CSS 级修复（触控 ≥44px、flex-wrap、100svh、aria-label）。
- **检验见 §2.2-8**；对照保护清单第 3 条核对排版未动。

#### 条目 9：字幕历史回看 + 中文落空可见化（**新增**，实用评审）（1 人日）

- **动机（实用评审：全计划价值/成本比最高的缺失项）**：`index.html:151` 超过 200 条 DOM 即 removeChild 挤掉最旧——「没听懂回头看老师几分钟前说什么」是上课高频刚需，18 项原条目无一覆盖；且 patchZh（`index.html:155-163`）在裁剪后到达的 ZH 会**静默丢弃**（技术评审补充），并发化+积压下窗口变大。
- **设计**：① 裁剪上限 200 → 1000（内存 ≈ 数百 KB，可承受），上限做成常量；② 「↓ 回到最新」悬浮按钮：scroll 监听，离底 >80px 显示，点击回底并恢复自动吸底；③ partial 不计入裁剪计数（衔接条目 12）；④ patchZh 落空 → footer 计数「N 条翻译因历史裁剪未显示」+ WS 上行 `{"type":"ui_metrics","zh_dropped":N}` 并入 `/api/status`（复用 `/ws` 现有 receive_text 循环，`main.py:233-234`）。
- **风险与回退**：纯前端增量；上限调回 200 即旧行为。

#### 条目 10：工程卫生包（0.5 人日）

- **动机**：`nul` 文件（`ls -la` 实测 115 字节）；config.json 未入库也未 ignore（git status `?? config.json`）；无 config.example.json；bat:5 硬编码路径、无 python 前置检查；`translate.py:88-90` 硬编码绝对路径。**保留草案 4.8 修正**：.backup_*.tar.gz 已被 `.gitignore:7` 覆盖，无需处理。
- **设计**：① 删 `nul`（`del \\?\D:\projects\lecture-translate\nul`）；② .gitignore 追加 `config.json`、`nul`；③ `config.example.json`（键集合以 `ALLOWED_CONFIG_KEYS` 常量为准，随阶段演进不腐烂）；④ bat 改 `%~dp0` + `where python` 前置检查；⑤ 删除 `translate.py:88-90` 硬编码路径（`main.py:299` 已传 ROOT/.env）；⑥ 术语表加载警告进 logging。
- **检验见 §2.2-10**；README 同步「首次部署从 example 复制」一句。

### v0.3 —— 流式 partial 字幕（条目 11–13，≈8 人日）

#### 条目 11：LocalAgreement-2 流式管线核心（含音频扇出与解码调度）（6 人日）

- **动机**：现状「VAD 静音 700ms 收尾后才出整句」（`capture.py:134,143`），流式 partial 可把 EN 首显提前到约 1.5–2.5s（推断值，检验实测）。技术评审裁定草案设计**阻断级缺口**：原始音频无出口、asr 签名不可用、Lock 表达不了定稿优先、热切换交接未定义——本条目按 8–10 人日口径重排吸收。
- **调查**（转引自流式调研，保持草案声明）：首选 **ufal/whisper_streaming 的 LocalAgreement-2**（MIT、单文件），确认 = 最近两次假设最长公共前缀（`HypothesisBuffer.flush()`）；`buffer_trimming=('segment',15)`；已提交文本 200 字符后缀作 prompt；词级时间戳依赖 `word_timestamps=True`。来源：https://github.com/ufal/whisper_streaming ；论文口径端到端延迟 3.3s（Macháček et al. 2023）：https://arxiv.org/abs/2307.14743 。**不采用 SimulStreaming**（torch 栈不能替换 asr.py、要求 ≥10GB 显存 vs 本机 8GB、turbo 不支持、延迟数据无可靠来源——详见 https://github.com/ufal/SimulStreaming 、https://aclanthology.org/2025.iwslt-1.41/ ）；备选 **WhisperLiveKit**（https://github.com/QuentinFuxa/WhisperLiveKit ，其基准页无两政策对比且记录 SimulStreaming-turbo WER 18.1% 异常）——仅当自移植受阻时试，切换前必须自测质量。
- **设计（补全四项缺口）**：
  - **音频扇出**：`MicSegmenter` 构造加可选 `stream_q=None` 参数；`run()` 循环（`capture.py:201-206`）取出块后 `handle_block(...)` 原样不动，随后 `if self.stream_q is not None: self.stream_q.put(block)`。默认 None ⇒ 现有行为与 smoke_test 喂块测试零变化；这是保护清单第 1 条修订表述允许的「可选扇出」，审查环节专门核对。
  - **asr.transcribe 参数化**：签名扩为 `(audio, initial_prompt=None, sample_rate=16000, beam_size=1, vad_filter=True, word_timestamps=False)`——默认值=现值（`asr.py:87-88`），整句路径调用点 `main.py:329-331` 零变化；流式路径传 `beam_size=5, vad_filter=False, word_timestamps=True`（per-chunk 解码开 vad_filter 会造成块边界/时间戳错位——技术评审）。
  - **解码调度取代裸 Lock**：单解码工作线程 + `queue.PriorityQueue`，任务 `(prio, seq, asr_ref, audio, meta)`：VAD final 段 prio=0、partial chunk prio=1（同优先级 FIFO）——「定稿优先」由调度顺序表达，普通 threading.Lock 表达不了优先级；CTranslate2 调用因此天然串行。
  - **模型热切换交接**：任务携带**入队时的 asr 实例引用快照**；`build_asr` 原子替换 `runtime['asr']`（`main.py:136`）后，队列中旧实例任务自然排空，新任务携带新实例；流式层状态（HypothesisBuffer、已提交 prompt）热切换时重置，避免跨模型 prompt 错位。
  - 其余沿用草案：移植 `HypothesisBuffer`+`OnlineASRProcessor`（保留 MIT 版权头）、min-chunk 0.5–1.0s（CPU 回退 1–2s）、200 字符 prompt 承接、`condition_on_previous_text=False`、partial 消息 `{type:'partial', text, key}` **不写笔记、不触发翻译**、final 覆盖、`streaming.enabled`（默认 false 灰度）；partial 经 `asyncio.run_coroutine_threadsafe` 进 hub 广播。专名改口与静音幻觉的防御沿用草案（术语表 initial_prompt 持续注入 + n-gram 去重 + silero VAD 双保险）。
- **工作量**：移植+单测 2、音频扇出+调度集成 2、真机调参+延迟量化 2。
- **风险与回退**：GPU 双解码负载——word_timestamps 预计 1.5–2×（推断，实测见检验）；上游停更——MIT 移植自持；回退 `streaming.enabled=false` 一键回 v0.2。

#### 条目 12：前端 partial 呈现 + 采集时间戳贯穿 + 延迟指标（1.5 人日）

- **动机**：流式产出需要视觉区分；用户需要「当前落后多少」的感知。**修正草案事实错误（技术评审）**：payload 的 `ts`（`main.py:342`）是广播墙钟字符串，不是音频时间——真正采集时间在 seg_q 元组首元素（`capture.py:191`）且被 `main.py:328` 丢弃。
- **设计**：① `main.py:328` 不再丢弃时间戳 → payload 增加 `audio_ts`（采集墙钟秒）；② footer「延迟 ≈ N s」= 最近 final 的 audio_ts 与当前墙钟差（含转写+翻译+广播全程，才是用户感知的落后量；显示含网络/渲染误差，标 ≈）；③ `.partial` 样式（灰色斜体+「…」游标），final 原位替换恢复配色；④ partial 不进裁剪计数（`index.html:151`），与条目 9 的「回到最新」联动（历史回看时 partial 仍追加于底部）。
- **风险与回退**：partial 1–2 条/s——广播已有死客户端剔除（`main.py:100-109`）；前端忽略 `type:'partial'` 即旧行为。

#### 条目 13：ASR 语言热切换（原条目 12 拆出提前，实用评审建议）（0.5 人日）

- **动机（实用评审）**：README.md:67 自认「中英混杂课堂会漏译中文段」；语言仅 CLI 固定 `--lang en`（`main.py:59`）且构建时写死（`main.py:133` `language=ARGS.lang`）——讲师切中文答疑时强按英文解码出乱码。改属性即生效的前提已核实（`asr.py:86` 每次调用读 `self.language`）。
- **设计**：① `/api/config` 加 `lang`（白名单 en/zh/auto + 常用语言码表，走条目 4 校验框架）；`lang` 变更直接改 `runtime['asr'].language`，无需重建模型；`auto` 传 `language=None` 让 whisper 检测，仅段长 >3s 采纳检测结果，短段沿用上段语言（防误检）；② **`build_asr`（`main.py:133`）改读 `cfg.get('lang', ARGS.lang)`**，保证重启后持久化生效（与 `main.py:150` 的 device 同类问题，后者在条目 15 处理）；③ CLI `--lang` 保持最高优先级（`main.py:271-278` 同款覆盖逻辑）；④ UI 语言下拉，保存进 config.json。
- **风险与回退**：auto 误检——保守采纳策略 + UI 显示当前生效语言；回退 = CLI 覆盖。

### v0.4 —— 音源与上下文扩展（条目 14–16，≈5.5 人日）

#### 条目 14：WASAPI loopback——系统声音内录（3 人日）

- **动机**：Zoom/网课场景收电脑声比收麦克风可靠；现状仅麦克风路径（`capture.py:197`）。
- **调查（本次已本机验证）**：sounddevice 不支持 WASAPI loopback（维护者原话 https://github.com/spatialaudio/python-sounddevice/issues/379 ；本机 `inspect.signature(sd.WasapiSettings.__init__)` 实测无 loopback 参数）；**首选 PyAudioWPatch**（`get_best_wasapi_loopback()`，https://github.com/s0d3s/PyAudioWPatch 、https://github.com/s0d3s/PyAudioWPatch/issues/21 ）；次选 soundcard（https://github.com/bastibe/soundcard ）；不做 VB-Cable。
- **设计（裁定技术评审指出的矛盾）**：48k→16k 重采样**采用 `scipy.signal.resample_poly`**（3 倍降采样必须抗混叠，「线性插值」即 naive 抽点不可用；纯 numpy 手写 polyphase 在 3 人日内质量不可保证——两者取一并修正依赖口径，见 §0.2）。其余：`app/loopback.py`（PyAudioWPatch 打开默认扬声器 loopback 设备 → downmix (L+R)/2 → resample_poly → 复用分句状态机——`handle_block` 抽共享基类，状态机逻辑零改动）；`/api/config` 加 `source ∈ {mic, loopback}`，复用 apply_segmenter 热切换（`main.py:148-155`）；**同步修正 `main.py:150` 的 `device=ARGS.device` 硬编码为读 config**（技术评审 missing 项）；loopback 默认强制 silero VAD；loopback 线程受条目 1 守护逻辑覆盖。
- **风险与回退**：PyAudioWPatch 维护节奏——回退换 soundcard 库（差异集中采集层一处）；回退开关：`source` 仅 CLI 可用，UI 先不暴露。

#### 条目 15：麦克风设备选择（原条目 12 剩余部分）（1 人日）

- **设计**：① GET `/api/devices` 返回 `sd.query_devices()` 过滤后的输入设备列表；② `/api/config` 加 `input_device`（条目 4 白名单）；③ **`main.py:150` `device=ARGS.device` 改读 `cfg.get('input_device', ARGS.device)`**——不改这行，config 化的 input_device 永远不生效（技术评审点名的遗漏）；④ 变更走 apply_segmenter 重建，旧线程优雅停止（复用 `main.py:152-155` 已验证语义）；⑤ UI 设备下拉。
- **风险与回退**：设备热切抖动——重建失败保留旧 segmenter；回退 = CLI `--device`。

#### 条目 16：翻译上下文窗口与术语一致性（1.5 人日）

- **动机**：翻译每段独立无历史（`translate.py:66-80`）——指代、人名、术语译名跨段漂移。成本可忽略（DeepSeek 定价页口径，每段 ~300 token 上下文，1h 课 ≈ 36k input token < $0.02）。
- **设计**：`translate(text_en, context: list[str])`——最近 3 条已定稿 EN 拼进 user message 前缀，标注 "Context for reference only; do not translate"；translate_loop（条目 5）维护窗口；**术语表 mtime 变化自动重载，且必须同步刷新翻译侧 glossary 与 whisper 侧 `runtime['prompt']`**——`main.py:297-303` 的 initial_prompt 是启动时一次性生成的，只重载翻译侧会出现两侧术语不一致（技术评审 missing 项）；`translate.model` 配置化（现状 `translate.py:48` deepseek-chat；切换档位前以官方定价页为准）；上限 1200 字符截断（对齐 `translate.py:42-44` 既有做法）。
- **风险与回退**：指代传染——temperature 0.2 + 「仅参考」约束；回退 = 窗口置 0。

### v0.5 —— 课程学习套件（条目 17–18，≈5 人日）

#### 条目 17：课后 AI 笔记摘要（map-reduce）（2.5 人日）

- **调查**：LangChain 官方摘要教程——map-reduce（分段并行摘要+汇总）是质量/成本/延迟最佳平衡：https://python.langchain.com/docs/tutorials/summarization/ ；**不引入 langchain**，只借管线形状。
- **设计**：POST `/api/notes/summarize`：读 `hub.transcript_path` → 按约 4000 字符分块（**按 EN 行边界分块，不切断句子**）→ `asyncio.gather` + `semaphore=4` 并行逐块摘要 → reduce 为结构化 md（概览/要点/术语对照/存疑处）→ 写 `lecture_*.summary.md`，进度经 /api/status 或 WS 推送；复用条目 6 重试封装；失败块标记，绝不整体失败。
- **风险与回退**：纯增量，不影响字幕链路。

#### 条目 18：Anki 复习卡生成（2.5 人日）

- **调查**：社区验证管线（role prompt + few-shot + 课程学习目标 → 生成 → **二次过滤低质卡**（必需环节）→ genanki 打包）：https://richardlyu.substack.com/p/creating-anki-flashcards-with-llms ；每卡一知识点原则：https://www.supermemo.com 。成本 < $0.05/节。
- **设计**：从条目 17 块摘要生成 10–20 卡（正=问题，反=答案，双语）→ 二次 LLM 过滤（打分低于阈值丢弃，保留定义类卡）→ genanki（纯 Python，MIT）打包 .apkg → GET `/api/notes/deck` 下载；卡面纯文本从简。本计划唯一 v0.5 新依赖。
- **风险与回退**：低质卡噪音——过滤不可省；功能独立，不影响其他链路。**验收门槛（实用评审）：首次实调后自用一周才宣布 v0.5 完成**——mock 下固定 10–20 张只证管线跑通，不证学习价值。

### v1.0 —— 分发与安全打磨（条目 19–21，≈6 人日；条目 20/21 挂待裁决）

#### 条目 19：LAN token 鉴权 + 全键白名单（1.5 人日；不挂裁决，理由见 §3.3-1）

- **动机**：服务绑 0.0.0.0 全端点无鉴权（`main.py:397`）——同网段可读课堂内容、切模型、暂停服务、下载笔记。**LAN 暴露今天就存在**（手机同 WiFi 场景，README.md:60），与是否分发给朋友无关。
- **设计**：① 首次启动自动生成随机 token 写入 config.json 并打印；所有 `/api/*` 与 `/ws` 校验（HTTP 用 `X-LectureLens-Token` 头或 `?token=`，WS 用 query 参数）；127.0.0.1 豁免（**只认 socket 对端地址，防 X-Forwarded-For 伪造**——技术评审审查项）；校验逻辑抽纯函数。② 白名单校验沿用条目 4 框架，**补全本计划自增键**：pipeline.serial、streaming.enabled、translate.model、input_device、auth.enabled、lang、source（技术评审 missing 项）。③ 先验后存已于条目 4 落地，本条不重复。④ 凭据（DeepSeek key）只从 env/.env 读，不进 config.json。⑤ **smoke_test 配套改造（技术评审）**：`ws_handshake_ok`（`smoke_test.py:191-204` 裸 socket 无 token）改为带 token 版，并新增 `ws_handshake_rejected`（无 token 断言非 101）。
- **风险与回退**：token 摩擦——豁免本机 + 手机粘贴一次 localStorage 记住；回退 `auth.enabled=false`。

#### 条目 20：PyInstaller onedir 打包（4.5 人日，技术评审重估；**挂待裁决**）

- **调查（转引）**：faster-whisper 官方 issue 命令清单 `--collect-all ctranslate2 --collect-all faster_whisper --collect-all huggingface_hub` + 模型走本地路径（https://github.com/SYSTRAN/faster-whisper/issues/351 ）；CTranslate2 维护者不支持 onefile（https://github.com/OpenNMT/CTranslate2/issues/1048 ），选 onedir；GPU 版另需 `--add-binary` 收集 cublas/cudnn DLL。PyInstaller+faster-whisper+CUDA DLL 冻结调试历来是重灾区，首版按 4–5 人日排（技术评审）。
- **设计**：`scripts/build_exe.py`：onedir + 首版 `console=True`；large-v3-turbo 模型目录随包（≈1.5GB），`WhisperModel` 传包内本地路径 + `HF_HUB_OFFLINE=1`；entry 复用 main()；**ROOT 推导补 `sys.frozen` 分支**（`main.py:28` 的 `Path(__file__)` 在 onedir 中仍有效但 cwd 语义不同——技术评审 missing 项）。可选后续 Inno Setup（不做签名）。
- **风险与回退**：打包失败就继续「绿文件夹 zip + bat」分发（现状可用）。

#### 条目 21：Tauri 桌面壳（4 人日，可选降级项；**挂待裁决**）

- **定位**：纯体验升级，**不解决任何打包难题**（sidecar 本质仍是 PyInstaller exe：https://tauri.app/develop/sidecar/ ；备选 PyO3：https://pytauri.github.io/pytauri/latest/ ）。前置条件 = 条目 19、20 完成 + 确有「浏览器不够顺手」的真实痛点。Rust 侧只做窗口 + 子进程生命周期（**关窗杀 sidecar 防孤儿进程**——对应 `smoke_test.py:280-287` 的残留检查思路）。
- **风险与回退**：Tauri/Rust 工具链学习成本是单开发者最大风险；随时可放弃。

---

## 2. 检验与验收

### 2.1 分工与纪律

- **pytest（tests/，全部离线秒级）**：纯逻辑——分句状态机、HypothesisBuffer、解码调度、重试与预算、prompt 构造、配置安全、鉴权白名单、watchdog、hub、错误映射、卫生断言、重采样频谱、分块、卡组结构。
- **smoke_test（本机，真模型/真服务/真音频）**：需要 GPU、TTS、子进程或真实 API 的段落。
- **手动清单（docs/manual-ui-checklist.md）**：浏览器行为与真机硬件场景（如实说明：不在 smoke_test 自动化范围；v1.0 前不引入 Playwright 守零预算）。
- 扩展纪律：复用 `record()`/RESULTS/exit-code（`smoke_test.py:31-33,308-313`）；每段独立 `test_xxx()` 挂 argparse 分支；服务子进程复用 Popen→轮询 ready→terminate→无残留检查骨架（`smoke_test.py:210-287`）。

### 2.2 每项验收标准与测试方法

| 条目 | 验收标准（可判定） | 测试方法 |
|---|---|---|
| 1 麦克风守护 | InputStream 连续异常时线程存活；5 连败熔断且 `status.mic` 出现占用提示；60s 全零输入 → `no_signal?`；恢复后字幕自动续上 | pytest `test_capture_guard.py`：monkeypatch `sd.InputStream` 前两次 raise 第三次成功，断言重试计数与存活；5 连败断言熔断；全零块序列断言 no_signal 不误报正常语音。手动清单：真机独占占用 → 红横幅 ≤10s；拔麦克风 → no_signal；恢复续上 |
| 2 测试骨架 | `python -m pytest` 全绿；`smoke_test --vad-only` exit 0；触发 save_config 的测试不写真实 config.json | conftest 全局 CONFIG_PATH→tmp_path fixture；`test_config_isolation.py` 跑完断言仓库 config.json 内容与 mtime 未变。CI 推迟，本地手动执行（命令：`.venv/Scripts/python.exe -m pytest`） |
| 3 日志 | `logs/lecturelens.log` 存在且含「model ready」「config 已更新」「笔记文件」三类关键字；utf-8 中文可读；假 key 不出现在日志 | smoke_test [5] 末尾断言 + 假 key 环境变量跑服务段后 grep 日志；GBK console 下验证（RotatingFileHandler 直写文件不经 stdout） |
| 4 配置安全 | 非法 model → 400 且 `runtime['config']` 与 config.json 均未变；构建失败 → 两者仍是旧值 | pytest `test_config_safety.py`（ASGI 直调；monkeypatch build_asr raise；临时 CONFIG_PATH）；smoke_test [5] 子用例：POST 非法 model → `status.asr` 仍 ready |
| 5 并发解耦 | 慢翻译(3s) 下 3 段 EN 广播间隔 <1s；ZH 按 seg 有序写入；队列满丢最旧段**有 ⚠ 缺失标记行**（backfill 可达）；暂停排空后恢复无旧段广播 | pytest `test_pipeline_concurrency.py`（fake translator + asyncio 队列直测，含丢弃标记与暂停语义用例）；smoke_test [6]：服务子进程 + 慢翻译环境变量注入 → `/api/status.backlog` 可见且 EN 段计数持续推进 |
| 6 重试补译 | 429→429→200 重试成功；429×3 返回 None + zh_fail 标记；持续 hang 时总耗时 ≤ 预算上限（≈10s）而非 65s | pytest `test_translate_retry.py`：先落地 transport 注入口，httpx.MockTransport 构造 429/200/hang 序列（小预算注入测 deadline）；smoke_test [6] 坏 key 子用例：`translate_misses` 递增、payload 含 `zh_fail` |
| 7 watchdog | `start_transcript` 失败或任务意外退出 → status 出现 fatal/dead；正常 cancel 不误报；修复后 fatal 清除 | pytest `test_watchdog.py`：monkeypatch `hub.start_transcript` 抛 OSError → 断言 pipeline fatal；恢复 patch 后 POST 合法 config → 回 ready；smoke_test [5] 子用例（子进程注入故障 env → status 报 fatal 而非静默） |
| 8 前端错误面 | 错误映射表命中/兜底；无笔记时 `/api/notes` 返回 404 JSON；手动三场景全过 | pytest `test_error_map.py`；smoke_test [5] 追加 404 断言；手动清单：杀服务看断线横幅、断网换模型看 toast、无笔记点导出。**如实说明**：浏览器行为不进自动化 |
| 9 历史回看 | 裁剪上限 1000 生效；「回到最新」按钮出现/消失逻辑正确；patchZh 落空计数 footer 可见并入 status | DOM 行为以手动清单覆盖（**如实标注不自动化**）；`test_hub.py` 扩展 ui_metrics 上行消息解析单测 |
| 10 工程卫生 | 根目录无 `nul`；config.example.json 键集合 == `ALLOWED_CONFIG_KEYS`；translate.py 无硬编码绝对路径 | pytest `test_hygiene.py`（本地跑）；手动：新目录克隆 + 无 python 双击 bat → 清晰诊断 |
| 11 流式核心 | partial 首字时间 < 整句模式首显；committed 命中 KEYS；word_timestamps 耗时增幅记录在案；热切换后新任务用新实例、流式状态重置 | pytest `test_hypothesis_buffer.py`（公共前缀提交/n-gram 去重/时间戳过滤）+ `test_decode_scheduler.py`（fake 实例验证 final 先于 partial、实例快照交接）；smoke_test [7]：TTS 句子按 0.5s 块喂流式处理器（复用 `smoke_test.py:105-107` SENTENCE/KEYS） |
| 12 partial 呈现与延迟 | WS 消息序存在 partial→final 对且 final.seg 兼容整句 payload；payload 含 `audio_ts`；footer 显示「延迟 ≈ N s」 | smoke_test [7] 服务级子用例（WS 客户端收消息序）+ pytest payload 构造单测；观感进手动清单 |
| 13 语言热切换 | POST `lang=zh` → status 反映、服务不重启、**重启后仍 zh**（持久化生效）；非法 lang 400 | pytest（白名单校验 + build_asr 读 config lang 单测）+ smoke_test [5]（POST lang=zh 断言 + config.json 内容断言） |
| 14 loopback | 播放 TTS → loopback 采集 5s → 切出 ≥1 段且 ASR 命中 ≥1 关键词；48k→16k 重采样后 8kHz 以上无混叠能量 | smoke_test [8]（仅 Windows 本机）+ pytest `test_resample.py`（正弦扫频 FFT 断言） |
| 15 设备选择 | `/api/devices` 返回过滤列表；apply_segmenter 使用 config 的 input_device（main.py:150 修正生效）；非法索引 400 | pytest（monkeypatch `sd.query_devices`；捕获 MicSegmenter 的 device 参数）；真实设备热切进手动清单 |
| 16 上下文与术语 | prompt 快照（3 段历史 + 术语 + 1200 截断）稳定；glossary mtime 变更 → 翻译侧 entries 与 `runtime['prompt']` **同步**刷新 | pytest `test_prompt_context.py` + `test_glossary_reload.py`；smoke_test `--translate` 段带上下文请求正常返回 |
| 17 笔记摘要 | 分块不切断句子；mock 全管线产物四节结构完整；失败块标记不整体失败 | pytest `test_chunking.py`（行边界/块数上限/空输入）+ smoke_test [9] dry-run（mock LLM 固定文本）+ `--translate` 实调短转录 |
| 18 复习卡 | .apkg 生成、mock 下卡数 10–20、字段非空、过滤阈值生效；Anki 导入成功；**自用一周记录在案后才宣布完成** | pytest `test_deck.py` + smoke_test [9] dry-run 断言 + 手动（Anki 导入 + 一周自用） |
| 19 鉴权白名单 | 无 token 非环回 403；环回豁免 200；全键白名单非法值 400 且 config.json 未改写；裸 socket 无 token 握手被拒（非 101） | pytest `test_auth.py`（parametrize 全键；临时 CONFIG_PATH）；smoke_test [5]：`ws_handshake_ok` 改带 token 版 + 新增 `ws_handshake_rejected`；测试子进程禁用环回豁免覆盖 403 分支 |
| 20 打包 | 干净 Windows（无 Python）双击起服务出字幕；参数化 exe 跑 [5] 式检验全过；产物无 .env/config.json/transcripts；`sys.frozen` 下 ROOT 解析正确、HF_HUB_OFFLINE 离线加载成功（路径类断言） | 打包脚本内置产物目录树断言 + 被测可执行文件参数化的 smoke_test + 干净虚拟机/朋友机实测（含 frozen 路径与 HF 缓存 checklist） |
| 21 Tauri 壳 | 开窗字幕可用；关窗后任务管理器无残留 python/exe 进程；安装器干净机可装可卸 | 手动验收（残留检查呼应 `smoke_test.py:280-287` 同款思路） |

### 2.3 scripts/smoke_test.py 扩展方案（自动化说明）

现 [1]VAD / [2]ASR / [3]GPU / [4]翻译 / [5]服务 五段保留，扩展全部走同一 `record()`/exit-code 纪律：

| 新段/改动 | 条目 | 内容 | 运行层 |
|---|---|---|---|
| [6] 编排并发（`--server` 变体） | 5、6 | 服务子进程 + 慢翻译/坏 key 环境变量注入（需 main.py 读取对应 env，实现时落地）：backlog 可见、EN 计数推进、translate_misses 递增、zh_fail、暂停排空后无回放 | 本机（子进程） |
| [7] 流式 | 11、12 | TTS 句子按 0.5s 块喂流式处理器：partial 首字 < 整句首显、committed 命中 KEYS、word_timestamps 耗时增幅记录；服务级子用例断言 partial→final 序与 audio_ts | 本机 |
| [8] loopback | 14 | TTS wav 播放 → loopback 采集 5s → 切段 + ASR 命中 | 仅本机 |
| [9] 笔记/卡 | 17、18 | dry-run（mock LLM 固定文本）全管线产物结构断言；`--translate` 时实调短转录 | dry-run 可离线 |
| [5] 服务段扩展 | 3、4、7、13、19 | 日志文件断言、非法 config 不污染、fatal 子用例、lang 热切换、（条目 19 后）带/不带 token 的 HTTP+WS 断言（`ws_handshake_ok` 改造 + `ws_handshake_rejected`） | 本机（子进程） |
| `--vad-only` 参数 | 2 | 只跑 [1] 段，供未来 CI 与快速回归 | 离线 |

**pytest 清单**：`test_capture_state`（自 smoke_test 的 synth_stream 参数化迁移）、`test_capture_guard`、`test_config_isolation`、`test_hypothesis_buffer`、`test_decode_scheduler`、`test_pipeline_concurrency`、`test_translate_retry`、`test_prompt_context`、`test_glossary_reload`、`test_config_safety`、`test_watchdog`、`test_hub`（含 ui_metrics）、`test_error_map`、`test_hygiene`、`test_resample`、`test_chunking`、`test_deck`、`test_auth`。

**已知局限（实用评审，如实声明）**：smoke_test 全部基于 SAPI TTS 合成音频（干净/近讲/单声线），测出的 partial 首字延迟与 VAD 切段会偏乐观；教室远场收音（README.md:68 自认最大变量）只有真实课堂能检验——见 §2.4。

### 2.4 真实课堂终检（v0.2 收口里程碑，0 成本）

带去 **1 节真课**：全程无人工干预跑完；课后 30 分钟内用 `logs/lecturelens.log` + 笔记文件回答 3 个预设故障问题：
1. 有没有段落没出字幕？哪一段、日志显示什么原因？
2. 翻译失败/重试了几次？（translate_misses 与重试打点）
3. 麦克风有没有重开或 no_signal 事件？

**通过标准**：字幕连续性主观可用 + 3 问都能从日志与笔记直接回答（而不是靠回忆）。这是「可靠性加固」主题唯一的真实检验，也是对条目 5（日志）动机的直接验收。

### 2.5 已核验 / 转引 / 未运行声明

- **已核验（本次实读 + 实跑）**：§0.1 全部条目。
- **转引调研（调研方声称已核验，本次未重跑）**：LocalAgreement-2 的 3.3s 论文口径、PyInstaller issue 351 命令清单、CTranslate2 onefile 立场、DeepSeek 2026-10 定价、whisper_streaming 维护状态、WhisperLiveKit 基准数据。落地前按对应条目检验步骤复核。
- **推断（未实测，均为检验待测项）**：partial 首字 1.5–2.5s、word_timestamps 解码耗时增幅 1.5–2×、turbo 8GB 显存可行性（2.2GB 占用口径来自 README.md:51 实测记录）。
- **未运行**：本次未执行 smoke_test、未启动服务、未跑任何 pytest——本计划所有「检验」为规划状态，落地时以 §2.2/§2.3 清单为准。

---

## 3. 风险与回退

### 3.1 日程风险与里程碑切片

v0.2 ≈14 人日，按实测节奏 2.5–3.5 人日/周约 4–5.5 周——**超出草案「每阶段 2–3 周」目标，如实标注**。处置：三个里程碑切片，每个结束都是可用状态，可随时停：

- **m1（≈5 人日）上课保命**：条目 1（麦克风守护）+ 2（测试骨架，随 1 同周补齐）+ 3（日志）+ 4（配置安全）→ **真实课堂终检（§2.4）在此后最近的课执行**。
- **m2（≈5.5 人日）管线**：条目 5（并发解耦）+ 6（重试补译）+ 7（watchdog）。
- **m3（≈3.5 人日）体验收尾**：条目 8（前端错误面）+ 9（历史回看）+ 10（卫生包）。条目 9/10 可顺延至 v0.3 期，不阻塞 m2。

### 3.2 技术风险登记

| 风险 | 概率/影响 | 处置与回退 |
|---|---|---|
| 流式 GPU 双解码负载（word_timestamps 1.5–2×，推断） | 中/中 | 检验量化（条目 11）；超限 → min-chunk 拉大 / CPU 回退档 / `streaming.enabled=false` 一键回退 |
| PyInstaller 冻结 faster-whisper+CUDA DLL（重灾区） | 高/中 | 条目 20 按 4.5 人日含余量；回退 = 绿文件夹 zip + bat（现状可用） |
| PyAudioWPatch / scipy 依赖维护 | 低/中 | 回退 soundcard 库（差异集中采集层一处）；scipy 仅用 resample_poly 一个函数，替换成本低 |
| whisper_streaming 上游停更 | 已发生/低 | MIT 代码移植进仓库自持 |
| 并发化引入乱序/回放回归 | 中/高 | `pipeline.serial=true` 一键回旧行为；暂停语义有专门测试用例 |
| 单写者笔记窗口死锁（某段 ZH 永不到） | 低/中 | 10s deadline + 缺失标记 + backfill 兜底，写者不阻塞 |

### 3.3 待用户裁决点（裁决前对应条目不启动）

1. **分发前提**：README.md:3 同时写「免费自用 + 朋友工具」与「不分发」。若「不分发」含朋友装机 → 条目 20（打包）/21（Tauri）取消或无限期挂起，节省 ≈4.5–8.5 人日；若仅指不公开分发 → 条目 20 按计划执行。**无论裁决结果，条目 19（token 鉴权）保留**——0.0.0.0 + 手机同 WiFi 场景下 LAN 暴露今天就存在（`main.py:397`、README.md:60），1.5 人日独立成立。
2. **v0.3 与 v0.5 顺序**（实用评审）：用户主读中文（zh 32px/600 > en 26px），而条目 11 设计明确 partial「不写笔记、不触发翻译」——流式提前的只有 EN，中文仍等 700ms 定稿 + 翻译往返，「主观体验跃升最大」的定位对中文为主用户不完全成立。**默认维持流式先行**（EN 听写跟随仍有直接价值；学习套件需要 m1-m3 落地后的自用反馈输入，条目 18 的「自用一周」验收也要求先真实使用）；若用户裁决对调，条目 17/18 与 11–13 互换阶段，条目 11 的音频扇出设计不受影响。**「partial 增量翻译预览」列为可选扩展不进首版**——API 成本约翻倍，且与 LocalAgreement 提交语义冲突需重新设计，待流式上线后按真实体验决定。

### 3.4 回退开关清单（每项一个开关或纯增量）

`pipeline.serial`（条目 5）｜ `streaming.enabled`（条目 11）｜ `source` 仅 CLI（条目 14）｜ 重试次数=0（条目 6）｜ `auth.enabled=false`（条目 19）｜ context 窗口=0（条目 16）｜ 裁剪上限调回 200（条目 9）｜ 条目 7/8/10 纯加固无回退必要。

---

## 4. 附录：评审意见处理（38 条逐条对照）

处理图例：**采纳**＝已落入终稿对应条目；**采信**＝确认类意见，本次独立抽验通过；**部分采纳**＝方向采纳但范围/结论修正。

### 技术评审（21 条：采纳 18、采信 3）

| # | 意见摘要 | 处理 | 落点/理由 |
|---|---|---|---|
| T1 | 条目 9 阻断级：流式 partial 音频来源未设计（seg_q 只出定稿段，原始块无出口） | 采纳 | 条目 11「音频扇出」：run() 循环可选 stream_q，保护清单第 1 条表述精确化 |
| T2 | 条目 9 配套三缺：asr.py:87-88 硬编码、Lock 表达不了定稿优先、热切换旧实例交接 | 采纳 | 条目 11：transcribe 参数化（默认值=现值）、PriorityQueue 调度、实例引用快照交接 |
| T3 | 条目 9 人日不足，v0.3 应按 8–10 重排 | 采纳 | 条目 11 定 6 + 条目 12 定 1.5 + 新增条目 13（0.5），v0.3 ≈8 |
| T4 | 条目 2 重试 3×20s+5s=65s 与 10s 上限冲突，65s 堵死 maxsize=8 队列 | 采纳 | 条目 6：deadline 预算制（单次 min(5s, 剩余)，退避计入预算） |
| T5 | 条目 1 「2s 缓冲解决崩溃丢尾部」动机相反：现状即写即落盘，TerminateProcess 不走 finally | 采纳 | 条目 5：改单写者有序窗口 + 写即 flush，解决乱序不碰耐久性 |
| T6 | 条目 16 「finally 持久化」修不掉 runtime 污染（main.py:181 先赋值）；落地后 smoke_test 裸 socket 握手会挂 | 采纳 | 条目 4（先验后赋值 + save-after-build）+ 条目 19（ws_handshake_ok 改造 + ws_handshake_rejected） |
| T7 | 条目 10 事实错误：payload.ts 是广播墙钟，采集时间戳被 main.py:328 丢弃 | 采纳 | 条目 12：audio_ts 贯穿管线 |
| T8 | 条目 11 矛盾：线性插值=naive 降采样，scipy 破坏依赖承诺，numpy polyphase 人日不够——两者取一 | 采纳（取一） | 条目 14 用 scipy.signal.resample_poly；依赖承诺口径修正（§0.2）——原承诺与 PyAudioWPatch/genanki 本就自相矛盾 |
| T9 | 暂停语义对 translate_q 未定义 | 采纳 | 条目 5：排空未翻译段 + 丢弃标记；在飞翻译放行完成（设计决策已写明） |
| T10 | pytest 的 CONFIG_PATH 全局隔离缺失，测试会写真实 config.json | 采纳 | 条目 2：conftest 级 fixture + test_config_isolation.py |
| T11 | 条目 13 initial_prompt 启动时一次性生成，mtime 重载需同步刷新 | 采纳 | 条目 16：双侧同步刷新 + 专门单测 |
| T12 | 条目 12 需改 main.py:150 的 device=ARGS.device 硬编码 | 采纳 | 条目 15（device）+ 条目 13（language 同类问题 main.py:133） |
| T13 | 白名单漏掉本计划自增键（pipeline.serial 等 5 项） | 采纳 | 条目 4 常量 + 条目 19 全键核对 |
| T14 | RotatingFileHandler 需显式 utf-8；uvicorn logger 纳入；本机 GBK 代码页 | 采纳 | 条目 3 设计明示 |
| T15 | 条目 17 检验清单缺 sys.frozen ROOT 推导与 HF 缓存路径断言 | 采纳 | 条目 20 检验含路径类断言 |
| T16 | translate_q 两端在事件循环内，用 asyncio.Queue 避免线程池往返 | 采纳 | 条目 5（main.py:328 现状已付一次 to_thread，已核实） |
| T17 | DeepSeekTranslator 需 transport 注入口才能用 MockTransport | 采纳 | 条目 6 设计先行落地 seam |
| T18 | patchZh 在 DOM 裁剪后到达的 ZH 静默丢弃，建议计数并入 status | 采纳 | 条目 9：footer 计数 + WS 上行 ui_metrics |
| T19 | 行号微偏差（translate.py:62、asr.py:79、asr.py:86） | 采纳 | 终稿行号按本次实读校正（三处均复核命中） |
| T20 | 条目 17 首版按 4–5 人日排（PyInstaller 冻结重灾区） | 采纳 | 条目 20 定 4.5 人日 |
| T21 | v0.2 的 12.5 人日会撞 3 周上限；日志提前到条目 1 之前 | 采纳 | 条目 3 提前；§3.1 里程碑切片 + 4–5.5 周如实标注 |
| T22 | 转引声明保持即可，落地前复核 | 采信 | §2.5 保持声明框架 |
| T23 | 「已验证前提」清单（约 30 锚点、nul、.gitignore 等） | 采信 | 本次独立抽验全部命中（§0.1） |

### 实用评审（17 条：采纳 15、部分采纳 2）

| # | 意见摘要 | 处理 | 落点/理由 |
|---|---|---|---|
| P1 | 条目 3（收音死）应列 v0.2 第一顺位，先于条目 1 | 采纳 | 条目 1，排序理由照录（延迟可容忍、收音死全灭） |
| P2 | CI 对单人私有仓库是负收益，砍掉或推迟 | 采纳 | 条目 2 保留 pytest 骨架，GitHub Actions 推迟至 v1.0 真分发前（卫生断言由本地 pytest 承担） |
| P3 | 「先验后存」是数据损坏 bug，不应与 token 鉴权捆绑排队到第 5 阶段 | 采纳 | 拆出为条目 4，进 v0.2 m1 |
| P4 | 分发前提矛盾（README:3 vs 计划红线），须用户本人裁决，否则 v1.0 可能白做 | 部分采纳 | §3.3-1 待裁决，条目 20/21 挂起；**不采纳「token 大幅缩减」**——LAN 暴露今天已存在（0.0.0.0 + 手机 WiFi 场景），条目 19 独立成立 |
| P5 | 条目 1/2 断链：队列满丢弃段不写标记 → backfill 不可达，中文永久消失 | 采纳 | 条目 5：丢弃路径同步写 ⚠ 缺失标记行 |
| P6 | 18 项无一是「真实课堂跑满一节课」，TTS 测不出教室远场 | 采纳 | §2.4 真实课堂终检里程碑（v0.2 收口） |
| P7 | 条目 4 动机错位：apply_segmenter 几乎不抛，真炸弹是 main.py:293 start_transcript | 采纳 | 条目 7 动机改写（本次复核确认：make_vad 有 energy 回退、InputStream 在线程内构造） |
| P8 | 字幕历史回看完全缺失，价值/成本比最高 | 采纳 | 新条目 9（裁剪 1000 + 回到最新按钮 + patchZh 落空计数） |
| P9 | 真实课堂终检：全程无人工干预 + 课后日志三问 | 采纳 | §2.4（与 P6 同源，问题清单照录） |
| P10 | 条目 10 应与「回到最新」按钮一起做 | 采纳 | 条目 12 与条目 9 联动设计 |
| P11 | 条目 12 语言热切换可提前（中英混杂漏译 + 改属性即生效已核实） | 采纳 | 拆出为条目 13，进 v0.3 |
| P12 | v0.3 与 v0.5 对调供用户裁决；或给 partial 加增量翻译预览 | 部分采纳 | §3.3-2 待裁决（默认维持现序，理由已写明）；增量翻译预览列为可选扩展不进首版（成本翻倍 + 与提交语义冲突） |
| P13 | Anki 验收只证管线不证学习价值，应自用一周再宣布完成 | 采纳 | 条目 18 验收门槛 |
| P14 | 勘误：translate.py 超时在 62 行非 63 行 | 采纳 | 全稿行号校正（复核命中） |
| P15 | 保持项：检验纪律自觉性好（五段结构、无残留检查等） | 采信 | 本次抽验与 `smoke_test.py:280-287` 等锚点一致 |
| P16 | 已核验声明（逐行实读 12 文件 + 三项独立检查） | 采信 | 抽验通过（§0.1） |
| P17 | （missing）先验后存提前 | 采纳 | 同 P3，条目 4 |

> 统计：技术评审 21 条 = 采纳 18 + 采信 3（T8 计采纳，附带口径修正）；实用评审 17 条 = 采纳 14 + 部分采纳 2（P4、P12）+ 采信 2（P15、P16）——合计 **38 条：采纳 32、采信 5、部分采纳/转裁决 2**（P17 与 P3 同源合并计数）。

---

## 5. 参考来源汇总

- 流式字幕：https://github.com/ufal/whisper_streaming ｜ https://arxiv.org/abs/2307.14743 ｜ https://github.com/ufal/SimulStreaming ｜ https://aclanthology.org/2025.iwslt-1.41/ ｜ https://github.com/QuentinFuxa/WhisperLiveKit ｜ https://github.com/QuentinFuxa/WhisperLiveKit/tree/main/benchmarks
- 音频采集：https://github.com/spatialaudio/python-sounddevice/issues/379 ｜ https://github.com/s0d3s/PyAudioWPatch ｜ https://github.com/s0d3s/PyAudioWPatch/issues/21 ｜ https://github.com/bastibe/soundcard ｜ https://soundcard.readthedocs.io/en/latest/
- 打包与桌面壳：https://github.com/SYSTRAN/faster-whisper/issues/351 ｜ https://github.com/OpenNMT/CTranslate2/issues/1048 ｜ https://tauri.app/develop/sidecar/ ｜ https://pytauri.github.io/pytauri/latest/
- 笔记与复习卡：https://python.langchain.com/docs/tutorials/summarization/ ｜ https://richardlyu.substack.com/p/creating-anki-flashcards-with-llms ｜ https://www.supermemo.com
- 成本：https://api-docs.deepseek.com/quick_start/pricing
- 本仓库（2026-10-12 实读）：`app/main.py`、`app/capture.py`、`app/asr.py`、`app/translate.py`、`app/web/index.html`、`scripts/smoke_test.py`、`start_translate.bat`、`requirements.txt`、`.gitignore`、`config.json`、`README.md`
