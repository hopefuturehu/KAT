# 05 - 工具系统

## 设计理念

工具系统是 Agent 的能力边界。每个工具都是独立的 `Tool` 子类，通过注册表统一管理，对外暴露为 OpenAI 兼容的 function-calling schema。

## Tool 抽象基类

```python
class Tool(ABC):
    name: str              # 工具名称
    description: str       # 自然语言描述（写入 system prompt）
    parameters: dict       # JSON Schema 参数定义

    async def execute(self, **kwargs) -> str: ...  # 执行入口

    read_only: bool = False       # 纯读取（无副作用）
    concurrency_safe: bool = True # 可并行执行
    exclusive: bool = False       # 必须独占执行
```

### 参数定义方式

使用 `@tool_parameters` 装饰器声明式定义参数 Schema：

```python
class ReadFileTool(Tool):
    @tool_parameters(StringSchema(description="文件路径"))
    async def execute(self, path): ...
```

底层使用 `ObjectSchema`, `StringSchema`, `IntegerSchema` 等 Schema 类构建 JSON Schema。

## 工具注册表 (ToolRegistry)

```
ToolRegistry
  ├── register(tool) / unregister(name)
  ├── get_definitions() → list[OpenAI function schema]
  ├── prepare_call(name, params) → (Tool, error?)
  └── execute(name, params) → str (result or error)
```

### 关键特性

- **Schema 缓存**: `get_definitions()` 输出稳定排序（内置工具 > MCP 工具），结果缓存到下次注册变更
- **参数转换**: `cast_params()` 自动完成类型强制（字符串 "42" → 整数 42）
- **错误即返回值**: 工具异常被捕获并转为错误字符串返回给 LLM，附带有 `[Analyze the error...]` 提示
- **并发策略**: `concurrency_safe` 工具并行执行，`exclusive` 工具串行执行

## 工具发现

`ToolLoader` 使用与 Channel 相同的二层发现：

1. `pkgutil.iter_modules` 扫描 `nanobot.agent.tools` 包
2. `importlib.metadata.entry_points(group="nanobot.tools")` 发现外部插件

通过 `enabled(ctx)` 类方法和 `config_key` 实现配置驱动的工具开关。

## 内置工具清单

| 工具 | 文件 | 功能 | 特性 |
|------|------|------|------|
| exec | shell.py | 执行 Shell 命令 | 沙箱隔离、超时、工作区限制、安全模式 |
| read_file | filesystem.py | 读取文件 | PDF/docx/xlsx/pptx 解析、图片渲染 |
| write_file | filesystem.py | 写入文件 | 原子写入 |
| edit_file | filesystem.py | 编辑文件 | 模糊匹配（空白/引号/缩进容忍） |
| list_dir | filesystem.py | 列出目录 | 递归、过滤 |
| web_search | web.py | 网络搜索 | DuckDuckGo/Brave/Tavily/SearXNG/Kagi |
| web_fetch | web.py | 网页抓取 | Jina Reader + readability-lxml，SSRF 防护 |
| glob | search.py | 文件模式匹配 | 二进制检测、分页 |
| grep | search.py | 正则搜索 | 类型/glob 过滤、分页 |
| spawn | spawn.py | 生成子 Agent | 需要 SubagentManager |
| my | self.py | 自省与配置 | 模型切换、API key 管理、运行状态读写 |
| ask_user | ask.py | 暂停询问用户 | 通过异常中断执行流 |
| message | message.py | 主动消息发送 | 跨 Channel 投递、文件附件、按钮 |
| image_generation | image_generation.py | 图片生成 | OpenRouter/AIHubMix |
| cron | cron.py | 定时任务 | 时区校验、ISO 时间解析 |
| notebook_edit | notebook.py | Jupyter 编辑 | replace/insert/delete 模式 |
| mcp_* | mcp.py | MCP 协议工具 | stdio/SSE/streamable HTTP 传输 |

## MCP 集成

`mcp.py` 实现了完整的 MCP (Model Context Protocol) 客户端：

- 支持 3 种传输: stdio（子进程）、SSE、Streamable HTTP
- `MCPToolWrapper` 将 MCP 工具适配为 `Tool` 子类
- `MCPResourceWrapper` 和 `MCPPromptWrapper` 将非工具能力也包装为工具
- Schema 规范化: 处理 nullable union 等 MCP 特有模式

## 上下文传递

工具通过两个机制获取运行时上下文：

1. **ToolContext** (构造时注入): config, workspace, bus, subagent_manager 等
2. **RequestContext** (请求时注入): channel, chat_id, session_key 等，通过 `ContextAware` 协议的 `set_context()` 方法

`FileStates` 使用 `ContextVar` 实现 per-task 隔离（读取去重、读前编辑警告），确保并发子 Agent 之间互不干扰。
