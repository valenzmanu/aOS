# E2E-DELEGATION-01

Analyze the three independent lanes in `inputs/authority.md`, `inputs/evidence.md`, and
`inputs/privacy.md`. Read `protocols/delegation.md` first.

The runner supplies one execution mode:

- `denied`: do all three lane analyses yourself and do not use native subagents.
- `native-read-parallel`: spawn exactly three native subagents concurrently, one named
  `authority`, one named `evidence`, and one named `privacy`. Give each child only its matching
  lane analysis. Begin each child message with `Lane task: <name>.` Children are read-only, must
  not spawn descendants, and must return their finding to you. You remain the integrating parent.
  The parent must not read the three lane input files directly in this mode; it must integrate the
  returned child findings.

Create exactly two durable result files:

1. `work/synthesis.json`, containing the exact finding from each lane plus the cross-lane total,
   highest severity, and common control.
2. `work/delegation-summary.json`, truthfully recording `schema_version`, `mode`, `child_count`,
   `depth`, `tasks`, and `parent_integrated`.

`work/synthesis.json` must have exactly this shape and key/value vocabulary:

```json
{
  "cross_lane": {
    "common_control": "<shared string from the findings>",
    "highest_severity": "<lowercase severity>",
    "total_impacted": "<integer sum>"
  },
  "lane_findings": {
    "authority": {"impacted": "<integer>", "owner": "<lowercase owner>", "severity": "<lowercase severity>"},
    "evidence": {"impacted": "<integer>", "owner": "<lowercase owner>", "severity": "<lowercase severity>"},
    "privacy": {"impacted": "<integer>", "owner": "<lowercase owner>", "severity": "<lowercase severity>"}
  },
  "schema_version": 1
}
```

Replace every angle-bracket placeholder with the analyzed value; do not copy placeholder strings.
In `delegation-summary.json`, `tasks` means native child task names, not analyzed lanes. It must be
`[]` in `denied` mode and `["authority", "evidence", "privacy"]` in
`native-read-parallel` mode. Use child count/depth `0`/`0` for denied and `3`/`1` for native.
Set `parent_integrated` to `true`. Do not create child transcripts or child-owned result files.

Your final response must be exactly:

```text
E2E-DELEGATION-01 complete.
```
