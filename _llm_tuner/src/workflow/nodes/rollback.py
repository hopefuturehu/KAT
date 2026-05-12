"""Rollback node: revert config to previous state."""

from src.workflow.state import ExperimentState, ExperimentPhase
from src.workflow.nodes.analyze import _record_trial_to_db
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def rollback_config(state: ExperimentState) -> ExperimentState:
    logger.info("rolling back configuration", trial=state.trial_number)
    state.phase = ExperimentPhase.ROLLING_BACK
    state.consecutive_rollbacks += 1
    if state.current_trial is not None:
        state.current_trial.status = "rolled_back"

    try:
        if state.direct_mode:
            from src.benchmark.direct_runner import DirectRedisRunner
            env = DirectRedisRunner(
                config_path=state.direct_config_path,
                host=state.redis_host, port=state.redis_port,
                password=state.redis_password,
                benchmark_cmd=state.direct_benchmark_cmd,
            )
        else:
            from src.environment.manager import TargetEnvironmentManager
            env = TargetEnvironmentManager.for_container(state.container_id)

        rolled_back = env.rollback_config()
        if rolled_back:
            from src.parameters.manager import ParameterManager
            pm = ParameterManager(state.target_system)
            state.current_config = pm.parse_and_validate(rolled_back)

            config_text = pm.serialize_config(state.current_config)

            if state.direct_mode:
                await env.write_config(config_text, restart=True)
            else:
                config_path_map = {
                    "redis": "/usr/local/etc/redis/redis.conf",
                    "mysql": "/etc/mysql/my.cnf",
                }
                config_path = config_path_map.get(state.target_system, "/etc/config.conf")
                await env.apply_config(config_text, config_path, restart=True)

            state.rollback_history.append({
                "trial": state.trial_number,
                "reason": state.errors[-1] if state.errors else "Unknown",
                "timestamp": str(state.current_trial.created_at if state.current_trial else "unknown"),
            })
            state.commit_current_trial(status="rolled_back")

            logger.info("rollback successful",
                         consecutive_rollbacks=state.consecutive_rollbacks)
        else:
            logger.warning("no config to rollback to")
            state.commit_current_trial(status="rollback_unavailable")

    except Exception as e:
        logger.error("rollback failed", error=str(e))
        state.errors.append(f"Rollback failed: {e}")
        state.commit_current_trial(status="rollback_failed")

    await _record_trial_to_db(state)
    return state
