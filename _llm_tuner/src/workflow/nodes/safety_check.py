"""Safety check node: validate proposed changes before applying."""

from src.workflow.state import ExperimentState, ExperimentPhase
from src.agents.safety_agent import SafetyAgent
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def safety_gate(state: ExperimentState) -> ExperimentState:
    logger.info("running safety check")
    state.phase = ExperimentPhase.SAFETY_CHECK

    safety = SafetyAgent()

    proposed_changes = state.tuning_proposal.get("changes", [])
    if not proposed_changes:
        state.safety_verdict = {"verdict": "APPROVE", "notes": "No changes to validate"}
        return state

    # Build parameter metadata from tunable params
    param_metadata = [
        {
            "name": p["name"],
            "risk": p.get("risk", "low"),
            "restart_required": p.get("restart_required", False),
            "type": p.get("type", "string"),
            "min": p.get("min"),
            "max": p.get("max"),
            "enum_values": p.get("enum_values"),
            "depends_on": p.get("depends_on", []),
            "conflicts_with": p.get("conflicts_with", []),
        }
        for p in state.tunable_parameters
    ]

    try:
        verdict = await safety.validate(
            state={
                "target_system": state.target_system,
                "target_version": state.target_version,
                "memory_headroom_pct": state.memory_headroom_pct,
                "max_restart_changes": state.max_restart_changes,
                "stability_window": 3,
                "max_consecutive_rollbacks": state.max_consecutive_rollbacks,
            },
            proposed_changes=proposed_changes,
            current_config=state.current_config,
            parameter_metadata=param_metadata,
            rollback_history=state.rollback_history,
        )
    except Exception as exc:
        logger.error("safety agent call failed — rejecting trial", error=str(exc))
        state.errors.append(f"Safety agent failed: {exc}")
        verdict = {
            "verdict": "REJECT",
            "overall_risk_level": "high",
            "warnings": [f"Safety check failed: {exc}"],
            "requires_human_approval": True,
        }

    state.safety_verdict = verdict
    logger.info(
        "safety verdict",
        verdict=verdict.get("verdict"),
        risk=verdict.get("overall_risk_level"),
    )

    if verdict.get("verdict") == "REJECT":
        state.safety_warnings.append(
            f"Trial {state.trial_number + 1}: Rejected — {verdict.get('warnings', [])}"
        )

    return state
