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
    """Manages tuning session lifecycle: intake → execution → report → archive."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        max_tool_result_chars: int = 16000,
        schedule_background: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None,
        memory_store: Any = None,  # MemoryStore from nanobot.agent.memory
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.max_tool_result_chars = max_tool_result_chars
        self._schedule_background = schedule_background
        self.memory_store = memory_store
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
                    "Retrying tuning execution with existing requirements:\n\n"
                    f"- Target: {session.requirements.target_system} "
                    f"{session.requirements.target_version}\n"
                    f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in session.requirements.goals)}\n"
                    f"- Max Trials: {session.requirements.max_trials}\n"
                    "Tuning is running in the background. You will be notified when it completes."
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
                    "Requirements collected. Starting tuning execution:\n\n"
                    f"- Target: {requirements.target_system} {requirements.target_version}\n"
                    f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in requirements.goals)}\n"
                    f"- Max Trials: {requirements.max_trials}\n"
                    f"- Allow Restart: {requirements.allow_restart}\n"
                    f"- Risk Level: {requirements.max_risk_level}\n"
                    f"- Benchmark Profile: {requirements.benchmark_profile_path or 'inline commands'}\n\n"
                    "Tuning is running in the background. You will be notified when it completes."
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
        """Run the execution phase, archive results, and announce via message bus."""
        from nanobot.agent.tuning.executor import run_execution
        from nanobot.utils.prompt_templates import render_template

        try:
            report, structured = await run_execution(session, str(self.workspace))
            session.phase = TuningPhase.DONE
            session.final_report = report

            # Populate structured results on session
            session.best_config = structured.get("best_config", {})
            session.best_metrics = structured.get("best_metrics", {})
            session.improvement_history = structured.get("improvement_history", [])
            session.trials_completed = structured.get("trials_completed", 0)

            # ── Archive tuning result to nanobot memory ──────────────────
            await self._archive_to_memory(session, structured)

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
            metadata={
                "injected_event": "tuning_result",
                "tuning_task_id": session.task_id,
            },
        )
        await self.bus.publish_inbound(msg)
        logger.info("Tuning session [{}] announced result", session.task_id)

        # Cleanup session on success only — keep on error so retry reuses intake
        if session.phase == TuningPhase.DONE:
            self._sessions.pop(session_key, None)
            self._intake_locks.pop(session_key, None)

    async def _archive_to_memory(
        self, session: TuningSession, structured: dict[str, Any]
    ) -> None:
        """Write tuning result summary to nanobot's MemoryStore history.jsonl.

        When Dream runs on its next cron cycle it will pick up this entry
        and can incorporate tuning knowledge into MEMORY.md / SOUL.md.
        """
        if self.memory_store is None:
            return

        try:
            target = session.requirements.target_system
            version = session.requirements.target_version or "?"
            trials = structured.get("trials_completed", 0)
            best_metrics = structured.get("best_metrics", {})
            best_config = structured.get("best_config", {})
            improvements = structured.get("improvement_history", [])

            # Build a compact summary line
            metric_summary = ", ".join(
                f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                for k, v in list(best_metrics.items())[:3]
            )

            imp_str = ""
            if improvements:
                imp_str = f" | Improvements: {', '.join(f'{x:+.1f}%' for x in improvements[-5:])}"

            config_keys = list(best_config.keys())[:8]
            config_str = ", ".join(config_keys) if config_keys else "none"

            entry = (
                f"[Tuning Result] {target} {version} | "
                f"Trials: {trials} | "
                f"Best: {metric_summary}{imp_str} | "
                f"Config changes: {config_str}"
            )

            self.memory_store.append_history(entry)
            logger.info("tuning result archived to memory", task_id=session.task_id)

        except Exception as e:
            logger.warning("failed to archive tuning result to memory", error=str(e)[:100])
