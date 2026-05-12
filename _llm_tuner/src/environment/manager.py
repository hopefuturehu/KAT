"""Target environment manager using Docker SDK."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import docker

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ContainerInfo:
    container_id: str
    name: str
    image: str
    ports: dict[str, str]
    status: str
    env_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class EnvironmentConfig:
    template: str
    cpu_limit: str = "4"
    memory_limit: str = "8g"
    docker_image: str = ""
    port_mappings: dict[str, str] = field(default_factory=dict)


class TargetEnvironmentManager:
    _registry: ClassVar[dict[str, "TargetEnvironmentManager"]] = {}

    def __init__(self, docker_client: docker.DockerClient | None = None):
        self.client = docker_client or docker.from_env()
        self.active_container: docker.models.containers.Container | None = None
        self._config_history: list[str] = []

    @classmethod
    def for_container(cls, container_id: str) -> "TargetEnvironmentManager":
        """Return the live manager bound to a provisioned container."""
        manager = cls._registry.get(container_id)
        if manager is None:
            raise RuntimeError(f"No environment manager registered for container {container_id}")
        return manager

    async def provision(
        self, config: EnvironmentConfig, experiment_id: str
    ) -> ContainerInfo:
        """Provision a container for the target system."""
        image = config.docker_image or self._default_image(config.template)

        logger.info("pulling image", image=image)
        try:
            self.client.images.pull(image)
        except docker.errors.ImageNotFound:
            logger.warning("image not found, trying to pull", image=image)
            self.client.images.pull(image)

        container_name = f"llm-tuner-{experiment_id[:8]}-{config.template}"

        ports = config.port_mappings or self._default_ports(config.template)
        port_bindings = {f"{internal}/tcp": int(host) for internal, host in ports.items()}

        env_vars = self._default_env(config.template)

        logger.info("starting container", name=container_name, image=image)
        container = self.client.containers.run(
            image=image,
            name=container_name,
            detach=True,
            ports=port_bindings,
            environment=env_vars,
            mem_limit=config.memory_limit,
            nano_cpus=int(config.cpu_limit) * 1_000_000_000,
            remove=True,
        )

        self.active_container = container
        self._registry[container.id] = self
        await asyncio.sleep(2)  # Wait for container to initialize

        container.reload()
        info = ContainerInfo(
            container_id=container.id,
            name=container_name,
            image=image,
            ports=ports,
            status=container.status,
            env_vars=env_vars,
        )
        logger.info("container provisioned", container=info)
        return info

    async def apply_config(
        self, config_content: str, config_path: str, restart: bool = False
    ) -> bool:
        """Write config to container and optionally restart."""
        if not self.active_container:
            raise RuntimeError("No active container")

        container = self.active_container
        container_id = container.id

        # Write config via exec
        encoded = config_content.encode("utf-8")
        mkdir_exec = container.exec_run(
            f"mkdir -p {Path(config_path).parent.as_posix()}"
        )
        if mkdir_exec.exit_code != 0:
            logger.error("failed to create config directory",
                          output=mkdir_exec.output.decode())

        # Write config using a pipe
        container.put_archive(
            str(Path(config_path).parent),
            self._make_tar(Path(config_path).name, encoded),
        )

        self._config_history.append(config_content)
        logger.info("config applied", path=config_path)

        if restart:
            logger.info("restarting container", id=container_id)
            container.restart()
            await asyncio.sleep(3)  # Wait for restart

        return True

    async def health_check(self, timeout: int = 30) -> bool:
        """Check if the target system is healthy."""
        _ = timeout
        if not self.active_container:
            return False

        container = self.active_container
        container.reload()
        return container.status == "running"

    async def get_config(self, config_path: str) -> str | None:
        """Read current config from container."""
        if not self.active_container:
            return None

        exec_result = self.active_container.exec_run(f"cat {config_path}")
        if exec_result.exit_code != 0:
            return None
        return exec_result.output.decode("utf-8")

    async def teardown(self) -> None:
        """Stop and remove the container."""
        if self.active_container:
            container_id = self.active_container.id
            try:
                self.active_container.stop(timeout=10)
            except Exception as e:
                logger.warning("error stopping container", error=str(e))
            self.active_container = None
            self._registry.pop(container_id, None)
            logger.info("container torn down")

    def rollback_config(self) -> str | None:
        """Rollback to previous config. Returns the rolled-back config or None."""
        if len(self._config_history) < 2:
            return None
        self._config_history.pop()  # Remove current (failed) config
        return self._config_history[-1]

    def _default_image(self, template: str) -> str:
        mapping = {
            "redis-standalone": settings.docker_default_image_redis,
            "redis-cluster": settings.docker_default_image_redis,
            "mysql-standalone": settings.docker_default_image_mysql,
            "mysql-replication": settings.docker_default_image_mysql,
        }
        return mapping.get(template, "redis:7.2-alpine")

    def _default_ports(self, template: str) -> dict[str, str]:
        mapping = {
            "redis-standalone": {"6379": "6379"},
            "redis-cluster": {"7000": "7000", "7001": "7001"},
            "mysql-standalone": {"3306": "3306"},
            "mysql-replication": {"3306": "3306", "3307": "3307"},
        }
        return mapping.get(template, {"6379": "6379"})

    def _default_env(self, template: str) -> dict[str, str]:
        if "mysql" in template:
            return {
                "MYSQL_ROOT_PASSWORD": "llmtuner123",
                "MYSQL_DATABASE": "benchmark",
            }
        return {}

    @staticmethod
    def _make_tar(name: str, data: bytes) -> bytes:
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return buf.read()
