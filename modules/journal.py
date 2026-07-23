from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from modules.protocol import (
    EventAggregateType,
    ProtocolViolation,
    require_generic_event_kind,
    require_reserved_event,
)

CURRENT_SCHEMA_VERSION = 4
LEGACY_STRUCTURE_FINGERPRINTS = {
    1: "17cc81e41aff3845a83e0da9496ec1eba30be0e849fd2679828fcc438271c541",
    2: "abb082983ec5da9b65a33f107143a131ffdddc6570947dc5e85142b50a8064d2",
}
LEGACY_PROJECTION_TABLES = {
    1: ("leases", "missions", "processes", "inbox_cursors"),
    2: (
        "leases",
        "missions",
        "processes",
        "inbox_cursors",
        "contract_revisions",
        "attempts",
        "work_orders",
        "worker_sessions",
        "provider_observations",
    ),
}
CONTESTED_AGGREGATES = {
    "leases": ("lease", "kind"),
    "missions": ("mission", "id"),
    "processes": ("process", "id"),
    "contract_revisions": ("contract_revision", "id"),
    "attempts": ("attempt", "id"),
    "work_orders": ("work_order", "id"),
    "worker_sessions": ("worker_session", "id"),
}
CURRENT_REQUIRED_TABLES = {
    "aggregate_revisions",
    "approval_decisions",
    "attempts",
    "authority_epochs",
    "contract_revisions",
    "event_envelopes",
    "event_targets",
    "events",
    "evidence_records",
    "inbox_cursors",
    "leases",
    "legacy_artifacts",
    "legacy_authority_epochs",
    "metadata",
    "migration_backups",
    "migration_baseline_revisions",
    "migration_cleanup",
    "migration_snapshots",
    "missions",
    "operation_conflicts",
    "operation_targets",
    "operations",
    "processes",
    "provider_observations",
    "work_orders",
    "worker_sessions",
}
V2_CORE_TABLES = {
    "approval_decisions",
    "authority_epochs",
    "event_envelopes",
    "event_targets",
    "evidence_records",
    "operation_conflicts",
    "operation_targets",
    "operations",
}
SCHEMA3_REQUIRED_TABLES = CURRENT_REQUIRED_TABLES - V2_CORE_TABLES
SCHEMA3_POST_BOUNDARY_EVENT_KINDS = {
    "attempt.reconciled",
    "authority.legacy_closed",
    "migration.boundary",
    "migration.cleanup_completed",
    "mission.reconciled",
    "process.reconciled",
    "worker_session.reconciled",
}
SCHEMA3_STRUCTURE_FINGERPRINTS = {
    1: "56d1787cd6f5b2ce0b9673072aa498c29947d2ea5665e7bfa576c390a0a91750",
    2: "9dbb2c3b35f0f43ff1fd50339aceb98b7eced27b90a118c81b23df7ba9b685dd",
}
SECRET_ARTIFACT_PATTERNS = (
    (
        "private key",
        re.compile(rb"-----BEGIN (?:(?:ENCRYPTED|RSA|EC|DSA|OPENSSH) )?PRIVATE KEY-----"),
    ),
    ("PGP private key", re.compile(rb"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    ("GitHub token", re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Cursor key", re.compile(rb"\bcrsr_[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "authorization bearer credential",
        re.compile(
            rb"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+"
            rb"[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        "JSON web token",
        re.compile(
            rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
            rb"[A-Za-z0-9_-]{8,}\b"
        ),
    ),
)
MIGRATION_OPERATION_PATTERN = re.compile(r"OP-[A-Z0-9][A-Z0-9_-]{1,124}\Z")
MIGRATION_INVOKER_PATTERN = re.compile(r"maintenance:[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
OPERATION_ACTION_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")
PUBLIC_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}\Z")
INVOKER_KINDS = {
    "eval_fixture",
    "liaison",
    "liaison_candidate",
    "maintenance_operator",
    "migration",
    "module",
    "reconciler",
    "worker",
}
AUTHORITY_BEARING_INVOKERS = {"liaison", "worker"}
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    ["']?([a-z][a-z0-9_.-]{1,63})["']?
    \s*[:=]\s*
    ["']?([^"',}\]\s]{1,512})
    """
)
SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SECRET_DIGEST_SUFFIXES = (
    "_hash",
    "_digest",
    "_checksum",
    "_fingerprint",
)
NONSECRET_METADATA_SUFFIXES = (
    "_expires_at",
    "_expired_at",
    "_expiry",
    "_expiration",
)
SECRET_FIELD_TERMS = {"password", "passwd", "secret", "credential"}
SECRET_FIELD_NAMES = {
    "api_key",
    "access_key",
    "access_token",
    "bearer",
    "private_key",
    "authorization",
    "auth_token",
    "bearer_token",
    "claim_token",
    "client_secret",
    "liaison_token",
    "provider_token",
    "refresh_token",
    "worker_token",
}
SECRET_FIELD_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_private_key",
    "_secret_key",
)


class JournalError(RuntimeError):
    """Base error for canonical journal operations."""


class MigrationRejected(JournalError):
    """The requested migration does not match the observed legacy root."""


class InvalidOperation(JournalError):
    """An operation request is malformed or unsafe to persist."""


@dataclass(frozen=True, slots=True)
class Invoker:
    kind: str
    id: str
    authority_epoch_id: str | None = None
    on_behalf_of: str | None = None
    decision_source_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedRevision:
    aggregate_type: str
    aggregate_id: str
    revision: int
    role: str = "write"


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_id: str
    action: str
    invoker: Invoker
    expected_revisions: tuple[ExpectedRevision, ...]
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GateRejection:
    code: str
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    status: str
    operation_id: str
    admission_event_id: int | None
    rejection_event_id: int | None
    domain_event_ids: tuple[int, ...]
    revisions: tuple[ExpectedRevision, ...]
    reason: str | None = None
    original_status: str | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: int
    occurred_at: str
    kind: str
    severity: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, object]
    envelope: Mapping[str, object] | None


@dataclass(frozen=True)
class MigrationRequest:
    operation_id: str
    invoker_id: str
    expected_schema: int
    expected_root_state: str
    expected_high_water_event_id: int


@dataclass(frozen=True)
class MigrationOutcome:
    status: str
    operation_id: str
    source_schema: int
    target_schema: int
    legacy_high_water_event_id: int
    boundary_event_id: int
    snapshot_digest: str
    backup_digest: str


@dataclass(frozen=True)
class SchemaUpgradeOutcome:
    status: str
    operation_id: str
    source_schema: int
    target_schema: int
    legacy_high_water_event_id: int
    upgrade_event_id: int
    backup_digest: str


@dataclass(frozen=True)
class InitializationRequest:
    operation_id: str
    invoker_id: str
    expected_schema: int
    expected_root_state: str


@dataclass(frozen=True)
class InitializationOutcome:
    status: str
    operation_id: str
    schema_version: int
    initialization_event_id: int


@dataclass(frozen=True)
class JournalInspection:
    schema_version: int
    event_high_water_event_id: int
    integrity: str


@dataclass(frozen=True)
class _LegacyArtifact:
    legacy_path: str
    digest: str | None
    size: int | None
    status: str
    blob_digest: str | None
    quarantine_reason: str | None
    content: bytes | None
    source_path: str | None


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json_bytes(value: object) -> bytes:
    """Encode deterministic JSON for request and content identities."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidOperation("value is not canonical JSON") from exc
    return encoded.encode("utf-8")


def content_digest(value: object) -> str:
    """Return the SHA-256 identity of canonical JSON content."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _request_hash(request: MigrationRequest) -> str:
    encoded = _canonical_json(
        {
            "expected_high_water_event_id": request.expected_high_water_event_id,
            "expected_root_state": request.expected_root_state,
            "expected_schema": request.expected_schema,
            "invoker_id": request.invoker_id,
            "operation_id": request.operation_id,
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _initialization_request_hash(request: InitializationRequest) -> str:
    encoded = _canonical_json(
        {
            "expected_root_state": request.expected_root_state,
            "expected_schema": request.expected_schema,
            "invoker_id": request.invoker_id,
            "operation_id": request.operation_id,
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise MigrationRejected(f"{label} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise MigrationRejected(f"{label} is required")
    if cleaned != value:
        raise MigrationRejected(f"{label} must not contain surrounding whitespace")
    return cleaned


def _required_migration_operation_id(value: str) -> str:
    cleaned = _required_text(value, "operation_id")
    if (
        MIGRATION_OPERATION_PATTERN.fullmatch(cleaned) is None
        or _content_policy_reason(cleaned.encode("utf-8")) is not None
    ):
        raise MigrationRejected(
            "operation_id must use the nonsecret OP-[A-Z0-9_-] migration identifier form"
        )
    return cleaned


def _required_migration_invoker_id(value: str) -> str:
    cleaned = _required_text(value, "invoker_id")
    if (
        MIGRATION_INVOKER_PATTERN.fullmatch(cleaned) is None
        or _content_policy_reason(cleaned.encode("utf-8")) is not None
    ):
        raise MigrationRejected(
            "invoker_id must identify a maintenance operator without secret-shaped text"
        )
    return cleaned


def _normalized_field_name(key: str) -> str:
    snake_case = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key.strip())
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake_case)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
    return normalized


def _secret_field_reason(key: str) -> str | None:
    normalized = _normalized_field_name(key)
    if not normalized or normalized.endswith(NONSECRET_METADATA_SUFFIXES):
        return None
    digest_form = normalized.endswith(SECRET_DIGEST_SUFFIXES)
    terms = set(normalized.split("_"))
    secret_semantics = (
        normalized in SECRET_FIELD_NAMES
        or any(marker in normalized for marker in SECRET_FIELD_NAMES)
        or normalized.endswith(SECRET_FIELD_SUFFIXES)
        or bool(terms & SECRET_FIELD_TERMS)
        or "token" in terms
    )
    if secret_semantics and not digest_form:
        return f"secret-shaped field {key!r}"
    return None


def _is_required_sha256_field(key: str) -> bool:
    normalized = _normalized_field_name(key)
    if normalized in {
        "content_hash",
        "contract_hash",
        "request_hash",
        "token_hash",
    }:
        return True
    if not normalized.endswith(SECRET_DIGEST_SUFFIXES):
        return False
    terms = set(normalized.split("_"))
    return (
        any(marker in normalized for marker in SECRET_FIELD_NAMES)
        or bool(terms & SECRET_FIELD_TERMS)
        or "token" in terms
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_HEX_PATTERN.fullmatch(value) is not None


def _secret_pattern_reason(content: bytes) -> str | None:
    return next(
        (label for label, pattern in SECRET_ARTIFACT_PATTERNS if pattern.search(content)),
        None,
    )


def _structured_secret_reason(
    value: object,
    path: str = "$",
    depth: int = 0,
) -> str | None:
    if depth > 8:
        return f"structured value exceeds recursive policy depth at {path}"
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            key_pattern = _secret_pattern_reason(str(key).encode("utf-8"))
            if key_pattern is not None:
                return f"{key_pattern} in field name at {path}"
            if (
                _is_required_sha256_field(str(key))
                and child not in (None, "")
                and not _valid_sha256(child)
            ):
                return f"invalid digest-shaped field {key!r} at {child_path}"
            field_reason = _secret_field_reason(str(key))
            if field_reason is not None and child not in (None, "", "[redacted]"):
                return f"{field_reason} at {child_path}"
            child_reason = _structured_secret_reason(child, child_path, depth + 1)
            if child_reason is not None:
                return child_reason
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            child_reason = _structured_secret_reason(
                child,
                f"{path}[{index}]",
                depth + 1,
            )
            if child_reason is not None:
                return child_reason
        return None
    if isinstance(value, str):
        label = _secret_pattern_reason(value.encode("utf-8"))
        if label is not None:
            return f"{label} at {path}"
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                if decoded != value:
                    return _structured_secret_reason(
                        decoded,
                        f"{path}.<json>",
                        depth + 1,
                    )
    return None


def _content_policy_reason(content: bytes) -> tuple[str, bool] | None:
    pattern_reason = _secret_pattern_reason(content)
    if pattern_reason is not None:
        return pattern_reason, True
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "opaque non-UTF-8 bytes", False

    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for candidate in candidates:
        structured_reason = _structured_secret_reason(candidate)
        if structured_reason is not None:
            return structured_reason, True

    for match in SECRET_ASSIGNMENT_PATTERN.finditer(text):
        field_reason = _secret_field_reason(match.group(1))
        value = match.group(2)
        if field_reason is not None and value.lower() not in {
            "none",
            "null",
            "redacted",
            "[redacted]",
        }:
            return field_reason, True
    return None


def _sqlite_value_policy_reason(column: str, value: object) -> str | None:
    if value is None:
        return None
    column_pattern = _secret_pattern_reason(column.encode("utf-8"))
    if column_pattern is not None:
        return f"{column_pattern} in field name"
    if _is_required_sha256_field(column) and not _valid_sha256(value):
        return f"invalid SHA-256 value in digest field {column!r}"
    field_reason = _secret_field_reason(column)
    if field_reason is not None and value not in {"", b""}:
        return field_reason
    if isinstance(value, bytes):
        result = _content_policy_reason(value)
    elif isinstance(value, str):
        result = _content_policy_reason(value.encode("utf-8"))
    else:
        return None
    if result is None:
        return None
    return result[0]


def _schema_version(db: sqlite3.Connection) -> int:
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    except sqlite3.Error as exc:
        raise MigrationRejected("root is not a supported legacy journal") from exc
    if row is None:
        raise MigrationRejected("journal schema version is missing")
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationRejected("journal schema version is invalid") from exc


def _high_water_event_id(db: sqlite3.Connection) -> int:
    try:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    except sqlite3.Error as exc:
        raise MigrationRejected("legacy event journal is missing") from exc


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _legacy_process_pid_candidates(db: sqlite3.Connection) -> list[tuple[str, int]]:
    active_states = ("created", "running", "paused", "blocked")
    placeholders = ",".join("?" for _ in active_states)
    rows = db.execute(
        f"""
        SELECT id, pid
        FROM processes
        WHERE pid IS NOT NULL AND state IN ({placeholders})
        ORDER BY id
        """,
        active_states,
    ).fetchall()
    return [(str(process_id), int(pid)) for process_id, pid in rows]


def _live_process_candidates(
    candidates: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    return [(process_id, pid) for process_id, pid in candidates if _pid_is_live(pid)]


def _live_legacy_processes(db: sqlite3.Connection) -> list[tuple[str, int]]:
    return _live_process_candidates(_legacy_process_pid_candidates(db))


def _live_legacy_daemon(root: Path) -> int | None:
    marker = root / "aosd.json"
    if not marker.exists():
        return None
    if marker.is_symlink() or not marker.is_file():
        raise MigrationRejected("legacy daemon marker is not a regular file")
    try:
        payload = json.loads(marker.read_text())
        pid = int(payload["pid"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationRejected("legacy daemon marker is invalid") from exc
    return pid if _pid_is_live(pid) else None


def _structural_fingerprint(db: sqlite3.Connection) -> str:
    rows = [
        {
            "name": str(row[1]),
            "sql": str(row[3] or ""),
            "table": str(row[2]),
            "type": str(row[0]),
        }
        for row in db.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        )
    ]
    return hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()


def _database_path_identity(db_path: Path) -> tuple[int, int]:
    try:
        metadata = db_path.lstat()
    except FileNotFoundError as exc:
        raise MigrationRejected(f"journal is not a regular file: {db_path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationRejected(f"journal is not a regular file: {db_path}")
    if metadata.st_nlink != 1:
        raise MigrationRejected("journal database must not be hard-linked")
    return int(metadata.st_dev), int(metadata.st_ino)


def _require_database_path_identity(
    db_path: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        observed_identity = _database_path_identity(db_path)
    except MigrationRejected as exc:
        raise MigrationRejected("journal database pathname changed during migration") from exc
    if observed_identity != expected_identity:
        raise MigrationRejected("journal database pathname changed during migration")


def _unlink_path_if_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    """Remove only the regular file identified by a previously held descriptor."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino)) != expected_identity
    ):
        return False
    path.unlink()
    return True


def _validate_sqlite_source_files(db_path: Path) -> tuple[int, int]:
    identity = _database_path_identity(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.is_symlink():
            raise MigrationRejected(f"journal {suffix[1:]} sidecar is a symbolic link")
        if sidecar.exists() and (not sidecar.is_file() or sidecar.stat().st_nlink != 1):
            raise MigrationRejected(f"journal {suffix[1:]} sidecar is not a private regular file")
    rollback_journal = Path(f"{db_path}-journal")
    if rollback_journal.is_symlink():
        raise MigrationRejected("journal rollback sidecar is a symbolic link")
    if rollback_journal.exists():
        raise MigrationRejected(
            "journal rollback sidecar must be recovered before migration or inspection"
        )
    return identity


@contextmanager
def _readonly_database_snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Read a SQLite database without letting SQLite write beside the source."""

    def signature() -> tuple[tuple[str, int, int, int], ...]:
        _validate_sqlite_source_files(db_path)
        values: list[tuple[str, int, int, int]] = []
        for candidate in (db_path, Path(f"{db_path}-wal")):
            if not candidate.exists():
                values.append((candidate.name, -1, -1, -1))
                continue
            metadata = candidate.stat()
            values.append(
                (
                    candidate.name,
                    int(metadata.st_ino),
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                )
            )
        return tuple(values)

    with tempfile.TemporaryDirectory(prefix="aos-readonly-") as temporary:
        snapshot_path: Path | None = None
        for attempt in range(5):
            before = signature()
            attempt_dir = Path(temporary) / str(attempt)
            attempt_dir.mkdir(mode=0o700)
            candidate_snapshot = attempt_dir / db_path.name
            try:
                shutil.copyfile(db_path, candidate_snapshot)
                source_wal = Path(f"{db_path}-wal")
                if source_wal.exists():
                    shutil.copyfile(source_wal, Path(f"{candidate_snapshot}-wal"))
            except FileNotFoundError:
                continue
            after = signature()
            if before == after:
                snapshot_path = candidate_snapshot
                break
        if snapshot_path is None:
            raise JournalError("journal changed repeatedly during read-only snapshot")
        with sqlite3.connect(snapshot_path) as snapshot:
            snapshot.execute("PRAGMA query_only=ON")
            snapshot.execute("PRAGMA foreign_keys=ON")
            yield snapshot


def _checkpoint_and_fsync_database(
    db: sqlite3.Connection,
    db_path: Path,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    if db.in_transaction:
        raise JournalError("cannot checkpoint journal with an active transaction")
    if expected_identity is not None:
        _require_database_path_identity(db_path, expected_identity)
    journal_mode_rows = db.execute("PRAGMA journal_mode").fetchall()
    journal_mode = str(journal_mode_rows[0][0])
    if journal_mode == "wal":
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise JournalError("journal WAL checkpoint did not converge")
    if expected_identity is not None:
        _require_database_path_identity(db_path, expected_identity)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(db_path, flags)
    except OSError as exc:
        raise JournalError("could not open journal for durable synchronization") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise JournalError("journal changed before durable synchronization")
        if (
            expected_identity is not None
            and (
                int(metadata.st_dev),
                int(metadata.st_ino),
            )
            != expected_identity
        ):
            raise MigrationRejected("journal database pathname changed during migration")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if expected_identity is not None:
        _require_database_path_identity(db_path, expected_identity)
    _fsync_directory(db_path.parent)


def _verify_v2_event_closure(db: sqlite3.Connection) -> None:
    first_envelope = db.execute("SELECT MIN(event_id) FROM event_envelopes").fetchone()
    if first_envelope is None or first_envelope[0] is None:
        raise JournalError("current journal has no v2 event envelope boundary")
    missing_envelopes = db.execute(
        """
        SELECT e.id
        FROM events AS e
        LEFT JOIN event_envelopes AS envelope ON envelope.event_id=e.id
        WHERE e.id >= ? AND envelope.event_id IS NULL
        LIMIT 1
        """,
        (int(first_envelope[0]),),
    ).fetchone()
    if missing_envelopes is not None:
        raise JournalError("post-boundary event is missing its v2 envelope")
    missing_targets = db.execute(
        """
        SELECT envelope.event_id
        FROM event_envelopes AS envelope
        JOIN events AS e ON e.id=envelope.event_id
        LEFT JOIN event_targets AS target
          ON target.event_id=e.id
         AND target.aggregate_type=e.aggregate_type
         AND target.aggregate_id=e.aggregate_id
        WHERE target.event_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing_targets is not None:
        raise JournalError("v2 event envelope is missing its immutable aggregate target")
    required_triggers = {
        "event_envelopes_no_delete",
        "event_envelopes_no_update",
        "event_targets_no_delete",
        "event_targets_no_update",
        "events_no_delete",
        "events_no_update",
    }
    observed_triggers = {
        str(row[0])
        for row in db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='trigger'
            """
        ).fetchall()
    }
    if not observed_triggers >= required_triggers:
        raise JournalError("current journal append-only guards are incomplete")


def inspect_journal(root: Path) -> JournalInspection:
    """Inspect a current journal without initializing or migrating it."""

    db_path = Path(root).expanduser().resolve() / "aos.db"
    if not db_path.is_file():
        raise MigrationRejected(f"journal does not exist: {db_path}")
    with _readonly_database_snapshot(db_path) as db:
        schema = _schema_version(db)
        if schema != CURRENT_SCHEMA_VERSION:
            raise MigrationRejected(
                f"schema {schema} requires explicit migration before ordinary reads"
            )
        tables = {
            str(row[0])
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        missing = sorted(CURRENT_REQUIRED_TABLES - tables)
        if missing:
            raise MigrationRejected(f"current journal structure is incomplete: {missing}")
        pending_cleanup = db.execute(
            "SELECT operation_id FROM migration_cleanup WHERE status='pending'"
        ).fetchall()
        if pending_cleanup:
            raise MigrationRejected(
                "journal has pending migration cleanup and is not readable as canonical v2 state"
            )
        _verify_v2_event_closure(db)
        _verify_canonical_cas_closure(db, db_path.parent)
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise JournalError(f"journal integrity check failed: {integrity}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise JournalError("journal foreign-key check failed")
        high_water = _high_water_event_id(db)
    return JournalInspection(
        schema_version=schema,
        event_high_water_event_id=high_water,
        integrity=integrity,
    )


def _snapshot_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise MigrationRejected(f"unsupported legacy SQLite value: {type(value).__name__}")


def _migration_snapshot(
    db: sqlite3.Connection,
    source_schema: int,
    legacy_high_water: int,
    artifacts: list[_LegacyArtifact],
) -> tuple[bytes, str, list[tuple[str, str]]]:
    event_columns = (
        "occurred_at",
        "kind",
        "severity",
        "aggregate_type",
        "aggregate_id",
        "payload",
    )
    for event in db.execute(
        """
        SELECT id, occurred_at, kind, severity, aggregate_type, aggregate_id, payload
        FROM events
        ORDER BY id
        """
    ):
        event_id = event[0]
        for column, value in zip(event_columns, event[1:], strict=True):
            policy_reason = _sqlite_value_policy_reason(column, value)
            if policy_reason is not None:
                raise MigrationRejected(
                    "legacy event history contains policy-unsafe data that cannot "
                    "cross the migration boundary: "
                    f"event {event_id} column {column}: {policy_reason}"
                )

    for key, value in db.execute("SELECT key, value FROM metadata ORDER BY key"):
        policy_reason = _sqlite_value_policy_reason(str(key), value)
        if policy_reason is not None:
            raise MigrationRejected(
                "legacy metadata contains policy-unsafe data that cannot cross "
                f"the migration boundary: {policy_reason}"
            )

    cloned_projection_db = sqlite3.connect(":memory:")
    cloned_projection_db.executescript("\n".join(db.iterdump()))
    if source_schema == 1:
        _backfill_v1_durable_objects(cloned_projection_db)
    else:
        _backfill_attempt_ceiling(cloned_projection_db)
    projection_db = cloned_projection_db

    try:
        projections: dict[str, object] = {}
        revisions: list[tuple[str, str]] = []
        for table in LEGACY_PROJECTION_TABLES[2]:
            columns = [
                str(row[1])
                for row in projection_db.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            rows: list[list[object]] = []
            for row_number, row in enumerate(
                projection_db.execute(f'SELECT * FROM "{table}"').fetchall(),
                start=1,
            ):
                row_by_column = dict(zip(columns, row, strict=True))
                if "content" in row_by_column and "content_hash" in row_by_column:
                    content = row_by_column["content"]
                    content_hash = row_by_column["content_hash"]
                    if (
                        not isinstance(content, str)
                        or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash
                    ):
                        raise MigrationRejected(
                            "legacy durable content hash does not match its content: "
                            f"{table} row {row_number}"
                        )
                snapshot_row: list[object] = []
                for column, value in zip(columns, row, strict=True):
                    policy_reason = _sqlite_value_policy_reason(column, value)
                    if policy_reason is not None:
                        raise MigrationRejected(
                            "legacy projection contains policy-unsafe data that cannot "
                            "cross the migration boundary: "
                            f"{table} row {row_number} column {column}: {policy_reason}"
                        )
                    snapshot_row.append(_snapshot_value(value))
                rows.append(snapshot_row)
            rows.sort(key=_canonical_json)
            projections[table] = {"columns": columns, "rows": rows}

            aggregate = CONTESTED_AGGREGATES.get(table)
            if aggregate is not None:
                aggregate_type, id_column = aggregate
                id_index = columns.index(id_column)
                revisions.extend((aggregate_type, str(row[id_index])) for row in rows)
    finally:
        cloned_projection_db.close()

    artifact_manifest = [
        {
            "blob_digest": artifact.blob_digest,
            "digest": artifact.digest,
            "legacy_path": artifact.legacy_path,
            "quarantine_reason": artifact.quarantine_reason,
            "size": artifact.size,
            "status": artifact.status,
        }
        for artifact in artifacts
    ]

    content = (
        _canonical_json(
            {
                "classifier_version": "aos.migration-policy.v1",
                "format": "aos.migration-snapshot.v2",
                "legacy_artifacts": artifact_manifest,
                "legacy_high_water_event_id": legacy_high_water,
                "projections": projections,
                "source_schema": source_schema,
            }
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    return content, digest, sorted(revisions)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, anchor: Path, label: str) -> None:
    """Create a directory beneath an anchor without accepting symlink parents."""

    anchor = anchor.resolve(strict=True)
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise JournalError(f"{label} escapes its trusted anchor: {path}") from exc
    current = anchor
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise JournalError(f"{label} contains a symbolic link: {current}")
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            created = False
        else:
            created = True
        if current.is_symlink() or not current.is_dir():
            raise JournalError(f"{label} is not a trusted directory: {current}")
        if created:
            _fsync_directory(current.parent)


def _validate_parent_chain(path: Path, anchor: Path, label: str) -> None:
    anchor = anchor.resolve(strict=True)
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise JournalError(f"{label} escapes its trusted anchor: {path}") from exc
    current = anchor
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise JournalError(f"{label} contains a symbolic link: {current}")
        if current.exists() and not current.is_dir():
            raise JournalError(f"{label} parent is not a directory: {current}")


def _validate_path_chain(path: Path, anchor: Path, label: str) -> None:
    """Reject an existing symlink/non-directory anywhere above a future file."""

    _validate_parent_chain(path, anchor, label)
    if path.is_symlink():
        raise JournalError(f"{label} is a symbolic link: {path}")


def _verified_regular_file(
    path: Path,
    digest: str,
    label: str,
    anchor: Path,
) -> None:
    _validate_path_chain(path, anchor, label)
    if path.is_symlink() or not path.is_file():
        raise JournalError(f"{label} is missing or not a regular file: {path}")
    if path.stat().st_nlink != 1:
        raise JournalError(f"{label} must not be hard-linked: {path}")
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != digest:
        raise JournalError(f"{label} digest mismatch: {path}")


def _verified_private_regular_file(
    path: Path,
    digest: str,
    label: str,
    anchor: Path,
) -> None:
    _verified_regular_file(path, digest, label, anchor)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o077:
        raise JournalError(f"{label} is not private to the current user: {path}")


def _verify_private_directory(path: Path, label: str) -> None:
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise JournalError(f"{label} is not private to the current user: {path}")


def _remove_temporary_sqlite_files(path: Path, *, include_main: bool) -> None:
    candidates = [path] if include_main else []
    candidates.extend(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    for candidate in candidates:
        if candidate.is_symlink():
            raise JournalError(f"temporary SQLite path is a symbolic link: {candidate}")
        if not candidate.exists():
            continue
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise JournalError(f"temporary SQLite path is not a private regular file: {candidate}")
        candidate.unlink()


def _blob_path(root: Path, digest: str) -> Path:
    if SHA256_HEX_PATTERN.fullmatch(digest) is None:
        raise JournalError("content-addressed digest is not canonical SHA-256")
    return root / "blobs" / "sha256" / digest[:2] / digest


def _backup_path(root: Path, operation_id: str, snapshot_digest: str) -> Path:
    if SHA256_HEX_PATTERN.fullmatch(snapshot_digest) is None:
        raise JournalError("backup snapshot digest is not canonical SHA-256")
    root_identity = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    operation_identity = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:16]
    return (
        root.parent
        / ".aos-backups"
        / root_identity
        / f"{operation_identity}-{snapshot_digest[:16]}.sqlite3"
    )


def _quarantine_path(root: Path, digest: str) -> Path:
    if SHA256_HEX_PATTERN.fullmatch(digest) is None:
        raise JournalError("quarantine digest is not canonical SHA-256")
    root_identity = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return root.parent / ".aos-quarantine" / root_identity / digest


def _publish_blob(root: Path, content: bytes, digest: str) -> Path:
    blobs_root = root / "blobs"
    staging = blobs_root / ".staging"
    destination_dir = blobs_root / "sha256" / digest[:2]
    _ensure_directory(staging, root, "blob staging directory")
    _ensure_directory(destination_dir, root, "blob destination directory")
    destination = destination_dir / digest
    if destination.is_symlink():
        raise JournalError(f"content-addressed blob is a symbolic link: {destination}")
    if destination.exists():
        _verified_regular_file(destination, digest, "content-addressed blob", root)
        return destination

    staged = staging / f"{digest}.tmp"
    for attempt in range(2):
        try:
            with staged.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            break
        except FileExistsError:
            if (
                not staged.is_symlink()
                and staged.is_file()
                and staged.stat().st_nlink == 1
                and hashlib.sha256(staged.read_bytes()).hexdigest() == digest
            ):
                break
            staged.unlink(missing_ok=True)
            if attempt == 1:
                raise JournalError(f"could not replace partial staged blob: {staged}") from None
    os.replace(staged, destination)
    _fsync_directory(destination_dir)
    _verified_regular_file(destination, digest, "content-addressed blob", root)
    return destination


def _backup_legacy_database(
    db: sqlite3.Connection,
    root: Path,
    operation_id: str,
    snapshot_digest: str,
) -> tuple[Path, str]:
    destination = _backup_path(root, operation_id, snapshot_digest)
    backup_dir = destination.parent
    _ensure_directory(backup_dir, root.parent, "migration backup directory")
    _verify_private_directory(
        root.parent / ".aos-backups",
        "migration backup root",
    )
    _verify_private_directory(backup_dir, "migration backup directory")
    temporary = destination.with_suffix(".sqlite3.tmp")
    if destination.is_symlink() or temporary.is_symlink():
        raise JournalError("migration backup destination is a symbolic link")
    if destination.exists() and (not destination.is_file() or destination.stat().st_nlink != 1):
        raise JournalError("migration backup destination is not a private regular file")
    _remove_temporary_sqlite_files(temporary, include_main=True)
    try:
        with sqlite3.connect(temporary) as backup:
            db.backup(backup)
            backup.commit()
            journal_mode = str(backup.execute("PRAGMA journal_mode").fetchone()[0])
            if journal_mode == "wal":
                checkpoint = backup.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise JournalError("migration backup WAL checkpoint did not converge")
                journal_mode = str(backup.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
            if journal_mode != "delete":
                raise JournalError(
                    f"migration backup journal mode is not standalone: {journal_mode}"
                )
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise JournalError(f"migration backup integrity check failed: {integrity}")
            if backup.execute("PRAGMA foreign_key_check").fetchall():
                raise JournalError("migration backup foreign-key check failed")
        _remove_temporary_sqlite_files(temporary, include_main=False)
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(backup_dir)
    except Exception:
        _remove_temporary_sqlite_files(temporary, include_main=True)
        raise
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    _verified_private_regular_file(
        destination,
        digest,
        "migration backup",
        root.parent,
    )
    return destination, digest


def _referenced_artifact_paths(db: sqlite3.Connection) -> set[str]:
    references: set[str] = set()

    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key is not None:
            normalized_key = key.lower()
            if normalized_key == "artifact" or normalized_key.endswith("_artifact_path"):
                references.add(value)

    for row in db.execute("SELECT payload FROM events ORDER BY id"):
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        visit(payload)
    return references


def _artifact_relative_path(reference: str) -> str | None:
    normalized = reference.replace("\\", "/")
    if "/artifacts/" in normalized:
        suffix = normalized.split("/artifacts/", 1)[1]
        candidate = f"artifacts/{suffix}"
    elif normalized.startswith("artifacts/"):
        candidate = normalized
    else:
        return None
    if any(part in {"", ".", ".."} for part in candidate.split("/")):
        return None
    return candidate


def _assert_publishable_legacy_path(value: str, label: str) -> None:
    encoded = os.fsencode(value)
    policy = _content_policy_reason(encoded)
    if policy is None:
        return
    path_digest = hashlib.sha256(encoded).hexdigest()
    raise MigrationRejected(
        f"{label} contains policy-unsafe text ({policy[0]}); path digest {path_digest}"
    )


def _legacy_artifacts(
    root: Path,
    references: set[str],
) -> list[_LegacyArtifact]:
    artifacts_root = root / "artifacts"
    records: list[_LegacyArtifact] = []
    if artifacts_root.is_symlink():
        raise MigrationRejected("legacy artifact root is a symbolic link")
    if artifacts_root.exists() and not artifacts_root.is_dir():
        raise MigrationRejected("legacy artifact root is not a directory")

    def visit(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            _assert_publishable_legacy_path(relative, "legacy artifact path")
            if entry.is_symlink():
                target = os.readlink(path).encode("utf-8")
                records.append(
                    _LegacyArtifact(
                        legacy_path=relative,
                        digest=hashlib.sha256(target).hexdigest(),
                        size=len(target),
                        status="quarantined",
                        blob_digest=None,
                        quarantine_reason=("policy-unsafe artifact matched symbolic link policy"),
                        content=target,
                        source_path=relative,
                    )
                )
            elif entry.is_dir(follow_symlinks=False):
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                policy = _content_policy_reason(content)
                if policy is not None:
                    policy_label, is_secret = policy
                    records.append(
                        _LegacyArtifact(
                            legacy_path=relative,
                            digest=digest,
                            size=len(content),
                            status="quarantined",
                            blob_digest=None,
                            quarantine_reason=(
                                "secret-bearing artifact matched "
                                if is_secret
                                else "policy-unsafe artifact matched "
                            )
                            + (f"{policy_label} policy"),
                            content=content,
                            source_path=relative,
                        )
                    )
                    continue
                records.append(
                    _LegacyArtifact(
                        legacy_path=relative,
                        digest=digest,
                        size=len(content),
                        status="available",
                        blob_digest=digest,
                        quarantine_reason=None,
                        content=content,
                        source_path=relative,
                    )
                )
            else:
                raise MigrationRejected(
                    f"legacy artifact tree contains an unsupported filesystem entry: {relative}"
                )

    if artifacts_root.is_dir():
        visit(artifacts_root)
    by_source = {record.source_path: record for record in records}
    for reference in sorted(references):
        _assert_publishable_legacy_path(reference, "legacy artifact reference")
        relative = _artifact_relative_path(reference)
        source = by_source.get(relative)
        if source is None:
            records.append(
                _LegacyArtifact(
                    legacy_path=reference,
                    digest=None,
                    size=None,
                    status="missing",
                    blob_digest=None,
                    quarantine_reason="referenced legacy artifact is unavailable",
                    content=None,
                    source_path=None,
                )
            )
        elif reference != relative:
            records.append(replace(source, legacy_path=reference))
    return sorted(records, key=lambda record: record.legacy_path)


def _quarantine_artifact(root: Path, artifact: _LegacyArtifact) -> Path:
    if artifact.content is None or artifact.digest is None:
        raise JournalError("quarantined artifact is missing protected content")
    destination = _quarantine_path(root, artifact.digest)
    quarantine_dir = destination.parent
    _ensure_directory(quarantine_dir, root.parent, "artifact quarantine directory")
    _verify_private_directory(
        root.parent / ".aos-quarantine",
        "artifact quarantine root",
    )
    _verify_private_directory(quarantine_dir, "artifact quarantine directory")
    if destination.is_symlink():
        raise JournalError(f"quarantined artifact is a symbolic link: {destination}")
    if destination.exists():
        metadata = destination.stat()
        if (
            not destination.is_file()
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise JournalError(f"quarantined artifact is not a private regular file: {destination}")
        if hashlib.sha256(destination.read_bytes()).hexdigest() == artifact.digest:
            return destination

    temporary = destination.with_name(f".{artifact.digest}.tmp")
    for attempt in range(2):
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(artifact.content)
                stream.flush()
                os.fsync(stream.fileno())
            break
        except FileExistsError:
            if temporary.is_symlink() or not temporary.is_file():
                raise JournalError(
                    f"quarantine staging path is not a regular file: {temporary}"
                ) from None
            metadata = temporary.stat()
            if metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o077:
                raise JournalError(f"quarantine staging path is not private: {temporary}") from None
            if hashlib.sha256(temporary.read_bytes()).hexdigest() == artifact.digest:
                break
            temporary.unlink()
            if attempt == 1:
                raise JournalError(
                    f"could not replace partial quarantine staging file: {temporary}"
                ) from None
    os.replace(temporary, destination)
    _fsync_directory(quarantine_dir)
    _verified_private_regular_file(
        destination,
        artifact.digest,
        "quarantined artifact",
        root.parent,
    )
    return destination


def _requires_protected_quarantine(reason: object) -> bool:
    text = str(reason)
    return text.startswith("secret-bearing") or text.startswith("policy-unsafe")


def _remove_quarantined_artifact_sources(
    root: Path,
    db: sqlite3.Connection,
) -> None:
    rows = db.execute(
        """
        SELECT legacy_path, digest, quarantine_reason
        FROM legacy_artifacts
        WHERE status='quarantined'
        ORDER BY legacy_path
        """
    ).fetchall()
    for legacy_path, digest, reason in rows:
        relative = _artifact_relative_path(str(legacy_path))
        if relative is None:
            continue
        source = root / relative
        _validate_parent_chain(source, root, "quarantined artifact source")
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink():
            observed = os.readlink(source).encode("utf-8")
        elif source.is_file():
            observed = source.read_bytes()
        else:
            raise JournalError(f"cannot remove unsupported quarantined artifact: {source}")
        if hashlib.sha256(observed).hexdigest() != str(digest):
            raise JournalError(f"quarantined artifact changed before source removal: {source}")
        if _requires_protected_quarantine(reason):
            protected = _quarantine_path(root, str(digest))
            _verified_private_regular_file(
                protected,
                str(digest),
                "protected quarantine copy",
                root.parent,
            )
        source.unlink()
        _fsync_directory(source.parent)


def _finish_quarantine_cleanup(
    root: Path,
    db: sqlite3.Connection,
    operation_id: str,
    clock: Callable[[], datetime],
) -> None:
    cleanup = db.execute(
        """
        SELECT c.status, c.pending_items
        FROM migration_cleanup AS c
        WHERE c.operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if cleanup is None:
        raise JournalError("migration cleanup projection is missing")
    if cleanup[0] == "completed":
        return
    completed_at = clock()
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise MigrationRejected("migration clock must return a timezone-aware datetime")
    completed_at_text = completed_at.isoformat(timespec="milliseconds")
    _remove_quarantined_artifact_sources(root, db)

    try:
        db.execute("BEGIN IMMEDIATE")
        completed_event_id = _append_migration_event(
            db,
            completed_at_text,
            "migration.cleanup_completed",
            "migration",
            operation_id,
            {
                "operation_id": operation_id,
                "removed_items": int(cleanup[1]),
            },
        )
        operation = db.execute(
            """
            SELECT request_hash, admission_event_id
            FROM operations
            WHERE operation_id=?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise JournalError("migration operation projection is missing")
        _insert_event_envelope(
            db,
            event_id=completed_event_id,
            invoker_kind="migration",
            invoker_id=operation_id,
            operation_id=operation_id,
            request_hash=str(operation[0]),
            admission_id=int(operation[1]),
        )
        cursor = db.execute(
            """
            UPDATE migration_cleanup
            SET status='completed', completed_at=?, completed_event_id=?
            WHERE operation_id=? AND status='pending'
            """,
            (completed_at_text, completed_event_id, operation_id),
        )
        if cursor.rowcount != 1:
            raise JournalError("migration cleanup status changed concurrently")
        db.execute(
            """
            UPDATE operations
            SET status='completed', completed_event_id=?, completed_at=?
            WHERE operation_id=? AND status='admitted'
            """,
            (completed_event_id, completed_at_text, operation_id),
        )
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def _preflight_migration_effect_paths(
    root: Path,
    operation_id: str,
    snapshot_digest: str,
    artifacts: list[_LegacyArtifact],
) -> None:
    _validate_path_chain(
        _blob_path(root, snapshot_digest),
        root,
        "migration snapshot destination",
    )
    backup = _backup_path(root, operation_id, snapshot_digest)
    _validate_path_chain(backup, root.parent, "migration backup destination")
    _validate_path_chain(
        backup.with_suffix(".sqlite3.tmp"),
        root.parent,
        "migration backup temporary destination",
    )
    for artifact in artifacts:
        if artifact.status == "available" and artifact.blob_digest is not None:
            _validate_path_chain(
                _blob_path(root, artifact.blob_digest),
                root,
                "legacy artifact blob destination",
            )
        elif (
            artifact.status == "quarantined"
            and artifact.content is not None
            and artifact.digest is not None
        ):
            quarantine = _quarantine_path(root, artifact.digest)
            _validate_path_chain(
                quarantine,
                root.parent,
                "legacy artifact quarantine destination",
            )
            _validate_path_chain(
                quarantine.with_name(f".{artifact.digest}.tmp"),
                root.parent,
                "legacy artifact quarantine staging path",
            )


def _verify_backup_database(path: Path, digest: str, anchor: Path) -> None:
    _verified_private_regular_file(path, digest, "migration backup", anchor)
    with _readonly_database_snapshot(path) as backup:
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise JournalError(f"migration backup integrity check failed: {integrity}")
        if backup.execute("PRAGMA foreign_key_check").fetchall():
            raise JournalError("migration backup foreign-key check failed")


def _verify_migration_effects(
    root: Path,
    operation_id: str,
    snapshot_digest: str,
    backup_digest: str,
    artifacts: list[_LegacyArtifact],
) -> None:
    _verified_regular_file(
        _blob_path(root, snapshot_digest),
        snapshot_digest,
        "migration snapshot blob",
        root,
    )
    _verify_backup_database(
        _backup_path(root, operation_id, snapshot_digest),
        backup_digest,
        root.parent,
    )
    for artifact in artifacts:
        if artifact.status == "available" and artifact.blob_digest is not None:
            _verified_regular_file(
                _blob_path(root, artifact.blob_digest),
                artifact.blob_digest,
                f"legacy artifact blob {artifact.legacy_path}",
                root,
            )
        elif (
            artifact.status == "quarantined"
            and artifact.content is not None
            and artifact.digest is not None
        ):
            _verified_private_regular_file(
                _quarantine_path(root, artifact.digest),
                artifact.digest,
                f"legacy artifact quarantine {artifact.legacy_path}",
                root.parent,
            )


def _verify_canonical_cas_closure(
    db: sqlite3.Connection,
    root: Path,
) -> None:
    snapshot_digests = {
        str(row[0]) for row in db.execute("SELECT digest FROM migration_snapshots ORDER BY digest")
    }
    artifact_digests = {
        str(row[0])
        for row in db.execute(
            """
            SELECT blob_digest
            FROM legacy_artifacts
            WHERE status='available'
            ORDER BY blob_digest
            """
        )
    }
    for digest in sorted(snapshot_digests | artifact_digests):
        _verified_regular_file(
            _blob_path(root, digest),
            digest,
            "canonical replay-critical blob",
            root,
        )


def _verify_recorded_migration_effects(
    db: sqlite3.Connection,
    root: Path,
    operation_id: str,
    snapshot_digest: str,
    backup_digest: str,
) -> None:
    _verified_regular_file(
        _blob_path(root, snapshot_digest),
        snapshot_digest,
        "migration snapshot blob",
        root,
    )
    _verify_backup_database(
        _backup_path(root, operation_id, snapshot_digest),
        backup_digest,
        root.parent,
    )
    rows = db.execute(
        """
        SELECT legacy_path, digest, status, blob_digest, quarantine_reason
        FROM legacy_artifacts
        ORDER BY legacy_path
        """
    ).fetchall()
    for legacy_path, digest, status, blob_digest, reason in rows:
        if status == "available":
            _verified_regular_file(
                _blob_path(root, str(blob_digest)),
                str(blob_digest),
                f"legacy artifact blob {legacy_path}",
                root,
            )
        elif status == "quarantined" and _requires_protected_quarantine(reason):
            _verified_private_regular_file(
                _quarantine_path(root, str(digest)),
                str(digest),
                f"legacy artifact quarantine {legacy_path}",
                root.parent,
            )


def _create_empty_base_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE leases (
            kind TEXT PRIMARY KEY,
            holder TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE missions (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            objective TEXT NOT NULL,
            state TEXT NOT NULL,
            allowed_backends TEXT NOT NULL,
            allowed_models TEXT NOT NULL,
            allow_anthropic INTEGER NOT NULL DEFAULT 0,
            verify_command TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE processes (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            attempt INTEGER NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            state TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            token_expires_at TEXT NOT NULL,
            worktree TEXT,
            session_id TEXT,
            pid INTEGER,
            checkpoint TEXT NOT NULL DEFAULT '{}',
            routing_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE inbox_cursors (
            consumer TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE INDEX events_aggregate
        ON events(aggregate_type, aggregate_id, id)
        """
    )
    db.execute(
        """
        CREATE INDEX processes_mission
        ON processes(mission_id, attempt)
        """
    )


def _create_durable_projection_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE contract_revisions (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            revision INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempt_ceiling INTEGER NOT NULL DEFAULT 3 CHECK(attempt_ceiling >= 1),
            UNIQUE(mission_id, revision)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            number INTEGER NOT NULL,
            state TEXT NOT NULL,
            worktree TEXT NOT NULL,
            branch TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(mission_id, number)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE work_orders (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            process_id TEXT NOT NULL REFERENCES processes(id),
            revision INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(process_id, revision)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE worker_sessions (
            id TEXT PRIMARY KEY,
            process_id TEXT NOT NULL REFERENCES processes(id),
            sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            backend_handle TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            UNIQUE(process_id, sequence)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE provider_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backend TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            observed_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX worker_sessions_process ON worker_sessions(process_id, sequence)")
    db.execute(
        """
        CREATE INDEX provider_observations_backend
        ON provider_observations(backend, observed_at)
        """
    )


def _backfill_attempt_ceiling(db: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in db.execute('PRAGMA table_info("contract_revisions")').fetchall()
    }
    if "attempt_ceiling" in columns:
        return
    db.execute(
        """
        ALTER TABLE contract_revisions
        ADD COLUMN attempt_ceiling INTEGER NOT NULL DEFAULT 3
        CHECK(attempt_ceiling >= 1)
        """
    )


def _deterministic_legacy_id(prefix: str, *parts: object, length: int) -> str:
    encoded = _canonical_json([str(part) for part in parts]).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()[:length].upper()


def _legacy_content_hash(content: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _backfill_v1_durable_objects(
    db: sqlite3.Connection,
) -> list[tuple[str, str]]:
    _create_durable_projection_tables(db)
    created: list[tuple[str, str]] = []
    missions = db.execute("SELECT * FROM missions ORDER BY id").fetchall()
    mission_columns = [
        str(row[1]) for row in db.execute('PRAGMA table_info("missions")').fetchall()
    ]
    mission_by_id = {
        str(row[mission_columns.index("id")]): dict(zip(mission_columns, row, strict=True))
        for row in missions
    }
    for mission_id, mission in mission_by_id.items():
        content = {
            "schema_version": 1,
            "objective": mission["objective"],
            "project": mission["project"],
            "allowed_backends": json.loads(str(mission["allowed_backends"])),
            "allowed_models": json.loads(str(mission["allowed_models"])),
            "allow_anthropic": bool(mission["allow_anthropic"]),
            "verification_command": mission["verify_command"],
        }
        encoded, digest = _legacy_content_hash(content)
        contract_id = _deterministic_legacy_id("C-", mission_id, digest, length=12)
        db.execute(
            """
            INSERT INTO contract_revisions(
                id, mission_id, revision, content, content_hash, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                contract_id,
                mission_id,
                encoded,
                digest,
                mission["created_at"],
            ),
        )
        created.append(("contract_revision", contract_id))

    process_columns = [
        str(row[1]) for row in db.execute('PRAGMA table_info("processes")').fetchall()
    ]
    processes = [
        dict(zip(process_columns, row, strict=True))
        for row in db.execute("SELECT * FROM processes ORDER BY id").fetchall()
    ]
    active_states = {"created", "running", "paused", "blocked"}
    attempt_groups: dict[tuple[str, int], list[dict[str, object]]] = {}
    for process in processes:
        attempt_groups.setdefault(
            (str(process["mission_id"]), int(process["attempt"])),
            [],
        ).append(process)
    for (mission_id, attempt_number), group in sorted(attempt_groups.items()):
        current = max(
            group,
            key=lambda process: (
                str(process["updated_at"]),
                str(process["created_at"]),
                str(process["id"]),
            ),
        )
        active = [process for process in group if str(process["state"]) in active_states]
        attempt_state = "active" if active else str(current["state"])
        attempt_id = _deterministic_legacy_id("A-", mission_id, attempt_number, length=8)
        db.execute(
            """
            INSERT INTO attempts(
                id, mission_id, number, state, worktree, branch, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                mission_id,
                attempt_number,
                attempt_state,
                current["worktree"] or "",
                f"legacy/{mission_id.lower()}/attempt-{attempt_number}",
                min(str(process["created_at"]) for process in group),
                max(str(process["updated_at"]) for process in group),
            ),
        )
        created.append(("attempt", attempt_id))

    for process in processes:
        mission_id = str(process["mission_id"])
        process_id = str(process["id"])

        contract = db.execute(
            """
            SELECT revision, content_hash
            FROM contract_revisions
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        mission = mission_by_id[mission_id]
        work_order_content = {
            "schema_version": 1,
            "mission_id": mission_id,
            "process_id": process_id,
            "contract_revision": int(contract[0]),
            "contract_hash": str(contract[1]),
            "objective": mission["objective"],
            "verification_command": mission["verify_command"],
            "migration_note": "reconstructed from 0.1 projection",
        }
        encoded, digest = _legacy_content_hash(work_order_content)
        work_order_id = _deterministic_legacy_id("W-", process_id, digest, length=12)
        db.execute(
            """
            INSERT INTO work_orders(
                id, mission_id, process_id, revision, content, content_hash, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                work_order_id,
                mission_id,
                process_id,
                encoded,
                digest,
                process["created_at"],
            ),
        )
        created.append(("work_order", work_order_id))

        session_id = _deterministic_legacy_id(
            "S-", process_id, process["session_id"] or "", length=10
        )
        terminal = str(process["state"]) in {
            "completed",
            "failed",
            "cancelled",
            "orphaned",
        }
        db.execute(
            """
            INSERT INTO worker_sessions(
                id, process_id, sequence, state, backend_handle,
                started_at, ended_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                session_id,
                process_id,
                process["state"],
                process["session_id"],
                process["created_at"],
                process["updated_at"] if terminal else None,
            ),
        )
        db.execute(
            "UPDATE processes SET session_id=? WHERE id=?",
            (session_id, process_id),
        )
        created.append(("worker_session", session_id))
    return sorted(created)


def _create_migration_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE migration_snapshots (
            digest TEXT PRIMARY KEY,
            format TEXT NOT NULL,
            source_schema INTEGER NOT NULL,
            legacy_high_water_event_id INTEGER NOT NULL,
            boundary_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE migration_backups (
            digest TEXT PRIMARY KEY,
            source_schema INTEGER NOT NULL,
            legacy_high_water_event_id INTEGER NOT NULL,
            snapshot_digest TEXT NOT NULL REFERENCES migration_snapshots(digest),
            boundary_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE legacy_artifacts (
            legacy_path TEXT PRIMARY KEY,
            digest TEXT,
            size INTEGER,
            status TEXT NOT NULL CHECK(status IN ('available', 'quarantined', 'missing')),
            blob_digest TEXT,
            quarantine_reason TEXT,
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            CHECK (
                (
                    status='available'
                    AND digest IS NOT NULL
                    AND size IS NOT NULL
                    AND blob_digest=digest
                    AND quarantine_reason IS NULL
                )
                OR
                (
                    status='quarantined'
                    AND digest IS NOT NULL
                    AND size IS NOT NULL
                    AND blob_digest IS NULL
                    AND quarantine_reason IS NOT NULL
                )
                OR
                (
                    status='missing'
                    AND digest IS NULL
                    AND size IS NULL
                    AND blob_digest IS NULL
                    AND quarantine_reason IS NOT NULL
                )
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE migration_cleanup (
            operation_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
            pending_items INTEGER NOT NULL CHECK(pending_items >= 0),
            completed_at TEXT,
            completed_event_id INTEGER REFERENCES events(id),
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            CHECK (
                (
                    status='pending'
                    AND completed_at IS NULL
                    AND completed_event_id IS NULL
                )
                OR (
                    status='completed'
                    AND completed_at IS NOT NULL
                    AND completed_event_id IS NOT NULL
                )
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE migration_baseline_revisions (
            snapshot_digest TEXT NOT NULL REFERENCES migration_snapshots(digest),
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY (snapshot_digest, aggregate_type, aggregate_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE aggregate_revisions (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            snapshot_digest TEXT REFERENCES migration_snapshots(digest),
            genesis_event_id INTEGER REFERENCES events(id),
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            PRIMARY KEY (aggregate_type, aggregate_id),
            CHECK (
                (snapshot_digest IS NOT NULL AND genesis_event_id IS NULL)
                OR
                (snapshot_digest IS NULL AND genesis_event_id IS NOT NULL)
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE legacy_authority_epochs (
            epoch_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('liaison', 'process')),
            subject_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            original_expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='closed'),
            closed_at TEXT NOT NULL,
            close_reason TEXT NOT NULL,
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            UNIQUE(kind, subject_id)
        )
        """
    )
    _create_v2_core_tables(db)


def _create_v2_core_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE authority_epochs (
            epoch_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(
                kind IN ('liaison', 'worker')
            ),
            subject_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('active', 'released', 'revoked', 'expired', 'closed')
            ),
            issued_event_id INTEGER NOT NULL REFERENCES events(id),
            closed_at TEXT,
            closed_event_id INTEGER REFERENCES events(id),
            close_reason TEXT,
            CHECK (
                (
                    status='active'
                    AND closed_at IS NULL
                    AND closed_event_id IS NULL
                    AND close_reason IS NULL
                )
                OR (
                    status!='active'
                    AND closed_at IS NOT NULL
                    AND closed_event_id IS NOT NULL
                    AND close_reason IS NOT NULL
                )
            )
        )
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX authority_epochs_one_active_liaison
        ON authority_epochs(kind)
        WHERE kind='liaison' AND status='active'
        """
    )
    db.execute(
        """
        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            invoker_kind TEXT NOT NULL,
            invoker_id TEXT NOT NULL,
            authority_epoch_id TEXT REFERENCES authority_epochs(epoch_id),
            status TEXT NOT NULL CHECK(
                status IN ('admitted', 'rejected', 'completed', 'failed')
            ),
            admission_event_id INTEGER REFERENCES events(id),
            rejection_event_id INTEGER REFERENCES events(id),
            completed_event_id INTEGER REFERENCES events(id),
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK (
                (status='rejected' AND rejection_event_id IS NOT NULL)
                OR (status!='rejected' AND rejection_event_id IS NULL)
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE operation_targets (
            operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('read', 'write')),
            expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
            observed_revision INTEGER NOT NULL CHECK(observed_revision >= 0),
            PRIMARY KEY (operation_id, aggregate_type, aggregate_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE operation_conflicts (
            operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            conflicting_request_hash TEXT NOT NULL,
            rejection_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
            observed_at TEXT NOT NULL,
            PRIMARY KEY (operation_id, conflicting_request_hash)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE event_envelopes (
            event_id INTEGER PRIMARY KEY REFERENCES events(id),
            envelope_version INTEGER NOT NULL CHECK(envelope_version = 1),
            payload_schema_version INTEGER NOT NULL CHECK(payload_schema_version >= 1),
            invoker_kind TEXT NOT NULL,
            invoker_id TEXT NOT NULL,
            authority_epoch_id TEXT REFERENCES authority_epochs(epoch_id),
            evaluated_expires_at TEXT,
            on_behalf_of TEXT,
            decision_source_hash TEXT,
            operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            request_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            causation_id TEXT,
            admission_id INTEGER REFERENCES events(id),
            CHECK (
                (on_behalf_of IS NULL AND decision_source_hash IS NULL)
                OR
                (on_behalf_of IS NOT NULL AND decision_source_hash IS NOT NULL)
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE event_targets (
            event_id INTEGER NOT NULL REFERENCES event_envelopes(event_id),
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('operation', 'target')),
            PRIMARY KEY (event_id, aggregate_type, aggregate_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE evidence_records (
            digest TEXT PRIMARY KEY,
            record_schema_version INTEGER NOT NULL CHECK(record_schema_version = 1),
            mission_id TEXT NOT NULL,
            mission_revision INTEGER NOT NULL CHECK(mission_revision >= 1),
            contract_revision_id TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            process_id TEXT NOT NULL,
            worker_session_id TEXT NOT NULL,
            git_head TEXT NOT NULL,
            index_digest TEXT NOT NULL,
            worktree_manifest_digest TEXT NOT NULL,
            verifier_spec_digest TEXT NOT NULL,
            command_hash TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            exit_status INTEGER NOT NULL,
            output_artifacts TEXT NOT NULL,
            source_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE approval_decisions (
            decision_event_id INTEGER PRIMARY KEY REFERENCES events(id),
            decision_schema_version INTEGER NOT NULL CHECK(decision_schema_version = 1),
            mission_id TEXT NOT NULL,
            expected_mission_revision INTEGER NOT NULL CHECK(expected_mission_revision >= 1),
            evidence_digest TEXT NOT NULL REFERENCES evidence_records(digest),
            decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
            human_principal TEXT NOT NULL,
            decision_source_hash TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TRIGGER events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER event_envelopes_no_update
        BEFORE UPDATE ON event_envelopes
        BEGIN
            SELECT RAISE(ABORT, 'event envelopes are immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER event_envelopes_no_delete
        BEFORE DELETE ON event_envelopes
        BEGIN
            SELECT RAISE(ABORT, 'event envelopes are immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER event_targets_no_update
        BEFORE UPDATE ON event_targets
        BEGIN
            SELECT RAISE(ABORT, 'event targets are immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER event_targets_no_delete
        BEFORE DELETE ON event_targets
        BEGIN
            SELECT RAISE(ABORT, 'event targets are immutable');
        END
        """
    )


def _append_migration_event(
    db: sqlite3.Connection,
    occurred_at: str,
    kind: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, object],
) -> int:
    cursor = db.execute(
        """
        INSERT INTO events(
            occurred_at, kind, severity, aggregate_type, aggregate_id, payload
        ) VALUES (?, ?, 'attention', ?, ?, ?)
        """,
        (
            occurred_at,
            kind,
            aggregate_type,
            aggregate_id,
            _canonical_json(payload),
        ),
    )
    return int(cursor.lastrowid)


def _insert_event_envelope(
    db: sqlite3.Connection,
    *,
    event_id: int,
    invoker_kind: str,
    invoker_id: str,
    operation_id: str,
    request_hash: str,
    admission_id: int | None,
    authority_epoch_id: str | None = None,
    evaluated_expires_at: str | None = None,
    on_behalf_of: str | None = None,
    decision_source_hash: str | None = None,
    payload_schema_version: int = 1,
) -> None:
    if not _valid_sha256(request_hash):
        raise JournalError("event envelope request hash is not canonical SHA-256")
    event = db.execute(
        """
        SELECT aggregate_type, aggregate_id
        FROM events
        WHERE id=?
        """,
        (event_id,),
    ).fetchone()
    if event is None:
        raise JournalError("cannot envelope a missing event")
    db.execute(
        """
        INSERT INTO event_envelopes(
            event_id, envelope_version, payload_schema_version,
            invoker_kind, invoker_id, authority_epoch_id,
            evaluated_expires_at, on_behalf_of, decision_source_hash,
            operation_id, request_key, request_hash, causation_id, admission_id
        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            payload_schema_version,
            invoker_kind,
            invoker_id,
            authority_epoch_id,
            evaluated_expires_at,
            on_behalf_of,
            decision_source_hash,
            operation_id,
            operation_id,
            request_hash,
            operation_id,
            admission_id,
        ),
    )
    db.execute(
        """
        INSERT INTO event_targets(
            event_id, aggregate_type, aggregate_id, role
        ) VALUES (?, ?, ?, ?)
        """,
        (
            event_id,
            str(event[0]),
            str(event[1]),
            "operation" if str(event[0]) in {"migration", "operation"} else "target",
        ),
    )


def _record_migration_operation_and_envelopes(
    db: sqlite3.Connection,
    *,
    operation_id: str,
    request_hash: str,
    invoker_id: str,
    admission_event_id: int | None,
    boundary_event_id: int,
    occurred_at: str,
    source_schema: int,
    legacy_high_water: int,
    snapshot_digest: str,
    backup_digest: str,
    target_schema: int = CURRENT_SCHEMA_VERSION,
) -> None:
    cleanup = db.execute(
        """
        SELECT status, completed_event_id, completed_at
        FROM migration_cleanup
        WHERE operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if cleanup is None:
        raise JournalError("migration cleanup projection is missing")
    completed = str(cleanup[0]) == "completed"
    completed_event_id = int(cleanup[1]) if completed and cleanup[1] is not None else None
    completed_at = str(cleanup[2]) if completed and cleanup[2] is not None else None
    if completed and (completed_event_id is None or completed_at is None):
        raise JournalError("completed migration cleanup projection is incomplete")
    outcome = _canonical_json(
        {
            "backup_digest": backup_digest,
            "boundary_event_id": boundary_event_id,
            "legacy_high_water_event_id": legacy_high_water,
            "snapshot_digest": snapshot_digest,
            "source_schema": source_schema,
            "target_schema": target_schema,
        }
    )
    db.execute(
        """
        INSERT INTO operations(
            operation_id, action, request_hash, invoker_kind, invoker_id,
            status, admission_event_id, completed_event_id, outcome,
            created_at, completed_at
        ) VALUES (?, 'journal.migrate', ?, 'maintenance_operator', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            request_hash,
            invoker_id,
            "completed" if completed else "admitted",
            admission_event_id,
            completed_event_id,
            outcome,
            occurred_at,
            completed_at,
        ),
    )
    db.execute(
        """
        INSERT INTO operation_targets(
            operation_id, aggregate_type, aggregate_id, role,
            expected_revision, observed_revision
        ) VALUES (?, 'migration', ?, 'write', 0, 0)
        """,
        (operation_id, operation_id),
    )
    rows = db.execute(
        """
        SELECT id, kind
        FROM events
        WHERE id >= ?
        ORDER BY id
        """,
        (admission_event_id if admission_event_id is not None else boundary_event_id,),
    ).fetchall()
    for event_id, kind in rows:
        is_admission = admission_event_id is not None and int(event_id) == admission_event_id
        _insert_event_envelope(
            db,
            event_id=int(event_id),
            invoker_kind=(
                "maintenance_operator"
                if is_admission or str(kind) == "migration.boundary"
                else "migration"
            ),
            invoker_id=(
                invoker_id if is_admission or str(kind) == "migration.boundary" else operation_id
            ),
            operation_id=operation_id,
            request_hash=request_hash,
            admission_id=(
                None if is_admission or admission_event_id is None else admission_event_id
            ),
        )


def _advance_aggregate_revision(
    db: sqlite3.Connection,
    aggregate_type: str,
    aggregate_id: str,
    source_event_id: int,
) -> None:
    cursor = db.execute(
        """
        UPDATE aggregate_revisions
        SET revision=revision+1, source_event_id=?
        WHERE aggregate_type=? AND aggregate_id=?
        """,
        (source_event_id, aggregate_type, aggregate_id),
    )
    if cursor.rowcount != 1:
        raise JournalError(f"missing aggregate revision for {aggregate_type}:{aggregate_id}")


def _legacy_epoch_id(kind: str, subject_id: str, token_digest: str) -> str:
    material = f"{kind}:{subject_id}:{token_digest}".encode()
    return "LE-" + hashlib.sha256(material).hexdigest()[:20].upper()


def _record_legacy_authority_closure(
    db: sqlite3.Connection,
    occurred_at: str,
    kind: str,
    subject_id: str,
    token_digest: str,
    original_expires_at: str,
) -> int:
    aggregate_type = "lease" if kind == "liaison" else "authority_epoch"
    event_id = _append_migration_event(
        db,
        occurred_at,
        "authority.legacy_closed",
        aggregate_type,
        subject_id,
        {
            "close_reason": "legacy writer cutover",
            "kind": kind,
            "subject_id": subject_id,
        },
    )
    db.execute(
        """
        INSERT INTO legacy_authority_epochs(
            epoch_id, kind, subject_id, token_hash, original_expires_at,
            status, closed_at, close_reason, source_event_id
        ) VALUES (?, ?, ?, ?, ?, 'closed', ?, 'legacy writer cutover', ?)
        """,
        (
            _legacy_epoch_id(kind, subject_id, token_digest),
            kind,
            subject_id,
            token_digest,
            original_expires_at,
            occurred_at,
            event_id,
        ),
    )
    return event_id


def _reconcile_legacy_runtime(
    db: sqlite3.Connection,
    source_schema: int,
    occurred_at: str,
) -> None:
    lease_rows = db.execute(
        "SELECT kind, token_hash, expires_at FROM leases ORDER BY kind"
    ).fetchall()
    for lease_kind, token_digest, expires_at in lease_rows:
        event_id = _record_legacy_authority_closure(
            db,
            occurred_at,
            "liaison",
            str(lease_kind),
            str(token_digest),
            str(expires_at),
        )
        _advance_aggregate_revision(db, "lease", str(lease_kind), event_id)
    db.execute("DELETE FROM leases")

    process_rows = db.execute(
        """
        SELECT id, token_hash, token_expires_at, state
        FROM processes
        ORDER BY id
        """
    ).fetchall()
    for process_id, token_digest, expires_at, _state in process_rows:
        _record_legacy_authority_closure(
            db,
            occurred_at,
            "process",
            str(process_id),
            str(token_digest),
            str(expires_at),
        )

    active_runtime_states = {"created", "running", "paused", "blocked"}
    for process_id, _token_digest, _expires_at, state in process_rows:
        if state not in active_runtime_states:
            continue
        db.execute(
            """
            UPDATE processes
            SET state='orphaned', token_expires_at=?, updated_at=?
            WHERE id=?
            """,
            (occurred_at, occurred_at, process_id),
        )
        event_id = _append_migration_event(
            db,
            occurred_at,
            "process.reconciled",
            "process",
            str(process_id),
            {"from": str(state), "reason": "legacy writer cutover", "to": "orphaned"},
        )
        _advance_aggregate_revision(db, "process", str(process_id), event_id)

    session_rows = db.execute("SELECT id, state FROM worker_sessions ORDER BY id").fetchall()
    for session_id, state in session_rows:
        if state not in active_runtime_states:
            continue
        db.execute(
            """
            UPDATE worker_sessions
            SET state='orphaned', ended_at=COALESCE(ended_at, ?)
            WHERE id=?
            """,
            (occurred_at, session_id),
        )
        event_id = _append_migration_event(
            db,
            occurred_at,
            "worker_session.reconciled",
            "worker_session",
            str(session_id),
            {
                "from": str(state),
                "reason": "legacy writer cutover",
                "to": "orphaned",
            },
        )
        _advance_aggregate_revision(db, "worker_session", str(session_id), event_id)

    attempt_rows = db.execute("SELECT id, state FROM attempts ORDER BY id").fetchall()
    for attempt_id, state in attempt_rows:
        if state != "active":
            continue
        db.execute(
            "UPDATE attempts SET state='orphaned', updated_at=? WHERE id=?",
            (occurred_at, attempt_id),
        )
        event_id = _append_migration_event(
            db,
            occurred_at,
            "attempt.reconciled",
            "attempt",
            str(attempt_id),
            {
                "from": str(state),
                "reason": "legacy writer cutover",
                "to": "orphaned",
            },
        )
        _advance_aggregate_revision(db, "attempt", str(attempt_id), event_id)

    terminal_mission_states = {
        "awaiting_approval",
        "delivered",
        "failed",
        "non_convergent",
        "cancelled",
    }
    mission_rows = db.execute("SELECT id, state FROM missions ORDER BY id").fetchall()
    for mission_id, state in mission_rows:
        if state in terminal_mission_states:
            continue
        db.execute(
            "UPDATE missions SET state='failed', updated_at=? WHERE id=?",
            (occurred_at, mission_id),
        )
        event_id = _append_migration_event(
            db,
            occurred_at,
            "mission.reconciled",
            "mission",
            str(mission_id),
            {"from": str(state), "reason": "legacy writer cutover", "to": "failed"},
        )
        _advance_aggregate_revision(db, "mission", str(mission_id), event_id)


def _replay_migration(
    db: sqlite3.Connection,
    root: Path,
    database_identity: tuple[int, int],
    operation_id: str,
    request_digest: str,
    clock: Callable[[], datetime],
) -> MigrationOutcome:
    db_path = root / "aos.db"
    _require_database_path_identity(db_path, database_identity)
    rows = db.execute(
        """
        SELECT id, payload
        FROM events
        WHERE kind='migration.boundary'
          AND aggregate_type='migration'
          AND aggregate_id=?
        ORDER BY id
        """,
        (operation_id,),
    ).fetchall()
    if len(rows) != 1:
        raise MigrationRejected(
            "current-schema root has no unique migration boundary for this operation"
        )
    try:
        payload = json.loads(rows[0][1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationRejected("recorded migration boundary payload is invalid") from exc
    if payload.get("request_hash") != request_digest:
        raise MigrationRejected("operation_id was already used for a different migration request")
    _verify_recorded_migration_effects(
        db,
        root,
        operation_id,
        str(payload["snapshot_digest"]),
        str(payload["backup_digest"]),
    )
    _finish_quarantine_cleanup(root, db, operation_id, clock)
    _require_database_path_identity(db_path, database_identity)
    _checkpoint_and_fsync_database(db, db_path, database_identity)
    return MigrationOutcome(
        status="replayed",
        operation_id=operation_id,
        source_schema=int(payload["source_schema"]),
        target_schema=int(payload["target_schema"]),
        legacy_high_water_event_id=int(payload["legacy_high_water_event_id"]),
        boundary_event_id=int(rows[0][0]),
        snapshot_digest=str(payload["snapshot_digest"]),
        backup_digest=str(payload["backup_digest"]),
    )


def _schema3_transition_details(db: sqlite3.Connection) -> dict[str, object]:
    tables = {
        str(row[0])
        for row in db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    if tables != SCHEMA3_REQUIRED_TABLES:
        missing = sorted(SCHEMA3_REQUIRED_TABLES - tables)
        unexpected = sorted(tables - SCHEMA3_REQUIRED_TABLES)
        raise MigrationRejected(
            f"transitional schema 3 structure mismatch; missing={missing}, unexpected={unexpected}"
        )
    revision_columns = [
        (str(row[1]), str(row[2]).upper(), int(row[3]))
        for row in db.execute("PRAGMA table_info(aggregate_revisions)").fetchall()
    ]
    if revision_columns != [
        ("aggregate_type", "TEXT", 1),
        ("aggregate_id", "TEXT", 1),
        ("revision", "INTEGER", 1),
        ("snapshot_digest", "TEXT", 1),
        ("source_event_id", "INTEGER", 1),
    ]:
        raise MigrationRejected("transitional schema 3 revision projection is not canonical")
    boundaries = db.execute(
        """
        SELECT id, aggregate_id, payload, occurred_at
        FROM events
        WHERE kind='migration.boundary'
        ORDER BY id
        """
    ).fetchall()
    if len(boundaries) != 1:
        raise MigrationRejected("transitional schema 3 requires one migration boundary")
    boundary_event_id = int(boundaries[0][0])
    try:
        payload = json.loads(str(boundaries[0][2]))
    except json.JSONDecodeError as exc:
        raise MigrationRejected("transitional migration boundary payload is invalid") from exc
    required_payload = {
        "backup_digest",
        "invoker",
        "legacy_high_water_event_id",
        "operation_id",
        "request_hash",
        "snapshot_digest",
        "source_schema",
        "target_schema",
    }
    if set(payload) != required_payload:
        raise MigrationRejected("transitional migration boundary payload is incomplete")
    if payload["target_schema"] != 3:
        raise MigrationRejected("transitional migration boundary does not target schema 3")
    operation_id = _required_migration_operation_id(payload["operation_id"])
    if str(boundaries[0][1]) != operation_id:
        raise MigrationRejected("transitional migration operation identity is inconsistent")
    if not _valid_sha256(payload["request_hash"]):
        raise MigrationRejected("transitional migration request hash is invalid")
    if not _valid_sha256(payload["snapshot_digest"]):
        raise MigrationRejected("transitional migration snapshot digest is invalid")
    if not _valid_sha256(payload["backup_digest"]):
        raise MigrationRejected("transitional migration backup digest is invalid")
    if type(payload["source_schema"]) is not int or payload["source_schema"] not in {1, 2}:
        raise MigrationRejected("transitional migration source schema is invalid")
    if (
        type(payload["legacy_high_water_event_id"]) is not int
        or payload["legacy_high_water_event_id"] < 0
        or boundary_event_id != payload["legacy_high_water_event_id"] + 1
    ):
        raise MigrationRejected("transitional migration boundary ordering is invalid")
    observed_structure = _structural_fingerprint(db)
    expected_structure = SCHEMA3_STRUCTURE_FINGERPRINTS[int(payload["source_schema"])]
    if observed_structure != expected_structure:
        raise MigrationRejected(
            "transitional schema 3 structure does not match the committed M1a schema"
        )
    unknown = db.execute(
        f"""
        SELECT id, kind
        FROM events
        WHERE id >= ?
          AND kind NOT IN ({",".join("?" for _ in SCHEMA3_POST_BOUNDARY_EVENT_KINDS)})
        ORDER BY id
        """,
        (boundary_event_id, *sorted(SCHEMA3_POST_BOUNDARY_EVENT_KINDS)),
    ).fetchall()
    if unknown:
        raise MigrationRejected(
            "transitional schema 3 contains an unknown post-boundary event kind"
        )
    event_columns = [str(row[1]) for row in db.execute("PRAGMA table_info(events)").fetchall()]
    for row in db.execute(
        "SELECT * FROM events WHERE id >= ? ORDER BY id",
        (boundary_event_id,),
    ).fetchall():
        for column, value in zip(event_columns, row, strict=True):
            reason = _sqlite_value_policy_reason(column, value)
            if reason is not None:
                raise MigrationRejected(
                    "transitional schema 3 contains a policy-unsafe post-boundary event"
                )
    invoker = payload.get("invoker")
    if not isinstance(invoker, dict) or set(invoker) != {"id", "kind"}:
        raise MigrationRejected("transitional migration invoker is invalid")
    if invoker.get("kind") != "maintenance_operator":
        raise MigrationRejected("transitional migration invoker kind is invalid")
    invoker_id = _required_migration_invoker_id(invoker.get("id"))
    original_request = MigrationRequest(
        operation_id=operation_id,
        invoker_id=invoker_id,
        expected_schema=int(payload["source_schema"]),
        expected_root_state="quiesced",
        expected_high_water_event_id=int(payload["legacy_high_water_event_id"]),
    )
    if _request_hash(original_request) != payload["request_hash"]:
        raise MigrationRejected("transitional migration request provenance is invalid")
    try:
        boundary_time = datetime.fromisoformat(str(boundaries[0][3]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationRejected("transitional migration boundary time is invalid") from exc
    if boundary_time.tzinfo is None or boundary_time.utcoffset() is None:
        raise MigrationRejected("transitional migration boundary time is not timezone-aware")
    return {
        "backup_digest": str(payload["backup_digest"]),
        "boundary_event_id": boundary_event_id,
        "invoker_id": invoker_id,
        "legacy_high_water_event_id": int(payload["legacy_high_water_event_id"]),
        "occurred_at": str(boundaries[0][3]),
        "operation_id": operation_id,
        "request_hash": str(payload["request_hash"]),
        "snapshot_digest": str(payload["snapshot_digest"]),
        "source_schema": int(payload["source_schema"]),
    }


def _verify_schema3_transition_closure(
    db: sqlite3.Connection,
    root: Path,
    transition: Mapping[str, object],
) -> None:
    _verify_recorded_migration_effects(
        db,
        root,
        str(transition["operation_id"]),
        str(transition["snapshot_digest"]),
        str(transition["backup_digest"]),
    )
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise JournalError("transitional schema 3 integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise JournalError("transitional schema 3 foreign-key check failed")


def _schema3_upgrade_high_water_matches(
    db: sqlite3.Connection,
    transition: Mapping[str, object],
    expected_high_water_event_id: int,
) -> bool:
    observed = _high_water_event_id(db)
    if observed == expected_high_water_event_id:
        return True
    cleanup = db.execute(
        """
        SELECT status, completed_event_id
        FROM migration_cleanup
        WHERE operation_id=?
        """,
        (str(transition["operation_id"]),),
    ).fetchone()
    if (
        cleanup is None
        or str(cleanup[0]) != "completed"
        or cleanup[1] is None
        or int(cleanup[1]) != observed
        or expected_high_water_event_id + 1 != observed
    ):
        return False
    event = db.execute(
        """
        SELECT kind, aggregate_type, aggregate_id
        FROM events
        WHERE id=?
        """,
        (observed,),
    ).fetchone()
    return event == (
        "migration.cleanup_completed",
        "migration",
        str(transition["operation_id"]),
    )


def _finish_schema3_quarantine_cleanup(
    root: Path,
    db: sqlite3.Connection,
    transition: Mapping[str, object],
    clock: Callable[[], datetime],
) -> bool:
    """Complete an admitted M1a cleanup without inventing a v2 admission envelope."""

    operation_id = str(transition["operation_id"])
    cleanup = db.execute(
        """
        SELECT status, pending_items
        FROM migration_cleanup
        WHERE operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if cleanup is None:
        raise JournalError("transitional migration cleanup projection is missing")
    if str(cleanup[0]) == "completed":
        return False
    completed_at = clock()
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise MigrationRejected("migration clock must return a timezone-aware datetime")
    completed_at_text = completed_at.isoformat(timespec="milliseconds")
    _remove_quarantined_artifact_sources(root, db)

    try:
        db.execute("BEGIN IMMEDIATE")
        if _schema3_transition_details(db) != dict(transition):
            raise MigrationRejected("transitional journal changed during cleanup recovery")
        status = db.execute(
            """
            SELECT status
            FROM migration_cleanup
            WHERE operation_id=?
            """,
            (operation_id,),
        ).fetchone()
        if status is None or str(status[0]) != "pending":
            raise MigrationRejected("transitional migration cleanup status changed")
        completed_event_id = _append_migration_event(
            db,
            completed_at_text,
            "migration.cleanup_completed",
            "migration",
            operation_id,
            {
                "operation_id": operation_id,
                "removed_items": int(cleanup[1]),
            },
        )
        cursor = db.execute(
            """
            UPDATE migration_cleanup
            SET status='completed', completed_at=?, completed_event_id=?
            WHERE operation_id=? AND status='pending'
            """,
            (completed_at_text, completed_event_id, operation_id),
        )
        if cursor.rowcount != 1:
            raise JournalError("transitional migration cleanup status changed concurrently")
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
    return True


def _upgrade_schema3_tables(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE aggregate_revisions RENAME TO aggregate_revisions_schema3")
    db.execute(
        """
        CREATE TABLE aggregate_revisions (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            snapshot_digest TEXT REFERENCES migration_snapshots(digest),
            genesis_event_id INTEGER REFERENCES events(id),
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            PRIMARY KEY (aggregate_type, aggregate_id),
            CHECK (
                (snapshot_digest IS NOT NULL AND genesis_event_id IS NULL)
                OR
                (snapshot_digest IS NULL AND genesis_event_id IS NOT NULL)
            )
        )
        """
    )
    db.execute(
        """
        INSERT INTO aggregate_revisions(
            aggregate_type, aggregate_id, revision, snapshot_digest,
            genesis_event_id, source_event_id
        )
        SELECT aggregate_type, aggregate_id, revision, snapshot_digest, NULL, source_event_id
        FROM aggregate_revisions_schema3
        """
    )
    db.execute("DROP TABLE aggregate_revisions_schema3")
    _create_v2_core_tables(db)


def _replay_schema3_upgrade(
    db: sqlite3.Connection,
    root: Path,
    database_identity: tuple[int, int],
    request: MigrationRequest,
    request_hash: str,
) -> SchemaUpgradeOutcome:
    operation = db.execute(
        """
        SELECT action, request_hash, outcome, completed_event_id
        FROM operations
        WHERE operation_id=?
        """,
        (request.operation_id,),
    ).fetchone()
    if operation is None or str(operation[0]) != "journal.upgrade_schema":
        raise MigrationRejected(
            "current-schema root has no schema-upgrade outcome for this operation"
        )
    if str(operation[1]) != request_hash:
        raise MigrationRejected("operation_id was already used for a different upgrade request")
    try:
        outcome = json.loads(str(operation[2]))
    except json.JSONDecodeError as exc:
        raise MigrationRejected("recorded schema-upgrade outcome is invalid") from exc
    tables = {
        str(row[0])
        for row in db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    if not tables >= CURRENT_REQUIRED_TABLES:
        raise JournalError("current journal structure is incomplete")
    _verify_v2_event_closure(db)
    _verify_canonical_cas_closure(db, root)
    backup_record = db.execute(
        """
        SELECT snapshot_digest
        FROM migration_backups
        WHERE digest=? AND boundary_event_id=?
        """,
        (str(outcome["backup_digest"]), int(operation[3])),
    ).fetchone()
    if backup_record is None:
        raise JournalError("recorded schema-upgrade backup projection is missing")
    _verify_backup_database(
        _backup_path(root, request.operation_id, str(backup_record[0])),
        str(outcome["backup_digest"]),
        root.parent,
    )
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise JournalError("schema-upgrade replay integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise JournalError("schema-upgrade replay foreign-key check failed")
    _require_database_path_identity(root / "aos.db", database_identity)
    _checkpoint_and_fsync_database(db, root / "aos.db", database_identity)
    return SchemaUpgradeOutcome(
        status="replayed",
        operation_id=request.operation_id,
        source_schema=int(outcome["source_schema"]),
        target_schema=int(outcome["target_schema"]),
        legacy_high_water_event_id=int(outcome["legacy_high_water_event_id"]),
        upgrade_event_id=int(operation[3]),
        backup_digest=str(outcome["backup_digest"]),
    )


def _replay_initialization(
    db: sqlite3.Connection,
    request: InitializationRequest,
    request_hash: str,
) -> InitializationOutcome:
    if _schema_version(db) != CURRENT_SCHEMA_VERSION:
        raise MigrationRejected("nonempty root requires explicit migration, not initialization")
    operation = db.execute(
        """
        SELECT action, request_hash, admission_event_id, completed_event_id
        FROM operations
        WHERE operation_id=?
        """,
        (request.operation_id,),
    ).fetchone()
    if operation is None or str(operation[0]) != "journal.initialize":
        raise MigrationRejected("initialization permanently refuses a nonempty root")
    if str(operation[1]) != request_hash:
        raise MigrationRejected(
            "operation_id was already used for a different initialization request"
        )
    if operation[2] is None or operation[3] is None:
        raise JournalError("recorded initialization outcome is incomplete")
    admission = db.execute(
        """
        SELECT e.kind, envelope.admission_id
        FROM events AS e
        JOIN event_envelopes AS envelope ON envelope.event_id=e.id
        WHERE e.id=?
        """,
        (int(operation[2]),),
    ).fetchone()
    initialized = db.execute(
        """
        SELECT e.kind, e.aggregate_type, e.aggregate_id, envelope.admission_id
        FROM events AS e
        JOIN event_envelopes AS envelope ON envelope.event_id=e.id
        WHERE e.id=?
        """,
        (int(operation[3]),),
    ).fetchone()
    if admission != ("gate.admitted", None):
        raise JournalError("recorded initialization admission is invalid")
    if initialized != (
        "journal.initialized",
        "journal",
        "root",
        int(operation[2]),
    ):
        raise JournalError("recorded initialization event is invalid")
    _verify_v2_event_closure(db)
    _verify_canonical_cas_closure(db, Path(db.execute("PRAGMA database_list").fetchone()[2]).parent)
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise JournalError("initialization replay integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise JournalError("initialization replay foreign-key check failed")
    return InitializationOutcome(
        status="replayed",
        operation_id=request.operation_id,
        schema_version=CURRENT_SCHEMA_VERSION,
        initialization_event_id=int(operation[3]),
    )


def _replay_existing_initialization(
    db_path: Path,
    request: InitializationRequest,
    request_hash: str,
) -> InitializationOutcome:
    database_identity = _validate_sqlite_source_files(db_path)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        outcome = _replay_initialization(db, request, request_hash)
        journal_mode_rows = db.execute("PRAGMA journal_mode=WAL").fetchall()
        journal_mode = str(journal_mode_rows[0][0])
        if journal_mode != "wal":
            raise JournalError("initialized journal could not enable WAL mode")
        _require_database_path_identity(db_path, database_identity)
    _require_database_path_identity(db_path, database_identity)
    with closing(sqlite3.connect(db_path)) as sync_db:
        _checkpoint_and_fsync_database(sync_db, db_path, database_identity)
    return outcome


def initialize_root(
    root: Path,
    request: InitializationRequest,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> InitializationOutcome:
    """Initialize exactly one empty root under narrow maintenance authority."""

    operation_id = _required_migration_operation_id(request.operation_id)
    invoker_id = _required_migration_invoker_id(request.invoker_id)
    if type(request.expected_schema) is not int or request.expected_schema != 0:
        raise MigrationRejected("initialization requires expected_schema=0")
    if request.expected_root_state != "empty":
        raise MigrationRejected("expected_root_state must explicitly be 'empty'")
    occurred_at = clock()
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise MigrationRejected("initialization clock must return a timezone-aware datetime")
    occurred_at_text = occurred_at.isoformat(timespec="milliseconds")
    request_hash = _initialization_request_hash(request)

    requested_root = Path(root).expanduser()
    if requested_root.is_symlink():
        raise MigrationRejected("initialization root must not be a symbolic link")
    root_path = requested_root.resolve()
    db_path = root_path / "aos.db"
    if root_path.exists():
        if not root_path.is_dir():
            raise MigrationRejected("initialization root must be a directory")
        if db_path.exists() or db_path.is_symlink():
            return _replay_existing_initialization(db_path, request, request_hash)
        if any(root_path.iterdir()):
            raise MigrationRejected("initialization permanently refuses a nonempty root")
    else:
        root_path.mkdir(parents=True, mode=0o700)
    os.chmod(root_path, 0o700)

    stage_descriptor, stage_name = tempfile.mkstemp(
        prefix=".aos-initialize-",
        suffix=".sqlite3",
        dir=root_path,
    )
    stage_path = Path(stage_name)
    os.fchmod(stage_descriptor, 0o600)
    os.close(stage_descriptor)
    stage_identity = _database_path_identity(stage_path)
    installed_identity: tuple[int, int] | None = None
    initialization_event_id: int | None = None
    try:
        with closing(sqlite3.connect(stage_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            journal_mode_rows = db.execute("PRAGMA journal_mode=DELETE").fetchall()
            journal_mode = str(journal_mode_rows[0][0])
            if journal_mode != "delete":
                raise JournalError("initialization staging could not use rollback mode")
            db.execute("BEGIN IMMEDIATE")
            _create_empty_base_tables(db)
            _create_durable_projection_tables(db)
            _create_migration_tables(db)
            db.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            admission_event_id = _append_raw_event(
                db,
                occurred_at=occurred_at_text,
                kind="gate.admitted",
                severity="info",
                aggregate_type="operation",
                aggregate_id=operation_id,
                payload={
                    "action": "journal.initialize",
                    "targets": [
                        {
                            "aggregate_id": "root",
                            "aggregate_type": "journal",
                            "expected_revision": 0,
                            "role": "write",
                        }
                    ],
                },
            )
            initialization_event_id = _append_migration_event(
                db,
                occurred_at_text,
                "journal.initialized",
                "journal",
                "root",
                {
                    "operation_id": operation_id,
                    "schema_version": CURRENT_SCHEMA_VERSION,
                },
            )
            outcome = _canonical_json(
                {
                    "initialization_event_id": initialization_event_id,
                    "schema_version": CURRENT_SCHEMA_VERSION,
                }
            )
            db.execute(
                """
                INSERT INTO operations(
                    operation_id, action, request_hash, invoker_kind, invoker_id,
                    status, admission_event_id, completed_event_id, outcome,
                    created_at, completed_at
                ) VALUES (
                    ?, 'journal.initialize', ?, 'maintenance_operator', ?,
                    'completed', ?, ?, ?, ?, ?
                )
                """,
                (
                    operation_id,
                    request_hash,
                    invoker_id,
                    admission_event_id,
                    initialization_event_id,
                    outcome,
                    occurred_at_text,
                    occurred_at_text,
                ),
            )
            db.execute(
                """
                INSERT INTO operation_targets(
                    operation_id, aggregate_type, aggregate_id, role,
                    expected_revision, observed_revision
                ) VALUES (?, 'journal', 'root', 'write', 0, 0)
                """,
                (operation_id,),
            )
            _insert_event_envelope(
                db,
                event_id=admission_event_id,
                invoker_kind="maintenance_operator",
                invoker_id=invoker_id,
                operation_id=operation_id,
                request_hash=request_hash,
                admission_id=None,
            )
            _insert_event_envelope(
                db,
                event_id=initialization_event_id,
                invoker_kind="maintenance_operator",
                invoker_id=invoker_id,
                operation_id=operation_id,
                request_hash=request_hash,
                admission_id=admission_event_id,
            )
            db.execute(
                """
                INSERT INTO aggregate_revisions(
                    aggregate_type, aggregate_id, revision, snapshot_digest,
                    genesis_event_id, source_event_id
                ) VALUES ('journal', 'root', 1, NULL, ?, ?)
                """,
                (initialization_event_id, initialization_event_id),
            )
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise JournalError("initialized journal integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise JournalError("initialized journal foreign-key check failed")
            db.commit()
            _verify_v2_event_closure(db)
            _checkpoint_and_fsync_database(db, stage_path, stage_identity)

        _remove_temporary_sqlite_files(stage_path, include_main=False)
        _require_database_path_identity(stage_path, stage_identity)
        unexpected = sorted(
            candidate.name for candidate in root_path.iterdir() if candidate != stage_path
        )
        if unexpected:
            if unexpected == ["aos.db"]:
                _unlink_path_if_identity(stage_path, stage_identity)
                return _replay_existing_initialization(db_path, request, request_hash)
            raise MigrationRejected(
                "initialization root stopped being empty before journal installation"
            )
        try:
            os.link(stage_path, db_path, follow_symlinks=False)
        except FileExistsError:
            _unlink_path_if_identity(stage_path, stage_identity)
            return _replay_existing_initialization(db_path, request, request_hash)
        installed_identity = stage_identity
        if not _unlink_path_if_identity(stage_path, stage_identity):
            raise JournalError("initialization staging database identity changed")
        _fsync_directory(root_path)
        _require_database_path_identity(db_path, installed_identity)
        replayed = _replay_existing_initialization(db_path, request, request_hash)
        if replayed.initialization_event_id != initialization_event_id:
            raise JournalError("installed initialization outcome changed")
    except Exception:
        with suppress(JournalError):
            _remove_temporary_sqlite_files(stage_path, include_main=False)
        removed = _unlink_path_if_identity(stage_path, stage_identity)
        if installed_identity is not None:
            removed = _unlink_path_if_identity(db_path, installed_identity) or removed
        if removed:
            _fsync_directory(root_path)
        raise
    if initialization_event_id is None:
        raise JournalError("initialization did not record its journal event")
    return InitializationOutcome(
        status="completed",
        operation_id=operation_id,
        schema_version=CURRENT_SCHEMA_VERSION,
        initialization_event_id=initialization_event_id,
    )


def upgrade_schema3_root(
    root: Path,
    request: MigrationRequest,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> SchemaUpgradeOutcome:
    """Explicitly upgrade the finite M1a schema-3 transition into schema 4."""

    operation_id = _required_migration_operation_id(request.operation_id)
    invoker_id = _required_migration_invoker_id(request.invoker_id)
    if request.expected_schema != 3:
        raise MigrationRejected("schema-3 upgrade requires expected_schema=3")
    if (
        type(request.expected_high_water_event_id) is not int
        or request.expected_high_water_event_id < 0
    ):
        raise MigrationRejected("expected_high_water_event_id must be a nonnegative integer")
    if request.expected_root_state != "quiesced":
        raise MigrationRejected("expected_root_state must explicitly be 'quiesced'")
    occurred_at = clock()
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise MigrationRejected("migration clock must return a timezone-aware datetime")
    occurred_at_text = occurred_at.isoformat(timespec="milliseconds")
    request_hash = _request_hash(request)
    db_path = Path(root).expanduser().resolve() / "aos.db"

    with _readonly_database_snapshot(db_path) as preflight:
        schema = _schema_version(preflight)
        if schema == CURRENT_SCHEMA_VERSION:
            pass
        elif schema != 3:
            raise MigrationRejected(f"schema-3 upgrade observed unsupported schema {schema}")
        else:
            high_water = _high_water_event_id(preflight)
            preflight_transition = _schema3_transition_details(preflight)
            if not _schema3_upgrade_high_water_matches(
                preflight,
                preflight_transition,
                request.expected_high_water_event_id,
            ):
                raise MigrationRejected(
                    "expected event high-water "
                    f"{request.expected_high_water_event_id}, observed {high_water}"
                )
            _verify_schema3_transition_closure(
                preflight,
                db_path.parent,
                preflight_transition,
            )
            if _live_legacy_processes(preflight):
                raise MigrationRejected("legacy process is still live during schema-3 upgrade")
            if _live_legacy_daemon(db_path.parent) is not None:
                raise MigrationRejected("legacy daemon is still live during schema-3 upgrade")

    database_identity = _validate_sqlite_source_files(db_path)
    with sqlite3.connect(db_path) as db:
        _require_database_path_identity(db_path, database_identity)
        db.execute("PRAGMA foreign_keys=ON")
        source_schema = _schema_version(db)
        if source_schema == CURRENT_SCHEMA_VERSION:
            return _replay_schema3_upgrade(
                db,
                db_path.parent,
                database_identity,
                request,
                request_hash,
            )
        if source_schema != 3:
            raise MigrationRejected(f"schema-3 upgrade observed unsupported schema {source_schema}")
        legacy_high_water = _high_water_event_id(db)
        transition = _schema3_transition_details(db)
        if not _schema3_upgrade_high_water_matches(
            db,
            transition,
            request.expected_high_water_event_id,
        ):
            raise MigrationRejected("journal changed during schema-3 upgrade admission")
        _verify_schema3_transition_closure(db, db_path.parent, transition)
        recovered_cleanup = _finish_schema3_quarantine_cleanup(
            db_path.parent,
            db,
            transition,
            clock,
        )
        if recovered_cleanup:
            _require_database_path_identity(db_path, database_identity)
            transition = _schema3_transition_details(db)
            _verify_schema3_transition_closure(db, db_path.parent, transition)
            if not _schema3_upgrade_high_water_matches(
                db,
                transition,
                request.expected_high_water_event_id,
            ):
                raise MigrationRejected("schema-3 cleanup recovery changed unexpected events")
            legacy_high_water = _high_water_event_id(db)
        snapshot_digest = str(transition["snapshot_digest"])
        upgrade_backup_path, backup_digest = _backup_legacy_database(
            db,
            db_path.parent,
            operation_id,
            snapshot_digest,
        )
        _verify_backup_database(
            upgrade_backup_path,
            backup_digest,
            db_path.parent.parent,
        )

        try:
            db.execute("BEGIN IMMEDIATE")
            if (
                _schema_version(db) != 3
                or _high_water_event_id(db) != legacy_high_water
                or _schema3_transition_details(db) != transition
            ):
                raise MigrationRejected("journal changed during schema-3 upgrade admission")
            _verify_schema3_transition_closure(db, db_path.parent, transition)
            _upgrade_schema3_tables(db)
            _record_migration_operation_and_envelopes(
                db,
                operation_id=str(transition["operation_id"]),
                request_hash=str(transition["request_hash"]),
                invoker_id=str(transition["invoker_id"]),
                admission_event_id=None,
                boundary_event_id=int(transition["boundary_event_id"]),
                occurred_at=str(transition["occurred_at"]),
                source_schema=int(transition["source_schema"]),
                legacy_high_water=int(transition["legacy_high_water_event_id"]),
                snapshot_digest=snapshot_digest,
                backup_digest=str(transition["backup_digest"]),
                target_schema=3,
            )
            upgrade_admission_event_id = _append_raw_event(
                db,
                occurred_at=occurred_at_text,
                kind="gate.admitted",
                severity="info",
                aggregate_type="operation",
                aggregate_id=operation_id,
                payload={
                    "action": "journal.upgrade_schema",
                    "targets": [
                        {
                            "aggregate_id": operation_id,
                            "aggregate_type": "migration",
                            "expected_revision": 0,
                            "role": "write",
                        }
                    ],
                },
            )
            upgrade_event_id = _append_migration_event(
                db,
                occurred_at_text,
                "schema.upgraded",
                "migration",
                operation_id,
                {
                    "from_schema": 3,
                    "legacy_high_water_event_id": legacy_high_water,
                    "operation_id": operation_id,
                    "to_schema": CURRENT_SCHEMA_VERSION,
                },
            )
            outcome_payload = _canonical_json(
                {
                    "backup_digest": backup_digest,
                    "legacy_high_water_event_id": legacy_high_water,
                    "source_schema": 3,
                    "target_schema": CURRENT_SCHEMA_VERSION,
                    "upgrade_event_id": upgrade_event_id,
                }
            )
            db.execute(
                """
                INSERT INTO operations(
                    operation_id, action, request_hash, invoker_kind, invoker_id,
                    status, admission_event_id, completed_event_id, outcome,
                    created_at, completed_at
                ) VALUES (
                    ?, 'journal.upgrade_schema', ?, 'maintenance_operator', ?,
                    'completed', ?, ?, ?, ?, ?
                )
                """,
                (
                    operation_id,
                    request_hash,
                    invoker_id,
                    upgrade_admission_event_id,
                    upgrade_event_id,
                    outcome_payload,
                    occurred_at_text,
                    occurred_at_text,
                ),
            )
            db.execute(
                """
                INSERT INTO operation_targets(
                    operation_id, aggregate_type, aggregate_id, role,
                    expected_revision, observed_revision
                ) VALUES (?, 'migration', ?, 'write', 0, 0)
                """,
                (operation_id, operation_id),
            )
            _insert_event_envelope(
                db,
                event_id=upgrade_admission_event_id,
                invoker_kind="maintenance_operator",
                invoker_id=invoker_id,
                operation_id=operation_id,
                request_hash=request_hash,
                admission_id=None,
            )
            _insert_event_envelope(
                db,
                event_id=upgrade_event_id,
                invoker_kind="maintenance_operator",
                invoker_id=invoker_id,
                operation_id=operation_id,
                request_hash=request_hash,
                admission_id=upgrade_admission_event_id,
            )
            db.execute(
                """
                INSERT INTO migration_backups(
                    digest, source_schema, legacy_high_water_event_id,
                    snapshot_digest, boundary_event_id, created_at
                ) VALUES (?, 3, ?, ?, ?, ?)
                """,
                (
                    backup_digest,
                    legacy_high_water,
                    snapshot_digest,
                    upgrade_event_id,
                    occurred_at_text,
                ),
            )
            db.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            _verify_v2_event_closure(db)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise JournalError("schema-3 upgrade integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise JournalError("schema-3 upgrade foreign-key check failed")
            _verify_backup_database(
                upgrade_backup_path,
                backup_digest,
                db_path.parent.parent,
            )
            _require_database_path_identity(db_path, database_identity)
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        _require_database_path_identity(db_path, database_identity)
        _verify_backup_database(
            upgrade_backup_path,
            backup_digest,
            db_path.parent.parent,
        )
        _checkpoint_and_fsync_database(db, db_path, database_identity)
    return SchemaUpgradeOutcome(
        status="completed",
        operation_id=operation_id,
        source_schema=3,
        target_schema=CURRENT_SCHEMA_VERSION,
        legacy_high_water_event_id=legacy_high_water,
        upgrade_event_id=upgrade_event_id,
        backup_digest=backup_digest,
    )


def migrate_legacy_root(
    root: Path,
    request: MigrationRequest,
    *,
    clock: Callable[[], datetime] = utc_now,
    fault_injector: Callable[[str], None] | None = None,
) -> MigrationOutcome:
    """Explicitly cross one legacy journal into the v2 schema boundary."""

    operation_id = _required_migration_operation_id(request.operation_id)
    invoker_id = _required_migration_invoker_id(request.invoker_id)
    if type(request.expected_schema) is not int or request.expected_schema <= 0:
        raise MigrationRejected("expected_schema must be a positive integer")
    if (
        type(request.expected_high_water_event_id) is not int
        or request.expected_high_water_event_id < 0
    ):
        raise MigrationRejected("expected_high_water_event_id must be a nonnegative integer")
    if request.expected_root_state != "quiesced":
        raise MigrationRejected("expected_root_state must explicitly be 'quiesced'")
    db_path = Path(root).expanduser().resolve() / "aos.db"
    if db_path.is_symlink() or not db_path.is_file():
        raise MigrationRejected(f"journal does not exist: {db_path}")

    request_digest = _request_hash(request)
    with _readonly_database_snapshot(db_path) as preflight:
        preflight_schema = _schema_version(preflight)
        if preflight_schema != CURRENT_SCHEMA_VERSION:
            preflight_high_water = _high_water_event_id(preflight)
            if preflight_schema != request.expected_schema:
                raise MigrationRejected(
                    f"expected schema {request.expected_schema}, observed {preflight_schema}"
                )
            if preflight_schema not in {1, 2}:
                raise MigrationRejected(f"unsupported legacy schema: {preflight_schema}")
            preflight_structure = _structural_fingerprint(preflight)
            expected_preflight_structure = LEGACY_STRUCTURE_FINGERPRINTS[preflight_schema]
            if preflight_structure != expected_preflight_structure:
                raise MigrationRejected(
                    "legacy schema structure does not match released "
                    f"schema {preflight_schema}: {preflight_structure}"
                )
            if preflight_high_water != request.expected_high_water_event_id:
                raise MigrationRejected(
                    "expected event high-water "
                    f"{request.expected_high_water_event_id}, "
                    f"observed {preflight_high_water}"
                )
            preflight_live_processes = _live_legacy_processes(preflight)
            if preflight_live_processes:
                raise MigrationRejected(
                    "legacy process is still live; stop writers before migration: "
                    f"{preflight_live_processes}"
                )
            preflight_daemon = _live_legacy_daemon(db_path.parent)
            if preflight_daemon is not None:
                raise MigrationRejected(
                    "legacy daemon is still live; "
                    f"stop writer pid {preflight_daemon} before migration"
                )
            preflight_references = _referenced_artifact_paths(preflight)
            preflight_artifacts = _legacy_artifacts(
                db_path.parent,
                preflight_references,
            )
            _migration_snapshot(
                preflight,
                preflight_schema,
                preflight_high_water,
                preflight_artifacts,
            )

    source_database_identity = _validate_sqlite_source_files(db_path)
    with sqlite3.connect(db_path) as db:
        _require_database_path_identity(db_path, source_database_identity)
        db.execute("PRAGMA foreign_keys=ON")
        source_schema = _schema_version(db)
        if source_schema == CURRENT_SCHEMA_VERSION:
            return _replay_migration(
                db,
                db_path.parent,
                source_database_identity,
                operation_id,
                request_digest,
                clock,
            )

        occurred_at = clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise MigrationRejected("migration clock must return a timezone-aware datetime")

        legacy_high_water = _high_water_event_id(db)
        if source_schema != request.expected_schema:
            raise MigrationRejected(
                f"expected schema {request.expected_schema}, observed {source_schema}"
            )
        if source_schema not in {1, 2}:
            raise MigrationRejected(f"unsupported legacy schema: {source_schema}")
        observed_structure = _structural_fingerprint(db)
        expected_structure = LEGACY_STRUCTURE_FINGERPRINTS[source_schema]
        if observed_structure != expected_structure:
            raise MigrationRejected(
                "legacy schema structure does not match released "
                f"schema {source_schema}: {observed_structure}"
            )
        if legacy_high_water != request.expected_high_water_event_id:
            raise MigrationRejected(
                "expected event high-water "
                f"{request.expected_high_water_event_id}, observed {legacy_high_water}"
            )
        monitored_processes = _legacy_process_pid_candidates(db)
        live_processes = _live_process_candidates(monitored_processes)
        if live_processes:
            raise MigrationRejected(
                f"legacy process is still live; stop writers before migration: {live_processes}"
            )
        live_daemon = _live_legacy_daemon(db_path.parent)
        if live_daemon is not None:
            raise MigrationRejected(
                f"legacy daemon is still live; stop writer pid {live_daemon} before migration"
            )

        root_path = db_path.parent
        artifact_references = _referenced_artifact_paths(db)
        artifacts = _legacy_artifacts(root_path, artifact_references)
        snapshot, snapshot_digest, revisions = _migration_snapshot(
            db,
            source_schema,
            legacy_high_water,
            artifacts,
        )
        _preflight_migration_effect_paths(
            root_path,
            operation_id,
            snapshot_digest,
            artifacts,
        )
        _, backup_digest = _backup_legacy_database(
            db,
            root_path,
            operation_id,
            snapshot_digest,
        )
        _publish_blob(root_path, snapshot, snapshot_digest)
        for artifact in artifacts:
            if artifact.status == "available":
                if artifact.content is None or artifact.blob_digest is None:
                    raise JournalError("available artifact is missing publication content")
                _publish_blob(root_path, artifact.content, artifact.blob_digest)
            elif artifact.status == "quarantined" and artifact.content is not None:
                _quarantine_artifact(root_path, artifact)
        if fault_injector is not None:
            fault_injector("after_effect_promotion")
        _verify_migration_effects(
            root_path,
            operation_id,
            snapshot_digest,
            backup_digest,
            artifacts,
        )

        payload = _canonical_json(
            {
                "backup_digest": backup_digest,
                "invoker": {"id": invoker_id, "kind": "maintenance_operator"},
                "legacy_high_water_event_id": legacy_high_water,
                "operation_id": operation_id,
                "request_hash": request_digest,
                "snapshot_digest": snapshot_digest,
                "source_schema": source_schema,
                "target_schema": CURRENT_SCHEMA_VERSION,
            }
        )

        try:
            db.execute("BEGIN IMMEDIATE")
            observed_schema = _schema_version(db)
            observed_high_water = _high_water_event_id(db)
            observed_structure = _structural_fingerprint(db)
            if (
                observed_schema != source_schema
                or observed_high_water != legacy_high_water
                or observed_structure != expected_structure
            ):
                raise MigrationRejected("legacy journal changed during migration admission")
            if _live_legacy_processes(db):
                raise MigrationRejected("legacy process became live during migration admission")
            if _live_legacy_daemon(root_path) is not None:
                raise MigrationRejected("legacy daemon became live during migration admission")
            confirmed_references = _referenced_artifact_paths(db)
            confirmed_artifacts = _legacy_artifacts(root_path, confirmed_references)
            if confirmed_references != artifact_references:
                raise MigrationRejected(
                    "legacy artifact references changed during migration admission"
                )
            if confirmed_artifacts != artifacts:
                raise MigrationRejected("legacy artifacts changed during migration admission")
            confirmed_snapshot, confirmed_digest, confirmed_revisions = _migration_snapshot(
                db,
                source_schema,
                legacy_high_water,
                confirmed_artifacts,
            )
            if (
                confirmed_snapshot != snapshot
                or confirmed_digest != snapshot_digest
                or confirmed_revisions != revisions
            ):
                raise MigrationRejected("legacy projections changed during migration admission")
            _verify_migration_effects(
                root_path,
                operation_id,
                snapshot_digest,
                backup_digest,
                artifacts,
            )

            _create_migration_tables(db)
            admission_event_id = legacy_high_water + 1
            inserted_admission_event_id = _append_raw_event(
                db,
                occurred_at=occurred_at.isoformat(timespec="milliseconds"),
                kind="gate.admitted",
                severity="info",
                aggregate_type="operation",
                aggregate_id=operation_id,
                payload={
                    "action": "journal.migrate",
                    "targets": [
                        {
                            "aggregate_id": operation_id,
                            "aggregate_type": "migration",
                            "expected_revision": 0,
                            "role": "write",
                        }
                    ],
                },
            )
            if inserted_admission_event_id != admission_event_id:
                raise JournalError("migration admission event ordering is not deterministic")
            boundary_event_id = legacy_high_water + 2
            db.execute(
                """
                INSERT INTO events(
                    id, occurred_at, kind, severity, aggregate_type, aggregate_id, payload
                ) VALUES (?, ?, 'migration.boundary', 'info', 'migration', ?, ?)
                """,
                (
                    boundary_event_id,
                    occurred_at.isoformat(timespec="milliseconds"),
                    operation_id,
                    payload,
                ),
            )
            if source_schema == 1:
                _backfill_v1_durable_objects(db)
            else:
                _backfill_attempt_ceiling(db)
            all_revisions = revisions
            db.execute(
                """
                INSERT INTO migration_snapshots(
                    digest, format, source_schema, legacy_high_water_event_id,
                    boundary_event_id, created_at
                ) VALUES (?, 'aos.migration-snapshot.v2', ?, ?, ?, ?)
                """,
                (
                    snapshot_digest,
                    source_schema,
                    legacy_high_water,
                    boundary_event_id,
                    occurred_at.isoformat(timespec="milliseconds"),
                ),
            )
            db.executemany(
                """
                INSERT INTO migration_baseline_revisions(
                    snapshot_digest, aggregate_type, aggregate_id, revision
                ) VALUES (?, ?, ?, 1)
                """,
                [
                    (snapshot_digest, aggregate_type, aggregate_id)
                    for aggregate_type, aggregate_id in all_revisions
                ],
            )
            db.executemany(
                """
                INSERT INTO aggregate_revisions(
                    aggregate_type, aggregate_id, revision, snapshot_digest, source_event_id
                ) VALUES (?, ?, 1, ?, ?)
                """,
                [
                    (aggregate_type, aggregate_id, snapshot_digest, boundary_event_id)
                    for aggregate_type, aggregate_id in all_revisions
                ],
            )
            db.execute(
                """
                INSERT INTO migration_backups(
                    digest, source_schema, legacy_high_water_event_id,
                    snapshot_digest, boundary_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    backup_digest,
                    source_schema,
                    legacy_high_water,
                    snapshot_digest,
                    boundary_event_id,
                    occurred_at.isoformat(timespec="milliseconds"),
                ),
            )
            db.executemany(
                """
                INSERT INTO legacy_artifacts(
                    legacy_path, digest, size, status, blob_digest,
                    quarantine_reason, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        artifact.legacy_path,
                        artifact.digest,
                        artifact.size,
                        artifact.status,
                        artifact.blob_digest,
                        artifact.quarantine_reason,
                        boundary_event_id,
                    )
                    for artifact in artifacts
                ],
            )
            pending_cleanup_items = len(
                {
                    artifact.source_path
                    for artifact in artifacts
                    if artifact.status == "quarantined" and artifact.source_path is not None
                }
            )
            db.execute(
                """
                INSERT INTO migration_cleanup(
                    operation_id, status, pending_items, completed_at,
                    completed_event_id, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    "pending" if pending_cleanup_items else "completed",
                    pending_cleanup_items,
                    (
                        None
                        if pending_cleanup_items
                        else occurred_at.isoformat(timespec="milliseconds")
                    ),
                    None if pending_cleanup_items else boundary_event_id,
                    boundary_event_id,
                ),
            )
            _reconcile_legacy_runtime(
                db,
                source_schema,
                occurred_at.isoformat(timespec="milliseconds"),
            )
            db.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            _record_migration_operation_and_envelopes(
                db,
                operation_id=operation_id,
                request_hash=request_digest,
                invoker_id=invoker_id,
                admission_event_id=admission_event_id,
                boundary_event_id=boundary_event_id,
                occurred_at=occurred_at.isoformat(timespec="milliseconds"),
                source_schema=source_schema,
                legacy_high_water=legacy_high_water,
                snapshot_digest=snapshot_digest,
                backup_digest=backup_digest,
            )
            _verify_v2_event_closure(db)

            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise JournalError(f"journal integrity check failed: {integrity}")
            foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise JournalError("journal foreign-key check failed")
            if _live_legacy_daemon(root_path) is not None:
                raise MigrationRejected("legacy daemon became live before migration commit")
            if fault_injector is not None:
                fault_injector("before_commit")
            final_references = _referenced_artifact_paths(db)
            final_artifacts = _legacy_artifacts(root_path, final_references)
            if final_references != artifact_references or final_artifacts != artifacts:
                raise MigrationRejected(
                    "legacy artifacts changed immediately before migration commit"
                )
            if _live_legacy_daemon(root_path) is not None:
                raise MigrationRejected("legacy daemon became live before migration commit")
            final_live_processes = _live_process_candidates(monitored_processes)
            if final_live_processes:
                raise MigrationRejected(
                    "legacy process became live immediately before migration commit: "
                    f"{final_live_processes}"
                )
            _verify_migration_effects(
                root_path,
                operation_id,
                snapshot_digest,
                backup_digest,
                artifacts,
            )
            _require_database_path_identity(db_path, source_database_identity)
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        _require_database_path_identity(db_path, source_database_identity)
        if fault_injector is not None:
            fault_injector("after_commit")
        _require_database_path_identity(db_path, source_database_identity)
        _finish_quarantine_cleanup(root_path, db, operation_id, clock)
        _require_database_path_identity(db_path, source_database_identity)
        _checkpoint_and_fsync_database(db, db_path, source_database_identity)

    return MigrationOutcome(
        status="completed",
        operation_id=operation_id,
        source_schema=source_schema,
        target_schema=CURRENT_SCHEMA_VERSION,
        legacy_high_water_event_id=legacy_high_water,
        boundary_event_id=boundary_event_id,
        snapshot_digest=snapshot_digest,
        backup_digest=backup_digest,
    )


@dataclass(slots=True)
class _OperationValidationContext:
    db: sqlite3.Connection
    request: OperationRequest
    observed_revisions: Mapping[tuple[str, str], int]
    occurred_at: datetime


class _OperationContext:
    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        request: OperationRequest,
        request_hash: str,
        admission_event_id: int,
        occurred_at: str,
        evaluated_expires_at: str | None,
        expected_revisions: tuple[ExpectedRevision, ...],
        fault_injector: Callable[[str], None] | None,
    ) -> None:
        self.db = db
        self.request = request
        self.request_hash = request_hash
        self.admission_event_id = admission_event_id
        self.occurred_at = occurred_at
        self.evaluated_expires_at = evaluated_expires_at
        self._expected = {
            (item.aggregate_type, item.aggregate_id): item for item in expected_revisions
        }
        self._advanced: set[tuple[str, str]] = set()
        self._event_ids: list[int] = []
        self._fault_injector = fault_injector

    @property
    def event_ids(self) -> tuple[int, ...]:
        return tuple(self._event_ids)

    def _append(
        self,
        *,
        kind: str,
        severity: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, object],
        payload_schema_version: int,
    ) -> EventRecord:
        _require_safe_operation_json(payload, "event payload")
        cursor = self.db.execute(
            """
            INSERT INTO events(
                occurred_at, kind, severity, aggregate_type, aggregate_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self.occurred_at,
                kind,
                severity,
                aggregate_type,
                aggregate_id,
                _canonical_json(dict(payload)),
            ),
        )
        event_id = int(cursor.lastrowid)
        _insert_event_envelope(
            self.db,
            event_id=event_id,
            invoker_kind=self.request.invoker.kind,
            invoker_id=self.request.invoker.id,
            authority_epoch_id=self.request.invoker.authority_epoch_id,
            evaluated_expires_at=self.evaluated_expires_at,
            on_behalf_of=self.request.invoker.on_behalf_of,
            decision_source_hash=self.request.invoker.decision_source_hash,
            operation_id=self.request.operation_id,
            request_hash=self.request_hash,
            admission_id=self.admission_event_id,
            payload_schema_version=payload_schema_version,
        )
        self._event_ids.append(event_id)
        if self._fault_injector is not None:
            self._fault_injector("after_domain_event")
        return EventRecord(
            event_id=event_id,
            occurred_at=self.occurred_at,
            kind=kind,
            severity=severity,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=dict(payload),
            envelope={
                "admission_id": self.admission_event_id,
                "authority_epoch_id": self.request.invoker.authority_epoch_id,
                "causation_id": self.request.operation_id,
                "decision_source_hash": self.request.invoker.decision_source_hash,
                "envelope_version": 1,
                "evaluated_expires_at": self.evaluated_expires_at,
                "invoker_id": self.request.invoker.id,
                "invoker_kind": self.request.invoker.kind,
                "on_behalf_of": self.request.invoker.on_behalf_of,
                "operation_id": self.request.operation_id,
                "payload_schema_version": payload_schema_version,
                "request_hash": self.request_hash,
                "request_key": self.request.operation_id,
            },
        )

    def _advance_revision(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        source_event_id: int,
    ) -> None:
        key = (aggregate_type, aggregate_id)
        expected = self._expected.get(key)
        if expected is None or expected.role != "write":
            raise JournalError("domain event attempted to advance an undeclared writable aggregate")
        if key in self._advanced:
            raise JournalError("one operation cannot advance an aggregate more than once")
        row = self.db.execute(
            """
            SELECT revision
            FROM aggregate_revisions
            WHERE aggregate_type=? AND aggregate_id=?
            """,
            key,
        ).fetchone()
        observed = 0 if row is None else int(row[0])
        if observed != expected.revision:
            raise JournalError("aggregate revision changed after operation admission")
        if row is None:
            self.db.execute(
                """
                INSERT INTO aggregate_revisions(
                    aggregate_type, aggregate_id, revision, snapshot_digest,
                    genesis_event_id, source_event_id
                ) VALUES (?, ?, 1, NULL, ?, ?)
                """,
                (aggregate_type, aggregate_id, source_event_id, source_event_id),
            )
        else:
            cursor = self.db.execute(
                """
                UPDATE aggregate_revisions
                SET revision=revision+1, source_event_id=?
                WHERE aggregate_type=? AND aggregate_id=? AND revision=?
                """,
                (
                    source_event_id,
                    aggregate_type,
                    aggregate_id,
                    expected.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError("aggregate revision changed during domain event append")
        self._advanced.add(key)

    def append_event(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, object],
        *,
        severity: str = "info",
        payload_schema_version: int = 1,
    ) -> EventRecord:
        """Append one module-owned reserved fact and advance its aggregate once."""

        try:
            spec = require_reserved_event(
                kind,
                EventAggregateType(aggregate_type),
                payload_schema_version=payload_schema_version,
            )
        except (ProtocolViolation, ValueError) as exc:
            raise InvalidOperation("reserved event violates the protocol vocabulary") from exc
        if not spec.requires_admission:
            raise InvalidOperation("special lifecycle facts use their dedicated journal operation")
        event = self._append(
            kind=kind,
            severity=_required_event_severity(severity),
            aggregate_type=aggregate_type,
            aggregate_id=_required_public_identifier(aggregate_id, "aggregate_id"),
            payload=payload,
            payload_schema_version=payload_schema_version,
        )
        if spec.advances_aggregate_revision:
            self._advance_revision(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                source_event_id=event.event_id,
            )
        return event

    def append_observation(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, object],
        *,
        severity: str = "info",
    ) -> EventRecord:
        """Append a nonconstitutional observation without changing a revision."""

        try:
            require_generic_event_kind(kind)
        except ProtocolViolation as exc:
            raise InvalidOperation("generic append cannot emit a reserved event kind") from exc
        return self._append(
            kind=kind,
            severity=_required_event_severity(severity),
            aggregate_type=_required_public_identifier(
                aggregate_type,
                "aggregate_type",
            ),
            aggregate_id=_required_public_identifier(aggregate_id, "aggregate_id"),
            payload=payload,
            payload_schema_version=1,
        )


def _required_public_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or PUBLIC_IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise InvalidOperation(f"{label} must be a bounded nonsecret identifier")
    if _content_policy_reason(value.encode("utf-8")) is not None:
        raise InvalidOperation(f"{label} must be a bounded nonsecret identifier")
    return value


def _required_event_severity(value: object) -> str:
    if value not in {"routine", "info", "attention", "warning", "error"}:
        raise InvalidOperation("event severity is not supported")
    return str(value)


def _require_safe_operation_json(value: object, label: str) -> bytes:
    encoded = canonical_json_bytes(value)
    if _content_policy_reason(encoded) is not None:
        raise InvalidOperation(f"{label} is policy-unsafe")
    return encoded


def _normalized_operation_request(
    request: OperationRequest,
) -> tuple[tuple[ExpectedRevision, ...], str]:
    if not isinstance(request, OperationRequest):
        raise InvalidOperation("operation request has the wrong type")
    if (
        not isinstance(request.operation_id, str)
        or MIGRATION_OPERATION_PATTERN.fullmatch(request.operation_id) is None
        or _content_policy_reason(request.operation_id.encode("utf-8")) is not None
    ):
        raise InvalidOperation("operation_id must use the nonsecret OP-[A-Z0-9_-] form")
    if (
        not isinstance(request.action, str)
        or OPERATION_ACTION_PATTERN.fullmatch(request.action) is None
    ):
        raise InvalidOperation("action must use the lowercase namespace.name form")
    invoker = request.invoker
    if not isinstance(invoker, Invoker) or invoker.kind not in INVOKER_KINDS:
        raise InvalidOperation("invoker kind is not supported")
    _required_public_identifier(invoker.id, "invoker id")
    if invoker.authority_epoch_id is not None:
        _required_public_identifier(invoker.authority_epoch_id, "authority epoch id")
    if (invoker.on_behalf_of is None) != (invoker.decision_source_hash is None):
        raise InvalidOperation(
            "human attribution requires both on_behalf_of and decision_source_hash"
        )
    if invoker.on_behalf_of is not None:
        _required_public_identifier(invoker.on_behalf_of, "on_behalf_of")
        if not _valid_sha256(invoker.decision_source_hash):
            raise InvalidOperation("decision_source_hash must be canonical SHA-256")
    if not isinstance(request.expected_revisions, tuple):
        raise InvalidOperation("expected_revisions must be an immutable tuple")
    normalized: list[ExpectedRevision] = []
    seen: set[tuple[str, str]] = set()
    for item in request.expected_revisions:
        if not isinstance(item, ExpectedRevision):
            raise InvalidOperation("expected revision has the wrong type")
        aggregate_type = _required_public_identifier(
            item.aggregate_type,
            "expected aggregate type",
        )
        aggregate_id = _required_public_identifier(
            item.aggregate_id,
            "expected aggregate id",
        )
        if type(item.revision) is not int or item.revision < 0:
            raise InvalidOperation("expected revision must be a nonnegative integer")
        if item.role not in {"read", "write"}:
            raise InvalidOperation("expected revision role must be read or write")
        key = (aggregate_type, aggregate_id)
        if key in seen:
            raise InvalidOperation("expected revisions contain a duplicate aggregate")
        seen.add(key)
        normalized.append(
            ExpectedRevision(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                revision=item.revision,
                role=item.role,
            )
        )
    if not isinstance(request.payload, Mapping):
        raise InvalidOperation("operation payload must be an object")
    canonical_json_bytes(dict(request.payload))
    ordered = tuple(
        sorted(
            normalized,
            key=lambda item: (item.aggregate_type, item.aggregate_id),
        )
    )
    request_hash = content_digest(
        {
            "action": request.action,
            "expected_revisions": [
                {
                    "aggregate_id": item.aggregate_id,
                    "aggregate_type": item.aggregate_type,
                    "revision": item.revision,
                    "role": item.role,
                }
                for item in ordered
            ],
            "invoker": {
                "authority_epoch_id": invoker.authority_epoch_id,
                "decision_source_hash": invoker.decision_source_hash,
                "id": invoker.id,
                "kind": invoker.kind,
                "on_behalf_of": invoker.on_behalf_of,
            },
            "operation_id": request.operation_id,
            "payload": dict(request.payload),
        }
    )
    return ordered, request_hash


def _outcome_payload(outcome: OperationOutcome) -> str:
    return _canonical_json(
        {
            "admission_event_id": outcome.admission_event_id,
            "domain_event_ids": list(outcome.domain_event_ids),
            "operation_id": outcome.operation_id,
            "reason": outcome.reason,
            "rejection_event_id": outcome.rejection_event_id,
            "revisions": [
                {
                    "aggregate_id": revision.aggregate_id,
                    "aggregate_type": revision.aggregate_type,
                    "revision": revision.revision,
                    "role": revision.role,
                }
                for revision in outcome.revisions
            ],
            "status": outcome.status,
        }
    )


def _outcome_from_record(
    operation_id: str,
    status: str,
    payload: str,
) -> OperationOutcome:
    try:
        recorded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JournalError("recorded operation outcome is invalid") from exc
    revisions = tuple(
        ExpectedRevision(
            aggregate_type=str(item["aggregate_type"]),
            aggregate_id=str(item["aggregate_id"]),
            revision=int(item["revision"]),
            role=str(item["role"]),
        )
        for item in recorded["revisions"]
    )
    return OperationOutcome(
        status="replayed",
        original_status=status,
        operation_id=operation_id,
        admission_event_id=(
            None if recorded["admission_event_id"] is None else int(recorded["admission_event_id"])
        ),
        rejection_event_id=(
            None if recorded["rejection_event_id"] is None else int(recorded["rejection_event_id"])
        ),
        domain_event_ids=tuple(int(item) for item in recorded["domain_event_ids"]),
        revisions=revisions,
        reason=None if recorded["reason"] is None else str(recorded["reason"]),
    )


def _observed_revisions(
    db: sqlite3.Connection,
    expected: tuple[ExpectedRevision, ...],
) -> dict[tuple[str, str], int]:
    observed: dict[tuple[str, str], int] = {}
    for item in expected:
        row = db.execute(
            """
            SELECT revision
            FROM aggregate_revisions
            WHERE aggregate_type=? AND aggregate_id=?
            """,
            (item.aggregate_type, item.aggregate_id),
        ).fetchone()
        observed[(item.aggregate_type, item.aggregate_id)] = 0 if row is None else int(row[0])
    return observed


def _result_revisions(
    db: sqlite3.Connection,
    expected: tuple[ExpectedRevision, ...],
) -> tuple[ExpectedRevision, ...]:
    observed = _observed_revisions(db, expected)
    return tuple(
        ExpectedRevision(
            aggregate_type=item.aggregate_type,
            aggregate_id=item.aggregate_id,
            revision=observed[(item.aggregate_type, item.aggregate_id)],
            role=item.role,
        )
        for item in expected
    )


def _insert_operation_targets(
    db: sqlite3.Connection,
    operation_id: str,
    expected: tuple[ExpectedRevision, ...],
    observed: Mapping[tuple[str, str], int],
) -> None:
    db.executemany(
        """
        INSERT INTO operation_targets(
            operation_id, aggregate_type, aggregate_id, role,
            expected_revision, observed_revision
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                operation_id,
                item.aggregate_type,
                item.aggregate_id,
                item.role,
                item.revision,
                observed[(item.aggregate_type, item.aggregate_id)],
            )
            for item in expected
        ],
    )


def _append_raw_event(
    db: sqlite3.Connection,
    *,
    occurred_at: str,
    kind: str,
    severity: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, object],
) -> int:
    _require_safe_operation_json(payload, "event payload")
    cursor = db.execute(
        """
        INSERT INTO events(
            occurred_at, kind, severity, aggregate_type, aggregate_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            occurred_at,
            kind,
            severity,
            aggregate_type,
            aggregate_id,
            _canonical_json(dict(payload)),
        ),
    )
    return int(cursor.lastrowid)


def _authority_gate(
    db: sqlite3.Connection,
    invoker: Invoker,
    occurred_at: datetime,
) -> tuple[GateRejection | None, str | None]:
    authority_required = invoker.kind in AUTHORITY_BEARING_INVOKERS
    if authority_required and invoker.authority_epoch_id is None:
        return GateRejection("authority_missing", {}), None
    if not authority_required and invoker.authority_epoch_id is not None:
        return GateRejection("authority_not_permitted", {}), None
    if invoker.authority_epoch_id is None:
        return None, None
    epoch = db.execute(
        """
        SELECT subject_id, expires_at, status
        FROM authority_epochs
        WHERE epoch_id=?
        """,
        (invoker.authority_epoch_id,),
    ).fetchone()
    if epoch is None:
        return GateRejection("authority_unknown", {}), None
    expires_at = str(epoch[1])
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return GateRejection("authority_invalid", {}), expires_at
    if str(epoch[0]) != invoker.id:
        return GateRejection("authority_scope_mismatch", {}), expires_at
    if str(epoch[2]) != "active":
        return GateRejection("authority_inactive", {}), expires_at
    if expiry.tzinfo is None or expiry.utcoffset() is None or expiry <= occurred_at:
        return GateRejection("authority_expired", {}), expires_at
    return None, expires_at


def _commit_new_rejection(
    db: sqlite3.Connection,
    *,
    request: OperationRequest,
    request_hash: str,
    expected: tuple[ExpectedRevision, ...],
    observed: Mapping[tuple[str, str], int],
    rejection: GateRejection,
    occurred_at: str,
    evaluated_expires_at: str | None,
) -> OperationOutcome:
    _require_safe_operation_json(dict(rejection.details), "gate rejection details")
    rejection_event_id = _append_raw_event(
        db,
        occurred_at=occurred_at,
        kind="gate.rejected",
        severity="attention",
        aggregate_type="operation",
        aggregate_id=request.operation_id,
        payload={
            "action": request.action,
            "details": dict(rejection.details),
            "reason": rejection.code,
            "targets": [
                {
                    "aggregate_id": item.aggregate_id,
                    "aggregate_type": item.aggregate_type,
                    "expected_revision": item.revision,
                    "observed_revision": observed[(item.aggregate_type, item.aggregate_id)],
                    "role": item.role,
                }
                for item in expected
            ],
        },
    )
    revisions = tuple(
        ExpectedRevision(
            item.aggregate_type,
            item.aggregate_id,
            observed[(item.aggregate_type, item.aggregate_id)],
            item.role,
        )
        for item in expected
    )
    outcome = OperationOutcome(
        status="rejected",
        operation_id=request.operation_id,
        admission_event_id=None,
        rejection_event_id=rejection_event_id,
        domain_event_ids=(),
        revisions=revisions,
        reason=rejection.code,
    )
    db.execute(
        """
        INSERT INTO operations(
            operation_id, action, request_hash, invoker_kind, invoker_id,
            authority_epoch_id, status, rejection_event_id, outcome,
            created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?)
        """,
        (
            request.operation_id,
            request.action,
            request_hash,
            request.invoker.kind,
            request.invoker.id,
            request.invoker.authority_epoch_id,
            rejection_event_id,
            _outcome_payload(outcome),
            occurred_at,
            occurred_at,
        ),
    )
    _insert_operation_targets(
        db,
        request.operation_id,
        expected,
        observed,
    )
    _insert_event_envelope(
        db,
        event_id=rejection_event_id,
        invoker_kind=request.invoker.kind,
        invoker_id=request.invoker.id,
        authority_epoch_id=request.invoker.authority_epoch_id,
        evaluated_expires_at=evaluated_expires_at,
        on_behalf_of=request.invoker.on_behalf_of,
        decision_source_hash=request.invoker.decision_source_hash,
        operation_id=request.operation_id,
        request_hash=request_hash,
        admission_id=None,
    )
    return outcome


def _commit_operation_conflict(
    db: sqlite3.Connection,
    *,
    request: OperationRequest,
    request_hash: str,
    occurred_at: str,
) -> OperationOutcome:
    existing = db.execute(
        """
        SELECT rejection_event_id
        FROM operation_conflicts
        WHERE operation_id=? AND conflicting_request_hash=?
        """,
        (request.operation_id, request_hash),
    ).fetchone()
    if existing is not None:
        return OperationOutcome(
            status="rejected",
            original_status="rejected",
            operation_id=request.operation_id,
            admission_event_id=None,
            rejection_event_id=int(existing[0]),
            domain_event_ids=(),
            revisions=(),
            reason="operation_id_conflict",
        )
    rejection_event_id = _append_raw_event(
        db,
        occurred_at=occurred_at,
        kind="gate.rejected",
        severity="attention",
        aggregate_type="operation",
        aggregate_id=request.operation_id,
        payload={
            "action": request.action,
            "conflicting_request_hash": request_hash,
            "reason": "operation_id_conflict",
        },
    )
    _insert_event_envelope(
        db,
        event_id=rejection_event_id,
        invoker_kind=request.invoker.kind,
        invoker_id=request.invoker.id,
        authority_epoch_id=None,
        on_behalf_of=request.invoker.on_behalf_of,
        decision_source_hash=request.invoker.decision_source_hash,
        operation_id=request.operation_id,
        request_hash=request_hash,
        admission_id=None,
    )
    db.execute(
        """
        INSERT INTO operation_conflicts(
            operation_id, conflicting_request_hash, rejection_event_id, observed_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            request.operation_id,
            request_hash,
            rejection_event_id,
            occurred_at,
        ),
    )
    return OperationOutcome(
        status="rejected",
        operation_id=request.operation_id,
        admission_event_id=None,
        rejection_event_id=rejection_event_id,
        domain_event_ids=(),
        revisions=(),
        reason="operation_id_conflict",
    )


def _run_governed(
    root: Path,
    request: OperationRequest,
    *,
    validator: Callable[[_OperationValidationContext], GateRejection | None] | None,
    projector: Callable[[_OperationContext], None],
    clock: Callable[[], datetime] = utc_now,
    fault_injector: Callable[[str], None] | None = None,
) -> OperationOutcome:
    """Run one database-only operation through the atomic admission kernel."""

    expected, request_hash = _normalized_operation_request(request)
    occurred_at = clock()
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise InvalidOperation("operation clock must return a timezone-aware datetime")
    occurred_at_text = occurred_at.isoformat(timespec="milliseconds")
    db_path = Path(root).expanduser().resolve() / "aos.db"
    database_identity = _validate_sqlite_source_files(db_path)
    with sqlite3.connect(db_path) as db:
        _require_database_path_identity(db_path, database_identity)
        db.execute("PRAGMA foreign_keys=ON")
        if _schema_version(db) != CURRENT_SCHEMA_VERSION:
            raise InvalidOperation("journal schema requires explicit migration")
        if db.execute("SELECT 1 FROM migration_cleanup WHERE status='pending' LIMIT 1").fetchone():
            raise InvalidOperation("journal has pending migration cleanup")
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """
                SELECT request_hash, status, outcome
                FROM operations
                WHERE operation_id=?
                """,
                (request.operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) == request_hash:
                    outcome = _outcome_from_record(
                        request.operation_id,
                        str(existing[1]),
                        str(existing[2]),
                    )
                    db.rollback()
                    return outcome
                outcome = _commit_operation_conflict(
                    db,
                    request=request,
                    request_hash=request_hash,
                    occurred_at=occurred_at_text,
                )
                db.commit()
                return outcome

            observed = _observed_revisions(db, expected)
            rejection = (
                GateRejection("secret_policy_rejected", {})
                if _content_policy_reason(canonical_json_bytes(dict(request.payload))) is not None
                else None
            )
            revision_rejection = next(
                (
                    GateRejection(
                        "revision_mismatch",
                        {
                            "aggregate_id": item.aggregate_id,
                            "aggregate_type": item.aggregate_type,
                        },
                    )
                    for item in expected
                    if observed[(item.aggregate_type, item.aggregate_id)] != item.revision
                ),
                None,
            )
            rejection = rejection or revision_rejection
            authority_rejection, evaluated_expires_at = _authority_gate(
                db,
                request.invoker,
                occurred_at,
            )
            rejection = rejection or authority_rejection
            validation_context = _OperationValidationContext(
                db=db,
                request=request,
                observed_revisions=observed,
                occurred_at=occurred_at,
            )
            if rejection is None and validator is not None:
                rejection = validator(validation_context)
                if rejection is not None and not isinstance(rejection, GateRejection):
                    raise JournalError("operation validator returned an invalid decision")
            if rejection is not None:
                outcome = _commit_new_rejection(
                    db,
                    request=request,
                    request_hash=request_hash,
                    expected=expected,
                    observed=observed,
                    rejection=rejection,
                    occurred_at=occurred_at_text,
                    evaluated_expires_at=evaluated_expires_at,
                )
                db.commit()
                return outcome

            admission_event_id = _append_raw_event(
                db,
                occurred_at=occurred_at_text,
                kind="gate.admitted",
                severity="info",
                aggregate_type="operation",
                aggregate_id=request.operation_id,
                payload={
                    "action": request.action,
                    "targets": [
                        {
                            "aggregate_id": item.aggregate_id,
                            "aggregate_type": item.aggregate_type,
                            "expected_revision": item.revision,
                            "role": item.role,
                        }
                        for item in expected
                    ],
                },
            )
            admitted = OperationOutcome(
                status="admitted",
                operation_id=request.operation_id,
                admission_event_id=admission_event_id,
                rejection_event_id=None,
                domain_event_ids=(),
                revisions=tuple(expected),
            )
            db.execute(
                """
                INSERT INTO operations(
                    operation_id, action, request_hash, invoker_kind, invoker_id,
                    authority_epoch_id, status, admission_event_id, outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'admitted', ?, ?, ?)
                """,
                (
                    request.operation_id,
                    request.action,
                    request_hash,
                    request.invoker.kind,
                    request.invoker.id,
                    request.invoker.authority_epoch_id,
                    admission_event_id,
                    _outcome_payload(admitted),
                    occurred_at_text,
                ),
            )
            _insert_operation_targets(
                db,
                request.operation_id,
                expected,
                observed,
            )
            _insert_event_envelope(
                db,
                event_id=admission_event_id,
                invoker_kind=request.invoker.kind,
                invoker_id=request.invoker.id,
                authority_epoch_id=request.invoker.authority_epoch_id,
                evaluated_expires_at=evaluated_expires_at,
                on_behalf_of=request.invoker.on_behalf_of,
                decision_source_hash=request.invoker.decision_source_hash,
                operation_id=request.operation_id,
                request_hash=request_hash,
                admission_id=None,
            )
            if fault_injector is not None:
                fault_injector("after_admission")
            context = _OperationContext(
                db=db,
                request=request,
                request_hash=request_hash,
                admission_event_id=admission_event_id,
                occurred_at=occurred_at_text,
                evaluated_expires_at=evaluated_expires_at,
                expected_revisions=expected,
                fault_injector=fault_injector,
            )
            projector(context)
            if fault_injector is not None:
                fault_injector("after_projection")
            revisions = _result_revisions(db, expected)
            completed_event_id = context.event_ids[-1] if context.event_ids else admission_event_id
            outcome = OperationOutcome(
                status="completed",
                operation_id=request.operation_id,
                admission_event_id=admission_event_id,
                rejection_event_id=None,
                domain_event_ids=context.event_ids,
                revisions=revisions,
            )
            db.execute(
                """
                UPDATE operations
                SET status='completed', completed_event_id=?, outcome=?, completed_at=?
                WHERE operation_id=? AND status='admitted'
                """,
                (
                    completed_event_id,
                    _outcome_payload(outcome),
                    occurred_at_text,
                    request.operation_id,
                ),
            )
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise JournalError("operation introduced a foreign-key violation")
            _require_database_path_identity(db_path, database_identity)
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        _require_database_path_identity(db_path, database_identity)
        _checkpoint_and_fsync_database(db, db_path, database_identity)
    return outcome


def append_observation(
    root: Path,
    request: OperationRequest,
    *,
    kind: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    payload: Mapping[str, object] | None = None,
    severity: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Append one attributable, idempotent, nonreserved observation."""

    supplied = {
        "aggregate_id": aggregate_id,
        "aggregate_type": aggregate_type,
        "kind": kind,
        "payload": payload,
        "severity": severity,
    }
    if all(value is None for value in supplied.values()):
        observation = dict(request.payload)
        kind = observation.get("kind")  # type: ignore[assignment]
        aggregate_type = observation.get("aggregate_type")  # type: ignore[assignment]
        aggregate_id = observation.get("aggregate_id")  # type: ignore[assignment]
        payload = observation.get("payload")  # type: ignore[assignment]
        severity = observation.get("severity", "info")  # type: ignore[assignment]
    elif any(value is None for key, value in supplied.items() if key != "severity"):
        raise InvalidOperation("observation fields must be supplied together")
    if severity is None:
        severity = "info"

    def validator(_context: _OperationValidationContext) -> GateRejection | None:
        if request.action != "journal.append_observation":
            return GateRejection("observation_action_invalid", {})
        if request.expected_revisions:
            return GateRejection("observation_revisions_not_permitted", {})
        try:
            if not isinstance(kind, str):
                raise InvalidOperation("observation kind must be text")
            require_generic_event_kind(kind)
            _required_public_identifier(aggregate_type, "aggregate_type")
            _required_public_identifier(aggregate_id, "aggregate_id")
            _required_event_severity(severity)
            if not isinstance(payload, Mapping):
                raise InvalidOperation("observation payload must be an object")
        except (InvalidOperation, ProtocolViolation):
            return GateRejection("observation_contract_rejected", {})
        return None

    def projector(context: _OperationContext) -> None:
        if (
            not isinstance(kind, str)
            or not isinstance(aggregate_type, str)
            or not isinstance(aggregate_id, str)
            or not isinstance(payload, Mapping)
            or not isinstance(severity, str)
        ):
            raise JournalError("admitted observation contract is incomplete")
        context.append_observation(
            kind,
            aggregate_type,
            aggregate_id,
            payload,
            severity=severity,
        )

    return _run_governed(
        root,
        request,
        validator=validator,
        projector=projector,
        clock=clock,
    )


def read_events(root: Path, *, after_event_id: int = 0) -> tuple[EventRecord, ...]:
    """Read ordered events without mutating or renewing authority."""

    if type(after_event_id) is not int or after_event_id < 0:
        raise InvalidOperation("after_event_id must be a nonnegative integer")
    db_path = Path(root).expanduser().resolve() / "aos.db"
    with _readonly_database_snapshot(db_path) as db:
        if _schema_version(db) != CURRENT_SCHEMA_VERSION:
            raise InvalidOperation("journal schema requires explicit migration")
        rows = db.execute(
            """
            SELECT
                e.id, e.occurred_at, e.kind, e.severity,
                e.aggregate_type, e.aggregate_id, e.payload,
                envelope.envelope_version,
                envelope.payload_schema_version,
                envelope.invoker_kind,
                envelope.invoker_id,
                envelope.authority_epoch_id,
                envelope.evaluated_expires_at,
                envelope.on_behalf_of,
                envelope.decision_source_hash,
                envelope.operation_id,
                envelope.request_key,
                envelope.request_hash,
                envelope.causation_id,
                envelope.admission_id
            FROM events AS e
            LEFT JOIN event_envelopes AS envelope ON envelope.event_id=e.id
            WHERE e.id > ?
            ORDER BY e.id
            """,
            (after_event_id,),
        ).fetchall()
    records: list[EventRecord] = []
    envelope_fields = (
        "envelope_version",
        "payload_schema_version",
        "invoker_kind",
        "invoker_id",
        "authority_epoch_id",
        "evaluated_expires_at",
        "on_behalf_of",
        "decision_source_hash",
        "operation_id",
        "request_key",
        "request_hash",
        "causation_id",
        "admission_id",
    )
    for row in rows:
        try:
            payload = json.loads(str(row[6]))
        except json.JSONDecodeError as exc:
            raise JournalError("event payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise JournalError("event payload is not a JSON object")
        envelope = None if row[7] is None else dict(zip(envelope_fields, row[7:], strict=True))
        records.append(
            EventRecord(
                event_id=int(row[0]),
                occurred_at=str(row[1]),
                kind=str(row[2]),
                severity=str(row[3]),
                aggregate_type=str(row[4]),
                aggregate_id=str(row[5]),
                payload=payload,
                envelope=envelope,
            )
        )
    return tuple(records)
