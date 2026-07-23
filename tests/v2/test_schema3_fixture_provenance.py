from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "schema-v3"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
DOCUMENTATION_FILES = {"README.md", "manifest.json"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text())


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


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
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def test_frozen_schema3_fixture_payload_matches_manifest() -> None:
    manifest = _manifest()
    assert manifest["manifest_version"] == 1
    assert manifest["fixture"] == "schema-v3"
    assert manifest["source"] == {
        "implementation_commit": "1582aa1fc5d070b63ffcad0b6e6689f04f1464bb",
        "legacy_artifact_sha256": (
            "08786b0c38fdfffb40f065d884661cb061042c40d8fc5a7a5c003992626709c0"
        ),
        "legacy_database_sha256": (
            "001ea8d9b3e1ca337f6c5a6b4eddd5aa481ed6f780950c8f695648de59664a61"
        ),
        "legacy_fixture": "tests/legacy/fixtures/schema-v2",
        "legacy_fixture_commit": "e920ff23654c16204c7f7547d4ca7952d52c29bd",
    }

    files = manifest["files"]
    assert isinstance(files, list)
    expected_paths = {str(entry["path"]) for entry in files}
    payload_paths = {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path.name not in DOCUMENTATION_FILES
    }
    assert payload_paths == expected_paths

    for entry in files:
        path = FIXTURE_ROOT / str(entry["path"])
        assert path.stat().st_size == entry["size"]
        assert _sha256(path) == entry["sha256"]


def test_frozen_schema3_database_matches_recorded_migration() -> None:
    manifest = _manifest()
    database = manifest["database"]
    migration = manifest["migration"]

    with _connect_read_only(FIXTURE_ROOT / str(database["path"])) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _structural_fingerprint(db) == database["structural_fingerprint_sha256"]
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            str(database["schema_version"]),
        )
        assert db.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone() == (
            database["event_count"],
            database["event_high_water"],
        )

        boundary_id, occurred_at, payload_json = db.execute(
            """
            SELECT id, occurred_at, payload
            FROM events
            WHERE kind='migration.boundary'
            """
        ).fetchone()
        payload = json.loads(payload_json)
        assert boundary_id == migration["boundary_event_id"]
        assert occurred_at == migration["occurred_at"]
        assert payload == {
            "backup_digest": migration["backup_digest"],
            "invoker": {
                "id": migration["invoker_id"],
                "kind": "maintenance_operator",
            },
            "legacy_high_water_event_id": migration["legacy_high_water_event_id"],
            "operation_id": migration["operation_id"],
            "request_hash": migration["request_hash"],
            "snapshot_digest": migration["snapshot_digest"],
            "source_schema": migration["source_schema"],
            "target_schema": migration["target_schema"],
        }
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN ('gate.admitted', 'gate.rejected')"
        ).fetchone() == (0,)

        assert db.execute(
            """
            SELECT digest, source_schema, legacy_high_water_event_id,
                   snapshot_digest, boundary_event_id, created_at
            FROM migration_backups
            """
        ).fetchone() == (
            migration["backup_digest"],
            migration["source_schema"],
            migration["legacy_high_water_event_id"],
            migration["snapshot_digest"],
            migration["boundary_event_id"],
            migration["occurred_at"],
        )
        assert db.execute(
            """
            SELECT digest, format, source_schema, legacy_high_water_event_id,
                   boundary_event_id, created_at
            FROM migration_snapshots
            """
        ).fetchone() == (
            migration["snapshot_digest"],
            "aos.migration-snapshot.v2",
            migration["source_schema"],
            migration["legacy_high_water_event_id"],
            migration["boundary_event_id"],
            migration["occurred_at"],
        )


def test_frozen_schema3_backup_and_cas_objects_are_self_consistent() -> None:
    manifest = _manifest()
    backup = manifest["rollback_backup"]
    migration = manifest["migration"]

    with _connect_read_only(FIXTURE_ROOT / str(backup["path"])) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _structural_fingerprint(db) == backup["structural_fingerprint_sha256"]
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            str(backup["schema_version"]),
        )
        assert db.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone() == (
            backup["event_count"],
            backup["event_high_water"],
        )

    artifact = FIXTURE_ROOT / "artifacts/P-E26D600C.jsonl"
    artifact_digest = _sha256(artifact)
    artifact_blob = FIXTURE_ROOT / "blobs/sha256" / artifact_digest[:2] / artifact_digest
    assert artifact_blob.read_bytes() == artifact.read_bytes()

    snapshot_digest = str(migration["snapshot_digest"])
    snapshot_blob = FIXTURE_ROOT / "blobs/sha256" / snapshot_digest[:2] / snapshot_digest
    snapshot = json.loads(snapshot_blob.read_text())
    assert _sha256(snapshot_blob) == snapshot_digest
    assert snapshot["format"] == "aos.migration-snapshot.v2"
    assert snapshot["source_schema"] == migration["source_schema"]
    assert snapshot["legacy_high_water_event_id"] == migration["legacy_high_water_event_id"]

    with _connect_read_only(FIXTURE_ROOT / "aos.db") as db:
        artifact_rows = db.execute(
            """
            SELECT legacy_path, digest, size, status, blob_digest, quarantine_reason
            FROM legacy_artifacts
            ORDER BY legacy_path
            """
        ).fetchall()
    assert artifact_rows == [
        (
            "/synthetic/aos-fixture/state/artifacts/P-E26D600C.jsonl",
            artifact_digest,
            artifact.stat().st_size,
            "available",
            artifact_digest,
            None,
        ),
        (
            "artifacts/P-E26D600C.jsonl",
            artifact_digest,
            artifact.stat().st_size,
            "available",
            artifact_digest,
            None,
        ),
    ]
