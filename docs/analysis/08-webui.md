# 08 - WebUI 与网关

## 技术栈

| 层 | 技术 |
|----|------|
| 框架 | React 18 + TypeScript |
| 构建 | Vite 5 |
| 样式 | Tailwind CSS 3 + Radix UI 原语 |
| 国际化 | i18next，9 种语言 (en, zh-CN, zh-TW, fr, ja, ko, es, vi, id) |
| Markdown | react-markdown + remark-gfm + rehype-katex |
| 语法高亮 | react-syntax-highlighter |
| 测试 | Vitest + happy-dom + Testing Library |

## 通信架构

### 双通道模式

```
┌─────────────────────────────────────┐
│              WebUI (React SPA)       │
│                                      │
│  REST API ────→ /api/sessions        │
│  (控制操作)     /api/settings        │
│                /api/commands         │
│                /webui/bootstrap      │
│                                      │
│  WebSocket ───→ ws://host:port/ws    │
│  (实时聊天)     JSON 帧多路复用       │
└─────────────────────────────────────┘
```

### WebSocket 协议 (JSON 帧)

**客户端 → 服务端**:
```json
{"type": "new_chat"}
{"type": "attach", "chat_id": "..."}
{"type": "message", "chat_id": "...", "content": "...",
 "media": [{"data_url": "data:image/...;base64,...", "name": "..."}]}
```

**服务端 → 客户端**:
```json
{"event": "delta", "chat_id": "...", "text": "..."}       // 流式 token
{"event": "stream_end", "chat_id": "..."}                  // 流段结束
{"event": "message", "chat_id": "...", "text": "..."}      // 完整消息
{"event": "turn_end", "chat_id": "..."}                    // 轮次完成
{"event": "runtime_model_updated", "model_name": "..."}    // 模型切换
```

### 流式处理

`useNanobotStream` hook 管理每个 chat_id 的流式状态：
- 累积 `delta` 事件到缓冲区
- 在收到 `turn_end` 时确认流结束
- 防抖计时器在工具调用间隙保持流式指示器

### 重连机制

指数退避: 500ms → 1s → 2s → 4s → 15s (上限)
重连后自动重新订阅所有已知 chat_id

## WebSocketChannel (网关)

单个 Python 类实现多协议服务器：

1. **WebSocket 升级**: 多 chat_id 多路复用，fan-out 订阅
2. **REST API**: 会话/设置/命令的 CRUD
3. **静态 SPA 服务**: 内置 webui/dist/，SPA history fallback
4. **Token 签发**: `/webui/bootstrap` 返回短期 token
5. **媒体签名**: HMAC-SHA256 签名 URL，防路径遍历

### 认证

- 动态: `/webui/bootstrap` 返回 `nbwt_*` token（单次 WS 握手 + 多次 REST）
- 静态: 配置文件中的固定 token
- Token 有 TTL 过期

### 媒体限制

服务端强制: 最多 4 张图片（各 8MB），最多 1 个视频（20MB）

## 图片编码 Worker

`imageEncode.worker.ts` 在 Web Worker 中缩放/编码图片，确保不超过 6MB 预算，避免阻塞主线程。

## 上传流程

1. 用户粘贴/拖拽图片
2. `useAttachedImages` → 提交到 ImageEncode Worker
3. Worker resize + 编码 → 返回 data URL
4. 随消息通过 WebSocket 发送
5. 服务端接收 base64 → 保存到媒体目录 → 生成签名 URL
6. WebUI 通过 `/api/media/<sig>/<payload>` 获取
