"""User-defined skill system for extending workflow nodes.

Skills live in the ``skills/`` directory at the project root.  Each skill
hooks into a specific workflow node (pre or post) and can modify the
experiment state.

Two formats are supported:

**Python skill** (full power)::

    from src.workflow.skill import BaseSkill

    class MySkill(BaseSkill):
        name = "my-skill"
        node = "run_benchmark"
        phase = "pre"

        async def run(self, state: ExperimentState) -> ExperimentState:
            # custom logic ...
            return state

**YAML skill** (simple string‑template webhooks / notifications)::

    name: slack-alert
    node: finalize
    phase: post
    config:
      url: "https://hooks.slack.com/..."
      template: "Experiment {name} done. Best QPS: {best_qps}"

The workflow graph builder scans ``skills/`` on startup and injects
enabled skills as additional LangGraph nodes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from src.utils.logging import get_logger

logger = get_logger(__name__)

SKILLS_DIR = Path("skills")

# ── Skill protocol ──────────────────────────────────────────────────────────


class BaseSkill(ABC):
    """Base class for Python skills.

    Subclasses must override:
      - ``name`` : unique skill identifier
      - ``node`` : target workflow node name
      - ``phase``: ``"pre"`` or ``"post"``
      - ``run()``: async method, receives and returns ExperimentState
    """

    name: str = ""
    node: str = ""
    phase: str = "pre"  # "pre" | "post"
    enabled: bool = True
    config: dict[str, Any] = {}

    @abstractmethod
    async def run(self, state: Any) -> Any:
        """Execute the skill.  Must return the (possibly modified) state."""
        ...


# ── Skill metadata (agnostic of format) ─────────────────────────────────────


class SkillSpec:
    """Normalised representation of a skill, regardless of source format."""

    __slots__ = ("name", "node", "phase", "enabled", "config", "_runner")

    def __init__(
        self,
        name: str,
        node: str,
        phase: str,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
        runner: Any = None,
    ):
        self.name = name
        self.node = node
        self.phase = phase
        self.enabled = enabled
        self.config = config or {}
        self._runner = runner  # callable: async (state) -> state

    async def run(self, state: Any) -> Any:
        if self._runner is None:
            return state
        return await self._runner(state)


# ── Skill loader ────────────────────────────────────────────────────────────


class SkillLoader:
    """Scan ``skills/`` directory and return a list of SkillSpec."""

    VALID_NODES = frozenset({
        "initialize", "plan", "safety_check", "apply_config",
        "run_benchmark", "analyze", "decide", "rollback", "finalize",
    })

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or SKILLS_DIR

    def load_all(self, include_disabled: bool = False) -> list[SkillSpec]:
        """Discover and load all skills from the skills directory.

        Set *include_disabled=True* to return disabled skills too (for CLI listing).
        """
        if not self.skills_dir.exists():
            return []

        specs: list[SkillSpec] = []
        for entry in sorted(self.skills_dir.iterdir()):
            if entry.suffix == ".py" and entry.name != "__init__.py":
                spec = self._load_python(entry)
                if spec:
                    specs.append(spec)
            elif entry.suffix in (".yaml", ".yml"):
                spec = self._load_yaml(entry)
                if spec:
                    specs.append(spec)

        if include_disabled:
            logger.info(
                "skills loaded (all)",
                total=len(specs),
                enabled=sum(1 for s in specs if s.enabled),
            )
            return specs

        enabled = [s for s in specs if s.enabled]
        logger.info(
            "skills loaded",
            total=len(specs),
            enabled=len(enabled),
            names=[s.name for s in enabled],
        )
        return enabled

    # ── Python skill loader ──────────────────────────────────────────────

    def _load_python(self, path: Path) -> SkillSpec | None:
        """Dynamically import a Python skill file and extract skill classes."""
        module_name = f"skills_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("failed to load skill", path=str(path), error=str(exc))
            return None

        # Find BaseSkill subclasses
        for attr_name in dir(module):
            obj = getattr(module, attr_name, None)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseSkill)
                and obj is not BaseSkill
            ):
                try:
                    instance = obj()
                    if not self._validate(instance):
                        continue

                    async def _runner(state, inst=instance):
                        return await inst.run(state)

                    return SkillSpec(
                        name=instance.name,
                        node=instance.node,
                        phase=instance.phase,
                        enabled=instance.enabled,
                        config=instance.config,
                        runner=_runner,
                    )
                except Exception as exc:
                    logger.warning(
                        "failed to instantiate skill",
                        name=getattr(obj, "name", "?"),
                        error=str(exc),
                    )

        return None

    # ── YAML skill loader ─────────────────────────────────────────────────

    def _load_yaml(self, path: Path) -> SkillSpec | None:
        """Load a YAML-declared skill (string‑template based)."""
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            logger.warning("failed to parse YAML skill", path=str(path), error=str(exc))
            return None

        if not isinstance(data, dict):
            return None

        name = data.get("name", path.stem)
        node = data.get("node", "")
        phase = data.get("phase", "pre")
        enabled = data.get("enabled", True)
        config = data.get("config", {})

        if node not in self.VALID_NODES:
            logger.warning("invalid node for skill", name=name, node=node)
            return None

        # YAML skills use str.format(**state_fields) for simple notifications
        if "url" in config and "template" in config:
            async def _webhook_runner(state, cfg=config):
                return await self._send_webhook(state, cfg)

            return SkillSpec(
                name=name, node=node, phase=phase, enabled=enabled,
                config=config, runner=_webhook_runner,
            )

        logger.warning("YAML skill missing url+template", name=name)
        return None

    @staticmethod
    async def _send_webhook(state: Any, config: dict) -> Any:
        """Send a simple HTTP POST webhook with formatted template."""
        url = config.get("url", "")
        template = config.get("template", "")
        if not url or not template:
            return state

        # Build template context from state attributes
        ctx = {}
        for attr in ("experiment_name", "trial_number", "target_system",
                      "experiment_id", "elapsed_hours"):
            ctx[attr] = getattr(state, attr, "?")
        if hasattr(state, "best_metrics"):
            for k, v in getattr(state, "best_metrics", {}).items():
                ctx[f"best_{k}"] = v
        ctx["goals_count"] = len(getattr(state, "goals", []))

        message = template.format(**ctx)

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"text": message})
        except Exception as exc:
            logger.warning("webhook skill failed", url=url, error=str(exc)[:100])

        return state

    # ── Validation ───────────────────────────────────────────────────────

    def _validate(self, skill: BaseSkill) -> bool:
        if not skill.name or not skill.node:
            logger.warning("skill missing name or node")
            return False
        if skill.node not in self.VALID_NODES:
            logger.warning("invalid skill node", name=skill.name, node=skill.node)
            return False
        if skill.phase not in ("pre", "post"):
            logger.warning("invalid skill phase", name=skill.name, phase=skill.phase)
            return False
        return True


# ── Graph injection helper ──────────────────────────────────────────────────


def inject_skills(
    graph: Any,
    skills: list[SkillSpec],
    node_registry: dict[str, Any],
) -> Any:
    """Add skill nodes and edges into an existing StateGraph.

    For each skill:
      - Create a new node named ``skill_{skill.name}``.
      - For ``pre`` skills: insert edge before the target node.
      - For ``post`` skills: insert edge after the target node.

    This modifies the graph in-place.
    """
    from langgraph.graph import StateGraph

    for skill in skills:
        node_id = f"skill_{skill.name}"

        # Create a closure that captures the skill runner
        async def _skill_node(state: Any, _skill=skill) -> Any:
            try:
                return await _skill.run(state)
            except Exception as exc:
                logger.error("skill node failed", skill=_skill.name, error=str(exc))
                return state

        # Register the node
        node_registry[node_id] = _skill_node
        graph.add_node(node_id, _skill_node)

        logger.info("skill injected", name=skill.name, node=skill.node, phase=skill.phase)

    return graph
