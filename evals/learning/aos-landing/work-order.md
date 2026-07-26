# Work order — M-LANDING / attempt 0001

Evolve the existing `tyr-site/` directory in place into a single-page landing experience branded
only **aOS**. Work as the dispatched implementation worker; do not change mission controls,
contracts, tests, or repository architecture.

## Admitted route

- Harness: T3 Code / Codex
- Model: gpt-5.6-sol
- Worker wall-clock budget: 900 seconds
- Workspace: `tyr-site/` in the existing repository working tree
- Isolation level: cooperative path and work-order membrane; no physical worktree is claimed

## Required outcome

Keep the current strong editorial-industrial visual language where useful—obsidian, warm bone,
vermillion, expressive serif type, grids, grain, and technical labels—but remove the Norse
identity. Replace the old monument/character motif with a product-native visual of the journal,
State Tree, distro, agents, gates, or evidence flow.

The page must:

1. remain a static site that opens directly from `tyr-site/index.html`, with exactly one HTML page
   and no build manifest;
2. expose the first-view primary brand as exact visible text `aOS` on an element carrying
   `data-primary-brand`;
3. include one short visible sentence describing aOS on an element carrying
   `data-product-sentence`;
4. expose exactly one primary CTA carrying `data-primary-cta`, linking exactly to
   `https://github.com/valenzmanu/aOS`;
5. contain no remaining `TYR` branding in site bytes, metadata, accessibility text, copy, local
   asset references, or documentation;
6. contain no credentials, token-like strings, or private runtime paths.

Preserve semantic landmarks, a single `h1`, strong focus treatment, responsive layouts, and
`prefers-reduced-motion` handling. Keep normal-size text at WCAG AA contrast.

## Completion record

When the page is complete, update the sibling attempt `notes.md` so it contains these exact fields:

```text
- Harness: t3-code
- Model: gpt-5.6-sol
- Elapsed-seconds: <non-negative integer>
- Outcome: completed
```

Then write a sibling `receipt.json` containing only:

```json
{
  "attempt_id": "A-M-LANDING-0001",
  "authority_epoch_id": "AE-M-LANDING-0001",
  "mission_id": "M-LANDING",
  "process_id": "P-M-LANDING-0001",
  "schema_version": 1,
  "status": "completed",
  "work_order_id": "WO-M-LANDING-0001",
  "worker_session_id": "S-M-LANDING-0001"
}
```

Do not authorize, verify, approve, publish, or commit. Return control after the files and completion
record are ready.
