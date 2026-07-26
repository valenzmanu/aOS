# Agent routing protocol

Use this protocol before dispatching a real aOS Worker Process. Routing is liaison judgment; it is
not a scheduler or a deterministic module.

## 1. Define the task

Preserve the authorized goal, desired outcome, acceptance predicates, constraints, privacy
boundary, tools, isolation, deadline, and aggregate budget. Classify the task only as an aid to
reasoning; the label does not choose a route.

## 2. Rank models for task fit

Rank the best model candidates independent of current price or availability. Explain the relevant
capability, likely failure mode, and confidence. There is no permanent global model ranking.

## 3. Expand models into practical routes

A practical route contains:

```text
model + reasoning effort
+ harness + execution mode + isolation
+ billing channel + observed availability
+ delegation policy + aggregate budget
```

Discover mutable model catalogs, CLI versions, authentication, provider health, quota pressure,
and cost class from current observations. Never infer that an unobserved route is cheap, healthy,
or available.

## 4. Apply hard filters

Reject any route outside authorized provider, model, billing, privacy, scope, tool, isolation,
availability, or budget boundaries. Record a concise reason for every material rejection.

## 5. Rank eligible routes

Use this order:

1. reject routes below the task's required quality threshold;
2. minimize expected cost to an accepted outcome;
3. prefer lower latency when quality and cost are comparable; and
4. preserve scarce included capacity when a later high-value task is likely to need it.

Use qualitative judgment until comparable run evidence exists. Do not hide tradeoffs behind an
opaque score.

## 6. Author the decision

Write a human-readable route proposal containing:

- the model-fit ranking;
- available model/harness/billing offers;
- hard-filtered alternatives and reasons;
- the chosen route and cost class;
- confidence, assumptions, and observation timestamps;
- the native-delegation policy and aggregate budget; and
- an explicit fallback that still requires liaison judgment.

Dispatch only after the route is decided. A failure never silently changes the model, harness,
billing channel, work order, or scope.
