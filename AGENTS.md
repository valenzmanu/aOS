# aOS — implementation mode (0.1 liaison protocol suspended)

> **The 0.1 liaison protocol that previously lived in this file is suspended.** Do NOT claim a
> liaison lease, do NOT start `uv run aos watch`, do NOT run `aos liaison sync`, and do NOT
> dispatch missions through the 0.1 CLI. The Python control plane under `aos/` is legacy code
> being ported, not the operating system of this repository.

## Why

aOS is being rebuilt on a new substrate (agents navigating a file tree, with determinism only at
constitutional joints). The decision and rationale are in
`docs/adr/0002-sparse-constitutional-agent-os.md`; the build order is in
`docs/aos-v2-implementation-plan.md`. Read both before changing anything.

The 0.1 protocol told every agent session to become a liaison and operate the old OS. During the
rebuild that behavior would interfere with implementation work, so it is disarmed here and in
`.cursor/rules/aos-liaison.mdc`, `.cursor/commands/aos-watch.md`, and `.vscode/tasks.json`. A new
dual-mode distro entry replaces this file at milestone M3 of the plan: repository-development
requests remain in implementor mode, while liaison mode requires explicit activation for a named
state root. Merely finding state files will never activate liaison mode.

## Working in this repository now

- You are an implementor, not a liaison. Work directly with normal tools: edit, test, commit.
- Treat `aos/` (0.1 Python) as characterization material. Preserve its proven lease atomicity,
  token hashing, event ordering, durable identities/hashes, retry identities, worktree isolation,
  and verifier-failure blocking; replace its caller-supplied FSM edges and evidence-free approval
  as specified in the plan. Do not extend 0.1 surfaces.
- Keep 0.1 runnable but frozen through M5's pre-cutover reconstruction gate. Delete its old
  entrypoints and orchestration surfaces only in the M5 cutover change, then rerun the full suite.
- Tests: `uv run pytest`. Lint: `uv run ruff check .`.

## Privacy (still in force — substrate-independent)

- Never include secrets, credentials, unrelated local paths, private repository contents, raw
  conversation history, or state artifacts in commits or public issues.
- `state/`, `projects/`, and (once created) `missions/` can contain prompts, transcripts, source
  excerpts, and process metadata. Keep them local; they are gitignored.
- Never write bearer tokens to files, chat, commits, command arguments, logs, or rules.
- Before publishing, run `uv run python scripts/check_public_tree.py` and inspect staged changes.
