"""Build a content-addressed Harbor task from an explicit file allowlist."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROTECTED_COMPONENTS = {
    ".git",
    ".runs",
    ".ssh",
    ".aws",
    "jobs",
    "missions",
    "projects",
    "state",
}


@dataclass(frozen=True)
class Materialization:
    """Paths and digest for one materialized Harbor task."""

    digest: str
    context_dir: Path
    task_dir: Path
    manifest_path: Path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_mode(path: Path) -> str:
    return f"{stat.S_IMODE(path.stat().st_mode):04o}"


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _validate_specification(specification: dict[str, Any]) -> None:
    expected_keys = {"schema_version", "task_files", "source_files"}
    if set(specification) != expected_keys:
        raise ValueError(
            "materialize.json must contain exactly schema_version, task_files, and source_files"
        )
    if specification["schema_version"] != 1:
        raise ValueError("unsupported materialize.json schema_version")
    for field in ("task_files", "source_files"):
        values = specification[field]
        if not isinstance(values, list):
            raise ValueError(f"materialize.json {field} must be an array")
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"materialize.json {field} entries must be strings")
        if len(values) != len(set(values)):
            raise ValueError(f"materialize.json {field} contains duplicate entries")


def _relative_file(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} entries must be non-empty relative file paths")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} entries must be relative file paths without traversal")
    lowered_parts = {part.casefold() for part in path.parts}
    filename = path.name.casefold()
    if lowered_parts & _PROTECTED_COMPONENTS or filename == ".env" or filename.startswith(".env."):
        raise ValueError(f"{field} entry names a protected path: {value}")
    return path


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"allowlist entry must name a regular, non-symlink file: {path}")


def _validate_existing_context(
    context_dir: Path,
    manifest: dict[str, object],
    digest: str,
) -> None:
    expected_files = {
        Path(entry["destination"]): (entry["sha256"], entry["mode"])
        for entry in manifest["files"]  # type: ignore[union-attr]
    }
    expected_manifest = _canonical_json({**manifest, "digest": digest})
    manifest_path = context_dir / "materialization.json"
    if not manifest_path.is_file() or manifest_path.read_bytes() != expected_manifest:
        raise ValueError("existing materialization manifest does not match its digest")

    observed_files: set[Path] = set()
    for path in context_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError("existing materialization contains a symlink")
        if path.is_file():
            observed_files.add(path.relative_to(context_dir))
    if observed_files != {*expected_files, Path("materialization.json")}:
        raise ValueError("existing materialization contains unexpected or missing files")
    for relative, (expected_digest, expected_mode) in expected_files.items():
        if _file_digest(context_dir / relative) != expected_digest:
            raise ValueError("existing materialization content does not match its manifest")
        if _file_mode(context_dir / relative) != expected_mode:
            raise ValueError("existing materialization mode does not match its manifest")


def materialize_task(
    repository: Path,
    task: Path,
    output_root: Path,
) -> Materialization:
    """Copy only task-declared files into a digest-addressed build context."""

    repository = repository.resolve()
    task = task.resolve()
    specification_path = task / "materialize.json"
    specification = _read_json(specification_path)
    _validate_specification(specification)
    task_files = specification["task_files"]
    source_files = specification["source_files"]

    entries: list[dict[str, str]] = []
    for value in task_files:
        relative = _relative_file(value, field="task_files")
        source = task / relative
        _require_regular_file(source)
        entries.append(
            {
                "destination": f"task/{relative.as_posix()}",
                "mode": _file_mode(source),
                "sha256": _file_digest(source),
                "source": f"task:{relative.as_posix()}",
            }
        )
    for value in source_files:
        relative = _relative_file(value, field="source_files")
        source = repository / relative
        _require_regular_file(source)
        entries.append(
            {
                "destination": (f"task/environment/aos-source/{relative.as_posix()}"),
                "mode": _file_mode(source),
                "sha256": _file_digest(source),
                "source": f"repository:{relative.as_posix()}",
            }
        )

    manifest = {
        "schema_version": 1,
        "specification_sha256": _file_digest(specification_path),
        "files": sorted(entries, key=lambda entry: entry["destination"]),
    }
    digest = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    context_dir = output_root.resolve() / digest
    materialized_task = context_dir / "task"

    if not context_dir.exists():
        context_dir.mkdir(parents=True)
        for entry in manifest["files"]:
            source_kind, relative = entry["source"].split(":", 1)
            source = (task if source_kind == "task" else repository) / relative
            destination = context_dir / entry["destination"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(int(entry["mode"], 8))
        manifest_path = context_dir / "materialization.json"
        manifest_path.write_bytes(_canonical_json({**manifest, "digest": digest}))
    else:
        manifest_path = context_dir / "materialization.json"
        _validate_existing_context(context_dir, manifest, digest)

    return Materialization(
        digest=digest,
        context_dir=context_dir,
        task_dir=materialized_task,
        manifest_path=manifest_path,
    )
