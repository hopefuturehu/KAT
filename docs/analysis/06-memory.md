# 06 - 记忆系统

## 三层记忆架构

```
┌─────────────────────────────────────────────┐
│              Layer 1: Session History        │
│  SessionManager → JSONL 文件持久化            │
│  (完整对话记录，token 预算内保留)              │
├─────────────────────────────────────────────┤
│              Layer 2: Consolidation          │
│  Consolidator → LLM 摘要 → history.jsonl    │
│  (超预算消息压缩为摘要存档)                    │
├─────────────────────────────────────────────┤
│              Layer 3: Long-term Memory       │
│  Dream → 两阶段处理 → MEMORY.md/SOUL.md     │
│  (周期性提炼为结构化长期记忆)                  │
└─────────────────────────────────────────────┘
```

## MemoryStore — 文件 I/O 层

管理四类持久化文件：

| 文件 | 用途 | 格式 |
|------|------|------|
| `MEMORY.md` | 长期记忆事实库 | Markdown |
| `memory/history.jsonl` | 压缩后的历史摘要 | JSONL |
| `SOUL.md` | Agent 人格定义 | Markdown |
| `USER.md` | 用户信息 | Markdown |

### 关键特性

- **原子写入**: 写入 `.tmp` 文件 → `os.replace()` 到正式文件名
- **fsync 持久化**: 支持 `fsync=True` 确保写入到磁盘（rclone VFS/NFS/FUSE 兼容）
- **Git 集成**: 通过 GitStore 自动 commit SOUL.md/USER.md/MEMORY.md 的变更
- **Cursor 管理**: `.cursor` 和 `.dream_cursor` 跟踪 Consolidator 和 Dream 的处理进度

## Consolidator — Token 预算驱动的压缩

当会话历史接近 LLM 上下文窗口限制时，Consolidator 自动触发：

1. `estimate_session_prompt_tokens()` 估算完整提示的 token 数
2. 若超过 `consolidation_ratio * context_window`，开始压缩
3. `pick_consolidation_boundary()` 找到满足移除量的自然用户轮次边界
4. `archive()` 调用 LLM 将旧消息摘要为一段文本
5. 摘要追加到 `history.jsonl`
6. 下次构建系统提示时，摘要以 `[Archived Context Summary]` 注入

### 回放溢出

当 `AgentRunner._snip_history()` 会丢弃的消息，Consolidator 将其存档为摘要，确保 LLM 仍可获得这些信息的概览。

### 按会话锁

使用 `weakref.WeakValueDictionary` 管理按 session_key 的合并锁，会话过期后锁自动 GC。

## Dream — 两阶段记忆处理

周期性地（通过 cron 触发）将 history.jsonl 中的对话记录提炼为结构化长期记忆。

### 第一阶段：分析

纯 LLM 对话（无工具），分析未处理的 history.jsonl 条目：
- 提取用户偏好、习惯、重要事实
- 对比当前 MEMORY.md 内容
- 生成建议的增删改操作

### 第二阶段：编辑

使用独立的 `AgentRunner` 实例，仅授予 `read_file`, `edit_file`, `write_file` 工具：
- 对 MEMORY.md, SOUL.md, USER.md 进行增量编辑
- Git blame 提供每行年龄信息，帮助 LLM 判断哪些行是过时的
- 避免全文件替换，保持编辑的精确性

### 种子知识库

`_llm_tuner/src/knowledge/seed/` 包含 MySQL、OS、Redis 等领域知识种子，用于初始化记忆系统。
