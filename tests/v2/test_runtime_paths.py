from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from modules.mission_controls import main as mission_main
from modules.runtime_paths import resolve_runtime_paths
from modules.v0_tracer import main as v0_main


@pytest.fixture(autouse=True)
def _clear_runtime_root_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AOS_STATE_ROOT", raising=False)
    monkeypatch.delenv("AOS_MISSIONS_ROOT", raising=False)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _linked_aos_installation(tmp_path: Path) -> tuple[Path, Path]:
    installation = tmp_path / "aos"
    installation.mkdir()
    _git(installation, "init")
    _git(installation, "config", "user.email", "test@example.com")
    _git(installation, "config", "user.name", "Test")
    (installation / "AGENTS.md").write_text("# aOS\n")
    (installation / "pyproject.toml").write_text('[project]\nname = "aos-agent-os"\n')
    _git(installation, "add", "AGENTS.md", "pyproject.toml")
    _git(installation, "commit", "-m", "initial")

    linked_worktree = tmp_path / "linked"
    _git(installation, "worktree", "add", "-b", "linked", str(linked_worktree))
    return installation, linked_worktree


def test_linked_worktree_discovers_primary_runtime_roots(tmp_path: Path) -> None:
    installation, linked_worktree = _linked_aos_installation(tmp_path)
    for name in ("state", "missions", "projects"):
        (installation / name).mkdir()
        assert not (linked_worktree / name).exists()

    primary_paths = resolve_runtime_paths(cwd=installation)
    linked_paths = resolve_runtime_paths(cwd=linked_worktree)

    assert linked_paths == primary_paths
    assert linked_paths.installation_root == installation.resolve()
    assert linked_paths.state_root == (installation / "state").resolve()
    assert linked_paths.missions_root == (installation / "missions").resolve()
    assert linked_paths.projects_root == (installation / "projects").resolve()
    assert linked_paths.source == "git"


def test_explicit_root_pair_does_not_require_git(tmp_path: Path) -> None:
    state_root = tmp_path / "isolated-state"
    missions_root = tmp_path / "isolated-missions"

    paths = resolve_runtime_paths(
        state_root=state_root,
        missions_root=missions_root,
        cwd=tmp_path,
    )

    assert paths.state_root == state_root.resolve()
    assert paths.missions_root == missions_root.resolve()
    assert paths.installation_root is None
    assert paths.projects_root is None
    assert paths.source == "explicit"


@pytest.mark.parametrize(
    ("state_root", "missions_root"),
    (("state", None), (None, "missions")),
)
def test_partial_explicit_root_pair_is_rejected(
    tmp_path: Path,
    state_root: str | None,
    missions_root: str | None,
) -> None:
    with pytest.raises(ValueError, match="provided together"):
        resolve_runtime_paths(
            state_root=state_root,
            missions_root=missions_root,
            cwd=tmp_path,
        )


def test_environment_root_pair_is_an_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state-from-environment"
    missions_root = tmp_path / "missions-from-environment"
    monkeypatch.setenv("AOS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("AOS_MISSIONS_ROOT", str(missions_root))

    paths = resolve_runtime_paths(cwd=tmp_path)

    assert paths.state_root == state_root.resolve()
    assert paths.missions_root == missions_root.resolve()
    assert paths.source == "explicit"


@pytest.mark.parametrize("root_name", ("state", "missions", "projects"))
def test_linked_worktree_with_shadow_runtime_material_is_rejected(
    tmp_path: Path,
    root_name: str,
) -> None:
    installation, linked_worktree = _linked_aos_installation(tmp_path)
    shadow_root = linked_worktree / root_name
    shadow_root.mkdir()
    (shadow_root / "material").write_text("divergent\n")

    with pytest.raises(ValueError, match="runtime material"):
        resolve_runtime_paths(cwd=linked_worktree)


def test_empty_runtime_directories_in_linked_worktree_are_harmless(tmp_path: Path) -> None:
    installation, linked_worktree = _linked_aos_installation(tmp_path)
    for name in ("state", "missions", "projects"):
        (linked_worktree / name).mkdir()

    paths = resolve_runtime_paths(cwd=linked_worktree)

    assert paths.installation_root == installation.resolve()


def test_unrelated_git_repository_is_not_an_aos_installation(tmp_path: Path) -> None:
    repository = tmp_path / "unrelated"
    repository.mkdir()
    _git(repository, "init")
    (repository / "AGENTS.md").write_text("# unrelated\n")
    (repository / "pyproject.toml").write_text('[project]\nname = "unrelated"\n')

    with pytest.raises(ValueError, match="does not identify an aOS installation"):
        resolve_runtime_paths(cwd=repository)


def test_non_git_directory_without_explicit_roots_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not inside an aOS Git installation"):
        resolve_runtime_paths(cwd=tmp_path)


def test_git_failure_does_not_inherit_or_report_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-that-must-stay-private"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setenv("GIT_ASKPASS", secret)

    def deny_git(*args: object, **kwargs: object) -> None:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert secret not in environment.values()
        raise PermissionError(secret)

    monkeypatch.setattr(subprocess, "run", deny_git)

    with pytest.raises(ValueError) as captured:
        resolve_runtime_paths(cwd=tmp_path)
    assert secret not in str(captured.value)


def test_paths_command_reports_shared_roots_from_linked_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation, linked_worktree = _linked_aos_installation(tmp_path)
    monkeypatch.chdir(linked_worktree)

    exit_code = mission_main(["paths"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "installation_root": str(installation.resolve()),
        "missions_root": str((installation / "missions").resolve()),
        "projects_root": str((installation / "projects").resolve()),
        "source": "git",
        "state_root": str((installation / "state").resolve()),
    }


def test_primary_and_linked_worktree_share_one_v0_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation, linked_worktree = _linked_aos_installation(tmp_path)
    monkeypatch.chdir(installation)

    primary_exit_code = v0_main(["init", "--op", "OP-SHARED-INIT"])
    primary_result = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(linked_worktree)
    linked_exit_code = v0_main(["init", "--op", "OP-SHARED-INIT"])
    linked_result = json.loads(capsys.readouterr().out)

    assert primary_exit_code == 0
    assert primary_result["status"] == "completed"
    assert linked_exit_code == 0
    assert linked_result["status"] == "replayed"
    assert (installation / "state" / "aos.db").is_file()
    assert not (linked_worktree / "state").exists()
