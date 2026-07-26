---
title: Live Codex and Cursor worker-adapter calibration
date: 2026-07-26
scenario: E2E-LAUNCH-01
status: calibration
---

# Live worker-adapter calibration

One direct live trial per worker harness exercised the same admitted-route, stdin work-order, and
normalized-observation boundary.

| Worker harness | CLI version | Selected model | Effort | Declared billing | Result |
| --- | --- | --- | --- | --- | --- |
| Codex | `codex-cli 0.145.0` | `gpt-5.6-sol` | medium | `chatgpt-plan` | pass |
| Cursor | `2026.07.20-8cc9c0b` | `gpt-5.6-terra-medium` | medium | `cursor-subscription` | pass |

Both observations preserved harness, exact model, reasoning effort, CLI version, session handle,
exit status, and harness-reported usage without credentials or private host paths. Billing is
explicitly labeled as an admitted-route argument, not provider-verified telemetry.

## Iteration findings

The first Cursor attempt failed before launch because `--auto-review` and `--force` are mutually
exclusive in the installed CLI. The adapter now uses the noninteractive `--force` mode with the
explicit sandbox and has a regression assertion forbidding the conflicting flag.

The second Cursor attempt failed before launch because current catalog IDs already encode effort,
and the CLI rejected an added bracketed effort override. The adapter now passes the exact
live-catalog model ID and rejects a recognizable model-effort mismatch. The unchanged admitted
route then passed.

## Limits

These are worker-adapter proofs, not live liaison/worker matrix results and not provider quality or
cost comparisons. The deterministic four-cell contract proves that liaison identity does not
select the adapter; live nested liaison-to-worker cells remain future comparative evidence.
