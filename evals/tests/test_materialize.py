from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.materialize import materialize_task


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_materialize_task_copies_only_declared_files_into_digest_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(repository / "modules" / "journal.py", "SCHEMA = 4\n")
    _write(repository / ".env", "SECRET=do-not-copy\n")
    _write(task / "instruction.md", "Produce the fixture.\n")
    _write(task / "task.toml", 'schema_version = "1.3"\n')
    _write(task / "environment" / "Dockerfile", "FROM python:3.12-slim\n")
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 1,
                "task_files": [
                    "instruction.md",
                    "task.toml",
                    "environment/Dockerfile",
                ],
                "source_files": ["modules/journal.py"],
            }
        ),
    )

    result = materialize_task(repository, task, tmp_path / "out")

    assert result.context_dir.name == result.digest
    assert (result.task_dir / "instruction.md").read_text() == "Produce the fixture.\n"
    assert (
        result.task_dir / "environment" / "aos-source" / "modules" / "journal.py"
    ).read_text() == "SCHEMA = 4\n"
    assert not (result.task_dir / ".env").exists()


def test_materialize_task_rejects_path_traversal(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(repository / "outside.txt", "private\n")
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 1,
                "task_files": ["../../../outside.txt"],
                "source_files": [],
            }
        ),
    )

    with pytest.raises(ValueError, match="relative file path"):
        materialize_task(repository, task, tmp_path / "out")


@pytest.mark.parametrize(
    "protected_path",
    [
        ".git/config",
        ".env",
        "state/aos.db",
        "projects/private.md",
        "missions/M-0001/contract.md",
        "evals/.runs/old/result.json",
        "jobs/old/result.json",
    ],
)
def test_materialize_task_rejects_protected_repository_paths(
    tmp_path: Path,
    protected_path: str,
) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(repository / protected_path, "must remain local\n")
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 1,
                "task_files": [],
                "source_files": [protected_path],
            }
        ),
    )

    with pytest.raises(ValueError, match="protected path"):
        materialize_task(repository, task, tmp_path / "out")


def test_materialize_task_rejects_symlinked_allowlist_entries(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(repository / "private.txt", "secret\n")
    task.mkdir(parents=True)
    (task / "instruction.md").symlink_to(repository / "private.txt")
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 1,
                "task_files": ["instruction.md"],
                "source_files": [],
            }
        ),
    )

    with pytest.raises(ValueError, match="regular, non-symlink file"):
        materialize_task(repository, task, tmp_path / "out")


def test_materialize_task_rejects_tampered_existing_context(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(task / "instruction.md", "Produce the fixture.\n")
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 1,
                "task_files": ["instruction.md"],
                "source_files": [],
            }
        ),
    )
    first = materialize_task(repository, task, tmp_path / "out")
    (first.task_dir / "instruction.md").write_text("tampered\n")

    with pytest.raises(ValueError, match="existing materialization"):
        materialize_task(repository, task, tmp_path / "out")


def test_materialize_task_rejects_unknown_allowlist_schema(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    task = repository / "evals" / "tasks" / "hbr-01"
    _write(
        task / "materialize.json",
        json.dumps(
            {
                "schema_version": 99,
                "task_files": [],
                "source_files": [],
            }
        ),
    )

    with pytest.raises(ValueError, match="schema_version"):
        materialize_task(repository, task, tmp_path / "out")
