"""Rollback node: revert config to previous snapshot and restore service."""

from src.workflow.nodes.analyze import _record_trial_to_db
from src.workflow.state import ExperimentPhase, ExperimentState
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def rollback_config(state: ExperimentState) -> ExperimentState:
    logger.info("rolling back configuration", trial=state.trial_number)
    state.phase = ExperimentPhase.ROLLING_BACK
    state.consecutive_rollbacks += 1
    if state.current_trial is not None:
        state.current_trial.status = "rolled_back"

    try:
        from src.benchmark.custom_direct_runner import CustomDirectRunner
        from src.benchmark.runner import BenchmarkProfile

        profile = BenchmarkProfile(
            name="rollback",
            restart_command=state.restart_command,
            health_check_command=state.health_check_command,
        )

        host = state.target_host or state.redis_host
        port = state.target_port or state.redis_port
        creds = state.target_credentials or state.redis_password

        runner = CustomDirectRunner(
            profile=profile,
            config_path=state.direct_config_path,
            host=host,
            port=port,
            credentials=creds,
        )

        rolled_back = runner.rollback_config()
        if rolled_back:
            from src.parameters.manager import ParameterManager

            pm = ParameterManager(state.target_system)
            state.current_config = pm.parse_and_validate(rolled_back)

            # Write restored config
            config_text = pm.serialize_config(state.current_config)
            await runner.write_config(config_text)

            # Restart to apply the restored config
            await runner.restart()

            state.rollback_history.append({
                "trial": state.trial_number,
                "reason": state.errors[-1] if state.errors else "Unknown",
                "timestamp": str(
                    state.current_trial.created_at if state.current_trial else "unknown"
                ),
            })
            state.commit_current_trial(status="rolled_back")

            logger.info(
                "rollback successful",
                consecutive_rollbacks=state.consecutive_rollbacks,
            )
        else:
            logger.warning("no config snapshot to rollback to")
            state.commit_current_trial(status="rollback_unavailable")

    except Exception as e:
        logger.error("rollback failed", error=str(e))
        state.errors.append(f"Rollback failed: {e}")
        state.commit_current_trial(status="rollback_failed")

    await _record_trial_to_db(state)
    return state
