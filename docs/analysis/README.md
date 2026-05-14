# nanobot 技术分析文档

> 轻量级开源 AI Agent 框架的完整技术流程与选型原理

## 目录

| 文档 | 内容 |
|------|------|
| [01-架构总览](01-architecture.md) | 高层架构、数据流、核心子系统关系 |
| [02-Agent 循环](02-agent-loop.md) | 核心状态机、AgentRunner、上下文治理 |
| [03-LLM 提供者系统](03-providers.md) | 多模型适配、工厂模式、重试策略 |
| [04-多渠道系统](04-channels.md) | 16 个聊天平台适配、消息总线、发布订阅 |
| [05-工具系统](05-tools.md) | 工具注册、Schema 定义、MCP 集成 |
| [06-记忆系统](06-memory.md) | MemoryStore、Dream 两阶段记忆、Consolidator |
| [07-会话管理](07-session.md) | 会话持久化、上下文压缩、token 预算 |
| [08-WebUI 与网关](08-webui.md) | React SPA、WebSocket 多路复用、媒体签名 |
| [09-配置系统](09-config.md) | Pydantic 配置、多 provider 路由、环境变量 |
| [10-技术栈与选型原理](10-tech-stack.md) | 完整技术栈、选型理由、设计权衡 |
| [11-简历简介](11-resume.md) | 可直接写在简历上的项目简介 |
| [12-LLM 自动调优](12-llm-tuner.md) | LangGraph 多 Agent 协作、Bayesian 优化、参数自动搜索 |
| [13-产品能力缺口评估](13-gap-analysis.md) | 调优引擎产品化缺失能力与优先级建议 |
