"""Config file parser for redis.conf and my.cnf formats."""

from abc import ABC, abstractmethod


class ConfigParser(ABC):
    @abstractmethod
    def parse(self, text: str) -> dict[str, str]:
        """Parse config text into a flat key-value dict."""

    @abstractmethod
    def serialize(self, config: dict[str, str]) -> str:
        """Serialize config dict back to native format text."""

    @staticmethod
    def for_system(target_system: str) -> "ConfigParser":
        if target_system == "redis":
            return RedisConfigParser()
        elif target_system == "mysql":
            return MySQLConfigParser()
        else:
            raise ValueError(f"Unsupported system: {target_system}")


class RedisConfigParser(ConfigParser):
    def parse(self, text: str) -> dict[str, str]:
        config: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) >= 1:
                key = parts[0].lower()
                value = parts[1] if len(parts) > 1 else ""
                # Handle duplicate keys (e.g. multiple "save" lines)
                if key in config:
                    config[key] = config[key] + "\n" + value
                else:
                    config[key] = value
        return config

    def serialize(self, config: dict[str, str]) -> str:
        lines = []
        for key, value in config.items():
            if "\n" in value:
                for sub_value in value.split("\n"):
                    lines.append(f"{key} {sub_value}")
            else:
                lines.append(f"{key} {value}" if value else str(key))
        return "\n".join(lines) + "\n"


class MySQLConfigParser(ConfigParser):
    def parse(self, text: str) -> dict[str, str]:
        config: dict[str, str] = {}
        current_section: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip().lower().replace("-", "_")
                value = value.strip()
                full_key = f"{current_section}.{key}" if current_section else key
                config[full_key] = value
        return config

    def serialize(self, config: dict[str, str]) -> str:
        sections: dict[str, dict[str, str]] = {}
        for key, value in config.items():
            if "." in key:
                section, param = key.split(".", 1)
            else:
                section, param = "mysqld", key
            sections.setdefault(section, {})[param] = value

        lines = []
        for section, params in sections.items():
            lines.append(f"[{section}]")
            for param, value in params.items():
                lines.append(f"{param} = {value}")
            lines.append("")
        return "\n".join(lines)
