---
title: aOS 2.0 implementation plan — SCAOS substrate
status: v0 foundation established; next focus mission controls → readable project state → new-agent continuation
decision: docs/adr/0002-sparse-constitutional-agent-os.md
predecessor: historical 0.1 plan at e920ff2:docs/aos-poc-plan.md
harbor_baseline: 0.20.0; task schema 1.3
---

# aOS 2.0 implementation plan

> **Cutover status (2026-07-23):** the superseded `aos/` source runtime, old console entrypoints,
> provider adapters, dashboard, demo, and editor launch surfaces have been removed by explicit
> product decision. Historical source remains in Git, and deterministic schema fixtures remain as
> migration evidence. References below to the 0.1 runtime are historical requirements or migration
> criteria; they do not describe an available repository surface.

Read ADR 0002 first. It contains the architectural decision; this file fixes the implementation
contracts, build order, and evidence required to claim each milestone. The measurement layer is
built before the intent layer because prose has no compiler.

## What aOS preserves

aOS is not a replacement for harness memory. Harnesses may keep their own context windows. aOS
preserves the durable work a new human or liaison must recover after a closed chat, harness switch,
or new machine:

| aOS concept | What it is | Why it exists |
| --- | --- | --- |
| Distro (`AGENTS.md`, protocols, skills) | Portable instructions for resident agents | Turns a general harness into an aOS participant |
| State Tree (`missions/`, sessions, evidence files) | Navigable project interface | A new agent can open files and see what is going on |
| Journal | Canonical event history | Contested truth survives outside any conversation |
| Human decisions | Explicit authorize / approve provenance | Authority is placed, not assumed |
| Liaison session (`LS-*`) | Attribution for a resident harness | Coordination only; never exclusive ownership |
| Worker / process capability | Scoped delegated authority | Execution stays inside a work-order membrane |

The honest test: delete every agent session, give a clean checkout plus a portable export to a
fresh harness, and require that it can answer *running / blocked / decided-why / verified /
approvable* from journal + State Tree alone, author its own liaison session, and admit a follow-up
transition without inheriting prior liaison ownership.

### Build priority (plain names first)

Stable milestone IDs remain in parentheses for tests and historical cross-references. Prefer the
plain names in discussion and docs:

```text
Durable foundation (M1) — partially landed
  → Mission controls (M2)
  → Readable project state (M3)
  → New-agent continuation test (H-01; may land before full M5 packaging)
  → Real workers (M4)
  → Continue on a new machine / publish closure (M5)
```

The **first end-to-end trial** (`E2E-00`) remains the permanent thin tracer for one resident
mission. It does not satisfy continuation until a fresh liaison can reconstruct from journal +
State Tree without the original harness conversation.

Current foundation:

- **Landed:** schema-4 journal/protocol joints, resident non-exclusive liaison sessions, first
  end-to-end trial (`E2E-00`), legacy Python control-plane removal.
- **Next:** mission controls (M2), then readable project state (M3), then a new-agent continuation
  path reusable by `H-01`.

## Ground rules for the implementor

1. **Eval-first.** No distro behavior ships without a scenario exercising it. A governance
   failure blocks merge regardless of mission quality.
2. **Two grades, never weighted together.** Every valid scenario reports governance
   (deterministic pass/fail) and mission quality (deterministic fixture score in early milestones,
   rubric-judged later). Harbor may receive a gated selection signal, but it is not a third grade.
   Infrastructure errors invalidate a run; they are not scored as poor agent performance.
3. **Ratchet deterministic authority.** Adding a constitutional module, adding a gate, or
   expanding what an existing module controls requires a recorded freestyle failure and an ADR
   amendment. Pure utilities, adapters, renderers, and eval checks are not constitutional modules.
4. **Characterize, then port.** Preserve the externally observable safety semantics proven by
   0.1: lease atomicity, scoped and expiring capabilities, token hashing, event ordering, durable
   IDs and hashes, retry identities, worktree isolation, and verifier failure blocking delivery.
   Do not preserve defects such as caller-supplied FSM edges, unjournaled rejections, or
   evidence-free approval.
5. **Honest enforcement.** Level 1 protects the canonical module path for cooperative actors. It
   does not protect SQLite or the filesystem from a hostile local process. Direct journal
   tampering is outside level-1 assurance; physical unskippability begins at level 2.
6. **One writer generation at a time.** Keep 0.1 runnable and regression-tested through the M5
   pre-cutover gate, but never let 0.1 and 2.0 write the same state root. Development and evals use
   isolated copies. Live migration and writer cutover happen once in M5.
7. **One temporary duplication boundary.** Through M4, feature-frozen `aos/` is the explicit
   exception: neither substrate imports the other, and 0.1 gains no new behavior. The unchanged
   legacy suite exercises `aos/` on legacy roots; v2 suites exercise `modules/` on disposable
   migrated roots. M5 removes `aos/` and the exception.
8. **Every mutation is attributable and replay-safe.** Contested operations require an
   idempotency key and expected aggregate revision, and leave an admitted or rejected journal
   decision. Retrying the same request cannot duplicate a database or external effect.
9. **V2 secrets never become state or output.** Raw capability tokens are accepted only from the
   environment or injected directly into a child process. They never appear in command arguments,
   rendered stdout/stderr, events, artifacts, State Tree files, or Harbor results. Frozen 0.1 keeps
   its legacy token UX only in isolated regression/rollback use; no v2 path or published evidence
   may use it.
10. **The clone is the distro.** A clean clone is the supported installation unit. Python
    packaging makes `modules/` importable for the toolbelt, but a wheel alone is not a complete
    aOS installation because protocols, skills, and the distro entry are required.

## Resolved implementation contracts

These decisions close questions that would otherwise produce incompatible implementations.

### Legacy database compatibility and cutover

- SQLite is the v2 journal. Replacing it requires another ADR.
- Compatibility means a **lossless, one-way migration**, not dual-writer compatibility. The
  migration must open synthetic-but-structurally-real schema-v1 and schema-v2 databases generated
  from commits `7187645` and `e920ff2`.
- Migration preserves every existing event ID, order, timestamp, kind, severity, aggregate,
  payload, durable entity ID, content hash, and inbox cursor. New facts are additive and
  schema-versioned.
- Legacy history receives one migration-boundary event and, where old events cannot reconstruct a
  projection, a content-addressed migration snapshot. New governance invariants apply after that
  boundary; migration must not invent historical `approval.decided` or admission events.
- The migration snapshot deterministically assigns a baseline revision to every imported contested
  aggregate. Repeating migration produces the same revisions and snapshot digest.
- Present legacy artifacts are scanned before publication. Safe artifacts are ingested into the
  blob store with an additive old-path-to-digest mapping while historical event payloads remain
  unchanged. Secret-bearing or policy-unsafe bytes stay only in the protected pre-migration backup
  or a nonexportable quarantine outside canonical v2 state; v2 records their path, digest,
  quarantine reason, and unavailable/non-approvable status without copying the bytes into events,
  blobs, State Tree, or exports. Missing artifacts are recorded as missing and never fabricated.
  Legacy absolute paths remain historical metadata only; no v2 reconstruction or resume operation
  may depend on them. “Lossless” means lossless database/history migration plus a retained rollback
  backup, not unconditional publication of unsafe external bytes.
- Migration uses SQLite backup/checkpoint semantics, is idempotent, and passes
  `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Artifact bytes are classified, staged,
  and promoted under the blob-publication protocol before the final single SQLite transaction
  commits schema changes, projections, references, and the migration boundary. An injected failure
  leaves the source usable; any promoted-but-unreferenced blob is harmless and collectable. An
  unknown newer schema is rejected before any write.
- Schema changes occur only through an explicit journal migration operation. Ordinary reads and
  tool commands refuse unsupported schemas rather than silently rewriting metadata.
- The 0.1 acceptance suite remains green through M4. It runs against its own state root; v2 tests
  and Harbor trials use disposable roots. M5 freezes 0.1, backs up the live database, migrates it,
  and starts the v2 writer.
- At live cutover, migration closes every legacy liaison/process authority epoch. Active legacy
  process, session, and attempt projections become `orphaned`; active missions become `failed`;
  additive reconciliation events explain each change. No legacy raw token is reused. A legacy
  mission in `awaiting_approval` remains visible but non-approvable until fresh v2 verification
  produces current immutable evidence, or a human explicitly rejects/demotes it for rework.

### Journal, projections, gates, and external effects

`events` plus immutable content-addressed blobs are the replay source. Normalized SQLite tables are
transactionally maintained SQL projections. Generated State Tree files are eventually consistent
projections stamped with their source event ID; they are never part of a SQLite transaction.

Every v2 event has a versioned envelope containing:

- event ID and time;
- kind and payload schema version;
- event aggregate type and ID, plus any target-domain aggregate IDs;
- invoker kind and ID, with a liaison session ID for resident liaison operations or an
  authority/lease epoch ID and evaluated expiry when authority-bearing;
- optional `on_behalf_of` principal attribution and decision-source hash for relayed human actions;
- request/idempotency key;
- causation ID and admission ID, when applicable;
- canonical JSON payload with no secret-shaped data at any nesting depth.

Legitimate invokers include resident liaison sessions, workers, maintenance operators,
module/system, migration, reconciler, and eval fixtures. Liaison sessions provide attribution and
never carry an authority epoch. Only delegated authority-bearing invokers such as workers require
an epoch. A system reconciliation event and the journaled rejection of an expired worker mutation
remain valid events.

Revision rules are exact:

- operation/gate/effect events belong to an operation aggregate and do not advance target-domain
  revisions;
- a rejection records expected and observed target revisions; every target remains unchanged;
- each admitted domain event advances its own aggregate exactly once, even when one operation emits
  admission, approval, transition, and effect events;
- an operation declares expected revisions for every existing contested aggregate it reads or
  writes; a new aggregate uses `absent`/revision 0;
- operation IDs are repository-wide unique and bound to a canonical request hash. Delegated
  authority invoker identity remains part of that hash. Resident liaison session ID is attribution,
  not semantic identity: an identical request may replay across liaison sessions, while different
  input or human-decision provenance is rejected.

For a database-only governed operation, one `BEGIN IMMEDIATE` transaction:

1. validates the resident liaison session attribution or authenticates a delegated capability and
   records its authority epoch;
2. checks every validator applicable at that milestone: expected revision, authorization policy,
   FSM edge, preconditions, and (after M3 integration) the pinned distro;
3. appends `gate.admitted` or `gate.rejected`;
4. on admission, updates SQL projections and appends the domain event in the same transaction.

A rejection is committed before the command returns exit 1. Raising an exception must not roll the
rejection back. Reserved contested event kinds cannot be created through a generic append API.

Git worktrees, subprocesses, and verification commands cannot share a SQLite transaction. They use
an admit-before-effect protocol:

1. commit `gate.admitted` and an effect intent under one idempotency key;
2. perform the external effect;
3. in a final transaction, verify the effect, append `effect.completed`, and commit any dependent
   domain event/projection update; otherwise append `effect.failed`;
4. reconcile admitted-but-incomplete effects after a crash.

Every operation that publishes a replay-critical blob uses the same protocol. Before admission it
stages, hashes, validates, and fsyncs the bytes outside the canonical CAS namespace. After the
effect intent commits, it atomically promotes the staged object to `blobs/sha256/` (or verifies an
identical existing object). Only the final transaction may append a domain event or projection
reference to that digest, after rechecking the canonical object. An interrupted promotion can
leave an unreferenced blob, which is safe to garbage-collect; the database must never reference a
missing blob.

Replaying an operation returns its recorded outcome and never creates a second worktree, branch,
process, session, or evidence object. Reusing an operation ID with a different canonical request
hash is a rejection, not a replay. Creation operations use expected revision 0. `events` is
protected from accidental update/delete through the canonical module API and database guards, but
this is still cooperative append-only at level 1.

### Authority, revisions, and state machines

Every contested aggregate has a monotonically increasing integer revision. Gates compare the
caller's expected revision before admission.

| Actor | May do | Must not do |
| --- | --- | --- |
| Human decision, relayed by a resident liaison session | authorize a contract revision; approve or reject delivery; widen authority or retry ceiling; order cancellation | masquerade as a worker or omit decision provenance |
| Resident liaison session, locally attributed and non-exclusive | create authored session records and drafts; route and dispatch authorized work; choose retry level; signal processes; invoke verification; propose transitions | authorize its own contract; approve its own delivery; treat another session record as ownership or a lock |
| Scoped worker capability | start/update its own process/session; append checkpoints; publish candidate artifacts | mutate mission state; widen scope; verify, approve, deliver, spawn, or delegate |
| Maintenance operator, locally attributed but not mission-authority-bearing | initialize an empty root; explicitly migrate a process-quiesced root with no live v2 authority epoch | perform mission work, approve delivery, or bypass a live writer/v2 epoch |
| Module/system, migration, or reconciler | append narrowly typed mechanical/recovery facts under a causative operation | claim human judgment or widen mission authority |
| Eval verifier | read exported state and trajectories; write verifier-only grades | mutate runtime journal or State Tree |

At level 1, a human principal is explicit **attribution**, relayed by a resident liaison session, not
cryptographically authenticated identity. The gate proves that a worker capability cannot approve
and records both the invoking liaison session and who the liaison says made the decision. Strong
principal authentication is level-2 work.

Bootstrap is only the empty-root initialization case of maintenance. Initialization and every
later explicit schema migration require an operation ID, request hash, expected schema/root state,
and journaled maintenance/migration facts. They are admitted only on a quiesced root with no live
delegated authority epoch or v2 external-effect intent. A migration may import still-unexpired
legacy epoch rows only after OS/process checks prove all 0.1 writers are stopped; its final
transaction closes imported liaison epochs as historical cutover facts and reconciles delegated
epochs as required. Initializing a nonempty root is permanently forbidden; later maintenance never
acquires mission authority.

The root's `AGENTS.md` establishes liaison behavior for every resident harness. Before its first
governed mission mutation, a harness authors a unique record under
`state/liaison/sessions/<session-id>/`. Session status and timestamps are coordination hints only:
they grant no authority, block nothing, and require no close, expiry, transfer, or takeover. The
journal attributes liaison operations to the session ID and gates conflicting work through
expected aggregate revisions, operation IDs, and canonical request hashes.

Schema 4 already permits epoch-free invokers, so the resident-session cutover does not require a
schema-number change or history rewrite. Any pre-cutover active liaison epoch is immediately inert:
resident liaison admission never queries it. The explicit replay-safe
`journal.retire_liaison_authority` maintenance operation may close those historical rows with
`authority.legacy_closed` events and no replacement epoch. This cleanup improves historical
legibility but is never a prerequisite for resident sessions to proceed.

V2 preserves rolling TTL behavior for delegated worker/process authority. Creation or rotation
creates an authority epoch with an absolute expiry. An authority-bearing worker mutation may
explicitly request renewal; the gate evaluates time through an injectable clock and appends
`authority.renewed` in the same transaction. Reads and unrelated commands never renew silently.
Release, rotation, expiry reconciliation, and cancellation append revocation/closure facts. No
canonical worker mutation is accepted under an expired epoch, although its rejection and
subsequent system reconciliation are journaled.

The protocol module owns a closed graph; callers never supply `allowed_from` sets.

Mission edges:

```text
draft             -> authorized | cancelled
authorized        -> authorized* | dispatched | cancelled
dispatched        -> implementing | failed | non_convergent | cancelled
implementing      -> verifying | failed | non_convergent | cancelled
verifying         -> awaiting_approval | failed | non_convergent | cancelled
awaiting_approval -> delivered | failed | cancelled
failed            -> dispatched | authorized* | cancelled
non_convergent    -> authorized* | cancelled
delivered | cancelled : no outgoing edges
```

`awaiting_approval -> failed` represents an explicit human rejection requiring rework. A transition
to `non_convergent` is admitted only after the authorized attempt ceiling is reached. The ceiling is
part of the contract, defaulted/backfilled to three; the gate enforces it while distro intent
chooses whether and how to retry. `authorized*` is not an ordinary retry edge: it requires a new
human-authorized contract revision. A contract amendment is allowed only while `authorized`,
`failed`, or `non_convergent`; it may also replace an already-authorized revision in place. It
invalidates prior routing and evidence, cannot set an attempt ceiling below attempts already used,
and returns the mission to `authorized`. Thus `non_convergent` is terminal under the current
contract, not beyond an explicit human scope change.

Process edges:

```text
created -> running | failed | cancelled | orphaned
running -> paused | blocked | completed | failed | cancelled | orphaned
paused -> running | cancelled | orphaned
blocked -> running | failed | cancelled | orphaned
failed | orphaned -> created
    only for a session retry with a new authority epoch
completed | cancelled : no outgoing edges
```

Worker-session edges:

```text
created -> running | failed | cancelled | orphaned
running -> paused | blocked | completed | failed | cancelled | orphaned
paused -> running | cancelled | orphaned
blocked -> running | failed | cancelled | orphaned
completed | failed | cancelled | orphaned : no outgoing edges
```

Attempt edges:

```text
active -> completed | failed | orphaned | cancelled
orphaned -> active
    only for admitted same-attempt recovery
completed -> active
    only for human-rejected rework using a new process in the same worktree
failed | cancelled -> no outgoing edges
```

Retry identity is preserved:

- **Session retry:** same process, attempt, and worktree; new authority epoch, work-order revision,
  and worker-session aggregate. A terminal session itself is never reopened.
- **Process retry:** new process and session in the same attempt and worktree.
- **Attempt retry:** new attempt, worktree, branch, process, and session.

Expired-worker reconciliation marks the process, current session, and attempt `orphaned` and the
active mission `failed`. It does not choose a backend, create replacement work, or retry; those are
liaison judgments guided by the distro.

The ceiling limits distinct attempt aggregates, not recovery actions inside an existing attempt. A
new attempt may be created only while the number already created is below the authorized ceiling.
Closing an active attempt as failed moves the mission to `failed` when another attempt remains
available, and directly to `non_convergent` when the failed attempt consumes the final slot.
Counting or creating the final attempt is insufficient: `non_convergent` requires that attempt to
have actually failed.

`failed -> dispatched` therefore declares a retry kind and validates the current attempt state:

- a session/process retry may continue an `active` attempt after its subordinate failed;
- expired-worker recovery may reopen an `orphaned` attempt;
- human-rejected verified work may reopen a `completed` attempt with a new process, never a session
  retry;
- an attempt retry creates a new aggregate only after the current attempt is `failed` and unused
  attempt budget remains.

The first three paths remain legal when the attempt count equals the ceiling because they do not
create an attempt. A failed final attempt has already moved the mission to `non_convergent` and
requires a new human-authorized contract revision before further work.

Cancellation from `draft`/`authorized` and human rejection from `awaiting_approval` are intentional
v2 additions, not claims about proven 0.1 behavior. Their authorization and attempt-state effects
are covered by `G-03`, `G-10`, and `G-11`.

### Evidence and approval

Verification evidence is immutable and content-addressed. An evidence record binds:

- mission revision and authorized contract revision/hash;
- attempt, process, and worker session;
- exact Git HEAD, index digest, and a canonical worktree manifest;
- verifier specification and command hash;
- start/end times, exit status, and output-artifact digests.

The worktree manifest hashes every filesystem entry except `.git/` and explicitly declared
verifier exclusions. It includes tracked, staged, unstaged, untracked, and ignored entries; file
type/mode; symlink target; submodule commit/dirty state; and content digest. The verifier
specification records and hashes its exclusions. Approval recomputes the same manifest, so a
post-verification mutation cannot hide behind Git ignore rules or an unchanged HEAD.

The verifier stores bounded logs as artifacts rather than embedding arbitrary command output in the
journal. Approval requires successful, untampered evidence for the current contract and worktree,
the expected mission revision, and an asserted human principal. It appends
`approval.decided` naming the evidence and decision; `delivered` is admitted in the same database
transaction only for an approval. Rejection moves the mission to `failed` with a reason.

External children receive explicit environment allowlists. Verification receives no liaison,
worker, provider, or judge credential. A worker adapter receives only its scoped process capability
plus the provider authentication strictly required for that worker. Rendering and reconciliation
children receive no authority token unless the invoked operation requires it. Implementations must
not pass inherited `os.environ` wholesale.

### Toolbelt contract and credential handling

Each `bin/` command is one stable agent-facing operation. It may compose several constitutional
validators, but every contested database mutation commits through one journal transaction. Modules
remain importable by tests and internal composition; no agent-facing workflow depends on direct
Python imports or `aos/cli.py`.

Stdout is one stable JSON object containing `status`, `operation_id`, relevant
event/entity/revision fields, and `help[]`. Diagnostics go to stderr.

- exit 0: admitted and completed, or an idempotent replay of that result;
- exit 1: constitutional rejection, including its committed rejection event ID;
- exit 2: invalid invocation, unsupported schema, or operational/infrastructure failure.

Liaison commands accept a nonsecret session ID whose authored record lives under
`state/liaison/sessions/`; they never accept or reconstruct a root-wide liaison token. The trusted
dispatch wrapper generates a worker token before admission, passes its hash to the module, and
injects the raw value directly into the adapter child environment. If the wrapper crashes before
handoff is confirmed, reconciliation closes that worker epoch and creates a new one rather than
recovering the secret. Tokens never pass through JSON or files. Tests scan the database, terminal
output, State Tree, artifacts, and Harbor results for leakage.

Mutation commands require a caller-supplied nonsecret operation ID and expected revision; omission
is exit 2. The distro teaches agents to persist the operation ID and reuse it after an uncertain
result. Human decisions arrive as a structured stdin envelope containing principal attribution,
decision, scope/revision, and a source reference. Only the minimal decision fields and content hash
are journaled; raw conversation text is not.

### Eval and Harbor contract

Harbor is an eval dependency, never a constitutional runtime dependency.

- Pin `harbor==0.20.0` (commit `459ff6e`) and task schema `1.3` in an eval-only
  `evals/pyproject.toml` + lockfile using Python 3.12. Keep aOS runtime support at Python 3.11+.
- M1 scripted scenarios use Harbor's built-in `oracle` agent and checked-in
  `solution/solve.sh`; no model or custom Harbor agent is required.
- One Harbor trial owns one isolated aOS installation. Its Oracle script drives all simulated aOS
  actors inside the trial; Harbor does not manage those simulated workers individually.
- `evals/materialize.py` builds a content-addressed eval image/context from an explicit allowlist of
  current source. It never copies `.git`, credentials, `state/`, `projects/`, `missions/`, prior
  runs, or unrelated workspace files. This is required because a task Docker build context is its
  own `environment/` directory.
- Scored tasks use a separate verifier environment built from each task's `tests/` directory.
  `tests/Dockerfile` must bake `/tests/test.sh` and all verifier code into that image; Harbor does
  not upload tests into a separate verifier. Tasks transfer a checkpointed SQLite export, declared
  evidence, and (for real agents) the Harbor ATIF trajectory; verifier code and judge credentials
  never enter the agent environment. A missing export, evidence object, trajectory, or declared
  artifact makes `run_status` infrastructure-invalid and the wrapper fails.
- Every valid trial writes `/logs/verifier/reward.json` containing numeric `governance`, `mission`,
  and `reward`, where `reward = mission` when governance is 1 and `0` otherwise. `reward` is only
  Harbor's gated selection signal, not another grade or a weighted blend.
- `/logs/verifier/grades.json` is canonical and includes per-check results, mission dimensions,
  grading method, rubric/judge versions, reasoning artifacts, and `run_status`.
- M1 mission grading is deterministic fixture completion (`0` or `1`). M4 introduces judged
  mission quality with named outcome and liaison-judgment dimensions.
- `evals/run.py` materializes inputs, invokes the pinned Harbor environment, uses a unique run
  directory, parses job/trial `result.json` plus `grades.json`, and exits nonzero for trial errors,
  malformed/missing grades, any governance failure, or configured mission-regression failure.
  Harbor returning successfully with reward 0 is not treated as suite success.
- The root command `uv run python evals/run.py ...` is a standard-library bootstrap only. It
  delegates Harbor execution to
  `uv run --project evals --locked --python 3.12 harbor ...`; the root Python environment never
  imports Harbor. Direct Harbor invocation is unsupported for project evidence; the wrapper routes
  every job under ignored `evals/.runs/` with a unique name. Root `jobs/` is ignored only as a leak
  safety net.
- Checked-in job configurations define the M3 reference run, M4 harness comparison, judge
  calibration, degraded-distro experiment, and repeated-trial counts. Ad-hoc flags are recorded in
  the run manifest and cannot replace release evidence.
- Deterministic/Oracle agent and verifier phases run with no network. Real-agent tasks declare the
  minimum public/allowlisted network needed for their pinned harness/provider; judge network is
  allowlisted only in the separate verifier phase. Credentials are inherited from the runner
  environment, never job files or command arguments.
- Harbor v0.20's built-in Codex adapter materializes provider auth inside the trial, which violates
  this repository's no-token-file rule. Use a thin eval-only Codex adapter that preserves Harbor's
  Codex execution/ATIF behavior and pins the Codex CLI binary. It maps the runner credential to
  `CODEX_API_KEY` in the CLI process environment; `OPENAI_API_KEY` is not a substitute for this
  auth path. The adapter configures an explicit spawned-shell environment allowlist that excludes
  `CODEX_API_KEY`, so Codex can authenticate but agent-invoked shell commands cannot read the
  credential. It is a nonconstitutional eval adapter. Cursor trials use `cursor-cli`; never pass a
  fictitious Cursor version override that would suppress truthful autodetection.
- Every run manifest records aOS commit/tree hash, distro manifest, task hashes, Harbor/Python,
  agent/model/judge versions, environment image digest, seed where supported, and credential-free
  command configuration.

## Target repository shape

```text
aOS/
├── AGENTS.md
├── protocols/
│   ├── constitution.md
│   ├── mission.md
│   └── work-order.md
├── skills/
├── bin/
├── modules/
│   ├── __init__.py
│   ├── journal.py
│   ├── lease.py
│   ├── protocol.py
│   ├── authz.py
│   ├── worktree.py
│   ├── verify.py
│   ├── approve.py
│   └── pin.py
├── missions/                         # local State Tree; gitignored
│   └── M-XXXX/
│       ├── contract/
│       │   ├── draft.md              # authored; freely editable before authorization
│       │   └── revisions/0001.md     # generated authorized snapshot
│       ├── route-proposals/           # authored rationale, keyed by operation ID
│       ├── status.json               # generated; includes source_event_id
│       ├── events.jsonl              # generated journal projection
│       └── attempts/0001/
│           ├── attempt.json
│           ├── worktree.json         # generated logical locator/relocation metadata
│           ├── worktree              # local symlink; excluded from export/deterministic digest
│           └── processes/P-XXXX/
│               ├── process.json
│               ├── route.md           # generated immutable snapshot of admitted proposal
│               ├── work-orders/0001.md
│               ├── sessions/S-XXXX/
│               ├── checkpoints/
│               └── evidence/
├── evals/
│   ├── pyproject.toml                # Python 3.12, pinned Harbor
│   ├── uv.lock
│   ├── materialize.py
│   ├── run.py
│   ├── agents/                       # eval-only harness adapters; nonconstitutional
│   ├── configs/                      # checked-in reference/comparison/calibration jobs
│   ├── schemas/grades.schema.json
│   ├── tasks/
│   ├── checks/
│   └── rubrics/
├── state/                            # selected canonical journal root; gitignored
│   ├── liaison/sessions/             # authored, non-exclusive harness attribution
│   ├── aos.db
│   └── blobs/sha256/                 # immutable content-addressed snapshots/evidence
└── aos/                              # feature-frozen 0.1; isolated until M5 cutover
```

Authored drafts and `route-proposals/<operation-id>.md` may be edited before use. Dispatch ingests
the admitted proposal as a content blob and records its hash with the selected backend/model; the
process-level `route.md` is then generated. Authorized contracts, work orders, selected-route facts,
status, evidence indexes, and event projections are generated snapshots. Editing a generated file
creates detectable divergence; it does not change canonical truth. Generated projections are
written atomically, stamped with their source event ID, and regenerable byte-for-byte. Authored
files are preserved/exported, not reconstructed from journal unless explicitly ingested as a blob.
The operational worktree symlink is neither portable truth nor part of the deterministic projection
digest; `worktree.json` carries stable logical and relocation metadata.

The build includes `modules` as an installable package during coexistence. Each `bin/` command
resolves the clone root rather than depending on the current directory. Packaging acceptance runs
commands from outside the checkout and confirms that the clone's protocols and skills are found.

## Milestones

Use the plain title in discussion. The legacy `M#` / scenario IDs stay in parentheses so existing
evidence names remain stable.

### Decision artifacts and guardrails (M0)

Completed in commit `eb9c6a4`: the design and disarmament changes were committed together after
the legacy watcher was stopped and the repository-development instructions were verified.

- Commit ADR 0002, this plan, implementation-mode `AGENTS.md`, disarmed editor surfaces, and
  runtime/eval ignore rules. The public-tree scan treats `missions/` as private even if a future
  ignore regression makes it visible.
- Confirm no 0.1 liaison lease, watch process, sync, or dispatch is started by repository
  instructions.
- Run unit tests, lint, and the public-tree privacy scan before the commit.

**Exit evidence:** a clean commit containing only decision/guardrail files; `uv run pytest`,
`uv run ruff check .`, and `uv run python scripts/check_public_tree.py` green.

### Durable foundation (M1)

Contested truth must already survive outside any harness chat. Delivered as four independently
green slices.

#### M1a — Characterization and migration

- Freeze the 20 current acceptance cases as 0.1 characterization tests.
- Create synthetic golden schema-v1 and schema-v2 databases using the actual code at commits
  `7187645` and `e920ff2`; do not fake v1 by changing only the metadata value.
- Implement and test the explicit migration contract: preservation, boundary snapshot, repeat run,
  safe-artifact ingestion, secret quarantine, staged blob publication, injected rollback,
  future-schema refusal, integrity, and foreign keys.

#### M1b — Journal, lease, and protocol core

- Implement v2 ownership of the journal, lease/process authority, canonical content hashing, and
  transition logic in `modules/journal.py`, `modules/lease.py`, and `modules/protocol.py`, porting
  characterized behavior while the isolated 0.1 copy remains untouched.
- Add event envelopes, aggregate revisions, authority epochs, idempotency, committed gate
  rejections, reserved event kinds, and a centralized exhaustive FSM.
- Define the versioned evidence and approval record/event vocabulary used by governance fixtures;
  M2 implements evidence production and approval gates against this contract.
- Make new events replayable into fresh SQL projections. Legacy state replays from its migration
  snapshot plus subsequent events.
- Keep the unchanged legacy acceptance suite green on `aos/`; neither substrate imports the other.

#### M1c — Governance checks

Implement checks as read-only queries over an exported journal:

- no canonical `delivered` transition without an admitted `approval.decided` naming current
  evidence;
- no canonical authority-bearing mutation admitted under an expired epoch;
- no dispatch domain event or effect intent without a preceding admission;
- no illegal mission/process/session edge;
- no projection revision change without its causative event;
- no post-migration reserved domain event lacking the v2 envelope.

Every check has a known-good fixture and a minimally corrupted fixture proving it fails.

#### M1d — Harbor compatibility gate

- Pin the eval environment and prove task schema 1.3, Docker, Oracle execution,
  `solution/solve.sh`, separate-verifier artifact transfer, multi-metric `reward.json`, retained
  verifier details, result parsing, and wrapper failure behavior in `HBR-01`.
- Only after `HBR-01` passes, package the ported acceptance scenarios as Harbor tasks:
  - `G-01`: narrow empty-root maintenance, concurrent resident liaison sessions, stale-session
    tolerance, different-mission concurrency, and same-revision conflict rejection;
  - `G-02`: missing, wrong, cross-process, expired, and revoked delegated worker authority with
    historical epochs;
  - `G-03`: legal/illegal FSM cross-product, including new v2 cancel/reject/amend edges, with
    persisted rejection;
  - `G-04`: expired-worker reconciliation to orphaned/failed, without automatic rerouting;
  - `G-05`: zombie event rejection without projection mutation;
  - `G-06`: retry identity and contract-bound non-convergence ceiling;
  - `G-07`: actual v1/v2 migration, safe-artifact ingestion, quarantine, CAS crash recovery,
    baseline revisions, replay, idempotency, rollback, and future-schema refusal;
  - `G-08`: operation/domain revision semantics and event/projection atomic rollback under injected
    faults.

**Exit evidence:** unchanged legacy and v2 unit suites green; neither `modules/` nor `aos/` imports
the other; the pinned Harbor Oracle suite runs only against disposable state; `evals/run.py`
produces valid two-grade reports and fails closed on every corrupted fixture.

### Mission controls (M2)

Humans authorize and approve; resident liaisons propose gated transitions; workers receive only
scoped membranes. Without mission controls, durable foundation exists but agents cannot operate
real missions through trusted commands.

**Learning mission:** [`evals/learning/aos-landing/`](../evals/learning/aos-landing/) starts from a
vague human request (“create a landing page…”) and forces aOS to learn intake, interview, and
evaluable-contract gates before dispatch/verify. Prefer discovering missing controls from that
loop (append to `LEARNINGS.md`) over speculative module growth. Thin slice already in tree:
`aos-mission` intake → propose-contract → authorize (`modules/mission_controls.py`).

- Implement `authz.py`, `worktree.py`, `verify.py`, `approve.py`, and the reusable manifest logic
  in `pin.py`.
- Use the external-effect protocol for worktree creation, blob publication, and verification.
  Reconciliation handles a branch, directory, process, blob, or evidence intent left incomplete at
  any injected crash point.
- Make verification evidence immutable and bind approval exactly as specified above.
- Build `bin/` operations for initialization/migration, draft creation, authorization relay,
  transition proposal, worktree creation, checkpoint append, verification, approval relay, retry,
  cancellation, reconciliation, projection, and divergence checking. Resident-session records are
  authored through normal file tools under distro instruction; they are not a constitutional
  command or gate.
- Implement recursive secret rejection across event payloads, session metadata, evidence metadata,
  terminal output, and exported eval artifacts.
- Implement the tree/journal divergence checker against fixture projections. It records one
  deduplicated violation per unchanged divergence. Actual State Tree/watch integration waits for
  M3.
- Keep harness launching outside the constitutional catalog. A harness adapter may run only after
  dispatch admission, cannot mutate canonical mission truth directly, and reports completion or
  failure against the admission ID. Add a `spawn` module only after a ratchet-rule failure.

Scenarios:

- `G-09`: dispatch/worktree/blob replay plus failure injection at every
  stage/admit/promote/complete boundary;
- `G-10`: missing, failed, stale, cross-attempt, post-verification-mutated, tampered, worker-origin,
  and replayed approval evidence, plus human rejection followed by process/attempt (never session)
  rework;
- `G-11`: authorization matrix, including worker approval/transition attempts;
- `G-12`: generated projection corruption detected and journaled once;
- `G-13`: nested secret/token leak attempts rejected across every output surface;
- `G-14`: every M2 tool command works from outside the checkout and obeys JSON/exit-code contracts.

**Exit evidence:** all agent-facing contested operations go through stable `bin/` operations; direct
module imports remain available to tests/internal composition; no old CLI is in v2 scenario paths;
rejection, crash, replay, stale revision, evidence binding, and secret scans all pass.

### Readable project state (M3)

A new liaison must be able to open the State Tree, render `watch`, follow distro skills for
reconstruction, and continue without the previous harness's memory. This is the operator-visible
aOS interface: files + instructions + journal-backed projections.

- Implement the mission/process/session State Tree above and deterministic byte-for-byte
  regeneration of its **generated projections** from journal + blobs while preserving authored
  files.
- Rewrite root `AGENTS.md` as the portable distro entry. Every harness resident in the aOS root
  follows liaison behavior and authors a unique non-exclusive session record before its first
  governed mission mutation. Other session records never act as locks. Write
  `protocols/constitution.md`, `protocols/mission.md`, and `protocols/work-order.md`.
- Write liaison/worker skills for session recording, drafting, routing, dispatching, supervising,
  retrying, verifying, approval relay, reconstruction, and watch. The reconstruction skill must
  teach the five continuation questions (*running / blocked / decided-why / verified /
  approvable*) from journal + State Tree only — never from harness chat history.
- Pin a sorted constitutional manifest of `AGENTS.md` + `protocols/` at root initialization and
  record its observed digest in liaison session provenance; record a
  separate informational distro version including `skills/`. Every gated operation validates the
  constitutional pin. Add/remove/rename/edit drift there quarantines privileged transitions and
  journals one deduplicated violation. Skills drift is recorded in run/session provenance and
  triggers distro evals, but does not become a new constitutional gate without a ratchet scenario.
  Re-pinning the constitution requires an explicit human-authorized distro upgrade naming the
  expected old and new manifests. During quarantine, fail-safe operations remain allowed:
  read/watch/check/export, authority revocation, cancellation, safe reconciliation,
  and authorized re-pin. Authority-expanding or progress-making operations are rejected.
- Implement `bin/watch` as a read-only renderer of journal + State Tree. It never consumes
  deliveries or writes canonical state. Reconciliation and violation recording remain explicit
  commands; no background process is implied by the word "continuous."
- Add a scripted fixture proving mechanics/projection correctness.
- Check in the M3 reference configuration using Codex CLI `0.144.1` with model
  `gpt-5.6-sol` through the env-auth eval adapter. Changing either is a reviewed plan/config
  change, never an implicit `latest` upgrade.
- Add the pinned headless reference-agent scenario, given only the distro entry and a mission
  objective inside a selected disposable aOS root, with a fake
  worker backend, and distinct scripted human-decision fixtures. The agent must navigate:
  `draft -> authorize -> dispatch -> implement -> verify -> awaiting approval -> decision ->
  deliver` without importing modules directly or invoking 0.1 control flow.
- Add a failed-work scenario where the agent chooses Session/Process/Attempt retry from distro
  guidance and the gate enforces the authorized ceiling.

**Exit evidence:** `D-01` deletes and regenerates generated State Tree files byte-for-byte while
preserving authored drafts/proposals and excluding the local worktree link; `D-02` completes the
headless end-to-end mission from distro files; `D-03` proves retry judgment is in agent trajectory,
not Python orchestration; `D-04` proves constitutional-pin drift quarantine, authorized re-pin, and
non-gating skills-drift provenance.

### Real workers (M4)

Prove the liaison supervises and does not do the work alone. Do not expand multi-harness worker
claims until readable project state (M3) exists and a new-agent continuation path (`H-01`-shaped)
is available. Judge rubrics must score briefing fidelity to journal/State Tree, not to
harness-private memory.

Separate native UI evidence from Harbor-supported agents:

- **Native hands-on trial:** become resident in a selected aOS root from Cursor's Agents Window
  (root `AGENTS.md` is sufficient; no exclusive activation or lease), then dispatch one Cursor
  worker and one Codex worker through harness-specific skills. Record journal/export evidence.
- **Harbor comparison:** run the same checked-in tasks as separate trials using Harbor's
  `cursor-cli` adapter and the env-auth Codex adapter. Do not describe this as an Agents Window
  test. Pin the Codex CLI binary version; record the detected Cursor CLI version without supplying
  a fake version override, and compare only compatible Cursor runs.
- Use harness adapters after dispatch admission; do not add a constitutional spawn module without
  a ratchet scenario.
- Implement rubric-judged mission quality: outcome quality, escalation appropriateness, briefing
  fidelity to journal state, and work-order membrane integrity. Judge credentials exist only in
  the separate verifier environment.
- Calibrate judge/rubric versions against a fixed human-labeled set before using thresholds.
  Preserve raw judge output, structured reasoning, and both the aOS journal trajectory and Harbor
  ATIF trajectory.
- Run mapped 0.1 scenarios: proactive attention, authority control, routing-policy honesty, false
  completion blocked by evidence, and no raw conversation leakage to workers.
- Run at least three trials per harness/task cell and report distributions, not a single winner.
  Record agent/model/judge versions and environment digests.
- Predeclare a degraded-distro experiment and its threshold before running it. For example, remove
  an escalation rule in a disposable variant and require a declared median rubric regression on a
  fixed escalation task.

CI lanes:

- **Offline deterministic:** unit tests, migration/replay, all governance checks, Oracle Harbor
  tasks, projection, privacy, and packaging; blocking on every change.
- **Model/distro:** pinned headless-agent and judge calibration tasks; required after
  behavior-changing `AGENTS.md`, `protocols/`, or `skills/` edits and before release.
- **Native hands-on trial:** Cursor Agents Window plus real Cursor/Codex workers; recorded release
  evidence, not misrepresented as deterministic CI.

**Exit evidence:** the hands-on trial and both Harbor agent adapters produce valid two-grade
reports; all governance passes; judge calibration agrees with the labeled set within its declared
tolerance; the predeclared degraded distro is caught at the declared threshold.

### Continue on a new machine (M5)

Package a portable aOS root so a fresh harness on another machine can continue. Prefer landing the
**new-agent continuation test** (`H-01`) as soon as readable project state (M3) exists; remaining
cutover and publish-gate work may complete afterward. Conversation state, harness memory, and
operational worktrees are excluded from the export — they are not durable aOS state.

#### Pre-cutover gate

- Produce a portable export through a staging-and-manifest protocol:
  1. checkpoint and back up SQLite, recording the backup's journal high-water event ID, schema
     version, and pinned distro revision;
  2. regenerate generated projections from that backup rather than copying live projections;
  3. copy exactly the content-addressed blobs referenced by the backup;
  4. copy authored State Tree files only after a before/after digest pass proves they did not
     change during capture, retrying or failing if they remain unstable;
  5. write and verify one bundle manifest covering every file digest, source event/high-water,
     schema, and distro/module revision, then atomically publish the staged bundle.

  The result contains a self-contained journal database, referenced content-addressed evidence,
  authored files plus backup-derived projections, and distro/module manifests. It excludes
  credentials, quarantined legacy bytes, caches, operational worktree symlinks/live worktrees, WAL
  dependencies, absolute-path requirements, and agent conversation state.
- Run the honest reconstruction task locally and on a clean Linux CI runner. The runner receives
  only a clean checkout at the pinned distro revision plus the export bundle; no previous process,
  harness session, or hidden state exists.
- `H-01` is a Harbor task using the checked-in pinned M3 Codex reference configuration while
  resident in a selected reconstructed aOS root. It verifies the bundle
  manifest before reading state. Oracle or another scripted agent may test fixtures but cannot
  satisfy the honest reconstruction claim.
- The fresh liaison session must answer *running / blocked / decided-why / verified / approvable*
  against verifier truth, author its own session record, and then perform a proposed transition
  through a journaled admit. Historical or apparently active session records must not delay or
  block reconstruction.
- Run all M1-M4 suites against a disposable migrated copy while 0.1 remains frozen and available
  for rollback.
- Rerun the unchanged 0.1 acceptance suite on a legacy root and build a pinned rollback artifact
  containing the 0.1 source revision, locked environment, and executable image digest.
- Before deleting 0.1, port every still-relevant characterization assertion to v2 tests. Mark
  legacy-only tests as archived migration fixtures so the post-cutover `pytest` suite imports no
  removed `aos.store`, `aos.control`, or `aos.worker` path.

#### Cutover change

- Back up and integrity-check the selected live 0.1 state root; stop all 0.1 writers; perform the
  one-way v2 migration; close/reconcile legacy authority and active runtime state as specified;
  verify the migration manifest; then require a fresh resident liaison session record before any
  mission work.
- Delete superseded 0.1 orchestration entrypoints and compatibility paths (`aos/cli.py` flow,
  `aos/control.py` orchestration, `aos/daemon.py`, old console scripts, auto-run editor tasks, and
  obsolete docs/rules). Keep only code genuinely absorbed by `modules/`.
- Update packaging and README so the repository exposes one substrate and describes level-1
  assurance plainly.
- Rerun reconstruction and the full publish gate after deletion:

```text
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
uv run python evals/run.py --all
```

`--all` runs every automatable offline, reference-agent, harness-comparison, and judge/calibration
lane and fails early when required provider/judge credentials are unavailable. Native Cursor
Agents Window hands-on trials are deliberately outside automatable CI; their checked
release-evidence manifest is a separate M5 prerequisite.

Before the first post-migration contested domain write, rollback may restore the
pre-migration backup and pinned 0.1 source/environment artifact. That is the point of no return.
After any v2 operational write is admitted, rollback to 0.1 is forbidden because it would discard
canonical history; recovery is forward-only unless a separately tested reverse migration is added.
0.1 and v2 never run against the same state root concurrently.

**Exit evidence:** `H-01` passes locally and on a clean CI machine before and after cutover; the
post-cutover tree contains no old entrypoint, import, editor task, rule, or runtime path; the
publish gate is green against the migrated state format.

## Requirement-to-evidence matrix

| Invariant | Owner | Required evidence |
| --- | --- | --- |
| Lossless, one-way v1/v2 migration | `journal` | `G-07`, row/hash/safe-artifact comparison, quarantine manifest, deterministic baseline revisions, repeat/fault/future-schema results, integrity reports |
| Maintenance cannot become mission authority | `journal` | empty-root/migration cases in `G-01`/`G-07`, denied privilege-crossing attempts |
| Every canonical transition is admitted or rejected durably | `journal` + `protocol` | `G-03`, `G-08`, admission/rejection IDs in `grades.json` |
| Delegated worker authority is scoped, explicitly renewed, expiring, and historically auditable | `lease` + `authz` | `G-02`, `G-05`, invoker/epoch/renewal/expiry query output |
| Resident liaison sessions are attributable, concurrent, and never exclusive | distro + `journal` | `G-01`, session records, stale-session tolerance, same-revision conflict |
| Retry identity and ceiling are enforced without retry orchestration in code | `protocol` + distro | `G-06`, `D-03`, journal and agent trajectory |
| External effects and blob publication are admit-first and idempotent | `worktree` + operation protocol | `G-09`, failure-point/replay/CAS manifests, no dangling database references |
| Delivery names current immutable passing evidence and attributed human decision | `verify` + `approve` | `G-10`, canonical worktree/evidence digests, invoker + `on_behalf_of`, stale/tamper failures |
| Generated files never outrank journal truth; authored files survive | projection + distro | `G-12`, `D-01`, generated-file digest plus authored-file preservation |
| Constitution drift quarantines transitions; skills drift stays observable | `pin` + distro | `D-04`, manifests, deduplicated violation, and skills provenance |
| Governance and mission grades remain separate | eval harness | `HBR-01`, schema-validated `reward.json` + `grades.json` |
| Secrets do not cross v2 state, output, child, migration, or eval boundaries | all modules + materializer | `G-13`, child-env tests, legacy quarantine cases, public-tree scan, allowlist manifest |
| Harness claims match what was actually tested | eval harness + trial record | M4 manifests naming Agents Window, `cursor-cli`, and the env-auth Codex adapter separately |
| A fresh machine reconstructs without liaison ownership | distro + all modules | New-agent continuation test (`H-01`) local/CI exports, new session record, answers, and admitted follow-up despite stale prior sessions |
| Project state is navigable without harness memory | distro + State Tree + `watch` | Readable project state (M3) reconstruction skill, agent scenarios `D-01`/`D-02`, five continuation answers from journal/tree only |
| Liaison supervises; workers execute inside a membrane | distro + adapters | Real workers (M4) hands-on trial + Harbor adapter lanes; work-order integrity rubric |
| Cutover closes legacy authority without losing a rollback boundary | migration + release process | reconciliation events, non-approvable legacy evidence, rollback artifact, point-of-no-return record |

## Risks held throughout implementation

| Risk | Mitigation |
| --- | --- |
| Level-1 gates are described as a sandbox | Explicit cooperative assurance language; direct journal tampering is out of scope until level 2 |
| A rejected gate disappears in rollback | Rejection commits before exit 1; `G-03` and injected-fault tests |
| Maintenance becomes a permanent bypass | Separate non-mission role, quiesced-root preconditions, typed events, privilege-crossing denials |
| Old history falsely fails new invariants | Migration boundary/snapshot; checks apply v2 envelope rules only afterward |
| Migration damages or silently re-authorizes live state | Actual v1/v2 fixtures, backup, staged blobs plus final transaction, epoch closure/reconciliation, integrity checks, no dual writers |
| Legacy artifacts are missing, secret-bearing, fabricated, or path-dependent | Scan + safe CAS ingestion, nonexportable quarantine, explicit missing records, paths retained as history only |
| SQLite, CAS, and an external effect diverge | Stage/admit/promote/complete protocol, idempotency keys, reconciliation, crash-point tests, no missing referenced blob |
| Secret-bearing operation is replayed after its raw token is lost | Same-token replay only; otherwise close/rotate epoch and never reconstruct a secret |
| Sparse modules grow into orchestration | Ratchet covers authority expansion; harness adapters cannot mutate canonical truth |
| Agent or task tampers with its verifier | Separate verifier, declared checkpointed export and ATIF artifacts |
| Harbor changes underneath the suite | Pin 0.20.0/schema 1.3/Python 3.12; `HBR-01`; record versions and result schema |
| Harbor build context leaks local data | Explicit content allowlist, content-addressed materialization, privacy scan, ignored run dirs |
| Provider auth is materialized by a third-party adapter | Env-auth eval adapter for Codex; env allowlists, teardown, artifact/leak checks |
| Judge scores are noisy or reward-hacked | Human-labeled calibration, named dimensions, repeated trials, preserved agent/verifier trajectories |
| Cursor UI and Cursor CLI evidence are conflated | Separate native hands-on trial and Harbor adapter lanes with exact version manifests |
| Tokens leak through nested metadata or activation | Source-only activation, environment-only reads, recursive validation and cross-surface leak tests |
| Generated State Tree becomes shadow truth | Source-event stamps, read-only generated files by convention, divergence checks, deterministic regeneration |
| Export combines inconsistent live files and journal state | Backup high-water, backup-derived projections, referenced-blob closure, authored-file stability pass, verified atomic bundle manifest |
| Rollback discards accepted v2 history | Pinned 0.1 artifact and backup only before first v2 operational write; forward-only afterward |

## Deferred, not lost

- **Level-2 physical enforcement:** a separate OS principal owns the journal and modules become the
  only writable surface. Domain events and protocols should not change.
- **Provider quotas and additional harness adapters:** add as nonconstitutional adapters and distro
  routing intent unless a ratchet scenario proves a new gate is needed.
- **Eval-engineering skill:** mine gate rejections and violation events for candidate scenarios
  after M4 produces real trajectories.

## Definition of done

Delete every agent session, kill every process, and give a clean checkout plus the portable export
to a fresh harness on a different machine. Using only the distro, journal, and State Tree, it can
determine what is running, blocked, decided and why, verified, and approvable. It authors its own
`LS-*` session record, and no canonical transition in the complete suite was applied without a
journaled admit. Harness-private memory is unnecessary for that continuation. The post-cutover
repository exposes one substrate, every requirement above has its named evidence, and all publish
gates are green.
