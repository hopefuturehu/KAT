import uuid
import datetime
from sqlalchemy import String, Text, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    trial_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trials.id", ondelete="CASCADE"), nullable=False
    )
    profile_name: Mapped[str] = mapped_column(String(100), nullable=False)
    runner_type: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_output: Mapped[str] = mapped_column(Text, nullable=True)
    parsed_metrics_json: Mapped[str] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trial: Mapped["Trial"] = relationship("Trial", back_populates="benchmark_runs")

    def __repr__(self) -> str:
        return f"<BenchmarkRun(profile={self.profile_name}, status={self.status})>"
