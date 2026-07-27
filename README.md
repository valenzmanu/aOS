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
- **One clone has one private runtime.** The primary checkout owns `state/`, `missions/`, and
  `projects/`; linked worktrees discover those roots through Git instead of creating local copies.

The architectural decision is [ADR 0002](docs/adr/0002-sparse-constitutional-agent-os.md). The
remaining build sequence and assurance claims are in the
[implementation plan](docs/aos-v2-implementation-plan.md). aOS preserves durable mission work in
the journal and State Tree; it does not replace harness memory.

`E2E-RECOVERY-01` proves the constitutional recovery joint with a fresh agent: one atomic
operation orphans the stalled session, closes its authority, resets the same process from
`running` to `created`, and binds a fresh session and authority epoch. The attempt, process, work
order, worktree, and partial work remain in place. Agents still diagnose the stall and decide
whether to recover; aOS adds no monitor or orchestration loop.

The current real-worker learning slice adds agent-authored layered routing, thin Codex and Cursor
launch adapters, and bounded native delegation. The liaison first ranks models for task fit, then
ranks eligible model/harness/billing routes from current observations. Either liaison harness can
select either worker adapter; billing belongs to the selected worker route. Both adapters have a
passing [direct live calibration](docs/evidence/2026-07-26-live-worker-adapter-calibration.md).

Three paired Codex calibration runs confirmed that native read-only children preserve parent
context and exact synthesis, but were inefficient for a tiny three-lane task. Median native
execution was 60.185 seconds and 226,680 input tokens versus 34.618 seconds and 116,819 without
delegation. Therefore delegation is not a default speed optimization; the worker must judge that
independent work is large enough to amortize orchestration. See the
[dated calibration evidence](docs/evidence/2026-07-26-codex-delegation-calibration.md).

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
uv run python evals/routing/run.py
uv run python evals/delegation/run.py --mode both
```

The permanent tracer remains deliberately narrow: one mission, one deterministic worker fixture,
and two scripted human decisions. The routing and delegation scenarios are learning evals, not a
new orchestration runtime. Live Cursor comparisons, generic verification, multi-mission operation,
and the general distro remain later milestones.

## Worktrees and editor sidebars

Normal resident commands need no root environment variables. From the primary checkout or a linked
aOS worktree containing this release, inspect the canonical private roots with:

```bash
uv run aos-mission paths
```

For an older linked branch, invoke the current primary installation with
`uv --directory <installation_root> run aos-mission paths`.

Explicit `--state-root` and `--missions-root` options, and their existing environment variables,
remain available as a pair for isolated evals and recovery tools. A partial pair is rejected.
If a linked worktree already contains non-empty checkout-local runtime roots, aOS fails closed
instead of choosing or merging histories.

The code-close editor bridge uses a project worktree and a sibling multi-root
`.code-workspace` under the returned `projects_root`. It puts the primary aOS checkout first, so
the sidebar starts with current liaison instructions, and the project worktree second, so its code
remains accessible. Before editing code, the liaison reads the project's own instruction hierarchy.
Mission commands run through the primary installation:

```bash
uv --directory <installation_root> run aos-mission paths
```

This private workspace supplies access to both roots without global skills or files injected into
the project. Before the first governed mutation, verify a fresh sidebar chat received the current
primary aOS instructions and can access the project worktree; filesystem access alone does not
prove instruction loading.

## Repository

```text
modules/       deterministic constitutional joints
protocols/     portable agent routing and native-delegation guidance
distro/skills/ thin Codex and Cursor discovery/launch adapters
evals/routing/ agent-authored layered route-choice proof
evals/launch/  deterministic four-cell cross-harness contract
evals/delegation/ paired parent-only/native-delegation proof
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
issues. The primary checkout's ignored roots are canonical, so destructive ignored-file cleanup is
unsafe there. Read [SECURITY.md](SECURITY.md) before operating on private repositories.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md). aOS is released under the
[MIT License](LICENSE).
