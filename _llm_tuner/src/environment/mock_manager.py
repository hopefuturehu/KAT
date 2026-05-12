"""File-based environment manager for mock/simulator testing — no Docker required."""

import asyncio
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MockContainerInfo:
    container_id: str
    name: str
    status: str
    config_path: Path


class MockEnvironmentManager:
    """File-based config manager for testing with the Redis simulator."""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self._history: list[Path] = []
        self.active_container = None

    async def provision(self, experiment_id: str) -> MockContainerInfo:
        """Set up the mock environment — ensure config file exists."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.config_path.exists():
            self.config_path.write_text("")

        info = MockContainerInfo(
            container_id=f"mock-{experiment_id[:8]}",
            name=f"mock-redis-{experiment_id[:8]}",
            status="running",
            config_path=self.config_path,
        )
        self.active_container = info
        logger.info("mock environment ready", config=str(self.config_path))
        return info

    async def apply_config(
        self, config_text: str, restart: bool = False
    ) -> bool:
        """Write config to file."""
        # Backup current config
        if self.config_path.exists():
            backup = self.config_path.with_suffix(".conf.bak")
            shutil.copy2(self.config_path, backup)
            self._history.append(backup)

        self.config_path.write_text(config_text)
        logger.info("config written", path=str(self.config_path))

        if restart:
            await asyncio.sleep(0.1)  # Simulate restart delay

        return True

    async def get_config(self) -> str | None:
        """Read current config from file."""
        if not self.config_path.exists():
            return None
        return self.config_path.read_text()

    async def health_check(self, timeout: int = 30) -> bool:
        """Mock health check — file is reachable."""
        return self.config_path.parent.exists()

    async def teardown(self) -> None:
        """Clean up backup files."""
        for backup in self._history:
            if backup.exists():
                backup.unlink(missing_ok=True)
        self.active_container = None
        logger.info("mock environment torn down")

    def rollback_config(self) -> str | None:
        """Rollback to previous config."""
        if not self._history:
            return None
        backup = self._history.pop()
        if backup.exists():
            content = backup.read_text()
            shutil.copy2(backup, self.config_path)
            return content
        return None
