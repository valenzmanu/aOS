"""Atomic, scoped authority epochs for the sparse aOS v2 kernel.

Agents remain the runtime.  This module is only the deterministic capability
joint: it claims, validates, renews, and closes authority through the canonical
journal transaction kernel.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from modules.journal import (
    ExpectedRevision,
    GateRejection,
    InvalidOperation,
    Invoker,
    OperationOutcome,
    OperationRequest,
    _run_governed,
    canonical_json_bytes,
    utc_now,
)

LIAISON_SCOPE = "liaison:root"
MINIMUM_TOKEN_BYTES = 32
MAXIMUM_TOKEN_BYTES = 512
MAXIMUM_TTL_SECONDS = 86_400


@dataclass(frozen=True, slots=True)
class LiaisonClaimRequest:
    operation_id: str
    candidate_id: str
    epoch_id: str
    expected_epoch_revision: int
    ttl_seconds: int = 600


@dataclass(frozen=True, slots=True)
class AuthorityEpoch:
    epoch_id: str
    kind: str
    subject_id: str
    scope: str
    issued_at: str
    expires_at: str
    status: str
    revision: int
    issued_event_id: int
    closed_at: str | None
    closed_event_id: int | None
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class AuthorityOutcome:
    status: str
    operation_id: str
    epoch_id: str
    expires_at: str | None
    admission_event_id: int | None
    authority_event_id: int | None
    rejection_event_id: int | None
    reason: str | None
    original_status: str | None = None


class _ValidationContext(Protocol):
    db: sqlite3.Connection
    request: OperationRequest
    occurred_at: datetime


class _ProjectionContext(Protocol):
    db: sqlite3.Connection
    request: OperationRequest
    occurred_at: str

    def append_event(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, object],
        *,
        severity: str = "info",
        payload_schema_version: int = 1,
    ) -> object: ...


def _token_bytes(token: str) -> bytes:
    if not isinstance(token, str):
        raise InvalidOperation("capability token must be text")
    encoded = token.encode("utf-8")
    if (
        len(encoded) < MINIMUM_TOKEN_BYTES
        or len(encoded) > MAXIMUM_TOKEN_BYTES
        or token != token.strip()
        or "\x00" in token
    ):
        raise InvalidOperation("capability token must be bounded private material")
    return encoded


def capability_digest(
    token: str,
    *,
    kind: str,
    subject_id: str,
    scope: str,
) -> str:
    """Hash a raw capability while binding it to its authority scope."""

    encoded_token = _token_bytes(token)
    material = canonical_json_bytes(
        {
            "format": "aos.capability.v1",
            "kind": kind,
            "scope": scope,
            "subject_id": subject_id,
            "token": token,
        }
    )
    # Keep the explicit token validation above: canonical JSON accepts values
    # that are unsuitable as ephemeral capability material.
    if not encoded_token:
        raise InvalidOperation("capability token must not be empty")
    return hashlib.sha256(material).hexdigest()


def _require_ttl(ttl_seconds: object) -> int:
    if type(ttl_seconds) is not int or ttl_seconds <= 0 or ttl_seconds > MAXIMUM_TTL_SECONDS:
        raise InvalidOperation(f"ttl_seconds must be between 1 and {MAXIMUM_TTL_SECONDS}")
    return ttl_seconds


def _authority_epoch_from_row(row: sqlite3.Row | tuple[object, ...]) -> AuthorityEpoch:
    return AuthorityEpoch(
        epoch_id=str(row[0]),
        kind=str(row[1]),
        subject_id=str(row[2]),
        scope=str(row[3]),
        issued_at=str(row[4]),
        expires_at=str(row[5]),
        status=str(row[6]),
        revision=int(row[7]),
        issued_event_id=int(row[8]),
        closed_at=None if row[9] is None else str(row[9]),
        closed_event_id=None if row[10] is None else int(row[10]),
        close_reason=None if row[11] is None else str(row[11]),
    )


def read_authority_epoch(root: Path, epoch_id: str) -> AuthorityEpoch | None:
    """Read an authority epoch without renewing or otherwise mutating it."""

    db_path = Path(root).expanduser().resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT
                epoch.epoch_id, epoch.kind, epoch.subject_id, epoch.scope,
                epoch.issued_at, epoch.expires_at, epoch.status,
                revision.revision, epoch.issued_event_id,
                epoch.closed_at, epoch.closed_event_id, epoch.close_reason
            FROM authority_epochs AS epoch
            JOIN aggregate_revisions AS revision
              ON revision.aggregate_type='authority_epoch'
             AND revision.aggregate_id=epoch.epoch_id
            WHERE epoch.epoch_id=?
            """,
            (epoch_id,),
        ).fetchone()
    return None if row is None else _authority_epoch_from_row(row)


def read_liaison(root: Path) -> AuthorityEpoch | None:
    """Return the current active liaison epoch, even when its TTL has elapsed."""

    db_path = Path(root).expanduser().resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT
                epoch.epoch_id, epoch.kind, epoch.subject_id, epoch.scope,
                epoch.issued_at, epoch.expires_at, epoch.status,
                revision.revision, epoch.issued_event_id,
                epoch.closed_at, epoch.closed_event_id, epoch.close_reason
            FROM authority_epochs AS epoch
            JOIN aggregate_revisions AS revision
              ON revision.aggregate_type='authority_epoch'
             AND revision.aggregate_id=epoch.epoch_id
            WHERE epoch.kind='liaison' AND epoch.status='active'
            """
        ).fetchone()
    return None if row is None else _authority_epoch_from_row(row)


def _authority_outcome(
    outcome: OperationOutcome,
    *,
    root: Path,
    epoch_id: str,
) -> AuthorityOutcome:
    epoch = read_authority_epoch(root, epoch_id)
    return AuthorityOutcome(
        status=outcome.status,
        original_status=outcome.original_status,
        operation_id=outcome.operation_id,
        epoch_id=epoch_id,
        expires_at=None if epoch is None else epoch.expires_at,
        admission_event_id=outcome.admission_event_id,
        authority_event_id=(outcome.domain_event_ids[-1] if outcome.domain_event_ids else None),
        rejection_event_id=outcome.rejection_event_id,
        reason=outcome.reason,
    )


def claim_liaison(
    root: Path,
    request: LiaisonClaimRequest,
    *,
    token: str,
    clock: Callable[[], datetime] = utc_now,
) -> AuthorityOutcome:
    """Atomically claim the vacant singleton liaison epoch."""

    ttl_seconds = _require_ttl(request.ttl_seconds)
    token_hash = capability_digest(
        token,
        kind="liaison",
        subject_id=request.candidate_id,
        scope=LIAISON_SCOPE,
    )
    operation = OperationRequest(
        operation_id=request.operation_id,
        action="lease.claim_liaison",
        invoker=Invoker(kind="liaison_candidate", id=request.candidate_id),
        expected_revisions=(
            ExpectedRevision(
                "authority_epoch",
                request.epoch_id,
                request.expected_epoch_revision,
            ),
        ),
        payload={
            "epoch_id": request.epoch_id,
            "kind": "liaison",
            "scope": LIAISON_SCOPE,
            "subject_id": request.candidate_id,
            "token_hash": token_hash,
            "ttl_seconds": ttl_seconds,
        },
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        existing = context.db.execute(
            """
            SELECT epoch_id, subject_id, expires_at
            FROM authority_epochs
            WHERE kind='liaison' AND status='active'
            """
        ).fetchone()
        if existing is not None:
            return GateRejection(
                "liaison_lease_held",
                {
                    "epoch_id": str(existing[0]),
                    "expires_at": str(existing[2]),
                    "holder": str(existing[1]),
                },
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        issued_at = context.occurred_at
        occurred_at = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        expires_at = (occurred_at + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="milliseconds"
        )
        event = context.append_event(
            "authority.claimed",
            "authority_epoch",
            request.epoch_id,
            {
                "epoch_id": request.epoch_id,
                "expires_at": expires_at,
                "kind": "liaison",
                "scope": LIAISON_SCOPE,
                "subject_id": request.candidate_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO authority_epochs(
                epoch_id, kind, subject_id, scope, token_hash,
                issued_at, expires_at, status, issued_event_id
            ) VALUES (?, 'liaison', ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                request.epoch_id,
                request.candidate_id,
                LIAISON_SCOPE,
                token_hash,
                issued_at,
                expires_at,
                event.event_id,  # type: ignore[attr-defined]
            ),
        )

    outcome = _run_governed(
        root,
        operation,
        validator=validator,
        projector=projector,
        clock=clock,
    )
    return _authority_outcome(outcome, root=root, epoch_id=request.epoch_id)
