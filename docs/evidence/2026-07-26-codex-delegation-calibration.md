---
title: Codex native-delegation calibration
date: 2026-07-26
scenario: E2E-DELEGATION-01
status: calibration
---

# Codex native-delegation calibration

Three compatible paired trials compared one parent-only worker with one worker using three
depth-one, read-only native children. Every trial used Codex CLI `0.145.0` and GPT-5.6 Sol on the
same deterministic three-lane synthesis task.

## Results

| Measurement | Parent only | Native read-parallel | Native delta |
| --- | ---: | ---: | ---: |
| Governance passes | 3/3 | 3/3 | — |
| Exact outcome passes | 3/3 | 3/3 | — |
| Median wall time | 34.618s | 60.185s | +73.9% |
| Wall-time range | 30.496–34.790s | 59.255–67.570s | — |
| Median input tokens | 116,819 | 226,680 | +94.0% |
| Input-token range | 116,578–136,693 | 197,979–247,995 | — |

In every native trial, the parent did not read the child lane files, declared exactly three
depth-one children, produced only the two parent-owned outputs, and integrated the exact result.
The Codex JSONL stream emitted one or two completed collaboration waits but omitted spawn items
and child thread IDs. Grades therefore label child telemetry `indirect`; they do not claim a
harness-observed count.

## Decision

The predeclared speed hypothesis and the 1.75× aggregate-input hypothesis both fail for this small
task. Native delegation remains available but is not the default merely because work is separable.
A worker should use it only when independent lanes are substantial enough to amortize orchestration
or when parent-context isolation has value beyond raw speed.

Input-token usage is not a dollar-cost observation. Billing was not exposed by the harness, and
the ChatGPT plan's marginal cost may differ from API pricing.

## Limits

This is one model, one CLI version, one machine, and one small task family. It establishes a local
policy threshold, not a provider ranking. Cursor native delegation and larger read-parallel tasks
require separate compatible trials.
