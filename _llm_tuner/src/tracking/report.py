"""Report generation for experiment results."""

import json
from pathlib import Path
from datetime import datetime


class ReportGenerator:
    def __init__(self, output_dir: str = "data/reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_markdown(self, state) -> str:
        """Generate a comprehensive markdown report from experiment state."""

        lines = [
            f"# Experiment Report: {state.experiment_name}",
            f"",
            f"**Generated**: {datetime.utcnow().isoformat()}",
            f"",
            f"## Summary",
            f"",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Target System | {state.target_system} {state.target_version} |",
            f"| Status | {state.phase.value} |",
            f"| Trials | {state.trial_number} / {state.max_trials} |",
            f"| Duration | {state.elapsed_hours:.1f} hours |",
            f"| Convergence Window | {state.convergence_window} trials |",
            f"| Improvement Threshold | {state.improvement_threshold_pct}% |",
            f"",
            f"## Goals",
            f"",
            f"| Metric | Target | Achieved | Status |",
            f"|--------|--------|----------|--------|",
        ]

        for goal in state.goals:
            current = state.best_metrics.get(goal.metric, 0)
            met = state.goal_met(goal.metric, current)
            icon = "✓" if met else "✗"
            lines.append(
                f"| {goal.metric} | {goal.operator} {goal.value} | {current} | {icon} |"
            )

        lines.extend([
            f"",
            f"## Optimization Timeline",
            f"",
            f"| Trial | Improvement | Changes | Benchmark |",
            f"|-------|-------------|---------|-----------|",
        ])

        for trial in state.trial_history:
            changes_str = ", ".join(
                c.get("parameter", "") for c in trial.parameter_changes
            ) or "none"
            bm_summary = ", ".join(
                f"{op.get('name', '')}: {op.get('value', 0):.0f}"
                for op in trial.benchmark_results[:2]
            ) or "none"
            lines.append(
                f"| {trial.trial_number} | {trial.improvement_pct:+.1f}% | {changes_str[:60]} | {bm_summary[:60]} |"
            )

        lines.extend([
            f"",
            f"## Best Configuration",
            f"",
            f"```",
        ])

        if state.best_config:
            from src.parameters.manager import ParameterManager
            pm = ParameterManager(state.target_system)
            lines.append(pm.serialize_config(state.best_config).strip())
        else:
            lines.append("(no best config recorded)")

        lines.append("```")

        # Advisor recommendations
        if state.advisor_recommendations:
            lines.extend([
                f"",
                f"## Alternative Recommendations",
                f"",
                state.advisor_recommendations.get("summary", ""),
                f"",
            ])
            for i, rec in enumerate(state.advisor_recommendations.get("recommendations", []), 1):
                lines.extend([
                    f"### {i}. [{rec.get('category', 'General')}] {rec.get('recommendation', '')}",
                    f"- **Expected Benefit**: {rec.get('expected_benefit', '')}",
                    f"- **Effort**: {rec.get('effort', 'unknown')} | **Risk**: {rec.get('risk', 'unknown')}",
                    f"",
                ])

        # Warnings
        if state.safety_warnings:
            lines.extend([
                f"## Warnings",
                f"",
            ])
            for w in state.safety_warnings:
                lines.append(f"- {w}")

        report = "\n".join(lines)
        filepath = self.output_dir / f"{state.experiment_name}-report.md"
        filepath.write_text(report)
        return report

    def generate_json(self, state) -> str:
        """Generate a JSON report."""
        report = {
            "experiment_name": state.experiment_name,
            "target_system": state.target_system,
            "target_version": state.target_version,
            "status": state.phase.value,
            "trials_completed": state.trial_number,
            "duration_hours": state.elapsed_hours,
            "goals": [g.model_dump() for g in state.goals],
            "best_metrics": state.best_metrics,
            "best_config": state.best_config,
            "trial_history": [
                {
                    "trial": t.trial_number,
                    "improvement_pct": t.improvement_pct,
                    "metrics": t.metrics,
                    "parameter_changes": t.parameter_changes,
                }
                for t in state.trial_history
            ],
            "advisor_recommendations": state.advisor_recommendations,
            "safety_warnings": state.safety_warnings,
        }

        json_str = json.dumps(report, indent=2, default=str)
        filepath = self.output_dir / f"{state.experiment_name}-report.json"
        filepath.write_text(json_str)
        return json_str
