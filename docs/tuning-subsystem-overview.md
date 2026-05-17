# Nanobot Tuning 子系统设计详解

## 一句话概括

**用自然语言聊天的方式，让 AI 自动帮你调优 Redis / MySQL 的性能参数。** 你说"帮我把 Redis 的吞吐调到 8 万 QPS"，它自己收集需求、连接实例、跑 benchmark、分析结果、提出参数变更、安全审核、应用配置，循环迭代直到达到目标。

---

## 整体架构

```
用户用自然语言提出需求
        │
        ▼
┌──────────────────────────┐
│  阶段 1: 需求收集 (Intake) │  ← LLM 多轮对话，收集结构化需求
│  · 目标系统、版本         │
│  · 连接信息               │
│  · Benchmark 命令         │
│  · 优化目标 & 安全约束    │
└──────────┬───────────────┘
           │ TuningRequirements (结构化数据)
           ▼
┌──────────────────────────┐
│  阶段 2: 自动调优执行       │  ← LangGraph 状态机循环
│                            │
│  initialize → plan → safety_check               │
│       ▲                    │                    │
│       │                    ▼                    │
│       │              apply_config               │
│       │                    │                    │
│       │                    ▼                    │
│       │              run_benchmark              │
│       │                    │                    │
│       │                    ▼                    │
│       │                analyze                  │
│       │                    │                    │
│       └─── decide ◄────────┘                   │
│           (继续/结束/回滚)                        │
└──────────┬───────────────┘
           │ 结果归档到 MemoryStore
           ▼
      📊 调优报告
```

---

## 核心亮点

### 1. 自然语言驱动的调优入口

传统 DB 调优需要写 YAML 配置、手动连 SSH、跑脚本、分析 CSV。这里你只需要用聊天——和平时跟 agent 对话一样。系统会自动从自然语言中提取：

- 目标系统（Redis / MySQL）和版本
- 连接地址、端口、认证信息
- Benchmark 命令（支持模板变量 `{host}`, `{port}`, `{clients}` 等）
- 优化目标（如"吞吐 >= 80000 ops/sec，延迟 p99 <= 5ms"）
- 安全约束（是否允许重启、风险上限、禁止修改的参数列表）

### 2. LLM + Bayesian 混合优化器

这是整个系统最有趣的设计。参数变更提案由 **HybridTuner** 生成：

```
LLM (知识驱动)  ──优先──▶  基于预训练知识 + trial 历史做上下文感知的提案
     │  失败/断路器断开
     └──回退──▶  Bayesian Optimization (GP+EI / TPE)
                 基于历史 trial 数据做纯数学优化
```

- **LLM 路径**：利用模型对 Redis/MySQL 参数的理解（如"增加 `io-threads` 可以提升并发吞吐，但可能增加延迟"），结合当前 trial 的分析结果做推理
- **Bayesian 回退**：当 LLM API 不可用时（断路、限流等），自动降级到 Gaussian Process + Expected Improvement（≤30 参数）或 TPE（30-100 参数）。根据参数维度**自动选择**最优贝叶斯后端
- 两个路径共享同一份 trial history，BO 从历史数据中 seed，做到无缝切换

### 3. 多 Agent 协作的安全审核链

每次参数变更在应用前必须通过**安全网关**：

```
Plan (提议变更) → Safety Agent (风险审核) → Apply (写入配置)
                       │
                       ├─ 检查是否涉及需要重启的参数
                       ├─ 检查风险等级是否超过用户设定上限
                       ├─ 检查连续回滚次数是否超阈值
                       ├─ 检查内存余量约束
                       └─ 输出: APPROVE / APPROVE_WITH_MODIFICATIONS / REJECT
```

被拒绝的提案会**带上修改建议**返回 Plan 节点重新生成。这套机制保证即使 LLM 提出激进或不合理的参数变更，也不会直接写到线上。

### 4. 完整的状态机与断点续跑

整个执行过程是一个 **LangGraph 状态机**，每个节点都是独立的 Agent：

| 节点 | 职责 | 用什么 |
|------|------|--------|
| **Initialize** | 连接目标实例，读取基线配置 | CustomDirectRunner (SSH/Shell) |
| **Plan** | 分析当前状态，提出参数变更 | HybridTuner (LLM + BO) |
| **Safety Check** | 审核变更风险 | SafetyAgent (LLM) |
| **Apply Config** | 写入新配置，必要时重启服务 | CustomDirectRunner |
| **Run Benchmark** | 执行用户提供的 benchmark 命令 | 用户自定义 shell 命令 |
| **Analyze** | 解析 benchmark 输出，对比历史 | AnalyzerAgent (LLM) + 输出解析器 |
| **Decide** | 判断继续/收敛/回滚 | OrchestratorAgent (LLM) |
| **Rollback** | 恢复上一次配置 | 本地 .bak 备份 |
| **Finalize** | 输出最终报告和最佳配置 | 格式化输出 |

每个 trial 完成后自动保存 state checkpoint 到磁盘，**进程崩溃后可以从断点恢复**，已完成的 trial 数据不丢失。

### 5. 可复用的调优 Profile

每次 intake 的结果自动保存为 YAML profile（密码等敏感字段自动脱敏）。下次调优同一个实例时，系统会提示：

> "找到已有调优配置：1. redis-127.0.0.1-6379 (127.0.0.1:6379)。回复 1 复用，或 skip 重新配置。"

Profile 存储在 `.agent/tuning/profiles/` 下，支持跨会话复用。

### 6. 通用的 Benchmark 抽象

不绑定特定 benchmark 工具。用户提供 shell 命令模板，系统自动填入参数：

```bash
# Redis 示例
redis-benchmark -h {host} -p {port} -c {clients} -n {requests} -t {tests} --csv

# MySQL 示例
sysbench oltp_read_write --mysql-host={host} --mysql-port={port} --time={duration} run
```

内置解析器支持 `redis-benchmark --csv`、sysbench、自定义正则。也支持**稳定模式**（warmup + 多次迭代取中位数）来消除噪声。

### 7. 安全约束体系

在执行层面有多层保护：

- **风险等级过滤**：每个参数预定义了 risk 等级（low/medium/high/critical），用户可以设置上限
- **Blocklist**：指定某些参数绝对不允许修改
- **重启控制**：`allow_restart=false` 时，所有需要重启才能生效的参数变更都会被拒绝
- **回滚保护**：连续回滚超过阈值（默认 3 次）自动停止
- **内存余量**：配置变更总内存不超过物理内存的 `(100 - memory_headroom_pct)%`
- **Dry run**：先跑一遍看看会改哪些参数，确认后再实际执行

### 8. 与 Nanobot 框架的深度集成

调优子系统不是独立的工具，而是**作为 Nanobot Agent 的一个能力存在**：

- 通过消息总线收发进度通知
- 结果自动归档到 MemoryStore，后续 Dream 合并时可以被写入 MEMORY.md / SOUL.md，形成长期记忆
- 支持所有 nanobot 频道（Telegram、Slack、Discord、WebUI 等），可以在手机上发起调优
- 与其他 agent 能力（文件操作、Web 搜索、Cron 等）共享同一个会话上下文

### 9. 鲁棒的 JSON 提取

LLM 输出的 JSON 经常有格式问题（尾部逗号、单引号、注释等）。系统用三层策略保障提取：

1. **平衡括号扫描**：逐字符数 `{`/`}` 深度，找到所有合法 JSON 候选，取最大者
2. **json_repair 修复**：处理常见的 LLM JSON 错误
3. **LLM 自修复**：如果前两层都失败，再调一次 LLM 说"请只输出 JSON 对象"

### 10. 向后兼容的旧系统支持

Redis 和 MySQL 的调优参数预置了完整的元数据（类型、范围、依赖关系、冲突关系、风险等级）。新增目标系统只需扩展参数 schema 即可。

---

## 数据流一览

```
User: "帮我把 Redis 7.2 在 10.0.0.5:6379 上调优到 8 万 QPS"
        │
        ▼
[TuningIntentRouter]  ← 关键词检测 + 会话状态机
        │
        ├─ 新请求 → 创建 TuningSession，启动 Intake
        ├─ Intake 中 → 继续多轮对话收集需求
        ├─ ERROR 状态 → 用户说 "retry" 则重试
        └─ 取消关键词 → 终止会话
        │
        ▼
[TuningSessionManager]
        │
        ├─ Intake 阶段: run_intake_turn()  → LLM 多轮对话 → TuningRequirements
        │      └─ 提取的结构化数据自动保存为 YAML profile
        │
        └─ Execution 阶段: run_execution() → _llm_tuner LangGraph workflow
               │
               ├─ 每个 trial: Plan → Safety → Apply → Benchmark → Analyze → Decide
               ├─ 每个 trial 结束: 保存 checkpoint 到磁盘
               └─ 最终: 格式化报告 → 归档到 MemoryStore → 通知用户
```

---

## 关键文件索引

| 组件 | 路径 |
|------|------|
| 调优生命周期管理 | `nanobot/agent/tuning/manager.py` |
| 需求收集 Agent | `nanobot/agent/tuning/intake.py` |
| 意图检测 & 路由 | `nanobot/agent/tuning/router.py` + `intent.py` |
| 执行引擎桥接 | `nanobot/agent/tuning/executor.py` |
| 数据模型 | `nanobot/agent/tuning/schema.py` |
| Profile 存储 | `nanobot/agent/tuning/profile_store.py` |
| LangGraph 工作流 | `_llm_tuner/src/workflow/graph.py` |
| 实验状态定义 | `_llm_tuner/src/workflow/state.py` |
| 混合优化器 | `_llm_tuner/src/optimization/hybrid_tuner.py` |
| 贝叶斯优化 | `_llm_tuner/src/optimization/bayesian.py` |
| 安全审核 Agent | `_llm_tuner/src/agents/safety_agent.py` |
| 编排决策 Agent | `_llm_tuner/src/agents/orchestrator.py` |
| 直接模式 Runner | `_llm_tuner/src/benchmark/custom_direct_runner.py` |
| 参数元数据管理 | `_llm_tuner/src/parameters/manager.py` |
