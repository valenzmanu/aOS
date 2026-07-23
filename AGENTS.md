# aOS — sparse agent OS

aOS uses agents as the reasoning runtime. Agents navigate a State Tree and follow distro
instructions; code exists only at deterministic constitutional joints such as durable admission,
authority, evidence, and approval.

Read `docs/adr/0002-sparse-constitutional-agent-os.md` and
`docs/aos-v2-implementation-plan.md` before changing architecture.

## Operating mode

- Repository work defaults to implementor mode. Work directly with normal tools.
- Liaison mode requires an explicit activation naming a selected state root. Discovering
  `state/`, `missions/`, or distro files never activates it.
- `modules/` owns deterministic constitutional behavior. It must not choose mission goals,
  sequence work, supervise agents, or become a Python orchestration layer.
- `evals/v0/` is the permanent first vertical tracer. Keep its agent-runtime property intact:
  after activation, the runner waits and grades; it does not drive the lifecycle.
- Tests: `uv run pytest`. Lint: `uv run ruff check .`.

## Privacy

- Never include secrets, credentials, unrelated local paths, private repository contents, raw
  conversation history, or state artifacts in commits or public issues.
- `state/`, `projects/`, and `missions/` can contain prompts, transcripts, source excerpts, and
  process metadata. Keep them local; they are gitignored.
- Never write bearer tokens to files, chat, commits, command arguments, logs, or rules.
- Before publishing, run `uv run python scripts/check_public_tree.py` and inspect staged changes.
