"""Hybrid Tuner — LLM primary, Bayesian fallback with auto backend selection.

When the LLM API is healthy the HybridTuner delegates to the LLM TunerAgent
for knowledge-driven, context-aware parameter proposals.

When the LLM is unreachable (CircuitBreakerOpenError, rate-limited, or any
other exception), it falls back to Bayesian optimization seeded from all
prior trial history.  The Bayesian backend is auto-selected based on
parameter count:

    ≤ 30  params → GP+EI  (best sample efficiency)
    30-100 params → TPE    (handles higher dims)
    > 100 params → TPE + warning  (consider multi-fidelity)
"""

from __future__ import annotations

from typing import Any

from src.agents.tuner_agent import TunerAgent
from src.parameters.schema import ParameterDefinition, ParameterRisk
from src.utils.llm_resilience import CircuitBreakerOpenError
from src.utils.logging import get_logger

logger = get_logger(__name__)


class HybridTuner:
    """A tuner that tries LLM first and falls back to Bayesian optimization.

    The Bayesian backend is auto-selected based on parameter dimensionality.
    """

    def __init__(
        self,
        target_system: str,
        kb_retriever: Any = None,
        model: str | None = None,
    ):
        self.target_system = target_system
        self.llm_tuner = TunerAgent(model=model, kb_retriever=kb_retriever)
        self._bo: Any = None  # BayesianOptimizer | TPEOptimizer
        self._bo_seeded: bool = False
        self._last_source: str = ""

    @property
    def last_source(self) -> str:
        """'llm' or 'bayesian' — which source produced the last proposal."""
        return self._last_source

    async def propose(
        self,
        state: dict,
        analysis: dict,
        current_config: dict[str, str],
        baseline_config: dict[str, str] | None,
        tunable_params: list[dict],
        recent_changes: list[dict],
        trial_history: list[dict] | None = None,
        blocklist: list[str] | None = None,
    ) -> dict:
        """Propose parameter changes.  Tries LLM first, falls back to BO on failure.

        Args:
            state: Dict with target_system, target_version, goals, max_changes_per_trial, ...
            analysis: AnalyzerAgent output dict.
            current_config: Current live config.
            baseline_config: Original baseline config.
            tunable_params: List of parameter metadata dicts.
            recent_changes: Changes from recent trials.
            trial_history: List of past trial dicts for seeding the BO prior.
            blocklist: Parameters to exclude.

        Returns:
            Dict with ``changes`` (list) and ``overall_strategy`` (str),
            matching the TunerAgent output format.
        """
        # ── Try LLM first ──────────────────────────────────────────────
        try:
            proposal = await self.llm_tuner.propose_changes(
                state=state,
                analysis=analysis,
                current_config=current_config,
                baseline_config=baseline_config,
                tunable_params=tunable_params,
                recent_changes=recent_changes,
            )
            self._last_source = "llm"
            proposal["_source"] = "llm"
            return proposal

        except CircuitBreakerOpenError:
            logger.warning("circuit breaker open — falling back to Bayesian optimizer")
        except Exception as exc:
            logger.warning("LLM tuner failed — falling back to Bayesian optimizer", error=str(exc)[:150])

        # ── Bayesian fallback ──────────────────────────────────────────
        return await self._bayesian_propose(
            current_config=current_config,
            tunable_params=tunable_params,
            trial_history=trial_history or [],
            blocklist=blocklist or [],
        )

    async def propose_or_skip(
        self,
        state: dict,
        analysis: dict,
        current_config: dict[str, str],
        baseline_config: dict[str, str] | None,
        tunable_params: list[dict],
        recent_changes: list[dict],
        trial_history: list[dict] | None = None,
        blocklist: list[str] | None = None,
        skip_if_no_data: bool = True,
    ) -> dict:
        """Like *propose* but returns empty changes if BO also has insufficient data.

        This is the safe variant for workflow integration — if both LLM and BO
        fail to produce useful output, the trial is simply skipped rather than
        feeding garbage into the safety gate.
        """
        proposal = await self.propose(
            state=state,
            analysis=analysis,
            current_config=current_config,
            baseline_config=baseline_config,
            tunable_params=tunable_params,
            recent_changes=recent_changes,
            trial_history=trial_history,
            blocklist=blocklist,
        )

        changes = proposal.get("changes", [])
        if not changes and proposal.get("_source") == "bayesian":
            proposal["overall_strategy"] = (
                "No changes proposed — insufficient trial data for Bayesian inference"
            )
        return proposal

    # ── Internal ────────────────────────────────────────────────────────

    async def _bayesian_propose(
        self,
        current_config: dict[str, str],
        tunable_params: list[dict],
        trial_history: list[dict],
        blocklist: list[str],
    ) -> dict:
        """Build and run a Bayesian proposal via auto-selected backend."""
        param_defs = _build_param_defs(tunable_params, blocklist)
        if not param_defs:
            logger.warning("no tunable parameters available for Bayesian optimizer")
            return {"changes": [], "overall_strategy": "No parameters available for BO", "_source": "bayesian"}

        # Auto-select backend (GP+EI or TPE) and create optimizer on first call
        if self._bo is None:
            from src.optimization.selector import select_backend
            self._bo = select_backend(
                param_defs=param_defs,
                n_initial_points=5,
            )

        # Seed / re-seed with accumulated history
        self._bo.seed_from_history(trial_history, maximize=True)

        # Propose
        max_changes = min(4, len(param_defs))
        proposal = self._bo.propose_changes_diff(
            current_config=current_config,
            maximize=True,
            max_changes=max_changes,
        )

        if proposal is None or not proposal.get("changes"):
            self._last_source = "bayesian"
            return {
                "changes": [],
                "overall_strategy": (
                    "Bayesian fallback: insufficient trial data for inference. "
                    f"Need at least 2 trials with valid metrics."
                ),
                "_source": "bayesian",
            }

        self._last_source = "bayesian"
        proposal["_source"] = "bayesian"
        logger.info(
            "bayesian proposal generated",
            changes=len(proposal.get("changes", [])),
            seed_trials=getattr(self._bo, '_seed_count', 0),
        )
        return proposal


def _build_param_defs(
    tunable_params: list[dict],
    blocklist: list[str],
) -> list[ParameterDefinition]:
    """Convert tunable param dicts to ParameterDefinition objects, filtering out
    non-numeric types and blocklisted parameters."""
    blocked = set(blocklist)
    defs: list[ParameterDefinition] = []

    for p in tunable_params:
        name = p.get("name", "")
        if name in blocked:
            continue
        ptype = p.get("type", "string")
        if ptype not in ("integer", "float", "enum", "boolean"):
            continue

        defs.append(ParameterDefinition(
            name=name,
            category=p.get("category", "general"),
            description=p.get("description", ""),
            default_value=str(p.get("default", "")),
            type=ptype,
            min_value=str(p.get("min")) if p.get("min") is not None else None,
            max_value=str(p.get("max")) if p.get("max") is not None else None,
            enum_values=p.get("enum_values"),
            restart_required=p.get("restart_required", False),
            risk=_parse_risk(p.get("risk", "low")),
            notes=p.get("notes", ""),
        ))

    return defs


def _parse_risk(risk_str: str) -> ParameterRisk:
    try:
        return ParameterRisk(risk_str)
    except ValueError:
        return ParameterRisk.LOW
