"""Abstract benchmark runner and benchmark profile definitions."""

import asyncio
import time
import yaml
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from src.metrics.models import BenchmarkMetrics
from src.metrics.collector import MetricsCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class BenchmarkProfile:
    name: str
    runner_type: str = "custom"
    tests: list[str] = field(default_factory=list)
    clients: int = 50
    requests: int = 1_000_000
    duration_sec: int = 0  # If set, overrides requests
    key_space_size: int = 1_000_000
    data_size: int = 128
    pipeline: int = 1
    threads: int = 1
    extra_args: dict[str, Any] = field(default_factory=dict)

    # User-provided lifecycle commands (shell templates)
    start_command: str = ""
    run_command: str = ""
    teardown_command: str = ""
    health_check_command: str = ""
    restart_command: str = ""

    # Output parsing
    output_format: str = "redis-benchmark-csv"
    metric_regex: dict[str, str] = field(default_factory=dict)

    # Environment
    env_vars: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkProfile":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class BenchmarkRunner(ABC):
    def __init__(self, container_id: str, metrics_collector: MetricsCollector | None = None):
        self.container_id = container_id
        self.metrics = metrics_collector or MetricsCollector(container_id)

    @abstractmethod
    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        """Run benchmark with given profile and return parsed metrics."""

    @abstractmethod
    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        """Parse raw benchmark stdout into structured metrics."""

    @staticmethod
    def for_system(target_system: str, container_id: str) -> "BenchmarkRunner":
        from src.benchmark.redis_benchmark import RedisBenchmarkRunner
        from src.benchmark.sysbench import SysbenchRunner
        from src.benchmark.custom import CustomWorkloadRunner

        if target_system == "redis":
            return RedisBenchmarkRunner(container_id)
        elif target_system == "mysql":
            return SysbenchRunner(container_id)
        else:
            logger.warning(
                "using CustomWorkloadRunner for unsupported system '%s'", target_system
            )
            return CustomWorkloadRunner(container_id)

    @staticmethod
    def load_profile(yaml_path: str | Path) -> BenchmarkProfile:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        return BenchmarkProfile.from_dict(data)
