"""Metrics collector for system and application-level metrics."""

import asyncio
import os
from datetime import datetime
from src.metrics.models import SystemMetrics, DataPoint
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SystemMetricsCollector:
    """Collect system-level metrics (CPU, memory, disk I/O)."""

    def __init__(self, container_id: str | None = None):
        self.container_id = container_id

    async def collect(self) -> SystemMetrics:
        """Collect current system metrics. Uses /proc for container-aware metrics."""
        cpu = await self._get_cpu()
        mem = await self._get_memory()
        disk_read, disk_write = await self._get_disk_io()

        return SystemMetrics(
            cpu_percent=cpu,
            memory_percent=mem,
            disk_iops_read=disk_read,
            disk_iops_write=disk_write,
        )

    async def _get_cpu(self) -> float:
        try:
            import psutil
            return psutil.cpu_percent(interval=0.1)
        except ImportError:
            return 0.0

    async def _get_memory(self) -> float:
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0

    async def _get_disk_io(self) -> tuple[float, float]:
        try:
            import psutil
            io = psutil.disk_io_counters()
            return (
                float(io.read_count) if io else 0.0,
                float(io.write_count) if io else 0.0,
            )
        except ImportError:
            return 0.0, 0.0


class MetricsCollector:
    """Application-level metrics + system metrics collector."""

    def __init__(self, container_id: str | None = None):
        self.system = SystemMetricsCollector(container_id)
        self._data_points: list[DataPoint] = []

    async def collect_system(self) -> SystemMetrics:
        return await self.system.collect()

    def record_metric(self, name: str, value: float, unit: str, **tags: str) -> None:
        self._data_points.append(
            DataPoint(name=name, value=value, unit=unit, tags=tags)
        )

    def record_operation(
        self, op_name: str, rps: float, p50: float, p95: float, p99: float
    ) -> None:
        self.record_metric(f"{op_name}.rps", rps, "ops/sec")
        self.record_metric(f"{op_name}.p50", p50, "ms")
        self.record_metric(f"{op_name}.p95", p95, "ms")
        self.record_metric(f"{op_name}.p99", p99, "ms")

    def flush(self) -> list[DataPoint]:
        points = list(self._data_points)
        self._data_points.clear()
        return points
