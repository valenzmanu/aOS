# Contributing to aOS

Thanks for helping build a transparent agent operating substrate.

## Development setup

```bash
git clone https://github.com/valenzmanu/aOS.git
cd aOS
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

Use synthetic objectives and disposable state roots for tests. Do not commit provider tokens,
local aOS state, worker transcripts, private source, or copied prompts. New constitutional paths
require tests covering rejection as well as success.

Agents remain the runtime. Changes under `modules/` must be deterministic joints, not reasoning
loops, schedulers, or provider-specific orchestration.

## Pull requests

Keep changes focused, explain the authority or durability invariant being changed, include tests,
and update user-facing documentation. Changes to the distro or State Tree must preserve explicit
liaison activation and the read-only grader boundary. By contributing, you agree that your
contribution is licensed under the MIT License.
