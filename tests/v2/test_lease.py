from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modules.journal import InitializationRequest, initialize_root, read_events
from modules.lease import (
    LiaisonClaimRequest,
    capability_digest,
    claim_liaison,
    read_liaison,
)

FIXED_TIME = datetime(2026, 7, 23, 17, 0, tzinfo=UTC)
LIAISON_TOKEN = "liaison_capability_material_1234567890abcdef"


@dataclass(frozen=True)
class InitializedRoot:
    path: Path
    initialization_event_id: int


@pytest.fixture
def initialized_root(tmp_path: Path) -> InitializedRoot:
    root = tmp_path / "state"
    initialized = initialize_root(
        root,
        InitializationRequest(
            operation_id="OP-INIT-LEASE-TESTS",
            invoker_id="maintenance:lease-tests",
            expected_schema=0,
            expected_root_state="empty",
        ),
        clock=lambda: FIXED_TIME,
    )
    return InitializedRoot(root, initialized.initialization_event_id)


def test_first_liaison_claim_is_scoped_hashed_and_journaled(
    initialized_root: InitializedRoot,
) -> None:
    request = LiaisonClaimRequest(
        operation_id="OP-CLAIM-FIRST-LIAISON",
        candidate_id="liaison:cursor",
        epoch_id="AE-LIAISON-ONE",
        expected_epoch_revision=0,
        ttl_seconds=600,
    )

    outcome = claim_liaison(
        initialized_root.path,
        request,
        token=LIAISON_TOKEN,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    assert outcome.epoch_id == "AE-LIAISON-ONE"
    assert outcome.expires_at == "2026-07-23T17:10:00.000+00:00"
    lease = read_liaison(initialized_root.path)
    assert lease is not None
    assert lease.epoch_id == outcome.epoch_id
    assert lease.subject_id == "liaison:cursor"
    assert lease.scope == "liaison:root"
    assert lease.status == "active"
    assert lease.revision == 1

    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        stored_hash = db.execute(
            "SELECT token_hash FROM authority_epochs WHERE epoch_id=?",
            (outcome.epoch_id,),
        ).fetchone()[0]
    assert stored_hash == capability_digest(
        LIAISON_TOKEN,
        kind="liaison",
        subject_id="liaison:cursor",
        scope="liaison:root",
    )
    assert LIAISON_TOKEN.encode() not in (initialized_root.path / "aos.db").read_bytes()

    events = read_events(
        initialized_root.path,
        after_event_id=initialized_root.initialization_event_id,
    )
    assert [event.kind for event in events] == ["gate.admitted", "authority.claimed"]
    assert events[0].envelope is not None
    assert events[0].envelope["invoker_kind"] == "liaison_candidate"
    assert events[0].envelope["authority_epoch_id"] is None
    assert events[1].payload == {
        "epoch_id": "AE-LIAISON-ONE",
        "expires_at": "2026-07-23T17:10:00.000+00:00",
        "kind": "liaison",
        "scope": "liaison:root",
        "subject_id": "liaison:cursor",
    }
