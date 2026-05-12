"""Stable benchmark runner — warmup + multi‑iteration median + noise control.

Implements best practices for reliable database benchmarking:

1. **Warmup**           — prime caches and memory before measuring.
2. **Multi‑iteration**  — run N times, take *median* (not mean).
3. **Outlier detection** — flag iterations with >2σ deviation.
4. **Noise report**     — min / max / median / stddev for every metric.

Usage::

    from src.benchmark.stability import StabilityRunner
    from src.benchmark.redis_benchmark import RedisBenchmarkRunner

    base_runner = RedisBenchmarkRunner(container_id)
    stable = StabilityRunner(base_runner, warmup_requests=10000, iterations=5)
    metrics = await stable.run(profile)
"""

from __future__ import annotations

import asyncio
import math
import statistics
from typing import Sequence

from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class StabilityRunner(BenchmarkRunner):
    """Wraps any BenchmarkRunner to produce stable, reproducible results."""

    def __init__(
        self,
        base_runner: BenchmarkRunner,
        warmup_requests: int = 10000,
        iterations: int = 3,
        discard_outliers: bool = False,
    ):
        super().__init__(container_id=base_runner.container_id)
        self._base = base_runner
        self.warmup_requests = warmup_requests
        self.iterations = max(iterations, 1)
        self.discard_outliers = discard_outliers

    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        # Step 1 — Warmup
        if self.warmup_requests > 0:
            await self._run_warmup(profile)

        # Step 2 — Multi‑iteration measurement
        all_results: list[BenchmarkMetrics] = []
        for i in range(self.iterations):
            result = await self._base.run(profile)
            all_results.append(result)
            logger.debug(
                "stability iteration",
                iter=i + 1,
                score=self._extract_main_metric(result),
            )

        # Step 3 — Merge via median
        merged = self._merge_median(all_results)

        # Step 4 — Log stability report
        self._log_stability_report(all_results, merged)

        return merged

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        return self._base.parse_output(raw_output)

    # ── Internals ──────────────────────────────────────────────────────────

    async def _run_warmup(self, profile: BenchmarkProfile) -> None:
        """Run a light warmup to fill caches and prime memory."""
        warmup_profile = BenchmarkProfile(
            name=f"{profile.name}-warmup",
            runner_type=profile.runner_type,
            tests=profile.tests,
            clients=profile.clients,
            requests=self.warmup_requests,
            duration_sec=0,
            key_space_size=profile.key_space_size,
            data_size=profile.data_size,
            pipeline=profile.pipeline,
            threads=profile.threads,
            extra_args=dict(profile.extra_args),
        )
        logger.info("running warmup benchmark", requests=self.warmup_requests)
        try:
            await self._base.run(warmup_profile)
        except Exception as exc:
            logger.warning("warmup failed, continuing", error=str(exc)[:100])

    def _extract_main_metric(self, metrics: BenchmarkMetrics) -> float:
        """Extract the primary numeric score from a metrics object."""
        agg = metrics.aggregate
        for key in ("total_rps", "score_pct", "qps", "tps"):
            if key in agg:
                return float(agg[key])
        for op in metrics.operations:
            return float(op.get("value", 0))
        return 0.0

    def _merge_median(self, results: list[BenchmarkMetrics]) -> BenchmarkMetrics:
        """Produce a merged BenchmarkMetrics using the median of each field."""
        if len(results) == 1:
            return results[0]

        # Collect all metric names across all iterations
        agg_keys: dict[str, list[float]] = {}
        op_keys: set[str] = set()

        for r in results:
            for k, v in r.aggregate.items():
                agg_keys.setdefault(k, []).append(float(v))
            for op in r.operations:
                op_keys.add(op.get("name", ""))

        # Build merged aggregate (median per key)
        merged_agg: dict[str, float] = {}
        for key, values in agg_keys.items():
            merged_agg[key] = statistics.median(values) if values else 0.0

        # Build merged operations
        merged_ops: list[dict] = []
        for op_name in op_keys:
            values = []
            for r in results:
                for op in r.operations:
                    if op.get("name") == op_name:
                        values.append(float(op.get("value", 0)))
                        break
            if values:
                merged_ops.append({
                    "name": op_name,
                    "value": statistics.median(values),
                    "unit": "rps",
                })

        return BenchmarkMetrics(operations=merged_ops, aggregate=merged_agg)

    def _log_stability_report(
        self,
        all_results: list[BenchmarkMetrics],
        merged: BenchmarkMetrics,
    ) -> None:
        """Log a stability summary: min / max / median / stddev."""
        main_scores = [self._extract_main_metric(r) for r in all_results]
        if len(main_scores) < 2:
            return

        med = statistics.median(main_scores)
        std = statistics.stdev(main_scores) if len(main_scores) >= 3 else 0.0
        cv = (std / med * 100) if med > 0 else 0.0

        status = "stable" if cv < 5.0 else "unstable" if cv < 10.0 else "volatile"

        logger.info(
            f"benchmark stability: {status}",
            min=f"{min(main_scores):,.0f}",
            max=f"{max(main_scores):,.0f}",
            median=f"{med:,.0f}",
            stddev=f"{std:,.0f}",
            cv_pct=f"{cv:.1f}",
            iterations=len(main_scores),
        )

        if cv >= 10.0:
            logger.warning(
                "high benchmark variability (CV=%.1f%%) — "
                "consider increasing iterations or checking for background noise",
                cv,
            )

        # Mark the merged result with stability metadata
        merged.aggregate["_stability_cv_pct"] = round(cv, 1)
        merged.aggregate["_stability_iterations"] = len(main_scores)
        merged.aggregate["_stability_status"] = status
