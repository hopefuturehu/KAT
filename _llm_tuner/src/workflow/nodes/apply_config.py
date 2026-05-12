"""Apply config node: write config changes, handle restarts, and verify health."""

import copy
from src.workflow.state import ExperimentState, ExperimentPhase
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def apply_configuration(state: ExperimentState) -> ExperimentState:
    logger.info("applying configuration", trial=state.trial_number + 1)
    state.phase = ExperimentPhase.APPLYING_CONFIG

    changes = state.tuning_proposal.get("changes", [])

    # Apply modifications from safety verdict if any
    if state.safety_verdict.get("verdict") == "APPROVE_WITH_MODIFICATIONS":
        modifications = {
            m["parameter"]: m["suggested_value"]
            for m in state.safety_verdict.get("suggested_modifications", [])
        }
        for change in changes:
            if change["parameter"] in modifications:
                change["proposed_value"] = modifications[change["parameter"]]

    # Create new config by applying changes
    new_config = copy.deepcopy(state.current_config)
    trial_changes: list[dict] = []
    for change in changes:
        param = change["parameter"]
        value = str(change["proposed_value"])
        new_config[param] = value
        trial_changes.append({
            "parameter": param,
            "old_value": state.current_config.get(param),
            "new_value": value,
            "proposed_value": value,
            "rationale": change.get("rationale", ""),
            "expected_effect": change.get("expected_effect", ""),
            "risk": change.get("risk", "unknown"),
        })

    state.begin_trial(new_config, trial_changes)

    needs_restart = any(
        p.get("restart_required")
        for p in state.tunable_parameters
        if p["name"] in [c["parameter"] for c in changes]
    )

    # Apply config to target environment
    try:
        from src.parameters.manager import ParameterManager
        pm = ParameterManager(state.target_system)
        config_text = pm.serialize_config(new_config)

        if state.direct_mode:
            # Direct mode: write local config file + CONFIG SET
            from src.benchmark.direct_runner import DirectRedisRunner
            runner = DirectRedisRunner(
                config_path=state.direct_config_path,
                host=state.redis_host,
                port=state.redis_port,
                password=state.redis_password,
                benchmark_cmd=state.direct_benchmark_cmd,
            )
            await runner.write_config(config_text, restart=needs_restart)
            if needs_restart:
                logger.warning(
                    "restart-requiring params changed — "
                    "you may need to restart Redis manually"
                )
        else:
            # Docker mode
            from src.environment.manager import TargetEnvironmentManager
            env_manager = TargetEnvironmentManager.for_container(state.container_id)

            config_path_map = {
                "redis": "/usr/local/etc/redis/redis.conf",
                "mysql": "/etc/mysql/my.cnf",
            }
            config_path = config_path_map.get(state.target_system, "/etc/config.conf")
            await env_manager.apply_config(config_text, config_path, restart=needs_restart)

            if needs_restart:
                healthy = await env_manager.health_check(timeout=30)
                if not healthy:
                    logger.warning("health check failed after restart — rolling back")
                    state.errors.append(f"Health check failed after config change (trial {state.trial_number})")
                    if state.current_trial is not None:
                        state.current_trial.status = "failed"
                    state.orchestrator_decision = {"action": "ROLLBACK"}
                    return state

        state.current_config = new_config
        logger.info("configuration applied", changed_params=[c["parameter"] for c in changes])

    except Exception as e:
        logger.error("failed to apply config", error=str(e))
        state.errors.append(str(e))
        if state.current_trial is not None:
            state.current_trial.status = "failed"
        state.orchestrator_decision = {"action": "ROLLBACK"}

    return state
