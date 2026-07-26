# Security policy

aOS is an early cooperative-assurance system. It reduces accidental authority and state loss; it
is **not** a security sandbox and cannot prevent a compromised or deliberately non-compliant
agent harness from bypassing project instructions.

## Supported versions

Only the latest release on the default branch receives security fixes during the alpha.

## Report a vulnerability

Please do not open a public issue containing exploit details, credentials, private source, prompts,
or transcripts. Use GitHub's private vulnerability reporting for this repository. If that feature
is unavailable, open a minimal issue asking the maintainer for a private reporting channel without
including sensitive details.

Include the affected version, impact, reproduction conditions, and suggested mitigation. Remove
all real tokens and private repository content from examples.

## Sensitive local data

`state/`, `missions/`, and `projects/` are ignored because they may contain:

- liaison or worker capabilities in active use (the database stores hashes, but process
  environments temporarily hold scoped values)
- objectives, Work Orders, checkpoints, model output, transcripts, and source excerpts
- external repository worktrees and uncommitted code
- local usernames, absolute paths, process IDs, and provider session identifiers

Do not attach those directories to bug reports. Produce a minimal reproduction using synthetic
data and the deterministic worker fixture. Run `uv run python scripts/check_public_tree.py` before
publishing.

## Threat-model limits

- Worker and process capabilities are bearer credentials. Protect the environment of active
  processes. Resident liaison session IDs are attribution, not credentials.
- Distro instructions and agent-role boundaries are cooperative, not hard enforcement.
- Agent harnesses may send objectives, work orders, and repository content to their configured
  model providers under those providers' terms and account settings.
- Verifiers and worker commands execute local code and must be treated as trusted inputs.
- File-tree and worktree separation do not provide OS, network, or tenant isolation.
