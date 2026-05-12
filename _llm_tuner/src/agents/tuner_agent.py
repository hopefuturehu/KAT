"""Tuner Agent — proposes parameter changes based on analysis and knowledge base."""

import json
from pathlib import Path
from src.agents.base import BaseAgent
from src.utils.llm_resilience import safe_extract_json
from src.utils.logging import get_logger

logger = get_logger(__name__)


class TunerAgent(BaseAgent):
    agent_name = "tuner"

    def __init__(self, model: str | None = None, kb_retriever=None):
        super().__init__(model)
        self.kb = kb_retriever
        self._load_prompt()

    def _load_prompt(self) -> None:
        prompt_path = Path(__file__).parent / "prompts" / "tuner.j2"
        self.system_prompt_template = prompt_path.read_text()

    async def propose_changes(
        self,
        state: dict,
        analysis: dict,
        current_config: dict[str, str],
        baseline_config: dict[str, str] | None,
        tunable_params: list[dict],
        recent_changes: list[dict],
    ) -> dict:
        """Propose parameter changes based on current state and analysis."""

        # Query knowledge base for context
        kb_context = ""
        if self.kb:
            target = state.get("target_system", "")
            query = f"{analysis.get('recommended_focus', '')} {analysis.get('likely_bottleneck', '')} tuning"
            entries = await self.kb.query(query, system=target, n_results=5)
            kb_context = "\n\n".join(
                f"[{e.category}] {e.title}: {e.content}" for e in entries
            )

        context = {
            "target_system": state.get("target_system", "unknown"),
            "target_version": state.get("target_version", ""),
            "goals_text": json.dumps(state.get("goals", []), indent=2),
            "current_config": json.dumps(current_config, indent=2),
            "baseline_config": json.dumps(baseline_config or {}, indent=2),
            "analysis_result": json.dumps(analysis, indent=2),
            "tunable_parameters": json.dumps(tunable_params, indent=2),
            "kb_context": kb_context,
            "max_changes_per_trial": state.get("max_changes_per_trial", 4),
            "max_restart_changes": state.get("max_restart_changes", 2),
            "blocklist": json.dumps(state.get("blocklist", [])),
            "recent_changes": json.dumps(recent_changes, indent=2),
        }

        user_message = "Propose parameter changes to improve performance toward the stated goals."
        response = await self.invoke(user_message, context)

        return safe_extract_json(
            response,
            default={"changes": [], "overall_strategy": "Default: no changes proposed"},
        )
