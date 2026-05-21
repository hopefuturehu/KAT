"""Base agent with multi-provider LLM integration and resilience layer."""

from __future__ import annotations

import asyncio
import json
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import BaseModel

from src.config import settings
from src.utils.llm_resilience import (
    CircuitBreakerOpenError,
    ResilienceManager,
    ToolCallLoopExceededError,
    async_retry,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


class AgentTool(BaseModel):
    name: str
    description: str
    parameters: dict = {}
    handler: Callable | None = None

    class Config:
        arbitrary_types_allowed = True


# ── Per-provider resilience manager ──────────────────────────────────────────

_resilience = ResilienceManager(
    default_rate_limit_rps=settings.llm_rate_limit_rps,
    default_failure_threshold=settings.llm_circuit_breaker_failures,
    default_recovery_timeout=settings.llm_circuit_breaker_recovery,
)

# Per-provider RPS overrides (None = use default)
_PROVIDER_RPS: dict[str, float | None] = {
    "anthropic": settings.llm_rate_limit_rps_anthropic,
    "deepseek": settings.llm_rate_limit_rps_deepseek,
    "openai": settings.llm_rate_limit_rps_openai,
}


class BaseAgent(ABC):
    agent_name: str = "base"
    system_prompt_template: str = ""

    def __init__(self, model: str | None = None):
        self.model = model or settings.llm_model
        self.provider = settings.llm_provider
        self.tools: list[AgentTool] = []
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._register_tools()

    # ── Subclass hooks ─────────────────────────────────────────────────────

    def _register_tools(self) -> None:
        """Override in subclass to register agent-specific tools."""

    def register_tool(self, tool: AgentTool) -> None:
        self.tools.append(tool)

    def build_system_prompt(self, context: dict) -> str:
        from jinja2 import Template
        template = Template(self.system_prompt_template)
        return template.render(**context)

    # ── Client factory (thread‑safe lazy init) ─────────────────────────────

    def _get_openai_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            from openai import AsyncOpenAI  # type: ignore
            if self.provider == "deepseek":
                self._client = AsyncOpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_api_base,
                )
            else:
                self._client = AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_api_base or None,
                )
            return self._client

    async def _get_anthropic_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            return self._client

    # ── OpenAI‑compatible invocation ───────────────────────────────────────

    def _format_tools_openai(self) -> list[dict] | None:
        if not self.tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": t.parameters,
                        "required": list(t.parameters.keys()),
                    },
                },
            }
            for t in self.tools
        ]

    async def _invoke_openai(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
    ) -> str:
        client = self._get_openai_client()
        tools = self._format_tools_openai()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        iteration = 0
        max_iter = settings.llm_max_tool_iterations

        while True:
            iteration += 1

            response = await self._execute_openai_call(
                client=client,
                messages=messages,
                tools=tools,
                temperature=temperature,
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            if iteration > max_iter:
                raise ToolCallLoopExceededError(
                    f"Tool call loop exceeded {max_iter} iterations — likely infinite loop"
                )

            messages.append(msg)

            for tc in msg.tool_calls:
                try:
                    tool_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}
                result = await self._handle_tool_call(tc.function.name, tool_input)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })

    async def _execute_openai_call(self, *, client, messages, tools, temperature) -> Any:
        """Single OpenAI API call wrapped with per-provider rate limiting and circuit breaker."""

        async def _call():
            return await client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                temperature=temperature or settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                timeout=settings.llm_request_timeout,
            )

        return await _resilience.call(
            self.provider,
            lambda: async_retry(
                _call,
                max_retries=settings.llm_max_retries,
                base_delay=settings.llm_retry_base_delay,
                max_delay=settings.llm_retry_max_delay,
            ),
            rate_limit_rps=_PROVIDER_RPS.get(self.provider),
        )

    # ── Anthropic invocation ───────────────────────────────────────────────

    def _format_tools_anthropic(self) -> list[dict] | None:
        if not self.tools:
            return None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": {
                    "type": "object",
                    "properties": {k: v for k, v in t.parameters.items()},
                    "required": list(t.parameters.keys()),
                },
            }
            for t in self.tools
        ]

    async def _invoke_anthropic(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
    ) -> str:
        client = await self._get_anthropic_client()
        tools = self._format_tools_anthropic()
        messages: list[dict] = [{"role": "user", "content": user_message}]

        iteration = 0
        max_iter = settings.llm_max_tool_iterations

        while True:
            iteration += 1

            response = await self._execute_anthropic_call(
                client=client,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                temperature=temperature,
            )

            if response.stop_reason != "tool_use":
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                return "\n".join(text_blocks)

            if iteration > max_iter:
                raise ToolCallLoopExceededError(
                    f"Tool call loop exceeded {max_iter} iterations — likely infinite loop"
                )

            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "tool_use":
                    result = await self._handle_tool_call(block.name, block.input)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }],
                    })

    async def _invoke_anthropic_with_cache(
        self,
        system_prompt: str,
        stable_message: str,
        variable_message: str,
        temperature: float,
    ) -> str:
        """Anthropic invocation with explicit cache_control on the stable prefix.

        Places *stable_message* in its own user turn with a cache_control
        breakpoint so Anthropic caches it across calls.  *variable_message*
        goes in a second user turn and is never cached.
        """
        client = await self._get_anthropic_client()
        tools = self._format_tools_anthropic()
        messages: list[dict] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stable_message,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {"role": "user", "content": variable_message},
        ]

        iteration = 0
        max_iter = settings.llm_max_tool_iterations

        while True:
            iteration += 1

            response = await self._execute_anthropic_call(
                client=client,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                temperature=temperature,
            )

            if response.stop_reason != "tool_use":
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                return "\n".join(text_blocks)

            if iteration > max_iter:
                raise ToolCallLoopExceededError(
                    f"Tool call loop exceeded {max_iter} iterations — likely infinite loop"
                )

            # Follow-up turns are appended normally (assistant + tool_result).
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "tool_use":
                    result = await self._handle_tool_call(block.name, block.input)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }],
                    })

    async def _execute_anthropic_call(self, *, client, system_prompt, messages, tools, temperature) -> Any:
        """Single Anthropic API call wrapped with per-provider rate limiting and circuit breaker."""

        async def _call():
            return await client.messages.create(
                model=self.model,
                max_tokens=settings.llm_max_tokens,
                system=system_prompt,
                messages=messages,
                tools=tools,
                temperature=temperature or settings.llm_temperature,
            )

        return await _resilience.call(
            self.provider,
            lambda: async_retry(
                _call,
                max_retries=settings.llm_max_retries,
                base_delay=settings.llm_retry_base_delay,
                max_delay=settings.llm_retry_max_delay,
            ),
            rate_limit_rps=_PROVIDER_RPS.get(self.provider),
        )

    # ── Unified invoke ─────────────────────────────────────────────────────

    async def invoke(
        self,
        user_message: str,
        context: dict | None = None,
        temperature: float | None = None,
    ) -> str:
        """Invoke the LLM with full resilience: retry, backoff, rate‑limit, circuit‑breaker."""
        system_prompt = self.build_system_prompt(context or {})
        ts = _invoke_counter()

        if settings.log_prompts:
            _write_prompt_log(
                self.agent_name, ts, system_prompt, user_message, request_only=True,
            )

        try:
            if self.provider in ("deepseek", "openai"):
                result = await self._invoke_openai(
                    system_prompt, user_message, temperature or settings.llm_temperature
                )
            else:
                result = await self._invoke_anthropic(
                    system_prompt, user_message, temperature or settings.llm_temperature
                )

            if settings.log_prompts:
                _write_prompt_log(
                    self.agent_name, ts, system_prompt, user_message, response=result,
                )
            return result

        except CircuitBreakerOpenError:
            logger.error(
                "circuit breaker open — LLM provider may be down",
                provider=self.provider,
            )
            raise
        except ToolCallLoopExceededError:
            logger.error(
                "tool call loop exceeded max iterations",
                agent=self.agent_name,
            )
            raise

    async def invoke_with_cache(
        self,
        stable_message: str,
        variable_message: str,
        context: dict | None = None,
        temperature: float | None = None,
    ) -> str:
        """Invoke the LLM with a cacheable stable prefix and variable suffix.

        On Anthropic the *stable_message* is placed in its own user turn with
        a ``cache_control`` breakpoint, so subsequent calls with the same
        prefix enjoy a ~90% discount on those tokens.

        On OpenAI / DeepSeek the two messages are concatenated with the
        stable portion first, which works with those providers' automatic
        prefix caching.
        """
        system_prompt = self.build_system_prompt(context or {})
        ts = _invoke_counter()

        if settings.log_prompts:
            _write_prompt_log(
                self.agent_name,
                ts,
                system_prompt,
                f"{stable_message}\n\n---VARIABLE---\n\n{variable_message}",
                request_only=True,
            )

        try:
            if self.provider in ("deepseek", "openai"):
                combined = f"{stable_message}\n\n---\n\n{variable_message}"
                result = await self._invoke_openai(
                    system_prompt, combined, temperature or settings.llm_temperature
                )
            else:
                result = await self._invoke_anthropic_with_cache(
                    system_prompt,
                    stable_message,
                    variable_message,
                    temperature or settings.llm_temperature,
                )

            if settings.log_prompts:
                _write_prompt_log(
                    self.agent_name,
                    ts,
                    system_prompt,
                    f"{stable_message}\n\n---VARIABLE---\n\n{variable_message}",
                    response=result,
                )
            return result

        except CircuitBreakerOpenError:
            logger.error(
                "circuit breaker open — LLM provider may be down",
                provider=self.provider,
            )
            raise
        except ToolCallLoopExceededError:
            logger.error(
                "tool call loop exceeded max iterations",
                agent=self.agent_name,
            )
            raise

    # ── Tool dispatch ──────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> Any:
        for tool in self.tools:
            if tool.name == tool_name and tool.handler:
                try:
                    if hasattr(tool.handler, "__call__"):
                        return await tool.handler(**tool_input)
                    else:
                        return tool.handler(**tool_input)
                except Exception as e:
                    logger.error("tool call failed", tool=tool_name, error=str(e))
                    return {"error": str(e)}


# ── Prompt logging helpers ─────────────────────────────────────────────────


def _invoke_counter() -> str:
    """Monotonic counter for unique prompt log filenames."""
    import time
    return f"{int(time.monotonic() * 1000):012d}"


def _write_prompt_log(
    agent: str,
    ts: str,
    system_prompt: str,
    user_message: str,
    *,
    request_only: bool = False,
    response: str = "",
) -> None:
    """Write an LLM prompt + response to disk for debugging.

    Enabled via ``LLMTUNER_LOG_PROMPTS=true``.  Files land in
    ``{data_dir}/prompts/{agent}_{ts}_{direction}.txt``.
    """
    from pathlib import Path
    from src.config import settings

    out = settings.data_dir / "prompts"
    out.mkdir(parents=True, exist_ok=True)

    if request_only:
        path = out / f"{agent}_{ts}_request.txt"
        path.write_text(
            f"# SYSTEM PROMPT\n\n{system_prompt}\n\n"
            f"# USER MESSAGE\n\n{user_message}",
            encoding="utf-8",
        )
    else:
        path = out / f"{agent}_{ts}_response.txt"
        path.write_text(response, encoding="utf-8")
        return {"error": f"Unknown tool: {tool_name}"}

    def get_system_prompt(self) -> str:
        return self.system_prompt_template
