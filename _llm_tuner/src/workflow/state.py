"""Experiment state model — the central data contract for all agents and nodes."""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ExperimentPhase(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    PLANNING = "planning"
    SAFETY_CHECK = "safety_check"
    APPLYING_CONFIG = "applying_config"
    RUNNING_BENCHMARK = "running_benchmark"
    ANALYZING = "analyzing"
    DECIDING = "deciding"
    ROLLING_BACK = "rolling_back"
    ADVISING = "advising"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class GoalSpec(BaseModel):
    metric: str
    operator: str  # >=, <=, >, <, ==
    value: float
    weight: float = 1.0


class TrialResult(BaseModel):
    trial_number: int
    config: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    benchmark_results: list[dict] = Field(default_factory=list)
    parameter_changes: list[dict] = Field(default_factory=list)
    improvement_pct: float = 0.0
    status: str = "running"
    analysis: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExperimentState(BaseModel):
    """Central state object shared by all LangGraph nodes."""

    # Experiment identity
    experiment_id: str = ""
    experiment_name: str = ""
    target_system: str = ""
    target_version: str = ""

    # Goals
    goals: list[GoalSpec] = Field(default_factory=list)

    # Environment
    container_id: str = ""
    direct_mode: bool = True  # Always direct mode (Docker path is DEPRECATED)
    direct_config_path: str = ""
    direct_benchmark_cmd: str = ""

    # Generic connection details (replaces redis_host/redis_port/redis_password)
    target_host: str = "127.0.0.1"
    target_port: str = "6379"
    target_credentials: str = ""

    # DEPRECATED — kept for backward compat, mapped to target_* on init
    redis_host: str = ""
    redis_port: str = ""
    redis_password: str = ""

    # User-provided lifecycle commands (shell templates)
    start_command: str = ""
    run_command: str = ""
    teardown_command: str = ""
    health_check_command: str = ""
    restart_command: str = ""

    # Output parsing config
    output_format: str = "redis-benchmark-csv"
    metric_regex: dict[str, str] = Field(default_factory=dict)

    # Benchmark profile path (YAML)
    benchmark_profile_path: str = ""

    # Stability benchmark settings
    stable_mode: bool = False  # enable warmup + multi-iteration median
    stable_warmup_requests: int = 10000
    stable_iterations: int = 3
    current_config: dict[str, str] = Field(default_factory=dict)
    baseline_config: dict[str, str] = Field(default_factory=dict)
    hardware_spec: dict[str, Any] = Field(default_factory=dict)

    # Phase tracking
    phase: ExperimentPhase = ExperimentPhase.CREATED
    trial_number: int = 0
    max_trials: int = 30
    max_duration_hours: float = 8.0
    start_time: datetime | None = None
    elapsed_hours: float = 0.0

    # Trial data
    current_trial: TrialResult | None = None
    trial_history: list[TrialResult] = Field(default_factory=list)

    # Best results tracking
    best_metrics: dict[str, float] = Field(default_factory=dict)
    best_config: dict[str, str] = Field(default_factory=dict)
    best_trial_number: int = 0

    # Convergence
    convergence_window: int = 5
    improvement_threshold_pct: float = 2.0
    improvement_history: list[float] = Field(default_factory=list)

    # Safety
    max_changes_per_trial: int = 4
    allow_restart: bool = False
    max_restart_changes: int = 2
    max_risk_level: str = "medium"
    max_consecutive_rollbacks: int = 3
    consecutive_rollbacks: int = 0
    memory_headroom_pct: int = 20
    blocklist: list[str] = Field(default_factory=list)
    rollback_history: list[dict] = Field(default_factory=list)
    safety_warnings: list[str] = Field(default_factory=list)

    # Agent outputs
    orchestrator_decision: dict = Field(default_factory=dict)
    analysis_result: dict = Field(default_factory=dict)
    tuning_proposal: dict = Field(default_factory=dict)
    safety_verdict: dict = Field(default_factory=dict)
    advisor_recommendations: dict = Field(default_factory=dict)

    # Tunable parameters snapshot
    tunable_parameters: list[dict] = Field(default_factory=list)

    # Error tracking
    errors: list[str] = Field(default_factory=list)

    def begin_trial(
        self,
        config: dict[str, str],
        parameter_changes: list[dict[str, Any]] | None = None,
    ) -> TrialResult:
        """Start a new trial and make it the active trial."""
        self.trial_number += 1
        self.current_trial = TrialResult(
            trial_number=self.trial_number,
            config=copy.deepcopy(config),
            parameter_changes=copy.deepcopy(parameter_changes or []),
            status="running",
        )
        return self.current_trial

    def commit_current_trial(self, status: str | None = None) -> TrialResult | None:
        """Persist the active trial into history once and keep it addressable."""
        if self.current_trial is None:
            return None

        if status is not None:
            self.current_trial.status = status

        if not any(t.trial_number == self.current_trial.trial_number for t in self.trial_history):
            self.trial_history.append(self.current_trial)

        return self.current_trial

    def goal_met(self, metric_name: str, value: float) -> bool:
        """Check if a metric value meets its goal."""
        for goal in self.goals:
            if goal.metric == metric_name:
                if goal.operator == ">=":
                    return value >= goal.value
                elif goal.operator == "<=":
                    return value <= goal.value
                elif goal.operator == ">":
                    return value > goal.value
                elif goal.operator == "<":
                    return value < goal.value
                elif goal.operator == "==":
                    return value == goal.value
        return False

    def all_goals_met(self) -> bool:
        """Check if all goals are met by the best metrics."""
        if not self.best_metrics:
            return False
        return all(
            self.goal_met(goal.metric, self.best_metrics.get(goal.metric, 0))
            for goal in self.goals
        )

    def compute_improvement(self, new_metrics: dict[str, float]) -> float:
        """Compute weighted improvement over best previous metrics."""
        if not self.best_metrics:
            return 100.0  # No baseline — first trial is 100% improvement

        total_weight = sum(g.weight for g in self.goals)
        if total_weight == 0:
            return 0.0

        improvement_sum = 0.0
        for goal in self.goals:
            old_val = self.best_metrics.get(goal.metric, 0)
            new_val = new_metrics.get(goal.metric, 0)
            if old_val == 0:
                pct_change = 100.0 if new_val > 0 else 0.0
            else:
                # For "more is better" (>=), positive change is improvement
                # For "less is better" (<=), negative change is improvement
                if goal.operator in (">=", ">"):
                    pct_change = ((new_val - old_val) / old_val) * 100
                else:
                    pct_change = ((old_val - new_val) / old_val) * 100

            improvement_sum += pct_change * goal.weight

        return improvement_sum / total_weight

    def has_converged(self) -> bool:
        """Check if recent improvements are below threshold."""
        if len(self.improvement_history) < self.convergence_window:
            return False
        recent = self.improvement_history[-self.convergence_window:]
        return all(abs(imp) < self.improvement_threshold_pct for imp in recent)
