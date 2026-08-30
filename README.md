# SmartVault / 智能仓库

macOS（Apple Silicon）上 100% 本地运行的 Obsidian AI 知识管理系统。
由两个模块组成，推理全部交给本机 **LM Studio**（`http://localhost:1234/v1`），零外部网络请求。

| 模块 | 文件 | 职责 |
|---|---|---|
| A. 摄入与归档守护进程 | `ingest_daemon.py` | watchdog 监听多 Vault 的「待处理笔记」收件箱，多模态解析附件，LLM 提炼为 Strict JSON，自动归类落盘并唤醒 Obsidian |
| B. 对话式知识查询服务 | `rag_api.py` | FastAPI 本地 RAG：中文嵌入 + ChromaDB 向量检索 + LM Studio 生成；自带浏览器聊天界面（`GET /ui`），`POST /ask` 返回带引用来源的回答（支持 SSE 流式）；另暴露 OpenAI 兼容 `/v1/chat/completions`，可接 BMO Chatbot 等 Obsidian 插件 |

```
SmartVault/
├── PROJECT_DOC.md                   # 维护文档：架构地图 / 配置语义 / 二开指南 / 排障表
├── config.json                      # 全局配置（含个人路径，已 gitignore 不上传）
├── config.example.json              # 配置模板（入 git；复制为 config.json 后修改）
├── ingest_daemon.py                 # 模块 A：摄入与归档守护进程
├── rag_api.py                       # 模块 B：FastAPI 本地 RAG 服务
├── static/
│   └── chat.html                    # 浏览器聊天界面（GET /ui，零依赖离线单页）
├── requirements.txt                 # Python 依赖清单
├── launchd/
│   ├── com.user.aibrain.plist       # launchd 模板：模块 A（登录自启 + 崩溃拉起）
│   └── com.user.aibrain.rag.plist   # launchd 模板：模块 B
├── scripts/
│   ├── install_launchd.sh           # 一键安装两个 launchd 服务
│   ├── uninstall_launchd.sh         # 一键卸载
│   ├── start_all.sh                 # 前台手动联调（Ctrl+C 一起退出）
│   └── build_index.py               # 手动索引维护（增量 / 全量重建）
├── tests/                           # 单元测试：纯函数 / 最近错误扫描 / LLM 客户端 / RAG 流式解码 / OpenAI 兼容层 / 归档保真性
├── models/                          # 本地嵌入模型（bge-small-zh-v1.5，手动下载）
├── data/                            # ChromaDB 持久化 + 索引状态
└── logs/                            # 运行日志（自动轮转）
```

## 一、环境准备（一次性）

### 1. 系统依赖

```bash
brew install python@3.12 ffmpeg
# 说明：torch / mlx / chromadb 对 Python 3.12 的 wheel 支持最完整；
#       ffmpeg 供 whisper 解码音视频；ocrmac 由 macOS 系统自带 Vision 框架驱动。
```

### 2. Python 虚拟环境与依赖

```bash
cd /Users/tiger/Documents/Sync_Disk/SoftWare/GitHub/SmartVault
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. LM Studio

- 下载并加载对话模型（如 `Qwen2.5-7B-Instruct`，24GB 内存推荐 Q4_K_M 量化）
- 开启本地服务器（Developer → Start Server，默认端口 1234）
- `config.json` 中 `lm_studio.chat_model` 需与已加载模型名一致

### 4. 本地嵌入模型（一次性联网下载，之后完全离线）

```bash
pip install "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-small-zh-v1.5 --local-dir models/bge-small-zh-v1.5
```

### 5. whisper 模型（可选，处理音视频才需要）

```bash
# MLX(Metal) 后端：首次使用会自动缓存 mlx-community/whisper-large-v3-turbo
# 想严格离线，可先一次性预热，或在 config.json 的 whisper.mlx_model 里填本地模型目录
python -c "import mlx_whisper; mlx_whisper.transcribe('任意.mp3', path_or_hf_repo='mlx-community/whisper-large-v3-turbo', language='zh')"
```

### 6. 修改 config.json

```bash
cp config.example.json config.json   # 真实配置不入 git（含个人路径）
```

把 `vaults` 数组改成你的真实仓库（`name` 必须等于 Obsidian 里的**仓库名**，唤醒 URI 依赖它）：

```json
"vaults": [
  { "name": "工作事务", "path": "/Users/tiger/Documents/ObsidianVaults/工作事务" },
  { "name": "IT与电脑知识", "path": "/Users/tiger/Documents/ObsidianVaults/IT与电脑知识" },
  { "name": "文学与阅读", "path": "/Users/tiger/Documents/ObsidianVaults/文学与阅读" }
]
```

守护进程会自动在每个仓库根下创建「待处理笔记」收件箱（不存在时）。

## 二、日常使用

### 模块 A：把东西丢进收件箱即可

把 Markdown 草稿（可连同附件）拖入任意仓库的 `待处理笔记/`，守护进程会：

1. 等文件写入稳定（防抖）并等引用附件到齐（`![[xxx.png]]` / `[t](xxx.pdf)`）
2. 按类型解析附件：图像→Vision OCR；音视频→whisper(Metal)；PDF→PyMuPDF；Office→python-docx/openpyxl/python-pptx；iWork→QuickLook/Preview.pdf
3. 注入「仓库目录树 + ai_context.md（规则与历史索引）」让 LLM 生成 Strict JSON
   （`target_folder / new_filename / summary / tags / optimized_content`，全简体中文）
4. 校验净化（拒绝越权目录、非法文件名），移动附件、写入带 YAML 属性的终稿、备份草稿到 `.smartvault/backup/`（保留最近 100 份）后删除
5. 追加历史索引到 `ai_context.md`，并用 `obsidian://open?...` 唤醒 Obsidian 打开新笔记

**原文不可变（v1.4.0 起默认）**：归档笔记的正文**逐字保留草稿原文**，LLM 只负责提炼元数据（目录归类、文件名、摘要、标签、frontmatter），附件的 OCR/语音转录以折叠引用块附加在文末并标注「以原附件为准」——AI 幻觉再严重也改不动你的原文（v1.3.1 事故教训：LLM「整理」19941 字符长文曾丢失 94% 内容并编造数字）。如希望短文（语音转录、随手记）获得 AI 排版润色，可开启 `processing.content_rewrite: true`：此时不超过 `rewrite_max_chars`（默认 6000 字符）的草稿交由 LLM 整理（仍有逐字保真约束 + 草稿备份兜底），长文一律保留原文。

调试命令：

```bash
source .venv/bin/activate
python ingest_daemon.py --check                 # 环境自检
python ingest_daemon.py --once 测试.md --vault 工作事务   # 处理单篇（调试）
python ingest_daemon.py --scan                  # 批量处理积压草稿
python ingest_daemon.py                         # 前台常驻
```

### 模块 B：知识问答（聊天界面 + API）

服务启动后，**日常使用直接打开浏览器聊天界面**（推荐入口）：

```
http://127.0.0.1:8788/ui
```

输入问题即获得**流式回答**与**参考来源**（笔记标题 + 路径 + 相关度），页面顶部实时显示索引健康状态。零外部依赖、完全离线。另有交互式 API 调试台 `http://127.0.0.1:8788/docs`。

```bash
python rag_api.py        # 前台手动启动 http://127.0.0.1:8788，后台自动增量索引
```

| 接口 | 说明 |
|---|---|
| `GET /ui` | **浏览器聊天界面（日常使用推荐入口）**：流式回答 + 来源引用，零依赖离线单页 |
| `POST /ask` | `{"query": "...", "top_k": 4, "stream": false}` → `{"answer", "sources": [{path, title, distance}]}` |
| `POST /ask`（流式） | `"stream": true` → SSE：先 `event: sources`，再 `event: message` 增量，最后 `event: done` |
| `GET /v1/models` | OpenAI 兼容模型列表（固定暴露 `smartvault-rag`） |
| `POST /v1/chat/completions` | **OpenAI 兼容入口**：标准 `messages` 格式（支持 `stream` SSE 流式），供 BMO Chatbot 等任意 OpenAI 协议客户端直连 |
| `POST /reindex` | `{"rebuild": false}` 增量同步；`{"rebuild": true}` 全量重建 |
| `GET /status` | 索引文件数 / 分块数 / 最近同步时间 |
| `GET /health` | LM Studio 与嵌入模型健康状态 |

Obsidian 插件（Templater / 自定义脚本）中调用示例：

```javascript
const res = await fetch("http://127.0.0.1:8788/ask", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ query: "SmartVault 的隐私原则是什么？" }),
});
const { answer, sources } = await res.json();
```

#### 在 Obsidian BMO Chatbot 插件中接入（OpenAI 兼容）

服务自带 OpenAI 兼容适配层，BMO Chatbot 无需任何改造即可直连。插件设置 → **REST API Connection**：

| 设置项 | 填写值 |
|---|---|
| **API Key** | 留空（本地服务不鉴权） |
| **REST API URL** | `http://127.0.0.1:8788/v1` |
| **Enable Stream** | 建议开启（流式逐字输出） |

填完 URL 后插件会自动拉取模型列表，在聊天窗顶部的模型下拉框选择 **smartvault-rag**（REST API Models 分组）即可开始提问。

行为说明：每条提问都会先对你的全部笔记做向量检索，再带着命中片段生成，回答末尾自动附「参考来源」笔记路径；多轮对话携带最近 6 轮历史；BMO 的人设 system 提示词在 RAG 模式下被忽略（以「仅依据笔记上下文回答」约束为准）。也可改填 LM Studio 直连地址 `http://localhost:1234/v1`，但那样**没有笔记检索**，模型看不到库内容。

### 开机自启（launchd）

```bash
bash scripts/install_launchd.sh      # 安装并启动两个服务（幂等可重复执行）
launchctl list | grep aibrain        # 查看状态
tail -f logs/*.log                   # 看日志
bash scripts/uninstall_launchd.sh    # 卸载
```

`launchd/*.plist` 是模板（含 `@PROJECT_DIR@`、`@PYTHON@` 占位符），安装脚本会自动替换为真实路径并写入 `~/Library/LaunchAgents/`。

### 菜单栏控制台（推荐：图形化启停与诊断）

常驻 macOS 顶部状态栏的小工具，覆盖日常启停/开机自启/卸载与故障分析，无需敲命令：

```bash
source .venv/bin/activate
pip install rumps                    # 菜单栏 UI 框架（已收录 requirements.txt）
bash scripts/build_menubar_app.sh    # 生成 SmartVaultMenuBar.app（ad-hoc 签名）
open SmartVaultMenuBar.app           # 安装/唤醒 launchd 代理 → 状态栏出现图标
```

> **双击 `.app` 即同时注册开机自启**（launchd 代理 `com.user.aibrain.menubar`）。
> 技术注解：经 Finder/`open` 启动的 GUI app 受 macOS TCC 限制、读不了 `~/Documents`，
> 因此 `.app` 只是一个"确保 launchd 代理运行"的启动器，真正的菜单栏进程由 launchd 拉起
> （与 ingest/rag 同机制，无权限问题）。

功能一览：

| 菜单项 | 说明 |
|---|---|
| 状态图标 `● ◐ ○ ⚠` | 全部运行 / 部分运行 / 全部停止 / 异常（崩溃循环或 RAG 未就绪），每 5 秒自动刷新 |
| 启动 / 停止 / 重启全部 | 一键 bootstrap/bootout 两个 launchd 服务（启动幂等：已在运行不会重启） |
| 摄入守护进程 / RAG 服务 子菜单 | 单独启停、重启、卸载（移除开机自启）、Terminal 实时日志 |
| 🧹 清理已删笔记残留（同步索引） | 增量同步 RAG：移除已删除笔记的向量块 + 剔除 ai_context.md 失效归档条目（后台每 5 分钟也会自动执行；删除/清理测试笔记后点它立即生效） |
| ♻️ 重建 RAG 索引（清空后重建） | 确认后清空整个向量库，再按当前仓库实际存在的笔记全量重建；清空测试库后点它让索引归零 |
| 🔍 综合健康检查 | config / Vault 路径 / 嵌入模型 / LM Studio 端口 / `/health` `/status` / 进程退出码 逐项 ✔/✘ |
| ⚠️ 最近错误分析 | 增量监测 `logs/*.log` 自上次检查以来新增的 ERROR / Traceback / 启动失败（连续重复自动去重；历史旧错误不重复告警） |
| 🖥 开机自启：菜单栏控制台 | 控制台自身的登录项开关（安装 `com.user.aibrain.menubar`） |
| 🧹 卸载全部 SmartVault 服务 | 停止并移除全部三个 launchd 登录项（代码与数据不受影响） |

说明：

- 图标 `⚠` 且 RAG 显示"运行中"：通常是嵌入模型仍在加载（点击"启动/重启"后 10–30 秒内出现属正常），等待片刻即可
- 首次点击"实时日志"会请求控制 Terminal 的自动化权限，请点允许
- 内置单例锁（`logs/.menubar.lock`）防止双开；菜单栏自启项 `KeepAlive=false`，手动退出后不会被强行拉起
- **刘海屏 MacBook 看不到 `●`**：图标可能被系统排入刘海遮挡区（存在但不可见）。⌘ 拖动图标到时钟左侧可固定位置；或重启菜单栏控制台（`launchctl kickstart -k gui/$(id -u)/com.user.aibrain.menubar`）触发重新布局。诊断信息见 `logs/menubar.stderr.log` 的 `[SmartVault][诊断]` 行

## 三、隐私与安全设计

- 模块 A/B 仅访问 `localhost`（LM Studio）与系统框架；无任何遥测
- 模块 B 启动即设置 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，禁用 HuggingFace 在线检查
- ChromaDB 以 `anonymized_telemetry=False` 运行，纯本地文件持久化
- API 只绑定 `127.0.0.1`；文件名/目录一律净化（拒绝 `..` 穿越、非法字符），LLM 输出不可直接越权写盘

## 四、注意事项与已知限制

1. **iWork 附件**：`.pages/.numbers/.key` 走 `QuickLook/Preview.pdf`，通常只含首页预览；需要全文请在 iWork 套件中导出 PDF 再拖入。
2. **同仓库 wikilink 重名**：附件改名仅发生在目标目录已有同名文件时（追加 ` 2` 序号），正文引用会同步改写。
3. **LM Studio 结构化输出**：优先使用 `response_format: json_schema`；旧版本不支持时自动回退纯提示词 + 鲁棒 JSON 解析（去围栏、截取花括号）。
4. **LM Studio 上下文窗口**：归档会把草稿全文 + 仓库上下文整体发给模型（2 万字符草稿 ≈ 1.2 万 tokens），模型需以足够 context length 加载——建议 `lms load <model> -c 32768 --parallel 1`；超限时日志会给出明确指引，而非无效重试。
5. **首次索引较慢**：几千篇笔记约需几分钟（MPS 嵌入 ~1-2k chunks/秒），之后全部增量。
6. **失败保护**：单篇草稿处理失败会保留在收件箱原处并记入日志，可用 `--scan` 或 `--once` 重试，绝不会静默丢稿。

## 五、深入阅读

- 架构图 / 模块地图 / 配置全字段语义 / 二次开发指南 / 故障排查 / 升级路线 → [PROJECT_DOC.md](PROJECT_DOC.md)

