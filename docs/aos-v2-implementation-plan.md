---
title: aOS implementation plan
status: active
decision: docs/adr/0002-sparse-constitutional-agent-os.md
---

# aOS implementation plan

This is the active roadmap. Historical plans and completed migration details remain in Git.

## Outcome

aOS helps one human direct a workforce of agents toward high-quality outcomes without
micromanaging every session.

The system succeeds when a human can state an outcome, budget, and boundaries; remain in an
interactive conversation by default; delegate longer work when it improves quality or speed; and
spend attention only on decisions, risks, and approvals that actually need a human.

Agent conversations, processes, and machines may disappear. A fresh agent must still be able to
recover the mission, continue safely, verify the result, and explain what needs human attention.

## Priorities

In order:

1. Maximize useful, high-quality work completed for the human.
2. Let the human manage outcomes rather than individual agent sessions.
3. Minimize time to a quality result. Keep the main thread interactive; use parallel or
   long-running workers when the work benefits from them.
4. Preserve human control while treating human attention as expensive. Surface critical facts;
   do not require the human to follow routine execution.

## The smallest system that can work

- **Agents reason and act.** They interpret goals, plan, route, supervise, diagnose, recover, and
  decide how to retry.
- **The State Tree is the readable interface.** It contains only the durable material another
  human or agent needs to understand and continue work.
- **The journal is canonical for contested facts.** It records admissions, authority, revisions,
  evidence, and human decisions atomically.
- **Constitutional modules prevent specific demonstrated failures.** They do not choose goals,
  sequence work, monitor workers, or implement an orchestration loop.
- **Humans authorize mission scope, budget, and boundaries and approve delivery.** Agents decide
  routine execution within them.

The current stack remains Python, SQLite, pytest, Ruff, and Harbor where model evaluation is
actually required. A clone remains the installation unit.

## Invariants

These protect the outcome and are not simplification targets:

- Mission truth survives outside harness-private memory.
- Every contested mutation is attributable, revision-checked, and replay-safe; admissions and
  rejections both leave durable journal facts.
- Resident liaison sessions are concurrent attribution records, never ownership or locks.
- Worker authority is scoped, expiring, and unable to authorize scope or approve delivery.
- Delivery requires current immutable passing evidence bound to the authorized scope and exact
  candidate, plus an explicit human decision.
- Secrets and private mission state never enter source, public artifacts, command arguments, or
  recorded output.
- Level-1 assurance governs cooperative actors through the canonical module interface; it is not
  described as a hostile-process sandbox.
- The permanent `E2E-00` tracer remains agent-driven: after launch, its runner waits and grades.

## Simplicity rules

Every file, instruction, state, and module must earn its place.

1. **Delete before abstracting.** If deleting something removes complexity without moving an
   essential invariant into multiple callers, delete it.
2. **Prove before mechanizing.** Add or expand a constitutional module only after a learning
   mission or eval demonstrates a governance failure that capable agents cannot reliably avoid.
3. **Keep judgment with agents.** Diagnosis, routing, supervision, recovery, retry choice,
   briefing, and escalation are intent, not deterministic workflow code.
4. **Keep one operational path.** A tracer may exercise the product interface; it must not grow a
   parallel runtime.
5. **Keep active instructions current.** Completed plans and disposable artifacts live in Git
   history, not in the required context of every future agent.
6. **Prefer observed state over speculative infrastructure.** Do not add a scheduler, background
   monitor, effect ledger, automatic retry policy, provider-specific orchestrator, or elaborate
   projection until an eval proves the simpler agent-led path inadequate.
7. **Optimize human attention.** Routine progress stays agent-managed. The human sees decisions,
   material risk, budget pressure, non-convergence, and approval-ready evidence.

## Current foundation

Landed:

- schema-4 journal, protocol, and scoped worker-authority joints;
- concurrent resident liaison attribution;
- the permanent agent-driven `E2E-00` mission tracer;
- a learning mission that exercised intake, authorization, dispatch, worker completion, and
  verification;
- a same-work recovery joint that atomically closes a stalled worker session and authority, resets
  its process from `running` to `created`, and creates a fresh session and authority epoch without
  changing the attempt, process, work order, or worktree;
- removal of the legacy Python orchestration runtime.

The learning mission exposed reusable failures:

- completion cannot be trusted after worker authority expires;
- receipts must bind the attempt, process, session, work order, and authority epoch;
- verifier output must not echo attacker-controlled values or private paths;
- verification replay must use recorded immutable evidence rather than mutable files;
- Git subprocesses need an explicit isolated environment;
- a stalled worker needs an honest close-and-replace path, not automatic orchestration.

The landing page and its mission dossier were disposable discovery artifacts. Their product output
is not part of aOS.

## Proved: end-to-end agent-led recovery

Integration coverage proves the constitutional handoff: the old session becomes orphaned, its
authority closes before fresh authority is claimed, the same process restarts, and only the
replacement receipt may finish the work. The joint does not detect stalls, choose recovery,
launch workers, monitor progress, or retry; those remain agent behavior.

`E2E-RECOVERY-01` additionally proves the agent path:

1. Dispatch a worker with a bounded work order and scoped authority.
2. Stop it after a partial edit.
3. Start a fresh resident liaison with only the repository, State Tree, and journal.
4. Require that agent to reconstruct state, replace the worker, preserve and finish the work, and
   record completion.

The run passes governance and mission-quality grading with no overlapping authority. It stops
honestly at `verifying`: general task-owned evidence admission remains the next missing joint.

## Then

1. **Readable mission state.** A fresh agent can answer: what is running, what is blocked, what
   was decided and why, what is verified, and what may be approved.
2. **Real workers.** Exercise interactive and long-running work through at least two harness
   adapters without moving routing or supervision into Python.
3. **Portable continuation.** Give a clean clone plus exported durable state to a fresh harness on
   another machine and continue without prior conversation state.

Each step starts as an agent eval. Deterministic machinery is the result of demonstrated failure,
not a prerequisite invented in advance.

## Validation

Repository changes must keep these green:

```text
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

Behavior-changing distro instructions also rerun the relevant agent scenarios. Governance and
mission quality remain separate grades: governance can block delivery; it cannot inflate quality.

## Definition of done

Discard every harness conversation and stop every process. Give a clean clone plus durable mission
state to a fresh harness on another machine. It can determine what is running, blocked, decided
and why, verified, and approvable; continue the mission through agents; and apply no canonical
contested transition without a journaled admission while preserving durable rejections.

The human can manage several such missions by outcome, sees the few things that truly need human
attention, and receives verified high-quality work quickly. Anything that does not materially
serve that test should be removed.
