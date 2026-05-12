"""TuningExtension — nanobot AgentExtension for the tuning subsystem."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.extensions import AgentExtension, ExtensionContext

if TYPE_CHECKING:
    from nanobot.agent.tools.base import Tool


class TuningExtension(AgentExtension):
    """Extension that provides ``tune_start`` tool and background tuning workflow."""

    name = "tuning"
    sender_id = "tuning"

    def __init__(self):
        self._mgr: Any = None

    async def setup(self, ctx: ExtensionContext) -> None:
        from nanobot.agent.tuning.manager import TuningSessionManager

        self._mgr = TuningSessionManager(
            provider=ctx.provider,
            workspace=ctx.workspace,
            bus=ctx.bus,
            model=ctx.model,
            max_tool_result_chars=ctx.max_tool_result_chars,
        )

    def set_provider(self, provider: Any, model: str) -> None:
        if self._mgr is not None:
            self._mgr.set_provider(provider, model)

    def get_tool_classes(self) -> list[type["Tool"]]:
        from nanobot.agent.tools.tuner import TuneTool

        return [TuneTool]

    async def invoke(self, action: str, **kwargs: Any) -> Any:
        """Dispatch protocol-level extension actions."""
        if action != "tune_request":
            return await super().invoke(action, **kwargs)
        if self._mgr is None:
            raise RuntimeError("tuning extension is not initialized")
        return await self._mgr.handle_tune_request(**kwargs)

    async def teardown(self) -> None:
        await super().teardown()
        if self._mgr is not None:
            self._mgr._sessions.clear()
        self._mgr = None
