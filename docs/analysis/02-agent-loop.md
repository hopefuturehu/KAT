# 02 - Agent 循环

## AgentLoop — 核心编排引擎

`AgentLoop` (`nanobot/agent/loop.py`) 是整个系统的中央调度器。它管理消息消费、会话隔离、provider 切换、子 Agent 生成和生命周期钩子。

### 消息处理流程

```
InboundMessage 到达
    │
    ▼
AgentLoop.run() 主循环
    │
    ├─ 按 session_key 分组 → _pending_queues
    ├─ asyncio.Semaphore(3) 并发控制
    └─ asyncio.create_task(_dispatch(msg))
         │
         ▼
    _process_message() → TurnState 状态机
```

### TurnState 状态机

```
RESTORE ──→ COMPACT ──→ COMMAND ──→ BUILD ──→ RUN ──→ SAVE ──→ RESPOND ──→ DONE
   │                                                                           │
   └─────────────────────── 错误/中断时回退 ─────────────────────────────────┘
```

| 状态 | 处理函数 | 职责 |
|------|----------|------|
| RESTORE | `_state_restore` | 恢复运行时检查点（未完成的中断轮次） |
| COMPACT | `_state_compact` | Token 预算检查和会话压缩 |
| COMMAND | `_state_command` | 内置命令路由（/stop, /new, /model 等） |
| BUILD | `_state_build` | 构建系统提示 + 历史消息 + 当前用户消息 |
| RUN | `_state_run` | 委托给 AgentRunner 执行 LLM 对话 |
| SAVE | `_state_save` | 持久化会话历史，触发 Consolidator |
| RESPOND | `_state_respond` | 组装 OutboundMessage 并发布 |

### 并发控制

- **会话串行化**: 同一 session_key 的消息通过 `asyncio.Lock` 保证顺序处理
- **跨会话并发**: 全局 `asyncio.Semaphore` 限制同时处理的会话数（默认 3）
- **轮次中注入**: 在 LLM 轮次期间到达的新消息被放入 `_pending_queues`，由 AgentRunner 的 `injection_callback` 在每轮 LLM 调用后消费

### 运行时检查点

工具执行期间，`_emit_checkpoint` 将部分轮次状态（已完成的助手消息和待处理的工具调用）持久化到会话元数据中。如果轮次被 `/stop` 取消，下一轮次的 RESTORE 状态会恢复此检查点。

## AgentRunner — LLM 对话执行器

`AgentRunner` (`nanobot/agent/runner.py`) 是无领域耦合的纯工具型 Agent 执行循环。被 AgentLoop 和 Dream 共同使用。

### 执行循环

```python
for iteration in range(spec.max_iterations):
    # 1. 上下文治理
    messages = _drop_orphan_tool_results(messages)
    messages = _backfill_missing_tool_results(messages)
    messages = _microcompact(messages)
    messages = _apply_tool_result_budget(messages)
    messages = _snip_history(messages)

    # 2. 调用 LLM（支持流式 + 超时）
    response = await _request_model(messages, stream=True)

    # 3. 执行工具调用
    if response.tool_calls:
        results = await _execute_tools(response.tool_calls)

    # 4. 消费轮次中注入
    injections = await _try_drain_injections()

    # 5. 发送检查点
    await checkpoint_callback(messages, pending_tools)
```

### 上下文治理策略

| 策略 | 机制 | 触发条件 |
|------|------|----------|
| 孤儿工具结果清理 | `_drop_orphan_tool_results` | 每次 LLM 调用前 |
| 缺失结果回填 | `_backfill_missing_tool_results` | 每次 LLM 调用前 |
| 微压缩 | `_microcompact` | 旧工具结果超过 10 条时，压缩为单行摘要 |
| 工具结果预算 | `_apply_tool_result_budget` | 超过配置的字符数上限 |
| 历史修剪 | `_snip_history` | 估算 token 超过上下文窗口时，从旧消息开始丢弃 |

### 工具执行策略

- **并发安全工具**: 通过 `asyncio.gather` 并行执行（如 `read_file`, `grep`, `glob`）
- **排他工具**: 必须单独串行执行（如 `exec`, `write_file`）
- **ask_user**: 总是最后执行，执行后跳过余下所有批次
- **SSRF/工作区边界**: 错误分类处理，SSRF 违规给出不可绕过的指令

### AskUserInterrupt 机制

`ask_user` 工具不返回结果，而是抛出 `AskUserInterrupt` 异常，传播到执行链顶层，使 AgentRunner 返回 `stop_reason="ask_user"`。AgentLoop 将其渲染为带按钮的交互式提示。
