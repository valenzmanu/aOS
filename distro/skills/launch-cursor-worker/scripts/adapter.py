"""Reliable Cursor Agent CLI discovery and launch mechanics for aOS agents."""

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
    "CURSOR_API_ENDPOINT",
    "CURSOR_API_KEY",
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
MODEL_EFFORT_SUFFIXES = (
    ("-extra-high", "xhigh"),
    ("-xhigh", "xhigh"),
    ("-medium", "medium"),
    ("-high", "high"),
    ("-low", "low"),
    ("-max", "max"),
    ("-none", "none"),
)


def _model_ids(output: str) -> list[str]:
    return [
        line.split(" - ", 1)[0].strip()
        for line in output.splitlines()
        if " - " in line and line.split(" - ", 1)[0].strip()
    ]


def _encoded_effort(model: str) -> str | None:
    for suffix, effort in MODEL_EFFORT_SUFFIXES:
        if model.endswith(suffix) or f"{suffix}-fast" in model:
            return effort
    return None


def discover() -> dict[str, object]:
    executable = shutil.which("cursor-agent")
    if executable is None:
        return {
            "authenticated": False,
            "available": False,
            "catalog_source": "cursor_agent_cli",
            "harness": "cursor",
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
        [executable, "status"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    catalog = subprocess.run(
        [executable, "--list-models"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return {
        "authenticated": authentication.returncode == 0,
        "available": version.returncode == 0,
        "catalog_source": "cursor_agent_cli",
        "harness": "cursor",
        "models": _model_ids(catalog.stdout) if catalog.returncode == 0 else [],
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
        handle = record.get("session_id") or record.get("chat_id")
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
    executable = shutil.which("cursor-agent")
    if executable is None:
        raise ValueError("Cursor Agent CLI is not installed")
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
    capability = discover()
    if not capability["available"] or not capability["authenticated"]:
        raise ValueError("Cursor Agent CLI route is unavailable or unauthenticated")
    models = capability["models"]
    if not isinstance(models, list) or model not in models:
        raise ValueError("selected model is absent from the live Cursor catalog")
    encoded_effort = _encoded_effort(model)
    if encoded_effort is not None and encoded_effort != reasoning_effort:
        raise ValueError("selected Cursor model variant conflicts with reasoning effort")
    command = [
        executable,
        "--print",
        "--force",
        "--output-format",
        "stream-json",
        "--sandbox",
        "enabled",
        "--trust",
        "--model",
        model,
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
        "harness": "cursor",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "schema_version": 1,
        "session_handle": session,
        "usage": usage,
        "version": capability["version"],
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
            print(json.dumps({"error": str(exc), "harness": "cursor"}, sort_keys=True))
            return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
