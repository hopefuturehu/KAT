import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LLM_TUNER_ROOT = REPO_ROOT / "_llm_tuner"
if str(LLM_TUNER_ROOT) not in sys.path:
    sys.path.insert(0, str(LLM_TUNER_ROOT))

if "structlog" not in sys.modules:
    class _DummyLogger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

        def error(self, *_args, **_kwargs):
            return None

        def debug(self, *_args, **_kwargs):
            return None

    structlog_stub = types.ModuleType("structlog")
    structlog_stub.get_logger = lambda *args, **kwargs: _DummyLogger()
    structlog_stub.configure = lambda *args, **kwargs: None
    structlog_stub.processors = types.SimpleNamespace(
        TimeStamper=lambda *args, **kwargs: None,
        JSONRenderer=lambda *args, **kwargs: None,
        format_exc_info=lambda *args, **kwargs: None,
    )
    structlog_stub.dev = types.SimpleNamespace(
        ConsoleRenderer=lambda *args, **kwargs: None,
        set_exc_info=lambda *args, **kwargs: None,
    )
    structlog_stub.stdlib = types.SimpleNamespace(
        add_log_level=lambda *args, **kwargs: None,
        add_logger_name=lambda *args, **kwargs: None,
        BoundLogger=_DummyLogger,
        LoggerFactory=lambda *args, **kwargs: None,
    )
    sys.modules["structlog"] = structlog_stub

from src.workflow.nodes.plan import _invoke_advisor
from src.workflow.state import ExperimentPhase, ExperimentState, GoalSpec


def test_state_payloads_are_typed_but_keep_mapping_compatibility() -> None:
    state = ExperimentState(
        tuning_proposal={
            "changes": [
                {
                    "parameter": "maxmemory",
                    "proposed_value": "1gb",
                    "rationale": "increase memory",
                }
            ],
            "_source": "llm",
            "overall_strategy": "memory tuning",
        },
        safety_verdict={
            "verdict": "APPROVE_WITH_MODIFICATIONS",
            "suggested_modifications": [
                {"parameter": "maxmemory", "suggested_value": "768mb"}
            ],
        },
        analysis_result={
            "trend": "up",
            "likely_bottleneck": "memory",
        },
        orchestrator_decision={
            "action": "ROLLBACK",
            "reasoning": "health check failed",
        },
        advisor_recommendations={
            "summary": "consider storage tuning",
            "recommendations": [
                {"category": "disk", "recommendation": "move to faster storage"}
            ],
        },
    )

    assert state.tuning_proposal.source == "llm"
    assert state.tuning_proposal.get("_source") == "llm"
    assert state.tuning_proposal["changes"][0]["parameter"] == "maxmemory"
    assert state.safety_verdict["suggested_modifications"][0]["suggested_value"] == "768mb"
    assert state.analysis_result.likely_bottleneck == "memory"
    assert state.orchestrator_decision["action"] == "ROLLBACK"
    assert state.advisor_recommendations["recommendations"][0]["category"] == "disk"


def test_state_helpers_update_best_metrics_and_connection_fallbacks() -> None:
    state = ExperimentState(
        goals=[GoalSpec(metric="qps", operator=">=", value=100.0)],
        current_config={"maxmemory": "1gb"},
        target_host="",
        target_port="",
        target_credentials="",
        redis_host="10.0.0.7",
        redis_port="6380",
        redis_password="secret",
    )
    state.begin_trial({"maxmemory": "1gb"})
    assert state.current_trial is not None
    state.current_trial.metrics = {"qps": 120.0}

    state.record_analysis(
        {
            "trend": "up",
            "improvement_pct": 100.0,
            "likely_bottleneck": "memory",
            "recommended_focus": "cache sizing",
        }
    )

    assert state.best_metrics == {"qps": 120.0}
    assert state.best_config == {"maxmemory": "1gb"}
    assert state.best_trial_number == 1
    assert state.improvement_history == [100.0]
    assert state.current_trial.analysis.recommended_focus == "cache sizing"
    assert state.connection_host == "10.0.0.7"
    assert state.connection_port == "6380"
    assert state.connection_credentials == "secret"


def test_advisor_phase_stays_aligned_with_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdvisorAgent:
        def __init__(self, kb_retriever=None):
            self.kb_retriever = kb_retriever

        async def recommend(self, **kwargs):
            _ = kwargs
            return {
                "summary": "No more useful config changes found.",
                "recommendations": [],
            }

    monkeypatch.setattr("src.workflow.nodes.plan.AdvisorAgent", FakeAdvisorAgent)

    state = ExperimentState(
        target_system="redis",
        goals=[GoalSpec(metric="qps", operator=">=", value=100.0)],
        best_metrics={"qps": 90.0},
        current_config={"maxmemory": "1gb"},
        phase=ExperimentPhase.PLANNING,
    )

    result = asyncio.run(_invoke_advisor(state))

    assert result.phase == ExperimentPhase.PLANNING
    assert result.advisor_recommendations.summary == "No more useful config changes found."
