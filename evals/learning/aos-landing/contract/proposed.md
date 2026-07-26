# Proposed contract — aOS landing page

Refines the raw human request in `human-request.md` using interview answers in
`intake/answers.md`.

## Outcome

Evolve `tyr-site/` into a single-page landing experience branded **aOS**, with a primary CTA to
the GitHub repository, deliverable within a 15-minute worker wall-clock budget.

## Acceptance

- [ ] `tyr-site/index.html` remains a single static page (no build step required to open)
- [ ] First viewport primary brand text is `aOS` (not `TYR` as the hero brand)
- [ ] Page includes one short supporting sentence about what aOS is
- [ ] Primary CTA is an anchor/button linking to `https://github.com/valenzmanu/aOS`
- [ ] No secrets, private `state/` / `missions/` paths, or harness tokens appear under `tyr-site/`
- [ ] Worker records chosen harness/model and elapsed wall-clock under the attempt notes

## Constraints

- Evolve `tyr-site/` in place; do not add a parallel `aos-site/` tree for this mission
- Single page only
- Worker implementation wall-clock budget: 15 minutes
- Liaison does not implement the page after authorization; a worker does
- Visual direction is open; prefer distinctive aesthetics over generic AI-purple templates
- Time budget and harness/model choice are liaison/worker judgment recorded in attempt notes;
  they are not binary governance failures unless the worker never records them

## Out of scope

- Multi-page marketing site
- Backend, auth, analytics, or CMS
- Renaming the GitHub repository
- Changing constitutional `modules/` behavior beyond what this mission’s controls require

## Open questions

None.
