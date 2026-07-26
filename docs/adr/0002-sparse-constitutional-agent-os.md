---
status: accepted
date: 2026-07-22
decision_owners: aOS maintainers
supersedes-direction-of: historical 0.1 plan at e920ff2:docs/aos-poc-plan.md
---

# Sparse Constitutional Agent OS

## Decision

> Agents are the runtime, files are the interface, the journal is the truth, and deterministic
> code exists only at constitutional joints.

aOS preserves durable, governed mission work outside any agent conversation while leaving
reasoning and execution to agents.

The earlier architecture embodied aOS as a Python control plane that orchestrated agent work. That
was the wrong primitive for a system whose reasoning runtime is a model and whose processes are
harness sessions. aOS is instead a world agents inhabit: a portable instruction distro, a readable
State Tree, and a canonical journal.

Pure intent is insufficient where improvisation could corrupt admission, authority, evidence, or
approval. Those narrow facts remain deterministic and transactionally enforced.

## Substrates

| Substrate | Role |
| --- | --- |
| **Agents** | Interpret goals, plan, route, supervise, diagnose, recover, and execute. They are replaceable and intentionally non-deterministic. |
| **Distro** | Portable `AGENTS.md`, protocols, and skills that turn a general harness into an aOS participant. |
| **State Tree** | Inspectable mission, contract, work-order, session, checkpoint, and evidence files. Authored files preserve rationale; generated files are journal-derived views. |
| **Journal** | SQLite-backed canonical truth for contested events, revisions, admission, authority, evidence, and approval. |

The clone is the installation unit. aOS preserves mission state; it does not replace harness memory
or copy private conversation history.

## Resident liaison behavior

Every harness operating in an aOS root may act as a resident liaison. There is no exclusive
liaison process, lease, claim, activation ceremony, expiry, transfer, or takeover.

Before its first governed mission mutation, a harness authors a unique `LS-*` session record for
attribution and useful narrative checkpoints. A session record grants no authority, establishes no
canonical mission truth, blocks nothing, and need not be closed.

Expected revisions reject stale conflicting mutations. Operation IDs and canonical request hashes
make effects replay-safe. A different resident session may replay an identical operation after a
crash; the journal retains the original admitting session as provenance.

Human authorization and approval remain explicit attributed decisions. Workers receive scoped,
expiring authority. At cooperative assurance, human identity is recorded provenance rather than
cryptographic authentication.

## Constitutional joints

Deterministic modules attach in two ways:

- **Invoke:** an agent requests reliable mechanics it should not reproduce ad hoc.
- **Gate:** a module admits or rejects a proposed contested mutation on the canonical path. Both
  outcomes leave durable journal facts.

Use this decision:

```text
Could an agent freestyle this without corrupting admission, authority,
evidence, approval, or durable mission truth?
  yes → agent + distro + files
  no, reliable mechanics suffice → invoke
  no, the mutation must be rejected → gate
  both → invoke mechanics and gate admission
```

Routing, supervision, recovery judgment, retry choice, briefing, escalation, compaction, harness
selection, and mission-quality evaluation remain agent behavior unless a concrete failure proves
that one of the protected facts cannot remain sound.

### Ratchet

A new deterministic module, gate, or expansion of deterministic authority requires a demonstrated
freestyle failure: an incident, learning mission, or acceptance test. Workflow convenience and
speculative completeness are insufficient.

Apply the deletion test continuously. If deleting a module removes complexity without forcing an
essential invariant into several callers, the module did not earn its place.

Constitutional behavior is named by the invariant it protects, not by a predetermined file
catalog. Splitting one deep module into several shallow files is not architectural progress.

## Assurance

Current level-1 assurance governs cooperative actors through the canonical module interface.
Unauthorized file edits do not become canonical transitions and can be detected when journal and
State Tree are compared. aOS does not prevent a hostile local process from modifying SQLite or the
filesystem directly, and it does not claim continuous enforcement.

A future level may place journal ownership and module execution behind operating-system
permissions. That hardens the wall without changing the mission protocol.

## Evaluation

Intent has no compiler, so scenarios test both trajectory and outcome:

- **Governance:** deterministic pass/fail over the journal. A failure invalidates the eval or
  release claim; runtime delivery is gated by current evidence and human approval.
- **Mission quality:** whether the human received a good result. It is graded separately and
  cannot be inflated by governance success.

The journal is the durable trajectory. A distro change that preserves process execution but causes
authority, evidence, or attention failures is still a regression.

Evals are not constitutional merely because their checks are code. Scenario-specific rubrics and
artifact checks belong with the eval; only demonstrated governance failures may ratchet into
modules.

## Desired properties

1. One human can direct many concurrent liaison sessions and workers.
2. Mission truth survives compaction, closed windows, stopped processes, harness changes, vendor
   outages, and machine changes.
3. Authority is placed rather than assumed: humans authorize and approve; agents supervise;
   workers execute within scoped work orders.
4. Claims do not substitute for current immutable passing evidence bound to the authorized scope
   and exact candidate.
5. A human or fresh agent can see what is running, blocked, decided, verified, and approvable.
6. Routine work stays agent-managed; scarce human attention goes to consequential decisions,
   material risk, budget pressure, non-convergence, and approval.
7. The distro and durable state are portable by cloning and export.

## Non-goals

- Owning an orchestration loop, scheduler, or background supervisor.
- Choosing mission goals or competing on model quality.
- Encoding all workflow in Python or all contested truth in Markdown.
- Managing harness context windows or acting as a general personal-memory system.
- Claiming stronger enforcement than the current assurance level.
- Preserving disposable learning artifacts or historical implementation machinery in the active
  interface.

## Honest test

Discard every harness conversation, stop every process, and hand a clean clone plus durable
mission state to a fresh harness on another machine.

If it can determine what is running, blocked, decided and why, verified, and approvable; continue
through the distro; and no canonical contested transition was applied without a journaled
admission while every rejection remained durable, aOS is working. Anything that does not
materially serve that test is suspect.
