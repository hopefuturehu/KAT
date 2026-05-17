"""Analyzer Agent — interprets benchmark results and identifies bottlenecks."""

from pathlib import Path

from src.agents.base import BaseAgent
from src.agents.prompt_payload import build_json_message, limit_list, limit_mapping
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
        current_metrics = benchmark_results.get("aggregate", {})
        best_metrics = state.get("best_metrics", {})
        trend_data = state.get("trend_data", [])
        summarized_trend = [
            {
                "trial": item.get("trial"),
                "improvement_pct": item.get("improvement_pct", 0.0),
                "metrics": limit_mapping(item.get("metrics", {}), 6),
            }
            for item in limit_list(trend_data, state.get("convergence_window", 5))
        ]
        delta_vs_best = {
            key: current_metrics[key] - best_metrics[key]
            for key in sorted(current_metrics.keys() & best_metrics.keys())
            if isinstance(current_metrics.get(key), (int, float))
            and isinstance(best_metrics.get(key), (int, float))
        }

        payload = {
            "target": {
                "system": state.get("target_system", "unknown"),
                "version": state.get("target_version", ""),
            },
            "trial_number": state.get("trial_number", 0),
            "goals": state.get("goals", []),
            "current_metrics": limit_mapping(current_metrics, 12),
            "best_metrics": limit_mapping(best_metrics, 12),
            "delta_vs_best": limit_mapping(delta_vs_best, 12),
            "parameter_changes": limit_list(parameter_changes, 6),
            "trend_summary": summarized_trend,
            "convergence_window": state.get("convergence_window", 5),
        }

        user_message = build_json_message(
            "Analyze the summarized benchmark results and provide your assessment.",
            payload,
        )
        response = await self.invoke(user_message, context={})

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
