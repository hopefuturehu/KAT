"""Custom direct runner — arbitrary target system via user-provided lifecycle commands.

Replaces the Docker-provisioning path. Users supply shell command templates for
start, run, teardown, health-check and restart. Config files are managed locally.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Recognised output formats — maps format name to a parser function.
_OUTPUT_PARSERS: dict[str, Any] = {}


def register_parser(name: str):
    """Register a named output parser for use in benchmark profiles."""

    def deco(fn):
        _OUTPUT_PARSERS[name] = fn
        return fn

    return deco


# ── Built-in parsers ─────────────────────────────────────────────────────────


@register_parser("redis-benchmark-csv")
def _parse_redis_benchmark_csv(raw: str) -> BenchmarkMetrics:
    operations: list[dict] = []
    for line in raw.strip().split("\n"):
        if not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            try:
                operations.append({"name": parts[0], "value": float(parts[1]), "unit": "rps"})
            except (ValueError, IndexError):
                pass

    aggregate: dict[str, float] = {"total_rps": sum(op["value"] for op in operations)}
    p99_match = re.search(r"p99[=:]\s*([\d.]+)", raw)
    if p99_match:
        aggregate["p99_latency_ms"] = float(p99_match.group(1))
    return BenchmarkMetrics(operations=operations, aggregate=aggregate)


@register_parser("sysbench")
def _parse_sysbench(raw: str) -> BenchmarkMetrics:
    aggregate: dict[str, float] = {}
    patterns = {
        "tps": r"transactions:\s+\d+\s+\(([\d.]+)\s+per sec",
        "qps": r"queries:\s+\d+\s+\(([\d.]+)\s+per sec",
        "p95_latency_ms": r"95th percentile:\s+([\d.]+)",
        "p99_latency_ms": r"99th percentile:\s+([\d.]+)",
        "avg_latency_ms": r"avg:\s+([\d.]+)",
        "read_ops": r"read:\s+(\d+)",
        "write_ops": r"write:\s+(\d+)",
    }
    for name, pat in patterns.items():
        m = re.search(pat, raw)
        if m:
            aggregate[name] = float(m.group(1))
    return BenchmarkMetrics(aggregate=aggregate)


@register_parser("regex")
def _parse_regex(raw: str, metric_regex: dict[str, str] | None = None) -> BenchmarkMetrics:
    """Generic regex-based parser driven by *metric_regex* dict.

    If *metric_regex* is empty, returns raw output length as a single metric.
    """
    if not metric_regex:
        return BenchmarkMetrics(aggregate={"raw_output_length": float(len(raw))})

    aggregate: dict[str, float] = {}
    for name, pat in metric_regex.items():
        m = re.search(pat, raw)
        if m:
            try:
                aggregate[name] = float(m.group(1))
            except (ValueError, IndexError):
                pass
    return BenchmarkMetrics(aggregate=aggregate)


@register_parser("raw")
def _parse_raw(raw: str) -> BenchmarkMetrics:
    lines = raw.strip().split("\n")
    return BenchmarkMetrics(
        aggregate={
            "line_count": float(len(lines)),
            "char_count": float(len(raw)),
        }
    )


# ── Runner ───────────────────────────────────────────────────────────────────


class CustomDirectRunner(BenchmarkRunner):
    """Generic runner that uses user-provided lifecycle commands.

    Parameters
    ----------
    profile: The benchmark profile carrying command templates and output config.
    config_path: Local path to the target's config file (may be empty).
    host, port, credentials: Connection details for command interpolation.
    """

    def __init__(
        self,
        profile: BenchmarkProfile,
        config_path: str | Path = "",
        host: str = "127.0.0.1",
        port: str = "6379",
        credentials: str = "",
    ):
        super().__init__(container_id=f"direct-{host}:{port}")
        self.config_path = Path(config_path) if config_path else Path(".")
        self.host = host
        self.port = port
        self.credentials = credentials

        # Lifecycle command templates
        self.start_cmd = profile.start_command
        self.run_cmd_tpl = profile.run_command
        self.teardown_cmd = profile.teardown_command
        self.health_cmd = profile.health_check_command
        self.restart_cmd = getattr(profile, "restart_command", "")

        # Output parsing
        self.output_format = profile.output_format
        self.metric_regex: dict[str, str] = getattr(profile, "metric_regex", {}) or {}

        self._config_history: list[Path] = []
        self._process: asyncio.subprocess.Process | None = None

    # ── Template interpolation ────────────────────────────────────────────────

    class _SafeFormatDict(dict[str, Any]):
        """Return ``{key}`` unchanged when a key is missing, so an incomplete
        template still produces a readable (if wrong) command instead of raising
        ``KeyError``."""

        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    def _render(self, template: str, extra: dict[str, Any] | None = None) -> str:
        """Substitute ``{host}``, ``{port}``, ``{config_path}``, and extra vars."""
        if not template:
            return ""
        ctx = {
            "host": self.host or "",
            "port": self.port or "",
            "credentials": self.credentials or "",
            "config_path": str(self.config_path),
        }
        if extra:
            ctx.update(extra)
        return template.format_map(self._SafeFormatDict(ctx))

    # ── Lifecycle commands ────────────────────────────────────────────────────

    async def start(self, timeout: int = 30) -> bool:
        """Run *start_command* and wait for health-check."""
        if not self.start_cmd:
            return True

        cmd = self._render(self.start_cmd)
        logger.info("starting target", cmd=cmd)
        self._process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for health
        for _ in range(max(timeout // 2, 1)):
            await asyncio.sleep(2)
            if await self.health_check(timeout=5):
                logger.info("target is healthy")
                return True

        logger.error("target failed health check after start")
        return False

    async def stop(self) -> None:
        """Run *teardown_command* and terminate any managed process."""
        if self.teardown_cmd:
            cmd = self._render(self.teardown_cmd)
            logger.info("teardown", cmd=cmd)
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
            except Exception as exc:
                logger.warning("teardown command failed", error=str(exc)[:100])

        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                self._process.kill()

    async def health_check(self, timeout: int = 10) -> bool:
        """Run the health-check command; return True on success."""
        if not self.health_cmd:
            return True

        cmd = self._render(self.health_cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode == 0
        except Exception:
            return False

    async def restart(self) -> bool:
        """Execute restart command and verify health."""
        if not self.restart_cmd:
            logger.warning("no restart command configured")
            return False

        cmd = self._render(self.restart_cmd)
        logger.info("restarting target", cmd=cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

        for _ in range(6):
            await asyncio.sleep(2)
            if await self.health_check(timeout=5):
                return True
        return False

    # ── Config management ─────────────────────────────────────────────────────

    async def read_config(self) -> str | None:
        if not self.config_path.exists():
            logger.warning("config file not found", path=str(self.config_path))
            return None
        return self.config_path.read_text()

    async def write_config(self, config_text: str) -> bool:
        """Write config text, keeping a backup for rollback."""
        if self.config_path.exists():
            backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            shutil.copy2(self.config_path, backup)
            self._config_history.append(backup)

        self.config_path.write_text(config_text)
        logger.info("config written", path=str(self.config_path))
        return True

    def rollback_config(self) -> str | None:
        if not self._config_history:
            return None
        backup = self._config_history.pop()
        if backup.exists():
            content = backup.read_text()
            shutil.copy2(backup, self.config_path)
            return content
        return None

    def snapshot_config(self) -> Path | None:
        """Take an explicit snapshot of the current config and return its path."""
        if not self.config_path.exists():
            return None
        snap = self.config_path.with_suffix(self.config_path.suffix + f".snap{len(self._config_history)}")
        shutil.copy2(self.config_path, snap)
        self._config_history.append(snap)
        return snap

    def restore_snapshot(self) -> str | None:
        """Restore the most recent snapshot."""
        return self.rollback_config()

    # ── Benchmark execution ───────────────────────────────────────────────────

    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        """Execute *run_command* and parse output."""
        if not self.run_cmd_tpl:
            logger.warning("no run command configured")
            return BenchmarkMetrics()

        cmd = self._render(
            self.run_cmd_tpl,
            extra={
                "clients": str(profile.clients),
                "requests": str(profile.requests),
                "duration": str(profile.duration_sec),
                "tests": ",".join(profile.tests) if profile.tests else "",
            },
        )

        logger.info("running benchmark", cmd=cmd)

        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=profile.duration_sec or 300,
        )

        raw_output = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0 and not raw_output.strip():
            logger.error("benchmark command failed", stderr=stderr.decode("utf-8", errors="replace"))
            return BenchmarkMetrics()

        return self.parse_output(raw_output)

    def parse_output(self, raw_output: str) -> BenchmarkMetrics:
        """Dispatch to the configured output parser."""
        parser = _OUTPUT_PARSERS.get(self.output_format)
        if parser is None:
            logger.warning("unknown output format", format=self.output_format)
            return _parse_raw(raw_output)

        if self.output_format == "regex":
            return parser(raw_output, self.metric_regex)
        return parser(raw_output)
