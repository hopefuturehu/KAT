"""TuningSessionManager — orchestrate two-phase tuning sessions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tuning.intake import run_intake_turn
from nanobot.agent.tuning.schema import TuningPhase, TuningSession
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider


class TuningSessionManager:
    """Manages tuning session lifecycle: intake → execution → report."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        max_tool_result_chars: int = 16000,
        schedule_background: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.max_tool_result_chars = max_tool_result_chars
        self._schedule_background = schedule_background
        self.runner = AgentRunner(provider)
        self._sessions: dict[str, TuningSession] = {}  # session_key -> session
        self._intake_locks: dict[str, asyncio.Lock] = {}

    def _spawn_background(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        if self._schedule_background is not None:
            return self._schedule_background(coro)
        return asyncio.create_task(coro)

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    def get_session(self, session_key: str) -> TuningSession | None:
        return self._sessions.get(session_key)

    def cancel_session(self, session_key: str) -> bool:
        session = self._sessions.pop(session_key, None)
        self._intake_locks.pop(session_key, None)
        return session is not None

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._intake_locks:
            self._intake_locks[session_key] = asyncio.Lock()
        return self._intake_locks[session_key]

    async def handle_tune_request(
        self,
        task: str,
        user_response: str = "",
        session_key: str = "",
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ) -> str:
        """Handle a tune request from the main agent.

        On first call (no user_response), starts intake conversation.
        On subsequent calls (with user_response), continues intake.
        When requirements are complete, spawns execution phase.
        If a previous execution failed, retries without redoing intake.
        """
        lock = self._get_lock(session_key)

        async with lock:
            session = self._sessions.get(session_key)

            # If session exists and already has requirements, retry execution
            if session is not None and session.phase in (
                TuningPhase.EXECUTION,
                TuningPhase.ERROR,
            ):
                if session.phase == TuningPhase.ERROR:
                    session.phase = TuningPhase.EXECUTION
                self._spawn_background(
                    self._run_execution_and_report(
                        session, session_key, origin_channel, origin_chat_id
                    )
                )
                return (
                    f"Retrying tuning execution with existing requirements:\n\n"
                    f"- Target: {session.requirements.target_system} "
                    f"{session.requirements.target_version}\n"
                    f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in session.requirements.goals)}\n"
                    f"- Max Trials: {session.requirements.max_trials}\n"
                    f"Tuning is running in the background. You will be notified when it completes."
                )

            if session is None:
                # First call: create session and start intake
                session = TuningSession(
                    task_id=str(uuid.uuid4())[:8],
                    task_description=task,
                )
                self._sessions[session_key] = session
                conversation: list[dict[str, Any]] = [
                    {"role": "user", "content": task}
                ]
            elif user_response:
                # Continue intake with user's response
                conversation = session._intake_conversation
                conversation.append({"role": "user", "content": user_response})
            else:
                # No response provided, re-state current status
                conversation = session._intake_conversation

            response, updated_conversation, requirements = await run_intake_turn(
                runner=self.runner,
                provider=self.provider,
                model=self.model,
                workspace=str(self.workspace),
                conversation=conversation,
                max_tool_result_chars=self.max_tool_result_chars,
            )
            session._intake_conversation = updated_conversation

            if requirements is not None:
                # Requirements complete — transition to execution
                session.requirements = requirements
                session.phase = TuningPhase.EXECUTION

                # Spawn execution in background
                self._spawn_background(
                    self._run_execution_and_report(
                        session, session_key, origin_channel, origin_chat_id
                    )
                )

                return (
                    f"Requirements collected. Starting tuning execution:\n\n"
                    f"- Target: {requirements.target_system} {requirements.target_version}\n"
                    f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in requirements.goals)}\n"
                    f"- Max Trials: {requirements.max_trials}\n"
                    f"- Allow Restart: {requirements.allow_restart}\n"
                    f"- Risk Level: {requirements.max_risk_level}\n\n"
                    f"Tuning is running in the background. You will be notified when it completes."
                )

            if response is None:
                return "I encountered an issue processing your tuning request. Could you rephrase?"

            return response

    async def _run_execution_and_report(
        self,
        session: TuningSession,
        session_key: str,
        origin_channel: str,
        origin_chat_id: str,
    ) -> None:
        """Run the execution phase and announce results."""
        from nanobot.agent.tuning.executor import run_execution
        from nanobot.utils.prompt_templates import render_template

        try:
            report = await run_execution(session, str(self.workspace))
            session.phase = TuningPhase.DONE
            session.final_report = report

            announce = render_template(
                "agent/tuning_result.md",
                status="completed",
                task=session.task_description,
                report=report,
            )

        except Exception as e:
            logger.exception("Tuning execution failed: {}", session.task_id)
            session.phase = TuningPhase.ERROR
            session.error = str(e)

            announce = render_template(
                "agent/tuning_result.md",
                status="failed",
                task=session.task_description,
                report=f"Error: {e}",
            )

        # Announce via message bus
        msg = InboundMessage(
            channel="system",
            sender_id="tuning",
            chat_id=f"{origin_channel}:{origin_chat_id}",
            content=announce,
            session_key_override=session_key,
            metadata={"injected_event": "tuning_result", "tuning_task_id": session.task_id},
        )
        await self.bus.publish_inbound(msg)
        logger.info("Tuning session [{}] announced result", session.task_id)

        # Cleanup session on success only — keep on error so retry reuses intake
        if session.phase == TuningPhase.DONE:
            self._sessions.pop(session_key, None)
            self._intake_locks.pop(session_key, None)
