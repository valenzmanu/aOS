# aOS — an operating substrate for coding agents

aOS helps one human direct a workforce of agents without micromanaging every session. It keeps
durable mission truth outside any individual agent conversation while leaving reasoning and
execution to the agents themselves.

Agents navigate a State Tree and follow versioned distro instructions. A small constitutional
kernel admits or rejects contested changes, records authority, binds verifier evidence, and
requires explicit human decisions. It does not decide what work to do or orchestrate agent loops.

> **Alpha / cooperative assurance.** aOS provides durable coordination and auditable policy
> joints, not a hardened security sandbox.

## Architecture

- **Agents are the runtime.** Liaison and worker agents interpret objectives and choose actions.
- **Liaison behavior is resident and non-exclusive.** Every harness in an aOS root may act as a
  liaison participant; authored session records provide attribution without leases or ownership.
- **The file tree is the interface.** Missions, contracts, attempts, work orders, sessions, and
  evidence are inspectable files.
- **The journal is canonical.** SQLite records immutable events, revisions, admissions, authority
  epochs, evidence, and approvals.
- **Code is sparse.** `modules/` implements only deterministic constitutional behavior.
- **Residence establishes liaison behavior.** Reading the root distro is sufficient; contested
  changes still require revision-checked journal admission and explicit human provenance where
  required.

The architectural decision is [ADR 0002](docs/adr/0002-sparse-constitutional-agent-os.md). The
remaining build sequence and assurance claims are in the
[implementation plan](docs/aos-v2-implementation-plan.md). aOS preserves durable mission work in
the journal and State Tree; it does not replace harness memory.

`E2E-RECOVERY-01` proves the constitutional recovery joint with a fresh agent: one atomic
operation orphans the stalled session, closes its authority, resets the same process from
`running` to `created`, and binds a fresh session and authority epoch. The attempt, process, work
order, worktree, and partial work remain in place. Agents still diagnose the stall and decide
whether to recover; aOS adds no monitor or orchestration loop.

## Proven foundation

`E2E-00` starts a fresh Codex agent resident in a prepared aOS root with only the checked-in
distro, objective, and scripted human decisions. It records its session; after launch, the runner
waits while the agent drives the lifecycle.

The permanent tracer proves:

```text
draft → authorized → dispatched → implementing → verifying
      → awaiting_approval → delivered
```

Its grader checks the agent trajectory plus the durable contract, attempt, process, work order,
session, evidence, approval, State Tree, and journal causality. Docker is not required.

Run it with an authenticated Codex CLI:

```bash
uv sync --extra dev
uv run python evals/v0/run.py
uv run python evals/recovery/run.py
```

The current tracer is deliberately narrow: one mission, one deterministic worker fixture, and two
scripted human decisions. Generic verification, real worker adapters, multi-mission operation,
and the general distro remain later milestones.

## Repository

```text
modules/       deterministic constitutional joints
evals/recovery/ agent-driven stalled-worker recovery proof
evals/v0/      permanent agent-runtime vertical tracer
tests/v2/      governance, protocol, migration, and tracer tests
docs/          architecture decision and implementation plan
```

Archived schema fixtures remain only to prove deterministic migration. The legacy Python control
plane, daemon, provider adapters, dashboard, and orchestration CLI have been removed.

## Development and privacy

```bash
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

Runtime roots such as `state/`, `missions/`, and `projects/` can contain prompts, source excerpts,
transcripts, and capability material. They are ignored by Git and must never be attached to public
issues. Read [SECURITY.md](SECURITY.md) before operating on private repositories.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md). aOS is released under the
[MIT License](LICENSE).
