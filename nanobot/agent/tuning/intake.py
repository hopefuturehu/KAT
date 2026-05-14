"""TuningIntakeAgent — multi-turn requirements gathering via AgentRunner."""

from __future__ import annotations

import json
import re
from typing import Any

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tuning.prompts import build_intake_prompt
from nanobot.agent.tuning.schema import TuningGoal, TuningRequirements
from nanobot.providers.base import LLMProvider


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from LLM output text."""
    # Try to find JSON block in markdown code fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON object containing target_system
    m = re.search(r"\{[^{}]*\"target_system\"[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _parse_requirements(data: dict[str, Any]) -> TuningRequirements:
    """Parse a dict into TuningRequirements, with defaults for missing fields."""
    goals = [
        TuningGoal(
            metric=g.get("metric", "qps"),
            operator=g.get("operator", ">="),
            value=float(g.get("value", 0)),
            weight=float(g.get("weight", 1.0)),
        )
        for g in data.get("goals", [])
    ]
    return TuningRequirements(
        target_system=data.get("target_system", ""),
        target_version=data.get("target_version", ""),
        goals=goals,
        host=data.get("host", ""),
        port=str(data.get("port", "")),
        password=data.get("password", ""),
        config_file=data.get("config_file", ""),
        # Lifecycle commands
        start_command=data.get("start_command", ""),
        run_command=data.get("run_command", ""),
        teardown_command=data.get("teardown_command", ""),
        health_check_command=data.get("health_check_command", ""),
        restart_command=data.get("restart_command", ""),
        # Output parsing
        output_format=data.get("output_format", "redis-benchmark-csv"),
        metric_regex=data.get("metric_regex", {}),
        # Benchmark profile
        benchmark_profile_path=data.get("benchmark_profile_path", ""),
        # Stability
        stable_mode=data.get("stable_mode", False),
        stable_warmup_requests=int(data.get("stable_warmup_requests", 10000)),
        stable_iterations=int(data.get("stable_iterations", 3)),
        # Constraints
        allow_restart=data.get("allow_restart", False),
        max_restart_changes=int(data.get("max_restart_changes", 2)),
        max_risk_level=data.get("max_risk_level", "medium"),
        blocked_parameters=data.get("blocked_parameters", []),
        max_trials=int(data.get("max_trials", 30)),
        max_duration_hours=float(data.get("max_duration_hours", 8.0)),
        dry_run=data.get("dry_run", False),
    )


def _requirements_complete(req: TuningRequirements) -> bool:
    """Check if we have enough info to proceed."""
    if not req.target_system or not req.goals:
        return False
    if req.target_system not in ("redis", "mysql"):
        return False
    # Tuning always needs a live target plus a writable config file.
    if not req.host or not req.config_file:
        return False
    # The benchmark can come from an inline command or a reusable YAML profile.
    if not req.run_command and not req.benchmark_profile_path:
        return False
    return True


async def run_intake_turn(
    runner: AgentRunner,
    provider: LLMProvider,
    model: str,
    workspace: str,
    conversation: list[dict[str, Any]],
    max_tool_result_chars: int = 16000,
) -> tuple[str | None, list[dict[str, Any]], TuningRequirements | None]:
    """Run one turn of the intake conversation.

    Returns (response_text, updated_conversation, requirements_if_complete).
    """
    tools = ToolRegistry()  # Intake agent has no tools — conversation only
    system_prompt = build_intake_prompt(workspace)

    messages = [{"role": "system", "content": system_prompt}] + conversation

    result = await runner.run(
        AgentRunSpec(
            initial_messages=messages,
            tools=tools,
            model=model,
            max_iterations=1,  # No tool loops in intake
            max_tool_result_chars=max_tool_result_chars,
            fail_on_tool_error=False,
        )
    )

    response_text = result.final_content or ""
    if not response_text.strip():
        return None, conversation, None

    # Append assistant response to conversation
    updated = list(conversation)
    updated.append({"role": "assistant", "content": response_text})

    # Try to extract structured requirements
    data = _extract_json(response_text)
    if data:
        req = _parse_requirements(data)
        if _requirements_complete(req):
            return response_text, updated, req

    return response_text, updated, None
