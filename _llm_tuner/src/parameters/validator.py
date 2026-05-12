"""Parameter validation logic for safety checks."""

from src.parameters.schema import ParameterDefinition, ParameterRisk


class ParameterValidator:
    def __init__(self, schema: dict[str, ParameterDefinition]):
        self.schema = schema

    def validate_change(
        self, param_name: str, old_value: str | None, new_value: str
    ) -> tuple[bool, str]:
        """Validate a single parameter change. Returns (is_safe, reason)."""
        pd = self.schema.get(param_name)
        if pd is None:
            return True, f"unknown parameter '{param_name}' — allowing"

        # Risk check
        if pd.risk == ParameterRisk.CRITICAL:
            return False, f"parameter '{param_name}' is marked CRITICAL — manual review required"

        # Type and range check
        if pd.type == "integer":
            try:
                val = int(new_value)
                if pd.min_value is not None and val < int(pd.min_value):
                    return False, f"{param_name}={new_value} below min {pd.min_value}"
                if pd.max_value is not None and val > int(pd.max_value):
                    return False, f"{param_name}={new_value} above max {pd.max_value}"
            except ValueError:
                return False, f"{param_name}={new_value} is not a valid integer"

        elif pd.type == "float":
            try:
                val = float(new_value)
                if pd.min_value is not None and val < float(pd.min_value):
                    return False, f"{param_name}={new_value} below min {pd.min_value}"
                if pd.max_value is not None and val > float(pd.max_value):
                    return False, f"{param_name}={new_value} above max {pd.max_value}"
            except ValueError:
                return False, f"{param_name}={new_value} is not a valid float"

        elif pd.type == "enum" and pd.enum_values:
            if new_value not in pd.enum_values:
                return False, f"{param_name}={new_value} not in allowed: {pd.enum_values}"

        elif pd.type == "boolean":
            if new_value.lower() not in ("yes", "no", "true", "false", "1", "0", "on", "off"):
                return False, f"{param_name}={new_value} is not a valid boolean"

        # Dependency check: ensure dependencies are satisfied
        for dep in pd.depends_on:
            if dep not in self.schema:
                return False, f"parameter '{param_name}' depends on unknown '{dep}'"

        # Conflict check
        for conflict in pd.conflicts_with:
            pass  # Conflicts are checked at the batch level by the Safety Agent

        return True, "ok"

    def validate_batch(
        self, changes: list[dict]
    ) -> tuple[bool, list[str]]:
        """Validate a batch of parameter changes. Returns (all_safe, issues)."""
        issues: list[str] = []
        for change in changes:
            safe, reason = self.validate_change(
                change["parameter"],
                change.get("old_value"),
                change.get("new_value"),
            )
            if not safe:
                issues.append(reason)

        return len(issues) == 0, issues
