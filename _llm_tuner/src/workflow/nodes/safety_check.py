"""Safety check node: validate proposed changes before applying."""

from src.workflow.state import ExperimentState, ExperimentPhase
from src.agents.safety_agent import SafetyAgent
from src.parameters.schema import ParameterRisk
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
            "category": p.get("category", "general"),
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
    metadata_by_name = {p["name"]: p for p in param_metadata}

    rule_issues: list[str] = []
    restart_count = 0
    max_risk_rank = _risk_rank(state.max_risk_level)
    for change in proposed_changes:
        parameter = change.get("parameter", "")
        metadata = metadata_by_name.get(parameter)
        if metadata is None:
            rule_issues.append(f"Unknown or disallowed parameter '{parameter}'")
            continue

        if metadata.get("restart_required"):
            restart_count += 1
            if not state.allow_restart:
                rule_issues.append(
                    f"Parameter '{parameter}' requires restart but allow_restart is false"
                )

        if _risk_rank(metadata.get("risk", "low")) > max_risk_rank:
            rule_issues.append(
                f"Parameter '{parameter}' exceeds max risk level '{state.max_risk_level}'"
            )

    if restart_count > state.max_restart_changes:
        rule_issues.append(
            f"Restart-requiring changes ({restart_count}) exceed limit ({state.max_restart_changes})"
        )

    if rule_issues:
        verdict = {
            "verdict": "REJECT",
            "overall_risk_level": "high",
            "warnings": rule_issues,
            "requires_human_approval": True,
        }
        state.safety_verdict = verdict
        state.safety_warnings.append(
            f"Trial {state.trial_number + 1}: Rejected — {rule_issues}"
        )
        logger.info("safety verdict", verdict=verdict.get("verdict"), risk="high")
        return state

    try:
        verdict = await safety.validate(
            state={
                "target_system": state.target_system,
                "target_version": state.target_version,
                "allow_restart": state.allow_restart,
                "memory_headroom_pct": state.memory_headroom_pct,
                "max_restart_changes": state.max_restart_changes,
                "max_risk_level": state.max_risk_level,
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


def _risk_rank(risk_level: str) -> int:
    try:
        risk = ParameterRisk(str(risk_level).lower())
    except ValueError:
        risk = ParameterRisk.MEDIUM
    return {
        ParameterRisk.LOW: 0,
        ParameterRisk.MEDIUM: 1,
        ParameterRisk.HIGH: 2,
        ParameterRisk.CRITICAL: 3,
    }[risk]
