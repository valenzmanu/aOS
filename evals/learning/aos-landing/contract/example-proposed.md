# Proposed contract — aOS landing page (example)

This is an example refinement of the vague human request. A real run should
interview when any Acceptance item is still guesswork.

## Outcome

Deliver a static landing page for aOS under `aos-site/` that a visitor can open
without a build step.

## Acceptance

- [ ] `aos-site/index.html` exists and opens in a browser without bundling
- [ ] The product name `aOS` appears as the primary brand signal in the first viewport
- [ ] One short supporting sentence describes what aOS is
- [ ] One primary CTA is present
- [ ] No secrets, private state paths, or harness tokens appear in site files

## Constraints

- Worker implementation wall-clock budget: 15 minutes
- Liaison may choose harness/model but must record the choice under the attempt
- Visual direction must be intentional; avoid generic AI-purple template defaults
- Existing `tyr-site/` is out of scope unless the human explicitly asks to replace it

## Out of scope

- Full multi-page marketing site
- Backend, auth, or analytics
- Replacing the constitutional kernel

## Open questions

None.
