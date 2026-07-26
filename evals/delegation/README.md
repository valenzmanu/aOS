# E2E-DELEGATION-01 — bounded native delegation

This learning scenario compares a parent-only baseline with one fresh Codex worker that delegates
three independent, read-only lanes to native subagents. Both modes must produce the same exact
parent-owned synthesis.

The eval records correctness, wall time, usage reported by the harness, declared child count,
observed spawn/wait telemetry, and delegation depth. It does not assume that delegation is faster
or cheaper.

The first three compatible Codex pairs are summarized in
`docs/evidence/2026-07-26-codex-delegation-calibration.md`.

Run both modes from the repository root:

```sh
uv run python evals/delegation/run.py --mode both
```
