# 12 - LLM 自动调优引擎

## 概述

`_llm_tuner/` 是一个独立的 LangGraph 驱动的 LLM 参数自动调优子系统。它通过多 Agent 协作，自动搜索数据库/缓存系统的最优配置参数，实现性能优化。

## 架构图

```
User Intent (e.g., "tune Redis throughput")
    │
    ▼
TuningIntentRouter (nanobot/agent/tuning/router.py)
    │ 关键词意图识别 → 路由到调优流程
    ▼
TuningSessionManager (nanobot/agent/tuning/manager.py)
    │
    ├─ Phase 1: Intake (多轮对话收集需求)
    │   └─ AgentRunner → TuningRequirements
    │
    └─ Phase 2: Execution (LangGraph 工作流)
         │
         ▼
    ┌─────────────────────────────────────────┐
    │        LangGraph StateGraph              │
    │                                          │
    │  init → plan → safety → apply → bench   │
    │    ↑                ↓         ↓          │
    │    └── rollback ←──┴──── analyze         │
    │                          ↓               │
    │                       decide ──→ finalize│
    └─────────────────────────────────────────┘
```

## 多 Agent 协作系统

所有 Agent 继承自 `BaseAgent`，共享多 provider LLM 调用、令牌桶限流和断路器。

| Agent | 文件 | 职责 |
|-------|------|------|
| **Orchestrator** | `agents/orchestrator.py` | 中央控制器。每次试验后分析结果，决定下一步：继续调优 / 已收敛 / 达到上限 / 目标达成 / 回滚 |
| **Tuner** | `agents/tuner_agent.py` | 提出参数变更建议。查询知识库获取领域上下文，遵守单次变更上限、重启预算、blocklist 和风险级别约束 |
| **Analyzer** | `agents/analyzer_agent.py` | 解释基准测试结果，识别趋势（改善/稳定/下降）、瓶颈类型（CPU/内存/IO/网络/配置）、变更影响，推荐下一轮调优方向 |
| **Safety** | `agents/safety_agent.py` | 验证参数变更的安全性：检查 CRITICAL 风险、内存余量预算、重启预算、冲突/依赖规则、值域边界、连续回滚限制。可批准/拒绝/修改后批准 |
| **Advisor** | `agents/advisor_agent.py` | 当参数调优达到瓶颈时，推荐非参数替代方案：硬件升级、架构变更（分片/缓存）、查询优化、版本升级、负载分配 |
| **Benchmark** | `agents/benchmark_agent.py` | 规划基准测试方案，选择测试负载和参数 |

### 协作流程

```
Orchestrator 决定方向 → Tuner 提出变更 → Safety 验证
    → 应用配置 → 运行基准测试 → Analyzer 分析结果
    → Orchestrator 决定下一步 → 循环或结束
```

当目标达成或收敛时，Advisor 生成替代建议，系统进入终结阶段。

## LangGraph 工作流

### 9 个节点

| 节点 | 函数 | 职责 |
|------|------|------|
| `initialize` | `initialize_experiment()` | 设置开始时间、采集硬件规格、播种改进历史、持久化实验到 DB |
| `plan` | `plan_changes()` | 收敛/目标达成时调用 Advisor。否则调用 HybridTuner (LLM+Bayesian) 提出变更 |
| `safety_check` | `safety_gate()` | 将变更建议传给 SafetyAgent。拒绝时回到 planning |
| `apply_config` | `apply_configuration()` | 应用批准的变更到目标配置（直接或 Docker 模式）。处理重启和健康检查。失败时强制回滚 |
| `run_benchmark` | `execute_benchmark()` | 使用对应 runner (Redis/MySQL/Direct) 执行基准测试。支持稳定性模式 |
| `analyze` | `analyze_results()` | 计算相对基线的改进率、更新最佳配置、调用 AnalyzerAgent、持久化到 DB |
| `decide` | `make_decision()` | 检查退出条件。否则调用 OrchestratorAgent 决定下一步 |
| `rollback` | `rollback_config()` | 回滚到上一版配置，增加连续回滚计数 |
| `finalize` | `finalize_experiment()` | 生成 markdown 报告、更新实验状态、清理 Docker 环境 |

### 状态转换图

```
START → initialize → plan
plan → [safety_check] (有变更) 或 [decide] (无变更)
safety_check → [apply_config] (批准) 或 [plan] (拒绝)
apply_config → run_benchmark
run_benchmark → analyze
analyze → decide
decide → [plan] (继续) / [rollback] (回滚) / [finalize] (结束)
rollback → plan
finalize → END
```

工作流可选 SQLite 检查点 (`SqliteSaver`) 支持中断恢复。

## 混合优化策略

### HybridTuner: LLM 优先 + Bayesian 兜底

```
LLM TunerAgent (主要路径)
    │ 上下文感知、知识驱动
    │ 失败时（断路器打开/限流/API 错误）
    ▼
Bayesian 后端 (兜底路径)
    │ 数据驱动、从历史试验种子启动
```

### 后端自动选择

基于参数数量自动选择优化算法：

| 参数数量 | 后端 | 库 | 说明 |
|----------|------|-----|------|
| ≤ 30 | GP+EI (高斯过程 + 期望改进) | scikit-optimize | 低维最佳样本效率 |
| 31-100 | TPE (Tree-structured Parzen Estimator) | Optuna | 处理高维混合空间 |
| > 100 | TPE + 警告 | Optuna | 建议降维或多保真度优化 |

### GP+EI (`BayesianOptimizer`)

- `skopt.Optimizer` + `GP` 基础估计器 + `EI` 采集函数
- 通过 `tell()` 从历史数据种子启动（warm-start）
- 支持 `Integer`, `Real`, `Categorical` 参数空间

### TPE (`TPEOptimizer`)

- `optuna` + `TPESampler`
- 对范围 > 1000 的整数参数自动应用 log 尺度
- 种子启动: `optuna.trial.create_trial()` + `study.add_trial()`

## 领域知识库

### 种子知识 (ChromaDB)

21 条预置知识条目：

| 领域 | 条目数 | 文件 |
|------|--------|------|
| Redis | 9 条 | `knowledge/seed/redis_knowledge.py` |
| MySQL | 12 条 | `knowledge/seed/mysql_knowledge.py` |

`KnowledgeBaseRetriever` 使用 ChromaDB 向量相似度搜索 + 关键词回退，为 TunerAgent 和 AnalyzerAgent 提供领域上下文。

## 参数系统

### 参数 Schema

每个目标系统有 JSON Schema 定义（`parameters/schemas/`）：

- `redis_7.2.json`: 25 个参数
- `mysql_8.0.json`: MySQL 参数

每个参数 (`ParameterDefinition`) 包含：
- `name`, `category` (MEMORY, IO, PERSISTENCE, NETWORK, CONNECTIONS 等)
- `type` (string, integer, float, boolean, enum)
- `min_value`, `max_value`, `enum_values`, `default_value`
- `restart_required` (是否需要重启)
- `risk` (LOW, MEDIUM, HIGH, CRITICAL)
- `depends_on` / `conflicts_with` (依赖/冲突关系)

### ParameterManager

- 加载 JSON Schema → 解析配置文件 (redis.conf / my.cnf) → 校验 → 历史栈管理
- `get_tunable_parameters()`: 过滤可调参数 (按类别、风险级别、blocklist)
- `diff()` / `rollback()`: 变更追踪和回滚

### 配置解析器

- `RedisConfigParser`: 解析 `key value` 格式，处理重复 key
- `MySQLConfigParser`: 解析 `[section]` / `key=value` 格式

## 基准测试

### BenchmarkRunner

工厂方法 `for_system()` 返回对应 runner：
- `RedisBenchmarkRunner` → Redis 场景
- `SysbenchRunner` → MySQL 场景
- `CustomWorkloadRunner` → 自定义负载
- `DirectRedisRunner` → 直连模式（无 Docker），通过 `redis-cli CONFIG SET` 在线修改

### StabilityRunner

装饰器包装器，实现：
1. 缓存预热
2. 多次迭代（默认 3 次）
3. 按指标取中位数
4. 变异系数报告（stable < 5%, unstable 5-10%, volatile > 10%）

### 度量模型

`BenchmarkMetrics`: 各操作结果 (name, value, unit) + 聚合摘要 (total_rps, p99_latency_ms)
`SystemMetrics`: CPU%, 内存%, 磁盘 IOPS, 网络字节

## 实验追踪

基于 SQLAlchemy async ORM + SQLite (默认) 持久化：

| 模型 | 内容 |
|------|------|
| **Experiment** | 名称、目标系统、目标（JSON）、基线配置、硬件规格 |
| **Trial** | 试验编号、阶段、状态、配置快照、度量、改进率 |
| **ParameterChange** | 参数名、旧值/新值、理由、安全审批状态 |
| **BenchmarkRun** | 配置名称、runner 类型、原始输出、解析后的度量 |

CLI 命令 `history` 和 `show` 使用 `rich` 表格显示实验历史。

## 收敛检测

`ConvergenceDetector`: 滑动窗口（默认 5 次试验）内的绝对改进率全部低于阈值（默认 2%）时判定收敛。

## 中断与恢复

- Ctrl+C 中断时保存完整 `ExperimentState` 的 JSON 快照
- `--resume` 从快照重建状态
- 可选 LLM 生成的进度摘要（中断交接用）

## 部署模式

### 通用直连模式（默认）
`CustomDirectRunner` 使用用户提供的生命周期命令模板：
- `start_command`: 启动服务
- `run_command`: 运行基准测试
- `teardown_command`: 停止/清理
- `health_check_command`: 健康检查
- `restart_command`: 重启服务

命令模板支持 `{host}`, `{port}`, `{clients}`, `{requests}`, `{duration}`, `{tests}` 等占位符，运行时自动填入。

### 场景复原机制
- 每次试验前自动保存配置快照
- 回滚时恢复快照并执行 restart_command
- 实验结束时恢复 baseline 配置并执行 teardown_command
- 确保目标系统在调优前后状态一致

### YAML Profile 复用
`_llm_tuner/profiles/` 目录存放 YAML 格式的基准测试配置，用户可通过对话创建或复用已有 profile。已提供 `redis-basic.yaml`, `redis-custom-cmd.yaml`, `mysql-sysbench.yaml` 示例。

### Docker 模式（已废弃）
`TargetEnvironmentManager` 保留但标记为 DEPRECATED，不再作为默认路径。

## 集成到 Agent Loop

`nanobot/agent/tuning/` 四个文件实现集成：

| 文件 | 职责 |
|------|------|
| `schema.py` | 数据模型: `TuningPhase`, `TuningGoal`, `TuningRequirements`, `TuningSession` |
| `intake.py` | 多轮会话 intake agent。使用 `AgentRunner` 收集需求，产出 `TuningRequirements` JSON |
| `executor.py` | 桥接层: `TuningRequirements` → `ExperimentState` → 运行 LangGraph workflow.astream() → 格式化 markdown 报告 |
| `manager.py` | `TuningSessionManager`: 两阶段生命周期管理 (intake → execution 后台任务)，结果自动归档到 nanobot 记忆系统 |
| `router.py` | `TuningIntentRouter`: 关键词意图识别 (正则匹配 redis + tuning 关键词、中文关键词) → 路由到调优流程 |

### 记忆归档集成

调优完成后，`TuningSessionManager` 自动将结构化结果（最佳配置、指标、改进历史）写入 `MemoryStore.history.jsonl`。nanobot 的 Dream 两阶段记忆处理在下一个 cron 周期自动消费该条目，提炼为 `MEMORY.md` 中的调优知识。

### 弹性层集成

`ResilienceManager` 按 provider 隔离管理 `RateLimiter` 和 `CircuitBreaker`：
- Anthropic、DeepSeek、OpenAI 各持独立限流器和断路器
- 一个 provider 的故障不会影响其他 provider 的调用
- 断路器打开时工作流节点自动 fallback（plan → Bayesian、decide → 规则决策）

### 用户交互流程

```
用户: "帮我调优 Redis 吞吐量"
    │
    ▼
TuningIntentRouter 识别意图 → 进入 INTAKE 阶段
    │
Agent: "你的性能目标是什么？目标 Redis 地址？"
    │ 多轮对话
    ▼
收集完整 TuningRequirements → 后台启动 LangGraph 工作流
    │
    │ 9 节点循环执行 (plan → safety → apply → bench → analyze → decide → ...)
    ▼
生成调优报告 → 通过 MessageBus 推送给用户
```

## 关键设计决策

- **LLM + Bayesian 混合**: LLM 负责知识驱动的探索（上下文感知、利用领域知识），Bayesian 负责数据驱动的开发（样本高效、LLM 不可用时的回退）
- **双后端自动选择**: GP+EI 处理低维空间，TPE 处理高维混合空间，根据参数数量自动切换
- **Safety Agent 门控**: 所有参数变更必须通过安全检查，防止生产事故
- **ChromaDB 知识库**: 21 条专家知识种子，LLM 调优时提供领域指导
- **Skill 扩展**: 支持用户自定义 Python/YAML skill 挂载到 LangGraph 节点的 pre/post 钩子
