# M-V0 objective

Produce:

```text
missions/M-V0/attempts/0001/worker-output/result.txt
```

with exactly this UTF-8 content, including the final newline:

```text
TYR v0 end-to-end mission completed.
```

Success requires a digest-bound worker receipt, verifier exit status `0`, immutable evidence, an
explicit scripted human approval naming that evidence, and final durable mission state
`delivered`.
