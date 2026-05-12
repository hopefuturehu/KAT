"""Benchmark Agent — runs performance benchmarks."""

import json
from pathlib import Path
from src.agents.base import BaseAgent, AgentTool
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class BenchmarkAgent(BaseAgent):
    agent_name = "benchmark"

    def __init__(self, model: str | None = None):
        super().__init__(model)
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "benchmark.j2"
        self.system_prompt_template = prompt_path.read_text()

    def set_benchmark_context(self, target_system: str, runner_type: str, profile: dict) -> None:
        self._target_system = target_system
        self._runner_type = runner_type
        self._profile = profile

    async def plan_benchmark(self, state: dict) -> dict:
        """Plan which benchmark profiles to run given the experiment state."""
        context = {
            "target_system": state.get("target_system", "unknown"),
            "runner_type": self._runner_type,
            "profile_name": self._profile.get("name", "default"),
            "profile_details": json.dumps(self._profile, indent=2),
            "host": "127.0.0.1",
            "port": "6379" if state.get("target_system") == "redis" else "3306",
        }

        user_message = "Prepare the benchmark run. Determine any additional parameters needed."
        response = await self.invoke(user_message, context)

        return safe_extract_json(
            response,
            default={"action": "run_default_profile", "profile": self._profile.get("name")},
        )
