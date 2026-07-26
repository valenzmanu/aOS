# E2E-LAUNCH-01 — cross-harness contract

This deterministic gate proves that liaison identity and worker harness are independent routing
dimensions and that every matrix cell selects the worker harness's adapter through the same
admitted-route and normalized-observation contract.

Run:

```sh
uv run python evals/launch/grade.py \
  --matrix evals/launch/fixtures/matrix.json \
  --output /tmp/aos-launch-grade.json
```

This is not a live-provider proof. The adapter unit tests cover launch mechanics with fake CLIs;
real Codex and Cursor cells must retain private trial evidence before comparative claims are made.

Run one private live worker-adapter proof with an explicitly admitted route:

```sh
uv run python evals/launch/run.py \
  --worker-harness cursor \
  --model <live-catalog-model> \
  --reasoning-effort <admitted-effort> \
  --billing-channel <admitted-billing-channel>
```
