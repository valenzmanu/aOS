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

SCHEMA_VERSION = 1


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
                CREATE INDEX IF NOT EXISTS events_aggregate
                    ON events(aggregate_type, aggregate_id, id);
                CREATE INDEX IF NOT EXISTS processes_mission
                    ON processes(mission_id, attempt);
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
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
                    "SELECT * FROM processes WHERE mission_id=? ORDER BY attempt", (mission_id,)
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
        ttl: int = 900,
    ) -> tuple[str, str]:
        process_id = "P-" + secrets.token_hex(4).upper()
        token = secrets.token_urlsafe(32)
        now = utcnow()
        with self.connect() as db:
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

    def set_process_session(self, process_id: str, session_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE processes SET session_id=?, updated_at=? WHERE id=?",
                (session_id, utcnow(), process_id),
            )
            self._emit(
                db,
                "worker_session.started",
                "process",
                process_id,
                {"session_id": session_id},
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
