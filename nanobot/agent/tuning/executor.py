"""Run the in-repo `_llm_tuner` LangGraph workflow (direct mode only)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

# Ensure the in-repo `_llm_tuner` package is importable (preserves `from src.xxx` imports)
_LLM_TUNER_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "_llm_tuner"
if str(_LLM_TUNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_LLM_TUNER_ROOT))

from nanobot.agent.tuning.schema import TuningRequirements, TuningSession

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_TUNER_EXECUTION_LOCK = asyncio.Lock()

_CHECKPOINT_SUBDIR = ".agent/tuning/sessions"


def _get_checkpoint_path(workspace: str, task_id: str) -> Path:
    return Path(workspace) / _CHECKPOINT_SUBDIR / task_id / "checkpoint.json"


def _save_checkpoint(state: Any, workspace: str, task_id: str) -> None:
    """Serialize *state* (an ExperimentState) to a JSON checkpoint file."""
    path = _get_checkpoint_path(workspace, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    # Write atomically
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    logger.info("checkpoint saved", task_id=task_id, trials=state.trial_number)


def _load_checkpoint(workspace: str, task_id: str) -> Any | None:
    """Load a previously saved ExperimentState checkpoint, or None."""
    path = _get_checkpoint_path(workspace, task_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        from src.workflow.state import ExperimentState
        return ExperimentState.model_validate(payload)
    except Exception:
        logger.exception("failed to load checkpoint", task_id=task_id)
        return None


def _cleanup_checkpoint(workspace: str, task_id: str) -> None:
    path = _get_checkpoint_path(workspace, task_id)
    if path.exists():
        path.unlink(missing_ok=True)
        # Remove parent dir if empty
        try:
            path.parent.rmdir()
        except OSError:
            pass
        logger.info("checkpoint cleaned up", task_id=task_id)


@contextmanager
def _configure_tuner_llm(
    provider: "LLMProvider | None",
    model: str | None,
) -> Any:
    """Bridge nanobot provider credentials into the standalone _llm_tuner settings.

    The _llm_tuner package maintains its own Settings object (loaded from
    ``LLMTUNER_*`` env vars / ``.env``) that is completely independent of
    nanobot's provider config.  Without this bridge the tuner agents cannot
    call any LLM and fall back to Bayesian-only optimization.
    """
    if provider is None or not provider.api_key:
        yield
        return

    from src.config import settings as tuner_settings

    original = {
        "deepseek_api_key": tuner_settings.deepseek_api_key,
        "deepseek_api_base": tuner_settings.deepseek_api_base,
        "llm_model": tuner_settings.llm_model,
        "llm_provider": tuner_settings.llm_provider,
    }

    tuner_settings.deepseek_api_key = provider.api_key
    if provider.api_base:
        tuner_settings.deepseek_api_base = provider.api_base
    if model:
        tuner_settings.llm_model = model
    tuner_settings.llm_provider = "deepseek"
    logger.info(
        "configured _llm_tuner LLM: provider={} model={} base={}",
        tuner_settings.llm_provider,
        tuner_settings.llm_model,
        tuner_settings.deepseek_api_base,
    )
    try:
        yield
    finally:
        tuner_settings.deepseek_api_key = original["deepseek_api_key"]
        tuner_settings.deepseek_api_base = original["deepseek_api_base"]
        tuner_settings.llm_model = original["llm_model"]
        tuner_settings.llm_provider = original["llm_provider"]


def _check_dependencies() -> list[str]:
    """Verify the minimal tuning dependencies required for this execution path."""
    missing: list[str] = []

    required = [
        ("structlog", "structlog"),
        ("langgraph", "langgraph"),
    ]
    optional_optimizer_imports = [
        ("skopt", "skopt"),
        ("optuna", "optuna"),
    ]

    for pkg_name, import_name in required:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)

    optimizer_available = False
    for _, import_name in optional_optimizer_imports:
        try:
            __import__(import_name)
            optimizer_available = True
            break
        except ImportError:
            continue
    if not optimizer_available:
        missing.append("skopt or optuna")

    return missing


async def _execute_tuning_workflow(
    session: TuningSession,
    req: TuningRequirements,
    provider: "LLMProvider | None",
    model: str | None,
    workspace: str,
    report_progress: "Callable[[str], Awaitable[None]] | None" = None,
) -> tuple[str, dict[str, Any]]:
    async with _TUNER_EXECUTION_LOCK:
        with _configure_tuner_llm(provider, model):
            # ── Resume from checkpoint if available ──────────────────────
            state = _load_checkpoint(workspace, session.task_id)
            if state is not None:
                logger.info(
                    "resuming from checkpoint",
                    task_id=session.task_id,
                    trials=state.trial_number,
                )
                if report_progress:
                    await report_progress(
                        f"Resuming from checkpoint (trial {state.trial_number}/{state.max_trials})"
                    )
            else:
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

            if req.dry_run:
                return _format_dry_run_report(state), {}

            from src.workflow.graph import create_workflow

            workflow = create_workflow()
            logger.info("starting tuning workflow: {} trials", state.max_trials)
            if report_progress and state.trial_number == 0:
                await report_progress(_format_start_progress(state))

            final_event = None
            last_reported_trial = state.trial_number
            last_saved_trial = state.trial_number
            async for event in workflow.astream(state, stream_mode="values"):
                # Keep state in sync with the latest event so checkpointing is accurate
                state = _reconstruct_state(state, event)
                node_name = event.get("phase", "unknown")
                trial_num = event.get("trial_number", 0)
                msg = f"[Trial {trial_num}] Phase: {node_name}"
                logger.info(msg)
                session.progress_messages.append(msg)
                final_event = event

                # Publish progress at trial boundaries
                if report_progress and trial_num > last_reported_trial:
                    progress_message = _format_trial_progress(event, state.max_trials)
                    if progress_message:
                        last_reported_trial = trial_num
                        await report_progress(progress_message)

                # Save checkpoint at trial boundaries
                if trial_num > last_saved_trial:
                    _save_checkpoint(state, workspace, session.task_id)
                    last_saved_trial = trial_num

            # Clean up checkpoint on success
            _cleanup_checkpoint(workspace, session.task_id)

            report = _format_final_report(state, session)
            structured = _extract_structured_results(state)
            return report, structured

async def _build_experiment_state(req: TuningRequirements) -> Any:
    """Build an ExperimentState from TuningRequirements."""
    from src.workflow.state import ExperimentState, GoalSpec

    kwargs = req.to_experiment_state_kwargs()
    kwargs["goals"] = [GoalSpec(**goal) for goal in kwargs["goals"]]
    return ExperimentState(**kwargs)


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
async def run_execution(
    session: TuningSession,
    _workspace: str,
    provider: "LLMProvider | None" = None,
    model: str | None = None,
    report_progress: "Callable[[str], Awaitable[None]] | None" = None,
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
        raise RuntimeError(msg)

    execution_issue = _validate_execution_requirements(req)
    if execution_issue:
        logger.error(execution_issue)
        raise RuntimeError(execution_issue)

    try:
        return await _execute_tuning_workflow(
            session, req, provider, model, _workspace,
            report_progress=report_progress,
        )
    except Exception as e:
        logger.exception("Tuning execution failed")
        session.progress_messages.append(f"Error: {e}")
        raise


def _validate_execution_requirements(req: TuningRequirements) -> str | None:
    """Ensure intake produced an executable tuning request."""
    if not req.host or not req.port or not req.config_file:
        return (
            "Direct-connect mode requires host, port, and config file path. "
            "Please provide all three connection details."
        )
    if req.benchmark_profile_path:
        profile_path = Path(req.benchmark_profile_path)
        if not profile_path.exists():
            return (
                f"Benchmark profile not found: {req.benchmark_profile_path}. "
                f"Please verify the file path or provide a run_command instead."
            )
    elif not req.run_command:
        return (
            "A tuning run needs either an existing benchmark profile YAML or a run command. "
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
        "best_trial_number",
        "tuning_proposal",
        "safety_verdict",
        "analysis_result",
        "orchestrator_decision",
        "tunable_parameters",
    ):
        if field in event:
            setattr(original, field, event[field])
    return original


def _extract_structured_results(state: Any) -> dict[str, Any]:
    """Extract structured tuning data for archiving and session storage."""
    advisor_recommendations = getattr(state, "advisor_recommendations", {})
    if hasattr(advisor_recommendations, "model_dump"):
        advisor_recommendations = advisor_recommendations.model_dump()
    return {
        "best_config": dict(getattr(state, "best_config", {})),
        "best_metrics": dict(getattr(state, "best_metrics", {})),
        "improvement_history": list(getattr(state, "improvement_history", [])),
        "trials_completed": getattr(state, "trial_number", 0),
        "target_system": getattr(state, "target_system", ""),
        "target_version": getattr(state, "target_version", ""),
        "experiment_name": getattr(state, "experiment_name", ""),
        "elapsed_hours": getattr(state, "elapsed_hours", 0.0),
        "advisor_recommendations": advisor_recommendations,
    }


def _format_start_progress(state: Any) -> str:
    return (
        f"Tuning started ({state.max_trials} trial max) — "
        f"baseline config loaded with {len(state.current_config)} params"
    )


def _format_trial_progress(event: dict[str, Any], max_trials: int) -> str | None:
    trial_num = event.get("trial_number", 0)
    best = event.get("best_metrics", {})
    improvement = event.get("improvement_history", [])
    if not best and not improvement:
        return None
    best_str = ", ".join(
        f"{k}={v:.0f}" if isinstance(v, float) else f"{k}={v}"
        for k, v in list(best.items())[:3]
    )
    imp_str = f" (Δ={improvement[-1]:+.1f}%)" if improvement else ""
    return f"Trial {trial_num}/{max_trials}: {best_str}{imp_str}"


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
