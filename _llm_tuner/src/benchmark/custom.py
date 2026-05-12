"""Custom workload runner for arbitrary benchmark commands."""

import asyncio
from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class CustomWorkloadRunner(BenchmarkRunner):
    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        if not profile.extra_args.get("command"):
            logger.warning("no custom command specified in profile")
            return BenchmarkMetrics()

        args = profile.extra_args["command"].split()
        logger.info("running custom benchmark", args=args)

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=profile.duration_sec or 300
        )

        raw_output = stdout.decode("utf-8")
        return self.parse_output(raw_output)

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        return BenchmarkMetrics(
            aggregate={"raw_output_length": float(len(raw_output))}
        )
