"""Graceful interrupt handling for experiments.

On Ctrl+C:
  Step 1. Save core experiment data to ``data/interrupt_snapshot.json``
           (trial_history, best_config, best_metrics, ...).
  Step 2. Invoke LLM to generate a markdown progress summary.
           If the user presses Ctrl+C *again* during this step, skip
           the LLM call and just write a minimal summary from saved data.
  Step 3. Clean up and exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

SNAPSHOT_PATH = Path("data/interrupt_snapshot.json")

# Set to True by the signal handler on first Ctrl+C.
_interrupt_requested: bool = False

# Set to True by the signal handler on second Ctrl+C (during cleanup).
_force_exit: bool = False


def _handle_sigint(_signum: int, _frame: Any) -> None:
    global _interrupt_requested, _force_exit
    if _interrupt_requested:
        # Second Ctrl+C — abort any slow LLM call
        _force_exit = True
        print("\nInterrupted again — skipping LLM summary.")
        return
    _interrupt_requested = True
    print("\nInterrupt received — finishing current step, then saving progress...")


def install_handler() -> None:
    """Register the SIGINT handler. Call once before running the experiment."""
    signal.signal(signal.SIGINT, _handle_sigint)


def is_interrupted() -> bool:
    """Check whether the user has requested interruption."""
    return _interrupt_requested


def is_force_exit() -> bool:
    """Check whether the user has requested forced exit (second Ctrl+C)."""
    return _force_exit


# ── Snapshot serialisation ───────────────────────────────────────────────────


def _trial_to_dict(trial: Any) -> dict:
    """Convert a TrialResult to a plain dict for JSON serialisation."""
    return {
        "trial_number": trial.trial_number,
        "config": getattr(trial, "config", {}),
        "metrics": getattr(trial, "metrics", {}),
        "benchmark_results": getattr(trial, "benchmark_results", []),
        "parameter_changes": getattr(trial, "parameter_changes", []),
        "improvement_pct": getattr(trial, "improvement_pct", 0.0),
        "status": getattr(trial, "status", "unknown"),
    }


def save_snapshot(state: Any, interrupted_at: datetime | None = None) -> Path:
    """Persist core experiment data to an atomic JSON snapshot file.

    Returns the path to the saved file.
    """
    snapshot = {
        "version": 1,
        "interrupted_at": (interrupted_at or datetime.utcnow()).isoformat(),
        "experiment_name": getattr(state, "experiment_name", ""),
        "experiment_id": getattr(state, "experiment_id", ""),
        "target_system": getattr(state, "target_system", ""),
        "target_version": getattr(state, "target_version", ""),
        "goals": [g.model_dump() for g in getattr(state, "goals", [])],
        "trial_number": getattr(state, "trial_number", 0),
        "trial_history": [
            _trial_to_dict(t) for t in getattr(state, "trial_history", [])
        ],
        "best_config": getattr(state, "best_config", {}),
        "best_metrics": getattr(state, "best_metrics", {}),
        "best_trial_number": getattr(state, "best_trial_number", 0),
        "current_config": getattr(state, "current_config", {}),
        "baseline_config": getattr(state, "baseline_config", {}),
        "improvement_history": getattr(state, "improvement_history", []),
        "rollback_history": getattr(state, "rollback_history", []),
        "consecutive_rollbacks": getattr(state, "consecutive_rollbacks", 0),
        "container_id": getattr(state, "container_id", ""),
        "elapsed_hours": getattr(state, "elapsed_hours", 0.0),
        "hardware_spec": getattr(state, "hardware_spec", {}),
        "errors": getattr(state, "errors", [])[-5:],  # keep last 5
    }

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: tmp file then rename
    tmp_path = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2, default=str, ensure_ascii=False))
    os.replace(str(tmp_path), str(SNAPSHOT_PATH))

    logger.info("snapshot saved", path=str(SNAPSHOT_PATH), trials=len(snapshot["trial_history"]))
    return SNAPSHOT_PATH


def load_snapshot() -> dict | None:
    """Load a previously saved interrupt snapshot, or None if none exists."""
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("corrupted snapshot file, ignoring")
        return None


def has_snapshot() -> bool:
    """Check whether an interrupt snapshot exists on disk."""
    return SNAPSHOT_PATH.exists()


# ── LLM progress summary ────────────────────────────────────────────────────


_LLM_SUMMARY_PROMPT = (
    "You are a performance engineering assistant. "
    "The user just interrupted an autonomous parameter tuning experiment. "
    "Write a short, actionable markdown summary of the current progress.\n\n"
    "## Experiment State\n\n"
    "**Name**: {name}\n"
    "**Target**: {target_system} {target_version}\n"
    "**Trials completed**: {trials_done} / {max_trials}\n"
    "**Elapsed**: {elapsed:.1f} hours\n"
    "**Status**: {status}\n\n"
    "### Goals\n{goals}\n\n"
    "### Best Results\n{best_metrics}\n\n"
    "### Improvement Timeline\n{timeline}\n\n"
    "### Rollbacks\n{rollbacks}\n\n"
    "### Recent Errors\n{errors}\n\n"
    "## Your Task\n\n"
    "Write a concise markdown report with these sections:\n"
    "1. **Current Status** — one sentence summary\n"
    "2. **Best Configuration Found** — key parameter changes that worked\n"
    "3. **Progress vs Goals** — table of goal vs achieved\n"
    "4. **Recommendations** — what to try next if resuming, or what's left to tune\n"
    "5. **How to Resume** — suggest the command to resume\n\n"
    "Keep it under 400 words. Use markdown formatting."
)


def build_summary_context(snapshot: dict) -> str:
    """Build the LLM prompt context from a snapshot dict."""
    goals_text = "\n".join(
        f"- {g['metric']} {g['operator']} {g['value']} (weight: {g.get('weight', 1.0)})"
        for g in snapshot.get("goals", [])
    )

    best = snapshot.get("best_metrics", {})
    best_text = "\n".join(f"- {k}: {v}" for k, v in best.items()) if best else "- (no data yet)"

    timeline_lines = []
    for t in snapshot.get("trial_history", []):
        changes = len(t.get("parameter_changes", []))
        imp = t.get("improvement_pct", 0)
        timeline_lines.append(
            f"- Trial {t['trial_number']}: {imp:+.1f}% improvement, "
            f"{changes} changes, status={t.get('status', '?')}"
        )
    timeline_text = "\n".join(timeline_lines) if timeline_lines else "- No trials completed yet"

    rollbacks = snapshot.get("rollback_history", [])
    rb_text = "\n".join(
        f"- Trial {r.get('trial', '?')}: {r.get('reason', '?')}"
        for r in rollbacks
    ) if rollbacks else "None"

    errors = snapshot.get("errors", [])
    errors_text = "\n".join(f"- {e}" for e in errors) if errors else "None"

    return _LLM_SUMMARY_PROMPT.format(
        name=snapshot.get("experiment_name", "unknown"),
        target_system=snapshot.get("target_system", "unknown"),
        target_version=snapshot.get("target_version", ""),
        trials_done=snapshot.get("trial_number", 0),
        max_trials=snapshot.get("trial_number", 0),
        elapsed=snapshot.get("elapsed_hours", 0.0),
        status="interrupted by user",
        goals=goals_text,
        best_metrics=best_text,
        timeline=timeline_text,
        rollbacks=rb_text,
        errors=errors_text,
    )


async def generate_llm_summary(snapshot: dict) -> str:
    """Use LLM to produce a markdown progress summary.

    Checks ``is_force_exit()`` periodically so the user can cancel this step
    with a second Ctrl+C.
    """
    from src.config import settings

    prompt = build_summary_context(snapshot)

    # Use the unified invoke path via a lightweight agent
    from src.agents.base import BaseAgent

    class SummaryAgent(BaseAgent):
        agent_name = "summary"

    agent = SummaryAgent()
    agent.system_prompt_template = (
        "You write concise, informative markdown summaries for performance engineering experiments."
    )

    try:
        # Run with a shorter timeout since this is a "best effort" feature
        result = await asyncio.wait_for(
            agent.invoke(prompt, context={}, temperature=0.3),
            timeout=25.0,
        )
        return result.strip() if result else ""

    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("LLM summary generation failed", error=str(exc)[:100])
        return ""


def write_minimal_summary(snapshot: dict, filepath: Path | None = None) -> Path:
    """Generate a minimal markdown summary purely from snapshot data (no LLM).

    Used when the LLM call is skipped (timeout, API error, or second Ctrl+C).
    """
    name = snapshot.get("experiment_name", "unknown")
    target = f"{snapshot.get('target_system', '?')} {snapshot.get('target_version', '')}"
    trials = snapshot.get("trial_number", 0)
    elapsed = snapshot.get("elapsed_hours", 0.0)
    best = snapshot.get("best_metrics", {})
    best_config = snapshot.get("best_config", {})

    lines = [
        f"# Experiment Interrupted: {name}",
        "",
        f"**Target**: {target}  ",
        f"**Trials completed**: {trials}  ",
        f"**Elapsed**: {elapsed:.1f} hours  ",
        f"**Interrupted at**: {snapshot.get('interrupted_at', 'unknown')}  ",
        "",
        "## Best Results",
        "",
    ]

    if best:
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for k, v in best.items():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("(no results recorded yet)")

    lines.extend(["", "## Best Configuration", "", "```"])
    if best_config:
        for k, v in best_config.items():
            lines.append(f"{k} = {v}")
    else:
        lines.append("(no config recorded)")
    lines.append("```")

    lines.extend(["", "## Trial Timeline", ""])
    for t in snapshot.get("trial_history", []):
        imp = t.get("improvement_pct", 0)
        changes = t.get("parameter_changes", [])
        params = ", ".join(c.get("parameter", "?") for c in changes[:3])
        lines.append(f"- Trial {t['trial_number']}: {imp:+.1f}% ({params or 'no changes'})")

    lines.extend(["", "## Resume", "", "```bash"])
    lines.append(f"python -m src.main run <config.yaml> --resume")
    lines.append("```")

    output_path = filepath or Path("data/interrupt_summary.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))

    return output_path
