"""Metrics data models."""

from pydantic import BaseModel, Field
from datetime import datetime


class DataPoint(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    name: str
    value: float
    unit: str
    tags: dict[str, str] = Field(default_factory=dict)


class SystemMetrics(BaseModel):
    cpu_percent: float
    memory_percent: float
    disk_iops_read: float = 0.0
    disk_iops_write: float = 0.0
    network_rx_bytes: float = 0.0
    network_tx_bytes: float = 0.0


class BenchmarkMetrics(BaseModel):
    operations: list[dict] = Field(default_factory=list)
    aggregate: dict = Field(default_factory=dict)
    system_metrics: SystemMetrics | None = None

    def get_metric(self, metric_name: str) -> float | None:
        """Extract a specific metric from aggregate or operations."""
        if metric_name in self.aggregate:
            return float(self.aggregate[metric_name])
        for op in self.operations:
            if op.get("name") == metric_name:
                return float(op.get("value", 0))
        return None


class MetricsBundle(BaseModel):
    trial_id: str
    benchmark_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    benchmark_metrics: BenchmarkMetrics
    system_metrics: SystemMetrics | None = None
