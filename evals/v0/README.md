# E2E-00 — aOS v0 foundation tracer

E2E-00 asks one deliberately narrow question:

> Can one fresh general-purpose agent, guided only by the checked-in distro and an explicit
> activation, drive a durable mission from draft to delivered through constitutional CLI
> operations?

This is the permanent end-to-end tracer for the first narrow v0 foundation. It intentionally does
not use Docker, Harbor, a daemon, a Python lifecycle orchestrator, or a real worker backend. The
agent is the runtime. A deterministic shell fixture stands in for one worker so this first trial
tests the liaison instructions, journal boundary, evidence binding, and approval path.

Run it from the repository root:

```sh
UV_CACHE_DIR=/tmp/aos-uv-cache uv run python evals/v0/run.py
```

The runner:

1. creates a retained but disposable trial below `evals/.runs/`;
2. copies the minimum v0 runtime sources, distro, objective, worker, and human decisions;
3. initializes schema 4 and seeds `M-V0` with `aos-v0`;
4. starts exactly one fresh Codex CLI `0.144.1` / `gpt-5.6-sol` process;
5. gives that process `ACTIVATE.md` on stdin and performs no further lifecycle operation;
6. retains the JSONL trajectory and final response; and
7. grades the State Tree, immutable journal, evidence, approval, and command trajectory.

The generated liaison capability exists only in the child-process environment. It is never put in
a command argument, fixture, manifest, or retained output. If the value appears in captured output,
the runner refuses to retain that output and fails.

## Expected path

The mission revisions are:

```text
1 draft
2 authorized
3 dispatched
4 implementing
5 verifying
6 awaiting_approval
7 delivered
```

`eval-fixture/fake-worker.sh` writes the exact requested `result.txt` and a digest-bound
`receipt.json`. Verification must record successful evidence for that result. The scripted human
approval must name that evidence, and the approval and delivery events must share one admitted
operation.

## Known gaps

- The fake worker is a deterministic shell process, not another agent or a harness adapter.
- Human decisions are signed only by checked-in provenance hashes; v0 does not authenticate a
  remote human identity.
- The distro is intentionally tiny and scenario-specific. It is evidence about the architecture,
  not a claim that the broader aOS product is complete.
- The trial relies on the locally authenticated Codex CLI. It does not test Cursor, provider
  failover, model quality distributions, retries, crashes, reconstruction, or multi-mission work.
- This tracer and its constitutional path remain the regression baseline. Later milestones may
  deepen or replace provisional components without abandoning the agent-runtime architecture it
  proves.
