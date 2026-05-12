import asyncio
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.extensions import AgentExtension, ExtensionContext
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.tuner import TuneTool


class StubTuningExtension:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, action: str, **kwargs: Any) -> str:
        self.calls.append((action, kwargs))
        return "ok"


@pytest.mark.asyncio
async def test_tune_tool_uses_extension_invoke_protocol() -> None:
    extension = StubTuningExtension()
    tool = TuneTool(extension)
    tool.set_context(RequestContext(channel="telegram", chat_id="user-1", session_key="telegram:user-1"))

    result = await tool.execute(
        task="tune redis for throughput",
        response="yes",
        host="127.0.0.1",
        port="6380",
        password="secret",
        config_file="/tmp/redis.conf",
    )

    assert result == "ok"
    assert len(extension.calls) == 1
    action, kwargs = extension.calls[0]
    assert action == "tune_request"
    assert kwargs["session_key"] == "telegram:user-1"
    assert kwargs["origin_channel"] == "telegram"
    assert kwargs["origin_chat_id"] == "user-1"
    assert "Connection details" in kwargs["task"]
    assert "Host: 127.0.0.1:6380" in kwargs["task"]
    assert "Config file: /tmp/redis.conf" in kwargs["task"]
    assert "Password: [provided]" in kwargs["task"]


class BackgroundExtension(AgentExtension):
    name = "background"

    def __init__(self) -> None:
        self.cancelled = False

    async def setup(self, ctx: ExtensionContext) -> None:
        self.schedule_background(self._worker())

    async def _worker(self) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_extension_teardown_cancels_background_tasks() -> None:
    tracked_tasks: list[asyncio.Task[Any]] = []

    def _schedule_background(coro):
        task = asyncio.create_task(coro)
        tracked_tasks.append(task)
        return task

    extension = BackgroundExtension()
    ctx = ExtensionContext(
        name="background",
        provider=None,  # type: ignore[arg-type]
        workspace=Path("/tmp"),
        bus=None,  # type: ignore[arg-type]
        model="test-model",
        schedule_background=_schedule_background,
    )
    extension._bind_context(ctx)

    await extension.setup(ctx)
    assert tracked_tasks
    await extension.teardown()

    assert extension.cancelled is True
    assert all(task.done() for task in tracked_tasks)
