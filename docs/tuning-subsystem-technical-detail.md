# Nanobot LLM 自动调优子系统 — 技术详解

## 概述

基于开源 AI Agent 框架 **nanobot** 二次开发的自动参数调优系统，面向 Redis 7.2 / MySQL 8.0 场景，实现**自然语言驱动的端到端性能调优**。用户通过聊天对话描述调优目标（如"帮我把 Redis 吞吐调到 8 万 QPS"），系统自动完成需求收集、参数优化搜索、安全审核、配置应用、基准测试和结果报告的全闭环流程。

---

## 1. 系统架构与闭环流程

### 1.1 整体架构

```
User: "帮我把 Redis 7.2 在 10.0.0.5:6379 上调优到 8 万 QPS"
        │
        ▼
┌─────────────────────────────────────────────┐
│  TuningIntentRouter (nanobot/agent/tuning/)  │
│  · 关键词检测路由 (intent.py)                  │
│  · 会话状态机: NEW → INTAKE → EXECUTION → DONE│
│  · 取消关键词终止, ERROR 态可重试              │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  TuningSessionManager（会话生命周期）          │
│                                              │
│  ┌─────────────────────────────────┐         │
│  │ 阶段 1: Intake（需求收集）       │         │
│  │ · LLM 多轮对话                  │         │
│  │ · 三层 JSON 提取 + 自修复        │         │
│  │ · 结果保存为可复用 YAML profile   │         │
│  └─────────────┬───────────────────┘         │
│                │ TuningRequirements           │
│                ▼                              │
│  ┌─────────────────────────────────┐         │
│  │ 阶段 2: Execution（自动调优）    │         │
│  │ · _llm_tuner LangGraph 工作流   │         │
│  │ · 每 trial 存 checkpoint 到磁盘  │         │
│  │ · 失败 → ERROR 态, 不丢数据     │         │
│  └─────────────────────────────────┘         │
└──────────────────┬──────────────────────────┘
                   │ 结果归档 MemoryStore
                   ▼
              📊 调优报告
```

### 1.2 关键文件索引

**nanobot 集成层** (`nanobot/agent/tuning/`):

| 组件 | 文件 | 职责 |
|------|------|------|
| 会话生命周期管理 | `manager.py` | 两阶段状态机 (intake → execution)，后台任务调度 |
| 需求收集 + JSON 提取 | `intake.py` | 多轮对话 Agent，三层 JSON 提取 + LLM 自修复 |
| 意图检测 & 路由 | `router.py` + `intent.py` | 关键词正则匹配，中英文双语支持 |
| 执行引擎桥接 | `executor.py` | `sys.path` 注入，LLM 凭据桥接，checkpoint 管理 |
| 数据模型 | `schema.py` | `TuningPhase`, `TuningRequirements`, `TuningSession` |
| Profile YAML 存储 | `profile_store.py` | 调优配置持久化与复用 |

**_llm_tuner 调优引擎** (`_llm_tuner/src/`):

| 组件 | 路径 |
|------|------|
| LangGraph 工作流定义 | `workflow/graph.py` |
| 工作流节点 (9 个) | `workflow/nodes/*.py` |
| 实验状态 (Pydantic) | `workflow/state.py` |
| 混合优化器 (LLM+BO) | `optimization/hybrid_tuner.py` |
| GP+EI 贝叶斯优化 | `optimization/bayesian.py` |
| TPE 优化器 | `optimization/tpe_optimizer.py` |
| 后端自动选择 (GP vs TPE) | `optimization/selector.py` |
| 参数元数据 & 解析 | `parameters/manager.py` + `schema.py` |
| 6 个 Agent | `agents/tuner_agent.py`, `safety_agent.py`, `analyzer_agent.py`, `orchestrator.py`, `advisor_agent.py`, `benchmark_agent.py` |
| Agent 基类 | `agents/base.py` |
| 提示词负载工具 | `agents/prompt_payload.py` |
| LLM 韧性工具 | `utils/llm_resilience.py` |
| 通用直连 Runner | `benchmark/custom_direct_runner.py` |

---

## 2. LangGraph 多 Agent 调优工作流

### 2.1 工作流状态机

基于 LangGraph `StateGraph` 构建 9 节点调优管线，每个节点对应一个独立的调优阶段：

```
START → initialize → plan → safety_check → apply_config → run_benchmark
                                                      ↓
                                                   analyze
                                                      ↓
                                                   decide ──→ plan (CONTINUE)
                                                      │
                                                      ├──→ finalize (CONVERGED / GOALS_MET / MAX_REACHED)
                                                      │
                                                      └──→ rollback → plan
```

**状态转换逻辑**（定义在 `workflow/graph.py`）：

- `plan → safety_check`：当 Tuner 提出有效变更时进入安全审核；无变更时跳过，直接进入 decide
- `safety_check → apply_config`：Safety Agent 返回 APPROVE 或 APPROVE_WITH_MODIFICATIONS 时放行；REJECT 时回到 plan 重新生成
- `decide → plan`：CONTINUE_TUNING 时继续循环
- `decide → finalize`：CONVERGED / GOALS_MET / MAX_TRIALS_REACHED / MAX_DURATION_REACHED 时终止
- `decide → rollback`：ROLLBACK 时回滚配置后回到 plan

### 2.2 9 个节点详解

| 节点 | 文件 | 核心逻辑 |
|------|------|---------|
| **initialize** | `nodes/initialize.py` | 设置启动时间、采集硬件规格、初始化改进历史、持久化实验到 SQLite |
| **plan** | `nodes/plan.py` | 调用 `HybridTuner` (LLM+BOS) 提出参数变更方案。收敛/目标达成时调用 Advisor。连续 5 次空提案 → 强制 Advisor |
| **safety_check** | `nodes/safety_check.py` | 先执行**规则短路审批**（全低风险 + 无重启 + 无依赖冲突 → 直接 APPROVE）。不满足则调用 SafetyAgent LLM 审核 |
| **apply_config** | `nodes/apply_config.py` | 序列化配置文本 → 快照备份 → 写入目标实例 → 如需重启则执行 restart_command + 健康检查。失败自动回滚 |
| **run_benchmark** | `nodes/run_benchmark.py` | 通过 `CustomDirectRunner` 执行 benchmark 命令，按配置的格式解析输出 |
| **analyze** | `nodes/analyze.py` | 调用 AnalyzerAgent 分析结果，计算相对最佳指标的改进率，更新 best_config/best_metrics，持久化到 DB |
| **decide** | `nodes/decide.py` | 先检查硬终止条件（连续回滚超限/超时/超次数/目标达成）→ 执行**规则短路决策** → LLM Orchestrator |
| **rollback** | `nodes/rollback.py` | 恢复上一快照配置，递增连续回滚计数 |
| **finalize** | `nodes/finalize.py` | 生成 Markdown 报告、更新实验状态、写入最终结果 |

### 2.3 节点 LLM 调用分析

在 9 个节点中，**4 个节点会调用 LLM**，其中 2 个具备规则短路机制：

| 节点 | 是否调用 LLM | 触发条件 | LLM 失败处理 |
|------|-------------|---------|-------------|
| **initialize** | 否 | — | — |
| **plan** | **是（必调）** | 每次 trial 调用 `HybridTuner`（内含 `TunerAgent` LLM）；收敛/目标达成时额外调用 `AdvisorAgent` LLM | LLM 异常 → 自动回退到 Bayesian 优化（GP+EI 或 TPE），从历史 trial 数据 seed |
| **safety_check** | **条件调用** | 规则短路条件：全部低风险 + 无重启 + 无依赖冲突 + 值域合法 → 直接 APPROVE，跳过 LLM；不满足才调用 `SafetyAgent` LLM | SafetyAgent 调用失败 → 默认 REJECT，拒绝本次变更 |
| **apply_config** | 否 | — | — |
| **run_benchmark** | 否 | — | — |
| **analyze** | **是（必调）** | 每次 trial 调用 `AnalyzerAgent` LLM | AnalyzerAgent 失败 → 使用默认分析值（trend=stable, bottleneck=unknown） |
| **decide** | **条件调用** | 先检查硬终止条件（回滚超限/超时/超次数/目标达成）→ 直接决策；再执行 `_rule_based_decision()` 短路判断（改善>阈值→CONTINUE，连续无改善→CONVERGED）；都不满足才调用 `OrchestratorAgent` LLM | Orchestrator 失败 → 回退到规则决策；连续 5 次失败 → 强制 CONVERGED 终止 |
| **rollback** | 否 | — | — |
| **finalize** | 否 | — | — |

**各节点 LLM 调用开销估算**（一次 30-trial 调优）：

| 节点 | 每次 trial 理论调用 | 短路跳过比例 | 实际调用次数 |
|------|-------------------|-------------|------------|
| plan | 1 次 (Tuner/HybridTuner) | ~0%（每次都调） | ~30 |
| safety_check | 1 次 (SafetyAgent) | ~40%（短路） | ~18 |
| analyze | 1 次 (AnalyzerAgent) | ~0%（每次都调） | ~30 |
| decide | 1 次 (OrchestratorAgent) | ~30%（短路） | ~21 |
| Advisor（收敛时） | 0-1 次 | — | ~1-2 |
| **合计** | **~120 次** | **~35% 平均短路率** | **~80 次** |

加上提示词缓存优化（系统提示静态化，缓存命中率 ~97%），综合输入 token 减少约 25-35%。

**HybridTuner 内部的 BO 回退 vs BO 延续阶段**：

| 机制 | 触发时机 | 作用 | 启用方式 |
|------|---------|------|---------|
| HybridTuner BO 回退 | 每个 trial 的 plan 阶段，TunerAgent LLM 调用失败时 | 单次参数提案的降级替代，从历史数据生成提案 | 自动，无需配置 |
| BO 延续阶段 (`--bo-trials`) | 整个 LLM 工作流结束后 | 利用全部历史数据做纯数学优化搜索，在 LLM 最优配置基础上继续迭代 | 独立 CLI 模式需显式指定 `--bo-trials N`；Nanobot 集成模式目前未支持 |

### 2.4 六 Agent 协作体系

所有 Agent 继承自 `BaseAgent`（`agents/base.py`），共享多 provider LLM 调用、令牌桶限流和断路器保护。

| Agent | 文件 | 核心职责 |
|-------|------|---------|
| **Orchestrator** | `agents/orchestrator.py` | 中央决策控制器。根据当前试验状态、历史改进趋势、目标达成情况决定下一步：CONTINUE_TUNING / CONVERGED / ROLLBACK / GOALS_MET |
| **Tuner** | `agents/tuner_agent.py` | 参数变更提案生成。查询 ChromaDB 知识库获取领域上下文，遵守单次变更上限、重启预算、blocklist 和风险级别约束 |
| **Safety** | `agents/safety_agent.py` | 参数变更安全验证。检查 CRITICAL 风险参数、内存余量预算、重启预算、冲突/依赖规则、值域边界、连续回滚限制。可批准/拒绝/修改后批准 |
| **Analyzer** | `agents/analyzer_agent.py` | 基准测试结果解释。识别趋势（改善/稳定/下降）、瓶颈类型（CPU/内存/IO/网络/配置）、变更影响评估，推荐下一轮调优方向 |
| **Advisor** | `agents/advisor_agent.py` | 当参数调优达到瓶颈时推荐非参数替代方案：硬件升级、架构变更（分片/缓存）、查询优化、版本升级、负载分配 |
| **Benchmark** | `agents/benchmark_agent.py` | 规划基准测试方案，选择测试负载和参数组合 |

### 2.5 Skill 扩展机制

工作流支持用户自定义 Skill 挂载到任意节点的 pre/post 钩子（`workflow/graph.py` `_wrap_with_skills()`）。Skill 可以是 Python 脚本或 YAML 声明文件，放置于 `skills/` 目录即可自动加载。每个 Skill 指定目标节点（node）和阶段（pre/post），在节点执行前后介入状态修改。

### 2.6 ExperimentState 字段详解

`ExperimentState`（`workflow/state.py`）是 LangGraph 工作流中所有节点共享的 Pydantic `BaseModel`，约 45 个字段。各节点通过读写这些字段完成数据传递与状态驱动。

#### 实验身份

| 字段 | 类型 | 说明 |
|------|------|------|
| `experiment_id` | `str` | 实验 UUID |
| `experiment_name` | `str` | 实验名称 |
| `target_system` | `str` | 目标系统 (`redis` / `mysql`) |
| `target_version` | `str` | 目标版本号 |

#### 优化目标

| 字段 | 类型 | 说明 |
|------|------|------|
| `goals` | `list[GoalSpec]` | 多目标规格列表。每个 `GoalSpec` 包含 `metric`, `operator` (`>=`, `<=`, `>`, `<`, `==`), `value`, `weight` |

#### 环境与连接

| 字段 | 类型 | 说明 |
|------|------|------|
| `container_id` | `str` | 容器/实例标识，直连模式为 `direct-{host}:{port}` |
| `direct_mode` | `bool` | 是否直连模式，默认 `True`（Docker 路径已废弃） |
| `direct_config_path` | `str` | 目标配置文件本地路径 |
| `direct_benchmark_cmd` | `str` | 基准测试命令 |
| `target_host` | `str` | 目标主机地址，默认 `127.0.0.1` |
| `target_port` | `str` | 目标端口，默认 `6379` |
| `target_credentials` | `str` | 目标凭据（密码） |
| `start_command` | `str` | 启动服务 Shell 命令模板 |
| `run_command` | `str` | 运行 benchmark Shell 命令模板，支持 `{host}`, `{port}`, `{clients}`, `{requests}`, `{duration}`, `{tests}` 占位符 |
| `teardown_command` | `str` | 停止/清理命令模板 |
| `health_check_command` | `str` | 健康检查命令模板 |
| `restart_command` | `str` | 重启服务命令模板 |
| `output_format` | `str` | 输出解析格式：`redis-benchmark-csv` / `sysbench` / `regex` / `raw` |
| `metric_regex` | `dict[str, str]` | 自定义正则提取规则（`regex` 格式时使用） |
| `benchmark_profile_path` | `str` | YAML benchmark profile 文件路径 |

#### 配置状态

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_config` | `dict[str, str]` | 当前生效的配置（每次 apply_config 后更新） |
| `baseline_config` | `dict[str, str]` | 基线配置（调优起点，不变） |
| `hardware_spec` | `dict[str, Any]` | 硬件规格快照（cpu_count, platform, python_version） |

#### 阶段与进度

| 字段 | 类型 | 说明 |
|------|------|------|
| `phase` | `ExperimentPhase` | 当前阶段枚举：`CREATED` → `INITIALIZING` → `PLANNING` → `SAFETY_CHECK` → `APPLYING_CONFIG` → `RUNNING_BENCHMARK` → `ANALYZING` → `DECIDING` → `ROLLING_BACK` / `ADVISING` → `COMPLETED` / `FAILED` / `PAUSED` |
| `trial_number` | `int` | 当前试验编号 |
| `max_trials` | `int` | 最大试验次数，默认 30 |
| `max_duration_hours` | `float` | 最大运行时长，默认 8.0 |
| `start_time` | `datetime \| None` | 实验启动时间 |
| `elapsed_hours` | `float` | 已运行时长（由 `update_elapsed_hours()` 刷新） |

#### 试验数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_trial` | `TrialResult \| None` | 当前活跃试验。`TrialResult` 包含 `trial_number`, `config`, `metrics`, `benchmark_results`, `parameter_changes`, `improvement_pct`, `status`, `analysis`, `created_at` |
| `trial_history` | `list[TrialResult]` | 历史试验列表，按顺序追加 |

#### 最优结果追踪

| 字段 | 类型 | 说明 |
|------|------|------|
| `best_metrics` | `dict[str, float]` | 历史最佳指标值 |
| `best_config` | `dict[str, str]` | 对应的最佳配置 |
| `best_trial_number` | `int` | 达到最佳结果的试验编号 |

#### 收敛控制

| 字段 | 类型 | 说明 |
|------|------|------|
| `convergence_window` | `int` | 收敛判定滑动窗口大小，默认 5 |
| `improvement_threshold_pct` | `float` | 改善阈值百分比，默认 2.0 |
| `improvement_history` | `list[float]` | 历次 trial 改进率序列 |

`has_converged()` 方法检查 `improvement_history` 最近 `convergence_window` 个值是否全部低于 `improvement_threshold_pct`。

`compute_improvement(new_metrics)` 实现多目标加权改善率计算，根据 goal operator 区分 "越高越好"（正向变化为改善）和 "越低越好"（负向变化为改善）。

#### 安全约束

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_changes_per_trial` | `int` | 单次试验最大变更参数数，默认 4 |
| `allow_restart` | `bool` | 是否允许重启，默认 `False` |
| `max_restart_changes` | `int` | 单次最大需重启的变更数，默认 2 |
| `max_risk_level` | `str` | 允许的最高风险级别 (`low` / `medium` / `high`)，默认 `medium` |
| `max_consecutive_rollbacks` | `int` | 连续回滚上限，默认 3 |
| `consecutive_rollbacks` | `int` | 当前连续回滚计数 |
| `memory_headroom_pct` | `int` | 内存预留百分比，默认 20 |
| `blocklist` | `list[str]` | 禁止修改的参数名列表 |
| `rollback_history` | `list[dict]` | 回滚历史，每项含 `trial` 和 `reason` |
| `safety_warnings` | `list[str]` | 安全告警累积日志 |

#### Agent 输出（节点间传递的核心数据）

这些字段是工作流节点间状态驱动和数据传递的关键载体：

| 字段 | 类型 | 产出节点 | 消费节点 | 说明 |
|------|------|---------|---------|------|
| `tuning_proposal` | `TuningProposal` | `plan` | `safety_check`, `apply_config` | 参数变更提案。包含 `changes` 列表（每项含 `parameter`, `proposed_value`, `rationale`, `expected_effect`, `risk`）、`overall_strategy`、`_source`（`llm` 或 `bayesian`） |
| `safety_verdict` | `SafetyVerdict` | `safety_check` | `apply_config`, 路由条件 | 审批结论。包含 `verdict`（`APPROVE` / `REJECT` / `APPROVE_WITH_MODIFICATIONS`）、`overall_risk_level`、`warnings` 列表、`suggested_modifications` 列表、`requires_human_approval` |
| `analysis_result` | `AnalysisResult` | `analyze` | `plan`（下一轮）, `decide` | 基准测试分析。包含 `trend`（`improving` / `stable` / `declining`）、`improvement_pct`、`likely_bottleneck`（`cpu` / `memory` / `io` / `network` / `configuration`）、`change_impact`、`insights`、`recommended_focus` |
| `orchestrator_decision` | `OrchestratorDecision` | `decide` | 路由条件 | 下一步动作。包含 `action`（`CONTINUE_TUNING` / `CONVERGED` / `ROLLBACK` / `GOALS_MET` / `MAX_TRIALS_REACHED` / `MAX_DURATION_REACHED`）、`reasoning` |
| `advisor_recommendations` | `AdvisorRecommendations` | `plan`（收敛时） | `finalize` | 非参数替代方案。包含 `summary` 和 `recommendations` 列表（每项含 `category`, `recommendation`, `expected_benefit`, `effort`, `risk`） |
| `tunable_parameters` | `list[dict]` | `plan` | `safety_check`, `apply_config` | 当前可调参数的元数据快照。每项含 `name`, `category`, `risk`, `restart_required`, `type`, `min/max`, `enum_values`, `depends_on`, `conflicts_with` |

#### 异常与保护

| 字段 | 类型 | 说明 |
|------|------|------|
| `errors` | `list[str]` | 错误日志累积 |
| `consecutive_empty_proposals` | `int` | 连续空提案计数。≥5 时 `plan` 节点强制调用 Advisor |
| `consecutive_orchestrator_failures` | `int` | Orchestrator 连续失败计数。≥5 时 `decide` 节点强制 CONVERGED 终止 |

#### 关键方法

`ExperimentState` 不是纯数据容器，还封装了核心业务逻辑：

| 方法 | 说明 |
|------|------|
| `begin_trial(config, changes)` | 递增 `trial_number`，创建新 `TrialResult` 赋给 `current_trial` |
| `commit_current_trial(status)` | 将 `current_trial` 归档到 `trial_history`（已存在则跳过） |
| `record_analysis(analysis)` | 保存分析结果到 `analysis_result` 和 `current_trial.analysis`，调用 `compute_improvement()` 计算加权改善率，更新 `best_metrics`/`best_config`（当改善>0 时） |
| `compute_improvement(new_metrics)` | 多目标加权改善率计算。根据 goal.operator 区分方向：`>=`/`>` 正向变化为改善，`<=`/`<` 负向变化为改善。首 trial（无 best_metrics）返回 100% |
| `goal_met(metric_name, value)` | 单目标判定，支持 `>=`, `<=`, `>`, `<`, `==` 五种运算符 |
| `all_goals_met()` | 全部 goal 是否由 `best_metrics` 满足 |
| `has_converged()` | 滑动窗口收敛判定：最近 `convergence_window` 个 `improvement_history` 值全部低于 `improvement_threshold_pct` |
| `update_elapsed_hours()` | 基于 `start_time` 刷新 `elapsed_hours` |

---

## 3. LLM + Bayesian Optimization 混合优化引擎

### 3.1 混合策略架构

```
┌─────────────────────────────────────────────┐
│              HybridTuner                     │
│                                              │
│  ┌───────────────────────┐                   │
│  │ LLM 路径 (优先)        │                   │
│  │ · 利用预训练知识       │                   │
│  │ · 上下文感知推理       │                   │
│  │ · 理解参数语义关系     │                   │
│  │ · ChromaDB 知识库增强   │                   │
│  └───────────┬───────────┘                   │
│              │ 断路打开 / 限流 / API 异常      │
│              ▼                               │
│  ┌───────────────────────┐                   │
│  │ Bayesian 回退          │                   │
│  │ · ≤30 参数: GP + EI   │                   │
│  │ · 30-100 参数: TPE    │                   │
│  │ · 从 LLM trial 历史 seed│                  │
│  └───────────────────────┘                   │
└─────────────────────────────────────────────┘
```

**核心实现**：`optimization/hybrid_tuner.py` 中的 `HybridTuner.propose()` 方法：

1. 优先尝试 LLM TunerAgent 生成提案
2. 捕获 `CircuitBreakerOpenError` 或任意 LLM 调用异常
3. 自动降级到 Bayesian 路径，利用所有历史 trial 数据 seed 优化器
4. `propose_or_skip()` 变体在 LLM 和 BO 均无法产出有效建议时返回空变更，避免垃圾输入进入安全门控

### 3.2 后端自动选择

`optimization/selector.py` 根据参数空间维度自动选择最优算法：

| 参数数量 | 后端 | 库 | 说明 |
|----------|------|-----|------|
| ≤ 30 | **GP+EI** (高斯过程 + 期望改进) | scikit-optimize | 低维最佳样本效率，O(n³) 拟合成本可接受 |
| 31-100 | **TPE** (Tree-structured Parzen Estimator) | Optuna | 处理高维混合空间，O(n·log n) |
| > 100 | **TPE + 警告** | Optuna | 建议降维或多保真度优化 |

阈值通过环境变量 `LLMTUNER_BO_GP_MAX_DIMS`（默认 30）和 `LLMTUNER_BO_TPE_MAX_DIMS`（默认 100）配置。

### 3.3 GP+EI 优化器 (`BayesianOptimizer`)

`optimization/bayesian.py`:

- **底层**：`skopt.Optimizer` + `GP` 基础估计器 + `EI` (Expected Improvement) 采集函数
- **参数空间映射**：
  - `integer` → `skopt.space.Integer`
  - `float` → `skopt.space.Real`
  - `enum` / `boolean` → `skopt.space.Categorical`
- **Warm-start**：通过 `tell()` 方法从 LLM 阶段积累的所有有效 trial 数据中初始化 GP 先验
- **propose_changes_diff()**：生成提案后与 current_config 做 diff，仅保留真正不同的参数，上限 `max_changes`（默认 4）
- **optimize()**：完整的 BO 循环，支持 `n_calls` 指定新增迭代次数，与已有 seed 点累计

### 3.4 TPE 优化器 (`TPEOptimizer`)

`optimization/tpe_optimizer.py`:

- **底层**：`optuna.create_study()` + `TPESampler`
- **自动 log 尺度**：对范围 > 1000 的整数参数自动应用 `IntLogUniformDistribution`
- **Warm-start**：通过 `optuna.trial.create_trial()` + `study.add_trial()` 从历史数据注入
- **类型安全**：`_check_value_in_distribution()` 校验每个参数值在分布范围内，非法值跳过不崩溃
- **接口完全兼容** `BayesianOptimizer`，可无缝互换

### 3.5 提示词缓存优化

系统提示词完全静态化，不包含任何 Jinja2 变量插值。所有动态数据（目标、当前配置、可调参数列表、分析结果等）均通过 `build_json_message()` 放入用户消息的 `INPUT_JSON` 字段。

`agents/prompt_payload.py` 提供的共享工具：
- `compact_json()` — 排序键、无空白序列化，提升缓存亲和度
- `limit_list()` / `limit_mapping()` — 截断大数据结构
- `truncate_text()` — 长文本截断保留前缀稳定
- `build_json_message(instruction, payload)` — 统一的消息格式

**效果**：系统提示缓存命中率从 0% 提升到 ~97%，一次 30-trial 调优的综合输入 token 减少约 25-35%。

### 3.6 规则判断短路

在 LLM 调用链上插入确定性规则引擎，明确可判的场景直接跳过 LLM：

**Safety Check 短路** (`workflow/nodes/safety_check.py` `_can_short_circuit_approval()`):
- 所有变更参数 risk = low
- 无 restart_required 参数
- 无 depends_on / conflicts_with 依赖
- proposed_value 在 min/max/enum 范围内
→ 直接 APPROVE，不调用 SafetyAgent LLM

**Decide 短路** (`workflow/nodes/decide.py` `_rule_based_decision()`):
- 无历史 trial → CONTINUE_TUNING
- 最近一次改善 ≥ 阈值 → CONTINUE_TUNING
- 最近 2+ 次改善 ≤ 0 → CONVERGED
- 滑动窗口内所有改善 < 阈值 → CONVERGED

**效果**：约 35-45% 的 LLM 调用被消除。

---

## 4. 参数管理与安全门控机制

### 4.1 参数 Schema 系统

`parameters/schema.py` 定义了每个可调参数的完整元数据模型：

```python
class ParameterDefinition(BaseModel):
    name: str                           # 参数名
    category: ParameterCategory         # MEMORY, IO, PERSISTENCE, NETWORK, CONNECTIONS, REPLICATION, LOGGING, GENERAL
    description: str                    # 功能描述
    default_value: str                  # 默认值
    type: str                           # string, integer, float, boolean, enum
    min_value: str | None               # 最小值
    max_value: str | None               # 最大值
    enum_values: list[str] | None       # 枚举可选值
    restart_required: bool              # 是否需要重启
    risk: ParameterRisk                 # LOW, MEDIUM, HIGH, CRITICAL
    depends_on: list[str]               # 依赖参数列表
    conflicts_with: list[str]           # 冲突参数列表
    notes: str                          # 调优备注
```

**内置 Schema 文件**：`parameters/schemas/redis_7.2.json`（25 个参数）、`mysql_8.0.json`。

### 4.2 ParameterManager

`parameters/manager.py` 提供完整的参数生命周期管理：

| 方法 | 功能 |
|------|------|
| `parse_and_validate(config_text)` | 解析配置文件 → 类型校验 → 入历史栈 |
| `get_tunable_parameters()` | 按类别、风险级别、blocklist 过滤可调参数 |
| `diff(old, new)` | 计算两个配置的差异，标注每个变更的参数名、旧值、新值、是否需重启 |
| `snapshot(config)` | 创建当前配置的 JSON 快照 |
| `rollback()` | 从历史栈弹出当前配置，返回上一版本 |
| `get_parameter_info(name)` | 查询单个参数的完整元数据 |
| `serialize_config(config)` | 将配置 dict 序列化回原生格式文本 |

**配置解析器**（`parameters/parser.py`）：
- `RedisConfigParser`：解析 `key value` 格式，支持重复 key 处理
- `MySQLConfigParser`：解析 `[section]` / `key=value` 格式

### 4.3 多层安全约束体系

| 层级 | 机制 | 触发位置 | 说明 |
|------|------|---------|------|
| **参数风险过滤** | `max_risk_level` (low/medium/high) | plan 节点 `_parse_max_risk()` | 超过用户设定风险级别的参数不进入候选列表 |
| **Blocklist** | `blocklist: list[str]` | plan 节点 `get_tunable_parameters()` | 指定参数绝对不可修改 |
| **重启控制** | `allow_restart` + `max_restart_changes` | plan 排除 + safety 检测 | `allow_restart=false` 时 filter 掉所有 `restart_required` 参数；safety 阶段对重启参数做二次校验 |
| **值域校验** | `min_value` / `max_value` / `enum_values` | safety 短路 `_value_fits_metadata()` | 检查 proposed_value 是否符合参数定义的合法值域 |
| **依赖/冲突检查** | `depends_on` / `conflicts_with` | safety 短路 `_can_short_circuit_approval()` + LLM 审核 | 存在依赖/冲突关系的参数不适用短路审批，必须经 LLM Safety Agent 审核 |
| **回滚保护** | `max_consecutive_rollbacks` | decide 节点硬终止条件 | 连续回滚 ≥ 阈值自动终止调优 |
| **内存余量** | `memory_headroom_pct` (默认 20%) | Safety Agent LLM 审核 | 配置变更总内存不超过物理内存预留比例 |
| **Dry run** | `dry_run: bool` | executor `_format_dry_run_report()` | 先展示将改哪些参数，不实际执行 |

### 4.4 Safety Check 详细流程

`safety_check` 节点 (`workflow/nodes/safety_check.py`) 的执行顺序：

1. **空变更快速通道**：无 proposed changes → 直接 APPROVE
2. **参数元数据收集**：从 `tunable_parameters` 构建 `metadata_by_name` 索引
3. **规则校验**（遍历每个变更）：
   - 参数是否在已知可调参数列表中
   - 如果 `restart_required` 且 `allow_restart=false` → REJECT
   - 如果 `risk` 超过 `max_risk_level` → REJECT
   - 重启参数数量超过 `max_restart_changes` → REJECT
4. **短路审批判定**（`_can_short_circuit_approval()`）：全部低风险 + 无重启 + 无依赖冲突 + 值域合法 → APPROVE
5. **LLM Safety Agent 审核**：不满足短路条件时，传入仅包含被修改参数及其依赖/冲突项的元数据（`_select_relevant_parameter_metadata()` 裁剪），由 SafetyAgent LLM 做语义级安全审核
6. **异常兜底**：Safety Agent 调用失败时默认 REJECT

### 4.5 配置应用与回滚

`apply_config` 节点 (`workflow/nodes/apply_config.py`)：

1. 处理 `APPROVE_WITH_MODIFICATIONS` 裁决，用 Safety Agent 建议的值覆盖原始提案
2. 创建 `new_config = deepcopy(current_config)` 并逐参数修改
3. 调用 `runner.snapshot_config()` 创建回滚快照（`.bak` 后缀文件）
4. 序列化配置并通过 `runner.write_config()` 写入目标实例
5. 如需重启：执行 `restart_command` → 轮询 `health_check_command`（最多 6 次 × 2s）→ 健康检查失败则 `restore_snapshot()` 回滚
6. 异常时自动触发 ROLLBACK 决策

---

## 5. Benchmark 执行、实验追踪与断点恢复

### 5.1 通用直连 Runner

`benchmark/custom_direct_runner.py` 中的 `CustomDirectRunner` 是最核心的执行器，**不绑定特定工具**。用户提供 Shell 命令模板，系统自动填入连接参数：

**生命周期命令模板**：
- `start_command`：启动服务
- `run_command`：运行基准测试，支持 `{host}`, `{port}`, `{credentials}`, `{config_path}`, `{clients}`, `{requests}`, `{duration}`, `{tests}` 占位符
- `teardown_command`：停止/清理
- `health_check_command`：健康检查
- `restart_command`：重启服务

**模板渲染**使用 `_SafeFormatDict` 类，缺失 key 返回原始 `{key}` 字符串而非抛出 `KeyError`，保证不完整模板也能产生可读的命令。

### 5.2 内置输出解析器

通过 `@register_parser(name)` 装饰器注册：

| 解析器 | 格式 | 说明 |
|--------|------|------|
| `redis-benchmark-csv` | Redis CSV 输出 | 按行解析 `"operation","rps"` 格式，聚合 total_rps，正则提取 p99 |
| `sysbench` | Sysbench 标准输出 | 正则提取 tps/qps/p95/p99/avg 延迟、读写操作数 |
| `regex` | 自定义正则 | 用户通过 `metric_regex` dict 定义提取规则，无配置时返回原始输出长度 |
| `raw` | 通用 | 返回行数和字符数 |

### 5.3 配置快照与回滚

每次 `write_config()` 前自动执行 `shutil.copy2()` 创建 `.bak` 备份文件，存入 `_config_history` 栈。`rollback_config()` 从栈中弹出并恢复。`snapshot_config()` 创建带编号的命名快照（`.snap0`, `.snap1`, ...）。

### 5.4 稳定模式 Benchmark

`benchmark/stability.py` 中的 `StabilityRunner` 包装器：
1. 缓存预热（warmup）
2. 多次迭代（默认 3 次）
3. 按指标取中位数
4. 变异系数报告：stable (< 5%), unstable (5-10%), volatile (> 10%)

### 5.5 实验追踪（SQLAlchemy ORM + SQLite）

`tracking/experiment.py` 中的 `ExperimentTracker` 管理 4 个数据模型：

| 模型 | 持久化内容 |
|------|-----------|
| **Experiment** | 实验名称、目标系统、目标 JSON、基线配置、硬件规格、状态、时间戳 |
| **Trial** | 试验编号、阶段、状态、配置快照 JSON、度量 JSON、改进率 |
| **ParameterChange** | 参数名、旧值/新值、变更理由、安全审批状态 |
| **BenchmarkRun** | 配置名、runner 类型、原始输出、解析后的度量 |

CLI 命令 `history` 和 `show` 使用 `rich` 表格展示实验历史。`show <experiment_id>` 支持前缀 ID 匹配。

### 5.6 断点恢复机制

#### Checkpoint 持久化

`executor.py` 在每次 trial 完成后保存 checkpoint：

```python
def _save_checkpoint(state, workspace, task_id):
    path = Path(workspace) / ".agent/tuning/sessions" / task_id / "checkpoint.json"
    payload = state.model_dump(mode="json")
    # 原子写入：先写 .tmp 再 os.replace
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ...))
    tmp.replace(path)
```

#### 异常中断处理

`main.py` 的 `_run_workflow()` 使用三层防护：
1. `asyncio.CancelledError` 捕获（Ctrl+C 触发）
2. 通用 `Exception` 捕获
3. `finally` 块中总是调用 `save_snapshot()`

中断时自动：
- 保存完整 `ExperimentState` 的 JSON 快照
- 尝试 LLM 生成进度摘要（`generate_llm_summary()`），失败则生成最小摘要
- 打印恢复命令：`python -m src.main run <config.yaml> --resume`

#### 恢复路径

`--resume` 标志触发 `load_snapshot()` → 重建所有字段（`ExperimentState`, `GoalSpec`, `TrialResult` 列表）→ 删除快照文件防止误用 → 继续工作流执行。

#### 会话级错误恢复

`TuningSessionManager` 的错误状态机：
```
NEW → INTAKE → EXECUTION → DONE        (正常路径)
                    ↓
                  ERROR                  (执行失败)
                    ↓
             用户说 "retry" → EXECUTION  (恢复路径)
```

关键语义：
- `run_execution()` **抛异常而非返回失败文本**，确保上层捕获并设置 ERROR 状态
- 只有 DONE 状态才清理 session，ERROR 状态保留完整上下文
- `_handle_existing_execution()` 检测到 ERROR 会话时自动转为 EXECUTION 重试

### 5.7 Profile 复用

`profile_store.py` 将每次 intake 的结果自动保存为 YAML profile 到 `{workspace}/.agent/tuning/profiles/`。密码字段自动脱敏（存储时置空，标记在 `redacted_fields` 中）。下次调优同一实例时自动检测已有 profile 并提示复用。

已提供的示例 profiles：`profiles/redis-basic.yaml`, `profiles/redis-custom-cmd.yaml`, `profiles/mysql-sysbench.yaml`。

---

## 6. 与 Nanobot 主 Agent 框架的深度集成

### 6.1 意图路由

`TuningIntentRouter` (`nanobot/agent/tuning/router.py`) 在主 Agent Loop 的 `COMMAND` 阶段之前被调用：

**关键词检测** (`intent.py`)：
- 目标系统检测：正则匹配 `redis` / `mysql`（大小写不敏感）
- 调优意图检测：中英文关键词 — `tune`, `tuning`, `optimize`, `throughput`, `latency`, `qps`, `调优`, `调参`, `优化`, `性能`, `吞吐`, `延迟`
- 重试检测：`retry`, `continue`, `rerun`, `继续`, `重试`, `再试`
- 取消检测：`cancel tuning`, `stop tuning`, `取消调优`, `停止调优`, `退出调优`

**路由逻辑** (`_classify_request()`):
1. 取消关键词 → 终止 session，返回取消确认
2. 已有 INTAKE 阶段 session → 继续 intake 对话
3. 已有 ERROR 阶段 session + 重试关键词 → 恢复执行
4. 调优关键词 + 目标系统匹配 → 新建调优 session

### 6.2 会话管理

`TuningSessionManager` 管理每个 session_key 对应的独立调优会话：

- **并发控制**：每个 session_key 一个 `asyncio.Lock`，确保同一会话的 intake 串行执行
- **执行任务**：通过 `_spawn_background()` 将 LangGraph 工作流作为后台 `asyncio.Task` 运行
- **执行中检测**：`_is_execution_running()` 防止重复启动
- **取消**：`cancel_session()` 从字典中移除 session 并 cancel 后台 task

### 6.3 LLM 凭据桥接

`executor.py` 中的 `_configure_tuner_llm()` 上下文管理器：

```python
@contextmanager
def _configure_tuner_llm(provider, model):
    # 保存 _llm_tuner 原始配置
    original = {
        "deepseek_api_key": tuner_settings.deepseek_api_key,
        ...
    }
    # 注入 nanobot provider 凭据
    tuner_settings.deepseek_api_key = provider.api_key
    tuner_settings.llm_model = model
    ...
    try:
        yield
    finally:
        # 退出时恢复原始配置（隔离性保证）
        tuner_settings.deepseek_api_key = original["deepseek_api_key"]
        ...
```

**设计要点**：`_llm_tuner` 维护完全独立的 `Settings` 对象（`LLMTUNER_*` 环境变量前缀），与 nanobot 的配置体系隔离。桥接层仅在执行期间临时注入凭据，退出时恢复，避免状态污染。

### 6.4 消息推送（流式进度报告）

`_run_execution_and_report()` 通过 MessageBus 发布进度更新：

- **执行进度**：每个 trial 完成后通过 `report_progress` 回调发布进度消息（`_format_trial_progress()`）
- **完成通知**：工作流结束后，通过 `bus.publish_inbound()` 将调优报告作为 `injected_event: "tuning_result"` 注入主 Agent Loop
- **多频道支持**：进度消息通过 `OutboundMessage` 发布到 origin_channel（支持 Telegram/Slack/Discord/WebUI 等所有 nanobot 频道）

### 6.5 记忆归档

`_archive_to_memory()` 将调优结果写入 `MemoryStore.history.jsonl`：

```python
entry = (
    f"[Tuning Result] {target} {version} | "
    f"Trials: {trials} | "
    f"Best: {metric_summary}{imp_str} | "
    f"Config changes: {config_str}"
)
self.memory_store.append_history(entry)
```

nanobot 的 **Dream 两阶段记忆处理**在下一个 cron 周期自动消费该条目，提炼为 `MEMORY.md` 中的调优知识，形成长期可迭代的智能调优能力。

### 6.6 依赖检查

`executor.py` 的 `_check_dependencies()` 在执行前验证最小依赖：
- 必需：`structlog`, `langgraph`
- 可选优化器：`skopt` 或 `optuna`（至少一个）
- 缺失时抛出 `RuntimeError` 并附带安装指令：`pip install nanobot[tuning]`

### 6.7 全局执行锁

`_TUNER_EXECUTION_LOCK = asyncio.Lock()` 确保同一时刻只有一个调优工作流在运行，避免多个调优任务竞争 LLM API 配额和系统资源。

---

## 性能数据估算

一次 30-trial Redis 调优：

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| LLM 调用次数 | ~120 | ~80 | **-33%** |
| 系统提示缓存命中率 | 0% | 96.7% | **+96.7pp** |
| 输入 token 总量（估算） | ~600K | ~390K | **-35%** |
| LLM 成本（估算） | ~$0.60 | ~$0.35 | **-42%** |
| Safety check 平均延迟 | 1-3s (LLM) | 0ms (短路) 40% 场景 | - |
| Decide 平均延迟 | 1-2s (LLM) | 0ms (短路) 30% 场景 | - |
| 崩溃后数据损失 | 全部 trial | 最多 1 个 trial | **近零** |
