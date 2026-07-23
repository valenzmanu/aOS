from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modules.journal import (
    CURRENT_SCHEMA_VERSION,
    MigrationRejected,
    MigrationRequest,
    inspect_journal,
    migrate_legacy_root,
)

FIXTURES = Path(__file__).parents[1] / "legacy" / "fixtures"
FIXED_TIME = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
SNAPSHOT_PROJECTION_TABLES = (
    "leases",
    "missions",
    "processes",
    "inbox_cursors",
    "contract_revisions",
    "attempts",
    "work_orders",
    "worker_sessions",
    "provider_observations",
)


def read_legacy_events(db_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(db_path) as db:
        return db.execute(
            """
            SELECT id, occurred_at, kind, severity, aggregate_type, aggregate_id, payload
            FROM events
            ORDER BY id
            """
        ).fetchall()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def root_entries(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def file_tree_manifest(root: Path) -> list[tuple[str, str, str | None]]:
    manifest: list[tuple[str, str, str | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.name.endswith((".db-shm", ".db-wal")):
            continue
        if path.is_symlink():
            manifest.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            manifest.append((relative, "file", file_digest(path)))
        elif path.is_dir():
            manifest.append((relative, "directory", None))
    return manifest


def logical_tables(db_path: Path, tables: tuple[str, ...]) -> dict[str, object]:
    with sqlite3.connect(db_path) as db:
        result: dict[str, object] = {}
        for table in tables:
            columns = [
                str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            rows = [list(row) for row in db.execute(f'SELECT * FROM "{table}"')]
            rows.sort(
                key=lambda row: json.dumps(
                    row,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            result[table] = {"columns": columns, "rows": rows}
    return result


def all_logical_tables(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as db:
        tables = tuple(
            str(row[0])
            for row in db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )
    return logical_tables(db_path, tables)


def rows_without_columns(
    table: object,
    excluded: set[str],
) -> list[dict[str, object]]:
    assert isinstance(table, dict)
    columns = table["columns"]
    rows = table["rows"]
    assert isinstance(columns, list)
    assert isinstance(rows, list)
    return [
        {
            str(column): value
            for column, value in zip(columns, row, strict=True)
            if column not in excluded
        }
        for row in rows
    ]


def contested_aggregates(db_path: Path) -> set[tuple[str, str]]:
    tables = {
        "mission": ("missions", "id"),
        "process": ("processes", "id"),
        "attempt": ("attempts", "id"),
        "worker_session": ("worker_sessions", "id"),
        "contract_revision": ("contract_revisions", "id"),
        "work_order": ("work_orders", "id"),
        "lease": ("leases", "kind"),
    }
    with sqlite3.connect(db_path) as db:
        return {
            (aggregate_type, str(row[0]))
            for aggregate_type, (table, column) in tables.items()
            for row in db.execute(f'SELECT "{column}" FROM "{table}" ORDER BY "{column}"')
        }


def seed_populated_schema_v1(db_path: Path) -> None:
    now = "2026-07-23T10:00:02.000+00:00"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO leases(kind, holder, token_hash, expires_at, updated_at)
            VALUES ('liaison', 'fixture-v1', ?, '2026-07-24T10:00:02.000+00:00', ?)
            """,
            ("c" * 64, now),
        )
        db.execute(
            """
            INSERT INTO missions(
                id, project, objective, state, allowed_backends, allowed_models,
                allow_anthropic, verify_command, created_at, updated_at
            ) VALUES (
                'M-V1', '/synthetic/project', 'migrate v1 projections', 'implementing',
                '["codex"]', '["gpt-5.6-sol"]', 0, 'pytest', ?, ?
            )
            """,
            (now, now),
        )
        db.execute(
            """
            INSERT INTO processes(
                id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, pid, checkpoint,
                routing_reason, created_at, updated_at
            ) VALUES (
                'P-V1', 'M-V1', 1, 'codex', 'gpt-5.6-sol', 'running',
                ?, '2026-07-24T10:00:02.000+00:00', '/synthetic/worktree',
                'legacy-provider-handle', NULL, '{}', 'legacy route', ?, ?
            )
            """,
            ("a" * 64, now, now),
        )


def test_migrate_actual_schema_v1_preserves_history_and_adds_admission_and_boundary(
    tmp_path: Path,
) -> None:
    source = FIXTURES / "schema-v1"
    root = tmp_path / "state"
    shutil.copytree(source, root)
    db_path = root / "aos.db"
    legacy_events = read_legacy_events(db_path)
    with sqlite3.connect(db_path) as db:
        legacy_cursor = db.execute(
            "SELECT consumer, event_id FROM inbox_cursors ORDER BY consumer"
        ).fetchall()

    outcome = migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-V1",
            invoker_id="maintenance:test",
            expected_schema=1,
            expected_root_state="quiesced",
            expected_high_water_event_id=int(legacy_events[-1][0]),
        ),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    assert outcome.source_schema == 1
    assert outcome.target_schema == CURRENT_SCHEMA_VERSION
    assert outcome.legacy_high_water_event_id == legacy_events[-1][0]

    migrated_events = read_legacy_events(db_path)
    assert migrated_events[: len(legacy_events)] == legacy_events
    assert len(migrated_events) == len(legacy_events) + 2
    admission = migrated_events[-2]
    assert admission[0] == outcome.boundary_event_id - 1
    assert admission[1] == FIXED_TIME.isoformat(timespec="milliseconds")
    assert admission[2] == "gate.admitted"
    boundary = migrated_events[-1]
    assert boundary[0] == outcome.boundary_event_id
    assert boundary[1] == FIXED_TIME.isoformat(timespec="milliseconds")
    assert boundary[2] == "migration.boundary"

    with sqlite3.connect(db_path) as db:
        schema = int(
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        )
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        migrated_cursor = db.execute(
            "SELECT consumer, event_id FROM inbox_cursors ORDER BY consumer"
        ).fetchall()
    assert schema == CURRENT_SCHEMA_VERSION
    assert migrated_cursor == legacy_cursor
    assert integrity == "ok"
    assert foreign_key_errors == []


def test_migrate_rejects_metadata_spoof_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    db_path = root / "aos.db"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        high_water = int(db.execute("SELECT MAX(id) FROM events").fetchone()[0])
    before = file_digest(db_path)

    with pytest.raises(MigrationRejected, match="structure"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-SPOOFED-SCHEMA",
                invoker_id="maintenance:test",
                expected_schema=1,
                expected_root_state="quiesced",
                expected_high_water_event_id=high_water,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(db_path) == before


def test_migrate_replays_same_operation_without_new_event(tmp_path: Path) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    request = MigrationRequest(
        operation_id="OP-MIGRATE-REPLAY",
        invoker_id="maintenance:test",
        expected_schema=1,
        expected_root_state="quiesced",
        expected_high_water_event_id=2,
    )

    completed = migrate_legacy_root(root, request, clock=lambda: FIXED_TIME)
    db_path = root / "aos.db"
    completed_events = read_legacy_events(db_path)
    completed_digest = file_digest(db_path)
    completed_tree = file_tree_manifest(tmp_path)

    replayed = migrate_legacy_root(root, request, clock=lambda: FIXED_TIME)

    assert replayed.status == "replayed"
    assert replayed.boundary_event_id == completed.boundary_event_id
    assert read_legacy_events(db_path) == completed_events
    assert file_digest(db_path) == completed_digest
    assert file_tree_manifest(tmp_path) == completed_tree


def test_populated_migration_publishes_deterministic_snapshot_and_revisions(
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        shutil.copytree(FIXTURES / "schema-v2", root)
    expected_aggregates = contested_aggregates(roots[0] / "aos.db")
    expected_projections = logical_tables(
        roots[0] / "aos.db",
        SNAPSHOT_PROJECTION_TABLES,
    )
    contracts = expected_projections["contract_revisions"]
    assert isinstance(contracts, dict)
    assert isinstance(contracts["columns"], list)
    assert isinstance(contracts["rows"], list)
    contracts["columns"].append("attempt_ceiling")
    for row in contracts["rows"]:
        row.append(3)
    expected_events = read_legacy_events(roots[0] / "aos.db")

    outcomes = [
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id=f"OP-MIGRATE-SNAPSHOT-{index}",
                invoker_id="maintenance:test",
                expected_schema=2,
                expected_root_state="quiesced",
                expected_high_water_event_id=13,
            ),
            clock=lambda: FIXED_TIME,
        )
        for index, root in enumerate(roots, start=1)
    ]

    assert outcomes[0].snapshot_digest == outcomes[1].snapshot_digest
    digest = outcomes[0].snapshot_digest
    snapshot_paths = [root / "blobs" / "sha256" / digest[:2] / digest for root in roots]
    assert snapshot_paths[0].read_bytes() == snapshot_paths[1].read_bytes()
    snapshot = json.loads(snapshot_paths[0].read_text())
    assert snapshot["format"] == "aos.migration-snapshot.v2"
    assert snapshot["classifier_version"] == "aos.migration-policy.v1"
    assert snapshot["projections"] == expected_projections

    with sqlite3.connect(roots[0] / "aos.db") as db:
        revisions = {
            (str(row[0]), str(row[1]), int(row[2]), str(row[3]))
            for row in db.execute(
                """
                SELECT aggregate_type, aggregate_id, revision, snapshot_digest
                FROM migration_baseline_revisions
                """
            )
        }
        artifact_rows = [
            {
                "blob_digest": row[3],
                "digest": row[1],
                "legacy_path": row[0],
                "quarantine_reason": row[5],
                "size": row[2],
                "status": row[4],
            }
            for row in db.execute(
                """
                SELECT legacy_path, digest, size, blob_digest, status, quarantine_reason
                FROM legacy_artifacts
                ORDER BY legacy_path
                """
            )
        ]
    assert revisions == {
        (aggregate_type, aggregate_id, 1, digest)
        for aggregate_type, aggregate_id in expected_aggregates
    }
    assert snapshot["legacy_artifacts"] == artifact_rows

    migrated_events = read_legacy_events(roots[0] / "aos.db")
    assert migrated_events[: len(expected_events)] == expected_events
    migrated_projections = logical_tables(
        roots[0] / "aos.db",
        SNAPSHOT_PROJECTION_TABLES,
    )
    for table in (
        "inbox_cursors",
        "contract_revisions",
        "work_orders",
        "provider_observations",
    ):
        assert migrated_projections[table] == expected_projections[table]
    assert rows_without_columns(
        migrated_projections["missions"],
        {"state", "updated_at"},
    ) == rows_without_columns(
        expected_projections["missions"],
        {"state", "updated_at"},
    )
    assert rows_without_columns(
        migrated_projections["processes"],
        {"pid", "state", "token_expires_at", "updated_at"},
    ) == rows_without_columns(
        expected_projections["processes"],
        {"pid", "state", "token_expires_at", "updated_at"},
    )
    assert rows_without_columns(
        migrated_projections["attempts"],
        {"state", "updated_at"},
    ) == rows_without_columns(
        expected_projections["attempts"],
        {"state", "updated_at"},
    )
    assert rows_without_columns(
        migrated_projections["worker_sessions"],
        {"ended_at", "state"},
    ) == rows_without_columns(
        expected_projections["worker_sessions"],
        {"ended_at", "state"},
    )
    for table in ("contract_revisions", "work_orders"):
        table_data = migrated_projections[table]
        assert isinstance(table_data, dict)
        columns = table_data["columns"]
        rows = table_data["rows"]
        assert isinstance(columns, list)
        assert isinstance(rows, list)
        content_index = columns.index("content")
        hash_index = columns.index("content_hash")
        for row in rows:
            assert hashlib.sha256(str(row[content_index]).encode()).hexdigest() == row[hash_index]


def test_migration_ingests_safe_legacy_artifact_into_cas(tmp_path: Path) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    artifact = next((root / "artifacts").glob("*.jsonl"))
    artifact_bytes = artifact.read_bytes()
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    historical_path = "/synthetic/aos-fixture/state/artifacts/P-E26D600C.jsonl"
    with sqlite3.connect(root / "aos.db") as db:
        historical_payload = db.execute("SELECT payload FROM events WHERE id=11").fetchone()[0]

    outcome = migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-SAFE-ARTIFACT",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    cas_path = root / "blobs" / "sha256" / artifact_digest[:2] / artifact_digest
    assert cas_path.read_bytes() == artifact_bytes
    with sqlite3.connect(root / "aos.db") as db:
        records = db.execute(
            """
            SELECT legacy_path, digest, size, status, blob_digest, source_event_id
            FROM legacy_artifacts
            WHERE legacy_path IN (?, ?)
            ORDER BY legacy_path
            """,
            (historical_path, artifact.relative_to(root).as_posix()),
        ).fetchall()
        migrated_payload = db.execute("SELECT payload FROM events WHERE id=11").fetchone()[0]
    assert records == [
        (
            historical_path,
            artifact_digest,
            len(artifact_bytes),
            "available",
            artifact_digest,
            outcome.boundary_event_id,
        ),
        (
            artifact.relative_to(root).as_posix(),
            artifact_digest,
            len(artifact_bytes),
            "available",
            artifact_digest,
            outcome.boundary_event_id,
        ),
    ]
    assert migrated_payload == historical_payload


def test_migration_quarantines_secret_bearing_artifact_outside_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / "unsafe.json"
    unsafe_bytes = b'{"provider_key":"' + b"sk-" + b'fixture-secret-material-1234567890"}\n'
    unsafe.write_bytes(unsafe_bytes)
    unsafe_digest = hashlib.sha256(unsafe_bytes).hexdigest()

    migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-UNSAFE-ARTIFACT",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(root / "aos.db") as db:
        record = db.execute(
            """
            SELECT digest, status, blob_digest, quarantine_reason
            FROM legacy_artifacts
            WHERE legacy_path='artifacts/unsafe.json'
            """
        ).fetchone()
    assert record is not None
    assert record[0] == unsafe_digest
    assert record[1] == "quarantined"
    assert record[2] is None
    assert "secret" in str(record[3])
    assert not (root / "blobs" / "sha256" / unsafe_digest[:2] / unsafe_digest).exists()
    assert not unsafe.exists()
    quarantine = list((tmp_path / ".aos-quarantine").glob(f"*/{unsafe_digest}"))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == unsafe_bytes


def test_migration_rejects_symlinked_artifact_root_before_any_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    shutil.rmtree(root / "artifacts")
    outside = tmp_path / "outside-private"
    outside.mkdir()
    outside_artifact = outside / "private.json"
    outside_bytes = b'{"credential":"outside-only-value"}\n'
    outside_artifact.write_bytes(outside_bytes)
    (root / "artifacts").symlink_to(outside, target_is_directory=True)
    before = file_digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="artifact root.*symbolic link"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-MIGRATE-SYMLINKED-ARTIFACT-ROOT",
                invoker_id="maintenance:test",
                expected_schema=2,
                expected_root_state="quiesced",
                expected_high_water_event_id=13,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(root / "aos.db") == before
    assert outside_artifact.read_bytes() == outside_bytes
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()
    assert not (tmp_path / ".aos-quarantine").exists()


def test_migration_quarantines_nested_symlink_without_traversing_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    outside = tmp_path / "outside-private"
    outside.mkdir()
    outside_artifact = outside / "private.json"
    outside_bytes = b'{"credential":"outside-only-value"}\n'
    outside_artifact.write_bytes(outside_bytes)
    link = root / "artifacts" / "outside"
    link.symlink_to(outside, target_is_directory=True)

    migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-NESTED-ARTIFACT-LINK",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    assert outside_artifact.read_bytes() == outside_bytes
    assert not link.exists()
    with sqlite3.connect(root / "aos.db") as db:
        record = db.execute(
            """
            SELECT status, blob_digest, quarantine_reason
            FROM legacy_artifacts
            WHERE legacy_path='artifacts/outside'
            """
        ).fetchone()
    assert record is not None
    assert record[0] == "quarantined"
    assert record[1] is None
    assert "symbolic link" in str(record[2])


def test_migration_records_missing_legacy_artifact_without_fabricating_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    artifact = next((root / "artifacts").glob("*.jsonl"))
    artifact.unlink()
    historical_path = "/synthetic/aos-fixture/state/artifacts/P-E26D600C.jsonl"

    migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-MISSING-ARTIFACT",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(root / "aos.db") as db:
        record = db.execute(
            """
            SELECT digest, size, status, blob_digest, quarantine_reason
            FROM legacy_artifacts
            WHERE legacy_path=?
            """,
            (historical_path,),
        ).fetchone()
    assert record == (
        None,
        None,
        "missing",
        None,
        "referenced legacy artifact is unavailable",
    )


def test_migration_backs_up_committed_wal_history_before_cutover(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    db_path = root / "aos.db"
    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            """
            INSERT INTO events(
                occurred_at, kind, severity, aggregate_type, aggregate_id, payload
            ) VALUES (?, 'legacy.wal.committed', 'info', 'fixture', 'F-WAL', '{}')
            """,
            ("2026-07-23T11:00:00.000+00:00",),
        )
        keeper.commit()
        expected_backup_tables = all_logical_tables(db_path)

        outcome = migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-MIGRATE-WAL-BACKUP",
                invoker_id="maintenance:test",
                expected_schema=2,
                expected_root_state="quiesced",
                expected_high_water_event_id=14,
            ),
            clock=lambda: FIXED_TIME,
        )
    finally:
        keeper.close()

    backups = list((tmp_path / ".aos-backups").glob("*/*.sqlite3"))
    assert len(backups) == 1
    backup_root = tmp_path / ".aos-backups"
    assert not [
        path
        for path in backup_root.rglob("*")
        if path.is_file()
        and (".tmp" in path.name or path.name.endswith(("-wal", "-shm", "-journal")))
    ]
    assert backups[0].stat().st_mode & 0o077 == 0
    assert file_digest(backups[0]) == outcome.backup_digest
    assert all_logical_tables(backups[0]) == expected_backup_tables
    with sqlite3.connect(backups[0]) as backup:
        schema = int(
            backup.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        )
        events = read_legacy_events(backups[0])
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = backup.execute("PRAGMA foreign_key_check").fetchall()
    assert schema == 2
    assert events[-1][2] == "legacy.wal.committed"
    assert integrity == "ok"
    assert foreign_key_errors == []


def test_injected_failure_rolls_back_database_and_retry_completes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    request = MigrationRequest(
        operation_id="OP-MIGRATE-ROLLBACK",
        invoker_id="maintenance:test",
        expected_schema=2,
        expected_root_state="quiesced",
        expected_high_water_event_id=13,
    )
    db_path = root / "aos.db"
    before_tables = all_logical_tables(db_path)
    unsafe = root / "artifacts" / "rollback-secret.json"
    unsafe_bytes = b'{"api_key":"' + b"sk-" + b'rollback-secret-material-1234567890"}'
    unsafe.write_bytes(unsafe_bytes)
    unsafe_digest = hashlib.sha256(unsafe_bytes).hexdigest()

    def fail_before_commit(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("injected before_commit failure")

    with pytest.raises(RuntimeError, match="injected before_commit"):
        migrate_legacy_root(
            root,
            request,
            clock=lambda: FIXED_TIME,
            fault_injector=fail_before_commit,
        )

    assert all_logical_tables(db_path) == before_tables
    assert unsafe.read_bytes() == unsafe_bytes
    protected = list((tmp_path / ".aos-quarantine").glob(f"*/{unsafe_digest}"))
    assert len(protected) == 1
    assert protected[0].read_bytes() == unsafe_bytes
    with sqlite3.connect(db_path) as db:
        schema = int(
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        )
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        target_tables = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table'
              AND name IN ('migration_snapshots', 'migration_backups',
                           'aggregate_revisions', 'legacy_artifacts')
            """
        ).fetchall()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    assert schema == 2
    assert events == 13
    assert target_tables == []
    assert integrity == "ok"

    completed = migrate_legacy_root(root, request, clock=lambda: FIXED_TIME)
    assert completed.status == "completed"
    assert not unsafe.exists()


def test_cutover_closes_legacy_authority_and_orphans_active_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)

    outcome = migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-CLOSE-AUTHORITY",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(root / "aos.db") as db:
        lease_count = db.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        mission_state = db.execute("SELECT state FROM missions").fetchone()[0]
        process_state = db.execute("SELECT state FROM processes").fetchone()[0]
        attempt_state = db.execute("SELECT state FROM attempts").fetchone()[0]
        session_state = db.execute("SELECT state FROM worker_sessions").fetchone()[0]
        epochs = db.execute(
            """
            SELECT kind, subject_id, status, closed_at, close_reason
            FROM legacy_authority_epochs
            ORDER BY kind, subject_id
            """
        ).fetchall()
        reconciliation_kinds = [
            str(row[0])
            for row in db.execute(
                "SELECT kind FROM events WHERE id>? ORDER BY id",
                (outcome.boundary_event_id,),
            )
        ]
    assert lease_count == 0
    assert mission_state == "failed"
    assert process_state == "orphaned"
    assert attempt_state == "orphaned"
    assert session_state == "orphaned"
    assert epochs == [
        (
            "liaison",
            "liaison",
            "closed",
            FIXED_TIME.isoformat(timespec="milliseconds"),
            "legacy writer cutover",
        ),
        (
            "process",
            "P-E26D600C",
            "closed",
            FIXED_TIME.isoformat(timespec="milliseconds"),
            "legacy writer cutover",
        ),
    ]
    assert reconciliation_kinds == [
        "authority.legacy_closed",
        "authority.legacy_closed",
        "process.reconciled",
        "worker_session.reconciled",
        "attempt.reconciled",
        "mission.reconciled",
    ]


def test_schema_v1_backfills_durable_objects_deterministically(
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        shutil.copytree(FIXTURES / "schema-v1", root)
        seed_populated_schema_v1(root / "aos.db")
    expected_v1_projections = logical_tables(
        roots[0] / "aos.db",
        ("leases", "missions", "processes", "inbox_cursors"),
    )
    expected_v1_events = read_legacy_events(roots[0] / "aos.db")

    outcomes = []
    for index, root in enumerate(roots, start=1):
        outcomes.append(
            migrate_legacy_root(
                root,
                MigrationRequest(
                    operation_id=f"OP-MIGRATE-V1-BACKFILL-{index}",
                    invoker_id="maintenance:test",
                    expected_schema=1,
                    expected_root_state="quiesced",
                    expected_high_water_event_id=2,
                ),
                clock=lambda: FIXED_TIME,
            )
        )

    durable_rows: list[tuple[object, ...]] = []
    for root in roots:
        with sqlite3.connect(root / "aos.db") as db:
            durable_rows.append(
                (
                    db.execute(
                        "SELECT id, mission_id, revision, content, content_hash "
                        "FROM contract_revisions"
                    ).fetchone(),
                    db.execute(
                        "SELECT id, mission_id, number, state, worktree, branch FROM attempts"
                    ).fetchone(),
                    db.execute(
                        "SELECT id, mission_id, process_id, revision, content, content_hash "
                        "FROM work_orders"
                    ).fetchone(),
                    db.execute(
                        "SELECT id, process_id, sequence, state, backend_handle "
                        "FROM worker_sessions"
                    ).fetchone(),
                    db.execute(
                        "SELECT session_id, state FROM processes WHERE id='P-V1'"
                    ).fetchone(),
                )
            )
    assert durable_rows[0] == durable_rows[1]
    session = durable_rows[0][3]
    process = durable_rows[0][4]
    assert session is not None
    assert process is not None
    assert str(session[0]).startswith("S-")
    assert session[4] == "legacy-provider-handle"
    assert process[0] == session[0]
    assert session[3] == "orphaned"
    assert process[1] == "orphaned"

    digest = outcomes[0].snapshot_digest
    snapshot = json.loads((roots[0] / "blobs" / "sha256" / digest[:2] / digest).read_text())
    assert snapshot["projections"]["leases"] == expected_v1_projections["leases"]
    assert snapshot["projections"]["missions"] == expected_v1_projections["missions"]
    assert snapshot["projections"]["inbox_cursors"] == expected_v1_projections["inbox_cursors"]
    assert rows_without_columns(
        snapshot["projections"]["processes"],
        {"session_id"},
    ) == rows_without_columns(
        expected_v1_projections["processes"],
        {"session_id"},
    )
    assert read_legacy_events(roots[0] / "aos.db")[: len(expected_v1_events)] == (
        expected_v1_events
    )
    for table in (
        "contract_revisions",
        "attempts",
        "work_orders",
        "worker_sessions",
    ):
        assert snapshot["projections"][table]["rows"], table
    live_immutable = logical_tables(
        roots[0] / "aos.db",
        ("contract_revisions", "work_orders"),
    )
    assert snapshot["projections"]["contract_revisions"] == live_immutable["contract_revisions"]
    contract_columns = snapshot["projections"]["contract_revisions"]["columns"]
    contract_row = snapshot["projections"]["contract_revisions"]["rows"][0]
    assert contract_row[contract_columns.index("attempt_ceiling")] == 3
    assert snapshot["projections"]["work_orders"] == live_immutable["work_orders"]
    snapshot_process = snapshot["projections"]["processes"]
    process_columns = snapshot_process["columns"]
    snapshot_process_row = snapshot_process["rows"][0]
    assert snapshot_process_row[process_columns.index("session_id")] == session[0]
    aggregate_tables = {
        "lease": ("leases", "kind"),
        "mission": ("missions", "id"),
        "process": ("processes", "id"),
        "contract_revision": ("contract_revisions", "id"),
        "attempt": ("attempts", "id"),
        "work_order": ("work_orders", "id"),
        "worker_session": ("worker_sessions", "id"),
    }
    expected_baselines = {
        (aggregate_type, str(row[columns.index(id_column)]), 1, digest)
        for aggregate_type, (table, id_column) in aggregate_tables.items()
        for columns in [snapshot["projections"][table]["columns"]]
        for row in snapshot["projections"][table]["rows"]
    }
    with sqlite3.connect(roots[0] / "aos.db") as db:
        actual_baselines = {
            (str(row[0]), str(row[1]), int(row[2]), str(row[3]))
            for row in db.execute(
                """
                SELECT aggregate_type, aggregate_id, revision, snapshot_digest
                FROM migration_baseline_revisions
                """
            )
        }
    assert actual_baselines == expected_baselines


def test_schema_v1_backfill_derives_attempt_from_all_processes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    db_path = root / "aos.db"
    seed_populated_schema_v1(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE processes
            SET id='P-A', state='failed', pid=NULL,
                updated_at='2026-07-23T10:00:03.000+00:00'
            WHERE id='P-V1'
            """
        )
        db.execute(
            """
            INSERT INTO processes(
                id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, pid, checkpoint,
                routing_reason, created_at, updated_at
            ) VALUES (
                'P-Z', 'M-V1', 1, 'codex', 'gpt-5.6-sol', 'running',
                ?, '2026-07-24T10:00:02.000+00:00', '/synthetic/worktree',
                'newer-provider-handle', NULL, '{}', 'retry in same attempt',
                '2026-07-23T10:00:04.000+00:00',
                '2026-07-23T10:00:05.000+00:00'
            )
            """,
            ("b" * 64,),
        )

    migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-V1-MULTI-PROCESS-ATTEMPT",
            invoker_id="maintenance:test",
            expected_schema=1,
            expected_root_state="quiesced",
            expected_high_water_event_id=2,
        ),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(db_path) as db:
        attempt = db.execute(
            "SELECT state, worktree, updated_at FROM attempts WHERE mission_id='M-V1'"
        ).fetchone()
        process_states = db.execute("SELECT id, state FROM processes ORDER BY id").fetchall()
    assert attempt == (
        "orphaned",
        "/synthetic/worktree",
        FIXED_TIME.isoformat(timespec="milliseconds"),
    )
    assert process_states == [("P-A", "failed"), ("P-Z", "orphaned")]


def test_ordinary_inspection_refuses_legacy_schema_without_migrating(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    db_path = root / "aos.db"
    before = file_digest(db_path)
    entries_before = root_entries(root)

    with pytest.raises(MigrationRejected, match="explicit migration"):
        inspect_journal(root)

    assert file_digest(db_path) == before
    assert root_entries(root) == entries_before
    assert not (root / "blobs").exists()


def test_migration_rejects_live_legacy_process_before_any_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    db_path = root / "aos.db"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE processes SET pid=?, state='running'", (os.getpid(),))
    before = file_digest(db_path)

    with pytest.raises(MigrationRejected, match="still live"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-MIGRATE-LIVE-WRITER",
                invoker_id="maintenance:test",
                expected_schema=2,
                expected_root_state="quiesced",
                expected_high_water_event_id=13,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(db_path) == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_migration_rejects_unknown_newer_schema_before_any_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    db_path = root / "aos.db"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
    before = file_digest(db_path)
    entries_before = root_entries(root)

    with pytest.raises(MigrationRejected, match="unsupported legacy schema"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-MIGRATE-FUTURE-SCHEMA",
                invoker_id="maintenance:test",
                expected_schema=99,
                expected_root_state="quiesced",
                expected_high_water_event_id=2,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(db_path) == before
    assert root_entries(root) == entries_before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_reused_migration_operation_id_with_different_input_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    original = MigrationRequest(
        operation_id="OP-MIGRATE-BOUND-REQUEST",
        invoker_id="maintenance:first",
        expected_schema=1,
        expected_root_state="quiesced",
        expected_high_water_event_id=2,
    )
    migrate_legacy_root(root, original, clock=lambda: FIXED_TIME)
    completed_digest = file_digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="different migration request"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id=original.operation_id,
                invoker_id="maintenance:second",
                expected_schema=1,
                expected_root_state="quiesced",
                expected_high_water_event_id=2,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(root / "aos.db") == completed_digest


def test_replay_finishes_quarantine_cleanup_after_post_commit_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / "unsafe.json"
    unsafe.write_bytes(b"sk-" + b"post-commit-crash-secret-material-123456")
    request = MigrationRequest(
        operation_id="OP-MIGRATE-QUARANTINE-RECOVERY",
        invoker_id="maintenance:test",
        expected_schema=2,
        expected_root_state="quiesced",
        expected_high_water_event_id=13,
    )

    def crash_after_commit(point: str) -> None:
        if point == "after_commit":
            raise RuntimeError("injected after_commit failure")

    with pytest.raises(RuntimeError, match="injected after_commit"):
        migrate_legacy_root(
            root,
            request,
            clock=lambda: FIXED_TIME,
            fault_injector=crash_after_commit,
        )

    assert unsafe.exists()
    with pytest.raises(MigrationRejected, match="pending migration cleanup"):
        inspect_journal(root)
    retry_time = FIXED_TIME + timedelta(hours=2)
    replayed = migrate_legacy_root(root, request, clock=lambda: retry_time)
    assert replayed.status == "replayed"
    assert not unsafe.exists()
    inspection = inspect_journal(root)
    assert inspection.schema_version == CURRENT_SCHEMA_VERSION
    with sqlite3.connect(root / "aos.db") as db:
        cleanup = db.execute(
            """
            SELECT status, completed_at, completed_event_id
            FROM migration_cleanup
            WHERE operation_id=?
            """,
            (request.operation_id,),
        ).fetchone()
        cleanup_events = db.execute(
            """
            SELECT id, occurred_at
            FROM events
            WHERE kind='migration.cleanup_completed'
              AND aggregate_type='migration'
              AND aggregate_id=?
            """,
            (request.operation_id,),
        ).fetchall()
    assert cleanup is not None
    assert cleanup[0] == "completed"
    assert cleanup[1] == retry_time.isoformat(timespec="milliseconds")
    assert cleanup_events == [(cleanup[2], retry_time.isoformat(timespec="milliseconds"))]


def test_retry_replaces_partial_staged_blob_and_converges(tmp_path: Path) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    artifact = next((root / "artifacts").glob("*.jsonl"))
    artifact_bytes = artifact.read_bytes()
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    staging = root / "blobs" / ".staging"
    staging.mkdir(parents=True)
    (staging / f"{digest}.tmp").write_bytes(b"partial prior write")

    outcome = migrate_legacy_root(
        root,
        MigrationRequest(
            operation_id="OP-MIGRATE-PARTIAL-STAGE",
            invoker_id="maintenance:test",
            expected_schema=2,
            expected_root_state="quiesced",
            expected_high_water_event_id=13,
        ),
        clock=lambda: FIXED_TIME,
    )

    published = root / "blobs" / "sha256" / digest[:2] / digest
    assert outcome.status == "completed"
    assert published.read_bytes() == artifact_bytes
    assert not (staging / f"{digest}.tmp").exists()


def test_migration_rejects_live_legacy_daemon_marker_before_any_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v1", root)
    (root / "aosd.json").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": FIXED_TIME.isoformat()})
    )
    db_path = root / "aos.db"
    before = file_digest(db_path)

    with pytest.raises(MigrationRejected, match="daemon is still live"):
        migrate_legacy_root(
            root,
            MigrationRequest(
                operation_id="OP-MIGRATE-LIVE-DAEMON",
                invoker_id="maintenance:test",
                expected_schema=1,
                expected_root_state="quiesced",
                expected_high_water_event_id=2,
            ),
            clock=lambda: FIXED_TIME,
        )

    assert file_digest(db_path) == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()
