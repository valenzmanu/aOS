# E2E-ROUTING-01

Choose the best eligible practical route for the task in `eval-fixture/task-profile.md`.

1. Read `protocols/routing.md`, the task profile, and `eval-fixture/candidate-catalog.json`.
2. Create `routing/proposal.md` with the model-fit ranking, practical-route comparison, rejected
   alternatives, selected route, cost reasoning, confidence, and fallback boundary.
3. Create `routing/route-decision.json` with exactly these top-level fields:
   `schema_version`, `model_ranking`, `selected_route`, `cost_class`, `rejected_routes`, and
   `fallback_requires_agent_decision`.
4. Rank these models in order: `gpt-5.6-sol`, `claude-opus-5`, `gpt-5.6-terra`.
5. Choose the exact route supported by the catalog and task boundaries.
6. Include explicit rejections for Cursor's Sol offer and Cursor's Claude offer.
7. Set `fallback_requires_agent_decision` to `true`.

Do not inspect authentication files, environment variables, or private host paths. Do not launch
a provider or claim that work was dispatched, implemented, verified, or delivered.

Your final response must be exactly:

```text
E2E-ROUTING-01 selected codex/gpt-5.6-sol.
```
