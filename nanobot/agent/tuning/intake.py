"""TuningIntakeAgent — multi-turn requirements gathering via AgentRunner."""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tuning.prompts import build_intake_prompt
from nanobot.agent.tuning.schema import TuningRequirements
from nanobot.providers.base import LLMProvider


def _find_json_objects(text: str) -> list[str]:
    """Find all balanced JSON object strings in *text* using brace counting.

    Returns candidates sorted by length (largest first).
    """
    candidates: list[str] = []
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    candidates.sort(key=len, reverse=True)
    return candidates


def _parse_json_robust(json_str: str) -> tuple[dict[str, Any] | None, str]:
    """Try hard to parse *json_str* into a dict.

    Returns ``(parsed_dict, error_message)``.  If successful, *error_message*
    is empty; otherwise *parsed_dict* is ``None``.
    """
    if not json_str.strip():
        return None, "empty input"

    # 1. Direct parse
    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result, ""
    except json.JSONDecodeError as exc:
        pass

    # 2. Repair common LLM mistakes via json_repair (already a dependency)
    try:
        from json_repair import repair_json

        repaired = repair_json(json_str)
        result = json.loads(repaired)
        if isinstance(result, dict):
            return result, ""
    except Exception:
        pass

    # 3. Manual repair: remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", json_str)
    if cleaned != json_str:
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result, ""
        except json.JSONDecodeError:
            pass

    return None, "could not parse JSON after brace counting, json_repair, and trailing-comma fix"


def _extract_json(text: str) -> tuple[dict[str, Any] | None, str]:
    """Robust JSON extraction from LLM output text.

    Returns ``(parsed_dict, error_message)``.
    """
    candidates = _find_json_objects(text)
    for candidate in candidates:
        data, err = _parse_json_robust(candidate)
        if data is not None:
            return data, ""
    if not candidates:
        return None, "no JSON object (balanced braces) found in response"
    return None, f"found {len(candidates)} JSON candidate(s) but none parsed: {_parse_json_robust(candidates[0])[1]}"


def _parse_requirements(data: dict[str, Any]) -> TuningRequirements:
    """Parse a dict into TuningRequirements, with defaults for missing fields."""
    return TuningRequirements.from_dict(data)


def _requirements_complete(req: TuningRequirements) -> bool:
    """Check if we have enough info to proceed."""
    if not req.target_system or not req.goals:
        return False
    if req.target_system not in ("redis", "mysql"):
        return False
    # Tuning always needs a live target plus a writable config file.
    if not req.host or not req.port or not req.config_file:
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
    _ = provider
    tools = ToolRegistry()  # Intake agent has no tools — conversation only
    system_prompt = build_intake_prompt(workspace)

    messages = [{"role": "system", "content": system_prompt}] + conversation

    result = await runner.run(
        AgentRunSpec(
            initial_messages=messages,
            tools=tools,
            model=model,
            max_iterations=1,
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
    data, err_msg = _extract_json(response_text)
    if data is not None:
        req = _parse_requirements(data)
        if _requirements_complete(req):
            return response_text, updated, req
        # Incomplete — let intake conversation continue
        return response_text, updated, None

    # ── LLM self-repair: ask the model to fix its JSON ──────────────────
    logger.warning("intake JSON extraction failed: {}", err_msg[:120])
    repair_messages = [
        {"role": "system", "content": system_prompt},
        *updated,
        {
            "role": "user",
            "content": (
                "Your last response did not contain valid JSON. "
                "Please output ONLY the JSON object with the collected tuning requirements. "
                "Wrap it in ```json code fences."
            ),
        },
    ]
    try:
        repair_result = await runner.run(
            AgentRunSpec(
                initial_messages=repair_messages,
                tools=tools,
                model=model,
                max_iterations=1,
                max_tool_result_chars=max_tool_result_chars,
                fail_on_tool_error=False,
            )
        )
    except Exception:
        logger.exception("intake JSON repair call failed")
        return response_text, updated, None

    repaired_text = repair_result.final_content or ""
    if not repaired_text.strip():
        return response_text, updated, None

    data, err_msg2 = _extract_json(repaired_text)
    if data is not None:
        req = _parse_requirements(data)
        updated.append({"role": "user", "content": "Please output the JSON summary."})
        updated.append({"role": "assistant", "content": repaired_text})
        if _requirements_complete(req):
            return repaired_text, updated, req
        return repaired_text, updated, None

    logger.warning("intake JSON repair also failed: {}", err_msg2[:120])
    return response_text, updated, None
