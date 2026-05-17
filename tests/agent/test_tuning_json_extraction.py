"""Test robust JSON extraction from LLM-style responses."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.agent.tuning.intake import _extract_json, _find_json_objects, _parse_json_robust


def _has_system(data: dict | None) -> bool:
    return data is not None and isinstance(data, dict) and "target_system" in data


# ── _find_json_objects ───────────────────────────────────────────────────


def test_find_simple_json_object() -> None:
    candidates = _find_json_objects('some text {"a": 1, "b": 2} more text')
    assert len(candidates) >= 1
    assert '{"a": 1, "b": 2}' in candidates


def test_find_json_in_code_fence() -> None:
    text = """Here are the requirements:
```json
{
  "target_system": "redis",
  "target_version": "7.2",
  "host": "10.0.0.1",
  "port": "6379",
  "config_file": "/etc/redis.conf",
  "goals": [{"metric": "qps", "operator": ">=", "value": 80000, "weight": 1.0}]
}
```
Let me know if this looks right."""
    candidates = _find_json_objects(text)
    assert len(candidates) >= 1
    # The largest JSON object should be first
    assert "target_system" in candidates[0]


def test_find_multiple_json_objects() -> None:
    text = '{"a": 1} and {"b": 2, "c": 3, "d": 4}'
    candidates = _find_json_objects(text)
    assert len(candidates) == 2
    # Largest should be first
    assert "d" in candidates[0]
    assert "a" in candidates[1]


def test_no_braces_returns_empty() -> None:
    assert _find_json_objects("no json here") == []
    assert _find_json_objects("") == []


# ── _parse_json_robust ───────────────────────────────────────────────────


def test_parse_valid_json() -> None:
    data, err = _parse_json_robust('{"target_system": "redis", "host": "127.0.0.1"}')
    assert data is not None
    assert data["target_system"] == "redis"
    assert err == ""


def test_parse_trailing_comma() -> None:
    data, err = _parse_json_robust('{"target_system": "redis",}')
    assert data is not None
    assert data["target_system"] == "redis"
    assert err == ""


def test_parse_trailing_comma_in_nested() -> None:
    data, err = _parse_json_robust(
        '{"target_system": "redis", "goals": [{"metric": "qps", "value": 80000,}],}'
    )
    assert data is not None
    assert data["target_system"] == "redis"
    assert err == ""


def test_parse_single_quotes_repaired() -> None:
    # json_repair handles single quotes
    data, err = _parse_json_robust("{'target_system': 'redis'}")
    assert data is not None
    assert data["target_system"] == "redis"


def test_parse_llm_comment_in_json() -> None:
    # json_repair handles // comments and trailing commas
    data, err = _parse_json_robust("""{
        "target_system": "redis",
        "host": "127.0.0.1",
        // comment about port
        "port": "6379",
    }""")
    assert data is not None
    assert data["host"] == "127.0.0.1"


def test_parse_empty_returns_error() -> None:
    data, err = _parse_json_robust("")
    assert data is None
    assert "empty" in err.lower()


def test_parse_non_json_returns_error() -> None:
    data, err = _parse_json_robust("not json at all")
    assert data is None
    assert err != ""


# ── _extract_json ────────────────────────────────────────────────────────


def test_extract_from_code_fence() -> None:
    text = """Here are the requirements:
```json
{
  "target_system": "redis",
  "target_version": "7.2",
  "goals": [{"metric": "qps", "operator": ">=", "value": 80000, "weight": 1.0}],
  "host": "127.0.0.1",
  "port": "6379",
  "config_file": "/etc/redis.conf",
  "run_command": "redis-benchmark -h {host} -p {port} --csv"
}
```"""
    data, err = _extract_json(text)
    assert _has_system(data)
    assert data["target_system"] == "redis"


def test_extract_bare_json() -> None:
    text = """OK here's the summary:

{
  "target_system": "redis",
  "host": "127.0.0.1",
  "config_file": "/etc/redis.conf",
  "goals": [{"metric": "qps", "operator": ">=", "value": 80000, "weight": 1.0}]
}

Let me know if you want changes."""
    data, err = _extract_json(text)
    assert _has_system(data)
    assert data["host"] == "127.0.0.1"


def test_extract_trailing_comma_json() -> None:
    text = """```json
{
  "target_system": "redis",
  "host": "127.0.0.1",
  "port": "6379",
  "config_file": "/etc/redis.conf",
  "goals": [{"metric": "qps", "operator": ">=", "value": 80000, "weight": 1.0}],
}
```"""
    data, err = _extract_json(text)
    assert _has_system(data)
    assert data["target_system"] == "redis"


def test_extract_with_garbled_text() -> None:
    text = """I've collected the requirements. Here's a quick summary first...

The target system is Redis 7.2 running on 127.0.0.1:6379. The config file is at /etc/redis.conf.

```json
{
  "target_system": "redis",
  "target_version": "7.2",
  "goals": [{"metric": "qps", "operator": ">=", "value": 80000, "weight": 1.0}],
  "host": "127.0.0.1",
  "port": "6379",
  "config_file": "/etc/redis.conf",
  "run_command": "redis-benchmark -h {host} -p {port} --csv"
}
```

All set!"""
    data, err = _extract_json(text)
    assert _has_system(data)


def test_extract_no_json_returns_error() -> None:
    data, err = _extract_json("just some text, no JSON here")
    assert data is None
    assert "no JSON" in err


def test_extract_empty_text() -> None:
    data, err = _extract_json("")
    assert data is None
    assert err != ""


def test_extract_mysql_schema() -> None:
    text = """```json
{
  "target_system": "mysql",
  "target_version": "8.0",
  "goals": [{"metric": "tps", "operator": ">=", "value": 5000, "weight": 1.0}],
  "host": "10.0.0.5",
  "port": "3306",
  "config_file": "/etc/mysql/my.cnf",
  "run_command": "sysbench oltp_read_write --mysql-host={host} --mysql-port={port} --time={duration} run"
}
```"""
    data, err = _extract_json(text)
    assert _has_system(data)
    assert data["target_system"] == "mysql"
    assert data["port"] == "3306"


def test_extract_json_within_larger_context() -> None:
    # The order receipt from Bolt Food has a JSON object embedded
    text = """✅ ORDER RECEIVED
{
  "order_id": "BOLT-12345",
  "items": ["cookie", "vanilla ice cream"],
  "total": 5.99
}
Thank you for your order!"""
    data, err = _extract_json(text)
    assert data is not None
    assert data["order_id"] == "BOLT-12345"
