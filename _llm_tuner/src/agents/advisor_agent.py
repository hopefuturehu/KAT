"""Advisor Agent — recommends non-parameter solutions when goals cannot be met."""

import json
from pathlib import Path
from src.agents.base import BaseAgent
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class AdvisorAgent(BaseAgent):
    agent_name = "advisor"

    def __init__(self, model: str | None = None, kb_retriever=None):
        super().__init__(model)
        self.kb = kb_retriever
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "advisor.j2"
        self.system_prompt_template = prompt_path.read_text()

    async def recommend(
        self,
        state: dict,
        goals_with_progress: list[dict],
        current_config: dict[str, str],
        bottleneck: str,
        tuning_summary: list[dict],
    ) -> dict:
        """Generate recommendations when parameter tuning has converged or failed."""

        context = {
            "target_system": state.get("target_system", "unknown"),
            "goals_with_progress": json.dumps(goals_with_progress, indent=2),
            "best_metrics_text": json.dumps(state.get("best_metrics", {}), indent=2),
            "hardware_spec": json.dumps(state.get("hardware_spec", {}), indent=2),
            "current_config": json.dumps(current_config, indent=2),
            "tuning_summary": json.dumps(tuning_summary, indent=2),
            "bottleneck": bottleneck,
        }

        user_message = (
            "The parameter tuning experiment has converged or reached its limit. "
            "Please provide alternative recommendations to achieve the performance goals."
        )
        response = await self.invoke(user_message, context)

        return safe_extract_json(
            response,
            default={
                "summary": "Could not generate recommendations",
                "recommendations": [
                    {
                        "category": "Architecture",
                        "recommendation": "Consider adding a caching layer (Redis) in front of MySQL",
                        "expected_benefit": "Expected 50-90% read throughput improvement",
                        "effort": "medium",
                        "risk": "low",
                        "rationale": "Caching reduces database load for repetitive reads",
                    }
                ],
            },
        )
