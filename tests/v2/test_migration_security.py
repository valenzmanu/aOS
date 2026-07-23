from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modules.journal import (
    CURRENT_SCHEMA_VERSION,
    JournalError,
    MigrationRejected,
    MigrationRequest,
    inspect_journal,
    migrate_legacy_root,
)

FIXTURES = Path(__file__).parents[1] / "legacy" / "fixtures"
FIXED_TIME = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _request(operation_id: str) -> MigrationRequest:
    return MigrationRequest(
        operation_id=operation_id,
        invoker_id="maintenance:security-test",
        expected_schema=2,
        expected_root_state="quiesced",
        expected_high_water_event_id=13,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _root_identity(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def _assert_not_in_canonical_state(root: Path, sentinel: bytes) -> None:
    assert sentinel not in (root / "aos.db").read_bytes()
    blobs = root / "blobs"
    if blobs.exists():
        for candidate in blobs.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                assert sentinel not in candidate.read_bytes()


def test_migration_rejects_symlinked_journal_without_changing_external_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    external = tmp_path / "external-journal"
    external.mkdir()
    external_db = external / "aos.db"
    shutil.copy2(root / "aos.db", external_db)
    (root / "aos.db").unlink()
    (root / "aos.db").symlink_to(external_db)
    before = _digest(external_db)

    with pytest.raises(JournalError, match="journal"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-SYMLINKED-JOURNAL"),
            clock=lambda: FIXED_TIME,
        )

    assert (root / "aos.db").is_symlink()
    assert _digest(external_db) == before
    assert not (external / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()
    assert not (tmp_path / ".aos-quarantine").exists()


def test_migration_rejects_symlinked_blob_root_without_external_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    external = tmp_path / "external-blobs"
    external.mkdir()
    (root / "blobs").symlink_to(external, target_is_directory=True)
    before = _digest(root / "aos.db")

    with pytest.raises(JournalError, match="symbolic link"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-SYMLINKED-BLOBS"),
            clock=lambda: FIXED_TIME,
        )

    assert list(external.iterdir()) == []
    assert _digest(root / "aos.db") == before
    assert not (tmp_path / ".aos-backups").exists()
    assert not (tmp_path / ".aos-quarantine").exists()


def test_migration_rejects_symlinked_backup_parent_without_external_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    external = tmp_path / "external-backups"
    external.mkdir()
    (tmp_path / ".aos-backups").symlink_to(external, target_is_directory=True)
    before = _digest(root / "aos.db")

    with pytest.raises(JournalError, match="symbolic link"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-SYMLINKED-BACKUPS"),
            clock=lambda: FIXED_TIME,
        )

    assert list(external.iterdir()) == []
    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-quarantine").exists()


def test_migration_rejects_symlinked_quarantine_destination_without_deleting_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / "unsafe.json"
    unsafe_bytes = b"sk-" + b"quarantine-destination-sentinel-1234567890"
    unsafe.write_bytes(unsafe_bytes)
    unsafe_digest = hashlib.sha256(unsafe_bytes).hexdigest()

    external = tmp_path / "external-quarantine-object"
    external.write_bytes(unsafe_bytes)
    quarantine_dir = tmp_path / ".aos-quarantine" / _root_identity(root)
    quarantine_dir.mkdir(parents=True)
    destination = quarantine_dir / unsafe_digest
    destination.symlink_to(external)
    before = _digest(root / "aos.db")

    with pytest.raises(
        JournalError,
        match="quarantine.*symbolic link|symbolic link.*quarantine",
    ):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-SYMLINKED-QUARANTINE"),
            clock=lambda: FIXED_TIME,
        )

    assert unsafe.read_bytes() == unsafe_bytes
    assert destination.is_symlink()
    assert external.read_bytes() == unsafe_bytes
    assert _digest(root / "aos.db") == before


def test_migration_rejects_nested_projection_secret_before_crossing_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    sentinel = "nested-json-secret-sentinel-1234567890"
    checkpoint = json.dumps(
        {"progress": "legacy", "auth": {"client_secret": sentinel}},
        sort_keys=True,
    )
    with sqlite3.connect(root / "aos.db") as db:
        db.execute(
            "UPDATE processes SET checkpoint=? WHERE id='P-E26D600C'",
            (checkpoint,),
        )
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="policy-unsafe"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-NESTED-JSON-SECRET"),
            clock=lambda: FIXED_TIME,
        )

    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()
    assert not (tmp_path / ".aos-quarantine").exists()
    with sqlite3.connect(root / "aos.db") as db:
        preserved_checkpoint = str(
            db.execute("SELECT checkpoint FROM processes WHERE id='P-E26D600C'").fetchone()[0]
        )
    assert preserved_checkpoint == checkpoint


@pytest.mark.parametrize(
    ("label", "unsafe_bytes", "sentinel"),
    [
        (
            "generic-json-key",
            b'{"password":"generic-json-artifact-sentinel-1234567890"}\n',
            b"generic-json-artifact-sentinel-1234567890",
        ),
        (
            "authorization-bearer",
            b"Authorization: Bearer authorization-artifact-sentinel-1234567890\n",
            b"authorization-artifact-sentinel-1234567890",
        ),
        (
            "camel-case-secret-key",
            b'{"claimToken":"camel-case-artifact-sentinel-1234567890"}\n',
            b"camel-case-artifact-sentinel-1234567890",
        ),
        (
            "double-encoded-secret",
            b'{"metadata":"{\\"password\\":\\"double-encoded-sentinel-1234567890\\"}"}\n',
            b"double-encoded-sentinel-1234567890",
        ),
        (
            "secret-digest-with-raw-value",
            b'{"password_hash":"raw-password-hash-sentinel-1234567890"}\n',
            b"raw-password-hash-sentinel-1234567890",
        ),
        (
            "encrypted-private-key",
            b"-----BEGIN " + b"ENCRYPTED PRIVATE KEY-----\nprivate-key-sentinel-1234567890\n",
            b"private-key-sentinel-1234567890",
        ),
        (
            "pgp-private-key",
            b"-----BEGIN " + b"PGP PRIVATE KEY BLOCK-----\npgp-key-sentinel-1234567890\n",
            b"pgp-key-sentinel-1234567890",
        ),
    ],
)
def test_migration_quarantines_generic_secret_artifacts_without_canonical_leakage(
    tmp_path: Path,
    label: str,
    unsafe_bytes: bytes,
    sentinel: bytes,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / f"{label}.txt"
    unsafe.write_bytes(unsafe_bytes)
    unsafe_digest = hashlib.sha256(unsafe_bytes).hexdigest()

    outcome = migrate_legacy_root(
        root,
        _request(f"OP-SECURITY-{label.upper()}"),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    with sqlite3.connect(root / "aos.db") as db:
        record = db.execute(
            """
            SELECT status, blob_digest, quarantine_reason
            FROM legacy_artifacts
            WHERE legacy_path=?
            """,
            (unsafe.relative_to(root).as_posix(),),
        ).fetchone()
    assert record is not None
    assert record[0] == "quarantined"
    assert record[1] is None
    assert "secret" in str(record[2])
    assert not unsafe.exists()
    assert not (root / "blobs" / "sha256" / unsafe_digest[:2] / unsafe_digest).exists()
    _assert_not_in_canonical_state(root, sentinel)

    quarantine = list((tmp_path / ".aos-quarantine").glob(f"*/{unsafe_digest}"))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == unsafe_bytes


def test_migration_rejects_unsupported_artifact_entry_before_any_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    fifo = root / "artifacts" / "legacy-pipe"
    os.mkfifo(fifo)
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="unsupported filesystem entry"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-UNSUPPORTED-ARTIFACT-ENTRY"),
            clock=lambda: FIXED_TIME,
        )

    assert fifo.exists()
    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_replay_never_follows_swapped_artifact_parent_for_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / "unsafe.json"
    unsafe_bytes = b"sk-" + b"cleanup-parent-swap-sentinel-1234567890"
    unsafe.write_bytes(unsafe_bytes)
    request = _request("OP-SECURITY-CLEANUP-PARENT-SWAP")

    def crash_after_commit(point: str) -> None:
        if point == "after_commit":
            raise RuntimeError("injected post-commit crash")

    with pytest.raises(RuntimeError, match="post-commit crash"):
        migrate_legacy_root(
            root,
            request,
            clock=lambda: FIXED_TIME,
            fault_injector=crash_after_commit,
        )

    with pytest.raises(MigrationRejected, match="pending migration cleanup"):
        inspect_journal(root)

    held_artifacts = tmp_path / "held-artifacts"
    (root / "artifacts").rename(held_artifacts)
    external = tmp_path / "external-artifacts"
    external.mkdir()
    external_unsafe = external / unsafe.name
    external_unsafe.write_bytes(unsafe_bytes)
    (root / "artifacts").symlink_to(external, target_is_directory=True)

    with pytest.raises(JournalError, match="symbolic link"):
        migrate_legacy_root(root, request, clock=lambda: FIXED_TIME)

    assert external_unsafe.read_bytes() == unsafe_bytes
    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute(
            "SELECT status FROM migration_cleanup WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone() == ("pending",)


def test_retry_replaces_partial_quarantine_object_and_converges(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    unsafe = root / "artifacts" / "unsafe.json"
    unsafe_bytes = b"sk-" + b"partial-quarantine-sentinel-1234567890"
    unsafe.write_bytes(unsafe_bytes)
    digest = hashlib.sha256(unsafe_bytes).hexdigest()
    quarantine_root = tmp_path / ".aos-quarantine"
    quarantine_root.mkdir(mode=0o700)
    quarantine_dir = quarantine_root / _root_identity(root)
    quarantine_dir.mkdir(mode=0o700)
    partial = quarantine_dir / digest
    partial.write_bytes(b"partial prior write")
    partial.chmod(0o600)

    outcome = migrate_legacy_root(
        root,
        _request("OP-SECURITY-PARTIAL-QUARANTINE"),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    assert partial.read_bytes() == unsafe_bytes
    assert not unsafe.exists()


def test_migration_rejects_secret_shaped_artifact_path_before_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    credential_name = "sk-" + "path-sentinel-credential-1234567890"
    (root / "artifacts" / f"{credential_name}.json").write_text("{}")
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="path digest"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-SECRET-ARTIFACT-PATH"),
            clock=lambda: FIXED_TIME,
        )

    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_migration_rejects_non_hash_legacy_capability_before_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    sentinel = "generic-raw-capability-sentinel-1234567890"
    with sqlite3.connect(root / "aos.db") as db:
        db.execute("UPDATE processes SET token_hash=?", (sentinel,))
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="invalid SHA-256"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-NON-HASH-CAPABILITY"),
            clock=lambda: FIXED_TIME,
        )

    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


@pytest.mark.parametrize(
    "location",
    ["metadata-nested-json", "metadata-blob", "event-aggregate"],
)
def test_migration_rejects_secret_in_preserved_database_surfaces(
    tmp_path: Path,
    location: str,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    sentinel = "sk-" + "preserved-surface-sentinel-1234567890"
    with sqlite3.connect(root / "aos.db") as db:
        if location == "metadata-nested-json":
            nested = json.dumps({"metadata": json.dumps({"auth": {"password": sentinel}})})
            db.execute(
                "INSERT INTO metadata(key, value) VALUES ('settings', ?)",
                (nested,),
            )
        elif location == "metadata-blob":
            db.execute(
                "INSERT INTO metadata(key, value) VALUES ('notes', ?)",
                (sentinel.encode(),),
            )
        else:
            db.execute(
                "UPDATE events SET aggregate_id=? WHERE id=1",
                (sentinel,),
            )
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="policy-unsafe"):
        migrate_legacy_root(
            root,
            _request(f"OP-SECURITY-{location.upper()}"),
            clock=lambda: FIXED_TIME,
        )

    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_safe_non_sha_commit_hash_artifact_remains_publishable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    artifact = root / "artifacts" / "commit.json"
    artifact.write_text('{"commit_hash":"e920ff2"}\n')

    migrate_legacy_root(
        root,
        _request("OP-SECURITY-SAFE-COMMIT-HASH"),
        clock=lambda: FIXED_TIME,
    )

    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute(
            """
            SELECT status
            FROM legacy_artifacts
            WHERE legacy_path='artifacts/commit.json'
            """
        ).fetchone() == ("available",)


@pytest.mark.parametrize("sidecar_suffix", ["-shm", "-journal"])
def test_migration_rejects_unsafe_sqlite_sidecar_without_external_write(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    external = tmp_path / "external-sidecar"
    external.write_bytes(b"external-sidecar-sentinel")
    sidecar = Path(f"{root / 'aos.db'}{sidecar_suffix}")
    sidecar.unlink(missing_ok=True)
    if sidecar_suffix == "-shm":
        sidecar.symlink_to(external)
    else:
        sidecar.write_bytes(b"unrecovered rollback journal")
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected, match="sidecar"):
        migrate_legacy_root(
            root,
            _request(f"OP-SECURITY-SIDECAR-{sidecar_suffix[1:].upper()}"),
            clock=lambda: FIXED_TIME,
        )

    assert external.read_bytes() == b"external-sidecar-sentinel"
    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()


def test_future_schema_in_wal_is_rejected_without_touching_source_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    db_path = root / "aos.db"
    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
        keeper.commit()
        source_paths = [
            path
            for path in (
                db_path,
                Path(f"{db_path}-wal"),
                Path(f"{db_path}-shm"),
            )
            if path.exists()
        ]
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths}

        with pytest.raises(MigrationRejected, match="unsupported legacy schema"):
            migrate_legacy_root(
                root,
                replace(
                    _request("OP-SECURITY-FUTURE-SCHEMA-WAL"),
                    expected_schema=99,
                ),
                clock=lambda: FIXED_TIME,
            )

        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths}
        assert after == before
        assert not (root / "blobs").exists()
        assert not (tmp_path / ".aos-backups").exists()
    finally:
        keeper.close()


def test_migration_rejects_hardlinked_database_before_any_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    external_link = tmp_path / "external-aos.db"
    os.link(root / "aos.db", external_link)
    before = _digest(external_link)

    with pytest.raises(MigrationRejected, match="hard-linked"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-HARDLINKED-DATABASE"),
            clock=lambda: FIXED_TIME,
        )

    assert _digest(external_link) == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


@pytest.mark.parametrize(
    "migration_request",
    [
        replace(_request("OP-SECURITY-ROOT-STATE"), expected_root_state="active"),
        replace(_request("OP-SECURITY-WHITESPACE"), operation_id=" OP-SECURITY-WHITESPACE "),
        replace(_request("OP-SECURITY-FLOAT-SCHEMA"), expected_schema=2.0),
        replace(
            _request("OP-SECURITY-FLOAT-HIGH-WATER"),
            expected_high_water_event_id=13.0,
        ),
        replace(_request("OP-SECURITY-BOOL-SCHEMA"), expected_schema=True),
    ],
)
def test_migration_rejects_noncanonical_admission_request_before_effects(
    tmp_path: Path,
    migration_request: MigrationRequest,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    before = _digest(root / "aos.db")

    with pytest.raises(MigrationRejected):
        migrate_legacy_root(root, migration_request, clock=lambda: FIXED_TIME)

    assert _digest(root / "aos.db") == before
    assert not (root / "blobs").exists()
    assert not (tmp_path / ".aos-backups").exists()


def test_final_admission_rescans_source_artifacts_after_fault_hook(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    artifact = next((root / "artifacts").glob("*.jsonl"))
    replacement = b"sk-" + b"late-source-replacement-sentinel-1234567890"

    def replace_source(point: str) -> None:
        if point == "before_commit":
            artifact.write_bytes(replacement)

    with pytest.raises(MigrationRejected, match="immediately before migration commit"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-LATE-SOURCE-CHANGE"),
            clock=lambda: FIXED_TIME,
            fault_injector=replace_source,
        )

    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "2",
        )
        assert db.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type='table' AND name='migration_snapshots'
            """
        ).fetchone() == (0,)
    assert artifact.read_bytes() == replacement


def test_final_admission_rejects_source_database_path_swap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    db_path = root / "aos.db"
    held_path = root / "held.db"

    def swap_source_database(point: str) -> None:
        if point != "before_commit":
            return
        db_path.replace(held_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                sidecar.replace(Path(f"{held_path}{suffix}"))
        shutil.copy2(FIXTURES / "schema-v2" / "aos.db", db_path)

    with pytest.raises(MigrationRejected, match="database pathname changed"):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-DATABASE-PATH-SWAP"),
            clock=lambda: FIXED_TIME,
            fault_injector=swap_source_database,
        )

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "2",
        )
        assert db.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type='table' AND name='migration_snapshots'
            """
        ).fetchone() == (0,)


def test_inspection_is_portable_but_fails_when_canonical_cas_is_incomplete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    outcome = migrate_legacy_root(
        root,
        _request("OP-SECURITY-PORTABLE-INSPECTION"),
        clock=lambda: FIXED_TIME,
    )
    moved_root = tmp_path / "moved-state"
    shutil.copytree(root, moved_root)

    assert inspect_journal(moved_root).schema_version == CURRENT_SCHEMA_VERSION

    snapshot = (
        moved_root / "blobs" / "sha256" / outcome.snapshot_digest[:2] / outcome.snapshot_digest
    )
    snapshot.unlink()
    with pytest.raises(JournalError, match="replay-critical blob"):
        inspect_journal(moved_root)


@pytest.mark.parametrize(
    "effect_target",
    ["available-artifact", "backup", "quarantine"],
)
def test_late_effect_loss_rolls_back_without_dangling_reference(
    tmp_path: Path,
    effect_target: str,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    if effect_target == "quarantine":
        unsafe = root / "artifacts" / "unsafe.json"
        unsafe.write_bytes(b"sk-" + b"late-quarantine-loss-sentinel-1234567890")

    def delete_effect(point: str) -> None:
        if point != "before_commit":
            return
        if effect_target == "available-artifact":
            artifact = next((root / "artifacts").glob("*.jsonl"))
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            target = root / "blobs" / "sha256" / digest[:2] / digest
        elif effect_target == "backup":
            target = next((tmp_path / ".aos-backups").glob("*/*.sqlite3"))
        else:
            target = next((tmp_path / ".aos-quarantine").glob("*/*"))
        target.unlink()

    with pytest.raises(JournalError, match="missing|digest|backup|quarantine|blob"):
        migrate_legacy_root(
            root,
            _request(f"OP-SECURITY-LATE-{effect_target.upper()}"),
            clock=lambda: FIXED_TIME,
            fault_injector=delete_effect,
        )

    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "2",
        )
        assert db.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type='table' AND name='migration_snapshots'
            """
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "fault_point",
    ["after_effect_promotion", "before_commit"],
)
def test_missing_promoted_snapshot_rolls_back_without_dangling_reference(
    tmp_path: Path,
    fault_point: str,
) -> None:
    root = tmp_path / "state"
    shutil.copytree(FIXTURES / "schema-v2", root)
    deleted: list[Path] = []

    def delete_promoted_snapshot(point: str) -> None:
        if point != fault_point or deleted:
            return
        for candidate in sorted((root / "blobs" / "sha256").glob("*/*")):
            if (
                candidate.is_file()
                and b'"format":"aos.migration-snapshot.v2"' in candidate.read_bytes()
            ):
                candidate.unlink()
                deleted.append(candidate)
                return
        raise AssertionError("migration snapshot was not promoted before fault callback")

    with pytest.raises(
        JournalError,
        match="blob|snapshot|missing|corrupt",
    ):
        migrate_legacy_root(
            root,
            _request("OP-SECURITY-MISSING-PROMOTED-SNAPSHOT"),
            clock=lambda: FIXED_TIME,
            fault_injector=delete_promoted_snapshot,
        )

    assert len(deleted) == 1
    assert not deleted[0].exists()
    with sqlite3.connect(root / "aos.db") as db:
        schema = int(
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        )
        event_count = int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        v2_tables = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name IN (
                  'migration_snapshots',
                  'migration_backups',
                  'aggregate_revisions',
                  'legacy_artifacts'
              )
            """
        ).fetchall()
    assert schema == 2
    assert event_count == 13
    assert v2_tables == []
