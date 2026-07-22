# Changelog

All notable changes to aOS are documented here. The project follows semantic versioning after the
initial alpha series.

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
