# Intake protocol — vague human request to evaluable contract

Humans often speak in goals, taste, and budgets — not acceptance criteria. Preserve that. Your job
is to make the work executable and evaluable without inventing authority.

## Steps

1. **Intake** the raw request with `aos-mission intake`. Never edit
   `intake/human-request.md` after intake.
2. **Assess evaluability.** Can a worker and a verifier succeed or fail clearly?
   - If no: write `intake/open-questions.md` with unchecked boxes and interview the human.
   - If yes: skip to proposing a contract.
3. **Interview** in ordinary conversation. Record answers by checking boxes and appending short
   notes under `intake/answers.md`. Do not treat session files as locks.
4. **Propose a contract** that satisfies `contract/REQUIRED_SECTIONS.md` in this learning-mission
   package.
   Put judgment (aesthetics, model choice) in Constraints or Acceptance only when it can be
   checked or explicitly deferred to a later judged grade.
5. **Authorize** only after the human decision file exists. Do not self-authorize.
6. **Dispatch and start** only the admitted work order. Let the external worker create the exact
   bound receipt, then admit `finish` before the worker authority expires.
7. **Verify** with the pinned deterministic verifier. Stop at `awaiting_approval` or `failed`; the
   verifier does not make the human approval decision.

## What not to do

- Do not implement the landing page while acting as liaison.
- Do not overwrite the raw human request with the refined contract.
- Do not dispatch before an evaluable contract and explicit human authorization are admitted.
- After authorization, use the authored learning-mission work order and the separate
  dispatch/start/finish/verify commands. Worker launching and execution remain outside modules.
