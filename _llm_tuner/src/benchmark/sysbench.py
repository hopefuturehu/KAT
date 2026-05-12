"""Sysbench benchmark runner for MySQL."""

import asyncio
import re
from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SysbenchRunner(BenchmarkRunner):
    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        mysql_host = profile.extra_args.get("mysql_host", "127.0.0.1")
        mysql_port = profile.extra_args.get("mysql_port", "3306")
        mysql_user = profile.extra_args.get("mysql_user", "root")
        mysql_password = profile.extra_args.get("mysql_password", "llmtuner123")
        mysql_db = profile.extra_args.get("mysql_db", "benchmark")

        test_type = profile.tests[0] if profile.tests else "oltp_read_write"

        common_args = [
            "sysbench", test_type,
            f"--mysql-host={mysql_host}",
            f"--mysql-port={mysql_port}",
            f"--mysql-user={mysql_user}",
            f"--mysql-password={mysql_password}",
            f"--mysql-db={mysql_db}",
            f"--threads={profile.threads or profile.clients}",
            f"--tables={profile.extra_args.get('tables', 10)}",
            f"--table-size={profile.extra_args.get('table_size', 100000)}",
        ]

        # Prepare data if needed
        if profile.extra_args.get("prepare", False):
            prepare_args = common_args + ["prepare"]
            logger.info("sysbench prepare", test=test_type)
            process = await asyncio.create_subprocess_exec(
                *prepare_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=600)

        # Run benchmark
        run_args = common_args + [
            f"--time={profile.duration_sec or 60}",
            f"--report-interval=10",
            "run",
        ]

        logger.info("running sysbench", test=test_type, threads=profile.clients)

        process = await asyncio.create_subprocess_exec(
            *run_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=(profile.duration_sec or 60) + 60
        )

        raw_output = stdout.decode("utf-8")
        if process.returncode != 0:
            logger.error("sysbench failed", stderr=stderr.decode("utf-8"))
            return BenchmarkMetrics()

        return self.parse_output(raw_output)

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        operations: list[dict] = []
        aggregate: dict = {}

        # Parse sysbench output
        # transactions per sec
        tps_match = re.search(r"transactions:\s*\d+\s*\(([\d.]+)\s*per sec", raw_output)
        if tps_match:
            aggregate["tps"] = float(tps_match.group(1))

        # queries per sec
        qps_match = re.search(r"queries:\s*\d+\s*\(([\d.]+)\s*per sec", raw_output)
        if qps_match:
            aggregate["qps"] = float(qps_match.group(1))

        # latency
        for pct in ["95th", "99th"]:
            lat_match = re.search(
                rf"{pct} percentile:\s*([\d.]+)", raw_output
            )
            if lat_match:
                metric_name = f"p{pct.replace('th', '')}_latency_ms"
                aggregate[metric_name] = float(lat_match.group(1))

        # avg latency
        avg_match = re.search(r"avg:\s*([\d.]+)", raw_output)
        if avg_match:
            aggregate["avg_latency_ms"] = float(avg_match.group(1))

        # Read/write operations
        read_match = re.search(r"read:\s*(\d+)", raw_output)
        write_match = re.search(r"write:\s*(\d+)", raw_output)
        if read_match:
            aggregate["read_ops"] = float(read_match.group(1))
        if write_match:
            aggregate["write_ops"] = float(write_match.group(1))

        if "tps" in aggregate:
            operations.append({"name": "tps", "value": aggregate["tps"], "unit": "tps"})
        if "qps" in aggregate:
            operations.append({"name": "qps", "value": aggregate["qps"], "unit": "qps"})

        return BenchmarkMetrics(operations=operations, aggregate=aggregate)
