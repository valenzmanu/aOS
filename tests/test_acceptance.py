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
    select_route,
)
from aos.store import AuthorizationError, Store, StoreError, TransitionError, token_hash
from aos.worker import run_cursor_sdk


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
    run_cursor_sdk(store, process_id, "test", Path.cwd(), "composer-2.5")
    assert store.processes("M-SDK")[0]["session_id"] == "cursor-session-1"
