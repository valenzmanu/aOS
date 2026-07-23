---
title: aOS 2.0 implementation plan — SCAOS substrate
status: approved direction; tightened implementation plan; implementation not started
decision: docs/adr/0002-sparse-constitutional-agent-os.md
predecessor: docs/aos-poc-plan.md (0.1; goals retained, substrate replaced)
harbor_baseline: 0.20.0; task schema 1.3
---

# aOS 2.0 implementation plan

Read ADR 0002 first. It contains the architectural decision; this file fixes the implementation
contracts, build order, and evidence required to claim each milestone. The measurement layer is
built before the intent layer because prose has no compiler.

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
- invoker kind and ID, with authority/lease epoch ID and evaluated expiry when authority-bearing;
- optional `on_behalf_of` principal attribution and decision-source hash for relayed human actions;
- request/idempotency key;
- causation ID and admission ID, when applicable;
- canonical JSON payload with no secret-shaped data at any nesting depth.

Legitimate invokers include liaison, liaison candidate, worker, maintenance operator, module/system,
migration, reconciler, and eval fixture. Only authority-bearing invokers require an epoch. A system
reconciliation event and the journaled rejection of an expired mutation remain valid events.

Revision rules are exact:

- operation/gate/effect events belong to an operation aggregate and do not advance target-domain
  revisions;
- a rejection records expected and observed target revisions; every target remains unchanged;
- each admitted domain event advances its own aggregate exactly once, even when one operation emits
  admission, approval, transition, and effect events;
- an operation declares expected revisions for every existing contested aggregate it reads or
  writes; a new aggregate uses `absent`/revision 0;
- operation IDs are repository-wide unique and bound to canonical request hash plus invoker;
  reuse with different input or invoker is rejected.

For a database-only governed operation, one `BEGIN IMMEDIATE` transaction:

1. authenticates the capability and records its authority epoch;
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
| Human decision, relayed by active liaison | authorize a contract revision; approve or reject delivery; widen authority or retry ceiling; order cancellation | masquerade as a worker or omit decision provenance |
| Active liaison lease | create drafts; route and dispatch authorized work; choose retry level; signal processes; invoke verification; propose transitions | authorize its own contract; approve its own delivery |
| Liaison candidate, locally attributed but not authority-bearing | claim a liaison slot only when no unexpired epoch remains, or complete an explicitly authorized transfer | mutate mission state, migrate data, or bypass an unexpired liaison |
| Scoped worker capability | start/update its own process/session; append checkpoints; publish candidate artifacts | mutate mission state; widen scope; verify, approve, deliver, spawn, or delegate |
| Maintenance operator, locally attributed but not mission-authority-bearing | initialize an empty root; explicitly migrate a process-quiesced root with no live v2 authority epoch | perform mission work, approve delivery, or bypass a live writer/v2 epoch |
| Module/system, migration, or reconciler | append narrowly typed mechanical/recovery facts under a causative operation | claim human judgment or widen mission authority |
| Eval verifier | read exported state and trajectories; write verifier-only grades | mutate runtime journal or State Tree |

At level 1, a human principal is explicit **attribution**, relayed by the active liaison, not
cryptographically authenticated identity. The gate proves that a worker capability cannot approve
and records both the invoking liaison/epoch and who the liaison says made the decision. Strong
principal authentication is level-2 work.

Bootstrap is only the empty-root initialization case of maintenance. Initialization and every
later explicit schema migration require an operation ID, request hash, expected schema/root state,
and journaled maintenance/migration facts. They are admitted only on a quiesced root with no live
v2 authority epoch or v2 external-effect intent. A migration may import still-unexpired legacy
epoch rows only after OS/process checks prove all 0.1 writers are stopped; its final transaction
closes those imported epochs as part of cutover. Initializing a nonempty root is permanently
forbidden; later maintenance never acquires mission authority.

Every lease claim, including the first, uses the non-authority-bearing liaison-candidate role. Its
gate may only reconcile an expired epoch (or consume an explicit transfer authorization) and
atomically create the next liaison epoch when no unexpired epoch remains. Admission grants the new
liaison capability but never grants initialization, migration, or any pre-claim mission power.

V2 preserves rolling TTL behavior but makes it observable. Claim or rotation creates an authority
epoch with an absolute expiry. An authority-bearing mutation may explicitly request renewal; the
gate evaluates time through an injectable clock and appends `authority.renewed` in the same
transaction. Reads and unrelated commands never renew silently. Release, rotation, expiry
reconciliation, and cancellation append revocation/closure facts. No canonical mutation is
accepted under an expired epoch, although its rejection and subsequent system reconciliation are
journaled.

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

Except for shell activation, stdout is one stable JSON object containing `status`, `operation_id`,
relevant event/entity/revision fields, and `help[]`. Diagnostics go to stderr.

- exit 0: admitted and completed, or an idempotent replay of that result;
- exit 1: constitutional rejection, including its committed rejection event ID;
- exit 2: invalid invocation, unsupported schema, or operational/infrastructure failure.

Liaison claim is a source-only shell activation command used in a dedicated persistent shell. It
generates the raw token before admission, captures it through an anonymous non-rendered pipe into
`AOS_LIAISON_TOKEN`, and supplies it to the claim module through inherited environment; execution
instead of sourcing is refused and shell tracing is disabled. The module persists only the hash,
and the request fingerprint includes that hash. Other commands read the token only from the
environment.

Secret-bearing creation is the one replay exception: an uncertain claim/dispatch retry succeeds
only while the trusted wrapper still holds the same raw token and operation ID. A lost secret is
never reconstructed or returned from the journal; its epoch must expire or be explicitly
revoked/rotated. The trusted dispatch wrapper generates a worker token before admission, passes its
hash to the module, and injects the raw value directly into the adapter child environment. If the
wrapper crashes before handoff is confirmed, reconciliation closes that epoch and creates a new
one rather than recovering the secret. Tokens never pass through JSON or files. Tests scan the
database, terminal output, State Tree, artifacts, and Harbor results for leakage.

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

### M0 — Decision artifacts and implementation-mode guardrails

Content is complete but M0 is not done until the current uncommitted design and disarmament changes
are committed together.

- Commit ADR 0002, this plan, implementation-mode `AGENTS.md`, disarmed editor surfaces, and
  runtime/eval ignore rules. The public-tree scan treats `missions/` as private even if a future
  ignore regression makes it visible.
- Confirm no 0.1 liaison lease, watch process, sync, or dispatch is started by repository
  instructions.
- Run unit tests, lint, and the public-tree privacy scan before the commit.

**Exit evidence:** a clean commit containing only decision/guardrail files; `uv run pytest`,
`uv run ruff check .`, and `uv run python scripts/check_public_tree.py` green.

### M1 — Journal contract, compatibility, and Harbor spike

M1 is delivered as four independently green slices.

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
  - `G-01`: narrow empty-root maintenance, candidate first claim, singleton denial, explicit
    renewal/release, and vacant/expired takeover;
  - `G-02`: missing, wrong, cross-process, expired, and revoked authority with historical epochs;
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

### M2 — Constitutional mechanics, evidence, and toolbelt

- Implement `authz.py`, `worktree.py`, `verify.py`, `approve.py`, and the reusable manifest logic
  in `pin.py`.
- Use the external-effect protocol for worktree creation, blob publication, and verification.
  Reconciliation handles a branch, directory, process, blob, or evidence intent left incomplete at
  any injected crash point.
- Make verification evidence immutable and bind approval exactly as specified above.
- Build `bin/` operations for initialization/migration, shell lease activation, draft creation,
  authorization relay, transition proposal, worktree creation, checkpoint append, verification,
  approval relay, retry, cancellation, reconciliation, projection, and divergence checking.
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

### M3 — State Tree, pinned distro, and intent evaluation

- Implement the mission/process/session State Tree above and deterministic byte-for-byte
  regeneration of its **generated projections** from journal + blobs while preserving authored
  files.
- Rewrite root `AGENTS.md` as a dual-mode portable distro entry. Repository-development requests
  remain implementor mode and never claim a lease. Liaison mode requires explicit user/runtime
  activation naming a selected state root; discovering state files alone never activates it.
  Write `protocols/constitution.md`, `protocols/mission.md`, and `protocols/work-order.md`.
- Write liaison/worker skills for claiming, drafting, routing, dispatching, supervising, retrying,
  verifying, approval relay, reconstruction, and watch.
- Pin a sorted constitutional manifest of `AGENTS.md` + `protocols/` at lease claim; record a
  separate informational distro version including `skills/`. Every gated operation validates the
  constitutional pin. Add/remove/rename/edit drift there quarantines privileged transitions and
  journals one deduplicated violation. Skills drift is recorded in run/session provenance and
  triggers distro evals, but does not become a new constitutional gate without a ratchet scenario.
  Re-pinning the constitution requires an explicit human-authorized distro upgrade naming the
  expected old and new manifests. During quarantine, fail-safe operations remain allowed:
  read/watch/check/export, lease release, authority revocation, cancellation, safe reconciliation,
  and authorized re-pin. Authority-expanding or progress-making operations are rejected.
- Implement `bin/watch` as a read-only renderer of journal + State Tree. It never consumes
  deliveries or writes canonical state. Reconciliation and violation recording remain explicit
  commands; no background process is implied by the word "continuous."
- Add a scripted fixture proving mechanics/projection correctness.
- Check in the M3 reference configuration using Codex CLI `0.144.1` with model
  `gpt-5.6-sol` through the env-auth eval adapter. Changing either is a reviewed plan/config
  change, never an implicit `latest` upgrade.
- Add the pinned headless reference-agent scenario, given only the distro entry and a mission
  objective, with explicit liaison-mode activation, a selected disposable state root, a fake
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

### M4 — Real-harness dogfood and judge calibration

Separate native UI evidence from Harbor-supported agents:

- **Native dogfood:** explicitly activate the distro's liaison mode for a selected state root in
  Cursor's Agents Window, then dispatch one Cursor worker and one Codex worker through
  harness-specific skills. Record journal/export evidence.
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
- **Native dogfood:** Cursor Agents Window plus real Cursor/Codex workers; recorded release
  evidence, not misrepresented as deterministic CI.

**Exit evidence:** real dogfood and both Harbor agent adapters produce valid two-grade reports; all
governance passes; judge calibration agrees with the labeled set within its declared tolerance; the
predeclared degraded distro is caught at the declared threshold.

### M5 — Reconstruction, writer cutover, and 0.1 removal

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
- `H-01` is a Harbor task using the checked-in pinned M3 Codex reference configuration, explicit
  liaison-mode activation, and a selected reconstructed state root. It verifies the bundle
  manifest before reading state. Oracle or another scripted agent may test fixtures but cannot
  satisfy the honest reconstruction claim.
- The fresh liaison must answer *running / blocked / decided-why / verified / approvable* against
  verifier truth. It first reconstructs read-only while the exported prior liaison lease remains
  active and proves a second claim is rejected. After expiry through an injected clock or an
  explicit authorized transfer, it claims a new epoch and only then performs a proposed transition
  through a journaled admit.
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
  verify the migration manifest; then require a fresh v2 liaison claim before any mission work.
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
Agents Window dogfood is deliberately outside automatable CI; its checked release-evidence manifest
is a separate M5 prerequisite.

Before the first v2 liaison claim or post-migration contested domain write, rollback may restore the
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
| Maintenance and lease candidacy cannot become mission authority | `journal` + `lease` | empty-root/migration/first-and-later-claim cases in `G-01`/`G-07`, denied privilege-crossing attempts |
| Every canonical transition is admitted or rejected durably | `journal` + `protocol` | `G-03`, `G-08`, admission/rejection IDs in `grades.json` |
| Authority is scoped, explicitly renewed, expiring, and historically auditable | `lease` + `authz` | `G-01`, `G-02`, `G-05`, invoker/epoch/renewal/expiry query output |
| Retry identity and ceiling are enforced without retry orchestration in code | `protocol` + distro | `G-06`, `D-03`, journal and agent trajectory |
| External effects and blob publication are admit-first and idempotent | `worktree` + operation protocol | `G-09`, failure-point/replay/CAS manifests, no dangling database references |
| Delivery names current immutable passing evidence and attributed human decision | `verify` + `approve` | `G-10`, canonical worktree/evidence digests, invoker + `on_behalf_of`, stale/tamper failures |
| Generated files never outrank journal truth; authored files survive | projection + distro | `G-12`, `D-01`, generated-file digest plus authored-file preservation |
| Constitution drift quarantines transitions; skills drift stays observable | `pin` + distro | `D-04`, manifests, deduplicated violation, and skills provenance |
| Governance and mission grades remain separate | eval harness | `HBR-01`, schema-validated `reward.json` + `grades.json` |
| Secrets do not cross v2 state, output, child, migration, or eval boundaries | all modules + materializer | `G-13`, child-env tests, legacy quarantine cases, public-tree scan, allowlist manifest |
| Harness claims match what was actually tested | eval harness + dogfood record | M4 manifests naming Agents Window, `cursor-cli`, and the env-auth Codex adapter separately |
| A fresh machine reconstructs and transfers lease safely | distro + all modules | `H-01` local/CI exports, rejected concurrent claim, new epoch, answers, and admitted follow-up |
| Cutover closes legacy authority without losing a rollback boundary | migration + release process | reconciliation events, non-approvable legacy evidence, rollback artifact, point-of-no-return record |

## Risks held throughout implementation

| Risk | Mitigation |
| --- | --- |
| Level-1 gates are described as a sandbox | Explicit cooperative assurance language; direct journal tampering is out of scope until level 2 |
| A rejected gate disappears in rollback | Rejection commits before exit 1; `G-03` and injected-fault tests |
| Maintenance or claim candidacy becomes a permanent bypass | Separate non-mission roles, quiesced/vacant-root preconditions, typed events, privilege-crossing denials |
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
| Cursor UI and Cursor CLI evidence are conflated | Separate native dogfood and Harbor adapter lanes with exact version manifests |
| Tokens leak through nested metadata or activation | Source-only activation, environment-only reads, recursive validation and cross-surface leak tests |
| Generated State Tree becomes shadow truth | Source-event stamps, read-only generated files by convention, divergence checks, deterministic regeneration |
| Export combines inconsistent live files and journal state | Backup high-water, backup-derived projections, referenced-blob closure, authored-file stability pass, verified atomic bundle manifest |
| Rollback discards accepted v2 history | Pinned 0.1 artifact and backup only before first v2 operational write; forward-only afterward |

## Deferred, not lost

- **Multi-session liaison (ADR 0001):** revisit only if dogfood shows singleton lease exclusivity is
  the blocker. It would become Harness Sessions as State Tree objects plus delivery gates.
- **Level-2 physical enforcement:** a separate OS principal owns the journal and modules become the
  only writable surface. Domain events and protocols should not change.
- **Provider quotas and additional harness adapters:** add as nonconstitutional adapters and distro
  routing intent unless a ratchet scenario proves a new gate is needed.
- **Eval-engineering skill:** mine gate rejections and violation events for candidate scenarios
  after M4 produces real trajectories.

## Definition of done

Delete every agent session, kill every process, and give a clean checkout plus the portable export
to a fresh harness on a different machine. It can determine what is running, blocked, decided and
why, verified, and approvable. It can continue through the distro, and no canonical transition in
the complete suite was applied without a journaled admit. The post-cutover repository exposes one
substrate, every requirement above has its named evidence, and all publish gates are green.
