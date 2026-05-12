"""Analyzer Agent — interprets benchmark results and identifies bottlenecks."""

import json
from pathlib import Path
from src.agents.base import BaseAgent
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class AnalyzerAgent(BaseAgent):
    agent_name = "analyzer"

    def __init__(self, model: str | None = None):
        super().__init__(model)
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "analyzer.j2"
        self.system_prompt_template = prompt_path.read_text()

    async def analyze(
        self, state: dict, benchmark_results: dict, parameter_changes: list[dict]
    ) -> dict:
        """Analyze benchmark results and produce insights."""
        context = {
            "target_system": state.get("target_system", "unknown"),
            "target_version": state.get("target_version", ""),
            "goals_text": json.dumps(state.get("goals", []), indent=2),
            "trial_number": state.get("trial_number", 0),
            "benchmark_results": json.dumps(benchmark_results, indent=2),
            "best_metrics_text": json.dumps(state.get("best_metrics", {}), indent=2),
            "parameter_changes": json.dumps(parameter_changes, indent=2),
            "trend_data": json.dumps(state.get("trend_data", []), indent=2),
            "convergence_window": state.get("convergence_window", 5),
        }

        user_message = "Analyze the benchmark results and provide your assessment."
        response = await self.invoke(user_message, context)

        return safe_extract_json(
            response,
            default={
                "trend": "stable",
                "improvement_pct": 0.0,
                "likely_bottleneck": "unknown",
                "change_impact": "neutral",
                "insights": "Could not parse analysis",
                "recommended_focus": "general tuning",
            },
        )
