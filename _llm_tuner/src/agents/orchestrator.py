"""Orchestrator Agent — central workflow controller."""

import json
from pathlib import Path
from src.agents.base import BaseAgent
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class OrchestratorAgent(BaseAgent):
    agent_name = "orchestrator"

    def __init__(self, model: str | None = None):
        super().__init__(model)
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "orchestrator.j2"
        self.system_prompt_template = prompt_path.read_text()

    async def decide_next_action(self, state: dict) -> dict:
        """Given experiment state, decide the next action."""
        context = {
            "target_system": state.get("target_system", "unknown"),
            "target_version": state.get("target_version", ""),
            "experiment_name": state.get("experiment_name", "unnamed"),
            "trial_number": state.get("trial_number", 0),
            "max_trials": state.get("max_trials", 30),
            "elapsed_hours": state.get("elapsed_hours", 0),
            "max_duration_hours": state.get("max_duration_hours", 8.0),
            "goals_text": json.dumps(state.get("goals", []), indent=2),
            "best_metrics_text": json.dumps(state.get("best_metrics", {}), indent=2),
            "last_trial_summary": state.get("last_trial_summary", "No previous trial"),
            "convergence_window": state.get("convergence_window", 5),
        }

        user_message = "Analyze the current experiment state and decide the next workflow action."
        response = await self.invoke(user_message, context)

        return safe_extract_json(
            response,
            default={"action": "CONTINUE_TUNING", "reasoning": "Default: continue tuning"},
        )
