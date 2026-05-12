"""Decide node: invoke Orchestrator to determine next workflow step."""

from datetime import datetime
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
        state.elapsed_hours = (datetime.utcnow() - state.start_time).total_seconds() / 3600

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
        })
    except Exception as exc:
        logger.error("orchestrator agent call failed — using rule-based decision", error=str(exc))
        state.errors.append(f"Orchestrator agent failed: {exc}")
        decision = {"action": "CONTINUE_TUNING", "reasoning": f"Rule-based fallback after LLM error: {exc}"}

    state.orchestrator_decision = decision

    # Check convergence (rule-based override takes precedence)
    if state.has_converged() and decision.get("action") == "CONTINUE_TUNING":
        decision["action"] = "CONVERGED"
        state.orchestrator_decision = decision

    logger.info("decision made", action=decision.get("action"))
    return state
