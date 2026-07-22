from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StoreError(RuntimeError):
    pass


class AuthorizationError(StoreError):
    pass


class TransitionError(StoreError):
    pass


class Store:
    def __init__(self, home: Path | str):
        self.home = Path(home).expanduser().resolve()
        self.db_path = self.home / "aos.db"
        self.artifacts = self.home / "artifacts"

    def initialize(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    kind TEXT PRIMARY KEY,
                    holder TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missions (
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
                );
                CREATE TABLE IF NOT EXISTS processes (
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
                );
                CREATE TABLE IF NOT EXISTS inbox_cursors (
                    consumer TEXT PRIMARY KEY,
                    event_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS contract_revisions (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, revision)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    worktree TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(mission_id, number)
                );
                CREATE TABLE IF NOT EXISTS work_orders (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    process_id TEXT NOT NULL REFERENCES processes(id),
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(process_id, revision)
                );
                CREATE TABLE IF NOT EXISTS worker_sessions (
                    id TEXT PRIMARY KEY,
                    process_id TEXT NOT NULL REFERENCES processes(id),
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    backend_handle TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    UNIQUE(process_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS provider_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_aggregate
                    ON events(aggregate_type, aggregate_id, id);
                CREATE INDEX IF NOT EXISTS processes_mission
                    ON processes(mission_id, attempt);
                CREATE INDEX IF NOT EXISTS worker_sessions_process
                    ON worker_sessions(process_id, sequence);
                CREATE INDEX IF NOT EXISTS provider_observations_backend
                    ON provider_observations(backend, observed_at);
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._backfill_durable_objects(db)

    def _backfill_durable_objects(self, db: sqlite3.Connection) -> None:
        """Upgrade 0.1 databases without discarding active mission history."""
        missions = db.execute("SELECT * FROM missions ORDER BY created_at").fetchall()
        for mission in missions:
            has_contract = db.execute(
                "SELECT 1 FROM contract_revisions WHERE mission_id=?", (mission["id"],)
            ).fetchone()
            if not has_contract:
                content = {
                    "schema_version": 1,
                    "objective": mission["objective"],
                    "project": mission["project"],
                    "allowed_backends": json.loads(mission["allowed_backends"]),
                    "allowed_models": json.loads(mission["allowed_models"]),
                    "allow_anthropic": bool(mission["allow_anthropic"]),
                    "verification_command": mission["verify_command"],
                }
                encoded, digest = self._content_hash(content)
                db.execute(
                    """INSERT INTO contract_revisions
                       (id, mission_id, revision, content, content_hash, created_at)
                       VALUES (?, ?, 1, ?, ?, ?)""",
                    (
                        "C-" + secrets.token_hex(6).upper(),
                        mission["id"],
                        encoded,
                        digest,
                        mission["created_at"],
                    ),
                )
        processes = db.execute("SELECT * FROM processes ORDER BY created_at").fetchall()
        for process in processes:
            has_attempt = db.execute(
                "SELECT 1 FROM attempts WHERE mission_id=? AND number=?",
                (process["mission_id"], process["attempt"]),
            ).fetchone()
            if not has_attempt:
                db.execute(
                    """INSERT INTO attempts
                       (id, mission_id, number, state, worktree, branch, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'legacy/unknown', ?, ?)""",
                    (
                        "A-" + secrets.token_hex(4).upper(),
                        process["mission_id"],
                        process["attempt"],
                        process["state"],
                        process["worktree"] or "",
                        process["created_at"],
                        process["updated_at"],
                    ),
                )
            has_work_order = db.execute(
                "SELECT 1 FROM work_orders WHERE process_id=?", (process["id"],)
            ).fetchone()
            if not has_work_order:
                mission = next(item for item in missions if item["id"] == process["mission_id"])
                contract = db.execute(
                    """SELECT revision, content_hash FROM contract_revisions
                       WHERE mission_id=? ORDER BY revision DESC LIMIT 1""",
                    (process["mission_id"],),
                ).fetchone()
                content = {
                    "schema_version": 1,
                    "mission_id": process["mission_id"],
                    "process_id": process["id"],
                    "contract_revision": contract["revision"],
                    "contract_hash": contract["content_hash"],
                    "objective": mission["objective"],
                    "verification_command": mission["verify_command"],
                    "migration_note": "reconstructed from 0.1 projection",
                }
                encoded, digest = self._content_hash(content)
                db.execute(
                    """INSERT INTO work_orders
                       (id, mission_id, process_id, revision, content, content_hash, created_at)
                       VALUES (?, ?, ?, 1, ?, ?, ?)""",
                    (
                        "W-" + secrets.token_hex(6).upper(),
                        process["mission_id"],
                        process["id"],
                        encoded,
                        digest,
                        process["created_at"],
                    ),
                )
            has_session = db.execute(
                "SELECT 1 FROM worker_sessions WHERE process_id=?", (process["id"],)
            ).fetchone()
            if not has_session:
                internal_session = "S-" + secrets.token_hex(5).upper()
                terminal = process["state"] in {"completed", "failed", "cancelled", "orphaned"}
                db.execute(
                    """INSERT INTO worker_sessions
                       (id, process_id, sequence, state, backend_handle, started_at, ended_at)
                       VALUES (?, ?, 1, ?, ?, ?, ?)""",
                    (
                        internal_session,
                        process["id"],
                        process["state"],
                        process["session_id"],
                        process["created_at"],
                        process["updated_at"] if terminal else None,
                    ),
                )
                db.execute(
                    "UPDATE processes SET session_id=? WHERE id=?",
                    (internal_session, process["id"]),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _emit(
        db: sqlite3.Connection,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> int:
        clean = payload or {}
        forbidden = {key for key in clean if "token" in key.lower() or "secret" in key.lower()}
        if forbidden:
            raise StoreError(f"secret-shaped event fields are forbidden: {sorted(forbidden)}")
        cur = db.execute(
            """INSERT INTO events
               (occurred_at, kind, severity, aggregate_type, aggregate_id, payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                utcnow(),
                kind,
                severity,
                aggregate_type,
                aggregate_id,
                json.dumps(clean, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)

    def claim_liaison(self, holder: str, ttl: int = 600) -> tuple[str, str]:
        if not holder.strip():
            raise StoreError("liaison holder is required")
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expiry = future(ttl)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT * FROM leases WHERE kind='liaison'").fetchone()
            if lease and lease["expires_at"] > now:
                raise AuthorizationError(
                    f"liaison lease is held by {lease['holder']} until {lease['expires_at']}"
                )
            db.execute(
                """INSERT OR REPLACE INTO leases
                   (kind, holder, token_hash, expires_at, updated_at)
                   VALUES ('liaison', ?, ?, ?, ?)""",
                (holder, token_hash(token), expiry, now),
            )
            self._emit(
                db, "liaison.claimed", "lease", "liaison", {"holder": holder, "expires_at": expiry}
            )
        return token, expiry

    def require_liaison(self, token: str | None, ttl: int = 600) -> sqlite3.Row:
        if not token:
            raise AuthorizationError("a liaison token is required")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT * FROM leases WHERE kind='liaison'").fetchone()
            if not lease or lease["expires_at"] <= now:
                raise AuthorizationError("the liaison lease is absent or expired")
            if not secrets.compare_digest(lease["token_hash"], token_hash(token)):
                raise AuthorizationError("the liaison token is invalid")
            expiry = future(ttl)
            db.execute(
                "UPDATE leases SET expires_at=?, updated_at=? WHERE kind='liaison'",
                (expiry, now),
            )
            return lease

    def release_liaison(self, token: str | None) -> None:
        lease = self.require_liaison(token)
        with self.connect() as db:
            db.execute("DELETE FROM leases WHERE kind='liaison'")
            self._emit(db, "liaison.released", "lease", "liaison", {"holder": lease["holder"]})

    def event(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> int:
        with self.connect() as db:
            return self._emit(db, kind, aggregate_type, aggregate_id, payload, severity)

    def events_since(self, cursor: int = 0, significant_only: bool = False) -> list[dict[str, Any]]:
        clause = "AND severity IN ('attention', 'critical', 'approval')" if significant_only else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE id > ? {clause} ORDER BY id", (cursor,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def sync_significant(
        self, consumer: str, since: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        if not consumer.strip():
            raise StoreError("inbox consumer is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute(
                "SELECT event_id FROM inbox_cursors WHERE consumer=?", (consumer,)
            ).fetchone()
            cursor = since if since is not None else int(saved["event_id"] if saved else 0)
            rows = db.execute(
                """SELECT * FROM events
                   WHERE id > ? AND severity IN ('attention', 'critical', 'approval')
                   ORDER BY id""",
                (cursor,),
            ).fetchall()
            high_water = int(
                db.execute("SELECT COALESCE(MAX(id), ?) FROM events", (cursor,)).fetchone()[0]
            )
            db.execute(
                """INSERT INTO inbox_cursors(consumer, event_id) VALUES(?, ?)
                   ON CONFLICT(consumer) DO UPDATE SET event_id=excluded.event_id""",
                (consumer, high_water),
            )
        events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
        return events, high_water

    @staticmethod
    def _content_hash(content: dict[str, Any]) -> tuple[str, str]:
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def create_contract_revision(self, mission_id: str, content: dict[str, Any]) -> dict[str, Any]:
        encoded, digest = self._content_hash(content)
        revision_id = "C-" + secrets.token_hex(6).upper()
        with self.connect() as db:
            revision = int(
                db.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM contract_revisions WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()[0]
            )
            db.execute(
                """INSERT INTO contract_revisions
                   (id, mission_id, revision, content, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (revision_id, mission_id, revision, encoded, digest, utcnow()),
            )
            self._emit(
                db,
                "contract.revised",
                "mission",
                mission_id,
                {"contract_id": revision_id, "revision": revision, "content_hash": digest},
            )
        return {"id": revision_id, "revision": revision, "content_hash": digest, "content": content}

    def contract(self, mission_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM contract_revisions WHERE mission_id=? ORDER BY revision DESC LIMIT 1",
                (mission_id,),
            ).fetchone()
        if not row:
            raise StoreError(f"contract not found for mission: {mission_id}")
        result = dict(row)
        result["content"] = json.loads(result["content"])
        return result

    def create_attempt(self, mission_id: str, number: int, worktree: str, branch: str) -> str:
        attempt_id = "A-" + secrets.token_hex(4).upper()
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO attempts
                   (id, mission_id, number, state, worktree, branch, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
                (attempt_id, mission_id, number, worktree, branch, now, now),
            )
            self._emit(
                db,
                "attempt.created",
                "mission",
                mission_id,
                {
                    "attempt_id": attempt_id,
                    "number": number,
                    "worktree": worktree,
                    "branch": branch,
                },
            )
        return attempt_id

    def attempts(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM attempts WHERE mission_id=? ORDER BY number", (mission_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_attempt_state(self, mission_id: str, number: int, state: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, state FROM attempts WHERE mission_id=? AND number=?",
                (mission_id, number),
            ).fetchone()
            if not row:
                return
            db.execute(
                "UPDATE attempts SET state=?, updated_at=? WHERE id=?",
                (state, utcnow(), row["id"]),
            )
            self._emit(
                db,
                "attempt.state_changed",
                "mission",
                mission_id,
                {
                    "attempt_id": row["id"],
                    "number": number,
                    "from": row["state"],
                    "to": state,
                },
            )

    def create_work_order(self, process_id: str, content: dict[str, Any]) -> dict[str, Any]:
        process = next((p for p in self.processes() if p["id"] == process_id), None)
        if not process:
            raise StoreError(f"process not found: {process_id}")
        encoded, digest = self._content_hash(content)
        work_order_id = "W-" + secrets.token_hex(6).upper()
        with self.connect() as db:
            revision = int(
                db.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM work_orders WHERE process_id=?",
                    (process_id,),
                ).fetchone()[0]
            )
            db.execute(
                """INSERT INTO work_orders
                   (id, mission_id, process_id, revision, content, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    work_order_id,
                    process["mission_id"],
                    process_id,
                    revision,
                    encoded,
                    digest,
                    utcnow(),
                ),
            )
            self._emit(
                db,
                "work_order.created",
                "process",
                process_id,
                {"work_order_id": work_order_id, "revision": revision, "content_hash": digest},
            )
        return {
            "id": work_order_id,
            "revision": revision,
            "content_hash": digest,
            "content": content,
        }

    def work_order(self, process_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM work_orders WHERE process_id=? ORDER BY revision DESC LIMIT 1",
                (process_id,),
            ).fetchone()
        if not row:
            raise StoreError(f"work order not found for process: {process_id}")
        result = dict(row)
        result["content"] = json.loads(result["content"])
        return result

    def create_worker_session(self, process_id: str) -> str:
        session_id = "S-" + secrets.token_hex(5).upper()
        with self.connect() as db:
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM worker_sessions WHERE process_id=?",
                    (process_id,),
                ).fetchone()[0]
            )
            db.execute(
                """INSERT INTO worker_sessions
                   (id, process_id, sequence, state, started_at)
                   VALUES (?, ?, ?, 'created', ?)""",
                (session_id, process_id, sequence, utcnow()),
            )
            db.execute(
                "UPDATE processes SET session_id=?, updated_at=? WHERE id=?",
                (session_id, utcnow(), process_id),
            )
            self._emit(
                db,
                "worker_session.created",
                "process",
                process_id,
                {"session_id": session_id, "sequence": sequence},
            )
        return session_id

    def worker_sessions(self, process_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM worker_sessions WHERE process_id=? ORDER BY sequence", (process_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            result.append(item)
        return result

    def update_worker_session_metadata(self, process_id: str, metadata: dict[str, Any]) -> None:
        forbidden = {key for key in metadata if "token" in key.lower() or "secret" in key.lower()}
        if forbidden:
            raise StoreError(f"secret-shaped session metadata fields are forbidden: {forbidden}")
        with self.connect() as db:
            process = db.execute(
                "SELECT session_id FROM processes WHERE id=?", (process_id,)
            ).fetchone()
            if not process or not process["session_id"]:
                raise StoreError(f"active worker session not found for process: {process_id}")
            current = db.execute(
                "SELECT metadata FROM worker_sessions WHERE id=?", (process["session_id"],)
            ).fetchone()
            merged = json.loads(current["metadata"] if current else "{}")
            merged.update(metadata)
            db.execute(
                "UPDATE worker_sessions SET metadata=? WHERE id=?",
                (json.dumps(merged, sort_keys=True), process["session_id"]),
            )

    def observe_provider(
        self,
        backend: str,
        status: str,
        source: str,
        confidence: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        if status not in {"healthy", "degraded", "limited", "unhealthy", "unknown"}:
            raise StoreError(f"invalid provider status: {status}")
        if confidence not in {"authoritative", "estimated", "unknown"}:
            raise StoreError(f"invalid observation confidence: {confidence}")
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO provider_observations
                   (backend, status, source, confidence, details, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    backend,
                    status,
                    source,
                    confidence,
                    json.dumps(details or {}, sort_keys=True),
                    utcnow(),
                ),
            )
            self._emit(
                db,
                "provider.observed",
                "provider",
                backend,
                {"status": status, "source": source, "confidence": confidence},
                "attention" if status in {"limited", "unhealthy"} else "info",
            )
            return int(cur.lastrowid)

    def provider_status(self, backend: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE p.backend=?" if backend else ""
        params: tuple[Any, ...] = (backend,) if backend else ()
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT p.* FROM provider_observations p
                    JOIN (
                        SELECT backend, MAX(id) AS id
                        FROM provider_observations GROUP BY backend
                    ) latest ON latest.id=p.id
                    {where}
                    ORDER BY p.backend""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def latest_provider_status(self, backend: str) -> dict[str, Any] | None:
        items = self.provider_status(backend)
        return items[0] if items else None

    def mission(self, mission_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if not row:
            raise StoreError(f"mission not found: {mission_id}")
        result = dict(row)
        result["allowed_backends"] = json.loads(result["allowed_backends"])
        result["allowed_models"] = json.loads(result["allowed_models"])
        return result

    def missions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def processes(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if mission_id:
                rows = db.execute(
                    "SELECT * FROM processes WHERE mission_id=? ORDER BY attempt, created_at",
                    (mission_id,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM processes ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["checkpoint"] = json.loads(item["checkpoint"])
            item.pop("token_hash", None)
            result.append(item)
        return result

    def create_process(
        self,
        mission_id: str,
        backend: str,
        model: str,
        worktree: str,
        routing_reason: str,
        attempt: int | None = None,
        ttl: int = 900,
    ) -> tuple[str, str]:
        process_id = "P-" + secrets.token_hex(4).upper()
        token = secrets.token_urlsafe(32)
        now = utcnow()
        with self.connect() as db:
            if attempt is None:
                attempt = int(
                    db.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM processes WHERE mission_id=?",
                        (mission_id,),
                    ).fetchone()[0]
                )
            db.execute(
                """INSERT INTO processes
                   (id, mission_id, attempt, backend, model, state, token_hash,
                    token_expires_at, worktree, checkpoint, routing_reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, '{}', ?, ?, ?)""",
                (
                    process_id,
                    mission_id,
                    attempt,
                    backend,
                    model,
                    token_hash(token),
                    future(ttl),
                    worktree,
                    routing_reason,
                    now,
                    now,
                ),
            )
            self._emit(
                db,
                "process.created",
                "process",
                process_id,
                {"mission_id": mission_id, "backend": backend, "model": model, "attempt": attempt},
            )
        return process_id, token

    def rotate_process_authority(self, process_id: str, ttl: int = 900) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            row = db.execute("SELECT state FROM processes WHERE id=?", (process_id,)).fetchone()
            if not row:
                raise StoreError(f"process not found: {process_id}")
            if row["state"] not in {"failed", "orphaned", "cancelled"}:
                raise TransitionError(
                    f"cannot rotate authority for process in state {row['state']}"
                )
            db.execute(
                """UPDATE processes
                   SET state='created', token_hash=?, token_expires_at=?, pid=NULL,
                       checkpoint='{}', updated_at=?
                   WHERE id=?""",
                (token_hash(token), future(ttl), utcnow(), process_id),
            )
            self._emit(
                db,
                "process.authority_rotated",
                "process",
                process_id,
                {"reason": "authorized session retry"},
            )
        return token

    def require_process(self, process_id: str, token: str | None, ttl: int = 900) -> dict[str, Any]:
        if not token:
            raise AuthorizationError("a process token is required")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()
            if not row:
                raise AuthorizationError("process does not exist")
            if row["token_expires_at"] <= now:
                raise AuthorizationError("process authority is expired")
            if not secrets.compare_digest(row["token_hash"], token_hash(token)):
                raise AuthorizationError("process token is invalid")
            db.execute(
                "UPDATE processes SET token_expires_at=?, updated_at=? WHERE id=?",
                (future(ttl), now, process_id),
            )
            return dict(row)

    def update_process(
        self,
        process_id: str,
        token: str | None,
        state: str,
        checkpoint: dict[str, Any],
        severity: str = "info",
    ) -> None:
        process = self.require_process(process_id, token)
        with self.connect() as db:
            db.execute(
                "UPDATE processes SET state=?, checkpoint=?, updated_at=? WHERE id=?",
                (state, json.dumps(checkpoint, sort_keys=True), utcnow(), process_id),
            )
            if process.get("session_id"):
                terminal = state in {"completed", "failed", "cancelled", "orphaned"}
                db.execute(
                    """UPDATE worker_sessions
                       SET state=?, ended_at=CASE WHEN ? THEN ? ELSE ended_at END
                       WHERE id=?""",
                    (state, terminal, utcnow(), process["session_id"]),
                )
            self._emit(
                db,
                "process.checkpoint",
                "process",
                process_id,
                {"mission_id": process["mission_id"], "state": state, **checkpoint},
                severity,
            )

    def set_process_pid(self, process_id: str, pid: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE processes SET pid=?, updated_at=? WHERE id=?", (pid, utcnow(), process_id)
            )

    def set_process_session(self, process_id: str, backend_handle: str) -> None:
        with self.connect() as db:
            process = db.execute(
                "SELECT session_id FROM processes WHERE id=?", (process_id,)
            ).fetchone()
            if not process or not process["session_id"]:
                raise StoreError(f"active worker session not found for process: {process_id}")
            db.execute(
                "UPDATE worker_sessions SET backend_handle=?, state='running' WHERE id=?",
                (backend_handle, process["session_id"]),
            )
            self._emit(
                db,
                "worker_session.started",
                "process",
                process_id,
                {"session_id": process["session_id"], "backend_handle": backend_handle},
            )

    def transition_mission(self, mission_id: str, target: str, allowed_from: set[str]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not row:
                raise StoreError(f"mission not found: {mission_id}")
            if row["state"] not in allowed_from:
                raise TransitionError(f"cannot transition mission from {row['state']} to {target}")
            db.execute(
                "UPDATE missions SET state=?, updated_at=? WHERE id=?",
                (target, utcnow(), mission_id),
            )
            severity = "approval" if target == "awaiting_approval" else "info"
            self._emit(
                db,
                "mission.transitioned",
                "mission",
                mission_id,
                {"from": row["state"], "to": target},
                severity,
            )
