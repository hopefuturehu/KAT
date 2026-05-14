import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.agent.tuning.manager import TuningSessionManager
from nanobot.agent.tuning.profile_store import TuningProfileStore
from nanobot.agent.tuning.schema import TuningGoal, TuningPhase, TuningRequirements, TuningSession
from nanobot.bus.queue import MessageBus


def _provider() -> MagicMock:
    return MagicMock(get_default_model=MagicMock(return_value="test-model"))


def _requirements() -> TuningRequirements:
    return TuningRequirements(
        target_system="redis",
        target_version="7.2",
        goals=[TuningGoal(metric="qps", operator=">=", value=80000, weight=1.0)],
        host="127.0.0.1",
        port="6379",
        password="",
        config_file="/tmp/redis.conf",
        run_command="redis-benchmark -h {host} -p {port} --csv",
        health_check_command="redis-cli -h {host} -p {port} PING",
        allow_restart=False,
        max_risk_level="medium",
    )


def _make_manager(tmp_path: Path, scheduled: list[object]) -> TuningSessionManager:
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


def test_existing_yaml_profiles_are_offered_for_reuse(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    manager.profile_store.save_requirements(_requirements(), task_description="existing profile")

    response = asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            session_key="cli:direct",
        )
    )

    session = manager.get_session("cli:direct")
    assert session is not None
    assert session.awaiting_profile_selection is True
    assert "saved tuning profiles" in response
    assert "1." in response


def test_selecting_saved_profile_starts_execution(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    store_path = manager.profile_store.save_requirements(
        _requirements(),
        task_description="existing profile",
    )

    asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            session_key="cli:direct",
        )
    )
    response = asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            user_response="1",
            session_key="cli:direct",
        )
    )

    session = manager.get_session("cli:direct")
    assert session is not None
    assert session.phase == TuningPhase.EXECUTION
    assert session.requirements.host == "127.0.0.1"
    assert session.awaiting_profile_selection is False
    assert len(scheduled) == 1
    assert str(store_path.name) in response


def test_skip_profile_reuse_falls_back_to_intake(monkeypatch, tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    manager.profile_store.save_requirements(_requirements(), task_description="existing profile")

    captured: dict[str, object] = {}

    async def fake_run_intake_turn(**kwargs):
        captured["conversation"] = kwargs["conversation"]
        conversation = list(kwargs["conversation"])
        conversation.append({"role": "assistant", "content": "请提供目标实例的 host 和 config_file"})
        return "请提供目标实例的 host 和 config_file", conversation, None

    monkeypatch.setattr("nanobot.agent.tuning.manager.run_intake_turn", fake_run_intake_turn)

    asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            session_key="cli:direct",
        )
    )
    response = asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            user_response="skip",
            session_key="cli:direct",
        )
    )

    session = manager.get_session("cli:direct")
    assert session is not None
    assert session.awaiting_profile_selection is False
    assert captured["conversation"] == [{"role": "user", "content": "帮我调优 redis 吞吐量"}]
    assert response == "请提供目标实例的 host 和 config_file"


def test_completed_intake_is_saved_for_future_reuse(monkeypatch, tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    req = _requirements()

    async def fake_run_intake_turn(**kwargs):
        conversation = list(kwargs["conversation"])
        conversation.append({"role": "assistant", "content": '{"target_system":"redis"}'})
        return '{"target_system":"redis"}', conversation, req

    monkeypatch.setattr("nanobot.agent.tuning.manager.run_intake_turn", fake_run_intake_turn)

    response = asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            session_key="cli:direct",
        )
    )

    store = TuningProfileStore(tmp_path)
    profiles = store.list_profiles("redis")
    assert len(profiles) == 1
    assert profiles[0].requirements.config_file == "/tmp/redis.conf"
    assert "Saved Profile:" in response


def test_saved_profiles_use_unique_file_names(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)

    first = manager.profile_store.save_requirements(_requirements(), task_description="first")
    second = manager.profile_store.save_requirements(_requirements(), task_description="second")

    assert first != second
    assert first.exists()
    assert second.exists()
    assert len(manager.profile_store.list_profiles("redis")) == 2


def test_sensitive_password_is_redacted_from_saved_profile(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    req = _requirements()
    req.password = "super-secret"

    saved = manager.profile_store.save_requirements(req, task_description="has secret")
    raw = saved.read_text(encoding="utf-8")
    profiles = manager.profile_store.list_profiles("redis")

    assert "super-secret" not in raw
    assert profiles[0].requirements.password == ""
    assert profiles[0].summary()["redacted_fields"] == ["password"]


def test_execution_request_is_not_duplicated_while_running(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    session = TuningSession(
        task_id="abc12345",
        task_description="帮我调优 redis 吞吐量",
        phase=TuningPhase.EXECUTION,
        requirements=_requirements(),
    )
    manager._sessions["cli:direct"] = session

    async def _run() -> str:
        blocker = asyncio.create_task(asyncio.sleep(10))
        manager._execution_tasks["cli:direct"] = blocker
        try:
            return await manager.handle_tune_request(
                task="帮我调优 redis 吞吐量",
                session_key="cli:direct",
            )
        finally:
            blocker.cancel()

    response = asyncio.run(_run())

    assert "already running" in response
    assert len(scheduled) == 0


def test_selecting_redacted_profile_returns_to_intake_for_missing_secrets(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    req = _requirements()
    req.password = "super-secret"
    manager.profile_store.save_requirements(req, task_description="existing profile")

    asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            session_key="cli:direct",
        )
    )
    response = asyncio.run(
        manager.handle_tune_request(
            task="帮我调优 redis 吞吐量",
            user_response="1",
            session_key="cli:direct",
        )
    )

    session = manager.get_session("cli:direct")
    assert session is not None
    assert session.phase == TuningPhase.INTAKE
    assert session.awaiting_profile_selection is False
    assert len(scheduled) == 0
    assert "Missing sensitive fields: password" in response


def test_cancel_session_cancels_active_execution_task(tmp_path: Path) -> None:
    scheduled: list[object] = []
    manager = _make_manager(tmp_path, scheduled)
    manager._sessions["cli:direct"] = TuningSession(
        task_id="abc12345",
        task_description="帮我调优 redis 吞吐量",
        phase=TuningPhase.EXECUTION,
        requirements=_requirements(),
    )

    async def _run() -> tuple[str | None, bool]:
        sleeper = asyncio.create_task(asyncio.sleep(10))
        manager._execution_tasks["cli:direct"] = sleeper
        message = manager.cancel_session("cli:direct")
        await asyncio.sleep(0)
        return message, sleeper.cancelled()

    message, cancelled = asyncio.run(_run())

    assert message == "Cancelled the tuning session and stopped the active execution."
    assert cancelled is True
