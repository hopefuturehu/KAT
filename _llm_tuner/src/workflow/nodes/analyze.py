"""Analyze node: invoke Analyzer Agent to interpret benchmark results."""

from src.workflow.state import ExperimentState, ExperimentPhase
from src.agents.analyzer_agent import AnalyzerAgent
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def analyze_results(state: ExperimentState) -> ExperimentState:
    logger.info("analyzing results", trial=state.trial_number)
    state.phase = ExperimentPhase.ANALYZING

    analyzer = AnalyzerAgent()

    if state.current_trial is None:
        logger.warning("no current trial to analyze")
        return state

    benchmark_results = {
        "operations": state.current_trial.benchmark_results,
        "aggregate": state.current_trial.metrics,
    }

    # Compute improvement over best (skip history update for failed benchmarks)
    improvement = state.compute_improvement(state.current_trial.metrics)
    state.current_trial.improvement_pct = improvement
    if state.current_trial.metrics:
        state.improvement_history.append(improvement)

    # Update best if improved (skip empty metrics from failed benchmarks)
    if state.current_trial.metrics and (not state.best_metrics or improvement > 0):
        state.best_metrics = dict(state.current_trial.metrics)
        state.best_config = dict(state.current_config)
        state.best_trial_number = state.trial_number

    # Build trend data for analyzer
    trend_data = [
        {
            "trial": t.trial_number,
            "metrics": {k: v for k, v in t.metrics.items() if not k.startswith("aggregate") and isinstance(v, (int, float))},
            "improvement_pct": t.improvement_pct,
        }
        for t in state.trial_history[-state.convergence_window:]
    ]

    try:
        analysis = await analyzer.analyze(
            state={
                "target_system": state.target_system,
                "target_version": state.target_version,
                "goals": [g.model_dump() for g in state.goals],
                "trial_number": state.trial_number,
                "best_metrics": state.best_metrics,
                "trend_data": trend_data,
                "convergence_window": state.convergence_window,
            },
            benchmark_results=benchmark_results,
            parameter_changes=state.current_trial.parameter_changes,
        )
    except Exception as exc:
        logger.error("analyzer agent call failed", error=str(exc))
        state.errors.append(f"Analyzer agent failed: {exc}")
        analysis = {
            "trend": "stable",
            "improvement_pct": 0.0,
            "likely_bottleneck": "unknown",
            "change_impact": "neutral",
            "insights": "Analysis skipped due to LLM error",
            "recommended_focus": "general tuning",
        }

    state.analysis_result = analysis
    state.current_trial.analysis = analysis
    state.consecutive_rollbacks = 0
    state.commit_current_trial(status="completed")

    # Persist trial to database
    await _record_trial_to_db(state)

    logger.info(
        "analysis complete",
        bottleneck=analysis.get("likely_bottleneck"),
        trend=analysis.get("trend"),
        improvement=f"{improvement:.1f}%",
    )
    return state


async def _record_trial_to_db(state: ExperimentState) -> None:
    """Persist the current trial to the database (best-effort, non-fatal)."""
    if not state.experiment_id or state.current_trial is None:
        return

    try:
        from src.db.session import async_session
        from src.tracking.experiment import ExperimentTracker

        async with async_session() as session:
            tracker = ExperimentTracker(session)
            await tracker.record_trial(
                experiment_id=state.experiment_id,
                trial_number=state.current_trial.trial_number,
                config=state.current_trial.config,
                metrics=state.current_trial.metrics,
                parameter_changes=state.current_trial.parameter_changes,
                improvement_pct=state.current_trial.improvement_pct,
                phase=state.phase.value,
                status=state.current_trial.status,
            )
            logger.debug("trial persisted to database", trial=state.current_trial.trial_number)
    except Exception as exc:
        logger.warning("failed to persist trial to database (continuing)", error=str(exc))
