"""Bayesian Optimization for database parameter tuning.

Uses scikit-optimize (skopt) with Gaussian Process + Expected Improvement.
Best for 1-30 dimensional parameter spaces where sample efficiency is critical.

Supports two modes:

1.  ``propose_next()``  — fit GP on history, suggest one next config.
2.  ``optimize()``      — full BO loop (batch / standalone use).
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from skopt import Optimizer
from skopt.space import Real, Integer, Categorical
from skopt.space.space import (
    Categorical as CategoricalSpace,
    Integer as IntegerSpace,
    Real as RealSpace,
)

from src.parameters.schema import ParameterDefinition
from src.utils.logging import get_logger

logger = get_logger(__name__)


def _param_to_dimension(p: ParameterDefinition):
    if p.type == "integer":
        lo = int(p.min_value) if p.min_value is not None else 0
        hi = int(p.max_value) if p.max_value is not None else 2**31 - 1
        return Integer(lo, hi, name=p.name)
    elif p.type == "float":
        lo = float(p.min_value) if p.min_value is not None else 0.0
        hi = float(p.max_value) if p.max_value is not None else 1e9
        return Real(lo, hi, name=p.name)
    elif p.type == "enum" and p.enum_values:
        return Categorical(p.enum_values, name=p.name)
    elif p.type == "boolean":
        return Categorical(["yes", "no"], name=p.name)
    else:
        return Categorical([p.default_value], name=p.name)


def _param_to_value(p: ParameterDefinition, raw_value) -> str:
    if p.type == "integer":
        return str(int(raw_value))
    elif p.type == "float":
        return str(float(raw_value))
    else:
        return str(raw_value)


class BayesianOptimizer:
    """Gaussian Process Bayesian Optimization for database parameter tuning."""

    def __init__(
        self,
        param_defs: list[ParameterDefinition],
        objective_fn: Callable[[dict[str, str]], Awaitable[float]] | None = None,
        n_initial_points: int = 5,
        acquisition: str = "EI",
        random_state: int = 42,
        **__kwargs: Any,
    ):
        self.param_defs = param_defs
        self._objective = objective_fn
        self.n_initial_points = n_initial_points
        self.acquisition = acquisition
        self.random_state = random_state

        self.dimensions = [_param_to_dimension(p) for p in param_defs]
        self._name_to_idx: dict[str, int] = {
            p.name: i for i, p in enumerate(param_defs)
        }

        self._optimizer: Optimizer | None = None
        self._seed_count: int = 0

        self.best_config: dict[str, str] | None = None
        self.best_score: float = float("-inf")
        self.iteration_log: list[dict] = []

    def seed_from_history(
        self, trial_history: list[dict], maximize: bool = True
    ) -> int:
        X: list[list] = []
        y: list[float] = []

        for trial in trial_history:
            config = trial.get("config", {})
            metrics = trial.get("metrics", {})
            score = self._extract_score(metrics, maximize)
            if score is None:
                continue
            point = self._config_to_point(config)
            if point is None:
                continue
            X.append(point)
            y.append(-score if maximize else score)

        if not X:
            logger.warning("bayesian optimizer: no valid seed points from history")
            return 0

        self._optimizer = Optimizer(
            dimensions=self.dimensions,
            base_estimator="GP",
            acq_func=self.acquisition,
            acq_optimizer="sampling",
            n_initial_points=0,
            random_state=self.random_state,
        )

        for xi, yi in zip(X, y):
            self._optimizer.tell(xi, yi)

        for i, yi in enumerate(y):
            actual_score = -yi if maximize else yi
            if actual_score > self.best_score:
                self.best_score = actual_score

        self._seed_count = len(X)
        logger.info("bayesian optimizer seeded", backend="GP+EI", trials=self._seed_count)
        return self._seed_count

    def propose_next(self, maximize: bool = True, n_restarts: int = 5) -> dict | None:
        if self._optimizer is None or self._seed_count < 2:
            return None

        next_x = self._optimizer.ask(n_points=n_restarts)
        if isinstance(next_x, list) and isinstance(next_x[0], list):
            next_x = next_x[0]

        config = self._point_to_config(next_x)

        changes = [
            {
                "parameter": name,
                "proposed_value": value,
                "rationale": "Bayesian optimization (GP + Expected Improvement)",
                "expected_effect": "Predicted to improve objective",
                "risk": "low",
            }
            for name, value in config.items()
        ]

        return {
            "changes": changes,
            "overall_strategy": (
                f"Bayesian optimization proposes {len(changes)} parameter changes "
                f"based on {self._seed_count} historical trials"
            ),
            "_source": "bayesian",
        }

    def propose_changes_diff(
        self,
        current_config: dict[str, str],
        maximize: bool = True,
        n_restarts: int = 5,
        max_changes: int = 4,
    ) -> dict:
        proposal = self.propose_next(maximize=maximize, n_restarts=n_restarts)
        if proposal is None:
            return {
                "changes": [],
                "overall_strategy": "Insufficient data for Bayesian proposal",
                "_source": "bayesian",
            }

        diff_changes = []
        for c in proposal["changes"]:
            name = c["parameter"]
            new_val = str(c["proposed_value"])
            old_val = current_config.get(name)
            if old_val is None or old_val != new_val:
                c["current_value"] = old_val
                diff_changes.append(c)

        diff_changes = diff_changes[:max_changes]
        proposal["changes"] = diff_changes
        if not diff_changes:
            proposal["overall_strategy"] = "Bayesian optimization found no beneficial changes"
        return proposal

    async def optimize(
        self, maximize: bool = True, n_calls: int = 30, verbose: bool = True
    ) -> dict:
        if self._objective is None:
            raise ValueError("No objective_fn set")

        if self._optimizer is None:
            self._optimizer = Optimizer(
                dimensions=self.dimensions,
                base_estimator="GP",
                acq_func=self.acquisition,
                n_initial_points=self.n_initial_points,
                random_state=self.random_state,
            )

        sign = -1.0 if maximize else 1.0
        n_existing = len(self._optimizer.yi) if self._optimizer.yi else 0
        n_new = max(0, n_calls - n_existing)

        logger.info(
            "starting bayesian optimization loop",
            backend="GP+EI",
            existing_points=n_existing,
            new_calls=n_new,
            dimensions=len(self.dimensions),
        )

        for i in range(n_new):
            next_x = self._optimizer.ask()
            config = self._point_to_config(next_x)
            score = await self._objective(config)
            y = sign * score
            self._optimizer.tell(next_x, y)

            self.iteration_log.append({
                "config": dict(config),
                "score": score,
                "iteration": len(self.iteration_log),
            })

            if score > self.best_score:
                self.best_score = score
                self.best_config = dict(config)

            if verbose:
                logger.info(
                    "bo iteration",
                    iter=len(self.iteration_log),
                    score=f"{score:.1f}",
                    best=f"{self.best_score:.1f}",
                )

        return {
            "best_config": self.best_config or {},
            "best_score": self.best_score,
            "n_seed": self._seed_count,
            "n_total": len(self.iteration_log),
            "iteration_log": self.iteration_log,
        }

    def _extract_score(self, metrics: dict[str, float], maximize: bool) -> float | None:
        candidates = ["score_pct", "qps", "total_rps"]
        for key in candidates:
            if key in metrics:
                return float(metrics[key])
        for v in metrics.values():
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def _config_to_point(self, config: dict[str, str]) -> list | None:
        point = []
        for i, dim in enumerate(self.dimensions):
            name = dim.name
            raw = config.get(name, self.param_defs[i].default_value)
            try:
                if isinstance(dim, CategoricalSpace):
                    value = raw
                elif isinstance(dim, IntegerSpace):
                    value = int(raw)
                else:
                    value = float(raw)
            except (ValueError, TypeError):
                return None

            if isinstance(dim, CategoricalSpace):
                if value not in dim.categories:
                    return None
            elif isinstance(dim, IntegerSpace):
                if not (dim.low <= int(value) <= dim.high):
                    return None
            elif isinstance(dim, RealSpace):
                if not (dim.low <= float(value) <= dim.high):
                    return None

            point.append(value)
        return point

    def _point_to_config(self, point: list) -> dict[str, str]:
        config: dict[str, str] = {}
        for i, val in enumerate(point):
            pd = self.param_defs[i]
            config[pd.name] = _param_to_value(pd, val)
        return config

    def summary(self) -> str:
        lines = [
            f"Bayesian Optimization Summary (GP+EI)",
            f"  Dimensions: {len(self.dimensions)}",
            f"  Seed points: {self._seed_count}",
            f"  BO iterations: {len(self.iteration_log)}",
            f"  Best score: {self.best_score:.1f}",
            f"  Best config:",
        ]
        if self.best_config:
            for k, v in self.best_config.items():
                lines.append(f"    {k} = {v}")
        return "\n".join(lines)
