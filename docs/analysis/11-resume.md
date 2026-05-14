# 11 - 简历简介

以下内容可直接用于简历的项目经历描述。

---

## 项目名称

**nanobot — 开源多平台 AI Agent 框架**

## 一句话概述

从零设计并实现了开源 AI Agent 框架 nanobot，支持 16 个聊天平台、20+ LLM 后端，具备记忆系统、流式会话管理和插件扩展机制。

## 项目简介（适用简历）

**nanobot** | 核心开发者 | 2024-2025

- 设计并实现了一套完整的 **AI Agent 框架**，采用 Python asyncio 异步架构，核心包括 Agent 状态机、LLM Provider 抽象层、工具注册与执行系统和消息总线
- 构建了 **多模型适配层**，统一封装 Anthropic、OpenAI、Bedrock、Azure 等 20+ LLM 后端，支持自动路由、智能重试、流式传输和 prompt 缓存
- 实现了 **多渠道消息系统**，适配 Telegram、Discord、Slack、飞书、微信等 16 个聊天平台，通过发布/订阅消息总线实现 Channel 与 Agent 核心的解耦
- 设计了 **三层记忆架构**（会话历史 → LLM 摘要压缩 → 长期记忆提炼），包括 Token 预算感知的自动压缩器和两阶段增量记忆编辑系统
- 开发了基于 **React + TypeScript + Tailwind CSS** 的 WebUI，通过 WebSocket 多路复用协议实现实时流式对话，支持 9 种语言国际化
- 建立了 **插件扩展体系**，支持 Python Entry Points 和 MCP 协议，工具系统包含 Shell 执行、文件编辑、网络搜索、子 Agent 生成等 16 种能力
- 使用 Pydantic v2 构建了类型安全的配置系统，支持多 provider 路由、环境变量覆盖和旧版配置自动迁移
- 设计并实现了基于 **LangGraph 的 LLM 自动调优引擎**，通过 6 个专业 Agent 协作（Orchestrator/Tuner/Safety/Analyzer/Advisor/Benchmark），自动搜索数据库最优配置参数
- 构建了 **混合优化策略**（LLM 知识驱动 + Bayesian GP+EI/TPE 数据驱动），集成 ChromaDB 领域知识库（Redis/MySQL），支持收敛检测、安全门控和中断恢复

**技术栈**: Python 3.11+, asyncio, Pydantic v2, Typer, aiohttp, websockets, MCP 协议, LangGraph, Optuna, scikit-optimize, ChromaDB / React 18, TypeScript, Vite, Tailwind CSS, i18next

## 核心数据

- **16** 个聊天平台适配
- **20+** LLM 后端支持
- **100+** pytest 测试用例
- **9** 种 WebUI 语言
- **MIT** 开源许可

---

## 英文版 (English Resume Version)

**nanobot — Open-Source Multi-Platform AI Agent Framework**

- Designed and built a full-stack AI agent framework from scratch using **Python asyncio**, featuring a finite-state-machine-driven agent loop, LLM provider abstraction, tool registry, and message bus architecture
- Built a **multi-provider LLM layer** supporting 20+ backends (Anthropic, OpenAI, Bedrock, Azure, etc.) with automatic routing, exponential-backoff retry, streaming, and prompt caching
- Developed a **multi-channel messaging system** adapting 16 chat platforms (Telegram, Discord, Slack, WeChat, etc.) via a pub/sub message bus decoupling channels from the agent core
- Designed a **three-tier memory architecture** (session history → LLM summarization → long-term memory synthesis) with token-budget-aware auto-compaction and two-phase incremental memory editing
- Built a **React + TypeScript + Tailwind CSS** WebUI with real-time streaming via a multiplexed WebSocket protocol and 9-language i18n support
- Established an extensible **plugin system** supporting Python Entry Points and the MCP protocol; tool suite includes shell execution, file editing, web search, sub-agent spawning, and more
- Implemented a type-safe **Pydantic v2 configuration system** with multi-provider routing, environment variable overrides, and automatic config migration
- Designed a **LangGraph-powered LLM auto-tuning engine** featuring 6 collaborative agents (Orchestrator, Tuner, Safety, Analyzer, Advisor, Benchmark) that automatically searches for optimal database configuration parameters
- Built a **hybrid optimization strategy** combining LLM knowledge-driven exploration with Bayesian GP+EI/TPE data-driven exploitation, integrated with a ChromaDB domain knowledge base (Redis/MySQL), convergence detection, safety gating, and interrupt-resume support

**Tech Stack**: Python 3.11+, asyncio, Pydantic v2, Typer, aiohttp, websockets, MCP, LangGraph, Optuna, scikit-optimize, ChromaDB / React 18, TypeScript, Vite, Tailwind CSS, i18next
