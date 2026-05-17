import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.agent.tuning.executor import (
    _check_dependencies,
    _configure_tuner_llm,
    _build_experiment_state,
    _validate_execution_requirements,
    run_execution,
)
from nanobot.agent.tuning.intake import _requirements_complete
from nanobot.agent.tuning.manager import TuningSessionManager
from nanobot.agent.tuning.router import TuningIntentRouter
from nanobot.agent.tuning.schema import TuningGoal, TuningPhase, TuningRequirements, TuningSession
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
from src.agents.analyzer_agent import AnalyzerAgent
from src.agents.safety_agent import SafetyAgent
from src.agents.tuner_agent import TunerAgent
from src.workflow.nodes.decide import make_decision
from src.config import settings as tuner_settings
from src.workflow.nodes.plan import plan_changes
from src.workflow.nodes.safety_check import safety_gate
from src.workflow.state import ExperimentPhase, ExperimentState, GoalSpec


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
        "A tuning run needs either an existing benchmark profile YAML or a run command. "
        "Please provide one of them before execution."
    )


def test_execution_state_includes_restart_and_risk_constraints() -> None:
    req = _base_requirements()
    req.allow_restart = True
    req.max_risk_level = "high"

    state = asyncio.run(_build_experiment_state(req))

    assert state.allow_restart is True
    assert state.max_risk_level == "high"


def test_tuner_llm_configuration_is_restored_after_override() -> None:
    provider = MagicMock(api_key="secret", api_base="https://example.com/v1")
    original = (
        tuner_settings.deepseek_api_key,
        tuner_settings.deepseek_api_base,
        tuner_settings.llm_model,
        tuner_settings.llm_provider,
    )

    with _configure_tuner_llm(provider, "custom-model"):
        assert tuner_settings.deepseek_api_key == "secret"
        assert tuner_settings.deepseek_api_base == "https://example.com/v1"
        assert tuner_settings.llm_model == "custom-model"
        assert tuner_settings.llm_provider == "deepseek"

    assert (
        tuner_settings.deepseek_api_key,
        tuner_settings.deepseek_api_base,
        tuner_settings.llm_model,
        tuner_settings.llm_provider,
    ) == original


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


def test_tuner_agent_uses_static_system_prompt_and_compact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_invoke(self, user_message, context=None, temperature=None):
        captured["user_message"] = user_message
        captured["context"] = context
        return '{"changes":[],"overall_strategy":"noop"}'

    class _Entry:
        def __init__(self, title: str, content: str):
            self.category = "memory"
            self.title = title
            self.content = content

    class _KB:
        async def query(self, query, system="", n_results=5):
            _ = (query, system, n_results)
            return [_Entry("doc-1", "x" * 600)]

    monkeypatch.setattr("src.agents.base.BaseAgent.invoke", fake_invoke)
    agent = TunerAgent(kb_retriever=_KB())

    tunable_params = [
        {
            "name": f"param_{idx}",
            "category": "memory" if idx % 2 == 0 else "io",
            "description": "cache tuning parameter" if idx % 3 == 0 else "other",
            "notes": "",
            "restart_required": False,
            "risk": "low",
        }
        for idx in range(40)
    ]
    result = asyncio.run(
        agent.propose_changes(
            state={
                "target_system": "redis",
                "target_version": "7.2",
                "goals": [{"metric": "qps", "operator": ">=", "value": 1000}],
                "max_changes_per_trial": 4,
                "max_restart_changes": 1,
                "blocklist": ["param_2"],
            },
            analysis={"recommended_focus": "cache", "likely_bottleneck": "memory"},
            current_config={f"param_{idx}": str(idx) for idx in range(10)},
            baseline_config={f"param_{idx}": "0" for idx in range(10)},
            tunable_params=tunable_params,
            recent_changes=[{"parameter": "param_39", "old_value": "1", "new_value": "2"}],
        )
    )

    assert result["overall_strategy"] == "noop"
    assert captured["context"] == {}
    payload = json.loads(str(captured["user_message"]).split("INPUT_JSON:\n", 1)[1])
    assert len(payload["candidate_parameters"]) <= 24
    assert len(payload["knowledge_base_context"][0]["excerpt"]) < 230
    assert "current_config_subset" in payload
    assert "{{" not in agent.build_system_prompt({})


def test_safety_gate_short_circuits_simple_low_risk_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_validate(self, **kwargs):
        raise AssertionError("LLM safety validation should be skipped")

    monkeypatch.setattr(SafetyAgent, "validate", unexpected_validate)
    state = ExperimentState(
        target_system="redis",
        allow_restart=True,
        max_restart_changes=1,
        max_risk_level="medium",
        current_config={"tcp-keepalive": "300"},
        tuning_proposal={
            "changes": [
                {"parameter": "tcp-keepalive", "proposed_value": "120"},
            ]
        },
        tunable_parameters=[
            {
                "name": "tcp-keepalive",
                "category": "network",
                "risk": "low",
                "restart_required": False,
                "type": "integer",
                "min": "1",
                "max": "600",
                "depends_on": [],
                "conflicts_with": [],
            }
        ],
    )

    result = asyncio.run(safety_gate(state))

    assert result.safety_verdict["verdict"] == "APPROVE"
    assert "static low-risk guardrails" in result.safety_verdict["notes"]


def test_safety_gate_passes_only_relevant_metadata_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_validate(self, **kwargs):
        captured["kwargs"] = kwargs
        return {"verdict": "APPROVE", "overall_risk_level": "medium", "warnings": []}

    monkeypatch.setattr(SafetyAgent, "validate", fake_validate)
    state = ExperimentState(
        target_system="redis",
        allow_restart=True,
        max_restart_changes=1,
        max_risk_level="medium",
        current_config={
            "save": "3600 1",
            "appendonly": "no",
            "unrelated": "value",
        },
        tuning_proposal={
            "changes": [
                {"parameter": "save", "proposed_value": "900 1"},
            ]
        },
        tunable_parameters=[
            {
                "name": "save",
                "category": "persistence",
                "risk": "low",
                "restart_required": False,
                "type": "string",
                "depends_on": ["appendonly"],
                "conflicts_with": [],
            },
            {
                "name": "appendonly",
                "category": "persistence",
                "risk": "low",
                "restart_required": False,
                "type": "boolean",
                "depends_on": [],
                "conflicts_with": [],
            },
            {
                "name": "unrelated",
                "category": "general",
                "risk": "low",
                "restart_required": False,
                "type": "string",
                "depends_on": [],
                "conflicts_with": [],
            },
        ],
    )

    asyncio.run(safety_gate(state))

    kwargs = captured["kwargs"]
    assert [item["name"] for item in kwargs["parameter_metadata"]] == ["appendonly", "save"]
    assert kwargs["current_config"] == {"appendonly": "no", "save": "3600 1"}


def test_analyzer_agent_sends_summarized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_invoke(self, user_message, context=None, temperature=None):
        captured["user_message"] = user_message
        captured["context"] = context
        return (
            '{"trend":"improving","improvement_pct":4.0,"likely_bottleneck":"memory",'
            '"change_impact":"positive","insights":"ok","recommended_focus":"cache"}'
        )

    monkeypatch.setattr("src.agents.base.BaseAgent.invoke", fake_invoke)
    agent = AnalyzerAgent()

    result = asyncio.run(
        agent.analyze(
            state={
                "target_system": "redis",
                "target_version": "7.2",
                "goals": [{"metric": "qps", "operator": ">=", "value": 1000}],
                "trial_number": 3,
                "best_metrics": {"qps": 980.0, "p99": 6.0},
                "trend_data": [
                    {"trial": 1, "metrics": {"qps": 900.0, "p99": 7.0}, "improvement_pct": 1.0},
                    {"trial": 2, "metrics": {"qps": 950.0, "p99": 6.5}, "improvement_pct": 2.0},
                ],
                "convergence_window": 5,
            },
            benchmark_results={
                "operations": [{"name": "set", "value": 1000.0}],
                "aggregate": {"qps": 1000.0, "p99": 5.8, "avg": 1.2},
            },
            parameter_changes=[{"parameter": "maxmemory", "proposed_value": "2gb"}],
        )
    )

    assert result["trend"] == "improving"
    assert captured["context"] == {}
    payload = json.loads(str(captured["user_message"]).split("INPUT_JSON:\n", 1)[1])
    assert "operations" not in payload
    assert payload["current_metrics"]["qps"] == 1000.0
    assert payload["delta_vs_best"]["qps"] == 20.0


def test_make_decision_uses_rule_based_shortcut_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_decision(self, state):
        raise AssertionError("LLM orchestrator should be skipped")

    monkeypatch.setattr("src.agents.orchestrator.OrchestratorAgent.decide_next_action", unexpected_decision)
    state = ExperimentState(
        target_system="redis",
        goals=[GoalSpec(metric="qps", operator=">=", value=100.0)],
        phase=ExperimentPhase.ANALYZING,
        improvement_threshold_pct=2.0,
        current_config={"maxmemory": "1gb"},
        best_metrics={"qps": 120.0},
    )
    trial = state.begin_trial({"maxmemory": "1gb"})
    trial.metrics = {"qps": 120.0}
    trial.improvement_pct = 5.0
    state.commit_current_trial(status="completed")
    state.improvement_history = [5.0]

    result = asyncio.run(make_decision(state))

    assert result.orchestrator_decision["action"] == "CONTINUE_TUNING"
    assert "exceeding the 2.0% threshold" in result.orchestrator_decision["reasoning"]


# ── Validation: port required ────────────────────────────────────────────


def test_requirements_complete_rejects_missing_port() -> None:
    req = _base_requirements()
    req.port = ""
    assert _requirements_complete(req) is False


def test_validate_execution_rejects_missing_port() -> None:
    req = _base_requirements()
    req.port = ""
    result = _validate_execution_requirements(req)
    assert result is not None
    assert "port" in result


def test_validate_execution_rejects_missing_profile_file(tmp_path: Path) -> None:
    req = _base_requirements()
    req.run_command = ""
    req.benchmark_profile_path = str(tmp_path / "nonexistent.yaml")
    result = _validate_execution_requirements(req)
    assert result is not None
    assert "not found" in result


# ── run_execution raises on pre-flight failures ──────────────────────────


def test_run_execution_raises_on_missing_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(
        "nanobot.agent.tuning.executor._check_dependencies",
        lambda: ["skopt", "optuna"],
    )
    session = TuningSession(
        task_id="test1234",
        task_description="test",
    )
    with pytest.raises(RuntimeError, match="Missing packages"):
        asyncio.run(run_execution(session, "/tmp/ws"))


def test_run_execution_raises_on_validation_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "nanobot.agent.tuning.executor._check_dependencies",
        lambda: [],
    )
    req = _base_requirements()
    req.host = ""
    session = TuningSession(
        task_id="test1234",
        task_description="test",
        requirements=req,
    )
    with pytest.raises(RuntimeError, match="host, port, and config file"):
        asyncio.run(run_execution(session, "/tmp/ws"))


# ── Failure sets ERROR phase and allows retry ────────────────────────────


def _provider() -> MagicMock:
    return MagicMock(get_default_model=MagicMock(return_value="test-model"))


def _make_manager(tmp_path: Path, scheduled: list) -> TuningSessionManager:
    def schedule_background(coro):
        scheduled.append(coro)
        coro.close()
        return asyncio.create_task(asyncio.sleep(0))

    return TuningSessionManager(
        provider=_provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
        schedule_background=schedule_background,
    )


def test_execution_error_sets_phase_to_error(monkeypatch, tmp_path: Path) -> None:
    """When run_execution raises, _run_execution_and_report must set ERROR phase."""
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)

    session = TuningSession(
        task_id="abc12345",
        task_description="tune redis",
        phase=TuningPhase.EXECUTION,
        requirements=_base_requirements(),
    )
    manager._sessions["cli:test"] = session

    async def fake_run_execution(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(
        "nanobot.agent.tuning.executor.run_execution",
        fake_run_execution,
    )
    monkeypatch.setattr(
        "nanobot.utils.prompt_templates.render_template",
        lambda template, **kwargs: f"status={kwargs.get('status', '?')}",
    )

    # Trigger execution
    coro = manager._run_execution_and_report(
        session, "cli:test", "cli", "test"
    )
    asyncio.run(coro)

    assert session.phase == TuningPhase.ERROR
    assert "simulated crash" in session.error


def test_error_session_can_be_retried(monkeypatch, tmp_path: Path) -> None:
    """ERROR phase session should be retried via handle_tune_request."""
    session = TuningSession(
        task_id="abc12345",
        task_description="tune redis",
        phase=TuningPhase.ERROR,
        requirements=_base_requirements(),
        error="previous failure",
    )

    manager = TuningSessionManager(
        provider=_provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
    )
    manager._sessions["cli:test"] = session

    async def fake_retry_execution(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(
        "nanobot.agent.tuning.executor.run_execution",
        fake_retry_execution,
    )
    monkeypatch.setattr(
        "nanobot.utils.prompt_templates.render_template",
        lambda template, **kwargs: f"status={kwargs.get('status', '?')}",
    )

    async def _run_retry():
        response = await manager.handle_tune_request(
            task="tune redis",
            user_response="retry",
            session_key="cli:test",
        )
        # Let the background task execute
        await asyncio.sleep(0.1)
        return response

    response = asyncio.run(_run_retry())

    assert "Retrying tuning execution" in response
    assert session.phase == TuningPhase.ERROR  # Back to ERROR after crash
