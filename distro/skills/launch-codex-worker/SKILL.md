---
name: launch-codex-worker
description: Discovers and launches a Codex CLI worker for an admitted aOS route. Use when a liaison selects a Codex worker harness or must verify that the local Codex CLI route is available and authenticated.
---

# Launch Codex Worker

Use only after reading `protocols/routing.md`.

## Discover

Run:

```sh
python distro/skills/launch-codex-worker/scripts/adapter.py discover
```

Treat the JSON as a current observation, not permanent model truth. The Codex CLI does not expose
a live model catalog through this adapter, so `models` remains empty and
`catalog_source` explains the gap.

Reject the route honestly when `available` or `authenticated` is false. Never inspect or print
authentication files, tokens, or environment values.

## Launch

After dispatch admission, run:

```sh
python distro/skills/launch-codex-worker/scripts/adapter.py launch \
  --model <admitted-model> \
  --reasoning-effort <admitted-effort> \
  --billing-channel <admitted-billing-channel> \
  --workspace <workspace> \
  --work-order <admitted-work-order> \
  --admitted-route <attempt-route.md> \
  --output-dir <private-run-output>
```

The adapter passes the work-order content over stdin, applies the admitted Codex reasoning effort,
preserves only an allowlisted environment, and writes `observation.json`, `trajectory.jsonl`,
`stderr.log`, and `final.txt`. The normalized observation retains model and effort plus the billing
channel declared by the admitted route; it does not claim provider-verified billing telemetry.
Treat raw logs as private run artifacts.

## Safety

- Dispatch must be admitted before launch.
- Never place the work order, credentials, or worker capability in command arguments.
- Preserve the admitted model, workspace, sandbox, scope, and aggregate budget.
- A failed launch returns to liaison judgment; it never silently changes the route.
