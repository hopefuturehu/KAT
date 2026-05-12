"""TuningExecutionAgent — runs the llm-tuner LangGraph workflow."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from loguru import logger

# Ensure llm_tuner package is importable (preserves 'from src.xxx' imports)
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
        memory_headroom_pct=req.memory_headroom_pct,
        max_restart_changes=req.max_restart_changes,
    )

    # Direct-connect mode
    if req.host:
        state.direct_mode = True
        state.redis_host = req.host
        state.redis_port = req.port
        state.redis_password = req.password
        state.direct_config_path = req.config_file

    return state


async def _setup_direct_mode(state: Any, req: TuningRequirements) -> None:
    """Configure state for direct-connect Redis mode."""
    from src.benchmark.direct_runner import DirectRedisRunner

    runner = DirectRedisRunner(
        config_path=req.config_file,
        host=req.host,
        port=req.port,
        password=req.password,
        benchmark_cmd="",
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


async def run_execution(
    session: TuningSession,
    workspace: str,
) -> str:
    """Execute the tuning workflow and return a final report.

    Progress updates are appended to session.progress_messages.
    """
    req = session.requirements

    try:
        state = await _build_experiment_state(req)

        # Setup direct mode if connecting to existing instance
        if req.host and req.config_file:
            await _setup_direct_mode(state, req)
        else:
            # Docker mode: provision container
            from src.environment.manager import TargetEnvironmentManager, EnvironmentConfig

            env_config = EnvironmentConfig(
                template=f"{req.target_system}-standalone",
                cpu_limit="4",
                memory_limit="8g",
            )
            env_mgr = TargetEnvironmentManager()
            container = await env_mgr.provision(env_config, state.experiment_id)
            state.container_id = container.container_id

            # Load baseline config from container
            config_path_map = {
                "redis": "/usr/local/etc/redis/redis.conf",
                "mysql": "/etc/mysql/my.cnf",
            }
            config_path = config_path_map.get(req.target_system, "")
            if config_path and container.container_id:
                raw_config = await env_mgr.get_config(config_path)
                if raw_config:
                    from src.parameters.manager import ParameterManager

                    pm = ParameterManager(state.target_system)
                    state.current_config = pm.parse_and_validate(raw_config)
                    state.baseline_config = dict(state.current_config)

        # Dry run
        if req.dry_run:
            return _format_dry_run_report(state)

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

        return _format_final_report(state, session)

    except Exception as e:
        logger.exception("Tuning execution failed")
        session.progress_messages.append(f"Error: {e}")
        return f"## Tuning Failed\n\nError: {e}\n\nProgress:\n" + "\n".join(
            session.progress_messages
        )


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
    ):
        if field in event:
            setattr(original, field, event[field])
    return original


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


def _format_final_report(state: Any, session: TuningSession) -> str:
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
        lines.append(f"**Improvement History** (last {len(improvements)}): {', '.join(improvements)}")

    if state.best_config:
        lines.append("")
        lines.append("### Best Configuration")
        for k, v in list(state.best_config.items())[:15]:
            lines.append(f"- `{k}` = `{v}`")
        if len(state.best_config) > 15:
            lines.append(f"  ... and {len(state.best_config) - 15} more")

    return "\n".join(lines)
