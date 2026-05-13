"""Data models for tuning agent sessions and requirements."""

from __future__ import annotations

import enum
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
    _intake_conversation: list[dict[str, Any]] = field(default_factory=list)
