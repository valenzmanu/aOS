---
status: accepted
date: 2026-07-22
decision_owners: aOS maintainers
supersedes-direction-of: docs/aos-poc-plan.md (substrate only; goals retained)
related: ADR 0001 (unmerged analysis; kept as analysis, not roadmap)
---

# Sparse Constitutional Agent OS (SCAOS)

aOS 2.0 is rebuilt on a different substrate. The 0.1 implementation was a correct prototype of
the wrong architecture: it embodied the OS as a Python control plane (`aos/*.py`) that agents call
through a CLI. This ADR replaces that substrate with agents navigating a file tree, guided by a
portable instruction distro, with determinism confined to sparse modules at governance-critical
joints. The governance theory of 0.1 — authority and mission truth outside any harness context,
verification before approval, scoped worker authority — is retained unchanged.

## Thesis

> **aOS is a journal wearing a file tree as its interface.** Agents are the runtime: they navigate
> mission state as files, guided by a portable distro of protocols and skills. Determinism exists
> only at the constitutional joints — a small, ratcheted set of modules that agents invoke for
> reliable mechanics or that gate governance-critical transitions. Canonical truth for contested
> state lives behind the modules, not in the files.

One-line form: **agents are the runtime, files are the interface, the journal is the truth,
determinism only at the joints.**

## Why the direction changed

Three independent sources converged on the same conclusion during review of 0.1:

1. **firstmate's "agent distro" repositioning and the code-as-intent observation**
   ([tweet](https://x.com/kunchenguid/status/2078264987857150010)): a broken bash script did not
   break firstmate — the agent read the intent and worked around it. What you write for agents is
   no longer code ("here's what you do"); it is intent ("here's what I want"). Intent does not
   crash; its failure modes are *misses* (misarticulation, misunderstanding of demand). firstmate
   is therefore not an app but a distro: a portable directory of instructions, skills, tooling,
   policies, and state conventions that turns a general-purpose agent into a specialized one.
2. **Interpretable Context Methodology** ([arXiv:2603.16021](https://arxiv.org/abs/2603.16021),
   Van Clief & McDermott): filesystem structure replaces framework orchestration. Folder hierarchy
   is stage sequencing, context scoping, state management, and coordination. "The filesystem is
   doing the work that a framework would otherwise do in code."
3. **Google's Open Knowledge Format**: knowledge for agents as a portable directory of markdown
   with minimal frontmatter conventions — the same primitive shift applied to knowledge packs.

The shared insight: **programming languages are the wrong primitive for an OS whose CPU is models
and whose processes are harness sessions.** 0.1's Python kernel sat beside the agent ecosystem
instead of inside it; every privileged action crossed a process/language boundary, and the "OS"
was a durable ledger around a chat rather than a world agents inhabit.

But pure intent cannot govern. An OS whose goal is to *govern and manage* agents needs
deterministic components exactly where an agent freestyling would corrupt authority, isolation,
evidence, or protocol — and nowhere else.

## The four layers

| Layer | Substrate | Nature |
| --- | --- | --- |
| **Journal** | SQLite for v2, behind the `journal`/`protocol`/`lease` modules | Deterministic and transactionally atomic through the canonical module API. Append-only is API-enforced at assurance level 1. The only canonical store for contested state: transitions, authority, evidence, leases. Replacing SQLite requires a new ADR. |
| **State Tree** | Files: `missions/`, contracts, work orders, checkpoints, artifacts, projections | The navigable interface. Authored files (drafts, rationale, briefings) are editable and authoritative as files. Generated snapshots/projections are journal-derived and editing them creates divergence rather than canonical truth. |
| **Distro** | `AGENTS.md`, `protocols/`, `skills/` | Portable intent. Instantiates a general agent as liaison, worker, or verifier. Constitution files are hash-pinned in the journal so drift is detectable. |
| **Agents** | Any harness: Cursor, Codex, CLI, future | The runtime. Non-deterministic by design. Replaceable without loss of state. |

## Two attachment modes

Deterministic modules attach to the system in exactly two ways:

- **Invoke** — an agent calls a module for mechanics it should not freestyle: worktree creation,
  journal append, verification runs.
- **Gate** — a module admits or rejects a proposed transition: authorize, dispatch, approve,
  retry-level, lease claim. Rejections are journaled.

Decision procedure for every feature:

```text
What can go wrong if an agent freestyles this?
  → nothing governance-critical     → State Tree + Distro only
  → needs reliable mechanics        → Module (invoke)
  → must be rejected on the
    canonical transition path       → Module (gate) on the transition
  → both                            → invoke for mechanics + gate for admit
```

Rule of thumb: if a failure mode is "agent ignored the rule", it belongs in a deterministic
module. If it is "bad articulation / bad judgment", it belongs in the intent layer.

## Staged enforcement (honest gates)

- **Level 1 (cooperative, now):** gates enforce the canonical API, not the underlying filesystem
  against a hostile local process. Unauthorized tree edits do not become canonical transitions
  and are detected when a gate, tool command, or reconciliation pass compares tree and journal,
  provided the journal remains intact. Direct journal tampering is outside level-1 assurance.
  Detection is opportunistic when a tool runs; there is no claim of continuous enforcement.
- **Level 2 (physical, later):** gates become process/permission boundaries — state owned by a
  separate OS user, modules as the only writable surface. The domain protocol does not change
  between levels; only the wall gets harder.

aOS must never present level-1 guarantees as level-2. This was already 0.1's honest posture; it
is now normative.

## The ratchet rule

A new deterministic module, a new gate, or an expansion of an existing module's authority may be
added **only** with a demonstrated freestyle failure — an incident, a failed dogfood scenario, or
an acceptance test proving agents cannot be trusted with that transition. Workflow convenience is
never sufficient grounds. Pure utilities, harness adapters, renderers, and eval checks do not
become constitutional merely because they are code. This is the constitutional amendment process;
it is deliberately annoying. Without it, "sparse" erodes back into a framework.

## Constitutional module catalog (v0)

| Module | Attach | Guarantees |
| --- | --- | --- |
| `lease` | invoke + gate | singleton liaison; scoped, expiring process authority |
| `journal` | invoke | append-only event log; atomic append + projection update |
| `protocol` | gate | legal mission/process FSM transitions only |
| `authz` | gate | capability checks on privileged transitions |
| `worktree` | invoke | one isolated git worktree per attempt |
| `verify` | invoke | run verification command; record evidence artifact + exit status |
| `approve` | gate | current evidence + explicit human attribution at level 1 + expected revision |
| `pin` | invoke + gate | constitution/protocol file hashes journaled; drift detected |

Everything else — routing judgment, supervision, compaction, briefing, when to interrupt the
human, harness specifics — is Agent + Distro + files.

## Eval doctrine (how intent is debugged)

Adopted from applied-AI evaluation practice
([source](https://x.com/eyad_khrais/status/2074519552277336571)): an eval gives an agent a task
and grades **outcome** and **trajectory** separately; deterministic checks catch safety
violations, a judge model with a rubric scores quality; the grades are never blended.

Applied to aOS:

- **The journal is the trajectory.** Every governed action already leaves an ordered event trace.
  Eval checks are queries over `events`.
- **Two grades per scenario, reported separately:**
  - *Governance grade* — deterministic checks against the journal (no `delivered` without
    `approval.decided` naming evidence; no canonical worker mutation admitted under an expired
    authority epoch, while its rejection and subsequent system reconciliation remain journalable;
    no dispatch without an admit). Pass/fail; gates everything.
  - *Mission grade* — did the work get done well. Deterministic fixture grading is allowed for
    scripted early scenarios; real-agent quality is judge-graded, never agent-asserted.
- **Evals are the only regression detector for the intent layer.** Prose has no compiler. Every
  change to `AGENTS.md`, a skill, or a protocol file must rerun the scenario suite. A distro
  change that tanks the governance grade is a bug even though nothing crashed.
- **A judge model with a rubric grades liaison judgment** (escalation appropriateness, briefing
  fidelity to journal state, work-order membrane integrity). This is the tooling for *misses*.
- The same journal checks run on gated operations and explicit reconciliation/eval passes as the
  level-1 violation detector — one substrate serving recovery, audit, gating, and evaluation.

## What aOS aims to do

1. **One human, one conversation, many workers.** Any harness can be the liaison; none *is* the
   liaison.
2. **Truth survives everything.** Compaction, window closure, harness switch, daemon death,
   vendor outage — state, authority, and evidence reconstruct from journal and tree.
3. **Authority is placed, not assumed.** Humans authorize and approve; the liaison proposes and
   supervises; workers execute inside a membrane (worktree + work order).
4. **Delivery requires evidence.** Claims are not evidence.
5. **Legible at every altitude.** `ls` a mission and understand it; watch renders the fleet; the
   journal explains every decision with provenance.
6. **Misses are first-class.** Dogfood scenarios, journaled gate rejections, and hash-pins are the
   stack traces of the intent layer.
7. **Portable by cloning.** The distro is the install.

## Non-goals

- Being a framework: no module ever owns the orchestration loop.
- Being all-markdown: contested state is atomic and journaled, not prose.
- Enforcing beyond the assurance level: cooperative guardrails are labeled as such.
- Managing model context windows: aOS curates artifacts; harnesses manage their own windows.
- Competing on model quality: aOS governs work; it does not do the work.
- ADR 0001's internal actor/mailbox/context engine in Python: kept as analysis of the multi-session
  problem; not implemented on this substrate decision.

## The test that keeps it honest

Delete every agent session, kill every process, and hand the directory to a fresh harness on a
different machine. If the new liaison can answer — *what is running, what is blocked, what was
decided and why, what is verified, what may be approved* — and no canonical transition was applied
without a journaled admit, aOS is working. Anything in the codebase that does not serve that test
is suspect.

## Migration posture from 0.1

0.1 is not discarded; it is re-homed:

- **Port:** event ordering, lease atomicity, scoped token hashing, durable identities/hashes,
  verifier-failure blocking, retry identities, and worktree isolation (`aos/store.py`, parts of
  `aos/control.py`) → constitutional modules. Replace caller-supplied transition edges with a
  protocol-owned closed graph; strengthen checkpoint-shaped evidence into immutable evidence-bound
  approval.
- **Demote to distro intent:** orchestration flow, routing judgment, retry policy, briefing logic
  (most of `aos/control.py`, `aos/cli.py` prose behavior, `AGENTS.md` protocol).
- **Replace:** CLI-as-privileged-API → thin toolbelt invoking modules; daemon supervision →
  liaison supervision guided by protocols, with module-enforced leases.
- **Delete only after** the eval suite passes against the new substrate.
