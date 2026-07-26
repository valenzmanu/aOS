# Learning mission — aOS landing page

This mission **drives discovery of mission controls**. It is intentionally started from a vague
human request. The liaison's job is to turn that request into something agents can execute and
evaluate — interviewing the human when needed.

It is not the permanent first end-to-end trial (`E2E-00`). Learnings here should feed
[`docs/aos-v2-implementation-plan.md`](../../../docs/aos-v2-implementation-plan.md) under
**Mission controls**.

## Raw human request

See [`human-request.md`](human-request.md). Preserve it verbatim in mission intake. Do not
overwrite it with the refined contract.

## Intended loop

```text
human request (vague)
  → liaison intake (durable draft)
  → interview human if not evaluable yet
  → propose contract (outcome + acceptance + constraints)
  → human authorizes contract
  → dispatch admitted work order + cooperative worker membrane
  → start → external worker executes → finish with bound receipt
  → pinned deterministic verify
  → human approve/reject later
```

## Grades (keep separate)

- **Governance:** intake preserved raw request; authorize rejected while open questions remain;
  authorize admitted only with an evaluable proposed contract and human decision; no dispatch
  before authorization; worker execution stays outside modules; verification records immutable
  passing or failing evidence.
- **Mission / judgment:** aesthetic quality, harness/model choice, and use of the 15-minute budget.
  These remain separate from binary governance checks.

## Commands

```sh
uv run aos-mission init --op OP-DF-INIT
uv run aos-mission intake M-LANDING \
  --request evals/learning/aos-landing/human-request.md \
  --op OP-DF-INTAKE
# liaison interviews / edits State Tree files under missions/M-LANDING/
uv run aos-mission propose-contract M-LANDING \
  --contract path/to/proposed.md \
  --op OP-DF-PROPOSE --expect 1
uv run aos-mission authorize M-LANDING \
  --decision path/to/authorize.json \
  --op OP-DF-AUTHORIZE --expect 1
export AOS_WORKER_TOKEN="<ephemeral worker capability supplied by the trusted wrapper>"
uv run aos-mission dispatch M-LANDING \
  --work-order evals/learning/aos-landing/work-order.md \
  --backend t3-code --model gpt-5.6-sol \
  --workspace repo-root/tyr-site --budget-seconds 900 \
  --op OP-DF-DISPATCH --expect 2
unset AOS_WORKER_TOKEN
uv run aos-mission start M-LANDING --op OP-DF-START --expect 3
# a harness worker executes the admitted work order outside modules
uv run aos-mission finish M-LANDING \
  --receipt path/to/attempt/receipt.json \
  --op OP-DF-FINISH --expect 4
uv run aos-mission verify M-LANDING \
  --site-root tyr-site --op OP-DF-VERIFY --expect 5
uv run aos-mission status M-LANDING
```

`--expect` is the current mission revision. Proposal does not bump it; authorization still uses the
post-intake revision (`1` after a fresh intake).

Set `AOS_STATE_ROOT`, `AOS_MISSIONS_ROOT`, and `AOS_LIAISON_SESSION_ID`. Dispatch reads raw worker
capability material only from `AOS_WORKER_TOKEN`; never put it in an argument, file, log, or
checked-in shell example. The trusted wrapper should generate it immediately before dispatch and
inject it directly into the worker adapter when one exists.

`finish` must be admitted while the dispatched worker authority is still active. Its exact receipt
binds the attempt, process, worker session, work order, and authority epoch. The landing verifier
rejects all site symlinks and records only non-sensitive aggregate results in canonical evidence.
Replaying a completed verify operation reconstructs projections from that recorded report; it does
not reread mutable site files.

## Learnings

Append durable discoveries to [`LEARNINGS.md`](LEARNINGS.md) after each iteration.
