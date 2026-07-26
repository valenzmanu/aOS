# Changelog

All notable changes to aOS are documented here. The project follows semantic versioning after the
initial alpha series.

## Unreleased

- replaced exclusive liaison leases with resident, non-exclusive harness sessions
- made `AGENTS.md` residence sufficient for liaison behavior without activation or takeover
- attributed liaison operations to `LS-*` session IDs while retaining revision and idempotency
  gates
- allowed identical governed operations to replay safely across resident liaison sessions
- retained scoped, expiring capability epochs only for delegated worker/process authority
- added replay-safe retirement of historical liaison epochs and concurrency/orphan-tolerance tests
- cut the permanent `E2E-00` tracer over from liaison tokens and claims to authored session records
- reduced the required-read implementation plan to the active outcome, invariants, recovery proof,
  and definition of done; completed and speculative machinery remains in Git history
- added thin `aos-mission` controls learned from a disposable mission
  (intake → evaluable contract → authorize → dispatch → start → finish)
- removed the disposable landing-page output and mission dossier after preserving their reusable
  authority, evidence, privacy, replay, and recovery lessons
- removed the landing-specific HTML/brand verifier from constitutional code; a general evidence
  interface must be justified by the recovery eval
- made the default pytest command include the eval harness tests instead of maintaining a hidden
  second test invocation

## 0.2.0 — 2026-07-23

- adopted the sparse constitutional agent-OS architecture
- made agents the reasoning runtime and the State Tree their interface
- added the schema-4 journal, closed protocol vocabulary, and scoped liaison authority
- added deterministic legacy migration and provenance fixtures
- added the permanent fresh-agent `E2E-00` tracer and read-only governance grader
- removed the superseded Python control plane, daemon, worker adapters, dashboard, demo, and
  orchestration entrypoints

## 0.1.0 — 2026-07-21

Initial public alpha:

- durable SQLite journal and projections with schema migration
- singleton hashed liaison lease and scoped, expiring worker authority
- immutable hashed Contract revisions and versioned Work Orders
- explicit Mission, Attempt, Agent Process, and Worker Session records
- isolated Git worktrees, independent command verification, and human approval gate
- deterministic fake backend plus experimental Cursor CLI/SDK and Codex CLI transports
- streaming provider artifacts, durable backend handles and usage metadata
- provider capability discovery, confidence-tagged health observations, and routing fallback
- explicit Worker Session, Agent Process, and Attempt retries with non-convergence protection
- liaison-authorized pause, resume, cancel, and recovery-time authority revocation
- persistent liaison inbox cursor, reconstructable briefing, and read-only flight board
- Cursor/Codex cooperative delegation rules, privacy scanner, example, tests, and public CI

Known limitations: Cursor cloud agents, automatic subscription quota accounting, vendor-session
resume, Codex Goals, and hard enforcement against harness-native subagents remain roadmap items.
