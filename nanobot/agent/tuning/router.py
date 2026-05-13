"""Route Redis tuning requests directly into the tuning flow, bypassing the main LLM."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from loguru import logger

from nanobot.agent.tuning.manager import TuningSessionManager
from nanobot.agent.tuning.schema import TuningPhase

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider


_REDIS_RE = re.compile(r"\bredis\b", re.IGNORECASE)
_TUNING_KEYWORDS = (
    "tune", "tuning", "optimize", "optimization", "benchmark",
    "throughput", "latency", "qps", "rps", "performance",
    "config", "parameter",
    "调优", "调参", "优化", "性能", "吞吐", "延迟", "基准", "配置", "参数",
)
_RETRY_KEYWORDS = (
    "continue", "retry", "rerun", "run again", "try again",
    "继续", "重试", "再试", "重新跑", "重新执行",
)
_ESCAPE_KEYWORDS = (
    "cancel tuning", "stop tuning", "abort tuning",
    "取消调优", "停止调优", "退出调优",
    "not tuning", "no tuning",
)


class TuningRouteState(TypedDict, total=False):
    message: str
    session_key: str
    origin_channel: str
    origin_chat_id: str
    should_route: bool
    route_reason: str
    task: str
    user_response: str
    response: str | None


class TuningIntentRouter:
    """Route Redis tuning requests outside the main tool loop."""

    sender_id = "tuning"

    def __init__(
        self,
        provider: "LLMProvider",
        workspace: Path,
        bus: "MessageBus",
        model: str | None = None,
        max_tool_result_chars: int = 16000,
        schedule_background: Any | None = None,
    ) -> None:
        self.manager = TuningSessionManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=model,
            max_tool_result_chars=max_tool_result_chars,
            schedule_background=schedule_background,
        )

    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        self.manager.set_provider(provider, model)

    def has_pending_session(self, session_key: str) -> bool:
        session = self.manager.get_session(session_key)
        return session is not None and session.phase in {TuningPhase.INTAKE, TuningPhase.ERROR}

    # ------------------------------------------------------------------
    # Keyword-based intent detection
    # ------------------------------------------------------------------

    def _looks_like_redis_tuning_request(self, message: str) -> bool:
        normalized = message.strip().lower()
        if not normalized:
            return False
        if not _REDIS_RE.search(normalized):
            return False
        return any(keyword in normalized for keyword in _TUNING_KEYWORDS)

    def _looks_like_retry_request(self, message: str) -> bool:
        normalized = message.strip().lower()
        return any(keyword in normalized for keyword in _RETRY_KEYWORDS)

    def _looks_like_escape_request(self, message: str) -> bool:
        normalized = message.strip().lower()
        return any(keyword in normalized for keyword in _ESCAPE_KEYWORDS)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _classify_request(self, state: TuningRouteState) -> TuningRouteState:
        session_key = state["session_key"]
        message = state["message"].strip()
        session = self.manager.get_session(session_key)
        should_route = False
        route_reason = ""
        task = message
        user_response = ""

        if self._looks_like_escape_request(message):
            should_route = False
            if session is not None:
                self.manager._sessions.pop(session_key, None)
                self.manager._intake_locks.pop(session_key, None)
                logger.info("User cancelled tuning session {}", session_key)
        elif session is not None and session.phase == TuningPhase.INTAKE:
            should_route = True
            route_reason = "continue_intake"
            task = session.task_description
            user_response = message
        elif session is not None and session.phase == TuningPhase.ERROR and self._looks_like_retry_request(message):
            should_route = True
            route_reason = "retry_after_error"
            task = session.task_description
        elif self._looks_like_redis_tuning_request(message):
            should_route = True
            route_reason = "redis_tuning_intent"

        return {
            **state,
            "should_route": should_route,
            "route_reason": route_reason,
            "task": task,
            "user_response": user_response,
        }

    async def _dispatch_request(self, state: TuningRouteState) -> TuningRouteState:
        response = await self.manager.handle_tune_request(
            task=state["task"],
            user_response=state.get("user_response", ""),
            session_key=state["session_key"],
            origin_channel=state["origin_channel"],
            origin_chat_id=state["origin_chat_id"],
        )
        return {**state, "response": response}

    async def route_message(
        self,
        *,
        message: str,
        session_key: str,
        origin_channel: str,
        origin_chat_id: str,
    ) -> str | None:
        """Classify the message and, if it matches a tuning intent, dispatch it.

        Returns the tuning response string, or ``None`` when the main agent
        should handle the message normally.
        """
        state = self._classify_request({
            "message": message,
            "session_key": session_key,
            "origin_channel": origin_channel,
            "origin_chat_id": origin_chat_id,
        })
        if not state.get("should_route"):
            return None

        result = await self._dispatch_request(state)
        logger.info(
            "Routed session {} into tuning flow via {}",
            session_key,
            result.get("route_reason", "unknown"),
        )
        return result.get("response")
