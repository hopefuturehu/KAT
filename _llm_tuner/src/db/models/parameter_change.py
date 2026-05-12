import uuid
import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


class ParameterChange(Base):
    __tablename__ = "parameter_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    trial_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trials.id", ondelete="CASCADE"), nullable=False
    )
    parameter_name: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[str] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str] = mapped_column(String(50), nullable=True)
    safety_approved: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trial: Mapped["Trial"] = relationship("Trial", back_populates="parameter_changes")

    def __repr__(self) -> str:
        return f"<ParameterChange({self.parameter_name}: {self.old_value} -> {self.new_value})>"
