# aOS — Agent OS

> [!IMPORTANT]
> **aOS 2.0 is under active reconstruction.** The 0.1 Python control plane documented below is
> frozen compatibility material, not the repository's current development workflow. Do not claim
> a 0.1 liaison lease, start its watcher, sync it, or dispatch through its CLI while working on the
> v2 rebuild. Start with [ADR 0002](docs/adr/0002-sparse-constitutional-agent-os.md) and the
> [implementation plan](docs/aos-v2-implementation-plan.md).

aOS is an experimental, harness-agnostic control plane for supervising coding agents from one
human-facing liaison. It keeps mission state, authority, routing decisions, checkpoints, and
verification evidence outside any individual Cursor or Codex conversation.

> **Alpha / cooperative assurance.** aOS currently provides durable coordination and policy
> guardrails, not a hardened security boundary. Use it on repositories you are comfortable sending
> to the selected model provider and review every worktree before merging.

## Why aOS?

Agent harnesses are good at doing work, but their chat context is a fragile place to store the
truth about concurrent work. aOS supplies a small kernel around them:

- a singleton, expiring **liaison lease** so only one harness governs work at a time
- immutable journal events and rebuildable projections in local SQLite
- explicit mission states: draft → authorized → dispatched → implementing → verifying → approval
- scoped worker credentials that cannot authorize or approve missions
- one Git worktree per attempt
- policy-aware Cursor, Codex, and deterministic fake backends
- verifier evidence before human approval
- `aos watch`, a read-only flight board designed for a Cursor Agents Window terminal

## Current status

| Capability | Status in 0.1.0 |
| --- | --- |
| Durable journal, leases, mission/process projections | Implemented and tested |
| Fake backend end-to-end | Implemented and tested |
| Git worktree isolation and verification gate | Implemented and tested |
| Durable Contracts, Work Orders, Attempts, Sessions, inbox cursor | Implemented and tested |
| Session / Process / Attempt retry and non-convergence | Implemented and tested |
| Provider event streaming, usage capture, health-aware fallback | Implemented and tested |
| Cursor local execution | Experimental; real Grok 4.5 Fast dogfood passed locally |
| Codex local execution | Experimental; real GPT-5.6 Sol dogfood passed locally |
| Cursor Python SDK transport | Implemented behind explicit opt-in; requires a Cursor API key |
| Cursor cloud agents, provider quota ledger, Codex Goals | Planned, not implemented |
| Hard prevention of harness-native subagents | Not possible at assurance level 1; rules are cooperative |

The plan and evaluation criteria live in [docs/aos-poc-plan.md](docs/aos-poc-plan.md). This table is
the authoritative implementation-status statement; planned features are not presented as shipped.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Git
- Optional: an authenticated `cursor-agent` or `codex` executable for real workers

No provider credentials are required for the fake-driver demo or test suite.

## Legacy 0.1 example (frozen)

The commands in this section describe the preserved 0.1 release. They remain available for
characterization and rollback testing through the v2 migration, but are not the workflow for
developing this repository.

```bash
git clone https://github.com/valenzmanu/aOS.git
cd aOS
uv sync --extra dev
./scripts/demo.sh
```

The demo creates a temporary Git repository from `examples/calculator`, initializes isolated aOS
state, claims the liaison lease, dispatches a deterministic worker, runs the example tests, and
prints the approval-ready flight board. It deletes its temporary runtime data on exit.

## Legacy 0.1 manual quick start

Initialize local state and claim the liaison role:

```bash
uv run aos init
uv run aos liaison claim --harness cursor
# Capture the shown-once value without writing it to a file:
read -s AOS_LIAISON_TOKEN
export AOS_LIAISON_TOKEN
uv run aos watch
```

In another terminal, create and authorize a mission against any local Git repository:

```bash
uv run aos mission create \
  --project /absolute/path/to/repository \
  --objective "Add input validation and tests" \
  --backend fake \
  --verify "python -m unittest discover -s tests"

uv run aos mission authorize M-XXXXXXXX
uv run aos dispatch M-XXXXXXXX
uv run aos mission view M-XXXXXXXX
uv run aos approve M-XXXXXXXX
```

The fake backend exercises governance and verification without editing code. Use it to learn the
workflow. For a real worker, authorize and dispatch `cursor` or `codex`:

```bash
# Cursor: authenticate with `cursor-agent login` or set CURSOR_API_KEY locally.
uv run aos mission create --project "$PWD" --objective "..." --backend cursor \
  --model composer-2.5 --verify "uv run pytest"

# Codex: authenticate with the Codex app/CLI first.
uv run aos mission create --project "$PWD" --objective "..." --backend codex \
  --model gpt-5.6-sol --verify "uv run pytest"
```

Model IDs and account availability are provider-controlled. Run `cursor-agent --list-models` or
inspect your Codex setup, then explicitly authorize the model you intend to use. Anthropic models
are rejected unless the Mission Contract includes `--allow-anthropic`.

The Cursor backend defaults to the authenticated CLI so it can use your existing local Cursor
login. To use Cursor's official Python SDK instead, install the optional dependency and opt in:

```bash
uv sync --extra cursor
export CURSOR_API_KEY="..."       # keep this local; never commit it
export AOS_CURSOR_TRANSPORT=sdk
```

The SDK is usage-billed under Cursor's current SDK terms; it should not be assumed to consume the
same allowance as an IDE subscription. The Codex adapter intentionally uses the authenticated
local CLI because that is the verified subscription-backed surface. Codex Goals are not exposed by
the public CLI/SDK adapter in this release.

## Legacy 0.1 Cursor Agents Window behavior

The 0.1 release originally told a liaison to claim its lease and start one long-running
`uv run aos watch` terminal. Those repository and editor instructions are now explicitly disarmed
during the v2 rebuild; `.vscode/tasks.json` no longer starts the watcher on folder open.

These instructions discourage Cursor multitask and native subagents for governed work, but they
cannot technically disable every harness capability. Only workers created by `aos dispatch` are
registered and governed by aOS.

## Data and privacy model

aOS is designed to be developed in public without publishing runtime data:

- `state/` contains the database and local artifacts; `projects/` contains external worktrees.
  Both are ignored by Git.
- Liaison and process tokens are random bearer values. SQLite stores only SHA-256 hashes. The
  liaison token is shown once; pass it through `AOS_LIAISON_TOKEN`, never a committed file.
- Workers receive a curated, versioned Work Order—not the liaison's raw conversation.
- Raw provider output is kept under `state/artifacts/` and is never suitable for public issues.
- The public-tree scanner rejects common credential shapes and runtime-data paths.

Before every public push:

```bash
uv run python scripts/check_public_tree.py
git status --short
git diff --staged
```

Read [SECURITY.md](SECURITY.md) for threat-model limits and private vulnerability reporting.

## Commands

```text
aos                         live dashboard
aos init                    initialize private runtime state
aos doctor                  validate setup without printing credential values
aos liaison claim|sync|release
aos mission create|authorize|view
aos dispatch                route and spawn an isolated worker
aos retry --level ...       retry at Session, Process, or Attempt scope
aos signal <id> <action>    pause, resume, or cancel with liaison authority
aos watch [--once]          read-only flight board; survives daemon restarts
aos briefing                reconstruct active operational context
aos events [--since N]      inspect the append-only journal
aos recover                 mark expired workers orphaned
aos provider status|discover|observe
aos approve                 record human approval after verification
aosd                        run lease/recovery supervision
```

All commands are non-interactive, return exit code 0/1/2, and include next-step hints. Unknown flags
fail loudly through `argparse`.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python scripts/check_public_tree.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md). aOS is released under the
[MIT License](LICENSE).
