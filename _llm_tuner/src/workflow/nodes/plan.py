"""Plan node: invoke Hybrid Tuner (LLM + Bayesian fallback) or Advisor Agent to propose next steps."""
from src.workflow.state import ExperimentState, ExperimentPhase
from src.agents.advisor_agent import AdvisorAgent
from src.knowledge.retriever import knowledge_base
from src.parameters.schema import ParameterRisk
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def plan_changes(state: ExperimentState) -> ExperimentState:
    logger.info("planning changes", trial=state.trial_number + 1)
    state.phase = ExperimentPhase.PLANNING

    has_converged = state.has_converged()
    all_goals_met = state.all_goals_met()

    if has_converged or all_goals_met:
        return await _invoke_advisor(state)

    return await _invoke_tuner(state)


async def _invoke_tuner(state: ExperimentState) -> ExperimentState:
    from src.parameters.manager import ParameterManager
    from src.optimization.hybrid_tuner import HybridTuner

    pm = ParameterManager(state.target_system)
    tunable = pm.get_tunable_parameters(
        exclude_parameters=state.blocklist,
        max_risk=_parse_max_risk(state.max_risk_level),
    )
    if not state.allow_restart:
        tunable = [p for p in tunable if not p.restart_required]

    tunable_dicts = [
        {
            "name": p.name,
            "category": p.category.value,
            "description": p.description,
            "default": p.default_value,
            "type": p.type,
            "min": p.min_value,
            "max": p.max_value,
            "enum_values": p.enum_values,
            "restart_required": p.restart_required,
            "risk": p.risk.value,
            "depends_on": p.depends_on,
            "conflicts_with": p.conflicts_with,
            "notes": p.notes,
        }
        for p in tunable
    ]
    state.tunable_parameters = tunable_dicts

    recent_changes = []
    for trial in state.trial_history[-3:]:
        for change in trial.parameter_changes:
            recent_changes.append({
                "trial": trial.trial_number,
                "parameter": change.get("parameter"),
                "old_value": change.get("old_value"),
                "new_value": change.get("new_value"),
            })

    analysis = state.analysis_result or {
        "recommended_focus": "general tuning",
        "likely_bottleneck": "unknown",
    }

    # Build trial history dicts for Bayesian seeding
    trial_history = [
        {
            "config": t.config,
            "metrics": t.metrics,
        }
        for t in state.trial_history
        if t.metrics
    ]

    hybrid = HybridTuner(
        target_system=state.target_system,
        kb_retriever=knowledge_base,
    )

    proposal = await hybrid.propose_or_skip(
        state={
            "target_system": state.target_system,
            "target_version": state.target_version,
            "goals": [g.model_dump() for g in state.goals],
            "max_changes_per_trial": state.max_changes_per_trial,
            "allow_restart": state.allow_restart,
            "max_restart_changes": state.max_restart_changes,
            "max_risk_level": state.max_risk_level,
            "blocklist": state.blocklist,
        },
        analysis=analysis,
        current_config=state.current_config,
        baseline_config=state.baseline_config,
        tunable_params=tunable_dicts,
        recent_changes=recent_changes,
        trial_history=trial_history,
        blocklist=state.blocklist,
        skip_if_no_data=True,
    )

    state.tuning_proposal = proposal
    logger.info(
        "tuner proposal",
        source=proposal.get("_source", "unknown"),
        changes_count=len(proposal.get("changes", [])),
        strategy=proposal.get("overall_strategy", ""),
    )
    return state


async def _invoke_advisor(state: ExperimentState) -> ExperimentState:
    advisor = AdvisorAgent(kb_retriever=knowledge_base)

    goals_with_progress = []
    for goal in state.goals:
        current = state.best_metrics.get(goal.metric, 0)
        met = state.goal_met(goal.metric, current)
        goals_with_progress.append({
            "metric": goal.metric,
            "target": f"{goal.operator} {goal.value}",
            "current": current,
            "met": met,
            "gap_pct": round((goal.value - current) / goal.value * 100, 1) if not met else 0,
        })

    bottleneck = state.analysis_result.get("likely_bottleneck", "configuration")

    try:
        recommendations = await advisor.recommend(
            state={"target_system": state.target_system, "best_metrics": state.best_metrics,
                    "hardware_spec": state.hardware_spec},
            goals_with_progress=goals_with_progress,
            current_config=state.current_config,
            bottleneck=bottleneck,
            tuning_summary=[
                {"trial": t.trial_number, "improvement_pct": t.improvement_pct}
                for t in state.trial_history
            ],
        )
    except Exception as exc:
        logger.error("advisor agent call failed", error=str(exc))
        state.errors.append(f"Advisor agent failed: {exc}")
        recommendations = {
            "summary": "Could not generate recommendations due to LLM error",
            "recommendations": [],
        }

    state.advisor_recommendations = recommendations
    state.phase = ExperimentPhase.ADVISING
    logger.info("advisor recommendations generated", count=len(recommendations.get("recommendations", [])))
    return state


def _parse_max_risk(risk_level: str) -> ParameterRisk:
    try:
        return ParameterRisk(str(risk_level).lower())
    except ValueError:
        logger.warning("unknown max risk level, defaulting to medium", risk_level=risk_level)
        return ParameterRisk.MEDIUM
