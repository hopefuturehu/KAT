"""Agent extension protocol — lifecycle hooks, discovery, and tool integration.

Extensions are discovered via ``entry_points(group="nanobot.extensions")`` and
hook into the agent lifecycle.  They may register tools, publish/receive messages
on the bus, receive provider/model updates at runtime, and read their config
fragment from ``config.agents.defaults.extensions.<name>``.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager
    from nanobot.agent.tools.base import Tool


@dataclass
class ExtensionContext:
    """Injected into an extension during setup()."""

    name: str
    provider: "LLMProvider"
    workspace: Path
    bus: "MessageBus"
    model: str
    config: dict[str, Any] = field(default_factory=dict)
    max_tool_result_chars: int = 16_000
    session_manager: "SessionManager | None" = None
    schedule_background: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None


class UnsupportedExtensionActionError(RuntimeError):
    """Raised when a caller invokes an unsupported extension action."""


class AgentExtension(ABC):
    """Protocol for an agent subsystem extension.

    Lifecycle::

        Extension.__init__()           # Instantiated once per AgentLoop
        Extension.setup(ctx)           # Called during AgentLoop startup
        ... agent loop running ...     # Tools called, provider may change
        Extension.set_provider(p, m)   # Called when provider/model changes
        Extension.teardown()           # Called during AgentLoop shutdown

    Tools declared via ``get_tool_classes()`` are registered by AgentLoop after
    setup().  Each tool's ``create(ctx)`` receives a ``ToolContext`` whose
    ``extensions`` dict carries the live extension instance.
    """

    name: str = ""
    sender_id: str = ""

    # ---- lifecycle hooks ----

    def _bind_context(self, ctx: ExtensionContext) -> None:
        """Attach the live extension context before ``setup()`` runs."""
        self._extension_ctx = ctx
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def setup(self, _ctx: ExtensionContext) -> None:
        """Called once when the agent loop starts."""

    async def teardown(self) -> None:
        """Called when the agent loop shuts down."""
        tasks = list(getattr(self, "_background_tasks", set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        getattr(self, "_background_tasks", set()).clear()

    def set_provider(self, _provider: "LLMProvider", _model: str) -> None:
        """Called when the current provider or model changes at runtime."""

    async def invoke(self, action: str, **_kwargs: Any) -> Any:
        """Execute a named action exposed by the extension."""
        raise UnsupportedExtensionActionError(
            f"Extension {self.name or type(self).__name__} does not support action {action!r}"
        )

    # ---- tool support ----

    def get_tool_classes(self) -> list[type["Tool"]]:
        """Return Tool subclasses this extension provides."""
        return []

    # ---- config ----

    @classmethod
    def get_config_key(cls) -> str | None:
        """Config key under ``agents.defaults.extensions.<key>``."""
        return cls.name

    def schedule_background(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Schedule a background task via the owning agent loop."""
        ctx = getattr(self, "_extension_ctx", None)
        if ctx is None or ctx.schedule_background is None:
            raise RuntimeError(
                f"Extension {self.name or type(self).__name__} has no background scheduler bound"
            )
        task = ctx.schedule_background(coro)
        background_tasks = getattr(self, "_background_tasks", None)
        if background_tasks is None:
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

import importlib as _importlib

_BUILTIN_EXTENSIONS: dict[str, str] = {
    # "name": "module.path:ClassName"
    "tuning": "nanobot.agent.tuning.nanobot_plugin:TuningExtension",
}


def _load_extension(import_path: str) -> type[AgentExtension] | None:
    """Load an extension class from a ``module.path:ClassName`` string."""
    try:
        module_path, cls_name = import_path.split(":")
        module = _importlib.import_module(module_path)
        cls = getattr(module, cls_name)
        if isinstance(cls, type) and issubclass(cls, AgentExtension):
            return cls
    except Exception:
        logger.exception("Failed to load extension: %s", import_path)
    return None


def discover_extensions() -> dict[str, type[AgentExtension]]:
    """Discover all installed extensions.

    Merges built-in extensions (defined in ``_BUILTIN_EXTENSIONS``) with
    third-party extensions registered via ``nanobot.extensions`` entry_points.
    Entry-point extensions take priority over built-ins with the same name.
    """
    extensions: dict[str, type[AgentExtension]] = {}

    # Built-in extensions (loaded first; entry_points shadow them)
    for name, import_path in _BUILTIN_EXTENSIONS.items():
        cls = _load_extension(import_path)
        if cls is not None:
            extensions[name] = cls

    # Entry-point extensions (shadow built-ins with the same name)
    try:
        eps = importlib.metadata.entry_points(group="nanobot.extensions")
    except TypeError:
        eps = importlib.metadata.entry_points().get("nanobot.extensions", [])

    for ep in eps:
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, AgentExtension):
                if ep.name in extensions:
                    logger.info(
                        "Extension %s: entry_point shadows built-in", ep.name,
                    )
                extensions[ep.name] = cls
        except Exception:
            logger.exception("Failed to load extension plugin: %s", ep.name)

    return extensions
