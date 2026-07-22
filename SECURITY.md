# Security policy

aOS is an early cooperative-assurance proof of concept. It reduces accidental authority and state
loss; it is **not** a security sandbox and cannot prevent a compromised or deliberately
non-compliant harness from bypassing project instructions.

## Supported versions

Only the latest release on the default branch receives security fixes during the PoC phase.

## Report a vulnerability

Please do not open a public issue containing exploit details, credentials, private source, prompts,
or transcripts. Use GitHub's private vulnerability reporting for this repository. If that feature
is unavailable, open a minimal issue asking the maintainer for a private reporting channel without
including sensitive details.

Include the affected version, impact, reproduction conditions, and suggested mitigation. Remove
all real tokens and private repository content from examples.

## Sensitive local data

`state/` and `projects/` are ignored because they may contain:

- liaison/process credentials in active use (the database stores hashes, but process environments
  temporarily hold scoped tokens)
- objectives, Work Orders, checkpoints, model output, transcripts, and source excerpts
- external repository worktrees and uncommitted code
- local usernames, absolute paths, process IDs, and provider session identifiers

Do not attach those directories to bug reports. Produce a minimal reproduction using the fake
driver and synthetic data. Run `uv run python scripts/check_public_tree.py` before publishing.

## Threat-model limits

- Liaison and process tokens are bearer credentials. Protect the environment of active processes.
- The project rules preventing native subagents are cooperative instructions, not hard enforcement.
- Provider CLIs/SDKs send approved Work Orders and relevant repository content to their services
  under those providers' terms and account settings.
- Verification commands are executed directly without a shell, but they still run local code.
- Worktrees isolate Git working copies; they do not provide OS, network, or tenant isolation.
