# aOS — sparse agent OS

Agents are the reasoning runtime. Files are their interface; the journal is canonical for
contested mission truth; code exists only at demonstrated constitutional joints.

Read `docs/adr/0002-sparse-constitutional-agent-os.md` and
`docs/aos-v2-implementation-plan.md` before changing architecture.

## Operating mode

- Every harness in this root is a non-exclusive resident liaison. Before its first governed
  mission mutation, it creates a unique attribution record under
  `state/liaison/sessions/LS-<session>/`; that record grants no authority and blocks nothing.
- Perform ordinary repository work directly. Do not turn source edits into governed missions.
- Keep goals, planning, routing, supervision, recovery, and retry judgment in agents—not
  `modules/`.
- Preserve `E2E-00`: after launching the agent, its runner waits and grades.
- Run `uv run pytest` and `uv run ruff check .`.

## Privacy

Keep `state/`, `projects/`, `missions/`, credentials, private source, transcripts, and unrelated
local paths out of commits and public issues. Never write bearer tokens to files, chat, arguments,
logs, or rules. Before publishing, inspect staged changes and run
`uv run python scripts/check_public_tree.py`.
