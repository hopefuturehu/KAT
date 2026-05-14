import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.agent.tuning.executor import (
    _build_experiment_state,
    _validate_execution_requirements,
)
from nanobot.agent.tuning.intake import _requirements_complete
from nanobot.agent.tuning.router import TuningIntentRouter
from nanobot.agent.tuning.schema import TuningGoal, TuningRequirements
from nanobot.bus.queue import MessageBus


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

from src.parameters.schema import ParameterCategory, ParameterDefinition, ParameterRisk
from src.workflow.nodes.plan import plan_changes
from src.workflow.nodes.safety_check import safety_gate
from src.workflow.state import ExperimentState, GoalSpec


def _base_requirements() -> TuningRequirements:
    return TuningRequirements(
        target_system="mysql",
        target_version="8.0",
        goals=[TuningGoal(metric="qps", operator=">=", value=1000.0)],
        host="127.0.0.1",
        port="3306",
        password="",
        config_file="/tmp/my.cnf",
        run_command="sysbench oltp_read_write --time={duration} run",
    )


def test_mysql_tuning_request_is_routed(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    router = TuningIntentRouter(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
    )
    router.manager.handle_tune_request = AsyncMock(return_value="mysql tuning routed")

    result = asyncio.run(
        router.route_message(
            message="请帮我调优 mysql 延迟",
            session_key="cli:direct",
            origin_channel="cli",
            origin_chat_id="direct",
        )
    )

    assert result == "mysql tuning routed"
    router.manager.handle_tune_request.assert_awaited_once()


def test_requirements_complete_accepts_run_command_or_profile() -> None:
    req = _base_requirements()
    assert _requirements_complete(req) is True

    req.run_command = ""
    req.benchmark_profile_path = "/tmp/mysql-sysbench.yaml"
    assert _requirements_complete(req) is True


def test_requirements_complete_rejects_missing_benchmark_spec() -> None:
    req = _base_requirements()
    req.run_command = ""
    req.benchmark_profile_path = ""

    assert _requirements_complete(req) is False
    assert _validate_execution_requirements(req) == (
        "A tuning run needs either a benchmark profile path or a run command. "
        "Please provide one of them before execution."
    )


def test_execution_state_includes_restart_and_risk_constraints() -> None:
    req = _base_requirements()
    req.allow_restart = True
    req.max_risk_level = "high"

    state = asyncio.run(_build_experiment_state(req))

    assert state.allow_restart is True
    assert state.max_risk_level == "high"


def test_plan_filters_restart_and_high_risk_params_and_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.parameters.manager as manager_module

    captured: dict[str, object] = {}

    class FakeParameterManager:
        def __init__(self, target_system: str):
            assert target_system == "redis"

        def get_tunable_parameters(self, _include_categories=None, exclude_parameters=None, max_risk=ParameterRisk.HIGH):
            captured["exclude_parameters"] = exclude_parameters
            captured["max_risk"] = max_risk
            params = [
                ParameterDefinition(
                    name="save",
                    category=ParameterCategory.PERSISTENCE,
                    description="persist snapshots",
                    default_value="3600 1",
                    type="string",
                    restart_required=False,
                    risk=ParameterRisk.LOW,
                    depends_on=["appendonly"],
                    conflicts_with=["appendfsync"],
                ),
                ParameterDefinition(
                    name="appendfsync",
                    category=ParameterCategory.PERSISTENCE,
                    description="restart-required param",
                    default_value="everysec",
                    type="enum",
                    enum_values=["always", "everysec", "no"],
                    restart_required=True,
                    risk=ParameterRisk.LOW,
                ),
                ParameterDefinition(
                    name="io-threads",
                    category=ParameterCategory.IO,
                    description="high-risk param",
                    default_value="1",
                    type="integer",
                    min_value="1",
                    max_value="16",
                    restart_required=False,
                    risk=ParameterRisk.HIGH,
                ),
            ]
            risk_order = {
                ParameterRisk.LOW: 0,
                ParameterRisk.MEDIUM: 1,
                ParameterRisk.HIGH: 2,
                ParameterRisk.CRITICAL: 3,
            }
            return [p for p in params if risk_order[p.risk] <= risk_order[max_risk]]

    class FakeHybridTuner:
        def __init__(self, target_system: str, kb_retriever=None, model: str | None = None):
            assert target_system == "redis"

        async def propose_or_skip(self, **kwargs):
            captured["planner_state"] = kwargs["state"]
            captured["tunable_params"] = kwargs["tunable_params"]
            return {"changes": [], "overall_strategy": "noop", "_source": "llm"}

    monkeypatch.setattr(manager_module, "ParameterManager", FakeParameterManager)
    fake_hybrid_module = types.ModuleType("src.optimization.hybrid_tuner")
    fake_hybrid_module.HybridTuner = FakeHybridTuner
    monkeypatch.setitem(sys.modules, "src.optimization.hybrid_tuner", fake_hybrid_module)

    state = ExperimentState(
        target_system="redis",
        goals=[GoalSpec(metric="qps", operator=">=", value=1.0)],
        allow_restart=False,
        max_risk_level="medium",
        blocklist=["blocked-param"],
        current_config={},
        baseline_config={},
    )

    result = asyncio.run(plan_changes(state))
    tunable_params = captured["tunable_params"]

    assert captured["exclude_parameters"] == ["blocked-param"]
    assert captured["max_risk"] == ParameterRisk.MEDIUM
    assert captured["planner_state"]["allow_restart"] is False
    assert captured["planner_state"]["max_risk_level"] == "medium"
    assert [p["name"] for p in tunable_params] == ["save"]
    assert tunable_params[0]["depends_on"] == ["appendonly"]
    assert tunable_params[0]["conflicts_with"] == ["appendfsync"]
    assert result.tunable_parameters == tunable_params


def test_safety_gate_rejects_restart_when_restarts_not_allowed() -> None:
    state = ExperimentState(
        target_system="redis",
        allow_restart=False,
        max_restart_changes=1,
        max_risk_level="medium",
        current_config={"appendfsync": "everysec"},
        tuning_proposal={
            "changes": [
                {"parameter": "appendfsync", "proposed_value": "always"},
            ]
        },
        tunable_parameters=[
            {
                "name": "appendfsync",
                "category": "persistence",
                "risk": "low",
                "restart_required": True,
                "type": "enum",
                "enum_values": ["always", "everysec", "no"],
                "depends_on": [],
                "conflicts_with": [],
            }
        ],
    )

    result = asyncio.run(safety_gate(state))

    assert result.safety_verdict["verdict"] == "REJECT"
    assert "allow_restart is false" in result.safety_verdict["warnings"][0]


def test_safety_gate_rejects_changes_above_max_risk_level() -> None:
    state = ExperimentState(
        target_system="redis",
        allow_restart=True,
        max_restart_changes=1,
        max_risk_level="medium",
        current_config={"io-threads": "1"},
        tuning_proposal={
            "changes": [
                {"parameter": "io-threads", "proposed_value": "4"},
            ]
        },
        tunable_parameters=[
            {
                "name": "io-threads",
                "category": "io",
                "risk": "high",
                "restart_required": False,
                "type": "integer",
                "min": "1",
                "max": "16",
                "depends_on": [],
                "conflicts_with": [],
            }
        ],
    )

    result = asyncio.run(safety_gate(state))

    assert result.safety_verdict["verdict"] == "REJECT"
    assert "exceeds max risk level" in result.safety_verdict["warnings"][0]
