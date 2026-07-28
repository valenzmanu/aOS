"""Resolve the installation-wide private roots used by aOS."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


class RuntimePathError(ValueError):
    """The current process cannot identify one safe aOS runtime."""


@dataclass(frozen=True)
class RuntimePaths:
    installation_root: Path | None
    state_root: Path
    missions_root: Path
    projects_root: Path | None
    source: str


def _git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name in ("PATH", "SYSTEMROOT", "TMPDIR", "TMP", "TEMP")
        if (value := os.environ.get(name))
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    return environment


def _git_path(cwd: Path, argument: str) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", argument],
            check=True,
            capture_output=True,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimePathError(f"{cwd} is not inside an aOS Git installation") from exc
    path = Path(result.stdout.strip())
    return (path if path.is_absolute() else cwd / path).resolve()


def _is_aos_installation(path: Path) -> bool:
    agents = path / "AGENTS.md"
    project = path / "pyproject.toml"
    if not agents.is_file() or not project.is_file():
        return False
    try:
        metadata = tomllib.loads(project.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project_metadata = metadata.get("project")
    return (
        isinstance(project_metadata, dict)
        and project_metadata.get("name") == "aos-agent-os"
    )


def _contains_material(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    return next(path.iterdir(), None) is not None


def resolve_runtime_paths(
    state_root: str | Path | None = None,
    missions_root: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> RuntimePaths:
    """Resolve an explicit root pair or discover roots shared by the Git worktree."""

    selected_state = state_root or os.environ.get("AOS_STATE_ROOT") or None
    selected_missions = missions_root or os.environ.get("AOS_MISSIONS_ROOT") or None
    if (selected_state is None) != (selected_missions is None):
        raise RuntimePathError("state and missions roots must be provided together")
    if selected_state is not None and selected_missions is not None:
        return RuntimePaths(
            installation_root=None,
            state_root=Path(selected_state).expanduser().resolve(),
            missions_root=Path(selected_missions).expanduser().resolve(),
            projects_root=None,
            source="explicit",
        )

    current = (cwd or Path.cwd()).expanduser().resolve()
    worktree_root = _git_path(current, "--show-toplevel")
    common_git_dir = _git_path(current, "--git-common-dir")
    installation = common_git_dir.parent
    if not _is_aos_installation(installation):
        raise RuntimePathError(
            f"Git common directory {common_git_dir} does not identify an aOS installation"
        )
    if worktree_root != installation:
        try:
            shadows = [
                worktree_root / name
                for name in ("state", "missions", "projects")
                if _contains_material(worktree_root / name)
            ]
        except OSError as exc:
            raise RuntimePathError(
                f"cannot inspect runtime roots in linked worktree {worktree_root}"
            ) from exc
        if shadows:
            rendered = ", ".join(str(path) for path in shadows)
            raise RuntimePathError(
                f"linked worktree contains runtime material at {rendered}; "
                f"reconcile it with the shared installation at {installation} before retrying"
            )
    return RuntimePaths(
        installation_root=installation,
        state_root=installation / "state",
        missions_root=installation / "missions",
        projects_root=installation / "projects",
        source="git",
    )
