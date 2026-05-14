"""TuningSessionManager — orchestrate two-phase tuning sessions."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tuning.intake import run_intake_turn
from nanobot.agent.tuning.profile_store import TuningProfileStore
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
        self.profile_store = TuningProfileStore(workspace)
        self._sessions: dict[str, TuningSession] = {}  # session_key -> session
        self._intake_locks: dict[str, asyncio.Lock] = {}
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}

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

    def cancel_session(self, session_key: str) -> str | None:
        session = self._sessions.pop(session_key, None)
        self._intake_locks.pop(session_key, None)
        task = self._execution_tasks.pop(session_key, None)
        if task is not None and not task.done():
            task.cancel()
        if session is None and task is None:
            return None
        if task is not None and not task.done():
            return "Cancelled the tuning session and stopped the active execution."
        return "Cancelled the pending tuning session."

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
                if session.phase == TuningPhase.EXECUTION and self._is_execution_running(session_key):
                    return (
                        "Tuning execution is already running in the background.\n\n"
                        f"- Target: {session.requirements.target_system} {session.requirements.target_version}\n"
                        f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in session.requirements.goals)}\n"
                        f"- Max Trials: {session.requirements.max_trials}\n"
                        "You will be notified when it completes."
                    )
                if session.phase == TuningPhase.ERROR:
                    session.phase = TuningPhase.EXECUTION
                self._start_execution_task(
                    session, session_key, origin_channel, origin_chat_id
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
                session._intake_conversation = list(conversation)
                target_system = _infer_target_system(task)
                if target_system:
                    candidates = [
                        profile.summary()
                        for profile in self.profile_store.list_profiles(target_system)
                    ]
                    if candidates:
                        session.reuse_candidates = candidates
                        session.awaiting_profile_selection = True
                        return _format_profile_selection_prompt(target_system, candidates)
            elif user_response:
                should_append_user_response = True
                if session.awaiting_profile_selection:
                    action = _parse_profile_selection(user_response, session.reuse_candidates)
                    if action == "skip":
                        session.awaiting_profile_selection = False
                        session.reuse_candidates = []
                        should_append_user_response = False
                        conversation = session._intake_conversation or [
                            {"role": "user", "content": session.task_description}
                        ]
                    elif isinstance(action, dict):
                        selected_path = action.get("path", "")
                        requirements = self.profile_store.load_requirements(selected_path)
                        redacted_fields = [
                            str(item) for item in action.get("redacted_fields", [])
                        ]
                        if redacted_fields:
                            session.awaiting_profile_selection = False
                            session.reuse_candidates = []
                            session.phase = TuningPhase.INTAKE
                            prompt = _format_profile_completion_prompt(
                                action,
                                requirements,
                                redacted_fields,
                            )
                            session._intake_conversation = [
                                {"role": "user", "content": session.task_description},
                                {"role": "assistant", "content": prompt},
                            ]
                            return prompt
                        session.requirements = requirements
                        session.phase = TuningPhase.EXECUTION
                        session.awaiting_profile_selection = False
                        session.reuse_candidates = []
                        self._start_execution_task(
                            session, session_key, origin_channel, origin_chat_id
                        )
                        return (
                            "Using saved tuning profile and starting execution:\n\n"
                            f"- Profile: {action.get('name', Path(selected_path).stem)}\n"
                            f"- Profile YAML: {selected_path}\n"
                            f"- Target: {requirements.target_system} {requirements.target_version}\n"
                            f"- Host: {requirements.host}:{requirements.port}\n"
                            f"- Config: {requirements.config_file}\n"
                            f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in requirements.goals)}\n\n"
                            "Tuning is running in the background. You will be notified when it completes."
                        )
                    else:
                        return _format_profile_selection_prompt(
                            _infer_target_system(session.task_description) or "target",
                            session.reuse_candidates,
                            invalid_response=True,
                        )

                # Continue intake with user's response
                if should_append_user_response:
                    conversation = list(session._intake_conversation)
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
                saved_profile = self.profile_store.save_requirements(
                    requirements,
                    task_description=session.task_description,
                )

                # Spawn execution in background
                self._start_execution_task(
                    session, session_key, origin_channel, origin_chat_id
                )

                return (
                    "Requirements collected. Starting tuning execution:\n\n"
                    f"- Target: {requirements.target_system} {requirements.target_version}\n"
                    f"- Goals: {', '.join(f'{g.metric} {g.operator} {g.value}' for g in requirements.goals)}\n"
                    f"- Max Trials: {requirements.max_trials}\n"
                    f"- Allow Restart: {requirements.allow_restart}\n"
                    f"- Risk Level: {requirements.max_risk_level}\n"
                    f"- Benchmark Profile: {requirements.benchmark_profile_path or 'inline commands'}\n"
                    f"- Saved Profile: {saved_profile}\n\n"
                    "Tuning is running in the background. You will be notified when it completes."
                )

            if response is None:
                return "I encountered an issue processing your tuning request. Could you rephrase?"

            return response

    def _is_execution_running(self, session_key: str) -> bool:
        task = self._execution_tasks.get(session_key)
        if task is None:
            return False
        if task.done():
            self._execution_tasks.pop(session_key, None)
            return False
        return True

    def _start_execution_task(
        self,
        session: TuningSession,
        session_key: str,
        origin_channel: str,
        origin_chat_id: str,
    ) -> asyncio.Task[Any]:
        existing = self._execution_tasks.get(session_key)
        if existing is not None and not existing.done():
            return existing
        task = self._spawn_background(
            self._run_execution_and_report(
                session, session_key, origin_channel, origin_chat_id
            )
        )
        self._execution_tasks[session_key] = task
        task.add_done_callback(lambda _: self._execution_tasks.pop(session_key, None))
        return task

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
            report, structured = await run_execution(
                session, str(self.workspace),
                provider=self.provider,
                model=self.model,
            )
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

        except asyncio.CancelledError:
            logger.info("Tuning execution cancelled: {}", session.task_id)
            announce = render_template(
                "agent/tuning_result.md",
                status="cancelled",
                task=session.task_description,
                report="Execution cancelled by user request.",
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


_TARGET_SYSTEM_PATTERNS = {
    "redis": re.compile(r"\bredis\b", re.IGNORECASE | re.ASCII),
    "mysql": re.compile(r"\bmysql\b", re.IGNORECASE | re.ASCII),
}
_PROFILE_SKIP_KEYWORDS = {
    "none", "skip", "manual", "new", "no", "不用", "不使用", "跳过", "重新配置", "手动填写",
}


def _infer_target_system(task: str) -> str | None:
    normalized = task.strip().lower()
    if not normalized:
        return None
    for system, pattern in _TARGET_SYSTEM_PATTERNS.items():
        if pattern.search(normalized):
            return system
    return None


def _format_profile_selection_prompt(
    target_system: str,
    candidates: list[dict[str, Any]],
    *,
    invalid_response: bool = False,
) -> str:
    header = (
        "I found saved tuning profiles for this target. Reply with the number to reuse one, "
        "or reply `skip` to continue manual intake.\n\n"
    )
    if invalid_response:
        header = (
            "I couldn't match that selection. Reply with the profile number, exact profile name, "
            "or `skip` to continue manual intake.\n\n"
        )
    lines = [
        f"{idx}. {item['name']}  ({item['host']}:{item['port']})"
        + (" [needs confirmation]" if item.get("redacted_fields") else "")
        for idx, item in enumerate(candidates, start=1)
    ]
    details = [
        f"   - Config: {item['config_file']}"
        + (
            f" | Benchmark YAML: {item['benchmark_profile_path']}"
            if item.get("benchmark_profile_path")
            else ""
        )
        for item in candidates
    ]
    combined: list[str] = []
    for title, detail in zip(lines, details, strict=False):
        combined.append(title)
        combined.append(detail)
    return header + f"Detected target system: {target_system}\n\n" + "\n".join(combined)


def _parse_profile_selection(
    response: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | str | None:
    normalized = response.strip().lower()
    if not normalized:
        return None
    if normalized in _PROFILE_SKIP_KEYWORDS:
        return "skip"
    if normalized.isdigit():
        idx = int(normalized) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
        return None
    for item in candidates:
        if normalized in {
            str(item.get("name", "")).lower(),
            str(item.get("profile_id", "")).lower(),
        }:
            return item
    return None


def _format_profile_completion_prompt(
    candidate: dict[str, Any],
    requirements: Any,
    redacted_fields: list[str],
) -> str:
    missing = ", ".join(redacted_fields)
    return (
        "Loaded the saved tuning profile below, but some sensitive fields were not stored for safety.\n\n"
        f"- Profile: {candidate.get('name', 'unknown')}\n"
        f"- Profile YAML: {candidate.get('path', '')}\n"
        f"- Target: {requirements.target_system} {requirements.target_version}\n"
        f"- Host: {requirements.host}:{requirements.port}\n"
        f"- Config: {requirements.config_file}\n"
        f"- Missing sensitive fields: {missing}\n\n"
        "Please provide the missing values and confirm the target details. "
        "When the requirements are complete, I will continue with tuning."
    )
