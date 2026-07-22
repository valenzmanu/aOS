# Contributing to aOS

Thanks for helping build a transparent agent control plane.

## Development setup

```bash
git clone https://github.com/valenzmanu/aOS.git
cd aOS
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

Use synthetic repositories and the fake driver for tests. Do not commit provider tokens, local aOS
state, worker transcripts, private source, or copied prompts. New state transitions and authority
paths require tests covering rejection as well as success.

## Pull requests

Keep changes focused, explain the authority or durability invariant being changed, include tests,
and update user-facing documentation. By contributing, you agree that your contribution is
licensed under the MIT License.
