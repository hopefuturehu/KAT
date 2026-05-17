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

    relevant_metadata = _select_relevant_parameter_metadata(proposed_changes, metadata_by_name)
    relevant_config = {
        key: state.current_config[key]
        for key in sorted(
            {
                name
                for item in relevant_metadata
                for name in [item["name"], *item.get("depends_on", []), *item.get("conflicts_with", [])]
            }
        )
        if key in state.current_config
    }
    rollback_summary = [
        {
            "trial": item.get("trial"),
            "reason": item.get("reason", ""),
        }
        for item in state.rollback_history[-3:]
    ]

    if _can_short_circuit_approval(proposed_changes, relevant_metadata):
        verdict = {
            "verdict": "APPROVE",
            "overall_risk_level": "low",
            "warnings": [],
            "requires_human_approval": False,
            "notes": "Approved by static low-risk guardrails",
        }
        state.safety_verdict = verdict
        logger.info("safety verdict", verdict="APPROVE", risk="low", mode="static")
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
            current_config=relevant_config,
            parameter_metadata=relevant_metadata,
            rollback_history=rollback_summary,
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


def _select_relevant_parameter_metadata(
    proposed_changes: list[dict],
    metadata_by_name: dict[str, dict],
) -> list[dict]:
    relevant_names = {
        str(change.get("parameter", ""))
        for change in proposed_changes
    }
    expanded = set(relevant_names)
    for name in list(relevant_names):
        metadata = metadata_by_name.get(name)
        if metadata is None:
            continue
        expanded.update(str(item) for item in metadata.get("depends_on", []))
        expanded.update(str(item) for item in metadata.get("conflicts_with", []))

    return [
        metadata_by_name[name]
        for name in sorted(expanded)
        if name in metadata_by_name
    ]


def _can_short_circuit_approval(
    proposed_changes: list[dict],
    relevant_metadata: list[dict],
) -> bool:
    metadata_by_name = {item["name"]: item for item in relevant_metadata}
    if not proposed_changes:
        return True

    for change in proposed_changes:
        name = str(change.get("parameter", ""))
        metadata = metadata_by_name.get(name)
        if metadata is None:
            return False
        if metadata.get("restart_required"):
            return False
        if metadata.get("depends_on") or metadata.get("conflicts_with"):
            return False
        if str(metadata.get("risk", "medium")).lower() != "low":
            return False
        if not _value_fits_metadata(change.get("proposed_value"), metadata):
            return False
    return True


def _value_fits_metadata(value: object, metadata: dict) -> bool:
    param_type = str(metadata.get("type", "string")).lower()
    enum_values = metadata.get("enum_values") or []
    if enum_values and str(value) not in {str(item) for item in enum_values}:
        return False

    if param_type in {"integer", "float"}:
        try:
            numeric_value = float(str(value))
        except (TypeError, ValueError):
            return False
        min_value = metadata.get("min")
        max_value = metadata.get("max")
        if min_value not in (None, "") and numeric_value < float(str(min_value)):
            return False
        if max_value not in (None, "") and numeric_value > float(str(max_value)):
            return False
    elif param_type == "boolean" and str(value).lower() not in {"true", "false", "0", "1", "yes", "no"}:
        return False

    return True


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
