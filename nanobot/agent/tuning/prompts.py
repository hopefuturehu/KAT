"""System prompt builders for tuning agents."""

from __future__ import annotations

from nanobot.utils.prompt_templates import render_template


def build_intake_prompt(workspace: str) -> str:
    return render_template(
        "agent/tuning_intake.md",
        workspace=workspace,
    )


def build_executor_prompt(workspace: str, requirements_summary: str) -> str:
    return render_template(
        "agent/tuning_executor.md",
        workspace=workspace,
        requirements_summary=requirements_summary,
    )
