"""Run benchmark node — execute benchmarks and collect metrics via CustomDirectRunner."""

from pathlib import Path

from src.benchmark.runner import BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.workflow.state import ExperimentPhase, ExperimentState
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def execute_benchmark(state: ExperimentState) -> ExperimentState:
    logger.info("executing benchmark", trial=state.trial_number + 1)
    state.phase = ExperimentPhase.RUNNING_BENCHMARK

    try:
        # ── Resolve benchmark profile ─────────────────────────────────────────
        profile = _resolve_profile(state)

        # ── Build runner ──────────────────────────────────────────────────────
        from src.benchmark.custom_direct_runner import CustomDirectRunner

        runner = CustomDirectRunner(
            profile=profile,
            config_path=state.direct_config_path,
            host=state.target_host or state.redis_host,
            port=state.target_port or state.redis_port,
            credentials=state.target_credentials or state.redis_password,
        )

        # ── Stability wrapper ─────────────────────────────────────────────────
        if state.stable_mode:
            from src.benchmark.stability import StabilityRunner

            runner = StabilityRunner(
                runner,
                warmup_requests=getattr(state, "stable_warmup_requests", 10000),
                iterations=getattr(state, "stable_iterations", 3),
                discard_outliers=False,
            )

        # ── Execute ───────────────────────────────────────────────────────────
        metrics: BenchmarkMetrics = await runner.run(profile)

        # ── Record results ────────────────────────────────────────────────────
        if state.current_trial is None:
            state.begin_trial(dict(state.current_config), [])

        state.current_trial.benchmark_results = metrics.operations

        metrics_dict: dict[str, float] = {}
        metrics_dict.update(metrics.aggregate)
        for op in metrics.operations:
            metrics_dict[op.get("name", "unknown")] = float(op.get("value", 0))

        state.current_trial.metrics = metrics_dict
        logger.info("benchmark complete", metrics=metrics.aggregate)

    except Exception as e:
        logger.error("benchmark failed", error=str(e))
        state.errors.append(f"Benchmark failed: {e}")
        if state.current_trial is not None:
            state.current_trial.status = "failed"

    return state


def _resolve_profile(state: ExperimentState) -> BenchmarkProfile:
    """Build a BenchmarkProfile from state fields or a YAML file."""

    # 1) YAML file takes highest priority
    if state.benchmark_profile_path:
        path = Path(state.benchmark_profile_path)
        if path.exists():
            logger.info("loading benchmark profile from YAML", path=str(path))
            return BenchmarkRunner_shim.load_profile(path)

    # 2) Build from state fields (user-provided commands)
    profile_dict: dict = {
        "name": state.experiment_name or "custom",
        "runner_type": "custom",
        "tests": ["set", "get"] if state.target_system == "redis" else [],
        "clients": 50,
        "requests": 100000,
        "duration_sec": 30,
        "output_format": state.output_format or "redis-benchmark-csv",
        "start_command": state.start_command,
        "run_command": state.run_command,
        "teardown_command": state.teardown_command,
        "health_check_command": state.health_check_command,
        "restart_command": state.restart_command,
        "metric_regex": state.metric_regex,
    }

    # Carry over profile-level overrides from state if a profile path was set
    # but not found — use the command fields from state directly.
    if state.run_command:
        profile_dict["run_command"] = state.run_command  # type: ignore[assignment]

    return BenchmarkProfile.from_dict(profile_dict)


# Re-export for _resolve_profile
from src.benchmark import runner as BenchmarkRunner_shim
