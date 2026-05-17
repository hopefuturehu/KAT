"""Safety Agent — validates parameter changes before they are applied."""

from pathlib import Path

from src.agents.base import BaseAgent
from src.agents.prompt_payload import build_json_message, limit_list
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SafetyAgent(BaseAgent):
    agent_name = "safety"

    def __init__(self, model: str | None = None):
        super().__init__(model)
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "safety.j2"
        self.system_prompt_template = prompt_path.read_text()

    async def validate(
        self,
        state: dict,
        proposed_changes: list[dict],
        current_config: dict[str, str],
        parameter_metadata: list[dict],
        rollback_history: list[dict],
    ) -> dict:
        """Validate proposed parameter changes for safety."""

        # Count restart-requiring changes
        restart_count = sum(
            1 for c in proposed_changes
            if any(m.get("name") == c.get("parameter") and m.get("restart_required")
                   for m in parameter_metadata)
        )

        payload = {
            "target": {
                "system": state.get("target_system", "unknown"),
                "version": state.get("target_version", ""),
            },
            "safety_constraints": {
                "memory_headroom_pct": state.get("memory_headroom_pct", 20),
                "max_restart_changes": state.get("max_restart_changes", 2),
                "stability_window": state.get("stability_window", 3),
                "max_consecutive_rollbacks": state.get("max_consecutive_rollbacks", 3),
            },
            "restart_count": restart_count,
            "proposed_changes": proposed_changes,
            "relevant_live_config": current_config,
            "relevant_parameter_metadata": parameter_metadata,
            "recent_rollbacks": limit_list(rollback_history, 3),
        }

        user_message = build_json_message(
            f"Review these {len(proposed_changes)} proposed changes "
            f"({restart_count} require restart) and determine whether they are safe to apply.",
            payload,
        )
        response = await self.invoke(user_message, context={})

        # Fail-safe: reject if we can't parse
        return safe_extract_json(
            response,
            default={
                "verdict": "REJECT",
                "overall_risk_level": "high",
                "warnings": ["Could not validate changes properly"],
                "requires_human_approval": True,
            },
        )
