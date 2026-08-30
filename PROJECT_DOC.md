# SmartVault 维护文档（PROJECT_DOC.md）

> 本文档面向维护者（未来的你 / AI 助手）：记录架构决策、模块地图、配置语义、
> 常见二次开发场景与升级路线。日常使用请读 [README.md](README.md)。
> **约定：改动代码必须同步更新本文档对应小节；每次发布更新第 0 节并打 git tag。**

## 0. 版本记录

| 版本 | 日期 | 变更摘要 | git tag |
|---|---|---|---|
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
| 引用解析 | `find_attachment_refs` `resolve_attachment` `rewrite_links` | 提取 `![[x]]` 嵌入与标准 md 附件链接（跳过 URL/锚点/md 笔记）→ 收件箱定位（无扩展名自动补全）→ 附件改名后同步改写正文 |
| 多模态解析 | `parse_attachment`（按扩展名分发） | png/jpg…→ocrmac(Vision)；音视频→mlx-whisper（自动回退 openai-whisper）；pdf→PyMuPDF；docx/xlsx/pptx→对应库；pages/numbers/key→包内 QuickLook/Preview.pdf |
| 上下文 | `scan_tree` `load_ai_context` | 目录树（排除隐藏目录/收件箱，限 tree_depth 层）；ai_context 超长时保头尾智能截断 |
| LLM | `LLMClient` `SYSTEM_PROMPT` `parse_llm_json` | 优先 `response_format: json_schema` 结构化输出，LM Studio 400 时自动降级；鲁棒解析（去围栏/截噪声/tags 类型容错） |
| 落盘 | `build_final_markdown` `run_pipeline` `unique_path` | YAML frontmatter（引号转义）；管线编排；重名追加 ` 2` 序号 |
| 守护 | `VaultState`、watchdog handler、`run_daemon` / `run_check` / `run_scan` / `main` | 双闸防抖（观察窗+静默期+大小稳定）、附件到齐等待、每 Vault 独立队列与工作线程 |

CLI：`--check` 环境自检｜`--scan` 批处理积压｜`--once 文件 --vault 名称` 单篇调试｜无参前台常驻。

### 2.2 rag_api.py（单文件约 650 行）

| 层 | 关键符号 | 职责 / 修改入口 |
|---|---|---|
| 离线闸门 | 模块顶部 `os.environ` | `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` 等，必须在 import 前设置 |
| 嵌入 | `BGEEmbeddings` | 本地 sentence-transformers 加载 bge-small-zh-v1.5；检索 query 加官方指令前缀；MPS 不可用回退 CPU |
| 索引 | `VaultIndexer` | Markdown 标题+递归分块；mtime 短路 + md5 兜底的增量 `sync`；`rebuild` 全量重建；`search` 余弦检索；排除 `rag.exclude_folders` |
| 生成 | `LMStudioClient` | 阻塞 + SSE 流式两种调用；RAG 提示词拼装 |
| API | FastAPI `lifespan`、`/ask` `/health` `/status` `/reindex` | lifespan 启动后台周期 sync 线程；`/ask` 支持 `stream:true`（SSE：sources→message×N→done） |

持久化：索引状态 `data/index_state.json`（路径→{mtime,md5}）；向量库 `data/chroma/`。

### 2.3 menu_bar_app.py（菜单栏控制台，约 465 行）

macOS 状态栏常驻应用（rumps/PyObjC），是 launchd 的图形前端 + 诊断面板。

| 层 | 关键符号 | 职责 / 修改入口 |
|---|---|---|
| 服务描述 | `ServiceSpec`（INGEST / RAG / MENUBAR 三个实例） | label / 日志 / 模板路径；`render()` 做 `@PROJECT_DIR@`/`@PYTHON@` 占位符替换（与 install_launchd.sh 等价） |
| launchd 封装 | `svc_info` `svc_state` `svc_start/stop/uninstall` | `launchctl print` 判 installed；`list` 解析 PID 与上次退出码（区分 running/starting/crashed/stopped）；启动走 bootout→等待→bootstrap×5 重试（复用 v1.0.1 竞态修复）；停止=bootout（保留 plist）；卸载=bootout+删 plist |
| 诊断 | `port_open` `http_json` `recent_errors` `tail_in_terminal` | 端口探测（不做阻塞 HTTP）；`/health` `/status` 拉取；日志尾部错误正则聚合 + 连续去重；osascript 让 Terminal 执行 tail -f |
| 单例锁 | `_acquire_single_instance_lock` | `fcntl.flock(logs/.menubar.lock)`；重复实例直接退出（bootstrap 安装自启项时靠它避免双图标） |
| UI | `SmartVaultBar`（rumps.App） | `refresh()` 仅状态 key 变化时重建菜单（防闪烁）；`@rumps.timer(5)` 轮询；title 动态：●/◐/○/⚠；`_full_check` 弹窗逐项 ✔/✘ |

关键决策：MENUBAR 自启项 `KeepAlive=false`（手动退出不被拉起，RunAtLoad 登录启动）；对 KeepAlive 服务"停止"必须 bootout（kill 会被 launchd 复活）；**TCC 规避**——经 LaunchServices（Finder/open）启动的 GUI app 无 `~/Documents` 读权限（`PermissionError: pyvenv.cfg`），故 `.app` 仅是"确保 launchd 代理运行"的启动器（plist 内容构建时硬编码、运行时不读项目文件），菜单栏进程一律由 launchd 拉起（双击 .app = 安装/唤醒代理，同时注册开机自启）。

## 3. 配置参考（config.json 全字段语义）

| 键 | 默认值 | 说明 |
|---|---|---|
| `lm_studio.base_url` | `http://localhost:1234/v1` | LM Studio OpenAI 兼容端点 |
| `lm_studio.chat_model` | `qwen2.5-7b-instruct` | 必须与 LM Studio 已加载模型的标识**完全一致**（模型页可复制），否则 404 |
| `lm_studio.temperature / max_tokens / timeout_seconds` | 0.3 / 4096 / 300 | 归档生成参数；长文输入偶发超时可调大 timeout 或减小 limits |
| `lm_studio.structured_output` | `true` | json_schema 结构化输出；LM Studio 不支持报 300/400 时代码自动降级为提示词模式 |
| `inbox_folder_name` | `待处理笔记` | 各 Vault 收件箱目录名（守护进程自动创建） |
| `context_file` / `ai_context_max_chars` / `tree_depth` | `ai_context.md` / 6000 / 2 | 注入 LLM 的仓库上下文：规则+历史索引文件、截断上限、目录树层数 |
| `obsidian.wake_enabled` | `true` | 归档完成后用 `obsidian://open` 唤醒 |
| `vision.language_preference` | `[zh-Hans, en-US]` | Vision OCR 语言提示，乱码时调整 |
| `whisper.backend` | `auto` | `mlx`（Metal 优先）/ `openai` / `auto`（先 mlx 失败回退 openai-whisper） |
| `whisper.mlx_model` / `openai_model` / `language` | whisper-large-v3-turbo / small / zh | 两种后端的模型与源语言 |
| `processing.debounce_seconds` / `quiet_seconds` | 8 / 3 | 双闸防抖：事件观察窗 + 收件箱静默期 |
| `processing.attachment_wait_timeout` | 30 | 等待引用附件到齐的最长秒数（超时则继续处理现有部分） |
| `processing.attachments_subfolder` | `""`（空=与笔记同目录） | 附件落盘子目录名 |
| `processing.allow_new_folder` / `max_folder_depth` / `fallback_folder` | true / 2 / 未分类 | LLM 建目录的权限边界：是否允许新建、最大深度、非法/超深时兜底目录 |
| `limits.raw_note_max_chars` / `attachment_max_chars` | 30000 / 12000 | 注入 LLM 前截断阈值，防上下文爆炸 |
| `rag.enabled` | `true` | 模块 B 总开关 |
| `rag.embedding_model_path` / `embedding_device` | `models/bge-small-zh-v1.5` / `mps` | 相对 config.json 的本地模型目录；device 不可用时自动回退 cpu |
| `rag.chroma_dir` / `collection_name` | `data/chroma` / `smartvault` | 向量库持久化位置与集合名 |
| `rag.chunk_size` / `chunk_overlap` / `top_k` | 500 / 80 / 4 | 分块与检索参数；**改动后必须全量 rebuild 才生效** |
| `rag.rescan_seconds` | 300 | 后台增量扫描周期 |
| `rag.exclude_folders` | `.obsidian` `.trash` `待处理笔记` | 索引排除目录 |
| `api.host` / `api.port` | `127.0.0.1` / 8788 | 只绑本机；改端口须同步改 launchd plist 并重装 |
| `vaults[]` | — | `name` 必须与 Obsidian 内仓库名一致（唤醒 URI 依赖）；`path` 为绝对路径 |
| `log_dir` | `logs` | 日志目录（自动轮转） |

⚠️ 敏感性：`config.json` 含个人绝对路径与仓库名，**已列入 .gitignore 永不入库**；仓库中提交的是 `config.example.json` 模板（路径占位 `YOUR_NAME`）。

## 4. 关键流程时序

### 4.1 摄入管线（`run_pipeline`，模块 A 核心）
1. 读草稿（剥离已有 frontmatter）→ `find_attachment_refs` 提取引用 → `resolve_attachment` 在收件箱定位（含无扩展名补全）
2. 等待全部引用附件就位（`attachment_wait_timeout` 兜底放行）
3. 逐个 `parse_attachment` 按类型提取文本（超过 `attachment_max_chars` 截断）
4. 组装 Prompt：SYSTEM_PROMPT + 目录树 + ai_context（截断后）+ 草稿 + 附件文本
5. `LLMClient` 结构化输出 → `parse_llm_json` 容错解析出五字段 Strict JSON
6. `choose_target_dir` 校验/创建目录 → `unique_path` 确定终稿唯一路径
7. 附件移动（目标重名自动加序号 + `rewrite_links` 同步改写正文引用）→ 写终稿（YAML frontmatter）→ 删除草稿
8. `ai_context.md` 追加一行历史索引 → `obsidian://open?vault=…&file=…` 唤醒

### 4.2 增量索引（`VaultIndexer.sync`，模块 B 后台线程）
mtime+size 未变 → 直接跳过（O(1)）→ 变了才算 md5 复核 → 确实变化则重新分块并按文档 ID 先删后插 → 文件已删除则同步删向量。`rebuild()` 清空 collection 全量重建。

### 4.3 `/ask` 请求生命周期
查询加 bge 指令前缀 → 嵌入 → Top-K 余弦检索（按来源路径去重）→ 拼 RAG 提示词（上下文+问题+引用要求）→ LM Studio 生成。SSE 模式事件序：`sources`（先推引用）→ `message`×N（正文增量）→ `done`。

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

## 6. 测试与发布流程

```bash
# 日常验证（零三方依赖，任何机器可跑）
python3 -m unittest discover -s tests          # 22 项纯函数单测
python3 -m py_compile ingest_daemon.py rag_api.py scripts/build_index.py

# 联调冒烟
bash scripts/start_all.sh                      # 前台同跑 RAG + 守护进程
python ingest_daemon.py --once 测试.md --vault 工作事务   # 单篇全管线验证
```

发布 checklist（每次对外提交前）：
1. 单测全绿 + `--once` 带图草稿冒烟走通全管线
2. 更新本文档第 0 节版本表与受影响小节（README 同步）
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
| 草稿一直不被处理 | 双闸防抖未满足 / 附件未到齐 | 查 `logs/ingest_daemon.log`；用 `--once` 单篇复现 |
| 归档成功但 Obsidian 未弹出 | `vaults[].name` ≠ Obsidian 仓库名 | 改为完全一致的名称 |
| OCR 结果乱码 | 语言偏好不匹配 | 调 `vision.language_preference` |
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
7. 300s LLM 超时对 7B 模型 + 30k 字符输入偶发紧张，可调 `timeout_seconds` 或减小 `limits.*`

## 9. 升级路线（Roadmap，按价值排序）

- **短期**：检索重排（本地 bge-reranker，top20→top4）；`/ask` 支持按 vault 过滤；笔记反链注入上下文
- **中期**：图像语义检索（CLIP 嵌入附件）；多模型路由（超长附件走大上下文模型）；摄入管线断点续跑状态
- **长期**：MCP 服务器封装（让任意 AI 客户端查询 SmartVault）；浏览器剪藏直达收件箱；Obsidian 插件化问答面板

> 维护者自勉：动架构级改动前先跑第 6 节 checklist，保证 main 分支任何时刻可用。


