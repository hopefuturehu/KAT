"""Auto-select the optimal Bayesian optimization backend based on parameter count.

Thresholds (configurable via settings):

    params ≤ LLMTUNER_BO_GP_MAX_DIMS     → GP+EI  (skopt)
    params ≤ LLMTUNER_BO_TPE_MAX_DIMS    → TPE    (Optuna)
    params >  LLMTUNER_BO_TPE_MAX_DIMS   → TPE + warning (consider multi-fidelity)

Rationale:
  - GP+EI:   best sample-efficiency for 1-30 dimensions; O(n³) fit cost
  - TPE:     handles 30-100 dimensions well; O(n·log n); native categorical support
  - TPE+:    >100 dims needs dimensionality reduction or multi-fidelity pruning
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable
from src.config import settings
from src.parameters.schema import ParameterDefinition
from src.utils.logging import get_logger

logger = get_logger(__name__)


def select_backend(
    param_defs: list[ParameterDefinition],
    objective_fn: Callable[[dict[str, str]], Awaitable[float]] | None = None,
    **kwargs: Any,
):
    """Return the appropriate optimizer instance based on parameter count.

    Returns an object with this interface::

        optimizer.seed_from_history(trial_history, maximize)
        optimizer.propose_next(maximize) -> dict | None
        optimizer.propose_changes_diff(current_config, ...) -> dict
        optimizer.optimize(maximize, n_calls) -> dict

    Args:
        param_defs: Parameter definitions from the schema.
        objective_fn: Optional async benchmark function for full-loop optimization.
        **kwargs: Forwarded to the backend constructor (n_initial_points, random_state, ...).
    """
    n_params = len(param_defs)
    gp_max = settings.bo_gp_max_dims
    tpe_max = settings.bo_tpe_max_dims

    if n_params <= gp_max:
        backend = "GP+EI"
        from src.optimization.bayesian import BayesianOptimizer
        optimizer = BayesianOptimizer(
            param_defs=param_defs,
            objective_fn=objective_fn,
            **kwargs,
        )

    elif n_params <= tpe_max:
        backend = "TPE"
        try:
            from src.optimization.tpe_optimizer import TPEOptimizer
            optimizer = TPEOptimizer(
                param_defs=param_defs,
                objective_fn=objective_fn,
                **kwargs,
            )
        except ImportError:
            logger.warning(
                "optuna not installed — falling back to GP+EI for %d params", n_params
            )
            from src.optimization.bayesian import BayesianOptimizer
            backend = "GP+EI (fallback)"
            optimizer = BayesianOptimizer(
                param_defs=param_defs,
                objective_fn=objective_fn,
                **kwargs,
            )

    else:
        backend = "TPE (high-dim)"
        try:
            from src.optimization.tpe_optimizer import TPEOptimizer
            optimizer = TPEOptimizer(
                param_defs=param_defs,
                objective_fn=objective_fn,
                **kwargs,
            )
        except ImportError:
            logger.warning(
                "optuna not installed — falling back to GP+EI for %d params", n_params
            )
            from src.optimization.bayesian import BayesianOptimizer
            backend = "GP+EI (fallback, high-dim)"
            optimizer = BayesianOptimizer(
                param_defs=param_defs,
                objective_fn=objective_fn,
                **kwargs,
            )

        logger.warning(
            "high-dimensional parameter space detected — consider using "
            "multi-fidelity pruning or hierarchical decomposition for >%d params",
            tpe_max,
        )

    logger.info(
        "optimizer backend selected",
        n_params=n_params,
        backend=backend,
        gp_threshold=gp_max,
        tpe_threshold=tpe_max,
    )
    return optimizer
