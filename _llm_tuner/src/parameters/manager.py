"""Parameter manager: parse, validate, diff, snapshot, and rollback configs."""

import copy
import json
from pathlib import Path
from typing import Any
from src.utils.logging import get_logger
from src.parameters.schema import ParameterDefinition, ParameterCategory, ParameterRisk
from src.parameters.parser import ConfigParser

logger = get_logger(__name__)


class ParameterManager:
    def __init__(self, target_system: str):
        self.target_system = target_system
        self.parser = ConfigParser.for_system(target_system)
        self._schema: dict[str, ParameterDefinition] = {}
        self._history: list[dict[str, str]] = []
        self._load_schema()

    def _load_schema(self) -> None:
        schemas_dir = Path(__file__).parent / "schemas"
        schema_path = schemas_dir / f"{self.target_system}.json"

        # Fall back to versioned schema files (e.g., redis_7.2.json)
        if not schema_path.exists():
            candidates = sorted(schemas_dir.glob(f"{self.target_system}_*.json"))
            if candidates:
                schema_path = candidates[-1]  # Use latest version

        if schema_path.exists():
            raw = json.loads(schema_path.read_text())
            for item in raw.get("parameters", []):
                pd = ParameterDefinition(**item)
                self._schema[pd.name] = pd
        logger.info(
            "parameter schema loaded",
            system=self.target_system,
            count=len(self._schema),
        )

    def parse_and_validate(self, config_text: str) -> dict[str, str]:
        """Parse config text and validate against schema."""
        parsed = self.parser.parse(config_text)
        self._validate(parsed)
        self._history.append(copy.deepcopy(parsed))
        return parsed

    def _validate(self, config: dict[str, str]) -> list[str]:
        """Validate config values against known schema. Returns list of issues."""
        issues: list[str] = []
        for name, value in config.items():
            if name not in self._schema:
                continue
            pd = self._schema[name]
            if pd.type == "integer":
                try:
                    int_val = int(value)
                    if pd.min_value is not None and int_val < int(pd.min_value):
                        issues.append(f"{name}={value} below min {pd.min_value}")
                    if pd.max_value is not None and int_val > int(pd.max_value):
                        issues.append(f"{name}={value} above max {pd.max_value}")
                except ValueError:
                    issues.append(f"{name}={value} is not a valid integer")
            elif pd.type == "float":
                try:
                    float_val = float(value)
                    if pd.min_value is not None and float_val < float(pd.min_value):
                        issues.append(f"{name}={value} below min {pd.min_value}")
                    if pd.max_value is not None and float_val > float(pd.max_value):
                        issues.append(f"{name}={value} above max {pd.max_value}")
                except ValueError:
                    issues.append(f"{name}={value} is not a valid float")
            elif pd.type == "enum" and pd.enum_values:
                if value not in pd.enum_values:
                    issues.append(f"{name}={value} not in allowed: {pd.enum_values}")

        if issues:
            logger.warning("validation issues", issues=issues)
        return issues

    def diff(self, old: dict[str, str], new: dict[str, str]) -> list[dict[str, Any]]:
        """Compute diff between two configs."""
        changes = []
        for key in set(old.keys()) | set(new.keys()):
            old_val = old.get(key, "<unset>")
            new_val = new.get(key, "<unset>")
            if old_val != new_val:
                changes.append({
                    "parameter": key,
                    "old_value": old.get(key),
                    "new_value": new.get(key),
                    "restart_required": self._schema.get(key, ParameterDefinition(
                        name=key, category=ParameterCategory.GENERAL,
                        description="", default_value="",
                    )).restart_required,
                })
        return changes

    def snapshot(self, config: dict[str, str]) -> str:
        """Create a snapshot of current config as JSON string."""
        return json.dumps(config, indent=2)

    def rollback(self) -> dict[str, str] | None:
        """Return the previous config from history."""
        if len(self._history) < 2:
            return None
        self._history.pop()
        return self._history[-1]

    def get_parameter_info(self, name: str) -> ParameterDefinition | None:
        return self._schema.get(name)

    def get_tunable_parameters(
        self,
        include_categories: list[str] | None = None,
        exclude_parameters: list[str] | None = None,
        max_risk: ParameterRisk = ParameterRisk.HIGH,
    ) -> list[ParameterDefinition]:
        """Get parameters eligible for tuning with given constraints."""
        exclude = set(exclude_parameters or [])
        risk_order = {ParameterRisk.LOW: 0, ParameterRisk.MEDIUM: 1,
                      ParameterRisk.HIGH: 2, ParameterRisk.CRITICAL: 3}

        result = []
        for pd in self._schema.values():
            if pd.name in exclude:
                continue
            if include_categories and pd.category.value not in include_categories:
                continue
            if risk_order[pd.risk] > risk_order[max_risk]:
                continue
            result.append(pd)
        return result

    def serialize_config(self, config: dict[str, str]) -> str:
        """Serialize config dict back to native format text."""
        return self.parser.serialize(config)
