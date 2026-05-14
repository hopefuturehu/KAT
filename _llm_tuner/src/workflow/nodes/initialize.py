"""Initialize node: provision environment, capture baseline, set up experiment."""

from datetime import UTC, datetime
from src.workflow.state import ExperimentState, ExperimentPhase
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def initialize_experiment(state: ExperimentState) -> ExperimentState:
    logger.info("initializing experiment", name=state.experiment_name)
    state.phase = ExperimentPhase.INITIALIZING
    state.start_time = datetime.now(UTC)

    # Capture hardware spec
    import platform
    import os

    state.hardware_spec = {
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }

    # If baseline_config is not set, use current_config as baseline
    if not state.baseline_config and state.current_config:
        state.baseline_config = dict(state.current_config)

    # Seed improvement history with 0 for pre-baseline
    state.improvement_history = []

    # Persist experiment to database
    try:
        from src.db.session import async_session
        from src.tracking.experiment import ExperimentTracker

        async with async_session() as session:
            tracker = ExperimentTracker(session)
            experiment = await tracker.create_experiment(state)
            state.experiment_id = experiment.id
            logger.info("experiment persisted to database", id=experiment.id)
    except Exception as exc:
        logger.warning("failed to persist experiment to database (continuing)", error=str(exc))

    logger.info(
        "experiment initialized",
        hardware=state.hardware_spec,
        goals_count=len(state.goals),
    )
    return state
