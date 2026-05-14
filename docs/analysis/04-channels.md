# 04 - 多渠道系统

## 设计理念

通过统一的 `BaseChannel` 抽象，将 16 个聊天平台的差异封装在适配器层，Agent 核心只看到标准化的消息事件。

## 支持平台

| Channel | 库/SDK | 传输方式 |
|---------|--------|----------|
| Telegram | python-telegram-bot | 长轮询 |
| Discord | discord.py | WebSocket |
| Slack | slack_sdk | Socket Mode (WebSocket) |
| Feishu (飞书) | lark-oapi | WebSocket + HTTP |
| DingTalk (钉钉) | dingtalk-stream | WebSocket Stream |
| WeChat (微信) | 自定义 qrcode + 协议 | 长连接 |
| WeCom (企业微信) | wecom-aibot-sdk | Webhook |
| QQ | qq-botpy | WebSocket |
| Matrix | matrix-nio | 联合协议 |
| MSTeams | 自定义 | HTTP Webhook |
| Email | SMTP/IMAP | 邮件协议 |
| WhatsApp | @whiskeysockets/baileys (Node.js 桥接) | WebSocket |
| WebSocket | websockets | WebSocket (WebUI) |
| Mochat | 自定义 | HTTP |
| 内置 CLI | prompt-toolkit | 终端 I/O |

## BaseChannel 抽象

```python
class BaseChannel(ABC):
    name: str                        # 渠道标识
    display_name: str                # 显示名称

    async def start(self) -> None: ...   # 启动长连接
    async def stop(self) -> None: ...    # 优雅关闭
    async def send(self, msg: OutboundMessage) -> None: ...     # 发送消息
    async def send_delta(self, chat_id, delta, metadata) -> None: ...  # 流式增量
    async def login(self, force=False) -> bool: ...             # 交互式认证
```

## 消息总线 (MessageBus)

基于两个 `asyncio.Queue` 的极简发布/订阅：

```
Channel ──publish_inbound()──→ [inbound Queue] ──consume_inbound()──→ AgentLoop
AgentLoop ──publish_outbound()──→ [outbound Queue] ──consume_outbound()──→ ChannelManager
```

### 事件类型

**InboundMessage**: `channel`, `sender_id`, `chat_id`, `content`, `media`, `metadata`, `session_key_override`

**OutboundMessage**: `channel`, `chat_id`, `content`, `reply_to`, `media`, `buttons`

### 元数据信号

除了聊天消息，总线还承载以下元数据信号：
- `_stream_delta` / `_stream_end`: 流式文本增量
- `_progress`: 进度指示器（typing indicator, emoji 反应）
- `_tool_hint`: Agent 执行的中间步骤
- `_retry_wait`: Provider 重试延迟通知
- `_runtime_model_updated`: 模型切换广播（推送给 WebUI）
- `_session_updated` / `_turn_end`: 会话生命周期事件

## ChannelManager — 渠道生命周期管理

### 启动流程

1. `discover_all()` 扫描 `nanobot/channels/` 包和 entry points
2. 读取各渠道配置，实例化已启用的渠道
3. `start_all()` 为每个渠道创建异步任务
4. 启动出站调度器 `_dispatch_outbound()`

### 出站调度

`_dispatch_outbound()` 实现：
- **流式增量合并**: 连续的同 (channel, chat_id) 的 delta 消息合并为单次 API 调用
- **重复抑制**: 通过 SHA-1 指纹检测同 origin_message_id 的重复回复
- **指数退避重试**: 1s → 2s → 4s，可配置最大重试次数

## 渠道发现

两层发现机制：

1. **内置渠道**: `pkgutil.iter_modules()` 扫描 `nanobot.channels` 包
2. **外部插件**: `importlib.metadata.entry_points(group="nanobot.channels")`

内置渠道优先于同名外部插件。

## WhatsApp 桥接 (bridge/)

独立的 Node.js 进程，使用 `@whiskeysockets/baileys` 实现 WhatsApp Web 协议：

```
nanobot (Python) ──WebSocket──→ BridgeServer (Node.js) ──Baileys──→ WhatsApp
```

- 绑定 127.0.0.1，需要 token 握手认证
- 拒绝浏览器 Origin 头（防 XSS）
- 5 秒握手超时
- 支持 QR 码扫码登录
