# Learnings — aOS landing mission

Append-only notes from iterations. Prefer concrete misses over slogans.

## 2026-07-26 — slice 0

- Human requests stay vague on purpose; mission controls must preserve the raw request and gate
  authorization on an evaluable contract.
- Interview belongs in liaison/distro behavior (State Tree questions + answers), not a new
  exclusive lease or activation ceremony.
- First constitutional slice: `intake → propose-contract → authorize` with resident `LS-*`
  sessions. Dispatch/worker/verify deferred until this loop has been run for real.
- Prefer the phrase **learning mission** over industry slang when referring to this package.

## 2026-07-26 — interview closed

Human answers:

- evolve `tyr-site/` (not a new `aos-site/`)
- single page
- CTA → GitHub
- brand aOS only
- visuals open

Contract proposed and journaled on disposable root
`evals/learning/aos-landing/.run/` (repo `state/` was an older schema and was not reused).
Awaiting explicit human authorize.

## 2026-07-26 — authorized mission executed

- The handoff's implementation request supplied the human authorization needed to admit the exact
  proposed contract. The authorized snapshot retained the proposed contract hash unchanged.
- The smallest useful post-authorization control chain was enough for this mission:
  `dispatch → start → finish → verify`. Dispatch records an attempt, process, work order, worker
  session, and expiring authority epoch; it does not choose the mission or supervise the worker.
- The existing working tree is a cooperative membrane, not physical isolation. The constitutional
  boundary comes from a narrow work order, scoped authority, identity-bound receipts, immutable
  events, and deterministic verification. Stronger filesystem isolation remains a future option.
- A worker receipt created before its authority deadline can be relayed by a resident liaison after
  the deadline. The expired epoch forbids new worker mutations; it should not erase timely evidence
  that already exists.
- The verifier needed an explicit child-process environment and disabled host Git configuration.
  Ambient Git includes are otherwise enough to make a deterministic repository check depend on
  the operator's machine.
- The first worker stalled after making partial changes. A replacement worker completed the bounded
  work in 98 seconds, but the control plane has no explicit launch-intent reconciliation, failure
  receipt, or retry lineage yet. That is the clearest next constitutional control before unattended
  execution is trusted.
- Historical hidden deployment metadata contained secret-bearing material. It was moved into local,
  gitignored quarantine before verification; no raw secret material was copied into mission state,
  source, notes, or events.
- The verification report is recorded inline with the evidence event as well as written as a local
  projection. That keeps the six acceptance results regenerable from the State Tree rather than
  relying only on an adjacent mutable file.
- All six acceptance checks passed. The mission stops at `awaiting_approval`; deterministic evidence
  does not substitute for the human's later approve-or-reject decision.

## 2026-07-26 — hardening correction and fresh run

- **Correction:** the earlier claim that a receipt can be relayed after worker-authority expiry was
  not supportable. The receipt had no independently trustworthy completion time, so a post-expiry
  relay was indistinguishable from a post-expiry forgery. `finish` now rejects at or after expiry,
  and the exact receipt schema binds both the authority epoch and work order.
- The pre-hardening disposable run was moved intact into local gitignored quarantine. A fresh
  mission run then repeated intake, proposal, authorization, dispatch, start, external worker
  execution, finish, and verification under the corrected controls.
- The fresh worker completed its audit and receipt in 99 seconds. It found one implementation miss:
  vermillion microcopy on light panels did not meet AA contrast. A darker light-surface accent fixed
  the measured ratios before completion.
- A hostile symlink could previously disclose its target through the manifest, and a symlinked site
  root could bypass a check after path resolution. The verifier now rejects root and nested
  symlinks without resolving or recording their targets.
- Failed verification reports previously echoed attacker-controlled brand text, links, and paths.
  Inline evidence now contains only counts, booleans, lengths, and digests; raw site values and
  manifest paths never enter the journal.
- Verification replay now loads the canonical inline report and reconstructs missing State Tree
  projections without reading mutable live site files. Reusing the completed operation ID returns
  the recorded result without adding events.
- The public-tree scanner now tolerates a tracked file that has been deleted from the working tree,
  while continuing to fail on other read errors and prohibited publishable paths.
- The hardened fresh run passed all six acceptance checks and is `awaiting_approval`. Human review
  remains a separate constitutional decision.
