"""Convergence detection utilities."""

import numpy as np
from typing import Sequence


class ConvergenceDetector:
    def __init__(
        self,
        window: int = 5,
        threshold_pct: float = 2.0,
    ):
        self.window = window
        self.threshold_pct = threshold_pct

    def is_converged(self, improvement_history: list[float]) -> bool:
        """Check if improvements have plateaued below threshold."""
        if len(improvement_history) < self.window:
            return False

        recent = improvement_history[-self.window:]
        return all(abs(imp) < self.threshold_pct for imp in recent)

    def estimate_convergence_trial(self, history: list[float]) -> int | None:
        """Estimate at which trial convergence was reached."""
        for i in range(self.window, len(history) + 1):
            window = history[i - self.window:i]
            if all(abs(imp) < self.threshold_pct for imp in window):
                return i - self.window
        return None
