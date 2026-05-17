"""Tuner Agent — proposes parameter changes based on analysis and knowledge base."""

from pathlib import Path

from src.agents.base import BaseAgent
from src.agents.prompt_payload import build_json_message, limit_list, truncate_text
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

        # Query knowledge base for context.
        kb_context: list[dict[str, str]] = []
        if self.kb:
            target = state.get("target_system", "")
            query = f"{analysis.get('recommended_focus', '')} {analysis.get('likely_bottleneck', '')} tuning"
            entries = await self.kb.query(query, system=target, n_results=5)
            kb_context = [
                {
                    "category": str(e.category),
                    "title": str(e.title),
                    "excerpt": truncate_text(str(e.content), max_chars=220),
                }
                for e in entries
            ]

        relevant_names = {
            str(change.get("parameter", ""))
            for change in recent_changes
        }
        focus_terms = " ".join(
            str(analysis.get(key, ""))
            for key in ("recommended_focus", "likely_bottleneck")
        ).lower()
        selected_params = _select_tunable_params(
            tunable_params,
            focus_terms=focus_terms,
            recent_param_names=relevant_names,
            max_items=24,
        )

        selected_keys = {str(item.get("name", "")) for item in selected_params}
        current_subset = {
            key: current_config[key]
            for key in sorted(current_config)
            if key in selected_keys
        }
        changed_from_baseline = _summarize_config_delta(current_config, baseline_config or {}, max_items=20)

        payload = {
            "target": {
                "system": state.get("target_system", "unknown"),
                "version": state.get("target_version", ""),
            },
            "goals": state.get("goals", []),
            "constraints": {
                "max_changes_per_trial": state.get("max_changes_per_trial", 4),
                "max_restart_changes": state.get("max_restart_changes", 2),
                "blocklist": sorted(state.get("blocklist", [])),
            },
            "analysis": analysis,
            "current_config_subset": current_subset,
            "config_changes_from_baseline": changed_from_baseline,
            "candidate_parameters": selected_params,
            "recent_changes": limit_list(recent_changes, 6),
            "knowledge_base_context": kb_context,
        }

        user_message = build_json_message(
            "Propose parameter changes to improve performance toward the stated goals. "
            "Use only the candidate_parameters list when selecting changes.",
            payload,
        )
        response = await self.invoke(user_message, context={})

        return safe_extract_json(
            response,
            default={"changes": [], "overall_strategy": "Default: no changes proposed"},
        )


def _select_tunable_params(
    tunable_params: list[dict],
    *,
    focus_terms: str,
    recent_param_names: set[str],
    max_items: int,
) -> list[dict]:
    scored: list[tuple[int, dict]] = []
    lowered_focus = focus_terms.lower()

    for param in tunable_params:
        name = str(param.get("name", ""))
        haystack = " ".join(
            [
                name,
                str(param.get("category", "")),
                str(param.get("description", "")),
                str(param.get("notes", "")),
            ]
        ).lower()
        score = 0
        if name in recent_param_names:
            score += 100
        if lowered_focus and any(term and term in haystack for term in lowered_focus.split()):
            score += 50
        if param.get("restart_required") is False:
            score += 10
        risk = str(param.get("risk", "medium")).lower()
        if risk == "low":
            score += 5
        elif risk == "medium":
            score += 2
        scored.append((score, param))

    scored.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("name", "")),
        )
    )
    return [param for _, param in scored[:max_items]]


def _summarize_config_delta(
    current_config: dict[str, str],
    baseline_config: dict[str, str],
    *,
    max_items: int,
) -> list[dict[str, str]]:
    delta: list[dict[str, str]] = []
    keys = set(current_config) | set(baseline_config)
    for key in sorted(keys):
        old = baseline_config.get(key)
        new = current_config.get(key)
        if old == new:
            continue
        delta.append(
            {
                "parameter": key,
                "baseline": "" if old is None else str(old),
                "current": "" if new is None else str(new),
            }
        )
    return delta[:max_items]
