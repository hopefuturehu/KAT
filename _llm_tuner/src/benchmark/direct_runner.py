"""Direct Redis runner — manage a local redis.conf + run user-provided benchmarks.

Used when connecting to an already-running Redis (no Docker provisioning).
Config changes are written to a local config file AND applied live via
``redis-cli CONFIG SET`` for immediate effect.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from src.benchmark.runner import BenchmarkRunner, BenchmarkProfile
from src.metrics.models import BenchmarkMetrics
from src.utils.logging import get_logger

logger = get_logger(__name__)


class DirectRedisRunner(BenchmarkRunner):
    """Manages a Redis instance via local config file + redis-cli + user benchmark cmd.

    Parameters
    ----------
    config_path: Path to the local redis.conf file.
    host, port, password: Redis connection details.
    benchmark_cmd: Shell command template for benchmarking. Use ``{host}``,
        ``{port}`` placeholders.  Example::

            "redis-benchmark -h {host} -p {port} -c 50 -n 100000 --csv"
    """

    def __init__(
        self,
        config_path: str | Path,
        host: str = "127.0.0.1",
        port: str = "6379",
        password: str = "",
        benchmark_cmd: str = "",
    ):
        super().__init__(container_id="direct")
        self.config_path = Path(config_path)
        self.host = host
        self.port = port
        self.password = password
        self.benchmark_cmd = benchmark_cmd

        self._config_history: list[Path] = []

    # ── Config I/O ─────────────────────────────────────────────────────────

    async def read_config(self) -> str | None:
        """Read the current redis.conf from the local file."""
        if not self.config_path.exists():
            logger.warning("config file not found", path=str(self.config_path))
            return None
        return self.config_path.read_text()

    async def write_config(self, config_text: str, restart: bool = False) -> bool:
        """Write config text to the local file and apply live via CONFIG SET."""
        # Backup
        if self.config_path.exists():
            backup = self.config_path.with_suffix(".conf.bak")
            shutil.copy2(self.config_path, backup)
            self._config_history.append(backup)

        # Write file
        self.config_path.write_text(config_text)
        logger.info("config written to file", path=str(self.config_path))

        # Apply live via CONFIG SET for non-restart params
        await self._apply_via_config_set(config_text)

        if restart:
            logger.warning(
                "restart-requiring parameters were changed — "
                "please restart your Redis instance manually"
            )

        return True

    async def _apply_via_config_set(self, config_text: str) -> None:
        """Parse config text and apply each line via redis-cli CONFIG SET."""
        redis_cli = _find_redis_cli()
        auth_args = _auth_args(self.password)

        applied = 0
        for line in config_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                key, value = parts
                try:
                    subprocess.run(
                        [*redis_cli, "-h", self.host, "-p", self.port, *auth_args,
                         "CONFIG", "SET", key, value],
                        capture_output=True, text=True, timeout=5,
                    )
                    applied += 1
                except Exception:
                    pass

        if applied > 0:
            logger.info("applied config via CONFIG SET", count=applied)

    async def health_check(self, timeout: int = 10) -> bool:
        """Ping Redis to verify it's alive."""
        redis_cli = _find_redis_cli()
        auth_args = _auth_args(self.password)
        try:
            result = subprocess.run(
                [*redis_cli, "-h", self.host, "-p", self.port, *auth_args, "PING"],
                capture_output=True, text=True, timeout=timeout,
            )
            return "PONG" in result.stdout
        except Exception:
            return False

    def rollback_config(self) -> str | None:
        """Restore the previous config file."""
        if not self._config_history:
            return None
        backup = self._config_history.pop()
        if backup.exists():
            content = backup.read_text()
            shutil.copy2(backup, self.config_path)
            return content
        return None

    # ── Benchmark execution ────────────────────────────────────────────────

    async def run(self, profile: BenchmarkProfile) -> BenchmarkMetrics:
        """Execute the user-provided benchmark command."""
        cmd = self.benchmark_cmd.format(
            host=self.host, port=self.port, config_path=str(self.config_path),
        )
        if not cmd:
            cmd = f"redis-benchmark -h {self.host} -p {self.port} -c 50 -n 100000 --csv"

        logger.info("running direct benchmark", cmd=cmd)

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
        """Parse redis-benchmark --csv output."""
        operations: list[dict] = []
        for line in raw_output.strip().split("\n"):
            if not line.startswith('"'):
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try:
                    operations.append({
                        "name": parts[0],
                        "value": float(parts[1]),
                        "unit": "rps",
                    })
                except (ValueError, IndexError):
                    pass

        aggregate = {"total_rps": sum(op["value"] for op in operations)}

        # Try extracting p99 from non-CSV lines
        p99_match = re.search(r"p99[=:]\s*([\d.]+)", raw_output)
        if p99_match:
            aggregate["p99_latency_ms"] = float(p99_match.group(1))

        return BenchmarkMetrics(operations=operations, aggregate=aggregate)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _find_redis_cli() -> list[str]:
    """Find redis-cli binary. Return as list for subprocess."""
    for candidate in [
        "redis-cli",
        "/usr/local/bin/redis-cli",
        "/opt/homebrew/bin/redis-cli",
    ]:
        if shutil.which(candidate):
            return [candidate]
    return ["redis-cli"]


def _auth_args(password: str) -> list[str]:
    if password:
        return ["-a", password, "--no-auth-warning"]
    return []
