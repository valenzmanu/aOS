# aOS architecture

This guide is the self-contained operational map of aOS for humans and agents. It explains what
persists, what may disappear, who owns each lifecycle, and how aOS behaves when a harness enters
through a Git worktree. The accepted constitutional decision remains
[ADR 0002](adr/0002-sparse-constitutional-agent-os.md); the
[implementation plan](aos-v2-implementation-plan.md) records current proof and next work.

## The system in one sentence

Agents do the reasoning, files present durable state, a SQLite journal decides contested mission
truth, and a small deterministic kernel protects only the transitions that agents must not
improvise.

An agent conversation, process, checkout, or machine may disappear. The durable mission must still
be understandable and safely continuable by a fresh agent.

## Core vocabulary

| Term | Meaning |
| --- | --- |
| **Installation** | One aOS Git clone, its primary checkout, its linked worktrees, and its private runtime roots. |
| **Primary checkout** | The checkout that owns the clone's Git common directory and canonical private runtime. |
| **Worktree** | Another checkout linked to the same clone. It is an execution location, not a second aOS installation. |
| **Harness** | The agent host, such as Codex or Cursor. |
| **Resident liaison** | Any harness session operating from the aOS distro and coordinating mission work. It is not an exclusive daemon or owner. |
| **Worker** | An agent process executing a bounded work order with scoped, expiring authority. |
| **Mission** | The durable unit of governed human intent, contract, attempts, work, evidence, and approval. |
| **State Tree** | Human- and agent-readable files that explain current mission state and preserve rationale. |
| **Journal** | The SQLite-backed canonical record for contested events, revisions, authority, evidence, and approvals. |
| **Constitutional joint** | Deterministic code that performs reliable mechanics or admits/rejects a protected mutation. |

## Topology

```text
Human
├── desktop chat
├── phone Voice through Codex Remote
└── another supported harness
         │
         ▼
Resident liaison session
├── may run in the primary checkout
└── may run in a disposable harness-managed worktree
         │
         ▼
Runtime-path resolver
         │
         ├── primary/state/       canonical journal and installation state
         ├── primary/missions/    readable durable mission material
         └── primary/projects/    private aOS-managed project/worker area
                  │
                  ▼
          bounded worker processes
          through selected harness adapters
```

The important identity rule is:

> One clone has one private aOS runtime, regardless of how many linked worktrees enter it.

Worktrees may contain different source revisions and temporary execution files. They must not
contain competing copies of canonical mission state.

## The four substrates

### Agents

Agents interpret goals, plan, route, supervise, diagnose, recover, and execute. These activities
require judgment and remain outside deterministic orchestration code.

A resident liaison is non-exclusive. Before its first governed mutation, it creates a unique
`LS-*` attribution record under the resolved `state_root`. That record grants no authority, holds
no lock, blocks no other liaison, and need not remain alive for the mission to continue.

Workers receive bounded work orders and scoped authority epochs. A worker cannot authorize its own
scope or approve delivery.

### Distro

The checked-in `AGENTS.md`, protocols, and skills teach a general harness how to participate in
aOS. Residence supplies behavior; it does not bypass revision checks, authority, evidence, or human
approval.

Project-level work retains the project's own instruction hierarchy. aOS instructions are not
copied into unrelated projects or installed globally merely to create a bridge.

### State Tree

The State Tree is the readable interface. It contains the contracts, attempts, work orders,
sessions, checkpoints, evidence, decisions, and generated views that a fresh human or agent needs
to understand and continue a mission.

Authored files preserve rationale. Generated views can be reconstructed from the journal. A file
edit alone does not make a contested transition canonical.

### Journal

The journal is canonical for contested facts. Transactions, expected revisions, operation IDs,
request hashes, and immutable evidence bindings make cooperative concurrent mutations safe and
replayable.

Both an admitted mutation and its rejection leave durable facts. Human authorization and approval
remain explicit attributed decisions.

## Why deterministic code is sparse

Deterministic modules exist only where free-form agent behavior could corrupt admission, authority,
evidence, approval, or durable mission truth:

```text
Can an agent improvise this without corrupting a protected fact?
├── yes                         → agent + distro + files
├── no; reliable mechanics     → invoke a deterministic module
├── no; mutation needs policy  → gate the mutation
└── both                       → invoke mechanics and gate admission
```

Routing, monitoring, recovery judgment, retry choice, and orchestration remain agent behavior
unless a demonstrated failure earns a new constitutional joint.

## Installation filesystem

The primary checkout owns both tracked source and ignored private runtime:

```text
<installation_root>/
├── AGENTS.md                  tracked distro entry point
├── modules/                   deterministic constitutional joints
├── protocols/                 portable agent behavior
├── distro/skills/             thin harness discovery and launch adapters
├── evals/                     scenario proofs
├── docs/                      architecture, plans, and evidence
├── state/                     ignored; journal and installation state
├── missions/                  ignored; durable mission State Trees
└── projects/                  ignored; private aOS-managed project/worker area
```

`state/`, `missions/`, and `projects/` may contain prompts, transcripts, private source excerpts,
and capability material. They never belong in commits, public issues, or public evidence.

## Runtime-path resolution

Normal commands discover one installation-wide runtime:

1. If both explicit state and mission roots are provided, aOS uses that isolated pair. This exists
   for evals and recovery tooling.
2. A partial explicit pair is rejected because it could combine unrelated histories.
3. Otherwise aOS asks Git for the current top-level checkout and the clone's common Git directory.
4. The parent of the common Git directory must identify a valid aOS primary checkout.
5. `state/`, `missions/`, and `projects/` resolve under that primary checkout.
6. A linked worktree containing non-empty shadow runtime roots is rejected. aOS never guesses,
   merges, or deletes a potentially divergent journal.

Inspect the resolved paths from the primary checkout or a compatible linked worktree:

```bash
uv run aos-mission paths
```

An older branch that does not contain the resolver must invoke the current installation:

```bash
uv --directory <installation_root> run aos-mission paths
```

## Worktree classes and ownership

The phrase “aOS worktree” can hide two different lifecycles. Keep them distinct.

| Worktree class | Typical location | Creator and lifecycle owner | Purpose |
| --- | --- | --- | --- |
| **Harness-managed chat worktree** | Codex defaults to `$CODEX_HOME/worktrees/<id>/aOS` | The harness, such as Codex | Isolate one chat or background task from the local checkout. |
| **aOS-managed project or worker worktree** | `<installation_root>/projects/...` | The resident liaison following the mission protocol | Hold project execution associated with an admitted attempt or work order. |

The resolver works directly only from the primary aOS checkout or a worktree linked to that aOS
clone. A project or worker worktree belonging to another repository invokes mission commands
through the primary installation:

```bash
uv --directory <installation_root> run aos-mission ...
```

The resolver does not create, retain, or delete either worktree class.

### Harness-managed chat worktrees

Codex-managed worktrees are disposable execution locations. Codex creates them, associates them
with chats, and applies its own retention policy. This lifecycle ownership is the architectural
contract; the exact cleanup triggers and recovery behavior are non-normative product details that
may change. Consult the current [Codex worktree
documentation](https://learn.chatgpt.com/docs/environments/git-worktrees) before relying on them.

aOS must not add a competing cleanup mechanism. It cannot know whether a chat is active, pinned,
restorable, or still owns valuable uncommitted source changes.

### aOS-managed project or worker worktrees

These paths are mission execution facts and follow the admitted mission, attempt, process, and
work-order lifecycle. They are not Codex chat-retention artifacts. Agents must preserve partial work
during recovery and may remove a worktree only through an explicit, separately justified lifecycle
decision. PR-style chat cleanup does not apply to them.

## Phone Voice lifecycle

Voice is a control surface, not a source of canonical mission truth. A phone connects through
Codex Remote to a host that owns the repository, credentials, permissions, tools, and files.
Voice can start or coordinate a Codex task; that task may run locally or in a Codex-managed
worktree.

The intended lifecycle is:

```text
1. Human starts Voice on the phone.
2. Voice starts or steers a Codex task on the connected host.
3. Codex may create a disposable linked worktree under its managed worktree root.
4. The task reads the aOS distro from that checkout.
5. `aos-mission` resolves the primary installation's shared runtime.
6. Governed results are journaled and rendered under the primary runtime roots.
7. Valuable source changes are committed, pushed, proposed as a PR, or handed off.
8. The completed child chat is archived.
9. Codex snapshots and removes its disposable worktree.
```

Voice does not itself guarantee that every task uses a worktree, and aOS does not depend on that.
Local and linked-worktree entry must produce the same runtime identity.

This design applies within one clone on one connected host. A cloud environment, another machine,
or a separate clone does not share Git's common directory. Cross-machine continuation requires an
explicit durable-state export/import design; worktree discovery does not provide it.

## What survives worktree deletion

Deletion safety comes from placing durable truth outside the disposable checkout, not from keeping
every worktree forever.

| Material | Durable location or action | Expected after a harness worktree is deleted |
| --- | --- | --- |
| Journal events and revisions | Primary `state/` | Preserved |
| Mission State Tree | Primary `missions/` | Preserved |
| aOS-managed project/worker material | Primary `projects/` | Preserved |
| Chat transcript | Harness chat history | Preserved according to harness policy |
| Managed worktree snapshot | Harness storage | Recovery convenience, not aOS canonical truth |
| Source changes | Commit, push, PR, or handoff | Preserved only when explicitly retained |
| Dependencies, caches, temporary files | Disposable worktree | May be deleted |
| Ignored checkout-local files | Disposable worktree | Not a durable interface; do not rely on them |

Before archiving a chat that changed source, preserve those changes in Git or hand the worktree off
to a durable local workflow. Do not treat a harness snapshot as the authoritative archive.

## Mission and recovery lifecycle

The permanent tracer proves this mission trajectory:

```text
draft → authorized → dispatched → implementing → verifying
      → awaiting_approval → delivered
```

Humans authorize mission scope, budget, and boundaries and approve delivery. Agents handle routine
execution and surface only consequential decisions, material risks, budget pressure,
non-convergence, and approval-ready evidence.

If a worker stalls, an agent diagnoses the condition and chooses whether to recover. The
constitutional recovery joint can atomically close the old session and authority, reset the same
process, and create a replacement session and authority epoch without changing the attempt,
process, work order, or worktree. aOS does not run a background monitor or automatic retry loop.

## Fresh-agent recovery test

The architecture is working when all conversations and processes can be discarded and a fresh
agent, using only the distro and durable state, can answer:

- What is running?
- What is blocked?
- What was decided, and why?
- What has current immutable passing evidence?
- What requires human attention or approval?
- What authority exists, and what has expired?
- What safe action continues the mission?

The journal supplies canonical trajectory; the State Tree supplies readable context. Harness memory
and worktree survival are useful but non-canonical.

## Safety and privacy boundaries

- Cooperative assurance governs actors that use the canonical module interface. aOS is not a
  hostile-process sandbox.
- Never expose `state/`, `missions/`, `projects/`, credentials, private source, or transcripts in
  commits, public issues, logs, or public evidence.
- Never run forced ignored-file cleanup such as `git clean -fdx` against the primary installation;
  the ignored roots are canonical private data.
- Never automatically merge or delete shadow runtime roots found in a linked worktree. Stop and
  reconcile them manually.
- Worker and process capabilities are bearer credentials. Keep them out of files, chat, command
  arguments, logs, and rules.
- Filesystem access does not prove instruction loading. Before a governed mutation from a new
  editor or multi-root workspace, verify that the liaison received the current primary aOS
  instructions and can read the project instruction hierarchy.

## Operational checklist

For a new resident liaison:

1. Read the root `AGENTS.md` and this guide.
2. Run `uv run aos-mission paths`.
3. Confirm that a linked worktree resolves the primary installation's private roots.
4. Create a unique liaison attribution record before the first governed mutation.
5. Read the mission State Tree and current journal-derived status.
6. Read routing and delegation protocols before dispatching workers.
7. Preserve source changes before archiving a disposable harness chat.
8. Leave harness worktree cleanup to the harness; leave mission-worktree cleanup to an explicit
   mission lifecycle decision.

For repository changes:

```bash
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

## Non-goals

aOS does not:

- own a scheduler, orchestration loop, or background supervisor;
- decide mission goals or select work without agent judgment;
- preserve every harness conversation or disposable execution file;
- make a worktree, branch, chat, process, or machine canonical;
- manage Codex's chat worktree retention;
- merge divergent runtime histories automatically;
- claim security against a hostile local process; or
- provide cross-clone state synchronization merely through Git worktree discovery.
