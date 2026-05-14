# 03 - LLM 提供者系统

## 设计目标

支持 20+ LLM 后端（Anthropic, OpenAI, Azure, Bedrock, GitHub Copilot, DeepSeek, Gemini 等），通过统一的 `LLMProvider` 抽象屏蔽差异。

## 架构

```
                    ┌──────────────────┐
                    │   LLMProvider    │  ← 抽象基类
                    │   (ABC)          │
                    └────────┬─────────┘
           ┌─────────────────┼─────────────────────┐
           │                 │                     │
   ┌───────▼──────┐  ┌──────▼──────┐  ┌───────────▼──────────┐
   │ Anthropic    │  │ OpenAICompat│  │ Bedrock / Azure /     │
   │ Provider     │  │ Provider    │  │ Codex / Copilot       │
   │ (原生 SDK)    │  │ (OpenAI SDK)│  │                       │
   └──────────────┘  └─────────────┘  └───────────────────────┘
```

## 核心抽象

### LLMProvider (base.py)

```python
class LLMProvider(ABC):
    async def chat(self, messages, tools, settings) -> LLMResponse: ...
    async def chat_stream(self, messages, tools, settings) -> AsyncIterator[str]: ...
    def get_default_model(self) -> str: ...
```

### LLMResponse — 统一响应格式

所有 provider 返回标准化的 `LLMResponse` 数据类：
- `content`: 文本响应
- `tool_calls`: 工具调用列表（`ToolCallRequest`）
- `finish_reason`: 完成原因
- `usage`: token 用量统计
- `reasoning_content`: 推理链内容
- `error_*`: 结构化错误信息（状态码、类型、是否应重试）

## 注册表模式

`registry.py` 中使用声明式元组定义所有 provider：

```python
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(name="anthropic", keywords=["claude"], backend="anthropic", ...),
    ProviderSpec(name="openai", keywords=["gpt-", "o1", "o3", "o4"], backend="openai_compat", ...),
    ProviderSpec(name="deepseek", keywords=["deepseek"], backend="openai_compat", ...),
    # ... 20+ providers
)
```

**ProviderSpec 字段**:
- `keywords`: 模型名称子串匹配（用于自动路由）
- `backend`: 指定使用哪个 Python 类
- `supports_prompt_caching`: 是否支持 Anthropic 风格的 prompt 缓存
- `thinking_style`: 推理模式配置
- `model_overrides`: 模型级参数覆盖

**路由优先级**: 显式 provider 前缀 > 关键字匹配 > 本地 provider > 首个可用非 OAuth provider

## OpenAICompatProvider — 最复杂的 Provider

统一处理所有 OpenAI 兼容 API（OpenAI, DeepSeek, Gemini, Zhipu, DashScope, Moonshot, Ollama 等）：

- **Responses API 断路器**: 对推理模型优先尝试 OpenAI Responses API，失败 3 次后熔断，5 分钟后探测恢复
- **消息规范化**: tool_call ID 标准化为 9 字符字母数字，DeepSeek 内容强制转字符串，角色交替
- **Prompt 缓存**: 对 Anthropic/OpenRouter 格式注入缓存标记
- **推理链处理**: DeepSeek 的 `reasoning_content` 回填到消息历史
- **流式空闲超时**: 默认 90s（可通过 `NANOBOT_STREAM_IDLE_TIMEOUT_S` 配置）

## AnthropicProvider

- 将 OpenAI 格式消息转换为 Anthropic Messages API 格式
- 支持扩展思考（extended thinking），budget 映射：low=1024, medium=4096, high=8192+
- 自动对流式超长响应进行降级处理

## BedrockProvider

- 基于 AWS boto3 `converse` / `converse_stream` API
- 通过 `asyncio.to_thread()` 将同步 boto3 调用异步化
- 处理推理内容块（文本和脱敏块）

## 重试策略

所有 provider 共享 `LLMProvider._run_with_retry()`：

| 模式 | 最大尝试 | 退避策略 | 适用场景 |
|------|----------|----------|----------|
| standard | 3 次 | 1s → 2s → 4s | 一般瞬时错误 |
| persistent | 无限（同错误上限 10 次） | 指数退避，最大 60s | 429 限流等可恢复错误 |

重试条件：HTTP 408/409/429/5xx、超时、连接错误、以及 provider 自定义的可重试错误标记。

## 懒加载

`__init__.py` 使用 `__getattr__` 实现懒加载，避免启动时导入所有 provider 模块：

```python
def __getattr__(name):
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        globals()[name] = getattr(module, name)
        return globals()[name]
```
