"""Scoped delegated authority primitives for the sparse aOS v2 kernel.

Resident liaison sessions never acquire authority epochs. This module retains
the deterministic capability mechanics needed by delegated worker and process
authority without owning or orchestrating their lifecycles.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from modules.journal import InvalidOperation, canonical_json_bytes

MINIMUM_TOKEN_BYTES = 32
MAXIMUM_TOKEN_BYTES = 512
MAXIMUM_TTL_SECONDS = 86_400


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
    """Hash a raw delegated capability while binding it to its authority scope."""

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
    if not encoded_token:
        raise InvalidOperation("capability token must not be empty")
    return hashlib.sha256(material).hexdigest()


def read_authority_epoch(root: Path, epoch_id: str) -> AuthorityEpoch | None:
    """Read a delegated authority epoch without renewing or mutating it."""

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
    if row is None:
        return None
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
