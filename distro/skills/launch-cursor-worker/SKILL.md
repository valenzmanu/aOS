---
name: launch-cursor-worker
description: Discovers and launches a Cursor Agent CLI worker for an admitted aOS route. Use when a liaison selects a Cursor worker harness or must verify the account's current Cursor model offers and authentication.
---

# Launch Cursor Worker

Use only after reading `protocols/routing.md`.

## Discover

Run:

```sh
python distro/skills/launch-cursor-worker/scripts/adapter.py discover
```

Treat the returned model IDs, version, and authentication status as a timestamped observation.
Never replace this live catalog with remembered model slugs.

Reject the route honestly when `available` or `authenticated` is false or the selected model is
absent. Never inspect or print authentication files, tokens, or environment values.

## Launch

After dispatch admission, run:

```sh
python distro/skills/launch-cursor-worker/scripts/adapter.py launch \
  --model <admitted-model> \
  --reasoning-effort <admitted-effort> \
  --billing-channel <admitted-billing-channel> \
  --workspace <workspace> \
  --work-order <admitted-work-order> \
  --admitted-route <attempt-route.md> \
  --output-dir <private-run-output>
```

The adapter verifies and passes the exact selected live-catalog model ID. Cursor model IDs often
encode effort (for example, a `-medium` variant), so choose the catalog variant that matches the
admitted effort; the adapter records that effort without inventing a second CLI override. It
passes the work order over stdin, preserves only an allowlisted environment, and records the
billing channel declared by the admitted route. This is not provider-verified billing telemetry.
Raw logs remain private.

## Safety

- Dispatch must be admitted before launch.
- Never place the work order, credentials, or worker capability in command arguments.
- Preserve the admitted model, workspace, sandbox, scope, and aggregate budget.
- A failed launch returns to liaison judgment; it never silently changes the route.
