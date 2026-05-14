"""Data models for tuning agent sessions and requirements."""

from __future__ import annotations

import enum
from dataclasses import asdict
from dataclasses import dataclass, field
from typing import Any


class TuningPhase(enum.Enum):
    INTAKE = "intake"
    EXECUTION = "execution"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class TuningGoal:
    metric: str
    operator: str  # ">=", "<=", ">", "<", "=="
    value: float
    weight: float = 1.0


@dataclass(slots=True)
class TuningRequirements:
    """Structured requirements extracted by the intake agent."""

    target_system: str = ""  # "redis" or "mysql"
    target_version: str = ""
    goals: list[TuningGoal] = field(default_factory=list)

    # Connection (direct mode)
    host: str = ""
    port: str = ""
    password: str = ""
    config_file: str = ""

    # Lifecycle commands (shell templates)
    start_command: str = ""
    run_command: str = ""
    teardown_command: str = ""
    health_check_command: str = ""
    restart_command: str = ""

    # Output parsing
    output_format: str = "redis-benchmark-csv"
    metric_regex: dict[str, str] = field(default_factory=dict)

    # Benchmark profile (YAML path)
    benchmark_profile_path: str = ""

    # Stability settings
    stable_mode: bool = False
    stable_warmup_requests: int = 10000
    stable_iterations: int = 3

    # Constraints
    allow_restart: bool = False
    max_restart_changes: int = 2
    max_risk_level: str = "medium"  # low / medium / high
    blocked_parameters: list[str] = field(default_factory=list)
    memory_headroom_pct: int = 20

    # Optimization
    max_trials: int = 30
    max_duration_hours: float = 8.0
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TuningRequirements":
        goals = [
            TuningGoal(
                metric=g.get("metric", "qps"),
                operator=g.get("operator", ">="),
                value=float(g.get("value", 0)),
                weight=float(g.get("weight", 1.0)),
            )
            for g in data.get("goals", [])
        ]
        return cls(
            target_system=data.get("target_system", ""),
            target_version=data.get("target_version", ""),
            goals=goals,
            host=data.get("host", ""),
            port=str(data.get("port", "")),
            password=data.get("password", ""),
            config_file=data.get("config_file", ""),
            start_command=data.get("start_command", ""),
            run_command=data.get("run_command", ""),
            teardown_command=data.get("teardown_command", ""),
            health_check_command=data.get("health_check_command", ""),
            restart_command=data.get("restart_command", ""),
            output_format=data.get("output_format", "redis-benchmark-csv"),
            metric_regex=data.get("metric_regex", {}),
            benchmark_profile_path=data.get("benchmark_profile_path", ""),
            stable_mode=data.get("stable_mode", False),
            stable_warmup_requests=int(data.get("stable_warmup_requests", 10000)),
            stable_iterations=int(data.get("stable_iterations", 3)),
            allow_restart=data.get("allow_restart", False),
            max_restart_changes=int(data.get("max_restart_changes", 2)),
            max_risk_level=data.get("max_risk_level", "medium"),
            blocked_parameters=list(data.get("blocked_parameters", [])),
            memory_headroom_pct=int(data.get("memory_headroom_pct", 20)),
            max_trials=int(data.get("max_trials", 30)),
            max_duration_hours=float(data.get("max_duration_hours", 8.0)),
            dry_run=data.get("dry_run", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_experiment_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for building ExperimentState."""
        goals_dicts = [
            {
                "metric": g.metric,
                "operator": g.operator,
                "value": g.value,
                "weight": g.weight,
            }
            for g in self.goals
        ]
        return {
            "name": f"{self.target_system}-tuning",
            "target_system": self.target_system,
            "target_version": self.target_version,
            "goals": goals_dicts,
            "optimization": {
                "max_trials": self.max_trials,
                "max_duration_hours": self.max_duration_hours,
                "parameter_focus": {
                    "blocklist": self.blocked_parameters,
                },
            },
            "safety": {
                "max_restart_requiring_changes": self.max_restart_changes,
                "memory_headroom_pct": self.memory_headroom_pct,
            },
        }


@dataclass(slots=True)
class TuningSession:
    """Runtime state for a single tuning session."""

    task_id: str
    task_description: str
    phase: TuningPhase = TuningPhase.INTAKE
    requirements: TuningRequirements = field(default_factory=TuningRequirements)
    progress_messages: list[str] = field(default_factory=list)
    final_report: str = ""
    error: str = ""

    # Structured tuning results (populated after execution)
    best_config: dict[str, str] = field(default_factory=dict)
    best_metrics: dict[str, float] = field(default_factory=dict)
    improvement_history: list[float] = field(default_factory=list)
    trials_completed: int = 0
    reuse_candidates: list[dict[str, Any]] = field(default_factory=list)
    awaiting_profile_selection: bool = False

    _intake_conversation: list[dict[str, Any]] = field(default_factory=list)
