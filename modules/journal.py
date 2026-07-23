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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

CURRENT_SCHEMA_VERSION = 3
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
    "attempts",
    "contract_revisions",
    "events",
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
    "processes",
    "provider_observations",
    "work_orders",
    "worker_sessions",
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


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0])
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
            snapshot_digest TEXT NOT NULL REFERENCES migration_snapshots(digest),
            source_event_id INTEGER NOT NULL REFERENCES events(id),
            PRIMARY KEY (aggregate_type, aggregate_id)
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

            boundary_event_id = legacy_high_water + 1
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
            _create_migration_tables(db)
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
