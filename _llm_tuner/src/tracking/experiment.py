"""Experiment tracker — persists experiment state to database."""

import json
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models.experiment import Experiment
from src.db.models.trial import Trial
from src.db.models.parameter_change import ParameterChange
from src.db.models.benchmark_run import BenchmarkRun
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ExperimentTracker:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_experiment(self, state) -> Experiment:
        """Persist experiment from workflow state."""
        experiment = Experiment(
            name=state.experiment_name,
            target_system=state.target_system,
            target_version=state.target_version,
            goals_json=json.dumps([g.model_dump() for g in state.goals]),
            baseline_config_json=json.dumps(state.baseline_config),
            hardware_spec_json=json.dumps(state.hardware_spec),
            max_trials=state.max_trials,
            max_duration_hours=state.max_duration_hours,
            convergence_window=state.convergence_window,
            improvement_threshold_pct=state.improvement_threshold_pct,
            status="running",
        )
        self.session.add(experiment)
        await self.session.commit()
        return experiment

    async def record_trial(
        self,
        experiment_id: str,
        trial_number: int,
        config: dict,
        metrics: dict,
        parameter_changes: list[dict],
        improvement_pct: float,
        phase: str,
        status: str = "completed",
    ) -> Trial:
        """Record a completed trial."""
        trial = Trial(
            experiment_id=experiment_id,
            trial_number=trial_number,
            phase=phase,
            status=status,
            config_snapshot_json=json.dumps(config),
            metrics_json=json.dumps(metrics),
            improvement_pct=improvement_pct,
        )
        self.session.add(trial)
        await self.session.flush()

        for change in parameter_changes:
            pc = ParameterChange(
                trial_id=trial.id,
                parameter_name=change.get("parameter", "unknown"),
                old_value=str(change.get("old_value", "")),
                new_value=str(change.get("proposed_value", change.get("new_value", ""))),
                rationale=change.get("rationale", ""),
            )
            self.session.add(pc)

        await self.session.commit()
        return trial

    async def update_experiment_status(
        self, experiment_id: str, status: str, best_metrics: dict | None = None
    ) -> None:
        """Update experiment status and best metrics."""
        experiment = await self.session.get(Experiment, experiment_id)
        if experiment:
            experiment.status = status
            if best_metrics:
                experiment.best_metrics_json = json.dumps(best_metrics)
            if status in ("completed", "failed"):
                experiment.finished_at = datetime.utcnow()
            await self.session.commit()

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        return await self.session.get(Experiment, experiment_id)

    async def get_trials(self, experiment_id: str) -> list[Trial]:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        result = await self.session.execute(
            select(Trial)
            .where(Trial.experiment_id == experiment_id)
            .options(selectinload(Trial.parameter_changes))
            .order_by(Trial.trial_number)
        )
        return list(result.unique().scalars().all())

    async def get_parameter_changes(self, trial_id: str) -> list[ParameterChange]:
        from sqlalchemy import select
        result = await self.session.execute(
            select(ParameterChange)
            .where(ParameterChange.trial_id == trial_id)
            .order_by(ParameterChange.parameter_name)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_experiments(limit: int = 20) -> list[Experiment]:
        from sqlalchemy import select
        from src.db.session import async_session

        async with async_session() as session:
            result = await session.execute(
                select(Experiment)
                .order_by(Experiment.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_experiment_full(experiment_id: str) -> Experiment | None:
        from sqlalchemy import select
        from src.db.session import async_session

        async with async_session() as session:
            result = await session.execute(
                select(Experiment).where(Experiment.id == experiment_id)
            )
            return result.scalar_one_or_none()
