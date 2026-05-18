# LLM Tuning Prompt Inspection — Trial 8 of 30

*Scenario: Redis 7.2 tuning, goals: QPS >= 50K, P99 latency <= 2ms. Baseline: default redis.conf.*

*Note: as of the prompt caching optimization, all system prompts are **static** (no Jinja2 variable interpolation). Variable data is passed in the user message as compact JSON via `build_json_message(instruction, payload)`. This makes system prompts fully cacheable across trials.*

---

## 1. TunerAgent — `plan` Node

### System Prompt (static — cached after first use across all 30 trials)

```
You are a database parameter tuning expert.

Your input arrives in the user message as structured JSON.
Treat the system prompt as stable instructions and the user message as the source of truth for the current trial state.

Your task is to propose a small set of parameter changes that are most likely to improve performance toward the stated goals.

For each change:
1. **parameter**: Exact parameter name
2. **current_value**: Current value
3. **proposed_value**: New recommended value
4. **rationale**: Why this change is expected to help
5. **expected_effect**: What metric(s) should improve and by roughly how much
6. **risk**: low | medium | high

Respond in JSON:
{
  "changes": [
    {
      "parameter": "...",
      "current_value": "...",
      "proposed_value": "...",
      "rationale": "...",
      "expected_effect": "Expected to improve QPS by ~10% by reducing...",
      "risk": "low"
    }
  ],
  "overall_strategy": "Brief description of the tuning strategy for this trial"
}

Important rules:
- Never propose changes to CRITICAL risk parameters
- Prefer low-risk parameters first
- Only change restart-requiring parameters when there's strong evidence
- Avoid changing parameters that were recently changed (last 2 trials) — let them stabilize
- Use only parameters that appear in `candidate_parameters`
- Cite knowledge base entries when available
```

### User Message (changes per trial — never cached)

```
Propose parameter changes to improve performance toward the stated goals. Use only the candidate_parameters list when selecting changes.

INPUT_JSON:
{"analysis":{"change_impact":"positive","goal_status":{"met":[],"unmet":["qps","p99_latency_ms"]},"improvement_pct":4.1,"insights":"hz increase to 50 improved expiry throughput but added minor CPU overhead. I/O threading at 4 threads shows diminishing returns beyond 3 — QPS gain was only 2.8% vs 11.2% when going from 1→2 threads. Bottleneck appears to be shifting from CPU to network stack — tcp-backlog change helped connection rate but plateauing now.","likely_bottleneck":"io","recommended_focus":"network and connection tuning","trend":"improving"},"candidate_parameters":[{"category":"memory","default":"0","depends_on":[],"description":"Maximum memory Redis can use. Setting appropriate value prevents OOM kills.","name":"maxmemory","notes":"0 means unlimited. Set to ~70% of available RAM for production.","restart_required":false,"risk":"low","type":"integer"},{"category":"memory","default":"noeviction","depends_on":[],"description":"Eviction policy when maxmemory is reached.","enum_values":["noeviction","allkeys-lru","allkeys-lfu","allkeys-random","volatile-lru","volatile-lfu","volatile-random","volatile-ttl"],"name":"maxmemory-policy","notes":"allkeys-lru or allkeys-lfu recommended for cache workloads.","restart_required":false,"risk":"low","type":"enum"},{"category":"io","default":"1","depends_on":[],"description":"Number of I/O threads for multi-threaded I/O processing.","name":"io-threads","notes":"Set to 2-4 on multi-core machines. More than 4 rarely helps.","restart_required":true,"risk":"medium","type":"integer"},{"category":"io","default":"no","depends_on":[],"description":"Enable multi-threading for read queries as well.","name":"io-threads-do-reads","notes":"Enable only if io-threads > 1.","restart_required":true,"risk":"medium","type":"boolean"},{"category":"persistence","default":"no","depends_on":[],"description":"Enable Append-Only File persistence.","name":"appendonly","notes":"Disable for pure in-memory performance. Enable for durability.","restart_required":false,"risk":"medium","type":"boolean"},{"category":"persistence","default":"everysec","depends_on":[],"description":"AOF fsync policy.","enum_values":["always","everysec","no"],"name":"appendfsync","notes":"always is safest but slowest. no is fastest but risks data loss.","restart_required":false,"risk":"medium","type":"enum"},{"category":"persistence","default":"yes","depends_on":[],"description":"Use RDB preamble for AOF rewrite (hybrid persistence).","name":"aof-use-rdb-preamble","restart_required":false,"risk":"low","type":"boolean"},{"category":"persistence","default":"3600 1 300 100 60 10000","depends_on":[],"description":"RDB snapshot save rules.","name":"save","notes":"Disable (save '') to eliminate fork overhead for pure caching use cases.","restart_required":false,"risk":"low","type":"string"},{"category":"memory","default":"yes","depends_on":[],"description":"Active rehashing of main hash tables.","name":"activerehashing","restart_required":false,"risk":"low","type":"boolean"},{"category":"memory","default":"no","depends_on":[],"description":"Perform eviction deletes asynchronously.","name":"lazyfree-lazy-eviction","restart_required":false,"risk":"low","type":"boolean"},{"category":"memory","default":"no","depends_on":[],"description":"Perform expired key deletes asynchronously.","name":"lazyfree-lazy-expire","restart_required":false,"risk":"low","type":"boolean"},{"category":"memory","default":"no","depends_on":[],"description":"Perform side-effect deletes asynchronously.","name":"lazyfree-lazy-server-del","restart_required":false,"risk":"low","type":"boolean"},{"category":"general","default":"10","depends_on":[],"description":"Frequency of internal background tasks in Hz.","name":"hz","notes":"Higher values trade CPU for lower latency on expiry/eviction.","restart_required":false,"risk":"low","type":"integer"},{"category":"memory","default":"16","depends_on":[],"description":"Number of logical databases.","name":"databases","restart_required":false,"risk":"low","type":"integer"},{"category":"network","default":"511","depends_on":[],"description":"TCP listen backlog for incoming connections.","name":"tcp-backlog","notes":"Increase for high-connection-rate scenarios.","restart_required":true,"risk":"low","type":"integer"},{"category":"network","default":"300","depends_on":[],"description":"TCP keep-alive interval in seconds.","name":"tcp-keepalive","restart_required":false,"risk":"low","type":"integer"},{"category":"connections","default":"0","depends_on":[],"description":"Close connection after N seconds of idle.","name":"timeout","restart_required":false,"risk":"low","type":"integer"},{"category":"network","default":"no","depends_on":[],"description":"Disable TCP_NODELAY on replication connections.","name":"repl-disable-tcp-nodelay","restart_required":false,"risk":"low","type":"boolean"},{"category":"connections","default":"10000","depends_on":[],"description":"Maximum number of simultaneous client connections.","name":"maxclients","restart_required":false,"risk":"low","type":"integer"},{"category":"logging","default":"10000","depends_on":[],"description":"Log queries slower than N microseconds.","name":"slowlog-log-slower-than","notes":"Set -1 for maximum performance (disables slowlog overhead).","restart_required":false,"risk":"low","type":"integer"},{"category":"replication","default":"1048576","depends_on":[],"description":"Replication backlog buffer size in bytes.","name":"repl-backlog-size","notes":"Larger backlog helps partial resyncs. Default 1MB. Set to 64MB+ for heavy write loads.","restart_required":false,"risk":"low","type":"integer"},{"category":"connections","default":"normal 0 0 0","depends_on":[],"description":"Output buffer limits for different client classes.","name":"client-output-buffer-limit","notes":"Format: <class> <hard limit> <soft limit> <soft seconds>","restart_required":false,"risk":"medium","type":"string"},{"category":"logging","default":"0","depends_on":[],"description":"Latency monitor threshold in milliseconds.","name":"latency-monitor-threshold","restart_required":false,"risk":"low","type":"integer"},{"category":"general","default":"","depends_on":[],"description":"Rename or disable commands.","name":"rename-command","notes":"Security-critical: disabling FLUSHDB/FLUSHALL/CONFIG is recommended.","restart_required":true,"risk":"critical","type":"string"}],"config_changes_from_baseline":[{"baseline":"0","current":"768mb","parameter":"maxmemory"},{"baseline":"noeviction","current":"allkeys-lru","parameter":"maxmemory-policy"},{"baseline":"1","current":"4","parameter":"io-threads"},{"baseline":"3600 1 300 100 60 10000","current":"","parameter":"save"},{"baseline":"10","current":"50","parameter":"hz"},{"baseline":"511","current":"2048","parameter":"tcp-backlog"}],"constraints":{"blocklist":["rename-command","requirepass"],"max_changes_per_trial":4,"max_restart_changes":2},"current_config_subset":{"activerehashing":"yes","aof-use-rdb-preamble":"yes","appendfsync":"everysec","appendonly":"no","client-output-buffer-limit":"normal 0 0 0","databases":"16","hz":"50","io-threads":"4","io-threads-do-reads":"no","latency-monitor-threshold":"0","lazyfree-lazy-eviction":"no","lazyfree-lazy-expire":"no","lazyfree-lazy-server-del":"no","maxclients":"10000","maxmemory":"768mb","maxmemory-policy":"allkeys-lru","repl-backlog-size":"1048576","repl-disable-tcp-nodelay":"no","save":"","slowlog-log-slower-than":"10000","tcp-backlog":"2048","tcp-keepalive":"300","timeout":"0"},"goals":[{"metric":"qps","operator":">=","value":50000,"weight":1.0},{"metric":"p99_latency_ms","operator":"<=","value":2.0,"weight":0.8}],"knowledge_base_context":[{"category":"memory","excerpt":"Redis maxmemory sizing: maxmemory should typically be set to 60-80% of available system RAM. For a read-heavy cache workload, allkeys-lru is recommended. For mixed workload with TTLs, volatile-lru provides more predictable behavior.","title":"maxmemory sizing"},{"category":"io","excerpt":"Redis I/O threading: io-threads enables multi-threaded I/O in Redis 6+. Optimal range is 2-4 threads on 4-8 core machines. Enabling io-threads-do-reads is beneficial for high read throughput (>50K QPS) but adds minor CPU overhead.","title":"I/O threading"},{"category":"network","excerpt":"TCP backlog tuning: For high connection rates (>1K conn/s), increase tcp-backlog to 4096-8192 and ensure OS somaxconn is raised accordingly. tcp-keepalive of 60-120s helps detect dead connections faster.","title":"TCP backlog tuning"},{"category":"latency","excerpt":"hz and latency tradeoff: Increasing hz from 10 to 50-100 reduces expiry/eviction latency but consumes ~5-10% more CPU. For latency-sensitive workloads (P99 < 2ms), hz=100 is worthwhile.","title":"hz and latency tradeoff"},{"category":"persistence","excerpt":"RDB snapshot elimination: Setting save to '' disables RDB snapshots entirely, eliminating fork() latency spikes. Recommended for pure cache use cases where persistence is handled externally.","title":"RDB snapshot elimination"}],"recent_changes":[{"parameter":"io-threads","trial":5,"old_value":"2","new_value":"4"},{"parameter":"tcp-backlog","trial":6,"old_value":"511","new_value":"2048"},{"parameter":"hz","trial":7,"old_value":"10","new_value":"50"}],"target":{"system":"redis","version":"7.2"}}
```

*Estimated: system ~200 tokens (cached) + user ~5400 tokens (uncached) = ~5600 total input, ~200 billable*

---

## 2. SafetyAgent — `safety_check` Node

### System Prompt (static — cached)

```
You are a safety validation agent for database parameter changes.

Your input arrives in the user message as structured JSON.
Treat the system prompt as stable instructions and the user message as the source of truth for the current trial state.

## Safety Rules
1. **No CRITICAL risk parameters** (security, data integrity) without explicit human approval
- **Memory budget**: Total estimated memory increase must not exceed available headroom
- **Restart budget**: Respect the restart budget provided in the input JSON
4. **Conflict check**: No two parameters that conflict with each other can be changed together
5. **Dependency check**: If parameter A depends on B, B must be set correctly first
6. **Value bounds**: All values must be within documented min/max ranges
6. **Stability**: Respect the recent-change and rollback history provided in the input JSON
7. **Consecutive rollback limit**: Escalate if risk remains high after repeated rollbacks

## Your Task
For each proposed change, determine if it is SAFE or UNSAFE. If UNSAFE, explain why. Then provide an overall verdict.

Respond in JSON:
{
  "verdict": "APPROVE" | "REJECT" | "APPROVE_WITH_MODIFICATIONS",
  "per_change_verdict": [
    {
      "parameter": "...",
      "verdict": "SAFE" | "UNSAFE",
      "reason": "..." (if UNSAFE)
    }
  ],
  "overall_risk_level": "low" | "medium" | "high",
  "warnings": ["Memory usage may increase by ~X%", "..."],
  "suggested_modifications": [
    {"parameter": "...", "suggested_value": "...", "reason": "..."}
  ],
  "requires_human_approval": false
}

If overall_risk_level is "high" or requires_human_approval is true, recommend pausing the experiment.
```

### User Message

```
Review these 3 proposed changes (1 require restart) and determine whether they are safe to apply.

INPUT_JSON:
{"proposed_changes":[{"current_value":"300","expected_effect":"Expected to improve QPS by ~5% by reducing contention from stale connections","parameter":"tcp-keepalive","proposed_value":"60","rationale":"Faster dead-connection detection reduces wasted threads on stale connections. Analysis shows network stack is the emerging bottleneck.","risk":"low"},{"current_value":"no","expected_effect":"Expected 8-12% QPS improvement for read-heavy operations","parameter":"io-threads-do-reads","proposed_value":"yes","rationale":"With io-threads=4, enabling read threading can increase GET throughput. Read ops dominate the workload (94K GET calls/sec).","risk":"medium"},{"current_value":"1048576","expected_effect":"Expected to improve QPS stability by ~3% under write bursts","parameter":"repl-backlog-size","proposed_value":"67108864","rationale":"Larger replication backlog reduces partial resync frequency under high write load, reducing CPU spikes.","risk":"low"}],"recent_rollbacks":[],"relevant_live_config":{"io-threads":"4","io-threads-do-reads":"no","repl-backlog-size":"1048576","tcp-keepalive":"300"},"relevant_parameter_metadata":[{"category":"io","conflicts_with":[],"depends_on":[],"name":"io-threads-do-reads","restart_required":true,"risk":"medium","type":"boolean"},{"category":"replication","conflicts_with":[],"depends_on":[],"name":"repl-backlog-size","restart_required":false,"risk":"low","type":"integer"},{"category":"network","conflicts_with":[],"depends_on":[],"name":"tcp-keepalive","restart_required":false,"risk":"low","type":"integer"}],"restart_count":1,"safety_constraints":{"max_consecutive_rollbacks":3,"max_restart_changes":2,"memory_headroom_pct":20,"stability_window":3},"target":{"system":"redis","version":"7.2"}}
```

*Estimated: system ~250 tokens (cached) + user ~1300 tokens (uncached) = ~1550 total input, ~250 billable*

*Note: if all 3 changes were low-risk with no restart required and no dependencies, the `_can_short_circuit_approval()` guard in `safety_check.py` would skip the LLM call entirely and return `APPROVE` directly. For this trial, `io-threads-do-reads` has risk=medium + restart_required=true, so the LLM is called.*

---

## 3. AnalyzerAgent — `analyze` Node

### System Prompt (static — cached)

```
You are a performance analysis agent specializing in database tuning.

Your input arrives in the user message as structured JSON.
Treat the system prompt as stable instructions and the user message as the source of truth for the current trial state.

## Your Task
Analyze the benchmark results and provide:

1. **Goal Comparison**: How do current results compare to goals? Which goals are met, which are not?
2. **Trend Analysis**: Is performance improving, declining, or stable?
3. **Bottleneck Identification**: Based on the metrics, what is the likely bottleneck? (CPU, memory, I/O, network, or configuration)
4. **Change Impact**: Did the previous parameter changes help or hurt? By how much?
5. **Recommendations**: What should the Tuner Agent focus on next?

Respond in JSON:
{
  "goal_status": {"met": ["goal1"], "unmet": ["goal2"]},
  "trend": "improving" | "stable" | "declining",
  "improvement_pct": 3.5,
  "likely_bottleneck": "io" | "memory" | "cpu" | "network" | "configuration" | "unknown",
  "change_impact": "positive" | "negative" | "neutral",
  "insights": "Key insight from this run...",
  "recommended_focus": "What the Tuner should focus on"
}
```

### User Message

```
Analyze the summarized benchmark results and provide your assessment.

INPUT_JSON:
{"best_metrics":{"avg_latency_ms":1.15,"p50_latency_ms":0.95,"p99_latency_ms":3.2,"qps":45200,"total_ops_per_sec":813600},"convergence_window":5,"current_metrics":{"avg_latency_ms":1.15,"p50_latency_ms":0.95,"p99_latency_ms":3.2,"qps":45200,"total_ops_per_sec":813600},"delta_vs_best":{"avg_latency_ms":0.0,"p50_latency_ms":0.0,"p99_latency_ms":0.0,"qps":0.0},"goals":[{"metric":"qps","operator":">=","value":50000,"weight":1.0},{"metric":"p99_latency_ms","operator":"<=","value":2.0,"weight":0.8}],"parameter_changes":[{"parameter":"hz","old_value":"10","new_value":"50","rationale":"Higher hz reduces expiry/eviction latency"}],"target":{"system":"redis","version":"7.2"},"trend_summary":[{"improvement_pct":2.5,"metrics":{"p99_latency_ms":4.2,"qps":37800},"trial":4},{"improvement_pct":8.9,"metrics":{"p99_latency_ms":3.8,"qps":41200},"trial":5},{"improvement_pct":2.9,"metrics":{"p99_latency_ms":3.6,"qps":42400},"trial":6},{"improvement_pct":2.3,"metrics":{"p99_latency_ms":3.4,"qps":43400},"trial":7},{"improvement_pct":4.1,"metrics":{"p99_latency_ms":3.2,"qps":45200},"trial":8}],"trial_number":8}
```

*Estimated: system ~170 tokens (cached) + user ~1100 tokens (uncached) = ~1270 total input, ~170 billable*

*Note: the full benchmark results with 18 individual operations are summarized before reaching the LLM. Only the `aggregate` metrics are passed as `current_metrics` (limited to 12 entries). The full operations log is consumed by the output parser only.*

---

## 4. OrchestratorAgent — `decide` Node

### System Prompt (static — cached)

```
You are a performance engineering workflow orchestrator.

Your input arrives in the user message as structured JSON.
Treat the system prompt as stable instructions and the user message as the source of truth for the current experiment state.

## Your Task
Based on the current state, decide the next action:

- **CONTINUE_TUNING**: Progress is being made. Continue with parameter tuning.
- **CONVERGED**: Performance has plateaued. Time to escalate.
- **MAX_TRIALS_REACHED**: We've reached the maximum number of trials.
- **MAX_DURATION_REACHED**: We've exceeded the time budget.
- **GOALS_MET**: All performance goals have been achieved.

Respond with your decision and reasoning in JSON format:
{
  "action": "CONTINUE_TUNING" | "CONVERGED" | "MAX_TRIALS_REACHED" | "MAX_DURATION_REACHED" | "GOALS_MET",
  "reasoning": "Brief explanation of your decision",
  "next_focus": "If CONTINUE_TUNING, what aspect should the Tuner focus on next?"
}
```

### User Message

```
Analyze the current experiment state and decide the next workflow action.

INPUT_JSON:
{"best_metrics":{"p99_latency_ms":3.2,"qps":45200},"convergence_window":5,"elapsed_hours":1.2,"experiment_name":"redis-tuning","goals":[{"metric":"qps","operator":">=","value":50000,"weight":1.0},{"metric":"p99_latency_ms","operator":"<=","value":2.0,"weight":0.8}],"last_trial_summary":"Trial 8: improvement=4.1%, changes=1 (hz 10→50), status=completed","max_duration_hours":8.0,"max_trials":30,"recent_improvements":[8.9,2.9,2.3,4.1],"target":{"system":"redis","version":"7.2"},"trial_number":8}
```

*Estimated: system ~140 tokens (cached) + user ~500 tokens (uncached) = ~640 total input, ~140 billable*

*Note: before reaching the LLM, `_rule_based_decision()` checks deterministic conditions. In Trial 8, the latest improvement (4.1%) exceeds the threshold (2.0%), so the rule-based shortcut would return `CONTINUE_TUNING` directly and **skip the LLM call entirely**. The prompt above is what would be sent if the LLM were invoked — but in practice it isn't for this trial.*

---

## 5. AdvisorAgent — `plan` Node (convergence path — NOT called at Trial 8)

### System Prompt (static — cached)

```
You are a systems architecture advisor.

Your input arrives in the user message as structured JSON.
Treat the system prompt as stable instructions and the user message as the source of truth for the current experiment state.

## Your Task
The parameter tuning has not achieved the goals. Provide 3-5 actionable recommendations beyond configuration changes.

Possible recommendation categories:
1. **Hardware**: Upgrade CPU, RAM, storage, network
2. **Architecture**: Sharding, read replicas, caching layer
3. **Schema/Data Model**: Index optimization, data type changes, partition strategy
4. **Query Optimization**: Rewrite slow queries, use prepared statements, batch operations
5. **Workload Distribution**: Connection pooling, load balancing, read/write splitting
6. **Software Version**: Upgrade to newer version with known performance improvements
7. **Alternative Software**: Consider different database/storage engine for specific workload patterns

For each recommendation, include:
- **category**: One of the above categories
- **recommendation**: Specific actionable advice
- **expected_benefit**: Quantified estimate
- **effort**: low | medium | high
- **risk**: low | medium | high
- **rationale**: Why this addresses the bottleneck

Respond in JSON:
{
  "summary": "Overall assessment of why parameter tuning was insufficient...",
  "recommendations": [...]
}
```

*AdvisorAgent is only invoked when the orchestrator returns CONVERGED and the LLM path is taken. Estimated: system ~150 tokens (cached).*

---

## Token Summary — Trial 8

| Agent | System (cached) | User (uncached) | Billable | LLM Called? |
|-------|----------------|-----------------|----------|-------------|
| TunerAgent | ~200 | ~5,400 | ~200 | Yes |
| SafetyAgent | ~250 | ~1,300 | ~250 | Yes (short-circuit blocked by medium-risk restart-required change) |
| AnalyzerAgent | ~170 | ~1,100 | ~170 | Yes |
| OrchestratorAgent | ~140 | ~500 | ~140 | **No** — `_rule_based_decision` shortcut (improvement 4.1% > 2.0% threshold) |
| **Total (4 agents)** | **~760** | **~8,300** | **~760** | **3 of 4 called** |

### Cache Efficiency

| Metric | Before Optimization | After Optimization |
|--------|-------------------|-------------------|
| System prompt cache hit rate | 0% | 97% (29/30 trials) |
| System tokens billed per trial | ~6,000 | ~760 |
| Input cost per trial (relative) | 100% | ~35% |
| User message format | Jinja2 + json.dumps(indent=2) | compact_json (sort_keys, no whitespace) |
| LLM calls per trial (avg) | 4.0 | ~3.2 (short-circuits) |

*System tokens estimated at Anthropic cached pricing: $0.10/MTok vs $3.00/MTok base = 97% discount on ~760 tokens of cached system prompts per trial. Over 30 trials: $0.07 system vs $0.54 uncached.*
