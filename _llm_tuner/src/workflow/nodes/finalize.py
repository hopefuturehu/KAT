"""Finalize node: generate final report and clean up."""

from datetime import datetime
from src.workflow.state import ExperimentState, ExperimentPhase
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def finalize_experiment(state: ExperimentState) -> ExperimentState:
    logger.info("finalizing experiment", name=state.experiment_name)
    state.phase = ExperimentPhase.COMPLETED

    decision = state.orchestrator_decision
    action = decision.get("action", "UNKNOWN")

    report_lines = [
        f"# Experiment Report: {state.experiment_name}",
        f"",
        f"## Summary",
        f"- Target: {state.target_system} {state.target_version}",
        f"- Status: {action}",
        f"- Trials completed: {state.trial_number}",
        f"- Duration: {state.elapsed_hours:.1f} hours",
        f"",
        f"## Goals",
    ]

    for goal in state.goals:
        current = state.best_metrics.get(goal.metric, 0)
        met = state.goal_met(goal.metric, current)
        icon = "✓" if met else "✗"
        report_lines.append(f"- {icon} {goal.metric}: {goal.operator} {goal.value} (achieved: {current})")

    report_lines.extend([
        f"",
        f"## Best Configuration",
        f"```",
    ])

    if state.best_config:
        from src.parameters.manager import ParameterManager
        pm = ParameterManager(state.target_system)
        report_lines.append(pm.serialize_config(state.best_config))
    report_lines.append("```")

    # Include advisor recommendations if available
    if state.advisor_recommendations:
        report_lines.extend([
            f"",
            f"## Alternative Recommendations",
            f"",
            state.advisor_recommendations.get("summary", ""),
            f"",
        ])
        for i, rec in enumerate(state.advisor_recommendations.get("recommendations", []), 1):
            report_lines.extend([
                f"### {i}. {rec.get('category', 'Unknown')}",
                f"- **Recommendation**: {rec.get('recommendation', '')}",
                f"- **Expected Benefit**: {rec.get('expected_benefit', '')}",
                f"- **Effort**: {rec.get('effort', 'unknown')}",
                f"- **Risk**: {rec.get('risk', 'unknown')}",
                f"",
            ])

    report_lines.extend([
        f"",
        f"## Timeline",
    ])

    for trial in state.trial_history:
        report_lines.append(
            f"- Trial {trial.trial_number}: +{trial.improvement_pct:.1f}% — "
            f"{len(trial.parameter_changes)} changes — {trial.status}"
        )

    report = "\n".join(report_lines)

    # Save report
    from pathlib import Path
    report_dir = Path("data/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{state.experiment_name}-{state.experiment_id[:8]}.md"
    report_path.write_text(report)

    logger.info("experiment finalized", report=str(report_path))

    # Update experiment status in database
    if state.experiment_id:
        try:
            from src.db.session import async_session
            from src.tracking.experiment import ExperimentTracker

            async with async_session() as session:
                tracker = ExperimentTracker(session)
                await tracker.update_experiment_status(
                    state.experiment_id,
                    status=state.phase.value,
                    best_metrics=state.best_metrics,
                )
                logger.info("experiment status updated in database", status=state.phase.value)
        except Exception as exc:
            logger.warning("failed to update experiment status in database", error=str(exc))

    # Cleanup environment (Docker mode only — direct mode leaves Redis running)
    if state.container_id and not state.direct_mode:
        try:
            from src.environment.manager import TargetEnvironmentManager
            env_manager = TargetEnvironmentManager.for_container(state.container_id)
            await env_manager.teardown()
            logger.info("environment cleaned up")
        except Exception as e:
            logger.warning("cleanup failed", error=str(e))

    return state
