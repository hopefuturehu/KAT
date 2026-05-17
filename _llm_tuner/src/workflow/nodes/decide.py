"""Decide node: invoke Orchestrator to determine next workflow step."""
from src.workflow.state import ExperimentState, ExperimentPhase
from src.agents.orchestrator import OrchestratorAgent
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def make_decision(state: ExperimentState) -> ExperimentState:
    logger.info("making decision", trial=state.trial_number)
    state.phase = ExperimentPhase.DECIDING

    orchestrator = OrchestratorAgent()

    # Compute elapsed time
    if state.start_time:
        state.update_elapsed_hours()

    last_trial_summary = "No previous trial"
    if state.trial_history:
        last = state.trial_history[-1]
        last_trial_summary = (
            f"Trial {last.trial_number}: improvement={last.improvement_pct:.1f}%, "
            f"changes={len(last.parameter_changes)}, status={last.status}"
        )

    # Check for explicit failure states
    if state.consecutive_rollbacks >= state.max_consecutive_rollbacks:
        state.orchestrator_decision = {
            "action": "CONVERGED",
            "reasoning": f"Too many consecutive rollbacks ({state.consecutive_rollbacks})",
        }
        return state

    if state.elapsed_hours > state.max_duration_hours:
        state.orchestrator_decision = {
            "action": "MAX_DURATION_REACHED",
            "reasoning": f"Duration {state.elapsed_hours:.1f}h exceeds max {state.max_duration_hours}h",
        }
        return state

    if state.trial_number >= state.max_trials:
        state.orchestrator_decision = {
            "action": "MAX_TRIALS_REACHED",
            "reasoning": f"Reached max trials ({state.max_trials})",
        }
        return state

    if state.all_goals_met():
        state.orchestrator_decision = {
            "action": "GOALS_MET",
            "reasoning": "All performance goals have been achieved",
        }
        return state

    rule_based = _rule_based_decision(state, last_trial_summary)
    if rule_based is not None:
        state.orchestrator_decision = rule_based
        logger.info("decision made", action=rule_based.get("action"), mode="rules")
        return state

    # Invoke LLM Orchestrator
    try:
        decision = await orchestrator.decide_next_action({
            "target_system": state.target_system,
            "target_version": state.target_version,
            "experiment_name": state.experiment_name,
            "trial_number": state.trial_number,
            "max_trials": state.max_trials,
            "elapsed_hours": state.elapsed_hours,
            "max_duration_hours": state.max_duration_hours,
            "goals": [g.model_dump() for g in state.goals],
            "best_metrics": state.best_metrics,
            "last_trial_summary": last_trial_summary,
            "convergence_window": state.convergence_window,
            "recent_improvements": state.improvement_history[-5:],
        })
        state.consecutive_orchestrator_failures = 0
    except Exception as exc:
        state.consecutive_orchestrator_failures += 1
        logger.error(
            "orchestrator agent call failed — using rule-based decision",
            error=str(exc),
            consecutive_failures=state.consecutive_orchestrator_failures,
        )
        state.errors.append(f"Orchestrator agent failed: {exc}")
        if state.consecutive_orchestrator_failures >= 5:
            decision = {
                "action": "CONVERGED",
                "reasoning": (
                    f"Orchestrator LLM unavailable after "
                    f"{state.consecutive_orchestrator_failures} consecutive failures"
                ),
            }
        else:
            decision = {
                "action": "CONTINUE_TUNING",
                "reasoning": f"Rule-based fallback after LLM error: {exc}",
            }

    state.orchestrator_decision = decision

    # Check convergence (rule-based override takes precedence)
    if state.has_converged() and decision.get("action") == "CONTINUE_TUNING":
        decision["action"] = "CONVERGED"
        state.orchestrator_decision = decision

    logger.info("decision made", action=decision.get("action"))
    return state


def _rule_based_decision(
    state: ExperimentState,
    last_trial_summary: str,
) -> dict | None:
    if not state.trial_history:
        return {
            "action": "CONTINUE_TUNING",
            "reasoning": "No completed trials yet; continue collecting signal.",
        }

    last_trial = state.trial_history[-1]
    recent_improvements = state.improvement_history[-min(3, len(state.improvement_history)) :]
    last_improvement = last_trial.improvement_pct

    if state.has_converged():
        return {
            "action": "CONVERGED",
            "reasoning": (
                f"Recent improvement stayed below {state.improvement_threshold_pct:.1f}% "
                f"for {state.convergence_window} trials."
            ),
        }

    if last_trial.status != "completed":
        return {
            "action": "CONTINUE_TUNING",
            "reasoning": f"Latest trial status is {last_trial.status}; continue with caution.",
        }

    if last_improvement >= state.improvement_threshold_pct:
        return {
            "action": "CONTINUE_TUNING",
            "reasoning": (
                f"Latest trial improved by {last_improvement:.1f}%, exceeding the "
                f"{state.improvement_threshold_pct:.1f}% threshold."
            ),
            "next_focus": "Keep iterating near the current best configuration.",
        }

    if len(recent_improvements) >= 2 and all(imp <= 0 for imp in recent_improvements):
        return {
            "action": "CONVERGED",
            "reasoning": (
                "Recent trials did not improve the best result. "
                f"Latest summary: {last_trial_summary}"
            ),
        }

    return None
