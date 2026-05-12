"""Edge routing conditions for the LangGraph workflow."""


def should_continue_tuning(state) -> bool:
    """Determine if tuning should continue or finalize."""
    from src.workflow.state import ExperimentState
    decision = state.orchestrator_decision
    return decision.get("action") == "CONTINUE_TUNING"


def should_rollback(state) -> bool:
    """Determine if rollback is needed."""
    decision = state.orchestrator_decision
    return decision.get("action") == "ROLLBACK"


def is_safety_approved(state) -> bool:
    """Check if safety check passed."""
    verdict = state.safety_verdict
    return verdict.get("verdict") in ("APPROVE", "APPROVE_WITH_MODIFICATIONS")
