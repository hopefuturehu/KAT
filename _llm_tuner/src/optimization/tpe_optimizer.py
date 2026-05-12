"""TPE (Tree-structured Parzen Estimator) optimizer via Optuna.

Drop-in replacement for ``BayesianOptimizer`` with the same interface.
Automatically handles:
  - Large-range integer parameters (log-scale when range > 1000)
  - Mixed continuous / categorical / boolean spaces
  - Higher-dimensional spaces where GP+EI degrades

Interface (identical to BayesianOptimizer):
  - seed_from_history(trial_history, maximize) -> int
  - propose_next(maximize) -> dict | None
  - propose_changes_diff(current_config, ...) -> dict
  - optimize(maximize, n_calls) -> dict
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

import optuna
import optuna.trial
from optuna.samplers import TPESampler

from src.parameters.schema import ParameterDefinition
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ── Distribution builders ───────────────────────────────────────────────────


def _build_distributions(
    param_defs: list[ParameterDefinition],
) -> dict[str, optuna.distributions.BaseDistribution]:
    dists: dict[str, optuna.distributions.BaseDistribution] = {}
    for p in param_defs:
        try:
            dists[p.name] = _param_to_distribution(p)
        except ValueError:
            logger.debug("skipping parameter for TPE", name=p.name, type=p.type)
    return dists


def _param_to_distribution(p: ParameterDefinition) -> optuna.distributions.BaseDistribution:
    if p.type == "integer":
        lo = int(p.min_value) if p.min_value is not None else 0
        hi = int(p.max_value) if p.max_value is not None else 2**31 - 1

        # Auto log-scale for large spans
        if hi - lo > 1000:
            return optuna.distributions.IntLogUniformDistribution(lo, hi)
        return optuna.distributions.IntDistribution(lo, hi)

    elif p.type == "float":
        lo = float(p.min_value) if p.min_value is not None else 0.0
        hi = float(p.max_value) if p.max_value is not None else 1e9
        return optuna.distributions.FloatDistribution(lo, hi)

    elif p.type == "enum" and p.enum_values:
        return optuna.distributions.CategoricalDistribution(list(p.enum_values))

    elif p.type == "boolean":
        return optuna.distributions.CategoricalDistribution(["yes", "no"])

    else:
        raise ValueError(f"unsupported parameter type for TPE: {p.type}")


def _value_to_param(value_str: str, dist: optuna.distributions.BaseDistribution):
    """Convert a config-string value to the native type expected by a distribution."""
    if isinstance(dist, optuna.distributions.IntDistribution):
        return int(value_str)
    elif isinstance(dist, optuna.distributions.IntLogUniformDistribution):
        return int(value_str)
    elif isinstance(dist, optuna.distributions.FloatDistribution):
        return float(value_str)
    elif isinstance(dist, optuna.distributions.CategoricalDistribution):
        return value_str
    return value_str


# ── TPE Optimizer ───────────────────────────────────────────────────────────


class TPEOptimizer:
    """TPE-based optimizer using Optuna, with the same interface as BayesianOptimizer.

    Automatically switches to log-scale for integer parameters with range > 1000.
    Handles mixed continuous / categorical spaces natively — no special handling needed.
    """

    def __init__(
        self,
        param_defs: list[ParameterDefinition],
        objective_fn: Callable[[dict[str, str]], Awaitable[float]] | None = None,
        n_initial_points: int = 10,
        random_state: int = 42,
        **__kwargs: Any,  # accept and ignore extra kwargs from selector
    ):
        self.param_defs = param_defs
        self._objective = objective_fn
        self.n_initial_points = n_initial_points
        self.random_state = random_state

        self._distributions = _build_distributions(param_defs)
        self._study: optuna.Study | None = None
        self._seed_count: int = 0

        self.best_config: dict[str, str] | None = None
        self.best_score: float = float("-inf")
        self.iteration_log: list[dict] = []

    # ── Seed from history ─────────────────────────────────────────────────

    def seed_from_history(
        self,
        trial_history: list[dict],
        maximize: bool = True,
    ) -> int:
        """Feed historical trials into the TPE prior."""
        self._study = optuna.create_study(
            sampler=TPESampler(
                seed=self.random_state,
                n_startup_trials=min(self.n_initial_points, len(trial_history)),
            ),
            direction="maximize" if maximize else "minimize",
        )

        self._seed_count = 0
        for trial in trial_history:
            config = trial.get("config", {})
            metrics = trial.get("metrics", {})

            score = self._extract_score(metrics, maximize)
            if score is None:
                continue

            params = self._config_to_params(config)
            if not params:
                continue

            try:
                t = optuna.trial.create_trial(
                    params=params,
                    distributions=self._distributions,
                    value=float(score),
                )
                self._study.add_trial(t)
                self._seed_count += 1

                actual = score  # score already in study direction
                if actual > self.best_score:
                    self.best_score = float(actual)
                    self.best_config = {
                        name: str(params.get(name, ""))
                        for name in self._distributions
                    }
            except Exception as exc:
                logger.debug("skipping invalid trial for TPE seeding", error=str(exc))
                continue

        logger.info(
            "tpe optimizer seeded",
            backend="TPE",
            trials=self._seed_count,
            dims=len(self._distributions),
        )
        return self._seed_count

    # ── Single-step proposal ─────────────────────────────────────────────

    def propose_next(
        self,
        maximize: bool = True,
        n_restarts: int = 5,
    ) -> dict | None:
        """Suggest the next config via TPE.

        Returns same format as ``BayesianOptimizer.propose_next()``.
        """
        if self._study is None or self._seed_count < 2:
            return None

        if len(self._distributions) == 0:
            return None

        try:
            trial = self._study.ask(self._distributions)
        except Exception as exc:
            logger.warning("TPE ask failed", error=str(exc))
            return None

        changes = []
        for name, value in trial.params.items():
            changes.append({
                "parameter": name,
                "proposed_value": str(value),
                "rationale": "TPE optimization (likelihood-ratio density estimation)",
                "expected_effect": "Predicted via TPE to improve objective",
                "risk": "low",
            })

        return {
            "changes": changes,
            "overall_strategy": (
                f"TPE optimization proposes {len(changes)} parameter changes "
                f"based on {self._seed_count} historical trials"
            ),
            "_source": "bayesian",
        }

    # ── Diff helper ──────────────────────────────────────────────────────

    def propose_changes_diff(
        self,
        current_config: dict[str, str],
        maximize: bool = True,
        n_restarts: int = 5,
        max_changes: int = 4,
    ) -> dict:
        """Like *propose_next* but returns only differing parameters."""
        proposal = self.propose_next(maximize=maximize, n_restarts=n_restarts)
        if proposal is None:
            return {
                "changes": [],
                "overall_strategy": "Insufficient data for TPE proposal",
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
            proposal["overall_strategy"] = "TPE found no beneficial changes"
        return proposal

    # ── Full optimization loop (batch / standalone use) ─────────────────

    async def optimize(
        self,
        maximize: bool = True,
        n_calls: int = 30,
        verbose: bool = True,
    ) -> dict:
        """Run full TPE optimization loop via ask/tell.

        Requires *objective_fn* to have been set at init time.
        """
        if self._objective is None:
            raise ValueError("No objective_fn set — pass one to TPEOptimizer(...)")

        if self._study is None:
            self._study = optuna.create_study(
                sampler=TPESampler(
                    seed=self.random_state,
                    n_startup_trials=self.n_initial_points,
                ),
                direction="maximize" if maximize else "minimize",
            )

        n_existing = len(self._study.trials)
        n_new = max(0, n_calls - n_existing)

        logger.info(
            "starting tpe optimization loop",
            backend="TPE",
            existing_points=n_existing,
            new_calls=n_new,
            dimensions=len(self._distributions),
        )

        for i in range(n_new):
            trial = self._study.ask(self._distributions)
            config = {name: str(val) for name, val in trial.params.items()}

            score = await self._objective(config)

            sign = 1.0 if maximize else -1.0
            self._study.tell(trial, sign * score)

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
                    "tpe iteration",
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

    def summary(self) -> str:
        lines = [
            f"TPE Optimization Summary",
            f"  Dimensions: {len(self._distributions)}",
            f"  Seed points: {self._seed_count}",
            f"  Iterations: {len(self.iteration_log)}",
            f"  Best score: {self.best_score:.1f}",
            f"  Best config:",
        ]
        if self.best_config:
            for k, v in self.best_config.items():
                lines.append(f"    {k} = {v}")
        return "\n".join(lines)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _extract_score(
        self, metrics: dict[str, float], maximize: bool
    ) -> float | None:
        candidates = ["score_pct", "qps", "total_rps"]
        for key in candidates:
            if key in metrics:
                return float(metrics[key])
        for v in metrics.values():
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def _config_to_params(self, config: dict[str, str]) -> dict[str, Any]:
        """Convert config string dict to typed params dict for optuna."""
        params: dict[str, Any] = {}
        for name, dist in self._distributions.items():
            if name not in config:
                continue
            try:
                value = _value_to_param(config[name], dist)
                # Validate value is within distribution bounds
                _check_value_in_distribution(value, dist)
                params[name] = value
            except (ValueError, TypeError):
                continue
        return params


def _check_value_in_distribution(value, dist) -> None:
    """Raise ValueError if *value* is not valid for *dist*."""
    if isinstance(dist, optuna.distributions.IntDistribution):
        if not (dist.low <= int(value) <= dist.high):
            raise ValueError(f"{value} not in [{dist.low}, {dist.high}]")
    elif isinstance(dist, optuna.distributions.IntLogUniformDistribution):
        if not (dist.low <= int(value) <= dist.high):
            raise ValueError(f"{value} not in [{dist.low}, {dist.high}]")
    elif isinstance(dist, optuna.distributions.FloatDistribution):
        if not (dist.low <= float(value) <= dist.high):
            raise ValueError(f"{value} not in [{dist.low}, {dist.high}]")
    elif isinstance(dist, optuna.distributions.CategoricalDistribution):
        if value not in dist.choices:
            raise ValueError(f"{value} not in {dist.choices}")
