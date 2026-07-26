# Required sections for an evaluable proposed contract

A proposed contract must be Markdown and contain these level-2 headings, in any order:

```markdown
## Outcome
## Acceptance
## Constraints
## Out of scope
## Open questions
```

Rules enforced by `aos-mission propose-contract` / `authorize`:

- Every required heading must appear exactly once as a `## ` line.
- **Acceptance** must include at least one concrete checkbox `- [ ]` or `- [x]` item that a
  verifier could later check without tasting aesthetics.
- **Open questions** must be either the single word `None` / `None.`, or a checklist where every
  item is checked `- [x]`. Unchecked `- [ ]` items block proposal and authorization.
- If `intake/open-questions.md` exists under the mission and contains unchecked `- [ ]` items,
  authorization is rejected until the liaison resolves them with the human.
