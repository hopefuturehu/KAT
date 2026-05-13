from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tuning.schema import TuningPhase
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# Routing: tuning messages bypass the main LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_tuning_is_routed_without_main_agent_run(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    async def _unexpected_chat(**_kwargs):
        raise AssertionError("main agent LLM should not run for routed tuning turns")

    provider.chat_with_retry = _unexpected_chat

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    loop.tuning.route_message = AsyncMock(
        return_value="Requirements collected. Starting tuning execution."
    )

    outbound = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="请帮我调优 redis 吞吐量",
        )
    )

    assert outbound is not None
    assert outbound.content == "Requirements collected. Starting tuning execution."
    loop.tuning.route_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Non-tuning messages are NOT routed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_message_not_routed(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    main_agent_called = False

    async def _fake_chat(**_kwargs):
        nonlocal main_agent_called
        main_agent_called = True
        return {"content": [{"text": "Hello!"}], "role": "assistant"}

    provider.chat_with_retry = _fake_chat
    # _run_agent_loop uses chat() internally; mock the whole runner
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop._run_agent_loop = AsyncMock(
        return_value=("Hello!", [], [{"role": "assistant", "content": "Hello!"}], "ok", False)
    )

    outbound = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="what is redis used for",  # no tuning keywords
        )
    )

    assert outbound is not None
    assert outbound.content == "Hello!"
    loop._run_agent_loop.assert_awaited_once()
    # tuning router returned None, so the message was not intercepted
    loop.tuning.route_message = AsyncMock(return_value=None)


# ---------------------------------------------------------------------------
# Escape keywords cancel a pending tuning session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_keyword_cancels_tuning_session(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=MagicMock(get_default_model=MagicMock(return_value="test-model")),
        workspace=tmp_path,
        model="test-model",
    )

    # Pre-populate an INTAKE session so the router knows one is active
    loop.tuning.manager._sessions["cli:direct"] = MagicMock(
        phase=TuningPhase.INTAKE,
    )

    # "cancel tuning" should NOT route — it should cancel the session instead
    result = await loop.tuning.route_message(
        message="cancel tuning",
        session_key="cli:direct",
        origin_channel="cli",
        origin_chat_id="direct",
    )

    assert result is None  # not routed
    assert "cli:direct" not in loop.tuning.manager._sessions  # session cleared


# ---------------------------------------------------------------------------
# Tuning system messages are persisted as background results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tuning_system_message_is_persisted(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop._run_agent_loop = AsyncMock(
        return_value=("ack", [], [{"role": "assistant", "content": "ack"}], "ok", False)
    )

    outbound = await loop._process_system_message(
        InboundMessage(
            channel="system",
            sender_id="tuning",
            chat_id="cli:direct",
            content="## Tuning Report",
            metadata={"injected_event": "tuning_result", "tuning_task_id": "abc123"},
        )
    )

    assert outbound is not None
    session = loop.sessions.get_or_create("cli:direct")
    # Find the persisted system message (second-to-last, before the ack)
    system_msgs = [m for m in session.messages if m["role"] == "system" and m["content"] == "## Tuning Report"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["sender_id"] == "tuning"


# ---------------------------------------------------------------------------
# Missing dependencies produce a clear error before intake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_dependency_fails_fast(tmp_path: Path) -> None:
    from nanobot.agent.tuning.executor import _check_dependencies

    # docker is not installed in the test env; structlog may or may not be
    missing = _check_dependencies("docker")
    assert "docker" in missing  # docker mode requires docker

    missing_direct = _check_dependencies("direct")
    assert "docker" not in missing_direct  # direct mode does not require docker
