from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o700)


def test_codex_adapter_discovers_installation_authentication_and_version(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    binary_dir = tmp_path / "bin"
    _write_executable(
        binary_dir / "codex",
        """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\\n' 'codex-cli 9.9.9'
  exit 0
fi
if [ "$#" -eq 2 ] && [ "$1" = "login" ] && [ "$2" = "status" ]; then
  printf '%s\\n' 'Logged in'
  exit 0
fi
exit 2
""",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository
                / "distro"
                / "skills"
                / "launch-codex-worker"
                / "scripts"
                / "adapter.py"
            ),
            "discover",
        ],
        cwd=repository,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join((str(binary_dir), "/usr/bin", "/bin")),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "authenticated": True,
        "available": True,
        "catalog_source": "not_exposed_by_codex_cli",
        "harness": "codex",
        "models": [],
        "schema_version": 1,
        "version": "codex-cli 9.9.9",
    }


def test_cursor_adapter_discovers_the_authenticated_live_model_catalog(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    binary_dir = tmp_path / "bin"
    _write_executable(
        binary_dir / "cursor-agent",
        """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\\n' '2099.01.01-test'
  exit 0
fi
if [ "$#" -eq 1 ] && [ "$1" = "status" ]; then
  printf '%s\\n' 'Logged in'
  exit 0
fi
if [ "$#" -eq 1 ] && [ "$1" = "--list-models" ]; then
  printf '%s\\n' 'Available models'
  printf '%s\\n' 'gpt-5.6-sol-high - GPT-5.6 Sol High'
  printf '%s\\n' 'composer-2.5 - Composer 2.5'
  exit 0
fi
exit 2
""",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository
                / "distro"
                / "skills"
                / "launch-cursor-worker"
                / "scripts"
                / "adapter.py"
            ),
            "discover",
        ],
        cwd=repository,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join((str(binary_dir), "/usr/bin", "/bin")),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "authenticated": True,
        "available": True,
        "catalog_source": "cursor_agent_cli",
        "harness": "cursor",
        "models": ["gpt-5.6-sol-high", "composer-2.5"],
        "schema_version": 1,
        "version": "2099.01.01-test",
    }


def test_codex_adapter_launches_an_admitted_route_without_prompt_or_token_in_arguments(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    binary_dir = tmp_path / "bin"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_executable(
        binary_dir / "codex",
        """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\\n' 'codex-cli 9.9.9'
  exit 0
fi
printf '%s\\n' "$@" > fake-args.txt
cat > fake-stdin.txt
output=
previous=
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then
    output=$argument
  fi
  previous=$argument
done
test -n "$output"
printf '%s\\n' 'worker finished' > "$output"
if [ -n "${AOS_WORKER_TOKEN:-}" ]; then
  printf '%s\\n' 'present' > fake-token-status.txt
fi
printf '%s\\n' '{"type":"thread.started","thread_id":"thread-test"}'
printf '%s\\n' '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5}}'
""",
    )
    admitted_route = tmp_path / "route.md"
    admitted_route.write_text("# Admitted route\n")
    work_order = tmp_path / "work-order.md"
    sentinel = "private-work-order-sentinel"
    work_order.write_text(f"# Work order\n\n{sentinel}\n")
    output_dir = tmp_path / "output"

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository
                / "distro"
                / "skills"
                / "launch-codex-worker"
                / "scripts"
                / "adapter.py"
            ),
            "launch",
            "--model",
            "gpt-5.6-sol",
            "--reasoning-effort",
            "high",
            "--billing-channel",
            "chatgpt-plan",
            "--workspace",
            str(workspace),
            "--work-order",
            str(work_order),
            "--admitted-route",
            str(admitted_route),
            "--output-dir",
            str(output_dir),
        ],
        cwd=repository,
        env={
            "AOS_WORKER_TOKEN": "worker-capability-sentinel",
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join((str(binary_dir), "/usr/bin", "/bin")),
            "UNRELATED_SECRET": "must-not-cross",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    child_arguments = (workspace / "fake-args.txt").read_text()
    assert sentinel not in child_arguments
    assert "worker-capability-sentinel" not in child_arguments
    assert 'model_reasoning_effort="high"' in child_arguments
    assert (workspace / "fake-stdin.txt").read_text() == work_order.read_text()
    assert (workspace / "fake-token-status.txt").read_text().strip() == "present"
    observation = json.loads((output_dir / "observation.json").read_text())
    assert observation == {
        "exit_status": 0,
        "billing_channel": "chatgpt-plan",
        "billing_channel_source": "admitted_route_argument",
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "schema_version": 1,
        "session_handle": "thread-test",
        "usage": {"input_tokens": 12, "output_tokens": 5},
        "version": "codex-cli 9.9.9",
    }
    durable = b"".join(path.read_bytes() for path in output_dir.rglob("*") if path.is_file())
    assert b"worker-capability-sentinel" not in durable
    assert str(tmp_path).encode() not in durable


def test_cursor_adapter_launches_an_admitted_live_model_route(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    binary_dir = tmp_path / "bin"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_executable(
        binary_dir / "cursor-agent",
        """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\\n' '2099.01.01-test'
  exit 0
fi
if [ "$#" -eq 1 ] && [ "$1" = "status" ]; then
  printf '%s\\n' 'Logged in'
  exit 0
fi
if [ "$#" -eq 1 ] && [ "$1" = "--list-models" ]; then
  printf '%s\\n' 'Available models'
  printf '%s\\n' 'gpt-5.6-sol-high - GPT-5.6 Sol High'
  exit 0
fi
printf '%s\\n' "$@" > fake-args.txt
cat > fake-stdin.txt
if [ -n "${AOS_WORKER_TOKEN:-}" ] && [ -n "${CURSOR_API_KEY:-}" ]; then
  printf '%s\\n' 'present' > fake-token-status.txt
fi
printf '%s\\n' '{"type":"system","subtype":"init","session_id":"cursor-test"}'
printf '%s\\n' '{"type":"result","usage":{"input_tokens":7,"output_tokens":3}}'
""",
    )
    admitted_route = tmp_path / "route.md"
    admitted_route.write_text("# Admitted route\n")
    work_order = tmp_path / "work-order.md"
    sentinel = "private-cursor-work-order-sentinel"
    work_order.write_text(f"# Work order\n\n{sentinel}\n")
    output_dir = tmp_path / "output"

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository
                / "distro"
                / "skills"
                / "launch-cursor-worker"
                / "scripts"
                / "adapter.py"
            ),
            "launch",
            "--model",
            "gpt-5.6-sol-high",
            "--reasoning-effort",
            "high",
            "--billing-channel",
            "cursor-subscription",
            "--workspace",
            str(workspace),
            "--work-order",
            str(work_order),
            "--admitted-route",
            str(admitted_route),
            "--output-dir",
            str(output_dir),
        ],
        cwd=repository,
        env={
            "AOS_WORKER_TOKEN": "worker-capability-sentinel",
            "CURSOR_API_KEY": "cursor-auth-sentinel",
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join((str(binary_dir), "/usr/bin", "/bin")),
            "UNRELATED_SECRET": "must-not-cross",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    child_arguments = (workspace / "fake-args.txt").read_text()
    assert sentinel not in child_arguments
    assert "worker-capability-sentinel" not in child_arguments
    assert "cursor-auth-sentinel" not in child_arguments
    assert "\ngpt-5.6-sol-high\n" in f"\n{child_arguments}"
    assert "[effort=high]" not in child_arguments
    assert "--auto-review" not in child_arguments
    assert (workspace / "fake-stdin.txt").read_text() == work_order.read_text()
    assert (workspace / "fake-token-status.txt").read_text().strip() == "present"
    observation = json.loads((output_dir / "observation.json").read_text())
    assert observation == {
        "exit_status": 0,
        "billing_channel": "cursor-subscription",
        "billing_channel_source": "admitted_route_argument",
        "harness": "cursor",
        "model": "gpt-5.6-sol-high",
        "reasoning_effort": "high",
        "schema_version": 1,
        "session_handle": "cursor-test",
        "usage": {"input_tokens": 7, "output_tokens": 3},
        "version": "2099.01.01-test",
    }
    durable = b"".join(path.read_bytes() for path in output_dir.rglob("*") if path.is_file())
    assert b"worker-capability-sentinel" not in durable
    assert b"cursor-auth-sentinel" not in durable
    assert str(tmp_path).encode() not in durable
