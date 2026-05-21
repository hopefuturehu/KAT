"""Helpers for building compact, cache-friendly LLM payloads."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


def compact_json(data: Any) -> str:
    """Serialize *data* with stable ordering and minimal whitespace."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def truncate_text(text: str, max_chars: int = 240) -> str:
    """Trim long free-form text while keeping the prefix stable."""
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def limit_list(items: Iterable[Any], max_items: int) -> list[Any]:
    result = list(items)
    return result[:max_items]


def limit_mapping(mapping: Mapping[str, Any], max_items: int) -> dict[str, Any]:
    return {
        key: mapping[key]
        for key in sorted(mapping.keys())[:max_items]
    }


def build_json_message(instruction: str, payload: dict[str, Any]) -> str:
    """Combine a short instruction with a compact JSON payload."""
    return f"{instruction}\n\nINPUT_JSON:\n{compact_json(payload)}"


def split_payload(payload: dict[str, Any], stable_keys: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split *payload* into (stable, variable) dicts for prompt caching.

    Items whose keys are in *stable_keys* go into the first dict (intended
    as a cacheable prefix).  Everything else goes into the second dict.
    """
    stable = {k: payload[k] for k in stable_keys if k in payload}
    variable = {k: v for k, v in payload.items() if k not in stable_keys}
    return stable, variable
