# CLAUDE.md — SmartVault AI 协作规则

> 任何 AI 助手（Cline / Claude Code / Copilot / OpenCode 等）在本仓库工作前必读。
> 本约定由用户于 2026-08-30 设立，长期有效；修改本文件须经用户同意。

## 铁律：文档同步是「完成」的一部分（无需用户提醒）

任何功能、行为、配置、修复变更，在提交前必须**同轮**更新以下文档，缺一不可：

1. `PROJECT_DOC.md`（开发视角权威文档）
   - 第 0 节版本记录表：新增一行（版本号｜日期｜变更摘要｜git tag），版本号按语义递增（功能=次版本，修复/文档=修订号）
   - 受影响小节同步：模块地图（第 2 节）、配置参考（第 3 节）、流程时序（第 4 节）、How-to（第 5 节）、故障排查（第 7 节）——改了什么就更新什么，包括单测计数等易漂移细节
2. `README.md`（用户视角）：功能一览、使用说明、注意事项同步更新
3. **文档不同步 = 变更未完成**。不要等用户提醒，也不要在回复里说「文档可稍后更新」。

## 提交与发布

- 提交信息用 `git commit -F <消息文件>`（多行 message 写 /tmp 临时文件再引用；**禁止 heredoc 内联在命令数组中**——已三次踩坑导致后续命令链未执行）
- 发布：`git tag vX.Y.Z && git push origin main --tags`
- `config.json` 永不入 git（含个人路径，已在 .gitignore）；改配置默认值须三处一致：`config.json` + `config.example.json` + 代码内 `DEFAULT_CONFIG`

## 工程约定

- **测试收尾**：任何改动以 `.venv/bin/python -m unittest discover -s tests` 全绿收尾；新功能配单测（现有测试在 `tests/`，纯函数优先，真实 LLM 不进单测）
- **单文件架构**：三个主模块各自分层（`ingest_daemon.py` 摄入归档 / `rag_api.py` RAG 问答 / `menu_bar_app.py` 菜单栏），不轻易新建模块
- **代码风格**：中文注释与 docstring、中文日志、宽异常标注 `# noqa: BLE001`、遵循各文件既有分层
- **服务管理**：改代码后用 `launchctl kickstart -k gui/$(id -u)/com.user.aibrain[.rag|.menubar]` 重启对应服务并验证；Python 一律用 `.venv/bin/python`
- **E2E 验证**：涉及摄入/归档的改动，用真实草稿+附件投入收件箱走全管线（防抖 8s+3s+LLM ~13s 后查结果），验证完清理测试产物
- **数据安全红线**：永远不删用户的笔记/附件/仓库数据；AI 只处理收件箱内的文件；测试只在收件箱或临时目录进行
- **隐私承诺**：零云端依赖是产品底线——新增依赖不得引入网络上报；`rag_api.py` 顶部的离线闸门环境变量必须保持在 import 之前

## 项目速览

- 定位：macOS 本地离线「Obsidian 笔记自动归档 + 本地 RAG 问答」（LM Studio qwen3-14b + bge-small-zh-v1.5 嵌入 + ChromaDB），中文用户
- 架构与配置细节：读 `PROJECT_DOC.md`（权威）；快速上手：读 `README.md`
- 核心行为基线：**原文保留模式为默认**（v1.4.0 起，`processing.content_rewrite: false`）——LLM 只产元数据，永不改写用户原文；附件转录以折叠引用块附加文末
