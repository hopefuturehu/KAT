# 09 - 配置系统

## 设计原则

- **类型安全**: Pydantic v2 `BaseSettings` 全量校验
- **双命名兼容**: JSON 文件 camelCase，Python 代码 snake_case，通过 `AliasGenerator` 自动转换
- **环境变量覆盖**: `NANOBOT_` 前缀 + `__` 分隔嵌套字段
- **向后兼容**: `_migrate_config()` 自动迁移旧配置结构

## Config 模型

```python
class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")

    agents: AgentsConfig
    channels: ChannelsConfig
    providers: ProvidersConfig
    api: ApiConfig
    gateway: GatewayConfig
    tools: ToolsConfig
    model_presets: dict[str, ModelPresetConfig]
```

### AgentDefaults — 核心行为参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| model | "claude-sonnet-4-6" | 默认模型 |
| max_tokens | 4096 | 单次生成最大 token |
| context_window | 200000 | 上下文窗口大小 |
| temperature | 0.7 | 生成温度 |
| max_tool_iterations | 50 | 最大工具调用轮次 |
| max_concurrent_subagents | 3 | 最大并发子 Agent |
| session_ttl_seconds | 86400 | 会话过期时间 |
| consolidation_ratio | 0.5 | 上下文窗口压缩触发比例 |

## Provider 配置路由

`_match_provider(model, preset)` 的多级路由：

1. 显式 provider 前缀 (`anthropic/claude-opus-4-7`)
2. 模型名关键字匹配 (ProviderSpec.keywords)
3. 本地 provider (匹配 `api_base` 关键字)
4. 首个可用非 OAuth provider

```python
# 示例：自动路由 gpt-5 到 OpenAI provider
config.resolve_preset("gpt-5")
# → ModelPresetConfig(provider="openai", model="gpt-5", ...)
```

## Provider 注册表设计

`PROVIDERS` 元组中的顺序即优先级：
- Gateway 优先（可路由任何模型）
- 标准 provider 按关键字匹配
- 本地 provider 兜底
- OAuth provider 不会被自动选中（需要显式配置）

## 工具配置

```python
class ToolsConfig(Base):
    web: WebToolConfig        # 搜索引擎选择
    exec: ExecToolConfig      # Shell 安全限制
    my: MyToolConfig          # 自省工具权限
    image_generation: ImageGenConfig  # 图片生成 provider
    restrict_to_workspace: bool = True
    mcp_servers: dict[str, MCPServerConfig]  # MCP 服务器
    ssrf_whitelist: list[str]  # SSRF 白名单
```

每个工具通过 `config_key` + `enabled(ctx)` 实现配置驱动的功能开关。

## 配置加载流程

```
config.json (用户文件)
    │
    ▼
load_config()
    ├── 读取 JSON
    ├── _migrate_config() 旧结构迁移
    ├── resolve_config_env_vars() ${VAR} 替换
    ├── Config.model_validate()
    └── 返回 Config 实例
```

## 运行时路径

```python
get_data_dir()        → ~/.nanobot/           # 数据目录
get_workspace_path()  → ~/nanobot_workspace/  # Agent 工作区
get_media_dir()       → ~/nanobot_workspace/media/  # 媒体存储
get_cron_dir()        → ~/nanobot_workspace/cron/   # Cron 任务
get_logs_dir()        → ~/.nanobot/logs/      # 日志
```

## 环境变量解析

配置值支持 `${VAR}` 引用，递归解析：

```json
{
  "apiKey": "${OPENAI_API_KEY}",
  "nested": {"url": "${API_BASE}/v1"}
}
```

未设置的环境变量会抛出 `ValueError`。
