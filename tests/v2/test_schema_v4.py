from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modules.journal import (
    CURRENT_SCHEMA_VERSION,
    InitializationRequest,
    JournalError,
    MigrationRejected,
    MigrationRequest,
    _backup_path,
    _quarantine_path,
    initialize_root,
    migrate_legacy_root,
    upgrade_schema3_root,
)

FIXTURES = Path(__file__).parents[1] / "legacy" / "fixtures"
SCHEMA3_FIXTURE = Path(__file__).parent / "fixtures" / "schema-v3"
FIXED_TIME = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)

CORE_TABLES = {
    "approval_decisions",
    "authority_epochs",
    "event_envelopes",
    "event_targets",
    "evidence_records",
    "operation_conflicts",
    "operation_targets",
    "operations",
}


def _request(operation_id: str) -> MigrationRequest:
    return MigrationRequest(
        operation_id=operation_id,
        invoker_id="maintenance:schema-v4-test",
        expected_schema=2,
        expected_root_state="quiesced",
        expected_high_water_event_id=13,
    )


def _make_transitional_schema3(root: Path) -> int:
    manifest = json.loads((SCHEMA3_FIXTURE / "manifest.json").read_text())
    migration = manifest["migration"]
    root.mkdir()
    shutil.copy2(SCHEMA3_FIXTURE / "aos.db", root / "aos.db")
    shutil.copytree(SCHEMA3_FIXTURE / "artifacts", root / "artifacts")
    shutil.copytree(SCHEMA3_FIXTURE / "blobs", root / "blobs")
    backup = _backup_path(
        root,
        str(migration["operation_id"]),
        str(migration["snapshot_digest"]),
    )
    backup.parent.mkdir(parents=True, mode=0o700)
    backup.parent.parent.chmod(0o700)
    backup.parent.chmod(0o700)
    shutil.copy2(SCHEMA3_FIXTURE / "legacy-backup.sqlite3", backup)
    backup.chmod(0o600)
    return int(manifest["database"]["event_high_water"])


def test_legacy_migration_crosses_directly_into_complete_schema_v4(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)

    outcome = migrate_legacy_root(
        root,
        _request("OP-SCHEMA-V4-DIRECT"),
        clock=lambda: FIXED_TIME,
    )

    assert CURRENT_SCHEMA_VERSION == 4
    assert outcome.target_schema == 4
    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "4",
        )
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert tables >= CORE_TABLES

        missing_envelopes = db.execute(
            """
            SELECT e.id, e.kind
            FROM events AS e
            LEFT JOIN event_envelopes AS envelope ON envelope.event_id=e.id
            WHERE e.id >= ? AND envelope.event_id IS NULL
            ORDER BY e.id
            """,
            (outcome.boundary_event_id,),
        ).fetchall()
        assert missing_envelopes == []

        operation = db.execute(
            """
            SELECT action, request_hash, invoker_kind, invoker_id, status,
                   admission_event_id
            FROM operations
            WHERE operation_id=?
            """,
            (outcome.operation_id,),
        ).fetchone()
        assert operation is not None
        assert operation[0] == "journal.migrate"
        assert len(str(operation[1])) == 64
        assert operation[2:4] == (
            "maintenance_operator",
            "maintenance:schema-v4-test",
        )
        assert operation[4] == "completed"
        assert operation[5] == outcome.boundary_event_id - 1
        assert db.execute(
            "SELECT kind FROM events WHERE id=?",
            (operation[5],),
        ).fetchone() == ("gate.admitted",)
        assert db.execute(
            "SELECT admission_id FROM event_envelopes WHERE event_id=?",
            (outcome.boundary_event_id,),
        ).fetchone() == (operation[5],)

        revision_columns = {
            str(row[1]): row
            for row in db.execute("PRAGMA table_info(aggregate_revisions)").fetchall()
        }
        assert revision_columns["snapshot_digest"][3] == 0
        assert "genesis_event_id" in revision_columns

        triggers = {
            str(row[0])
            for row in db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='trigger'
                """
            ).fetchall()
        }
        assert {
            "events_no_delete",
            "events_no_update",
            "event_envelopes_no_delete",
            "event_envelopes_no_update",
            "event_targets_no_delete",
            "event_targets_no_update",
        } <= triggers


def test_explicit_schema3_upgrade_preserves_history_and_admits_the_new_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    high_water = _make_transitional_schema3(root)

    outcome = upgrade_schema3_root(
        root,
        MigrationRequest(
            operation_id="OP-SCHEMA-V3-TO-V4",
            invoker_id="maintenance:schema-v4-test",
            expected_schema=3,
            expected_root_state="quiesced",
            expected_high_water_event_id=high_water,
        ),
        clock=lambda: FIXED_TIME,
    )
    replayed = upgrade_schema3_root(
        root,
        MigrationRequest(
            operation_id="OP-SCHEMA-V3-TO-V4",
            invoker_id="maintenance:schema-v4-test",
            expected_schema=3,
            expected_root_state="quiesced",
            expected_high_water_event_id=high_water,
        ),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    assert replayed.status == "replayed"
    assert replayed.upgrade_event_id == outcome.upgrade_event_id
    assert outcome.source_schema == 3
    assert outcome.target_schema == 4
    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "4",
        )
        boundary = int(
            db.execute("SELECT MIN(id) FROM events WHERE kind='migration.boundary'").fetchone()[0]
        )
        assert (
            db.execute(
                """
            SELECT e.id
            FROM events AS e
            LEFT JOIN event_envelopes AS envelope ON envelope.event_id=e.id
            WHERE e.id >= ? AND envelope.event_id IS NULL
            """,
                (boundary,),
            ).fetchall()
            == []
        )
        assert db.execute(
            """
            SELECT COUNT(*)
            FROM events
            WHERE kind='gate.admitted'
            """
        ).fetchone() == (1,)
        assert db.execute(
            """
            SELECT admission_event_id
            FROM operations
            WHERE operation_id='OP-STRUCTURE-V3'
            """
        ).fetchone() == (None,)
        operation = db.execute(
            """
            SELECT action, status, admission_event_id, completed_event_id
            FROM operations
            WHERE operation_id=?
            """,
            (outcome.operation_id,),
        ).fetchone()
        assert operation[:2] == ("journal.upgrade_schema", "completed")
        assert db.execute(
            "SELECT kind FROM events WHERE id=?",
            (operation[2],),
        ).fetchone() == ("gate.admitted",)
        assert operation[3] == outcome.upgrade_event_id
        assert db.execute(
            "SELECT admission_id FROM event_envelopes WHERE event_id=?",
            (outcome.upgrade_event_id,),
        ).fetchone() == (operation[2],)


def test_schema3_upgrade_rejects_unknown_post_boundary_events_before_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _make_transitional_schema3(root)
    with sqlite3.connect(root / "aos.db") as db:
        cursor = db.execute(
            """
            INSERT INTO events(
                occurred_at, kind, severity, aggregate_type, aggregate_id, payload
            ) VALUES (?, 'mission.forged', 'info', 'mission', 'M-FORGED', '{}')
            """,
            (FIXED_TIME.isoformat(timespec="milliseconds"),),
        )
        high_water = int(cursor.lastrowid)
    before = hashlib.sha256((root / "aos.db").read_bytes()).hexdigest()

    with pytest.raises(MigrationRejected, match="unknown post-boundary"):
        upgrade_schema3_root(
            root,
            MigrationRequest(
                operation_id="OP-SCHEMA-V3-UNKNOWN-EVENT",
                invoker_id="maintenance:schema-v4-test",
                expected_schema=3,
                expected_root_state="quiesced",
                expected_high_water_event_id=high_water,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert hashlib.sha256((root / "aos.db").read_bytes()).hexdigest() == before
    assert not list((tmp_path / ".aos-backups").glob("*/*UNKNOWN-EVENT*"))


def test_schema3_upgrade_rejects_a_structurally_forged_transition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    high_water = _make_transitional_schema3(root)
    with sqlite3.connect(root / "aos.db") as db:
        db.execute("ALTER TABLE missions ADD COLUMN forged_value TEXT")

    with pytest.raises(MigrationRejected, match="committed M1a schema"):
        upgrade_schema3_root(
            root,
            MigrationRequest(
                operation_id="OP-SCHEMA-V3-FORGED-STRUCTURE",
                invoker_id="maintenance:schema-v4-test",
                expected_schema=3,
                expected_root_state="quiesced",
                expected_high_water_event_id=high_water,
            ),
            clock=lambda: FIXED_TIME,
        )

    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "3",
        )


def test_schema3_upgrade_recovers_pending_cleanup_and_replays_original_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    high_water = _make_transitional_schema3(root)
    artifact = root / "artifacts" / "P-E26D600C.jsonl"
    artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    quarantine = _quarantine_path(root, artifact_digest)
    quarantine.parent.mkdir(parents=True, mode=0o700)
    quarantine.parent.parent.chmod(0o700)
    quarantine.parent.chmod(0o700)
    shutil.copy2(artifact, quarantine)
    quarantine.chmod(0o600)
    with sqlite3.connect(root / "aos.db") as db:
        db.execute(
            """
            UPDATE legacy_artifacts
            SET status='quarantined', blob_digest=NULL,
                quarantine_reason='secret-bearing recovery fixture'
            WHERE legacy_path='artifacts/P-E26D600C.jsonl'
            """
        )
        db.execute(
            """
            UPDATE migration_cleanup
            SET status='pending', pending_items=1, completed_at=NULL,
                completed_event_id=NULL
            WHERE operation_id='OP-STRUCTURE-V3'
            """
        )

    request = MigrationRequest(
        operation_id="OP-SCHEMA-V3-CLEANUP-RECOVERY",
        invoker_id="maintenance:schema-v4-test",
        expected_schema=3,
        expected_root_state="quiesced",
        expected_high_water_event_id=high_water,
    )
    completed = upgrade_schema3_root(root, request, clock=lambda: FIXED_TIME)
    replayed = upgrade_schema3_root(root, request, clock=lambda: FIXED_TIME)

    assert completed.status == "completed"
    assert replayed.status == "replayed"
    assert not artifact.exists()
    with sqlite3.connect(root / "aos.db") as db:
        cleanup = db.execute(
            """
            SELECT status, completed_event_id
            FROM migration_cleanup
            WHERE operation_id='OP-STRUCTURE-V3'
            """
        ).fetchone()
        assert cleanup == ("completed", high_water + 1)
        assert db.execute(
            "SELECT kind FROM events WHERE id=?",
            (cleanup[1],),
        ).fetchone() == ("migration.cleanup_completed",)
        assert db.execute(
            "SELECT admission_id FROM event_envelopes WHERE event_id=?",
            (cleanup[1],),
        ).fetchone() == (None,)
        assert db.execute(
            """
            SELECT admission_event_id, completed_event_id
            FROM operations
            WHERE operation_id='OP-STRUCTURE-V3'
            """
        ).fetchone() == (None, cleanup[1])


def test_schema3_upgrade_replay_requires_its_verified_backup(tmp_path: Path) -> None:
    root = tmp_path / "state"
    high_water = _make_transitional_schema3(root)
    request = MigrationRequest(
        operation_id="OP-SCHEMA-V3-BACKUP-REPLAY",
        invoker_id="maintenance:schema-v4-test",
        expected_schema=3,
        expected_root_state="quiesced",
        expected_high_water_event_id=high_water,
    )
    completed = upgrade_schema3_root(root, request, clock=lambda: FIXED_TIME)
    with sqlite3.connect(root / "aos.db") as db:
        snapshot_digest = str(
            db.execute(
                """
                SELECT snapshot_digest
                FROM migration_backups
                WHERE digest=?
                """,
                (completed.backup_digest,),
            ).fetchone()[0]
        )
    upgrade_backup = _backup_path(root, request.operation_id, snapshot_digest)
    upgrade_backup.unlink()

    with pytest.raises(JournalError, match="migration backup"):
        upgrade_schema3_root(root, request, clock=lambda: FIXED_TIME)


def test_empty_root_initialization_is_explicit_attributable_and_replay_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    request = InitializationRequest(
        operation_id="OP-INITIALIZE-V4-ROOT",
        invoker_id="maintenance:schema-v4-test",
        expected_schema=0,
        expected_root_state="empty",
    )

    completed = initialize_root(root, request, clock=lambda: FIXED_TIME)
    replayed = initialize_root(root, request, clock=lambda: FIXED_TIME)

    assert completed.status == "completed"
    assert replayed.status == "replayed"
    assert replayed.initialization_event_id == completed.initialization_event_id
    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "4",
        )
        assert db.execute(
            """
            SELECT kind
            FROM events
            WHERE id=?
            """,
            (completed.initialization_event_id,),
        ).fetchone() == ("journal.initialized",)
        assert db.execute(
            """
            SELECT invoker_kind, invoker_id, admission_id
            FROM event_envelopes
            WHERE event_id=?
            """,
            (completed.initialization_event_id,),
        ).fetchone() == (
            "maintenance_operator",
            "maintenance:schema-v4-test",
            completed.initialization_event_id - 1,
        )
        assert db.execute(
            """
            SELECT admission_event_id
            FROM operations
            WHERE operation_id=?
            """,
            (completed.operation_id,),
        ).fetchone() == (completed.initialization_event_id - 1,)
        assert db.execute(
            "SELECT kind FROM events WHERE id=?",
            (completed.initialization_event_id - 1,),
        ).fetchone() == ("gate.admitted",)
        assert db.execute(
            """
            SELECT revision, snapshot_digest, genesis_event_id, source_event_id
            FROM aggregate_revisions
            WHERE aggregate_type='journal' AND aggregate_id='root'
            """
        ).fetchone() == (
            1,
            None,
            completed.initialization_event_id,
            completed.initialization_event_id,
        )


def test_initialization_permanently_refuses_a_nonempty_uninitialized_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    sentinel = root / "do-not-touch.txt"
    sentinel.write_text("existing state")

    with pytest.raises(JournalError, match="empty"):
        initialize_root(
            root,
            InitializationRequest(
                operation_id="OP-INITIALIZE-NONEMPTY",
                invoker_id="maintenance:schema-v4-test",
                expected_schema=0,
                expected_root_state="empty",
            ),
            clock=lambda: FIXED_TIME,
        )

    assert sentinel.read_text() == "existing state"
    assert not (root / "aos.db").exists()


def test_event_targets_are_immutable(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize_root(
        root,
        InitializationRequest(
            operation_id="OP-INITIALIZE-IMMUTABLE-TARGETS",
            invoker_id="maintenance:schema-v4-test",
            expected_schema=0,
            expected_root_state="empty",
        ),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(root / "aos.db") as db:
        event_id = int(db.execute("SELECT MIN(event_id) FROM event_targets").fetchone()[0])
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE event_targets SET role='target' WHERE event_id=?",
                (event_id,),
            )
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM event_targets WHERE event_id=?", (event_id,))


def test_initialization_cleanup_never_deletes_a_replacement_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    replacement = b"replacement-owned-by-another-writer"

    def replace_before_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        del source, follow_symlinks
        Path(destination).write_bytes(replacement)
        raise JournalError("simulated concurrent database installation")

    monkeypatch.setattr("modules.journal.os.link", replace_before_link)

    with pytest.raises(JournalError, match="concurrent database"):
        initialize_root(
            root,
            InitializationRequest(
                operation_id="OP-INITIALIZE-PATH-RACE",
                invoker_id="maintenance:schema-v4-test",
                expected_schema=0,
                expected_root_state="empty",
            ),
            clock=lambda: FIXED_TIME,
        )

    assert (root / "aos.db").read_bytes() == replacement
    assert not list(root.glob(".aos-initialize-*"))
