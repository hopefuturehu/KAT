"""Mock Redis benchmark runner — calls the simulator at redis-perf-optimizer/optimizer.py."""

import sys
from pathlib import Path

# Add the mock optimizer to Python path
OPTIMIZER_PATH = Path("/Users/huyang/codespace/redis-perf-optimizer")
if str(OPTIMIZER_PATH) not in sys.path:
    sys.path.insert(0, str(OPTIMIZER_PATH))

from optimizer import parse_config, simulate_benchmark, score_config, BENCH_OPS  # type: ignore
from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class MockRedisRunner(BenchmarkRunner):
    """Benchmark runner that calls the local Redis simulator instead of real redis-benchmark."""

    def __init__(self, config_file: str = ""):
        super().__init__(container_id="mock")
        self.config_file = config_file or str(OPTIMIZER_PATH / "test.conf")

    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        logger.info("running mock redis benchmark", config=self.config_file)

        # Parse the config file at the current path
        config_dict = parse_config(self.config_file)
        test_filter = {t.upper() for t in profile.tests} if profile.tests else set()

        # Call the simulator
        import random
        random.seed(42)

        results = simulate_benchmark(config_dict, test_filter)

        # Also get the score
        score = score_config(config_dict)

        # Convert to BenchmarkMetrics
        operations = []
        total_rps = 0.0
        for r in results:
            ops_rps = r.get("rps", 0)
            total_rps += ops_rps
            operations.append({"name": r["name"], "value": ops_rps, "unit": "rps"})

        for r in results:
            if "p99" in r:
                operations.append({
                    "name": f"{r['name']}_p99_latency",
                    "value": r["p99"],
                    "unit": "ms",
                })

        aggregate = {
            "total_rps": total_rps,
            "avg_rps": total_rps / max(len(results), 1),
            "qps": total_rps / max(len(results), 1),
            "score_pct": score.get("pct", 0),
            "score_total": score.get("total", 0),
            "score_max": score.get("max_total", 84),
            "multiplier": sum(r.get("multiplier", 0) for r in results) / max(len(results), 1),
        }

        # Also collect latency from all ops, find the p99 among them
        p99_values = [r.get("p99", 0) for r in results if "p99" in r]
        if p99_values:
            aggregate["p99_latency_ms"] = max(p99_values)

        logger.info(
            "mock benchmark complete",
            total_rps=f"{total_rps:,.0f}",
            score=f"{score.get('pct', 0):.1f}%",
            multiplier=f"{aggregate['multiplier']:.1%}",
        )

        return BenchmarkMetrics(operations=operations, aggregate=aggregate)

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        return BenchmarkMetrics()
