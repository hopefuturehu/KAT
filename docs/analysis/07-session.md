# 07 - 会话管理

## Session — 会话数据模型

```python
@dataclass
class Session:
    key: str                    # "channel:chat_id"
    messages: list[dict]        # 有序消息列表
    last_consolidated: int      # 已被 Dream 处理的消息索引
```

每条消息包含: `role`, `content`, `timestamp`, 以及可选的 `tool_calls`, `tool_call_id`, `reasoning_content`, `media`。

## SessionManager — 会话生命周期

### 持久化策略

- **格式**: JSONL（每行一条 JSON 消息 + 首行元数据）
- **存储位置**: `{workspace}/sessions/{session_key}.jsonl`
- **原子写入**: 写 `.jsonl.tmp` → `os.replace()` → 目录 fsync
- **延迟加载**: 仅在首次访问时从磁盘加载
- **内存缓存**: `_cache` 字典避免重复 I/O

### 关键方法

| 方法 | 功能 |
|------|------|
| `get_or_create(key)` | 从缓存获取或从磁盘加载 |
| `save(session, fsync=False)` | 原子写入磁盘 |
| `flush_all()` | 优雅关闭时 fsync 所有缓存会话 |
| `delete_session(key)` | 从磁盘和缓存删除 |
| `list_sessions()` | 读取所有会话的元数据（仅首行） |

### 容错

- **修复模式**: 遇到损坏的 JSONL 行时，跳过该行继续加载
- **旧版迁移**: 自动将 `~/.nanobot/sessions/` 中的旧会话迁移到工作区

## 上下文窗口管理

### get_history() — 核心方法

```python
def get_history(self, max_messages=0, max_tokens=0, include_timestamps=True):
    # 1. 切片未合并的消息
    # 2. 对齐到用户轮次边界
    # 3. 删除前部的孤儿工具结果
    # 4. 清理助手回复中的系统标记
    # 5. 为有图片的用户消息合成 [image: path] 标记
    # 6. 按 token 预算从尾部裁剪
```

### 合法边界对齐

`find_legal_message_start()` 确保历史记录不以孤儿工具结果开头（缺少对应的助手 tool_call 消息），这对 LLM API 的角色交替要求至关重要。

### 硬上限

`enforce_file_cap(limit=2000, on_archive=None)` 在消息数超过限制时丢弃旧消息，可选地通过 `on_archive` 回调传递给 Dream 进行长期记忆保存。

## 自动压缩集成

每个轮次的 `_state_build` 阶段，AgentLoop 调用 `Consolidator.maybe_consolidate_by_tokens()`：

```
估算提示 token → 超过预算? → 选择压缩边界 → LLM 摘要 → 写入 history.jsonl
```

压缩边界始终对齐到用户轮次边界，确保不会在对话中截断。

## 上下文构建

`ContextBuilder.build_messages()` 组装完整提示：

1. 系统提示（身份 + 引导文件 + 长期记忆 + 活跃技能）
2. 历史消息（经 `get_history()` 处理）
3. 当前用户消息（前置运行时上下文块: 时间、渠道、发送者 ID）

运行时上下文块在持久化到会话历史前被剥离（`_save_turn`），确保历史保留干净对话内容。
