from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import modules.lease as lease
from modules.journal import InitializationRequest, initialize_root
from modules.lease import capability_digest, read_authority_epoch

FIXED_TIME = datetime(2026, 7, 23, 17, 0, tzinfo=UTC)
WORKER_TOKEN = "worker_capability_material_1234567890abcdef"


def test_capability_digest_remains_bound_to_delegated_worker_scope() -> None:
    digest = capability_digest(
        WORKER_TOKEN,
        kind="worker",
        subject_id="worker:one",
        scope="process:P-ONE",
    )

    assert len(digest) == 64
    assert digest != capability_digest(
        WORKER_TOKEN,
        kind="worker",
        subject_id="worker:two",
        scope="process:P-ONE",
    )
    assert digest != capability_digest(
        WORKER_TOKEN,
        kind="worker",
        subject_id="worker:one",
        scope="process:P-TWO",
    )


def test_empty_root_has_no_delegated_authority_epoch(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize_root(
        root,
        InitializationRequest(
            operation_id="OP-INIT-DELEGATED-AUTHORITY",
            invoker_id="maintenance:lease-tests",
            expected_schema=0,
            expected_root_state="empty",
        ),
        clock=lambda: FIXED_TIME,
    )

    assert read_authority_epoch(root, "AE-WORKER-ONE") is None


def test_liaison_ownership_surface_is_removed() -> None:
    assert not hasattr(lease, "LIAISON_SCOPE")
    assert not hasattr(lease, "LiaisonClaimRequest")
    assert not hasattr(lease, "claim_liaison")
    assert not hasattr(lease, "read_liaison")
