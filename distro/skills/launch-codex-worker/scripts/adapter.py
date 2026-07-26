"""Reliable Codex CLI discovery and launch mechanics for aOS agents."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

SAFE_ENVIRONMENT = {
    "AOS_MISSIONS_ROOT",
    "AOS_STATE_ROOT",
    "AOS_WORKER_TOKEN",
    "CODEX_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "UV_CACHE_DIR",
}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}


def discover() -> dict[str, object]:
    executable = shutil.which("codex")
    if executable is None:
        return {
            "authenticated": False,
            "available": False,
            "catalog_source": "not_exposed_by_codex_cli",
            "harness": "codex",
            "models": [],
            "schema_version": 1,
            "version": None,
        }
    version = subprocess.run(
        [executable, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    authentication = subprocess.run(
        [executable, "login", "status"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    return {
        "authenticated": authentication.returncode == 0,
        "available": version.returncode == 0,
        "catalog_source": "not_exposed_by_codex_cli",
        "harness": "codex",
        "models": [],
        "schema_version": 1,
        "version": version.stdout.strip() if version.returncode == 0 else None,
    }


def _regular_bytes(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path.read_bytes()


def _events(output: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _session_and_usage(
    records: list[dict[str, object]],
) -> tuple[str | None, dict[str, int | float]]:
    session: str | None = None
    usage: dict[str, int | float] = {}
    for record in records:
        handle = record.get("thread_id") or record.get("session_id")
        if isinstance(handle, str) and handle:
            session = handle
        observed_usage = record.get("usage")
        if isinstance(observed_usage, dict):
            usage.update(
                {
                    str(key): value
                    for key, value in observed_usage.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            )
    return session, usage


def launch(
    *,
    model: str,
    reasoning_effort: str,
    billing_channel: str,
    workspace: Path,
    work_order_path: Path,
    admitted_route_path: Path,
    output_dir: Path,
) -> int:
    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("codex CLI is not installed")
    if not model or model != model.strip():
        raise ValueError("model is required")
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("reasoning effort is unsupported")
    if not billing_channel or billing_channel != billing_channel.strip():
        raise ValueError("billing channel is required")
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    work_order = _regular_bytes(work_order_path, label="work order")
    _regular_bytes(admitted_route_path, label="admitted route")
    if not work_order.strip():
        raise ValueError("work order is empty")
    if output_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(
        [executable, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    if version.returncode != 0:
        raise ValueError("could not read codex CLI version")
    final_path = output_dir / "final.txt"
    command = [
        executable,
        "exec",
        "--model",
        model,
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--json",
        "--output-last-message",
        str(final_path),
        "-",
    ]
    environment = {
        name: value for name, value in os.environ.items() if name in SAFE_ENVIRONMENT
    }
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        input=work_order,
        capture_output=True,
        check=False,
    )
    (output_dir / "trajectory.jsonl").write_bytes(bytes(completed.stdout or b""))
    (output_dir / "stderr.log").write_bytes(bytes(completed.stderr or b""))
    session, usage = _session_and_usage(_events(bytes(completed.stdout or b"")))
    observation = {
        "billing_channel": billing_channel,
        "billing_channel_source": "admitted_route_argument",
        "exit_status": completed.returncode,
        "harness": "codex",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "schema_version": 1,
        "session_handle": session,
        "usage": usage,
        "version": version.stdout.strip(),
    }
    (output_dir / "observation.json").write_text(
        json.dumps(observation, indent=2, sort_keys=True) + "\n"
    )
    return int(completed.returncode)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover")
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--model", required=True)
    launch_parser.add_argument("--reasoning-effort", required=True)
    launch_parser.add_argument("--billing-channel", required=True)
    launch_parser.add_argument("--workspace", type=Path, required=True)
    launch_parser.add_argument("--work-order", type=Path, required=True)
    launch_parser.add_argument("--admitted-route", type=Path, required=True)
    launch_parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "discover":
        print(json.dumps(discover(), sort_keys=True))
        return 0
    if arguments.command == "launch":
        try:
            return launch(
                model=arguments.model,
                reasoning_effort=arguments.reasoning_effort,
                billing_channel=arguments.billing_channel,
                workspace=arguments.workspace,
                work_order_path=arguments.work_order,
                admitted_route_path=arguments.admitted_route,
                output_dir=arguments.output_dir,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print(json.dumps({"error": str(exc), "harness": "codex"}, sort_keys=True))
            return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
