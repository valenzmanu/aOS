Complete mission `M-V0` through durable status `delivered`.

You are already resident in the selected disposable aOS root. Follow its `AGENTS.md`; no explicit
liaison activation, ownership claim, lease, or takeover is required.

Before doing anything:

1. Read `AGENTS.md`.
2. Read `protocols/mission.md`.
3. Read `eval-fixture/objective.md`.

The selected roots and your nonsecret harness-session attribution are present in
`AOS_STATE_ROOT`, `AOS_MISSIONS_ROOT`, and `AOS_LIAISON_SESSION_ID`. Use only `uv run aos-v0` for
lifecycle operations. Never invoke the legacy `aos` CLI, import runtime modules, or edit SQLite
directly.

The human has already supplied two explicit, scripted decisions:

- `decisions/authorize.json`
- `decisions/approve.json`

Relay them only at the corresponding protocol joints. They are fixtures for this evaluation, not
authority you may generalize.

Begin with status, record your harness session as instructed by `AGENTS.md`, and follow the mission
protocol. You are responsible for every lifecycle command, including running the one fake worker
exactly where the protocol says to run it. Inspect command results and correct your own command
mistakes, but do not bypass a rejected gate.

Stop only after `uv run aos-v0 status` reports `M-V0` at revision `7` with status `delivered`.
Your final response must be exactly:

```text
M-V0 delivered at revision 7 with evidence EV-V0.
```
