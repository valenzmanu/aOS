# aOS — sparse agent OS

aOS uses agents as the reasoning runtime. Agents navigate a State Tree and follow distro
instructions; code exists only at deterministic constitutional joints such as durable admission,
authority, evidence, and approval.

Read `docs/adr/0002-sparse-constitutional-agent-os.md` and
`docs/aos-v2-implementation-plan.md` before changing architecture.

## Operating mode

- Every harness operating in this aOS root is a resident liaison participant. There is no
  exclusive liaison process, lease, claim, activation ceremony, expiry, transfer, or takeover.
- Before the first governed mission mutation, choose a unique `LS-<session>` identifier and create
  an authored record under `state/liaison/sessions/<session-id>/`. Record the harness, current
  focus, and useful narrative checkpoints. Session records provide attribution and coordination
  only: they grant no authority, establish no canonical mission truth, and never block another
  session.
- Perform repository implementation work directly with normal tools. Resident liaison behavior
  does not require turning every source change into a mission or governed transition.
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
