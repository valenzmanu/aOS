from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = FIXTURES_DIR.parents[2]
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"
GENERATOR_PATH = Path(__file__).resolve()
FIXTURE_CLOCK_START = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
SYNTHETIC_PROJECT = "/synthetic/aos-fixture/project"
SYNTHETIC_WORKTREE = "/synthetic/aos-fixture/projects/attempt-1"
ALLOWED_FIXTURE_NAMES = frozenset({"schema-v1", "schema-v2"})


@dataclass(frozen=True)
class LegacySource:
    name: str
    revision: str
    expected_schema: int


LEGACY_SOURCES = (
    LegacySource("schema-v1", "7187645", 1),
    LegacySource("schema-v2", "e920ff2", 2),
)


class DeterministicClock:
    def __init__(self) -> None:
        self._tick = 0

    def _next(self, offset_seconds: int = 0) -> str:
        value = FIXTURE_CLOCK_START + timedelta(seconds=self._tick + offset_seconds)
        self._tick += 1
        return value.isoformat(timespec="milliseconds")

    def utcnow(self) -> str:
        return self._next()

    def future(self, seconds: int) -> str:
        return self._next(seconds)


class DeterministicSecrets:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._counter = 0
        self.issued_tokens: list[str] = []

    def _material(self, purpose: str) -> bytes:
        self._counter += 1
        return f"{self._namespace}:{purpose}:{self._counter}".encode()

    def token_hex(self, nbytes: int | None = None) -> str:
        size = nbytes if nbytes is not None else 32
        return hashlib.sha256(self._material("hex")).hexdigest()[: size * 2]

    def token_urlsafe(self, nbytes: int | None = None) -> str:
        size = nbytes if nbytes is not None else 32
        digest = hashlib.sha256(self._material("urlsafe")).hexdigest()
        token = f"synthetic-{self._namespace}-{size}-{digest[:24]}"
        self.issued_tokens.append(token)
        return token

    @staticmethod
    def compare_digest(left: str, right: str) -> bool:
        return hmac.compare_digest(left, right)


def run_git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def run_git_text(*args: str) -> str:
    return run_git_bytes(*args).decode().strip()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def extract_store_source(source: LegacySource) -> tuple[str, str, bytes]:
    commit = run_git_text("rev-parse", f"{source.revision}^{{commit}}")
    store_blob = run_git_text("rev-parse", f"{commit}:aos/store.py")
    archive_bytes = run_git_bytes("archive", "--format=tar", commit, "aos/store.py")
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        extracted = archive.extractfile("aos/store.py")
        if extracted is None:
            raise RuntimeError(f"{source.revision} archive did not contain aos/store.py")
        store_source = extracted.read()
    return commit, store_blob, store_source


def load_store_module(source: LegacySource, store_source: bytes, directory: Path) -> ModuleType:
    module_path = directory / f"{source.name}-store.py"
    module_path.write_bytes(store_source)
    module_name = f"_aos_fixture_{source.name.replace('-', '_')}_store"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load extracted Store for {source.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    actual_schema = int(module.SCHEMA_VERSION)
    if actual_schema != source.expected_schema:
        raise RuntimeError(
            f"{source.revision} declared schema {actual_schema}, expected {source.expected_schema}"
        )
    return module


def install_deterministic_runtime(
    module: ModuleType, namespace: str
) -> tuple[DeterministicClock, DeterministicSecrets]:
    clock = DeterministicClock()
    deterministic_secrets = DeterministicSecrets(namespace)
    module.utcnow = clock.utcnow
    module.future = clock.future
    module.secrets = deterministic_secrets
    return clock, deterministic_secrets


def insert_mission(module: ModuleType, store: Any, mission_id: str) -> None:
    now = module.utcnow()
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO missions
                (id, project, objective, state, allowed_backends, allowed_models,
                 allow_anthropic, verify_command, created_at, updated_at)
            VALUES (?, ?, ?, 'implementing', ?, ?, 0, ?, ?, ?)
            """,
            (
                mission_id,
                SYNTHETIC_PROJECT,
                f"Exercise deterministic migration from legacy schema {module.SCHEMA_VERSION}",
                json.dumps(["codex"]),
                json.dumps(["gpt-5.6-sol"]),
                "python -m pytest -q",
                now,
                now,
            ),
        )
        store._emit(
            db,
            "mission.created",
            "mission",
            mission_id,
            {
                "project": SYNTHETIC_PROJECT,
                "allowed_backends": ["codex"],
                "allowed_models": ["gpt-5.6-sol"],
                "allow_anthropic": False,
            },
        )


def create_artifact(root: Path, process_id: str, schema_version: int) -> tuple[Path, str]:
    relative_path = Path("artifacts") / f"{process_id}.jsonl"
    artifact_path = root / relative_path
    lines = (
        {
            "type": "thread.started",
            "thread_id": f"fixture-thread-schema-{schema_version}",
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "output_tokens": 5},
        },
    )
    artifact_path.write_text(
        "".join(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n" for line in lines)
    )
    historical_path = f"/synthetic/aos-fixture/state/{relative_path.as_posix()}"
    return relative_path, historical_path


def seed_schema_v1(store: Any) -> None:
    """Seed the minimal first migration slice without projection or artifact dependencies."""
    with store.connect() as db:
        db.executemany(
            """
            INSERT INTO events
                (id, occurred_at, kind, severity, aggregate_type, aggregate_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    1,
                    "2026-07-23T10:00:00.000+00:00",
                    "legacy.fixture.started",
                    "info",
                    "fixture",
                    "F-V1",
                    '{"ordinal": 1, "message": "preserve these exact bytes"}',
                ),
                (
                    2,
                    "2026-07-23T10:00:01.000+00:00",
                    "legacy.fixture.attention",
                    "attention",
                    "fixture",
                    "F-V1",
                    '{ "items" : [3, 2, 1], "decision" : "needed" }',
                ),
            ),
        )
        db.execute(
            "INSERT INTO inbox_cursors(consumer, event_id) VALUES (?, ?)",
            ("fixture-liaison", 1),
        )


def seed_schema_v2(module: ModuleType, store: Any, root: Path) -> None:
    mission_id = "M-FIXTURE-V2"
    insert_mission(module, store, mission_id)
    contract = store.create_contract_revision(
        mission_id,
        {
            "schema_version": 1,
            "objective": "Exercise deterministic migration from legacy schema 2",
            "project": SYNTHETIC_PROJECT,
            "allowed_backends": ["codex"],
            "allowed_models": ["gpt-5.6-sol"],
            "allow_anthropic": False,
            "verification_command": "python -m pytest -q",
        },
    )
    store.create_attempt(
        mission_id,
        1,
        SYNTHETIC_WORKTREE,
        "aos/m-fixture-v2/attempt-1",
    )
    process_id, process_token = store.create_process(
        mission_id,
        "codex",
        "gpt-5.6-sol",
        SYNTHETIC_WORKTREE,
        "deterministic fixture route",
        attempt=1,
        ttl=14_400,
    )
    store.create_work_order(
        process_id,
        {
            "schema_version": 1,
            "mission_id": mission_id,
            "process_id": process_id,
            "contract_revision": contract["revision"],
            "contract_hash": contract["content_hash"],
            "objective": "Exercise deterministic migration from legacy schema 2",
            "verification_command": "python -m pytest -q",
        },
    )
    store.create_worker_session(process_id)
    store.set_process_session(process_id, "fixture-provider-handle-v2")
    store.update_worker_session_metadata(
        process_id,
        {"usage": {"input_tokens": 12, "output_tokens": 5}},
    )
    store.update_process(
        process_id,
        process_token,
        "running",
        {
            "phase": "implementing",
            "completed": ["initialized representative durable objects"],
            "files_touched": ["fixture.txt"],
        },
    )
    store.observe_provider(
        "codex",
        "healthy",
        "fixture",
        "authoritative",
        {"models": ["gpt-5.6-sol"]},
    )
    _, historical_artifact_path = create_artifact(root, process_id, module.SCHEMA_VERSION)
    store.event(
        "process.spawned",
        "process",
        process_id,
        {
            "pid": 4242,
            "artifact": historical_artifact_path,
            "routing_reason": "deterministic fixture route",
            "worker_session_id": store.processes(mission_id)[0]["session_id"],
        },
    )
    store.event(
        "worker.progress",
        "process",
        process_id,
        {"message": "representative routine progress"},
    )
    store.event(
        "worker.blocked",
        "process",
        process_id,
        {"message": "representative human decision required"},
        "attention",
    )
    store.sync_significant("fixture-liaison")
    with store.connect() as db:
        db.execute(
            "INSERT INTO inbox_cursors(consumer, event_id) VALUES (?, ?)",
            ("fixture-observer", 2),
        )


def checkpoint_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0] != 0:
            raise RuntimeError(f"WAL checkpoint failed for {db_path}: {result}")
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    if wal_path.exists() and wal_path.stat().st_size != 0:
        raise RuntimeError(f"checkpoint left a nonempty WAL for {db_path}")
    wal_path.unlink(missing_ok=True)
    shm_path.unlink(missing_ok=True)


def schema_fingerprint(db: sqlite3.Connection) -> tuple[str, list[dict[str, str]]]:
    rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": str(row[3] or ""),
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
    return sha256_bytes(canonical_json(rows)), rows


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def inspect_journal_mode_without_touching_fixture(db_path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="aos-fixture-journal-mode-") as temporary:
        copy_path = Path(temporary) / "aos.db"
        shutil.copyfile(db_path, copy_path)
        with sqlite3.connect(copy_path) as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0])


def inspect_database(db_path: Path) -> dict[str, object]:
    with connect_readonly(db_path) as db:
        integrity_rows = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
        foreign_key_rows = [list(row) for row in db.execute("PRAGMA foreign_key_check")]
        if integrity_rows != ["ok"]:
            raise RuntimeError(f"integrity check failed for {db_path}: {integrity_rows}")
        if foreign_key_rows:
            raise RuntimeError(f"foreign key check failed for {db_path}: {foreign_key_rows}")
        fingerprint, schema_rows = schema_fingerprint(db)
        table_names = [
            str(row[0])
            for row in db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        row_counts = {
            name: int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in table_names
        }
        schema_version = int(
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        )
        event_high_water = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        inbox_cursors = {
            str(row[0]): int(row[1])
            for row in db.execute("SELECT consumer, event_id FROM inbox_cursors ORDER BY consumer")
        }
        user_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    return {
        "schema_version": schema_version,
        "structural_fingerprint_sha256": fingerprint,
        "schema_objects": [
            {"type": row["type"], "name": row["name"], "table": row["table"]} for row in schema_rows
        ],
        "table_row_counts": row_counts,
        "event_high_water": event_high_water,
        "inbox_cursors": inbox_cursors,
        "integrity_check": integrity_rows,
        "foreign_key_check": foreign_key_rows,
        "journal_mode": inspect_journal_mode_without_touching_fixture(db_path),
        "user_version": user_version,
    }


def artifact_records(root: Path) -> list[dict[str, object]]:
    artifacts_root = root / "artifacts"
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(artifacts_root.rglob("*"))
        if path.is_file()
    ]


def assert_tokens_absent(root: Path, tokens: list[str]) -> None:
    files = [path for path in root.rglob("*") if path.is_file()]
    for path in files:
        content = path.read_bytes()
        for token in tokens:
            if token.encode() in content:
                raise RuntimeError(f"synthetic raw token leaked into {path}")


def generate_fixture(
    source: LegacySource,
    staging_parent: Path,
    extracted_source_parent: Path,
) -> dict[str, object]:
    commit, store_blob, store_source = extract_store_source(source)
    module = load_store_module(source, store_source, extracted_source_parent)
    _, deterministic_secrets = install_deterministic_runtime(module, source.name)

    root = staging_parent / source.name
    store = module.Store(root)
    store.initialize()
    if source.expected_schema == 1:
        seed_schema_v1(store)
    elif source.expected_schema == 2:
        store.claim_liaison("fixture-liaison", ttl=14_400)
        seed_schema_v2(module, store, root)
    else:
        raise RuntimeError(f"unsupported legacy schema: {source.expected_schema}")

    checkpoint_database(store.db_path)
    assert_tokens_absent(root, deterministic_secrets.issued_tokens)

    inspection = inspect_database(store.db_path)
    if inspection["schema_version"] != source.expected_schema:
        raise RuntimeError(
            f"{source.name} produced schema {inspection['schema_version']}, "
            f"expected {source.expected_schema}"
        )
    artifacts = artifact_records(root)
    return {
        "name": source.name,
        "source": {
            "requested_revision": source.revision,
            "commit": commit,
            "store_blob": store_blob,
            "store_sha256": sha256_bytes(store_source),
        },
        "database": {
            "path": f"{source.name}/aos.db",
            "sha256": sha256_file(store.db_path),
            "size": store.db_path.stat().st_size,
        },
        "artifacts": artifacts,
        "sqlite": {
            "library_version": sqlite3.sqlite_version,
            **inspection,
        },
    }


def replace_fixture_tree(source: Path, target: Path) -> None:
    if target.parent != FIXTURES_DIR or target.name not in ALLOWED_FIXTURE_NAMES:
        raise RuntimeError(f"refusing to replace unexpected fixture target: {target}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def build_manifest(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "generator": {
            "path": GENERATOR_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": sha256_file(GENERATOR_PATH),
        },
        "fixture_clock_start": FIXTURE_CLOCK_START.isoformat(timespec="milliseconds"),
        "python_version": sys.version.split()[0],
        "fixtures": sorted(records, key=lambda item: str(item["name"])),
    }


def generate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=".legacy-fixtures-", dir=FIXTURES_DIR) as temporary:
        temporary_root = Path(temporary)
        staged_fixtures = temporary_root / "generated"
        extracted_sources = temporary_root / "extracted"
        staged_fixtures.mkdir()
        extracted_sources.mkdir()
        records = [
            generate_fixture(source, staged_fixtures, extracted_sources)
            for source in LEGACY_SOURCES
        ]
        manifest = build_manifest(records)
        for source in LEGACY_SOURCES:
            replace_fixture_tree(
                staged_fixtures / source.name,
                FIXTURES_DIR / source.name,
            )
        temporary_manifest = FIXTURES_DIR / ".manifest.json.tmp"
        temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(temporary_manifest, MANIFEST_PATH)
    return manifest


def verify_source_provenance(record: dict[str, object]) -> None:
    source_record = dict(record["source"])  # type: ignore[arg-type]
    source = next(item for item in LEGACY_SOURCES if item.name == record["name"])
    commit, store_blob, store_source = extract_store_source(source)
    expected = {
        "requested_revision": source.revision,
        "commit": commit,
        "store_blob": store_blob,
        "store_sha256": sha256_bytes(store_source),
    }
    if source_record != expected:
        raise RuntimeError(f"source provenance mismatch for {source.name}")


def verify() -> dict[str, object]:
    manifest = json.loads(MANIFEST_PATH.read_text())
    generator = manifest.get("generator", {})
    if generator.get("sha256") != sha256_file(GENERATOR_PATH):
        raise RuntimeError("generator digest does not match manifest")
    if generator.get("path") != GENERATOR_PATH.relative_to(REPOSITORY_ROOT).as_posix():
        raise RuntimeError("generator path does not match manifest")

    expected_names = {source.name for source in LEGACY_SOURCES}
    records = manifest.get("fixtures", [])
    if {record["name"] for record in records} != expected_names:
        raise RuntimeError("fixture names do not match configured legacy sources")

    for record in records:
        verify_source_provenance(record)
        name = str(record["name"])
        root = FIXTURES_DIR / name
        database_record = record["database"]
        db_path = FIXTURES_DIR / database_record["path"]
        if sha256_file(db_path) != database_record["sha256"]:
            raise RuntimeError(f"database digest mismatch for {name}")
        if db_path.stat().st_size != database_record["size"]:
            raise RuntimeError(f"database size mismatch for {name}")

        actual_inspection = inspect_database(db_path)
        recorded_inspection = dict(record["sqlite"])
        if actual_inspection != {
            key: value for key, value in recorded_inspection.items() if key != "library_version"
        }:
            raise RuntimeError(f"SQLite inspection mismatch for {name}")

        actual_artifacts = artifact_records(root)
        if actual_artifacts != record["artifacts"]:
            raise RuntimeError(f"artifact manifest mismatch for {name}")
        expected_files = {
            Path(database_record["path"]).relative_to(name).as_posix(),
            *(str(item["path"]) for item in record["artifacts"]),
        }
        actual_files = {
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
        }
        if actual_files != expected_files:
            raise RuntimeError(f"unmanifested fixture files for {name}: {actual_files}")
    return manifest


def summarize(manifest: dict[str, object], action: str) -> None:
    fixtures = manifest["fixtures"]
    print(f"{action} {len(fixtures)} legacy fixtures")
    for record in fixtures:
        database = record["database"]
        sqlite_record = record["sqlite"]
        print(
            f"{record['name']}: schema={sqlite_record['schema_version']} "
            f"events={sqlite_record['event_high_water']} "
            f"db_sha256={database['sha256']} "
            f"structure_sha256={sqlite_record['structural_fingerprint_sha256']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic aOS 0.1 database fixtures")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify checked-in fixtures and provenance without rewriting them",
    )
    args = parser.parse_args()
    manifest = verify() if args.check else generate()
    if not args.check:
        verify()
    summarize(manifest, "verified" if args.check else "generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
