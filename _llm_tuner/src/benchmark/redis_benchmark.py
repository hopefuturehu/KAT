"""Redis benchmark runner using redis-benchmark CLI."""

import asyncio
import re
from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class RedisBenchmarkRunner(BenchmarkRunner):
    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        args = [
            "redis-benchmark",
            "-h", "127.0.0.1",
            "-p", "6379",
            "-c", str(profile.clients),
            "-n", str(profile.requests),
            "-d", str(profile.data_size),
            "--csv",
            "-P", str(profile.pipeline),
        ]

        if profile.tests:
            for test in profile.tests:
                args.extend(["-t", test])

        logger.info("running redis-benchmark", args=args)

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=profile.duration_sec or 300
        )

        raw_output = stdout.decode("utf-8")
        if process.returncode != 0:
            logger.error("redis-benchmark failed", stderr=stderr.decode("utf-8"))
            return BenchmarkMetrics()

        return self.parse_output(raw_output)

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        operations: list[dict] = []
        # redis-benchmark --csv output format: "test","rps","avg_latency_ms",...
        for line in raw_output.strip().split("\n"):
            if not line.startswith('"'):
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                test_name = parts[0]
                try:
                    rps = float(parts[1])
                    operations.append({
                        "name": test_name,
                        "value": rps,
                        "unit": "rps",
                    })
                except (ValueError, IndexError):
                    pass

        # Also try parsing non-CSV output for latency information
        p99_match = re.search(r"p99[=:]\s*([\d.]+)", raw_output)
        p99_val = float(p99_match.group(1)) if p99_match else 0.0

        aggregate = {"total_rps": sum(op["value"] for op in operations)}
        if p99_val > 0:
            aggregate["p99_latency_ms"] = p99_val

        return BenchmarkMetrics(operations=operations, aggregate=aggregate)
