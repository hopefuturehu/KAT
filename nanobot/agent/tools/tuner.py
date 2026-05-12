"""Tune tool for delegating performance tuning tasks to the TuningAgent."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.tuning.manager import TuningSessionManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("Description of the tuning task (e.g., 'tune Redis 7.2 for max throughput')"),
        response=StringSchema(
            "User's response to a clarifying question from a previous tune_start call (omit on first call)"
        ),
        host=StringSchema("Optional: Redis/MySQL host address for direct-connect mode (skip for Docker mode)"),
        port=StringSchema("Optional: Redis/MySQL port"),
        password=StringSchema("Optional: Redis/MySQL password"),
        config_file=StringSchema("Optional: path to the config file (redis.conf or my.cnf) for direct-connect mode"),
        required=["task"],
    )
)
class TuneTool(Tool, ContextAware):
    """Start or continue a performance tuning session.

    On first call, the agent will ask clarifying questions about the tuning
    goals, target system, and constraints. On subsequent calls, provide the
    user's answers via the ``response`` parameter to continue the intake
    conversation.

    Once requirements are collected, the tuning workflow runs in the
    background. You will be notified when it completes.
    """

    _scopes = {"core"}

    def __init__(self, manager: "TuningSessionManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar("tune_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("tune_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("tune_session_key", default="cli:direct")

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.tuning_manager)

    def set_context(self, ctx: RequestContext) -> None:
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    @property
    def name(self) -> str:
        return "tune_start"

    @property
    def description(self) -> str:
        return (
            "Start or continue a performance tuning session for Redis or MySQL. "
            "On first call, the agent will ask clarifying questions about goals, "
            "constraints, and target system. On subsequent calls, provide the "
            "user's answers via the 'response' parameter. "
            "Once all requirements are collected, the tuning workflow runs "
            "automatically in the background and reports back when done."
        )

    async def execute(
        self,
        task: str,
        response: str = "",
        host: str = "",
        port: str = "",
        password: str = "",
        config_file: str = "",
        **kwargs: Any,
    ) -> str:
        channel = self._origin_channel.get()
        chat_id = self._origin_chat_id.get()
        session_key = self._session_key.get()

        # Merge optional direct-connect params into task context
        full_task = task
        extras = []
        if host:
            extras.append(f"Host: {host}:{port or '6379'}")
        if config_file:
            extras.append(f"Config file: {config_file}")
        if password:
            extras.append("Password: [provided]")
        if extras:
            full_task = task + "\n\nConnection details:\n" + "\n".join(extras)

        return await self._manager.handle_tune_request(
            task=full_task,
            user_response=response,
            session_key=session_key,
            origin_channel=channel,
            origin_chat_id=chat_id,
        )
