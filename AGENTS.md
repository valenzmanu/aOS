# aOS liaison protocol

This repository uses aOS as its agent control plane. A harness session may act as the liaison only
while it holds the singleton lease.

## Start of every liaison session

1. Run `uv run aos init` if state is not initialized.
2. Run `uv run aos liaison claim --harness <harness-name>`.
3. Keep the returned token only in `AOS_LIAISON_TOKEN`; never write it to a file, chat, commit,
   command argument, log, rule, or Work Order.
4. Start one long-running `uv run aos watch` terminal. Reuse it rather than opening duplicates.
5. Run `uv run aos liaison sync --since <cursor>` before each user-facing turn.

## Delegation boundary

- All mission work starts with `aos mission create`, explicit authorization, then `aos dispatch`.
- Do not use harness-native subagents, Cursor multitask, Codex collaboration tools, or undeclared
  background agents for mission work. This is a cooperative PoC guardrail, not a sandbox boundary.
- Workers may edit only their assigned worktree and may not authorize, approve, deliver, merge,
  push, spawn another agent, or control another mission.
- The liaison must not report completion until aOS records verifier evidence and the mission is
  `awaiting_approval`. Only the human can authorize and approve.

## Privacy

- Never include secrets, credentials, unrelated local paths, private repository contents, raw
  conversation history, or state artifacts in commits or public issues.
- `state/` can contain prompts, transcripts, source excerpts, and process metadata. Keep it local.
- Promote only the objective and explicitly approved constraints into a Work Order.
- Before publishing, run `uv run python scripts/check_public_tree.py` and inspect staged changes.
