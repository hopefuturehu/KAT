# Nanobot Tuning 子系统设计详解

## 一句话概括

**用自然语言聊天的方式，让 AI 自动帮你调优 Redis / MySQL 的性能参数。** 你说"帮我把 Redis 的吞吐调到 8 万 QPS"，它自己收集需求、连接实例、跑 benchmark、分析结果、提出参数变更、安全审核、应用配置，循环迭代直到达到目标。

---

## 整体架构

```
User: "帮我把 Redis 7.2 在 10.0.0.5:6379 上调优到 8 万 QPS"
        │
        ▼
┌─────────────────────────────────────────────┐
│  TuningIntentRouter                          │
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

### LangGraph 工作流节点图

```
     ┌──────────┐
     │Initialize │ ← 连接实例，读基线配置
     └────┬─────┘
          │
          ▼
     ┌──────────┐       ┌──────────┐
     │   Plan   │◄──────│ Rollback │ ← 恢复配置备份
     └────┬─────┘       └────▲─────┘
          │                   │
          ▼                   │
  ┌───────────────┐           │
  │ Safety Check  │─── REJECT ─┘
  └───────┬───────┘
          │ APPROVE
          ▼
  ┌───────────────┐
  │ Apply Config  │ ← 写入配置, 重启服务
  └───────┬───────┘
          │
          ▼
  ┌───────────────┐
  │ Run Benchmark │ ← 用户提供的 shell 命令
  └───────┬───────┘
          │
          ▼
  ┌───────────────┐
  │   Analyze     │ ← 解析输出, 对比历史趋势
  └───────┬───────┘
          │
          ▼
  ┌───────────────┐
  │   Decide      │ ← 继续 / 收敛 / 回滚 / 目标达成
  └───────┬───────┘
          │
          ├─ CONTINUE ──→ Plan
          ├─ CONVERGED ──→ Advisor → Finalize
          ├─ GOALS_MET ──→ Finalize
          └─ ROLLBACK ──→ Rollback → Plan
```

---

## 核心亮点

### 1. 自然语言驱动的调优入口

传统 DB 调优需要写 YAML、手动 SSH、跑脚本、分析 CSV。这里只需要聊天——和跟普通 agent 对话完全一样。系统通过多轮对话自动提取：

- **目标系统**：Redis / MySQL + 版本号
- **连接信息**：host、port、密码（密码在 profile 保存时自动脱敏）
- **Benchmark 命令**：支持模板变量 `{host}` `{port}` `{clients}` `{requests}` `{duration}` `{tests}`，系统自动填入
- **优化目标**：多目标加权（如 "QPS >= 80000 weight=1.0, P99 <= 2ms weight=0.8"）
- **安全约束**：是否允许重启、风险上限、参数 blocklist、内存余量

### 2. LLM + Bayesian 混合优化器

参数变更提案由 **HybridTuner** 生成，这是系统最核心的设计：

```
┌─────────────────────────────────────────────┐
│              HybridTuner                     │
│                                              │
│  ┌───────────────────────┐                   │
│  │ LLM 路径 (优先)        │                   │
│  │ · 利用预训练知识       │                   │
│  │ · 上下文感知推理       │                   │
│  │ · 理解参数语义关系     │                   │
│  └───────────┬───────────┘                   │
│              │ 断路/限流/异常                 │
│              ▼                               │
│  ┌───────────────────────┐                   │
│  │ Bayesian 回退          │                   │
│  │ · ≤30 参数: GP + EI   │                   │
│  │ · 30-100 参数: TPE    │                   │
│  │ · 自动选择最优后端     │                   │
│  └───────────────────────┘                   │
│                                              │
│  两路径共享 trial history，BO 从历史 seed     │
└─────────────────────────────────────────────┘
```

**LLM 路径**利用模型对参数的语义理解（如"`io-threads` 提升并发但可能增延迟，配合 `io-threads-do-reads` 效果更好"），结合当前 trial 的分析结果做推理。**Bayesian 回退**在 LLM 不可用时自动降级为纯数学优化，根据参数维度自动选择 GP+EI（≤30 维度，采样效率最优）或 TPE（高维场景）。

### 3. 提示词缓存架构——系统提示永远不变

这是整个子系统在成本和延迟层面最重要的设计决策。**所有 4 个 Agent 的系统提示完全静态**，不包含任何 Jinja2 变量插值：

```
变更前（每次 trial 系统提示都变，缓存命中率 0%）：
┌──────────────────────────────────────────┐
│ System: "你是 Redis 7.2 调优专家。        │
│          目标: [{qps>=80000,...}]         │  ← 每次内容不同
│          当前配置: {maxmemory:768mb,...}   │  ← 每次内容不同
│          可调参数: [{25个参数元数据}...]"   │  ← 每次内容不同
│ User: "请提出参数变更建议"                 │
└──────────────────────────────────────────┘

变更后（系统提示静态，缓存命中率 96.7%）：
┌──────────────────────────────────────────┐
│ System: "你是数据库调优专家。              │
│          输入以结构化 JSON 到达用户消息中。  │  ← 永远不变
│          规则: 优先低风险参数, 不超限..."   │  ← 永远不变
│                                          │      缓存命中率 96.7%
│ User: "INPUT_JSON:                       │
│        {target:redis,                     │  ← 每次不同（本该不同）
│         current_config_subset:{...},       │
│         candidate_parameters:[...24个...]}"│
└──────────────────────────────────────────┘
```

**共享工具模块** `prompt_payload.py` 所有 4 个 Agent 统一使用：

- `compact_json()` — 排序键、无空白序列化，提升缓存亲和度并减少 token 数
- `limit_list()` / `limit_mapping()` — 截断大数据结构
- `truncate_text()` — 长文本截断并保留前缀稳定
- `build_json_message(instruction, payload)` — 统一的消息格式

**收益：** 一次 30-trial 调优（4 Agent × 30 = 120 次 LLM 调用），系统提示缓存命中率从 0% 提升到 97%，综合输入 token 减少约 25-35%。以 Anthropic 缓存折扣（命中约 0.1x 价格）计算，调优会话的 LLM 成本降低约三分之一。

### 4. 规则判断短路——能不算的就算

在 LLM 调用链上插入了多个**确定性规则引擎**，遇到明确可判的场景直接跳过 LLM：

```
Safety Check 短路:
  全低风险 + 无重启 + 无依赖/冲突 + 值在范围内 → 跳过 LLM → 直接 APPROVE

Decide 短路:
  改善 > 阈值 → 跳过 LLM → CONTINUE_TUNING
  连续改善 ≤ 0 → 跳过 LLM → CONVERGED
  目标已达成 → 跳过 LLM → GOALS_MET
  回滚过多 / 超时 / 超次数 → 跳过 LLM → 对应终止动作
```

这两处短路让约 **35-45% 的 LLM 调用被消除**。加上提示词缓存优化，一次 30-trial 调优从约 120 次 LLM 调用降到约 80 次，每次调用的 token 也更少。延迟方面，省一次 LLM 调用就是省 1-3 秒，30 trial 累积可快 1.5-3 分钟。

### 5. 智能参数选择——不把所有参数全塞给 LLM

MySQL 有 400+ 参数，但绝大多数对当前负载类型没有影响。TunerAgent 用 **`_select_tunable_params()`** 做参数筛选：

```
评分规则:
  最近被改动过         → +100 分 (LLM 需要知道，但建议避免再次改动)
  匹配 analyzer 推荐方向 → +50 分  (如瓶颈在 IO → IO 相关参数加分)
  不需要重启           → +10 分
  risk = low          → +5 分
  risk = medium       → +2 分

取 Top 24 送入 LLM
```

同时只有**相比基线有变化的参数**被传给 LLM（`_summarize_config_delta`），而非完整的 `current_config`。Safety Agent 也类似——只传被修改参数及其依赖/冲突项的元数据，无关参数全部裁剪。

### 6. 完整的错误状态机与断点续跑

```
NEW → INTAKE → EXECUTION → DONE        (正常路径)
                    ↓
                  ERROR                  (执行失败)
                    ↓
             用户说 "retry" → EXECUTION  (恢复路径)
```

关键语义健壮性：
- `run_execution()` **抛异常而非返回失败文本**，确保上层捕获并设置 ERROR
- 只有 DONE 状态才清理 session，ERROR 状态保留完整上下文
- `_handle_existing_execution()` 检测到 ERROR 会话时自动转为 EXECUTION 重试
- 每个 trial 完成后序列化 `ExperimentState` (Pydantic `.model_dump()`) 到 `.agent/tuning/sessions/{task_id}/checkpoint.json`
- **原子写入**：先写 `.tmp` 再 `os.replace`，防止写入中途崩溃损坏文件
- 恢复时通过 `ExperimentState.model_validate()` 重建，从断点继续

### 7. 三层 JSON 提取 + LLM 自修复

LLM 输出的 JSON 常见问题：尾部逗号、单引号、注释、裸对象未包裹在代码块中。系统用三层策略保障：

```
第 1 层: 平衡括号扫描
  逐字符数 { / } 深度，找到所有合法 JSON 候选，按长度排序取最大者

第 2 层: json_repair 修复
  处理尾部逗号、单引号键、// 注释等常见 LLM 错误

第 3 层: LLM 自修复
  如果前两层都失败，再调一次 LLM: "你的输出没有合法 JSON，请只输出 JSON 对象"
```

### 8. 可复用的调优 Profile

每次 intake 的结果自动保存为 YAML profile。密码字段自动脱敏（存储时置空，标记在 `redacted_fields` 中）。下次调优同一实例时：

> "找到已有调优配置：1. redis-127.0.0.1-6379 (127.0.0.1:6379) [needs confirmation]。回复 1 复用，或 skip 重新配置。"

脱敏后的 profile 进入 intake 补齐缺失字段（密码等），然后直接跳入执行阶段。

### 9. 通用的 Benchmark 抽象

不绑定特定工具。用户提供 shell 模板，系统自动填入连接参数：

```bash
redis-benchmark -h {host} -p {port} -c {clients} -n {requests} -t {tests} --csv
sysbench oltp_read_write --mysql-host={host} --mysql-port={port} --time={duration} run
```

内置解析器：`redis-benchmark-csv`、`sysbench`、自定义 `regex`、以及 `raw`（行数/字符数）。支持**稳定模式**（warmup + 多次迭代取中位数）消除噪声。

### 10. 多层安全约束体系

| 层级 | 机制 | 触发条件 |
|------|------|---------|
| 参数风险过滤 | 每个参数预定义 risk 等级，用户可设上限 | plan 阶段过滤 + safety 阶段二次审核 |
| Blocklist | 指定参数绝对不可修改 | plan 阶段排除 |
| 重启控制 | `allow_restart=false` 拒绝任何需要重启的变更 | safety 阶段检测 |
| 值域校验 | 检测 proposed_value 是否符合 min/max/enum | safety 短路阶段 + LLM 审核 |
| 依赖/冲突检查 | 参数 A 依赖 B 则 B 必须正确，不兼容的参数不同时改 | safety 阶段 |
| 回滚保护 | 连续回滚 ≥ 阈值自动停止 | decide 阶段 |
| 内存余量 | 配置变更总内存不超过物理内存预留比例 | safety 阶段 |
| Dry run | 先跑一遍看哪些参数会被改，不实际执行 | intake 阶段 |

### 11. 与 Nanobot 框架的深度集成

- 通过 MessageBus 收发进度通知（支持流式进度报告到 WebUI / Telegram / Slack）
- 结果自动归档到 MemoryStore，Dream 合并后可写入 MEMORY.md / SOUL.md 形成长期记忆
- 支持所有 nanobot 频道，手机端可发起调优
- 复用 nanobot 的 provider 凭据（通过 `_configure_tuner_llm` 上下文管理器注入到 `_llm_tuner` 的 settings）

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

---

## 关键文件索引

### nanobot 集成层

| 组件 | 路径 |
|------|------|
| 会话生命周期管理 | `nanobot/agent/tuning/manager.py` |
| 需求收集 + JSON 提取 | `nanobot/agent/tuning/intake.py` |
| 意图检测 & 路由 | `nanobot/agent/tuning/router.py` + `intent.py` |
| 执行引擎桥接 + checkpoint | `nanobot/agent/tuning/executor.py` |
| 数据模型 (Session/Requirements) | `nanobot/agent/tuning/schema.py` |
| Profile YAML 存储 | `nanobot/agent/tuning/profile_store.py` |

### _llm_tuner 调优引擎

| 组件 | 路径 |
|------|------|
| LangGraph 工作流定义 | `_llm_tuner/src/workflow/graph.py` |
| 工作流节点 (9 个) | `_llm_tuner/src/workflow/nodes/*.py` |
| 实验状态 (Pydantic) | `_llm_tuner/src/workflow/state.py` |
| 混合优化器 (LLM+BO) | `_llm_tuner/src/optimization/hybrid_tuner.py` |
| GP+EI 贝叶斯优化 | `_llm_tuner/src/optimization/bayesian.py` |
| TPE 优化器 | `_llm_tuner/src/optimization/tpe_optimizer.py` |
| 后端自动选择 (GP vs TPE) | `_llm_tuner/src/optimization/selector.py` |
| 参数元数据 & 解析 | `_llm_tuner/src/parameters/manager.py` + `schema.py` |
| Tuner Agent + 参数选择 | `_llm_tuner/src/agents/tuner_agent.py` |
| Safety Agent + 短路审批 | `_llm_tuner/src/agents/safety_agent.py` |
| Analyzer Agent + delta 计算 | `_llm_tuner/src/agents/analyzer_agent.py` |
| Orchestrator Agent | `_llm_tuner/src/agents/orchestrator.py` |
| Advisor Agent (收敛后) | `_llm_tuner/src/agents/advisor_agent.py` |
| 通用 Agent 基类 | `_llm_tuner/src/agents/base.py` |
| 提示词负载工具 | `_llm_tuner/src/agents/prompt_payload.py` |
| 4 个 Agent 的静态系统提示 | `_llm_tuner/src/agents/prompts/*.j2` |
| 直接模式 Runner (shell 生命周期) | `_llm_tuner/src/benchmark/custom_direct_runner.py` |
| Benchmark 输出解析器 | `_llm_tuner/src/benchmark/custom_direct_runner.py` (注册式) |
| 稳定模式 wrapper | `_llm_tuner/src/benchmark/stability.py` |
| LLM 韧性工具 (断路器/JSON 安全提取) | `_llm_tuner/src/utils/llm_resilience.py` |
