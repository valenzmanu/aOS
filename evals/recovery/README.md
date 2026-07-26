# E2E-RECOVERY-01 — agent-led stalled-worker recovery

This eval asks one narrow question:

> Can a fresh resident agent recover a mission whose worker stopped after a partial edit, without
> losing work or creating overlapping authority?

The runner creates a disposable trial, advances one mission to `implementing`, launches and stops
a deterministic worker after its first edit, then launches one fresh Codex process. From that
point the runner only waits. The agent must rotate the worker session and authority with
`replace-worker`, restart the existing process, finish the artifact in place, and submit a receipt
bound to the replacement session and authority epoch.

Run from the repository root:

```sh
uv run python evals/recovery/run.py
```

The read-only grader separates governance from mission quality. It checks the exact artifact,
single attempt/process/work order, S1-to-S2 and AE1-to-AE2 lineage, close-before-claim ordering,
replacement-bound completion, admitted operation provenance, and absence of raw capability
material or private host paths in durable mission truth.

This scenario intentionally stops at mission revision 5, `verifying`. aOS does not yet have the
general evidence interface needed to verify arbitrary work. Passing this eval proves durable
agent-led recovery; it does not claim verified evidence, human approval, or delivery.
