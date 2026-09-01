# SmartVault 维护文档（PROJECT_DOC.md）

> 本文档面向维护者（未来的你 / AI 助手）：记录架构决策、模块地图、配置语义、
> 常见二次开发场景与升级路线。日常使用请读 [README.md](README.md)。
> **约定：改动代码必须同步更新本文档对应小节；每次发布更新第 0 节并打 git tag。**

## 0. 版本记录

| 版本 | 日期 | 变更摘要 | git tag |
|---|---|---|---|
| 1.8.0 | 2026-09-01 | 功能（用户需求：引入 RapidOCR 识别手写中文 PDF 附件。根因：原 `extract_pdf` 仅取 PDF 文本层，手写笔记 PDF 是扫描件——每页一张图、无文本层，提取结果只能提示「可能为纯扫描件」）：① **PDF 双通道提取**——`extract_pdf` 改为逐页判定：文本层字符数 ≥ `pdf_min_text_chars`（默认 20）直接取文本（电子 PDF 零成本、逐字保真），低于阈值视为扫描页——以 `pdf_dpi`（默认 200）经 PyMuPDF 渲染成 PNG 交给 RapidOCR（rapidocr 3.9，PP-OCRv6 det/rec 模型**内置于 wheel、离线可用**，简体中文手写友好；引擎惰性单例 `_get_rapidocr_engine`，首建 ~1s 进程内复用，初始化失败缓存原因不重试）；返回值改 `(kind, text)` 双通道动态标签（「PDF 文本（PyMuPDF）」/「PDF 提取（PyMuPDF+RapidOCR，手写 N 页）」）；② **降级与护栏**——OCR 关闭（`ocr.engine=off`）/引擎不可用/单页识别失败一律降级为按页占位说明，绝不中断归档流水线；`pdf_max_ocr_pages`（默认 20）防大扫描件阻塞，超出页如实注明可调参重试；③ 新增配置 `ocr` 节（engine/pdf_dpi/pdf_max_ocr_pages/pdf_min_text_chars，CONFIG_DEFAULTS + config.json + config.example.json 三处同步）；`--check` 增测 rapidocr；requirements.txt 增 `rapidocr>=3.0.0`（实测 3.9.2，带入 opencv-python/Shapely/pyclipper/omegaconf，100 项既有测试回归无冲突）。新增 9 项单测 tests/test_pdf_ocr.py（mock 引擎验证双通道分发/混合页/阈值/上限/关闭/不可用降级/dispatch 动态 kind，共 109 项全绿）；E2E：手写模拟（宋体+随机抖动±4px+微旋转±0.9°+米色纸底）2 页扫描 PDF 函数级 8/8 行完整命中（1.2s）+ `--once` 全管线两次验证（OCR 1.1s → 归档 8.5~13.3s → 转录折叠块附加文末逐行引用 → PDF 落位 `附件/`、正文引用同步改写、退出码 0；测试产物已清理）；README + 本文档模块地图/配置表/排障表同步 | `v1.8.0` |
| 1.7.1 | 2026-09-01 | 修复（取证闭环：9 月 1 日 01:21 重启后三个 launchd 作业全部不自启、双击 .app 零反馈；`launchctl print` 示 exit 78 EX_CONFIG / runs 递增全失败 / 日志文件 0 字节——launchd 在 posix_spawn **之前**就打开 StandardOutPath/StandardErrorPath，macOS 26 TCC（kTCCServiceSystemPolicyDocumentsFolder）拒绝 launchd/xpcproxy 打开 ~/Documents 下文件，进程从未被创建故日志为空；8/30 一切正常是因当时自 Terminal 手动 bootstrap、进程持续在跑未经历冷启，重启由 launchd 自行拉起才暴露；内核日志 deny 证据仅涉日志文件，venv/Python/plist 本体均排除）：① **日志迁出 TCC 保护区**——三个 plist 模板 StandardOut/ErrorPath 改用新 `@LOG_DIR@` 占位符（= `~/Library/Logs/SmartVault`，不受 TCC 保护），install_launchd.sh（sed 替换 + 预建目录 + 幂等迁移历史日志——复制不删除、隐藏状态文件刻意不迁以重置增量扫描基线）/ menu_bar_app.render() / build_menubar_app.sh 内嵌 plist 三处同步；② **log_dir 支持绝对路径**——config.json / config.example.json / 两处 DEFAULT_CONFIG 默认值改 `~/Library/Logs/SmartVault`，load_config 与新增 `menu_bar_app._resolve_log_dir()` 统一 expanduser 解析（相对路径仍挂 config 所在目录，老配置兼容；模块导入时预建目录保单例锁可用）；③ **.app 启动器反馈闭环**——已安装分支由 kickstart 改为 bootout→bootstrap（kickstart 不会重读 plist，配置变更不生效的隐患一并修复），bootstrap 后轮询 `state = running`（~10s 上限），超时 osascript 弹窗给出 last exit code 与日志路径（LSUIElement 无 Dock 图标，此前失败零反馈即"点了没反应"的根因之二）。新增 6 项单测 tests/test_log_dir.py（共 100 项全绿）；实测：三作业 state=running、PID 稳定、日志写入新目录、`/health` ok（chunks 6365）、守护进程成功补扫重启期间积压的收件箱草稿（该草稿仅因 LM Studio 模型上下文 8192 过小而保留收件箱，属环境问题非 TCC） | `v1.7.1` |
| 1.7.0 | 2026-08-31 | 功能（用户需求：① 分类粗粒度——一级大类优先、最多二级严禁更深、单一设备/协议不单独建目录；② 正文零删改前提下自动双链专有名词、且要防 AI 幻觉删减正文。核心思路：**提示词永远无法 100% 约束 LLM 不删改正文（v1.3.1 教训），可靠保证只能来自机制隔离**）：① **LLM 只输出名词清单**——输出契约/NOTE_JSON_SCHEMA/原文保留模式指令三处新增 `link_terms`（正文中实际出现的关键专有名词 0~8 个、逐字摘自原文），整理规则 3 改为「双链注入由系统代码完成，严禁 LLM 在 optimized_content 中自行添加/改写/删除双链」；`parse_llm_json` 对 link_terms 类型容错（缺失→[]、字符串→拆分、截前 8 个）；② **`apply_wikilinks()` 纯代码注入**——新增纯函数，`run_pipeline` 在附件引用改写之后落盘之前调用：纯 `re` 对正文包裹 `[[ ]]`，除新增括号外不动任何字符（逐字保真可校验）；LLM 幻觉名词正则匹配不到自动忽略零副作用；禁区保护（代码围栏/行内代码/md 链接 URL/HTML 标签不注入，防命令与路径污染）；ASCII 词边界防 RS485 误伤 RS4855；lookaround 防重复包裹已有 [[x]]；长词优先替换防短词破坏长词；③ 新增配置 `processing.auto_wikilinks`（默认 true，三处配置文件同步）；④ ai_context.md 规则区重写为用户定制版（目录一级大类粗粒度/推荐「智能家居/家用电器/开发环境」三域/最多二级/维修维护配置开发语义精度/正文与双链机制/标签连字符约定/简体中文含专有名词例外）。新增 8 项单测（共 94 项全绿）；E2E：含 RS485/Modbus/MQTT/Home Assistant/北鼎蒸锅 + 代码块 + 链接的草稿全管线验证（LLM 提炼 → 双链注入 6 处 → 归档 78.9s → 去 [[]] 后与草稿原文逐字一致 → 代码块与 URL 禁区完好；测试产物已清理）；README + 本文档模块地图/配置表/单测计数同步 | `v1.7.0` |
| 1.6.4 | 2026-08-31 | 功能（用户需求：附件中存在非图片/音视频/PDF/Office 等可解析类型之外的文件——如 .zip/.epub/.dwg——但被笔记链接，须如实随笔记迁移；孤立文件除外）：① **被链接即附件**——`find_attachment_refs` 的 `_add` 由扩展名白名单（ATTACHMENT_EXTS）改为排除法：有扩展名且非 `.md` 的引用一律视为附件（.md 与无扩展名双链仍是笔记链接，URL/data: 内嵌仍跳过），任意格式文件随笔记迁入目标目录 `附件/` 子目录；② **不可解析类型不读内容**——`dispatch_attachment` 原 else 分支把未知扩展名当纯文本读（二进制乱码会灌入 LLM prompt），现改为返回「暂不支持解析」占位（如实标注「文件已随笔记归档至附件目录」，0.0s 秒过），转录块仅提示打开原文件；③ 未被任何笔记引用的孤立文件维持现状保留收件箱待人工处置。新增 2 项单测（共 86 项全绿）；E2E：草稿 md 链接 `附件/配置备份包.zip` 全管线验证（暂不支持解析 0.0s → 归档 69.6s → zip 落位 `配置备份/附件/` → 正文引用保留 → 文末折叠块如实标注；测试产物已清理）；README 迁移指南 + 本文档模块地图/单测计数同步 | `v1.6.4` |
| 1.6.3 | 2026-08-31 | 修复（E2E 取证：北鼎蒸锅等 5 篇 Kindle 导入笔记归档后正文引用的 45 个附件全部滞留收件箱——笔记用 HTML `<img src="附件/x.png">` 引用图片，Obsidian 能正常渲染但 `find_attachment_refs` 只识别 wikilink / md 链接两种语法，refs 为空导致附件不解析不迁移；微波烤箱 86 个引用归档日志「附件转录 0 份」且无任何等待/告警即为直接证据）：① **HTML src 引用识别**——新增 `HTML_SRC_RE`（`<img>/<audio>/<video>/<source>/<embed>` 的 src 属性，双引号/单引号/裸值/大小写/URL 编码全兼容），`find_attachment_refs` 提取时取 basename 并跳过带 scheme 的 URL 与 `data:` 内嵌资源；`rewrite_links` 新增 `rep_htmlsrc` 同步改写（相对路径语义，改写为 子目录/新名，引号风格保持，按 group 区间精确重组防 `alt` 同值误伤）；② **存量数据一次性修复**——修复脚本复用归档逻辑对全 vault 扫描：45 个被引用附件迁移至对应笔记 `附件/` 子目录（正文 src 字节级不变，秒级完成不重跑 LLM/OCR），20 个无笔记引用的孤儿附件保留收件箱待人工处置；③ E2E：新草稿带 HTML img 引用 2 张图全管线验证（附件解析完成 ×2 → 归档 54.7s → 附件落位 `厨房设备/附件/` → 正文引用原样保留）。新增 6 项单测（共 84 项全绿）；README 迁移指南附件语法说明同步 | `v1.6.3` |
| 1.6.2 | 2026-08-30 | 调优（Qwen3 双模式推理参数；取证结论：LM Studio server 端加载参数已最优——32768 ctx / parallel 1 / GPU 满载，真正瓶颈在请求参数层——此前仅发 temperature=0.3，top_p/top_k 落 LM Studio 默认值 1.0/40 属非官方组合，且 Qwen3 thinking 全程开启每请求白烧 200~2000 推理 token；LM Studio /v1 实测不支持 chat_template_kwargs / enable_thinking 请求参数，`/no_think` 软开关是唯一思考控制通道，实测同请求 16.2s→1.25s）：① **采样参数显式下发**——`LLMClient` / `LMStudioClient` 请求体新增 top_p / top_k；归档侧对齐 Qwen3 官方 thinking 模式推荐值 temp 0.6 / top_p 0.95 / top_k 20（`lm_studio.temperature` 0.3→0.6，新增 `lm_studio.top_p / top_k / thinking`）；② **问答侧默认关闭思考**——新增 `apply_thinking_switch()`（末条 user 消息追加 `/no_think`，Qwen3 官方 chat template 据此注入空 think 块；深拷贝不污染原消息，两模块同实现各自内聚）；问答采样取官方非 thinking 推荐值 temp 0.7 / top_p 0.8 / top_k 20（新增 `rag.chat_temperature / chat_top_p / chat_top_k / chat_thinking`，chat_ 前缀避免与检索 top_k 混淆、不回读 lm_studio.temperature）；归档侧默认 thinking=true（质量优先，可配置关）。新增 5 项单测（共 78 项全绿）；E2E 实测：1474 字符草稿归档 15.8s（旧同量级输入 59.6~73.5s）且分类正确、正文逐字保留；RAG 事实问答全链路 5.6s（旧 30s+）答对并正确引用来源、`/no_think` 零泄漏；README LM Studio 调优说明 + 本文档配置表/模块地图/已知问题 7/单测计数同步 | `v1.6.2` |
| 1.6.1 | 2026-08-30 | 修复（E2E 取证：claude_pro.md 内容与 Obsidian 无关却归入「Obsidian指南」）：① **重归档自愈**——`append_ai_context()` 追加前经 `_drop_stale_ctx_entries()` 按文件名移除旧历史条目：旧条目随 Prompt 注入会形成「历史一致性」锚定（SYSTEM_PROMPT 规则 5 要求与历史索引保持一致），使误归档笔记移回收件箱后仍被 LLM 沿用旧目录、纠错失效（实测两次重归档均跟随旧目录），且 append-only 堆积重复条目；现同 stem 仅保留最新一条，**误归档纠正=移回收件箱即可**（无需再手动删 ai_context 条目）；与 `prune_ai_context_entries`（死链清理）正交互补；② **超短草稿防蹭目录**——SYSTEM_PROMPT 新增规则 8：正文不足约 200 字符的链接收藏/账号信息/碎片备忘须按实际用途与关键实体（站点/工具/邮箱/账号）归类，禁止凭个别词语的弱关联塞入已有目录。新增 3 项单测（`TestAppendAiContext`，共 73 项）；README「分类体系全自动」+ 本文档 How-to/管线时序/模块地图同步 | `v1.6.1` |
| 1.6.0 | 2026-08-30 | 功能：① **AI 自动分类**——SYSTEM_PROMPT 整理规则强化：目录树无合适目录时 LLM 须依据主题自建简洁一级目录（2~6 字，如「开发环境」「AI 工具」），分类体系随归档自然生长、同主题复用；严禁输出「未分类」「笔记」「文档」「其他」等无信息量目录名——根治空仓库冷启动首篇回退 `fallback_folder` 且后续笔记持续跟随的雪球效应；② **收件箱空目录自动清理**——新增 `prune_empty_dirs()`（归档成功后 + 启动补扫时自底向上清理，含仅含 .DS_Store 的目录；收件箱根与含真实文件的目录永不删），解决迁移场景残留空 `附件/` 目录问题；③ 无附件时不再预创建空 `附件/` 目录（`attach_dir` 条件创建）。README「分类体系全自动」节替换原「冷启动雪球」三步手动法；新增 3 项单测（`TestPruneEmptyDirs`）；E2E：15 篇「未分类」存量笔记移回收件箱自动重归档为 4 个主题目录（开发环境 11/Obsidian指南 2/网络工具 2/安全工具 1），附件随迁、空目录自动清理、历史索引死链自动剔除 | `v1.6.0` |
| 1.5.4 | 2026-08-30 | 文档：① 新增**旧仓库迁移指南**（README + 本文档第 5 节）——笔记连同 `附件/` 目录整体投入收件箱即可（watchdog 递归监听 + `resolve_attachment` 递归按文件名定位，wikilink 带不带路径均可解析）；两个注意事项：附件目录内 .md 会被误当草稿、归档后空附件目录需手动清理；② 新增**「未分类」冷启动雪球**对策（README + 本文档第 5 节 + Roadmap）——根因：空目录树时首篇易回退 `fallback_folder`，「优先选已有目录」规则使后续笔记持续跟随未分类；对策：建分类目录 + ai_context.md 规则区写分类约定 + 存量笔记拖动归位（mtime 变化自动重索引）；Roadmap 短期新增「冷启动分类引导」优化项 | `v1.5.4` |
| 1.5.3 | 2026-08-30 | 文档：明确 **BMO/问答 API 的检索范围为全部注册仓库（全局）**（README BMO 小节 + 本文档 4.3）——`/ask` 与 `/v1/chat/completions` 共用 `search()`，对共享 collection 整体 Top-K 检索、无 vault 过滤（块元数据 `vault` 字段仅用于路径展示），Obsidian「当前打开哪个仓库」不影响检索范围；标注按仓库过滤暂不支持及实现思路（Chroma `where` 过滤） | `v1.5.3` |
| 1.5.2 | 2026-08-30 | 文档：① 明确**附件转录内容可被知识问答命中**（README 模块 B + 本文档 4.3）——OCR/whisper/PDF/Office 转录随笔记正文分块嵌入，E2E 实测发票图片上的编号/金额/开户行三个事实（正文只字未提）问答全部命中并引用来源；同时标注边界：从未走过收件箱管线的存量附件不被解析索引；② 故障排查表 + README 模块 A 新增**收件箱目录删除后 watchdog 断监听**问题（目录重建不重挂监听、新草稿被无限搁置且日志无记录；处置=重启摄入守护进程，启动时自动补扫积压——E2E 实测复现并验证恢复） | `v1.5.2` |
| 1.5.1 | 2026-08-30 | 文档：① README 新增「多仓库与索引清理」节 + 本文档 4.2/5 节补充——明确多 Vault 共用同一向量集合、按 `仓库名/相对路径` 键隔离的架构事实；🧹 按文件粒度对其他仓库零影响 / ♻️ 全局清空重建的影响边界；清空/弃用仓库的标准流程（先注销 config → 删文件夹 → 重启守护进程 → 点 🧹，顺序反了会持续产生「Vault 目录不存在」错误日志）；「注销即清理」技巧（config 移除条目+重启即自动移除该仓库全部向量）；② 新增 `CLAUDE.md` AI 协作规则——确立**文档同步铁律**（任何功能/行为/配置/修复变更，提交前必须同轮更新 PROJECT_DOC 第 0 节版本表+受影响小节与 README，文档不同步视为变更未完成，无需用户提醒）及提交/测试/服务重启/E2E 验证/数据安全红线约定；第 6 节发布 checklist 同步强化，单测计数修正为 67 | `v1.5.1` |
| 1.5.0 | 2026-08-30 | 新增：① **附件统一收纳**——`processing.attachments_subfolder` 默认值改为 `附件`（原空=与笔记同目录），归档附件进入归类目录下 `附件/` 子目录（wikilink 全局按名解析无需改写，重名改写/标准 md 链接子目录前缀逻辑不变）；② **菜单栏 RAG 维护按钮**——「🧹 清理已删笔记残留（同步索引）」触发 `/reindex` 增量同步、「♻️ 重建 RAG 索引（清空后重建）」带确认弹窗触发全量重建（清空测试库后一键归零索引），配套 `http_post_json`；③ **删除笔记的残留清理**——新增 `prune_ai_context_entries`：`sync()` 每轮（后台周期 + 手动触发）顺带剔除 ai_context.md 中指向已删除笔记的失效归档条目，剔除后 ai_context.md mtime 变化即被本轮重新索引（自愈闭环）；`/status` 新增 `last_prune` 字段。新增 4 项单测 `tests/test_rag_client.py`；E2E 实测：带附件草稿归档后附件落位 `房产证办理/附件/`、删除笔记后增量同步剔除 ai_context 死链条目 | `v1.5.0` |
| 1.4.0 | 2026-08-30 | 行为变更：**原文保留模式成为默认**——用户核心诉求「原文不可被 AI 改变」从长文扩展到全部草稿：新增 `processing.content_rewrite`（默认 false），关闭时所有草稿正文逐字保留、LLM 仅产元数据（目录/文件名/摘要/标签）；开启后仅 `rewrite_max_chars` 阈值内短文允许 AI 整理，超阈值仍保留原文。修复保守模式丢附件转录：新增 `build_preserved_content`，把 OCR/Whisper 转录以「## 附：附件转录（机器自动生成…以原附件为准）」折叠引用块附加文末（v1.3.1 原实现正文=原文导致转录不可检索）。summary 幻觉防线：SYSTEM_PROMPT / NOTE_JSON_SCHEMA / 保留模式指令三处强制摘要严格取材原文、禁止出现原文没有的数字。新增 3 项单测（`build_preserved_content`）；实测短草稿默认走保留模式、正文逐字一致 | `v1.4.0` |
| 1.3.1 | 2026-08-30 | 修复：**长草稿归档内容失真**——19941 字符面试准备笔记被 LLM「整理」后仅剩 2702 字节（94% 内容丢失），且编造「准确率提升 17%」「接口覆盖率 75%」等原文不存在的数字。根因四连：① 归档设计为 LLM 重写而非原文保留，「保留关键事实」给了模型自由压缩裁量权；② `max_tokens:4096` 输出预算物理上装不下长文，「禁止遗漏要点」不可能执行；③ 压缩任务+「善用表格代码块」排版诱导触发补全式幻觉；④ 归档成功即删草稿且无备份，损失不可逆。修复：**长文保守模式**（超 `processing.rewrite_max_chars`(默认6000) 字符的草稿，LLM 仅生成元数据，正文原样保留原文）；prompt 加忠实性硬约束（数字/事实必须逐字来自原文、对话体保持原结构、禁缩写扩写）；短文模式 LLM 未返回正文也回退原文（内容永不丢失）；`max_tokens` 提至 16384；归档前自动备份草稿至 `.smartvault/backup/`（保留 100 份，RAG 已排除该目录）；`choose_target_dir` 剥离 LLM 误带的仓库名前缀（修复三级路径误回退未分类）。新增 11 项单测 `tests/test_ingest_fidelity.py`；已实测长文归档正文 43168 字节完整保留、幻觉数字 0 次出现 | `v1.3.1` |
| 1.3.0 | 2026-08-30 | 新增：**OpenAI 兼容适配层** `GET /v1/models` + `POST /v1/chat/completions`（非流式 + `chat.completion.chunk` SSE 流式）——BMO Chatbot 等 Obsidian 插件填 REST API URL `http://127.0.0.1:8788/v1` 即可直连 SmartVault RAG；末条 user 消息作检索 query、携带最近 6 轮历史、客户端 system 人设被忽略（以 RAG 接地约束为准）、参考来源以 Markdown 附录追加在回答末尾；协议契约对齐 BMO 源码（模型列表 `data[].id`、流式 `delta.content` + `finish_reason=="stop"` 停止帧 + `data: [DONE]`）；新增 14 项单测 `tests/test_openai_compat.py`（含 BMO 解析逻辑 Python 复刻回归） | `v1.3.0` |
| 1.2.1 | 2026-08-30 | 修复：聊天界面流式回答中文乱码（ç¬è®° 式 mojibake）——LM Studio 的 `text/event-stream` 响应头不带 charset，requests 按 RFC 默认 ISO-8859-1 解码（`iter_lines(decode_unicode=True)`），UTF-8 中文增量全部变乱码（阻塞模式走 `resp.json()` 有编码探测故正常）；改为逐行取原始 bytes 显式 UTF-8 解码；新增 2 项单测 `tests/test_rag_client.py` | `v1.2.1` |
| 1.2.0 | 2026-08-30 | 新增：**浏览器知识问答界面** `GET /ui`（`static/chat.html` 单页应用，零 CDN 依赖遵守离线隐私承诺）——SSE 流式逐字渲染回答、来源引用 chips（标题+路径+余弦距离）、`/status` 健康角标轮询；此前问答 API 仅能通过 curl / `/docs` 调试台 / 自写脚本调用 | `v1.2.0` |
| 1.1.3 | 2026-08-30 | 修复：长草稿归档报 400「exceeds the available context size」（LM Studio 侧表现为 Channel Error）被误判为 response_format 不支持而无效重发一发注定失败的请求——`LLMClient.chat` 现识别上下文超限错误，直接抛出带操作指引的 RuntimeError（以更大 context length 重载模型或拆分草稿），不误触回退；配套：模型已以 32k 上下文重载（`lms load qwen3-14b -c 32768 --parallel 1`，M4 Pro 24GB 实测可容纳 2 万字符草稿归档，耗时 144s）；新增 3 项单测 `tests/test_llm_client.py` | `v1.1.3` |
| 1.1.2 | 2026-08-30 | 修复：「最近错误分析」误报历史错误——`recent_errors` 原扫描各日志「尾部 3000 行」，而日志 append-only 从不轮转，v1.0.0 时代 pydantic 崩溃循环的 Traceback 被永久当作"最近错误"展示；改为**字节偏移增量扫描**（状态持久化 `logs/.menubar_err_state.json`：首跑从 EOF 清零历史、新文件全文扫描、截断/轮转自动重读、末尾半行顺延；`consume=False` 供综合健康检查"只看不消费"，不抢走错误菜单的新错误）；新增 8 项单测 `tests/test_recent_errors.py` | `v1.1.2` |
| 1.1.1 | 2026-08-30 | 修复：① 菜单栏 ● 图标在刘海屏 MacBook 上不可见——状态项被 macOS 26 ControlCenter 以 ephemeral 定位排入刘海遮挡区（本机 x 663..848），launchd 每次重启 PID 变化又使位置不持久；对策：状态项设 `autosaveName`（位置跨重启持久化，⌘ 拖拽后被记住）+ 启动自诊断（AX 坐标与刘海检测写入 `logs/menubar.stderr.log`，被遮挡时告警）；② 「启动」改幂等——已运行则跳过重启（原逻辑对运行中的服务做 bootout→bootstrap 全量重启，RAG 重载模型 ~17s 期间图标 ⚠ 易被误判为故障）；③ 启动/重启弹窗追加「RAG 加载 10–30 秒期间 ⚠/◐ 属正常」提示 | `v1.1.1` |
| 1.1.0 | 2026-08-30 | 新增菜单栏控制台 `menu_bar_app.py`（rumps）：状态栏图标实时显示服务健康度（●/◐/○/⚠，5 秒轮询）；下拉菜单提供启动/停止/重启/卸载（launchd bootstrap/bootout，含竞态等待）、控制台自身开机自启开关（`com.user.aibrain.menubar`）、综合健康检查、最近错误聚合分析、Terminal 实时日志；`scripts/build_menubar_app.sh` 生成 `SmartVaultMenuBar.app`——因 macOS TCC 限制（GUI app 读不了 ~/Documents），.app 设计为"确保 launchd 代理运行"的启动器（双击即注册开机自启）；单例锁防双开 | `v1.1.0` |
| 1.0.1 | 2026-08-30 | 修复：① `BGEEmbeddings.QUERY_INSTRUCTION` 缺 ClassVar 注解导致 pydantic v2 下类定义即崩（RAG 服务无法启动）；② install_launchd.sh 的 bootout/bootstrap 竞态导致 `Bootstrap failed: 5`（改为等待旧实例真正拆除 + bootstrap 重试） | `v1.0.1` |
| 1.0.0 | 2026-08-30 | 首个完整版本：模块 A 摄入归档守护进程 + 模块 B 本地 RAG 服务 + launchd 自启 + 22 项纯函数单测 | `v1.0.0` |

版本号约定：功能/行为变更递增次版本号（X.**Y**.Z），缺陷修复递增修订号（X.Y.**Z**）。

## 1. 架构总览

```
                     ┌──────────────────── 本机 localhost（零外联）────────────────────┐
                     │                                                                │
  Obsidian Vault ×N  │  ┌─ ingest_daemon.py（模块 A，常驻）─┐      ┌ LM Studio :1234 ┐ │
 ┌────────────────┐  │  │ watchdog 监听「待处理笔记」        │      │ qwen2.5-7b-     │ │
 │ 待处理笔记/     ├──┼─▶│ 多模态解析 → LLM 结构化 → 归档落盘 ├─────▶│ instruct (对话)  │ │
 │ ai_context.md  │◀─┼──│ 改写链接 + obsidian:// 唤醒       │      └─────────────────┘ │
 │ 其余目录       │  │  └──────────────────────────────────┘                            │
 └───────┬────────┘  │                                                                │
         │           │  ┌─ rag_api.py（模块 B，:8788）────┐      ┌ bge-small-zh(MPS)┐ │
         └───────────┼─▶│ 后台增量索引(mtime+md5)           ├─────▶│ + ChromaDB 持久化 │ │
                     │  │ POST /ask 检索→RAG 生成→sources  │      └─────────────────┘ │
                      │  │ /v1/chat/completions(OpenAI 兼容)│◀─ BMO Chatbot 等 Obsidian 插件│
                     │  └──────────────────────────────────┘                            │
                     └────────────────────────────────────────────────────────────────┘
```

两条数据面：
- **归档链路（写）**：收件箱草稿 → 附件解析 → LLM Strict JSON → 净化校验 → 终稿落盘 → `ai_context.md` 追加历史索引 → `obsidian://open` 唤醒
- **查询链路（读）**：Vault 全量 Markdown → 增量嵌入索引 → `/ask` Top-K 检索 → RAG 生成（带来源）

三条设计红线（改动前自问是否破坏）：
1. **100% 本地**：运行期只访问 localhost；首次手动下载模型除外
2. **LLM 输出永不直接写盘**：一律经 `sanitize_*` 净化与 `choose_target_dir` 校验
3. **失败保留原稿**：任何异常草稿留在收件箱，绝不静默丢稿

## 2. 模块地图（改哪里、找哪里）

### 2.1 ingest_daemon.py（单文件约 1150 行，自上而下分层）

| 层 | 关键符号 | 职责 / 修改入口 |
|---|---|---|
| 配置 | `load_config` | 读 config.json；注入 `_config_dir` 私有键作为相对路径基准 |
| 数据模型 | `Vault` | name / root / inbox / context_file 四元组；唤醒 URI 依赖 name |
| 净化校验 | `sanitize_filename` `sanitize_folder_parts` `sanitize_tags` | 非法字符、`..` 穿越拦截、tag 规范化去重 |
| 目标目录 | `choose_target_dir` | 已存在目录优先 → 深度 ≤ max_folder_depth 才允许新建整条链路 → 兜底 fallback；收件箱与根目录永远禁止 |
| 引用解析 | `find_attachment_refs` `resolve_attachment` `rewrite_links` | 提取 `![[x]]` 嵌入、标准 md 附件链接与 HTML `<img src>` 等内嵌标签（Kindle/HTML 转换产物常用）→ 被链接即附件：任意扩展名（.zip/.epub/.dwg 等）均随笔记迁移，仅排除 .md/无扩展名笔记链接与 URL/data: 内嵌 → 收件箱定位（无扩展名自动补全常见类型）→ 附件改名后同步改写正文（HTML src 按相对路径语义改写，引号风格保持） |
| 多模态解析 | `parse_attachment`（按扩展名分发） | png/jpg…→ocrmac(Vision)；音视频→mlx-whisper（自动回退 openai-whisper）；pdf→PyMuPDF 文本层优先、扫描页（中文手写）RapidOCR（PP-OCRv6）兜底；docx/xlsx/pptx→对应库；pages/numbers/key→包内 QuickLook/Preview.pdf |
| 上下文 | `scan_tree` `load_ai_context` `_drop_stale_ctx_entries` | 目录树（排除隐藏目录/收件箱，限 tree_depth 层）；ai_context 超长时保头尾智能截断；追加历史条目前按文件名移除同 stem 旧条目（重归档自愈，防旧目录经 Prompt 锚定 LLM、防条目重复堆积，仅匹配标准生成行、人工条目保守不动） |
| LLM | `LLMClient` `SYSTEM_PROMPT` `parse_llm_json` `apply_thinking_switch` | 优先 `response_format: json_schema` 结构化输出，LM Studio 400 时自动降级（上下文超限除外——识别为独立错误并给出重载指引，不做无谓重发）；鲁棒解析（去围栏/截噪声/tags 与 link_terms 类型容错）；采样参数显式下发（Qwen3 thinking 模式推荐值），thinking=false 时经 `/no_think` 软开关跳过思考 |
| 双链注入 | `apply_wikilinks` | v1.7.0 **确定性双链注入**：LLM 仅输出 `link_terms` 名词清单，包裹由纯 `re` 代码完成——逐字保真（除新增 `[[ ]]` 外不动任何字符）、幻觉名词匹配不到自动忽略；禁区（代码围栏/行内代码/链接 URL/HTML 标签）不注入；ASCII 词边界 + lookaround 防重复包裹 + 长词优先 |
| 落盘 | `build_final_markdown` `run_pipeline` `unique_path` `prune_empty_dirs` | YAML frontmatter（引号转义）；管线编排；重名追加 ` 2` 序号；归档后/启动补扫时清理收件箱空目录（含仅含 .DS_Store 的，根目录与含真实文件的目录永不删） |
| 守护 | `VaultState`、watchdog handler、`run_daemon` / `run_check` / `run_scan` / `main` | 双闸防抖（观察窗+静默期+大小稳定）、附件到齐等待、每 Vault 独立队列与工作线程 |

CLI：`--check` 环境自检｜`--scan` 批处理积压｜`--once 文件 --vault 名称` 单篇调试｜无参前台常驻。

### 2.2 rag_api.py（单文件约 810 行）

| 层 | 关键符号 | 职责 / 修改入口 |
|---|---|---|
| 离线闸门 | 模块顶部 `os.environ` | `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` 等，必须在 import 前设置 |
| 嵌入 | `BGEEmbeddings` | 本地 sentence-transformers 加载 bge-small-zh-v1.5；检索 query 加官方指令前缀；MPS 不可用回退 CPU |
| 索引 | `VaultIndexer` | Markdown 标题+递归分块；mtime 短路 + md5 兜底的增量 `sync`；`rebuild` 全量重建；`search` 余弦检索；排除 `rag.exclude_folders` |
| 死链清理 | `prune_ai_context_entries` | `sync()` 开头顺带剔除 ai_context.md 中指向已删除笔记的「历史归档索引」条目（append-only 遗留死链）；无剔除不写盘；人工改写过的非标准条目保守不动；剔除后 mtime 变化即被本轮重新索引（自愈闭环）；`/status` 报 `last_prune` |
| 生成 | `LMStudioClient` | 阻塞 + SSE 流式两种调用（流式逐行 bytes 显式 UTF-8 解码，防 requests 对无 charset 头按 ISO-8859-1 解码的乱码陷阱）；RAG 提示词拼装；默认非 thinking 采样（Qwen3 官方推荐值）+ `/no_think` 软开关 |
| API | FastAPI `lifespan`、`/ask` `/health` `/status` `/reindex`、`GET /ui`、`/v1/models` `/v1/chat/completions` | lifespan 启动后台周期 sync 线程；`/ask` 支持 `stream:true`（SSE：sources→message×N→done）；`/ui` 返回 `static/chat.html` 单页聊天界面；`/v1/*` 为 OpenAI 兼容适配层（BMO Chatbot 等插件直连，query=末条 user 消息、历史≤6 轮、来源以 Markdown 附录并入回答） |

持久化：索引状态 `data/index_state.json`（路径→{mtime,md5}）；向量库 `data/chroma/`。

### 2.3 menu_bar_app.py（菜单栏控制台，约 465 行）

macOS 状态栏常驻应用（rumps/PyObjC），是 launchd 的图形前端 + 诊断面板。

| 层 | 关键符号 | 职责 / 修改入口 |
|---|---|---|
| 服务描述 | `ServiceSpec`（INGEST / RAG / MENUBAR 三个实例）、`_resolve_log_dir` | label / 日志 / 模板路径；`render()` 做 `@PROJECT_DIR@`/`@PYTHON@`/`@LOG_DIR@` 占位符替换（与 install_launchd.sh 等价）；日志目录读 config `log_dir`（默认 ~/Library/Logs/SmartVault，v1.7.1 迁出 TCC 保护区） |
| launchd 封装 | `svc_info` `svc_state` `svc_start/stop/uninstall` | `launchctl print` 判 installed；`list` 解析 PID 与上次退出码（区分 running/starting/crashed/stopped）；启动幂等（已运行直接返回，不再隐式重启）；未运行时 bootout→等待→bootstrap×5 重试（复用 v1.0.1 竞态修复）；停止=bootout（保留 plist）；卸载=bootout+删 plist |
| 诊断 | `port_open` `http_json` `http_post_json` `recent_errors` `tail_in_terminal` | 端口探测（不做阻塞 HTTP）；`/health` `/status` 拉取；`http_post_json` 触发型接口（`/reindex`）；日志错误**增量扫描**（按字节偏移只报自上次检查以来的新增，状态持久化 `LOG_DIR/.menubar_err_state.json`；首跑清零历史、截断/轮转自动重读、末尾半行顺延、连续去重；`consume=False` 供健康检查只看不消费）；osascript 让 Terminal 执行 tail -f |
| 单例锁 | `_acquire_single_instance_lock` | `fcntl.flock(LOG_DIR/.menubar.lock)`；重复实例直接退出（bootstrap 安装自启项时靠它避免双图标） |
| UI | `SmartVaultBar`（rumps.App） | `refresh()` 仅状态 key 变化时重建菜单（防闪烁）；`@rumps.timer(5)` 轮询；title 动态：●/◐/○/⚠；`_full_check` 弹窗逐项 ✔/✘；`_log_item_geometry` 启动自诊断（状态项 AX 坐标 + 刘海遮挡检测写 stderr；`autosaveName` 位置持久化） |
| RAG 维护 | `_sync_index` `_rebuild_index` | 「🧹 清理已删笔记残留（同步索引）」→ `POST /reindex {rebuild:false}`（移除已删文件向量 + 剔除 ai_context.md 死链）；「♻️ 重建 RAG 索引（清空后重建）」→ 确认弹窗后 `POST /reindex {rebuild:true}`（清空向量库按现状重建，清空测试库后归零索引）；RAG 未运行时给出指引 |

关键决策：MENUBAR 自启项 `KeepAlive=false`（手动退出不被拉起，RunAtLoad 登录启动）；对 KeepAlive 服务"停止"必须 bootout（kill 会被 launchd 复活）；**TCC 规避（v1.7.1 修正认知）**——经 LaunchServices（Finder/open）启动的 GUI app 无 `~/Documents` 读权限（`PermissionError: pyvenv.cfg`），故 `.app` 仅是"确保 launchd 代理运行"的启动器（plist 内容构建时硬编码、运行时不读项目文件），菜单栏进程一律由 launchd 拉起（双击 .app = 安装/唤醒代理，同时注册开机自启）；**但 launchd 代理并非不受 TCC 约束**——launchd 在 exec 前就要打开 stdout/stderr 日志，macOS 26 起对 ~/Documents 的打开请求（kTCCServiceSystemPolicyDocumentsFolder）被拒，作业以 78 EX_CONFIG 秒退且日志为空（v1.7.1 前曾误判"launchd 无 TCC 限制"，重启后三作业全体阵亡）；故**日志一律放 `~/Library/Logs/SmartVault`**（plist `@LOG_DIR@` + config `log_dir` 双通道），项目/数据文件的常规读写不受影响（实测：迁移后守护进程正常读写 ~/Documents 下的 Vault 与配置，无需"完全磁盘访问"）。

**刘海屏注意（v1.1.1 教训）**：第三方状态项由 ControlCenter 按 ephemeral 定位自动布局，菜单栏拥挤时会被排入刘海遮挡区（本机 14" MBP 为 x 663..848）——图标「存在于系统但肉眼不可见」，且 ControlCenter 不会因其他图标被移除而自动重排旧项。对策：状态项已设 `autosaveName`（位置跨重启持久化，⌘ 拖拽后记住）；排查看 `logs/menubar.stderr.log` 的 `[SmartVault][诊断]` 行（含 AX 坐标与遮挡告警）；复发时可重启 menubar 作业触发重新布局，或 ⌘ 拖拽图标到时钟左侧。

## 3. 配置参考（config.json 全字段语义）

| 键 | 默认值 | 说明 |
|---|---|---|
| `lm_studio.base_url` | `http://localhost:1234/v1` | LM Studio OpenAI 兼容端点 |
| `lm_studio.chat_model` | `qwen2.5-7b-instruct` | 必须与 LM Studio 已加载模型的标识**完全一致**（模型页可复制），否则 404 |
| `lm_studio.temperature / top_p / top_k / thinking` | 0.6 / 0.95 / 20 / true | 归档侧采样参数，Qwen3 官方 thinking 模式推荐值；thinking=false 时末条 user 消息追加 `/no_think` 软开关跳过思考（大幅提速） |
| `lm_studio.max_tokens / timeout_seconds` | 4096 / 300 | 归档生成预算与超时；长文输入偶发超时可调大 timeout 或减小 limits |
| `lm_studio.structured_output` | `true` | json_schema 结构化输出；LM Studio 不支持报 300/400 时代码自动降级为提示词模式 |
| `inbox_folder_name` | `待处理笔记` | 各 Vault 收件箱目录名（守护进程自动创建） |
| `context_file` / `ai_context_max_chars` / `tree_depth` | `ai_context.md` / 6000 / 2 | 注入 LLM 的仓库上下文：规则+历史索引文件、截断上限、目录树层数 |
| `obsidian.wake_enabled` | `true` | 归档完成后用 `obsidian://open` 唤醒 |
| `vision.language_preference` | `[zh-Hans, en-US]` | Vision OCR 语言提示，乱码时调整 |
| `ocr.engine` | `rapidocr` | PDF 扫描页 OCR 引擎（v1.8.0）：`rapidocr`（PP-OCRv6，中文手写友好，模型内置 wheel 离线可用）/ `off`（关闭，扫描页仅占位说明） |
| `ocr.pdf_dpi` / `pdf_max_ocr_pages` / `pdf_min_text_chars` | 200 / 20 / 20 | 扫描页渲染 DPI（手写推荐 200~300）/ 单文件 OCR 页数上限（防大扫描件阻塞流水线，超出页如实注明）/ 页文本层字符阈值（低于该值视为扫描页走 OCR） |
| `whisper.backend` | `auto` | `mlx`（Metal 优先）/ `openai` / `auto`（先 mlx 失败回退 openai-whisper） |
| `whisper.mlx_model` / `openai_model` / `language` | whisper-large-v3-turbo / small / zh | 两种后端的模型与源语言 |
| `processing.debounce_seconds` / `quiet_seconds` | 8 / 3 | 双闸防抖：事件观察窗 + 收件箱静默期 |
| `processing.attachment_wait_timeout` | 30 | 等待引用附件到齐的最长秒数（超时则继续处理现有部分） |
| `processing.attachments_subfolder` | `附件`（空=与笔记同目录） | 附件落盘子目录名：归档附件进入归类目录下的 `附件/` 子目录 |
| `processing.allow_new_folder` / `max_folder_depth` / `fallback_folder` | true / 2 / 未分类 | LLM 建目录的权限边界：是否允许新建、最大深度、非法/超深时兜底目录 |
| `processing.content_rewrite` / `rewrite_max_chars` | `false` / 6000 | **正文改写总开关**：false=原文保留模式（默认，全部草稿正文逐字保留，LLM 仅产元数据，附件转录折叠附加文末）；true=短于阈值的草稿允许 AI 排版整理（仍受逐字保真约束），超阈值一律保留原文 |
| `processing.auto_wikilinks` | `true` | **确定性双链注入开关**（v1.7.0）：LLM 输出 `link_terms` 名词清单，归档落盘前由纯代码对正文包裹 `[[ ]]`（逐字保真、幻觉名词自动失效、代码/URL/HTML 禁区不注入）；false=整体关闭 |
| `limits.raw_note_max_chars` / `attachment_max_chars` | 30000 / 12000 | 注入 LLM 前截断阈值，防上下文爆炸 |
| `rag.enabled` | `true` | 模块 B 总开关 |
| `rag.embedding_model_path` / `embedding_device` | `models/bge-small-zh-v1.5` / `mps` | 相对 config.json 的本地模型目录；device 不可用时自动回退 cpu |
| `rag.chroma_dir` / `collection_name` | `data/chroma` / `smartvault` | 向量库持久化位置与集合名 |
| `rag.chunk_size` / `chunk_overlap` / `top_k` | 500 / 80 / 4 | 分块与检索参数；**改动后必须全量 rebuild 才生效** |
| `rag.chat_temperature` / `chat_top_p` / `chat_top_k` / `chat_thinking` | 0.7 / 0.8 / 20 / false | 问答侧 LLM 采样参数（Qwen3 官方非 thinking 推荐值）；chat_thinking=false 经 `/no_think` 软开关跳过思考（E2E 全链路 5.6s）；chat_ 前缀避免与检索 top_k 混淆，且不回读 lm_studio.temperature |
| `rag.rescan_seconds` | 300 | 后台增量扫描周期 |
| `rag.exclude_folders` | `.obsidian` `.trash` `待处理笔记` | 索引排除目录 |
| `api.host` / `api.port` | `127.0.0.1` / 8788 | 只绑本机；改端口须同步改 launchd plist 并重装 |
| `vaults[]` | — | `name` 必须与 Obsidian 内仓库名一致（唤醒 URI 依赖）；`path` 为绝对路径 |
| `log_dir` | `~/Library/Logs/SmartVault` | 日志目录（自动轮转）；支持 `~` 绝对路径与相对路径（挂 config.json 所在目录）。**不可指回 ~/Documents**——TCC 禁止 launchd 打开其下 stdout/stderr 日志，作业会以 78 EX_CONFIG 秒退（v1.7.1） |

⚠️ 敏感性：`config.json` 含个人绝对路径与仓库名，**已列入 .gitignore 永不入库**；仓库中提交的是 `config.example.json` 模板（路径占位 `YOUR_NAME`）。

## 4. 关键流程时序

### 4.1 摄入管线（`run_pipeline`，模块 A 核心）
1. 读草稿（剥离已有 frontmatter）→ `find_attachment_refs` 提取引用 → `resolve_attachment` 在收件箱定位（含无扩展名补全）
2. 等待全部引用附件就位（`attachment_wait_timeout` 兜底放行）
3. 逐个 `parse_attachment` 按类型提取文本（超过 `attachment_max_chars` 截断）
4. 组装 Prompt：SYSTEM_PROMPT + 目录树 + ai_context（截断后）+ 草稿 + 附件文本
5. `LLMClient` 结构化输出 → `parse_llm_json` 容错解析出六字段 Strict JSON（含 `link_terms` 名词清单）
6. `choose_target_dir` 校验/创建目录 → `unique_path` 确定终稿唯一路径
7. 附件移动（目标重名自动加序号 + `rewrite_links` 同步改写正文引用）→ **`apply_wikilinks` 确定性双链注入**（v1.7.0：按 link_terms 纯代码包裹 `[[ ]]`，逐字保真）→ 写终稿（YAML frontmatter）→ 删除草稿 → `prune_empty_dirs` 清理收件箱空目录
8. `ai_context.md` 追加历史索引（v1.6.1 起先按文件名移除同名旧条目——重归档自愈，防旧目录锚定 LLM）→ `obsidian://open?vault=…&file=…` 唤醒

### 4.2 增量索引（`VaultIndexer.sync`，模块 B 后台线程）
mtime+size 未变 → 直接跳过（O(1)）→ 变了才算 md5 复核 → 确实变化则重新分块并按文档 ID 先删后插 → 文件已删除则同步删向量 + `prune_ai_context_entries` 剔除 ai_context.md 死链条目。`rebuild()` 清空 collection 全量重建（**全局操作**：所有 Vault 的向量一并清空后重嵌）。

多仓库隔离事实：所有 Vault 共用同一 collection（`data/chroma/`，位于项目目录而非任何仓库内），索引键 `仓库名/相对路径` 隔离——增量 sync 按文件粒度互不影响；rebuild 波及全部仓库；注销某 Vault（config 移除条目 + 重启守护进程）后其全部向量在下一轮 sync 自动移除（「注销即清理」，无需 rebuild）。

### 4.3 `/ask` 请求生命周期
查询加 bge 指令前缀 → 嵌入 → Top-K 余弦检索（按来源路径去重）→ 拼 RAG 提示词（上下文+问题+引用要求）→ LM Studio 生成。SSE 模式事件序：`sources`（先推引用）→ `message`×N（正文增量）→ `done`。

检索范围包含归档笔记文末的「附：附件转录」折叠块（OCR/whisper/PDF/Office 转录随正文一起分块嵌入）——仅存在于附件中的事实同样可被问答命中并引用来源；但**从未走过收件箱管线的存量附件**（直接放进仓库的图片/录音）不会被解析与索引。

检索范围是**全部注册仓库（全局）**：`/ask` 与 `/v1/chat/completions`（BMO 等客户端）共用 `search()`——对共享 collection 做整体 Top-K 余弦检索，无 vault 过滤（块元数据中的 `vault` 字段仅用于路径展示）。BMO 在 A 仓库提问可命中 B 仓库笔记；按仓库过滤提问目前不支持（如需实现，可在 `search()` 加 Chroma `where={"vault": ...}` 过滤）。

## 5. 常见二次开发场景（How-to）

| 想做什么 | 改哪里 |
|---|---|
| 新增附件类型（如 .epub） | `ingest_daemon.py` 的 `parse_attachment` 扩展名分发表加分支 + 写新解析函数（返回纯文本）+ `requirements.txt` 加库 |
| 换对话模型 | LM Studio 加载新模型 → 改 `lm_studio.chat_model`（建议先用 `--check` 验证连通） |
| 换嵌入模型 | 下载到 `models/` → 改 `rag.embedding_model_path` → `POST /reindex {"rebuild": true}`；非 bge 系模型需同步调整 `BGEEmbeddings` 的 query 指令前缀 |
| 调归档文风/输出字段 | `SYSTEM_PROMPT`（五个 JSON 字段名不能变，`build_final_markdown` 依赖它们） |
| 调分块粒度 | `rag.chunk_size/overlap` → 必须 rebuild |
| 新增 Vault | `config.vaults` 追加 → `launchctl kickstart -k gui/$(id -u)/com.user.aibrain` 重启守护进程 |
| 改 API 端口 | `api.port` + `launchd/com.user.aibrain.rag.plist` 里的 8788 → 重跑 `scripts/install_launchd.sh` |
| 新增排除目录（不索引） | `rag.exclude_folders` 追加 → 下轮 sync 自动剔除（或手动 rebuild） |
| 删除笔记 / 清空测试目录后的清理 | 向量块：后台每 300s 增量 sync 自动移除，或菜单栏「🧹 清理已删笔记残留」立即触发；ai_context.md 死链条目同轮被剔除；彻底归零索引（如清空整个测试库）用菜单栏「♻️ 重建 RAG 索引」（全局操作，所有 Vault 一并重建） |
| 注销一个 Vault（弃用仓库） | **先**从 `config.vaults` 移除条目 → 删除仓库文件夹 → 重启摄入守护进程 → 点「🧹 清理已删笔记残留」（该仓库全部向量自动移除，无需 rebuild）。顺序不能反：只删文件夹不注销，守护进程每次启动都会记「Vault 目录不存在」ERROR（不会重建目录、不影响其他仓库，但污染「最近错误分析」）。多仓库清理影响详见 README「多仓库与索引清理」 |
| 从旧仓库迁移（笔记 + 附件目录） | 文档连同 `附件/` 子目录整体放入收件箱即可：watchdog `recursive=True` 递归监听、`resolve_attachment()` 递归按文件名定位（wikilink 带不带路径均可）。注意：附件目录内的 **.md 会被当作草稿**（`rglob("*.md")`）；归档后空 `附件/` 目录残留需手动删 |
| 归类全进「未分类」 | v1.6.0 起已根治：SYSTEM_PROMPT 要求 LLM 无合适目录时按主题自建简洁一级目录（2~6 字）、严禁输出「未分类」等无意义名，分类体系随归档自然生长。存量「未分类」笔记重新归类：整篇（连同附件）移回收件箱自动重归档（正文原样保留、元数据重新生成、历史索引死链自动剔除）；也可在 ai_context.md 规则区写分类约定进一步约束 |
| 笔记被误归档（如内容与目录主题无关） | 整篇（连同附件）移回收件箱即可：v1.6.1 起归档时自动替换 ai_context.md 中同名旧条目（此前旧条目注入 Prompt 会锚定 LLM 沿用旧目录，需手动删条目）；元数据重新生成、正文原样保留、指向旧路径的向量在下一轮增量同步自动剔除。另：超短碎片笔记（链接收藏/账号信息）已由 SYSTEM_PROMPT 规则 8 要求按实际用途归类、禁止凭弱关联蹭已有目录 |

## 6. 测试与发布流程

```bash
# 日常验证（零三方依赖，任何机器可跑）
python3 -m unittest discover -s tests          # 94 项单测（纯函数 + 客户端解码 + OpenAI 兼容层 + 归档保真性 + ai_context 清理 + 空目录清理 + 重归档自愈 + Qwen3 采样调优 + HTML src 附件引用 + 任意类型附件随迁 + 确定性双链注入）
python3 -m py_compile ingest_daemon.py rag_api.py scripts/build_index.py

# 联调冒烟
bash scripts/start_all.sh                      # 前台同跑 RAG + 守护进程
python ingest_daemon.py --once 测试.md --vault 工作事务   # 单篇全管线验证
```

发布 checklist（每次对外提交前）：
1. 单测全绿 + `--once` 带图草稿冒烟走通全管线
2. **文档同步（铁律，详见 `CLAUDE.md`）**：更新本文档第 0 节版本表 + 全部受影响小节，README.md 对应段落同步——**文档不同步 = 变更未完成**。AI 助手必须主动执行本条，无需用户提醒
3. 提交并打标：
```bash
git add -A && git commit -m "feat: <摘要>"
git tag vX.Y.Z && git push && git push --tags
```

## 7. 故障排查表

| 症状 | 多半原因 | 处置 |
|---|---|---|
| `--check` 报 LM Studio 不通 | 服务未启动 / 端口非 1234 | LM Studio → Developer → Start Server |
| 归档报模型 404 | `chat_model` 与实际加载模型名不符 | 从 LM Studio 模型页复制准确标识 |
| 结构化输出 400 后速度变慢 | 走了降级路径（提示词模式） | 升级 LM Studio 至支持 `response_format` 的版本 |
| 归档报 exceeds the available context size / LM Studio 侧 Channel Error | 草稿+仓库上下文超过模型加载时的 context length（归档需容纳 ~12k 输入 + 4k 输出） | 以更大上下文重载模型：`lms load qwen3-14b -c 32768 --parallel 1`；或拆分超长草稿（日志会给出该指引） |
| 草稿一直不被处理 | 双闸防抖未满足 / 附件未到齐 | 查 `logs/ingest_daemon.log`；用 `--once` 单篇复现 |
| 归档成功但 Obsidian 未弹出 | `vaults[].name` ≠ Obsidian 仓库名 | 改为完全一致的名称 |
| OCR 结果乱码 | 语言偏好不匹配 | 调 `vision.language_preference` |
| 手写 PDF 识别效果差/漏字 | 渲染 DPI 偏低或字迹过草 | 调高 `ocr.pdf_dpi`（200→300）；确认 `ocr.engine` 未设 `off`；转录仅以折叠块附加文末，原文与原文件不受影响 |
| 新草稿放入收件箱后一直不被处理（日志也无任何记录） | 收件箱目录曾被删除，watchdog 监听已断开（重建目录不会自动重挂，守护进程看似正常） | 重启摄入守护进程（菜单栏或 `launchctl kickstart -k gui/$(id -u)/com.user.aibrain`），启动时自动补扫积压草稿 |
| `/ask` 慢且日志见 CPU 回退 | MPS 不可用（内存压力/驱动） | 确认 PyTorch 为 arm64 wheel：`python -c "import torch; print(torch.backends.mps.is_available())"` |
| Chroma 报锁 / 句柄错误 | 同时跑了两个 rag 实例（手动+launchd） | `launchctl list \| grep aibrain` 只保留一份 |
| launchd 反复重启（10s 一次） | venv 路径失效（重建过 .venv） | 重跑 `scripts/install_launchd.sh` 重新生成 plist |
| 改了 chunk_size 检索无变化 | 未全量重建 | `POST /reindex {"rebuild": true}` |
| `--once` 报草稿不存在 | 相对路径基于该 Vault 收件箱解析 | 传绝对路径，或确认文件名 |
| `Bootstrap failed: 5: Input/output error` | bootout 拆旧实例是异步的，立刻 bootstrap 同名标签时旧实例未拆完（竞态） | 已在 install_launchd.sh 内置等待+重试；手动操作时 bootout 后等 1-2 秒再 bootstrap |
| 改代码后 RAG 服务仍报旧错误 | launchd 跑的是旧进程 | `bash scripts/install_launchd.sh` 重装（会自动重启两个服务） |
| 自定义 pydantic 模型子类在类定义时抛 `PydanticUserError` | pydantic v2 把类体内未注解的裸赋值当模型字段 | 给常量加 `ClassVar[...]` 注解（见 `BGEEmbeddings.QUERY_INSTRUCTION`） |
| 菜单栏图标不出现 | rumps 未安装 / Python 非 GUI 会话 | `pip install rumps`；确认用 `.venv/bin/python menu_bar_app.py` 或 `open SmartVaultMenuBar.app` 启动，看 `logs/menubar.stderr.log` |
| 双击 .app 后秒退 / `PermissionError: pyvenv.cfg` | macOS TCC：LaunchServices 启动的 GUI app 默认无 `~/Documents` 读权限 | 用官方 `.app`（仅做 launchctl，进程由 launchd 拉起）即可规避；若自行改为直接 exec python 则需给 app 授"完全磁盘访问"或在终端运行 |
| 重启后三个服务全不自启（`launchctl print` 见 exit 78 EX_CONFIG、runs 递增、日志 0 字节）；双击 .app 也无反应 | macOS 26 TCC：launchd 在 exec 前打开 stdout/stderr 日志被拒（~/Documents 受保护，xpcproxy 的授权请求被 sandboxd 否决，进程从未创建） | v1.7.1 已将日志迁至 `~/Library/Logs/SmartVault`；旧装机复现时重跑 `install_launchd.sh` + 重建 .app 让新 plist 生效；.app 启动器现会在失败时弹窗给出退出码与日志路径 |
| 菜单里点"停止"后服务又复活 | KeepAlive 作业 `launchctl kill` 后会被拉起 | 属正常——菜单"停止"已用 bootout（会连同自启一起失效），需恢复点"启动"即可 |
| 菜单栏点了"启动"但 RAG 仍显示 ⚠ | 嵌入模型加载需 30–60 秒 | 等 `/health` 就绪后图标自动变 ●；也可用"综合健康检查"确认 |
| 双击 .app 提示已在运行 | 单例锁生效（另一实例持有 `logs/.menubar.lock`） | 顶部状态栏找已有图标；确无图标则删锁文件后重启 |

## 8. 技术债与已知限制（诚实清单）

1. **iWork 附件**只读包内 `QuickLook/Preview.pdf`（通常仅首页预览）；需要全文请先导出 PDF
2. `load_config` 向 cfg 注入 `_config_dir` 私有键——靠下划线约定区分，若未来序列化 cfg 需剔除
3. **单文件架构**：ingest ~1150 行尚可控；超过 ~1500 行应拆为 `smartvault/` 包（parsers / llm / pipeline / daemon 分层）
4. `run_check` 中用 `(_ for _ in ()).throw(...)` 表达探测失败，可读性一般，可改为普通 try/except
5. 无 watchdog / LM Studio 真实环境集成测试，单测只覆盖纯函数
6. 同名附件若在仓库多处存在，只保证目标目录内无冲突（Obsidian wikilink 语义本身如此）
7. 300s LLM 超时对 14B 模型 + 30k 字符输入偏紧张（实测 14B / 2 万字符草稿归档全程 144s），可调 `timeout_seconds` 或减小 `limits.*`；v1.6.2 采样对齐官方推荐值后归档明显提速（1474 字符 15.8s），仍紧张可设 `lm_studio.thinking: false` 跳过思考

## 9. 升级路线（Roadmap，按价值排序）

- **短期**：检索重排（本地 bge-reranker，top20→top4）；`/ask` 支持按 vault 过滤；笔记反链注入上下文
- **中期**：图像语义检索（CLIP 嵌入附件）；多模型路由（超长附件走大上下文模型）；摄入管线断点续跑状态
- **长期**：MCP 服务器封装（让任意 AI 客户端查询 SmartVault）；浏览器剪藏直达收件箱；Obsidian 插件化问答面板

> 维护者自勉：动架构级改动前先跑第 6 节 checklist，保证 main 分支任何时刻可用。


