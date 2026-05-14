# 10 - 技术栈与选型原理

## 完整技术栈

### 后端 (Python 3.11+)

| 类别 | 技术 | 选型理由 |
|------|------|----------|
| **异步运行时** | asyncio | Python 原生异步，零额外依赖，适合 I/O 密集型 Agent 场景 |
| **Web 框架** | aiohttp | 轻量异步 HTTP，仅用于 API/gateway，不需 Django/Flask 的重量 |
| **CLI 框架** | Typer + Rich | 类型提示驱动 CLI，Rich 提供 ANSI/Markdown 终端渲染 |
| **数据校验** | Pydantic v2 | Rust 核心高性能，BaseSettings 支持 env 覆盖，alias 支持 camelCase |
| **LLM SDK** | anthropic + openai | 原生 SDK 保证 API 兼容性和最新特性支持 |
| **实时通信** | websockets | 纯 Python 异步 WebSocket，用于 WebUI 和 gateway |
| **聊天平台** | python-telegram-bot, discord.py, slack_sdk, lark-oapi 等 | 各平台官方或社区成熟的 Python SDK |
| **MCP 协议** | mcp | Anthropic 官方 MCP SDK，支持 stdio/SSE/HTTP 传输 |
| **消息序列化** | msgpack | 比 JSON 更紧凑的二进制序列化，用于内部消息传递 |
| **模板引擎** | Jinja2 | 系统提示和输出模板渲染 |
| **文件解析** | pypdf, python-docx, openpyxl, python-pptx | 支持 Agent 读写常见文档格式 |
| **Git 操作** | dulwich | 纯 Python Git 实现，无需系统 Git |
| **搜索** | ddgs (DuckDuckGo) | 免费无 API Key 的网页搜索 |
| **日志** | loguru | 比标准库 logging 更简洁的 API，结构化日志支持 |
| **Prompt 交互** | prompt-toolkit + questionary | 终端历史记录、自动补全、交互式问答 |
| **沙箱** | bubblewrap | Linux 容器级命令隔离 |
| **自动调优引擎** | LangGraph + Optuna + scikit-optimize + ChromaDB + SQLAlchemy | 6 Agent 协作的数据库参数自动调优（LLM 探索 + Bayesian 优化） |

### 前端 (TypeScript)

| 类别 | 技术 | 选型理由 |
|------|------|----------|
| **UI 框架** | React 18 | 生态最大，社区最活跃，适合 SPA |
| **构建工具** | Vite 5 | 极快 HMR，原生 ESM，零配置 TypeScript |
| **样式** | Tailwind CSS 3 | 原子化 CSS，无需维护独立样式文件 |
| **UI 原语** | Radix UI | 无样式但可访问的 UI 原语，自定义样式灵活 |
| **国际化** | i18next | 成熟稳定的 React 国际化方案 |
| **Markdown** | react-markdown + KaTeX | GFM 表格 + LaTeX 数学公式渲染 |
| **测试** | Vitest + happy-dom | Vite 原生测试框架，比 jsdom 更轻量 |
| **WebSocket** | 原生 WebSocket | 无额外依赖，自建多路复用协议 |

### 部署

| 类别 | 技术 | 选型理由 |
|------|------|----------|
| **容器化** | Docker + docker-compose | 标准化部署，支持 gateway/api/cli 三种服务模式 |
| **包管理** | uv (Astral) | Rust 实现，比 pip 快 10-100 倍 |
| **构建系统** | Hatchling | Python 官方推荐的现代构建后端 |
| **TypeScript 桥接** | Node.js + ws + @whiskeysockets/baileys | WhatsApp 无官方 Python SDK，JS 生态更成熟 |

### LLM 自动调优子系统

| 类别 | 技术 | 选型理由 |
|------|------|----------|
| **工作流引擎** | LangGraph | 有向图状态机，天然适合多 Agent 协作流水线 |
| **贝叶斯优化** | scikit-optimize (GP+EI) + Optuna (TPE) | 根据参数数量自动选择后端（≤30 GP+EI / >30 TPE） |
| **向量知识库** | ChromaDB | 轻量嵌入式向量库，存储 Redis/MySQL 专家知识 |
| **实验追踪** | SQLAlchemy async + SQLite/Alembic | ORM 持久化实验历史，支持迁移 |
| **环境管理** | Docker SDK for Python | 自动 provision/销毁测试容器 |
| **多 Agent LLM** | Anthropic + DeepSeek + OpenAI（共享限流/断路器） | 多 provider 确保 LLM 可用性 |

## 核心设计权衡

### 1. 消息总线 vs 直接调用

**选择**: 基于 asyncio.Queue 的消息总线

**理由**: Channel 和 AgentLoop 完全解耦。Channel 崩溃不影响 Agent 核心，Agent 升级不影响 Channel。支持一对多 fan-out（同一 chat_id 推送到多个 WebSocket 客户端）。

### 2. 状态机 vs 协程式

**选择**: 显式 TurnState 有限状态机

**理由**: 每个状态的进入/退出条件明确，便于调试、监控和审计。每个状态附加 `StateTraceEntry` 记录耗时和事件，出问题时快速定位。协程式代码虽简洁但控制流隐式，难以追踪。

### 3. OpenAI 兼容格式 vs 各 Provider 原生格式

**选择**: 内部统一使用 OpenAI 消息格式，Provider 各自转换

**理由**: OpenAI function-calling 格式是行业事实标准，工具 Schema、消息角色、流式协议都以此为基础。减少内部格式转换层数。

### 4. JSONL vs SQLite

**选择**: JSONL 文件持久化

**理由**: 会话数据是追加为主的顺序写入，JSONL 天然适合。无需 ORM，文件可直接用文本编辑器查看和修复。原子写入简单（写 tmp + os.replace）。

### 5. 单进程 vs 微服务

**选择**: 单进程异步架构

**理由**: Agent 是 I/O 密集型（LLM API 调用 + 聊天平台消息），asyncio 单进程即可充分利用 CPU。多进程增加部署复杂度和状态同步开销，对个人 Agent 场景无必要。

### 6. Python 3.11+ 下限

**理由**: `asyncio.TaskGroup` (3.11), `Self` type (3.11), TOML 标准库支持 (3.11)。这些特性显著简化了异步任务管理和类型标注。

## 插件扩展机制

三种扩展方式：

1. **Entry Points**: `nanobot.channels` 和 `nanobot.tools` 组，pip 安装即可注册
2. **包扫描**: `pkgutil.iter_modules` 自动发现内置模块
3. **MCP 协议**: 外部工具服务器通过标准协议接入

## 安全设计

- **SSRF 防护**: `security/network.py` 网络出口过滤 + IP 白名单
- **工作区沙箱**: `restrict_to_workspace` 限制文件读写范围
- **Shell 沙箱**: bubblewrap 容器隔离 + deny/allow 模式策略
- **Token 管理**: 短期 token (nbwt_*)，HMAC 签名媒体 URL
- **路径遍历防护**: 所有文件操作验证路径在工作区内
