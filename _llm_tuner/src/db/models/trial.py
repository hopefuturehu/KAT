import uuid
import datetime
from sqlalchemy import String, Text, Integer, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


class Trial(Base):
    __tablename__ = "trials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    experiment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    trial_number: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(30), default="pending")
    status: Mapped[str] = mapped_column(String(20), default="running")
    config_snapshot_json: Mapped[str] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=True)
    improvement_pct: Mapped[float] = mapped_column(Float, nullable=True)
    agent_notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    experiment: Mapped["Experiment"] = relationship("Experiment", back_populates="trials")
    parameter_changes: Mapped[list["ParameterChange"]] = relationship(
        "ParameterChange", back_populates="trial", cascade="all, delete-orphan"
    )
    benchmark_runs: Mapped[list["BenchmarkRun"]] = relationship(
        "BenchmarkRun", back_populates="trial", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Trial(id={self.id}, number={self.trial_number}, status={self.status})>"
