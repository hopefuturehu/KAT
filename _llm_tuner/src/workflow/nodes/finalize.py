"""Finalize node: generate final report, teardown environment, and clean up."""

from pathlib import Path

from src.workflow.state import ExperimentPhase, ExperimentState
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def finalize_experiment(state: ExperimentState) -> ExperimentState:
    logger.info("finalizing experiment", name=state.experiment_name)
    state.phase = ExperimentPhase.COMPLETED

    decision = state.orchestrator_decision
    action = decision.get("action", "UNKNOWN")

    report_lines = [
        f"# Experiment Report: {state.experiment_name}",
        "",
        "## Summary",
        f"- Target: {state.target_system} {state.target_version}",
        f"- Status: {action}",
        f"- Trials completed: {state.trial_number}",
        f"- Duration: {state.elapsed_hours:.1f} hours",
        "",
        "## Goals",
    ]

    for goal in state.goals:
        current = state.best_metrics.get(goal.metric, 0)
        met = state.goal_met(goal.metric, current)
        icon = "✓" if met else "✗"
        report_lines.append(
            f"- {icon} {goal.metric}: {goal.operator} {goal.value} (achieved: {current})"
        )

    report_lines.extend(["", "## Best Configuration", "```"])

    if state.best_config:
        from src.parameters.manager import ParameterManager

        pm = ParameterManager(state.target_system)
        report_lines.append(pm.serialize_config(state.best_config))
    report_lines.append("```")

    # Include advisor recommendations if available
    if state.advisor_recommendations:
        report_lines.extend([
            "",
            "## Alternative Recommendations",
            "",
            state.advisor_recommendations.get("summary", ""),
            "",
        ])
        for i, rec in enumerate(
            state.advisor_recommendations.get("recommendations", []), 1
        ):
            report_lines.extend([
                f"### {i}. {rec.get('category', 'Unknown')}",
                f"- **Recommendation**: {rec.get('recommendation', '')}",
                f"- **Expected Benefit**: {rec.get('expected_benefit', '')}",
                f"- **Effort**: {rec.get('effort', 'unknown')}",
                f"- **Risk**: {rec.get('risk', 'unknown')}",
                "",
            ])

    report_lines.extend(["", "## Timeline"])

    for trial in state.trial_history:
        report_lines.append(
            f"- Trial {trial.trial_number}: +{trial.improvement_pct:.1f}% — "
            f"{len(trial.parameter_changes)} changes — {trial.status}"
        )

    report = "\n".join(report_lines)

    # Save report
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

    # ── Teardown: restore baseline config + run teardown command ──────────
    await _teardown_environment(state)

    return state


async def _teardown_environment(state: ExperimentState) -> None:
    """Restore the target to its pre-experiment state."""
    from src.benchmark.custom_direct_runner import CustomDirectRunner
    from src.benchmark.runner import BenchmarkProfile

    # Restore baseline config if we have one
    if state.baseline_config and state.direct_config_path:
        try:
            from src.parameters.manager import ParameterManager

            pm = ParameterManager(state.target_system)
            config_text = pm.serialize_config(state.baseline_config)

            profile = BenchmarkProfile(
                name="teardown",
                restart_command=state.restart_command,
                health_check_command=state.health_check_command,
            )

            runner = CustomDirectRunner(
                profile=profile,
                config_path=state.direct_config_path,
                host=state.connection_host,
                port=state.connection_port,
                credentials=state.connection_credentials,
            )

            await runner.write_config(config_text)
            await runner.restart()
            logger.info("baseline config restored")
        except Exception as e:
            logger.warning("failed to restore baseline config", error=str(e)[:100])

    # Run teardown command if provided
    if state.teardown_command:
        try:
            from src.benchmark.custom_direct_runner import CustomDirectRunner
            from src.benchmark.runner import BenchmarkProfile

            profile = BenchmarkProfile(
                name="teardown-final", teardown_command=state.teardown_command
            )
            runner = CustomDirectRunner(
                profile=profile,
                config_path=state.direct_config_path,
                host=state.connection_host,
                port=state.connection_port,
                credentials=state.connection_credentials,
            )
            await runner.stop()
            logger.info("teardown command executed")
        except Exception as e:
            logger.warning("teardown failed", error=str(e)[:100])
