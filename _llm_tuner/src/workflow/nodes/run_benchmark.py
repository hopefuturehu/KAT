"""Run benchmark node: execute benchmarks and collect metrics."""

from src.workflow.state import ExperimentState, ExperimentPhase
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def execute_benchmark(state: ExperimentState) -> ExperimentState:
    logger.info("executing benchmark", trial=state.trial_number + 1)
    state.phase = ExperimentPhase.RUNNING_BENCHMARK

    try:
        if state.direct_mode:
            from src.benchmark.direct_runner import DirectRedisRunner
            from src.benchmark.runner import BenchmarkProfile

            runner = DirectRedisRunner(
                config_path=state.direct_config_path,
                host=state.redis_host,
                port=state.redis_port,
                password=state.redis_password,
                benchmark_cmd=state.direct_benchmark_cmd,
            )
        else:
            from src.benchmark.runner import BenchmarkRunner
            runner = BenchmarkRunner.for_system(state.target_system, state.container_id)

        from src.benchmark.runner import BenchmarkProfile

        profile_dict = {
            "name": "default",
            "runner_type": "redis_benchmark" if state.target_system == "redis" else "sysbench",
            "tests": ["set", "get"] if state.target_system == "redis" else ["oltp_read_write"],
            "clients": 50,
            "requests": 100000,
            "duration_sec": 30,
        }
        profile = BenchmarkProfile.from_dict(profile_dict)

        # Wrap in stability runner when enabled
        if state.stable_mode:
            from src.benchmark.stability import StabilityRunner
            runner = StabilityRunner(
                runner,
                warmup_requests=getattr(state, "stable_warmup_requests", 10000),
                iterations=getattr(state, "stable_iterations", 3),
                discard_outliers=False,
            )

        metrics = await runner.run(profile)

        if state.current_trial is None:
            state.begin_trial(dict(state.current_config), [])

        state.current_trial.benchmark_results = metrics.operations

        # Convert to metrics dict for goal checking
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
