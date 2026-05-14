# 01 - 架构总览

## 高层架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                        External Platforms                         │
│  Telegram  Discord  Slack  Feishu  WhatsApp  WeChat  WebSocket   │
└──────────────┬───────────────────────────────────────────────────┘
               │  Inbound Messages
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                      MessageBus (asyncio.Queue)                   │
│                  publish_inbound() / consume_inbound()             │
│                  publish_outbound() / consume_outbound()           │
└──────┬───────────────────────────────────────────────────┬────────┘
       │ Inbound                                           │ Outbound
       ▼                                                   │
┌──────────────────┐                                       │
│   AgentLoop      │                                       │
│  ┌─────────────┐ │                                       │
│  │ TurnState   │ │                                       │
│  │ StateMachine│ │                                       │
│  └──────┬──────┘ │                                       │
│         │         │                                       │
│  ┌──────▼──────┐ │                                       │
│  │ AgentRunner │ │                                       │
│  │ LLM + Tools │ │                                       │
│  └──────┬──────┘ │                                       │
│         │         │                                       │
│  ┌──────▼──────┐ │                                       │
│  │  Session    │ │                                       │
│  │  Manager    │ │                                       │
│  └─────────────┘ │                                       │
│                   │                                       │
│  ┌─────────────┐ │                                       │
│  │ MemoryStore │ │                                       │
│  │ + Dream     │ │                                       │
│  └─────────────┘ │                                       │
└──────────────────┘                                       │
                                                           ▼
                                              ┌─────────────────────┐
                                              │   ChannelManager    │
                                              │   Outbound Dispatch  │
                                              └─────────────────────┘
```

## 核心数据流

1. **Channel** 从外部平台接收消息 → 封装为 `InboundMessage` → 发布到 `MessageBus.inbound`
2. **AgentLoop.run()** 消费 `inbound` 队列 → 按 session_key 分发到 `_process_message()`
3. **_process_message()** 驱动 `TurnState` 状态机: RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND
4. **AgentRunner.run()** 执行 LLM 对话循环：发送消息到 Provider → 接收 tool_calls → 执行工具 → 流式返回
5. 响应封装为 `OutboundMessage` → 发布到 `MessageBus.outbound`
6. **ChannelManager._dispatch_outbound()** 消费 `outbound` 队列 → 调用对应 Channel 的 `send()` / `send_delta()`

## 关键子系统

| 子系统 | 目录 | 职责 |
|--------|------|------|
| Agent Loop | `nanobot/agent/loop.py` | 核心编排引擎，状态机驱动 |
| Agent Runner | `nanobot/agent/runner.py` | LLM 对话循环，工具执行 |
| LLM Providers | `nanobot/providers/` | 多模型适配（Anthropic, OpenAI, Bedrock 等） |
| Channels | `nanobot/channels/` | 16 个聊天平台适配器 |
| Message Bus | `nanobot/bus/` | 基于 asyncio.Queue 的发布/订阅 |
| Tools | `nanobot/agent/tools/` | Agent 工具能力（shell, 文件, 搜索, MCP 等） |
| Memory | `nanobot/agent/memory.py` | 长期记忆与 Dream 两阶段处理 |
| Session | `nanobot/session/manager.py` | 会话持久化与上下文压缩 |
| Config | `nanobot/config/` | Pydantic 配置系统 |
| WebUI | `webui/` | React SPA 前端 |
| Bridge | `bridge/` | TypeScript 边车（WhatsApp 桥接） |
| CLI | `nanobot/cli/` | Typer 命令行工具 |
| Cron | `nanobot/cron/` | 定时任务调度 |
| Heartbeat | `nanobot/heartbeat/` | 周期性唤醒服务 |
| Security | `nanobot/security/` | 网络出口过滤（SSRF 防护） |
| LLM Tuner | `_llm_tuner/` + `nanobot/agent/tuning/` | LLM 驱动的数据库参数自动调优（LangGraph 多 Agent 协作 + Bayesian 优化 + ChromaDB 知识库） |

## 设计原则

- **解耦**: MessageBus 将 Channel 和 AgentLoop 完全解耦，彼此独立演进
- **状态机驱动**: AgentLoop 使用显式 TurnState 枚举，每个状态的进入/退出清晰可追踪
- **工厂 + 注册表**: Provider 和 Channel 都通过注册表 + 工厂模式创建，支持插件扩展
- **协议抽象**: 所有关键组件（LLMProvider, BaseChannel, Tool）都定义了抽象基类
- **配置驱动**: 通过 Pydantic 模型 + JSON 配置文件管理所有行为参数
