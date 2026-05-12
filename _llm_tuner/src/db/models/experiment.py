import uuid
import datetime
from sqlalchemy import String, Text, Integer, Float, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_system: Mapped[str] = mapped_column(String(50), nullable=False)
    target_version: Mapped[str] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="created")
    goals_json: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_config_json: Mapped[str] = mapped_column(Text, nullable=True)
    hardware_spec_json: Mapped[str] = mapped_column(Text, nullable=True)
    experiment_config_yaml: Mapped[str] = mapped_column(Text, nullable=True)
    max_trials: Mapped[int] = mapped_column(Integer, default=30)
    max_duration_hours: Mapped[float] = mapped_column(Float, default=8.0)
    current_trial: Mapped[int] = mapped_column(Integer, default=0)
    best_metrics_json: Mapped[str] = mapped_column(Text, nullable=True)
    convergence_window: Mapped[int] = mapped_column(Integer, default=5)
    improvement_threshold_pct: Mapped[float] = mapped_column(Float, default=2.0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    trials: Mapped[list["Trial"]] = relationship(
        "Trial", back_populates="experiment", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Experiment(id={self.id}, name={self.name}, status={self.status})>"
