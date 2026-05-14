# 13 - LLM 调优引擎产品能力缺口评估

## 评估方法

对 `_llm_tuner/` 和 `nanobot/agent/tuning/` 的完整代码审查，从产品化视角识别设计缺陷、功能缺失和工程短板。

---

## 一、致命缺口（阻塞生产部署）

### 1.1 无 API 层

`_llm_tuner/src/api/` 目录完全为空。整个调优引擎只能通过 CLI (`typer`) 和 nanobot 聊天界面使用，没有 REST API 或 gRPC 接口。

**缺失**:
- 启动实验的 API 端点
- 查询实验状态/进度的端点
- 取消运行中实验的端点
- 获取历史实验报告的端点
- WebSocket 实时进度推送

**影响**: 无法集成到 CI/CD、监控平台或自动化运维流程。

### 1.2 无持久化会话存储

`TuningSessionManager` 将所有会话保存在内存 `dict` 中：

```python
# manager.py
self._sessions: dict[str, TuningSession] = {}
```

**缺失**:
- 进程重启后所有进行中的调优会话丢失
- 无会话超时/过期机制，内存泄漏风险
- 无并发会话数量上限

**影响**: 生产环境中进程重启或崩溃导致用户体验断崖。

### 1.3 同步 Docker SDK 阻塞事件循环

`environment/manager.py` 使用同步 `docker-py` 库：

```python
# 这些调用在 async 上下文中阻塞事件循环
self.client.images.pull(...)
self.client.containers.run(...)
container.put_archive(...)
```

**缺失**:
- 未使用 `asyncio.to_thread()` 包装同步调用
- 多个并发实验会互相阻塞

**影响**: 单个 Docker 操作阻塞整个 nanobot 事件循环，导致所有聊天渠道停止响应。

### 1.4 无并发实验隔离

两个调优实验同时针对同一 Redis 实例时，配置变更互相覆盖：

**缺失**:
- 目标实例级别的分布式锁
- 事务隔离机制
- 冲突检测

**影响**: 并发调优导致数据损坏和错误结论。

---

## 二、功能性缺口

### 2.1 仅支持 Redis（名义上支持 MySQL）⚠️ 部分改进

意图路由仍限于 Redis 关键词，但：
- ✅ 基准测试通过 `CustomDirectRunner` 支持任意目标系统
- ✅ `mysql-sysbench.yaml` profile 已提供
- ✅ 不再硬编码 MySQL 容器密码（Docker 已废弃）
- 仍缺：`mysql_8.0.json` 参数 schema、MySQL 意图路由关键词

### 2.2 基准测试配置完全硬编码 ✅ 已解决

- ✅ `run_benchmark.py` 从 YAML profile 或 state 字段动态构建配置
- ✅ 用户通过对话收集 `run_command` 等命令模板
- ✅ `_llm_tuner/profiles/` 提供可复用的 YAML profile 示例
    "duration_sec": 30,
    "tests": ["set", "get"],  # 仅 Redis
}
```

**缺失**:
- 用户无法自定义负载模型（读写比例、key 分布、数据大小）
- 无法模拟真实业务负载
- 无基线基准测试（应先在原始配置下跑一次作为对比基准）
- `duration_sec=0` 默认值可能导致无限运行

### 2.3 无人工介入机制

工作流完全自动运行，无法在关键决策点暂停等待人工确认：

**缺失**:
- Safety Agent 拒绝变更后无法人工覆盖
- Orchestrator 决策无法人工干预
- 无法在 apply_config 前预览变更并确认
- 无 dry-run 模式预览完整调优计划

### 2.4 无调优范围控制

用户无法指定调优范围：

**缺失**:
- 无法限定只调某类参数（仅内存、仅网络）
- 无法设置参数变更预算上限（单次最多改几个）
- 无法标记"不可触碰"参数的 deny list（有 blocklist 字段但未被用户可配置地使用）
- 无法设置性能退化容忍上限

### 2.5 知识库无法扩展

ChromaDB 种子知识只有 21 条静态数据（9 Redis + 12 MySQL）：

**缺失**:
- 无知识反馈循环（调优结果不回写知识库）
- 用户无法添加自定义领域知识
- 无知识版本管理或 A/B 测试机制
- 知识条目无来源标注和置信度

### 2.6 实验对比能力缺失

每次调优实验独立，无法横向对比：

**缺失**:
- 无 A/B 对比报告
- 无跨实验趋势分析
- CSV/Parquet 数据导出
- `ReportGenerator` 类已实现但从未被 workflow 调用（死代码）

---

## 三、安全与可靠性缺口

### 3.1 安全门控全凭 LLM

`SafetyAgent` 是 LLM 判断，无硬性规则兜底：

**缺失**:
- 无硬编码的生产安全规则（如"绝不设置 `requirepass` 为空"）
- 无参数变更上限硬限制（如"内存类参数单次变更不超过 50%"）
- `ParameterDefinition.risk` 有 CRITICAL 级别但无对应的强制性阻止逻辑
- 无目标环境类型标注（生产/预发/测试），风险策略一刀切

### 3.2 回滚不完整 ✅ 已改进

`rollback_config` 现在通过 `CustomDirectRunner` 实现：
- 配置快照 + restart_command 恢复原始运行状态
- finalize 阶段执行 teardown_command 并恢复 baseline_config
- ✅ 回滚到基线能力
- ✅ 回滚后 health_check 验证
- 连续回滚计数仍仅在内存中（待持久化）

### 3.3 容错链路不闭环 ⚠️ 部分改进

已改进项：
- `apply_config.py` → 使用 CustomDirectRunner，回滚时 restore_snapshot
- `manager.py` → 异常处理保留 session 用于 retry
- ✅ Docker 容器泄漏已消除（Docker 路径已废弃）

仍待改进：
- `graph.py:_wrap_with_skills` Skill 钩子异常仍只记日志
- `sysbench.py` prepare 阶段输出仍丢弃

### 3.4 重试和限流机制未被集成 ✅ 已解决

`ResilienceManager` 已添加，按 provider 管理独立的 `RateLimiter` + `CircuitBreaker` 实例。`base.py` 已替换模块级单例为 per-provider 隔离。工作流节点已有 LLM 失败时的 fallback 路径（plan → Bayesian、decide → 规则决策）。
# RateLimiter — 定义了但从未实例化
# CircuitBreaker — 定义了但从未实例化
```

这些代码实际上没有被工作流中的任何 Agent 使用。

---

## 四、可观测性缺口

### 4.1 无指标暴露

**缺失**:
- Prometheus metrics（实验数、试验数、成功率、LLM 调用延迟、优化收敛曲线）
- OpenTelemetry tracing（跨 Agent 调用的分布式追踪）
- 结构化审计日志（谁、何时、做了什么变更）

### 4.2 进度不可见

用户发起调优后完全失去可见性：

**缺失**:
- `_spawn_background` 创建后台任务后用户只看到"运行中"
- 无法查询当前试验编号、改进率、预计剩余时间
- 无中间结果推送（仅最终报告通过 MessageBus 推送）

### 4.3 中断恢复不可靠

**缺失**:
- SQLite checkpoint 未在进程重启后重新加载
- checkpoint 路径 `data/workflow_checkpoints.db` 硬编码
- `_reconstruct_state` 只复制部分字段，新增字段会静默丢失
- LLM 中断摘要生成是 optional 的，失败时用固定文本替代

---

## 五、扩展性与多租户缺口

### 5.1 单目标系统限制

整个 `ExperimentState` 假设只有一个 target：

**缺失**:
- 无多目标系统同时调优
- 无分组实验（staging 先调、确认后再推生产）

### 5.2 无多租户隔离

**缺失**:
- 无用户认证
- 无 namespace/workspace 隔离
- 不同用户的实验数据混合在同一 SQLite 数据库

### 5.3 参数 Schema 版本管理缺失

**缺失**:
- Redis 大版本间参数 Schema 变化无法感知
- 无 Schema 版本号和兼容性矩阵
- `client-output-buffer-limit` 等复杂语法参数无结构化校验

---

## 六、优先级建议

### P0 — 阻塞生产（必须修复才能上线）

| # | 问题 | 建议方案 |
|---|------|----------|
| 1 | 无 API 层 | 基于 aiohttp 实现 REST API + WebSocket 进度推送 |
| 2 | 内存会话丢失 | Session 持久化到 SQLite/Redis，支持进程重启恢复 |
| 3 | Docker SDK 阻塞事件循环 | 所有 Docker 调用包装 `asyncio.to_thread()` |
| 4 | 无并发实验隔离 | 目标实例级别 `asyncio.Lock` + 数据库行锁 |

### P1 — 核心功能缺失（影响产品完整性）

| # | 问题 | 建议方案 |
|---|------|----------|
| 5 | MySQL 支持不可用 | 完成参数 Schema、Parser 校验、sysbench 清理逻辑 |
| 6 | 基准测试硬编码 | 支持 YAML/JSON 自定义 profile，自动基线测试 |
| 7 | 无人工介入 | LangGraph `interrupt` 机制实现 human-in-the-loop |
| 8 | 安全门控纯 LLM | 添加硬编码安全规则层（CRITICAL 参数门控、变更上限） |
| 9 | 重试/断路器未集成 | 将 `llm_resilience.py` 接入所有 Agent 调用路径 |

### P2 — 体验与运维（提升产品成熟度）

| # | 问题 | 建议方案 |
|---|------|----------|
| 10 | 无监控指标 | 接入 Prometheus + OpenTelemetry |
| 11 | 进度不可见 | 中间结果通过 MessageBus 实时推送 |
| 12 | 知识库不可扩展 | 调优结果自动回写，用户自定义知识入口 |
| 13 | 实验无法对比 | 多实验对比报告，数据导出 |
| 14 | 中断恢复不可靠 | 进程重启后自动加载 checkpoint，完整 state 序列化 |

### P3 — 长期竞争力

| # | 问题 | 建议方案 |
|---|------|----------|
| 15 | 单目标限制 | 多目标并行 + 分组实验 |
| 16 | 无多租户 | 用户认证 + namespace 隔离 |
| 17 | Schema 版本管理 | 参数 Schema 版本化 + 兼容性检测 |
