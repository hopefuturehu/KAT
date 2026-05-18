"""Route tuning requests into an internal LangGraph tuning flow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from loguru import logger

from nanobot.agent.tuning.intent import (
    detect_target_system,
    looks_like_escape_request,
    looks_like_retry_request,
    looks_like_tuning_request,
)
from nanobot.agent.tuning.manager import TuningSessionManager
from nanobot.agent.tuning.schema import TuningPhase

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider


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
    """Route tuning requests outside the main tool loop."""

    sender_id = "tuning"

    def __init__(
        self,
        provider: "LLMProvider",
        workspace: Path,
        bus: "MessageBus",
        model: str | None = None,
        max_tool_result_chars: int = 16000,
        schedule_background: Any | None = None,
        memory_store: Any = None,
    ) -> None:
        self.manager = TuningSessionManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=model,
            max_tool_result_chars=max_tool_result_chars,
            schedule_background=schedule_background,
            memory_store=memory_store,
        )
    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        self.manager.set_provider(provider, model)

    def has_pending_session(self, session_key: str) -> bool:
        session = self.manager.get_session(session_key)
        return session is not None and session.phase in {TuningPhase.INTAKE, TuningPhase.ERROR}

    # ------------------------------------------------------------------
    # Keyword-based intent detection
    # ------------------------------------------------------------------

    def _detect_target_system(self, message: str) -> str | None:
        return detect_target_system(message)

    def _looks_like_tuning_request(self, message: str) -> bool:
        return looks_like_tuning_request(message)

    def _looks_like_retry_request(self, message: str) -> bool:
        return looks_like_retry_request(message)

    def _looks_like_escape_request(self, message: str) -> bool:
        return looks_like_escape_request(message)

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
            cancel_response = None
            if session is not None:
                cancel_response = self.manager.cancel_session(session_key)
            if cancel_response:
                logger.info("User cancelled tuning session {}", session_key)
                return {
                    **state,
                    "should_route": False,
                    "route_reason": "cancel_tuning",
                    "task": task,
                    "user_response": user_response,
                    "response": cancel_response,
                }
        elif session is not None and session.phase == TuningPhase.INTAKE:
            should_route = True
            route_reason = "continue_intake"
            task = session.task_description
            user_response = message
        elif session is not None and session.phase == TuningPhase.ERROR:
            if self._looks_like_retry_request(message):
                should_route = True
                route_reason = "retry_after_error"
                task = session.task_description
            elif self._looks_like_tuning_request(message):
                # User is trying to start a new tuning but a failed one exists.
                # Prompt them to retry instead of silently starting a new session.
                return {
                    **state,
                    "should_route": False,
                    "route_reason": "tuning_error_exists",
                    "response": (
                        "A previous tuning session failed with the error below. "
                        "Reply `retry` to re-run with the same requirements, or "
                        "`cancel tuning` to discard it and start fresh.\n\n"
                        f"**Previous task**: {session.task_description}\n"
                        f"**Error**: {session.error or 'unknown'}"
                    ),
                }
            # Not a tuning/retry request — don't route, let main agent handle it.
        elif session is not None and session.phase in (TuningPhase.EXECUTION, TuningPhase.DONE):
            # A tuning session is already active (or just finished) for this
            # conversation.  Don't allow a second tuning process — return a
            # status message if the user appears to be asking for tuning,
            # otherwise stay silent and let the main agent handle the message.
            if self._looks_like_tuning_request(message):
                return {
                    **state,
                    "should_route": False,
                    "route_reason": "tuning_already_active",
                    "response": (
                        "A tuning session is already in progress for this conversation.\n\n"
                        + (session.execution_summary() if session.requirements else "")
                        + "\nWait for it to complete, or reply `cancel tuning` to stop it."
                    ),
                }
            # Not a tuning request — don't route, let main agent handle it.
        elif self._looks_like_tuning_request(message):
            should_route = True
            target_system = self._detect_target_system(message) or "unknown"
            route_reason = f"{target_system}_tuning_intent"

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
        initial_state = {
            "message": message,
            "session_key": session_key,
            "origin_channel": origin_channel,
            "origin_chat_id": origin_chat_id,
        }
        state = self._classify_request(initial_state)
        if not state.get("should_route"):
            return state.get("response")

        result = await self._dispatch_request(state)
        logger.info(
            "Routed session {} into tuning flow via {}",
            session_key,
            result.get("route_reason", "unknown"),
        )
        return result.get("response")
