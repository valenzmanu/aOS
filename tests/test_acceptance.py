from __future__ import annotations

import subprocess
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aos.control import (
    approve_mission,
    authorize_mission,
    create_mission,
    dispatch_mission,
    process_expired_workers,
    retry_mission,
    select_route,
    signal_process,
)
from aos.store import AuthorizationError, Store, StoreError, TransitionError, token_hash
from aos.worker import run_cursor_sdk, stream_cli_driver


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "state")
    result.initialize()
    return result


def make_repo(path: Path) -> Path:
    path.mkdir()
    (path / "sample.py").write_text("VALUE = 42\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "aOS Tests"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


def create_authorized(store: Store, project: Path, token: str, **kwargs: object) -> str:
    mission_id = create_mission(
        store,
        token,
        str(project),
        str(kwargs.get("objective", "Test mission")),
        list(kwargs.get("backends", ["fake"])),
        list(kwargs.get("models", [])),
        str(kwargs["verify"]) if kwargs.get("verify") else None,
        bool(kwargs.get("allow_anthropic", False)),
    )
    authorize_mission(store, token, mission_id)
    return mission_id


def wait_for_state(store: Store, mission_id: str, expected: set[str], timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.mission(mission_id)["state"]
        if state in expected:
            return state
        time.sleep(0.05)
    return store.mission(mission_id)["state"]


def test_singleton_liaison_lease_and_hashed_token(store: Store) -> None:
    token, _ = store.claim_liaison("cursor", ttl=60)
    with pytest.raises(AuthorizationError, match="held by cursor"):
        store.claim_liaison("codex", ttl=60)
    with store.connect() as db:
        row = db.execute("SELECT token_hash FROM leases WHERE kind='liaison'").fetchone()
        event_text = "\n".join(r[0] for r in db.execute("SELECT payload FROM events"))
    assert row["token_hash"] == token_hash(token)
    assert token not in store.db_path.read_bytes().decode("utf-8", errors="ignore")
    assert token not in event_text


def test_privileged_commands_reject_missing_or_wrong_token(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    with pytest.raises(AuthorizationError):
        create_mission(store, None, str(project), "x", ["fake"], [], None)
    with pytest.raises(AuthorizationError):
        create_mission(store, "wrong", str(project), "x", ["fake"], [], None)
    mission_id = create_mission(store, token, str(project), "x", ["fake"], [], None)
    assert store.mission(mission_id)["state"] == "draft"


def test_illegal_mission_transition_is_rejected(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_mission(store, token, str(project), "x", ["fake"], [], None)
    with pytest.raises(TransitionError):
        approve_mission(store, token, mission_id)


def test_process_token_is_scoped_and_expiry_revokes_authority(store: Store) -> None:
    with store.connect() as db:
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO missions
               (id, project, objective, state, allowed_backends, allowed_models,
                verify_command, created_at, updated_at)
               VALUES ('M-ONE', '.', 'one', 'implementing', '[\"fake\"]', '[]', NULL, ?, ?),
                      ('M-TWO', '.', 'two', 'implementing', '[\"fake\"]', '[]', NULL, ?, ?)""",
            (now, now, now, now),
        )
    process_id, process_token = store.create_process("M-ONE", "fake", "fake", ".", "test")
    other_id, _ = store.create_process("M-TWO", "fake", "fake", ".", "test")
    store.update_process(process_id, process_token, "running", {"phase": "implementing"})
    with pytest.raises(AuthorizationError):
        store.update_process(other_id, process_token, "running", {"phase": "bad"})
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE processes SET token_expires_at=? WHERE id=?", (expired, process_id))
    with pytest.raises(AuthorizationError, match="expired"):
        store.update_process(process_id, process_token, "running", {"phase": "zombie"})


def test_anthropic_requires_explicit_contract_authorization(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        backends=["cursor"],
        models=["claude-opus-4"],
    )
    mission = store.mission(mission_id)
    with pytest.raises(AuthorizationError, match="Anthropic"):
        select_route(mission, "cursor", "claude-opus-4")


def test_event_journal_rejects_secret_shaped_fields(store: Store) -> None:
    with pytest.raises(StoreError, match="secret-shaped"):
        store.event("bad", "test", "one", {"liaison_token": "never"})


def test_fake_driver_worktree_verification_and_approval_gate(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        verify=f'{sys.executable} -c "import sample; assert sample.VALUE == 42"',
    )
    result = dispatch_mission(store, token, mission_id)
    assert Path(result["worktree"]).is_dir()
    assert result["backend"] == "fake"
    assert wait_for_state(store, mission_id, {"awaiting_approval", "failed"}) == "awaiting_approval"
    process = store.processes(mission_id)[0]
    assert process["state"] == "completed"
    assert process["checkpoint"]["verification"]["exit_code"] == 0
    approve_mission(store, token, mission_id)
    assert store.mission(mission_id)["state"] == "delivered"


def test_false_completion_is_blocked_by_verifier(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        verify=f'{sys.executable} -c "raise SystemExit(7)"',
    )
    dispatch_mission(store, token, mission_id)
    assert wait_for_state(store, mission_id, {"awaiting_approval", "failed"}) == "failed"
    with pytest.raises(TransitionError):
        approve_mission(store, token, mission_id)


def test_significant_sync_filters_routine_events(store: Store) -> None:
    store.event("routine", "test", "one", {"message": "working"}, "info")
    store.event("blocked", "test", "one", {"message": "decision needed"}, "attention")
    events = store.events_since(0, significant_only=True)
    assert [event["kind"] for event in events] == ["blocked"]


def test_cursor_sdk_transport_is_explicit_and_records_session(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    with store.connect() as db:
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO missions
               (id, project, objective, state, allowed_backends, allowed_models,
                verify_command, created_at, updated_at)
               VALUES ('M-SDK', '.', 'sdk test', 'implementing', '[\"cursor\"]', '[]', NULL, ?, ?)""",
            (now, now),
        )
    process_id, _ = store.create_process("M-SDK", "cursor", "composer-2.5", ".", "test")

    class Options:
        def __init__(self, **kwargs: object):
            self.kwargs = kwargs

    class Result:
        status = "completed"

    class Run:
        def stream(self):
            return iter([{"type": "assistant", "text": "done"}])

        def wait(self):
            return Result()

    class AgentInstance:
        agent_id = "cursor-session-1"

        def __enter__(self):
            return self

        def __exit__(self, *args: object):
            return None

        def send(self, _prompt: str):
            return Run()

    class Agent:
        @staticmethod
        def create(_options: object):
            return AgentInstance()

    fake_module = types.SimpleNamespace(
        Agent=Agent,
        AgentOptions=Options,
        LocalAgentOptions=Options,
        SandboxOptions=Options,
    )
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake_module)
    monkeypatch.setenv("CURSOR_API_KEY", "synthetic-test-value")
    session_id = store.create_worker_session(process_id)
    run_cursor_sdk(store, process_id, "test", Path.cwd(), "composer-2.5")
    assert store.processes("M-SDK")[0]["session_id"] == session_id
    assert store.worker_sessions(process_id)[0]["backend_handle"] == "cursor-session-1"


def test_contract_work_order_attempt_and_session_are_durable(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(store, project, token)
    contract = store.contract(mission_id)
    result = dispatch_mission(store, token, mission_id)
    assert contract["revision"] == 1
    assert len(contract["content_hash"]) == 64
    assert store.attempts(mission_id)[0]["worktree"] == result["worktree"]
    work_order = store.work_order(result["process_id"])
    assert work_order["content"]["contract_hash"] == contract["content_hash"]
    assert work_order["content"]["objective"] == "Test mission"
    assert store.worker_sessions(result["process_id"])[0]["sequence"] == 1
    assert wait_for_state(store, mission_id, {"awaiting_approval", "failed"}) == "awaiting_approval"


def test_liaison_sync_persists_high_water_without_consuming_watch_state(store: Store) -> None:
    store.event("routine", "test", "one", {"message": "working"}, "info")
    store.event("blocked", "test", "one", {"message": "decision needed"}, "attention")
    first, cursor = store.sync_significant("cursor")
    assert [event["kind"] for event in first] == ["blocked"]
    assert cursor == 2
    store.event("routine.two", "test", "one", {"message": "still working"}, "info")
    second, next_cursor = store.sync_significant("cursor")
    assert second == []
    assert next_cursor == 3
    assert len(store.events_since(0)) == 3


def test_session_retry_reuses_process_and_rotates_worker_session(
    store: Store, tmp_path: Path
) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        verify=f"{sys.executable} -c \"from pathlib import Path; assert Path('ready').exists()\"",
    )
    first = dispatch_mission(store, token, mission_id)
    assert wait_for_state(store, mission_id, {"failed"}) == "failed"
    Path(first["worktree"], "ready").write_text("fixed\n")
    second = retry_mission(store, token, mission_id, "session")
    assert second["process_id"] == first["process_id"]
    assert wait_for_state(store, mission_id, {"awaiting_approval", "failed"}) == "awaiting_approval"
    assert len(store.worker_sessions(first["process_id"])) == 2
    assert store.work_order(first["process_id"])["revision"] == 2


def test_process_retry_reuses_attempt_and_worktree(store: Store, tmp_path: Path) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        verify=f"{sys.executable} -c \"from pathlib import Path; assert Path('ready').exists()\"",
    )
    first = dispatch_mission(store, token, mission_id)
    assert wait_for_state(store, mission_id, {"failed"}) == "failed"
    Path(first["worktree"], "ready").write_text("fixed\n")
    second = retry_mission(store, token, mission_id, "process")
    assert second["process_id"] != first["process_id"]
    assert second["worktree"] == first["worktree"]
    assert len(store.attempts(mission_id)) == 1
    assert wait_for_state(store, mission_id, {"awaiting_approval", "failed"}) == "awaiting_approval"


def test_attempt_retries_trip_non_convergence_after_three_failures(
    store: Store, tmp_path: Path
) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(
        store,
        project,
        token,
        verify=f'{sys.executable} -c "raise SystemExit(9)"',
    )
    dispatch_mission(store, token, mission_id)
    assert wait_for_state(store, mission_id, {"failed"}) == "failed"
    retry_mission(store, token, mission_id, "attempt")
    assert wait_for_state(store, mission_id, {"failed", "non_convergent"}) == "failed"
    retry_mission(store, token, mission_id, "attempt")
    assert wait_for_state(store, mission_id, {"non_convergent"}) == "non_convergent"
    assert len(store.attempts(mission_id)) == 3
    with pytest.raises(TransitionError):
        retry_mission(store, token, mission_id, "attempt")


def test_recovery_marks_expired_process_session_attempt_and_mission(
    store: Store, tmp_path: Path
) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(store, project, token)
    worktree = str(tmp_path / "worktree")
    store.create_attempt(mission_id, 1, worktree, "aos/test")
    process_id, _ = store.create_process(
        mission_id, "fake", "fake-deterministic", worktree, "test", attempt=1
    )
    session_id = store.create_worker_session(process_id)
    store.transition_mission(mission_id, "dispatched", {"authorized"})
    store.transition_mission(mission_id, "implementing", {"dispatched"})
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.connect() as db:
        db.execute(
            "UPDATE processes SET state='running', token_expires_at=? WHERE id=?",
            (expired, process_id),
        )
    assert process_expired_workers(store) == 1
    assert store.processes(mission_id)[0]["state"] == "orphaned"
    assert store.worker_sessions(process_id)[0]["state"] == "orphaned"
    assert store.attempts(mission_id)[0]["state"] == "orphaned"
    assert store.mission(mission_id)["state"] == "failed"
    assert session_id == store.processes(mission_id)[0]["session_id"]


def test_limited_provider_falls_through_without_expanding_authority(
    store: Store, tmp_path: Path
) -> None:
    token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(store, project, token, backends=["cursor", "codex"])
    store.observe_provider("cursor", "limited", "test", "authoritative")
    backend, model, reason = select_route(store.mission(mission_id), None, None, store)
    assert backend == "codex"
    assert model == "gpt-5.6-sol"
    assert "cursor rejected: status=limited" in reason
    with pytest.raises(AuthorizationError, match="no eligible route"):
        select_route(store.mission(mission_id), "cursor", None, store)


def test_streaming_cli_records_backend_handle_usage_and_health(
    store: Store, tmp_path: Path
) -> None:
    with store.connect() as db:
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO missions
               (id, project, objective, state, allowed_backends, allowed_models,
                verify_command, created_at, updated_at)
               VALUES ('M-STREAM', '.', 'stream', 'implementing', '[\"codex\"]', '[]', NULL, ?, ?)""",
            (now, now),
        )
    process_id, process_token = store.create_process(
        "M-STREAM", "codex", "synthetic-model", str(tmp_path), "test", attempt=1
    )
    store.create_worker_session(process_id)
    process = store.processes("M-STREAM")[0]
    code = (
        "import json,sys; sys.stdin.read(); "
        "print(json.dumps({'type':'thread.started','thread_id':'thread-1'})); "
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}))"
    )
    stream_cli_driver(
        store,
        process,
        process_id,
        process_token,
        [sys.executable, "-c", code],
        "synthetic prompt",
        tmp_path,
    )
    session = store.worker_sessions(process_id)[0]
    assert session["backend_handle"] == "thread-1"
    assert session["metadata"]["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert store.latest_provider_status("codex")["status"] == "healthy"


def test_schema_v2_backfills_durable_objects_from_alpha_projection(store: Store) -> None:
    now = datetime.now(UTC).isoformat()
    with store.connect() as db:
        db.execute(
            """INSERT INTO missions
               (id, project, objective, state, allowed_backends, allowed_models,
                verify_command, created_at, updated_at)
               VALUES ('M-LEGACY', '.', 'legacy objective', 'failed', '[\"codex\"]', '[]',
                       'python -m unittest', ?, ?)""",
            (now, now),
        )
        db.execute(
            """INSERT INTO processes
               (id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, checkpoint, routing_reason,
                created_at, updated_at)
               VALUES ('P-LEGACY', 'M-LEGACY', 1, 'codex', 'model', 'failed', ?, ?, '.',
                       'legacy-thread', '{}', 'legacy route', ?, ?)""",
            (token_hash("legacy-process-token"), now, now, now),
        )
        db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    store.initialize()
    assert store.contract("M-LEGACY")["content"]["objective"] == "legacy objective"
    assert store.attempts("M-LEGACY")[0]["branch"] == "legacy/unknown"
    assert store.work_order("P-LEGACY")["content"]["migration_note"]
    session = store.worker_sessions("P-LEGACY")[0]
    assert session["backend_handle"] == "legacy-thread"
    assert store.processes("M-LEGACY")[0]["session_id"] == session["id"]
    with store.connect() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "2"
        )


def test_liaison_can_pause_resume_and_cancel_with_authority_revocation(
    store: Store, tmp_path: Path
) -> None:
    liaison_token, _ = store.claim_liaison("cursor")
    project = make_repo(tmp_path / "repo")
    mission_id = create_authorized(store, project, liaison_token)
    store.create_attempt(mission_id, 1, str(tmp_path), "aos/signal")
    process_id, process_token = store.create_process(
        mission_id, "fake", "fake-deterministic", str(tmp_path), "test", attempt=1
    )
    store.create_worker_session(process_id)
    store.transition_mission(mission_id, "dispatched", {"authorized"})
    store.transition_mission(mission_id, "implementing", {"dispatched"})
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    store.set_process_pid(process_id, child.pid)
    with store.connect() as db:
        db.execute("UPDATE processes SET state='running' WHERE id=?", (process_id,))
    try:
        assert signal_process(store, liaison_token, process_id, "pause")["state"] == "paused"
        assert store.processes(mission_id)[0]["state"] == "paused"
        assert signal_process(store, liaison_token, process_id, "resume")["state"] == "running"
        assert signal_process(store, liaison_token, process_id, "cancel")["state"] == "cancelled"
        child.wait(timeout=5)
        assert store.mission(mission_id)["state"] == "cancelled"
        with pytest.raises(AuthorizationError):
            store.require_process(process_id, process_token)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
