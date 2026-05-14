"""Run the in-repo `_llm_tuner` LangGraph workflow (direct mode only)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

# Ensure the in-repo `_llm_tuner` package is importable (preserves `from src.xxx` imports)
_LLM_TUNER_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "_llm_tuner"
if str(_LLM_TUNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_LLM_TUNER_ROOT))

from nanobot.agent.tuning.schema import TuningRequirements, TuningSession


async def _build_experiment_state(req: TuningRequirements) -> Any:
    """Build an ExperimentState from TuningRequirements."""
    from src.workflow.state import ExperimentState, GoalSpec

    goals = [
        GoalSpec(
            metric=g.metric,
            operator=g.operator,
            value=g.value,
            weight=g.weight,
        )
        for g in req.goals
    ]

    state = ExperimentState(
        experiment_name=f"{req.target_system}-tuning",
        target_system=req.target_system,
        target_version=req.target_version,
        goals=goals,
        max_trials=req.max_trials,
        max_duration_hours=req.max_duration_hours,
        blocklist=req.blocked_parameters,
        allow_restart=req.allow_restart,
        memory_headroom_pct=req.memory_headroom_pct,
        max_restart_changes=req.max_restart_changes,
        max_risk_level=req.max_risk_level,
        # Connection
        direct_mode=True,
        target_host=req.host,
        target_port=req.port,
        target_credentials=req.password,
        direct_config_path=req.config_file,
        # Lifecycle commands
        start_command=req.start_command,
        run_command=req.run_command,
        teardown_command=req.teardown_command,
        health_check_command=req.health_check_command,
        restart_command=req.restart_command,
        # Output parsing
        output_format=req.output_format,
        metric_regex=req.metric_regex,
        # Benchmark profile
        benchmark_profile_path=req.benchmark_profile_path,
        # Stability
        stable_mode=req.stable_mode,
        stable_warmup_requests=req.stable_warmup_requests,
        stable_iterations=req.stable_iterations,
    )

    return state


async def _setup_direct_mode(state: Any, req: TuningRequirements) -> None:
    """Load baseline config from the target instance via the direct runner."""
    from src.benchmark.custom_direct_runner import CustomDirectRunner
    from src.benchmark.runner import BenchmarkProfile

    profile = BenchmarkProfile(
        name="baseline-load",
        health_check_command=req.health_check_command,
    )

    runner = CustomDirectRunner(
        profile=profile,
        config_path=req.config_file,
        host=req.host,
        port=req.port,
        credentials=req.password,
    )
    state.container_id = f"direct-{req.host}:{req.port}"

    raw_config = await runner.read_config()
    if raw_config:
        from src.parameters.manager import ParameterManager

        pm = ParameterManager(state.target_system)
        state.current_config = pm.parse_and_validate(raw_config)
        state.baseline_config = dict(state.current_config)
        logger.info(
            "baseline config loaded from {}: {} params",
            req.config_file,
            len(state.current_config),
        )


def _check_dependencies() -> list[str]:
    """Verify all in-repo tuning dependencies are importable."""
    missing: list[str] = []

    for pkg_name, import_name in [
        ("structlog", "structlog"),
        ("langgraph", "langgraph"),
        ("langchain", "langchain"),
        ("langchain_anthropic", "langchain_anthropic"),
        ("instructor", "instructor"),
        ("optuna", "optuna"),
        ("skopt", "skopt"),
    ]:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)

    return missing


async def run_execution(
    session: TuningSession,
    _workspace: str,
) -> tuple[str, dict[str, Any]]:
    """Execute the tuning workflow and return (report_md, structured_data).

    Progress updates are appended to session.progress_messages.
    """
    req = session.requirements

    # Pre-flight dependency check
    missing = _check_dependencies()
    if missing:
        msg = (
            f"Missing packages for tuning: {', '.join(missing)}\n\n"
            f"Install with:  python -m pip install 'nanobot[tuning]'\n"
            f"Or re-run with:  pip install {' '.join(missing)}"
        )
        logger.error(msg)
        return f"## Tuning Failed\n\n{msg}", {}

    try:
        execution_issue = _validate_execution_requirements(req)
        if execution_issue:
            logger.error(execution_issue)
            return f"## Tuning Failed\n\n{execution_issue}", {}

        state = await _build_experiment_state(req)

        # Load baseline config from target
        if req.host and req.config_file:
            await _setup_direct_mode(state, req)
        else:
            msg = (
                "Direct-connect mode requires host, port, and config file path. "
                "A benchmark profile can replace inline benchmark commands, but it does not "
                "replace the connection details needed to read and write the target config."
            )
            logger.error(msg)
            return f"## Tuning Failed\n\n{msg}", {}

        # Dry run
        if req.dry_run:
            return _format_dry_run_report(state), {}

        # Execute workflow
        from src.workflow.graph import create_workflow

        workflow = create_workflow()
        logger.info("starting tuning workflow: {} trials", state.max_trials)

        final_event = None
        async for event in workflow.astream(state, stream_mode="values"):
            node_name = event.get("phase", "unknown")
            trial_num = event.get("trial_number", 0)
            msg = f"[Trial {trial_num}] Phase: {node_name}"
            logger.info(msg)
            session.progress_messages.append(msg)
            final_event = event

        # Reconstruct final state
        if final_event:
            state = _reconstruct_state(state, final_event)

        report = _format_final_report(state, session)
        structured = _extract_structured_results(state)

        return report, structured

    except Exception as e:
        logger.exception("Tuning execution failed")
        session.progress_messages.append(f"Error: {e}")
        return f"## Tuning Failed\n\nError: {e}\n\nProgress:\n" + "\n".join(
            session.progress_messages
        ), {}


def _validate_execution_requirements(req: TuningRequirements) -> str | None:
    """Ensure intake produced an executable tuning request."""
    if not req.host or not req.config_file:
        return (
            "Direct-connect mode requires host, port, and config file path. "
            "Please provide the target instance connection details."
        )
    if not req.run_command and not req.benchmark_profile_path:
        return (
            "A tuning run needs either a benchmark profile path or a run command. "
            "Please provide one of them before execution."
        )
    return None


def _reconstruct_state(original: Any, event: dict[str, Any]) -> Any:
    """Merge event dict back into ExperimentState."""
    for field in (
        "trial_number",
        "current_config",
        "best_config",
        "best_metrics",
        "best_trial_number",
        "improvement_history",
        "consecutive_rollbacks",
        "rollback_history",
        "container_id",
        "elapsed_hours",
        "phase",
        "errors",
        "advisor_recommendations",
    ):
        if field in event:
            setattr(original, field, event[field])
    return original


def _extract_structured_results(state: Any) -> dict[str, Any]:
    """Extract structured tuning data for archiving and session storage."""
    return {
        "best_config": dict(getattr(state, "best_config", {})),
        "best_metrics": dict(getattr(state, "best_metrics", {})),
        "improvement_history": list(getattr(state, "improvement_history", [])),
        "trials_completed": getattr(state, "trial_number", 0),
        "target_system": getattr(state, "target_system", ""),
        "target_version": getattr(state, "target_version", ""),
        "experiment_name": getattr(state, "experiment_name", ""),
        "elapsed_hours": getattr(state, "elapsed_hours", 0.0),
        "advisor_recommendations": dict(getattr(state, "advisor_recommendations", {})),
    }


def _format_dry_run_report(state: Any) -> str:
    """Format a dry-run summary."""
    from src.parameters.manager import ParameterManager

    pm = ParameterManager(state.target_system)
    tunable = pm.get_tunable_parameters()
    lines = [
        "## Dry Run Report",
        "",
        f"**Target**: {state.target_system} {state.target_version}",
        f"**Max Trials**: {state.max_trials}",
        f"**Max Duration**: {state.max_duration_hours}h",
        f"**Tunable Parameters**: {len(tunable)}",
        "",
        "### Goals",
    ]
    for g in state.goals:
        lines.append(f"- {g.metric} {g.operator} {g.value} (weight: {g.weight})")
    lines.append("")
    lines.append("### Baseline Config (top 10)")
    for k, v in list(state.current_config.items())[:10]:
        lines.append(f"- `{k}` = `{v}`")
    if len(state.current_config) > 10:
        lines.append(f"  ... and {len(state.current_config) - 10} more")
    lines.append("")
    lines.append("Dry run completed — no changes were made.")
    return "\n".join(lines)


def _format_final_report(state: Any, _session: TuningSession) -> str:
    """Format the final tuning report."""
    lines = [
        "## Tuning Report",
        "",
        f"**Experiment**: {getattr(state, 'experiment_name', 'unknown')}",
        f"**Target**: {state.target_system} {state.target_version}",
        f"**Trials Completed**: {getattr(state, 'trial_number', 0)}",
        f"**Phase**: {getattr(state, 'phase', 'unknown')}",
        "",
    ]

    if state.best_metrics:
        lines.append("### Best Results")
        for k, v in state.best_metrics.items():
            if isinstance(v, float):
                lines.append(f"- **{k}**: {v:.2f}")
            else:
                lines.append(f"- **{k}**: {v}")

    if state.improvement_history:
        improvements = [f"{x:+.1f}%" for x in state.improvement_history[-5:]]
        lines.append("")
        lines.append(
            f"**Improvement History** (last {len(improvements)}): {', '.join(improvements)}"
        )

    if state.best_config:
        lines.append("")
        lines.append("### Best Configuration")
        for k, v in list(state.best_config.items())[:15]:
            lines.append(f"- `{k}` = `{v}`")
        if len(state.best_config) > 15:
            lines.append(f"  ... and {len(state.best_config) - 15} more")

    return "\n".join(lines)
