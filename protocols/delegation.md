# Native delegation protocol

Use native harness subagents only inside an already admitted aOS Worker Process. Native children
are an execution tactic, not new aOS Workers: they inherit the parent's route, authorization,
workspace, privacy boundary, aggregate budget, and desired outcome.

## Decide deliberately

Delegate only when the work has two or more independent lanes whose context can be bounded and
whose results a parent can verify and integrate. Prefer delegation for read-heavy investigation,
independent review, or isolated implementation surfaces. Keep work in the parent when it is
sequential, tightly coupled, tiny, or dominated by integration.

## Bound the delegation

Before spawning, state:

- the exact child tasks and expected result shape;
- the maximum child count and depth;
- whether children may write, and to which isolated paths;
- the aggregate time and usage budget; and
- the evidence the parent will use to accept each result.

Depth defaults to one. Children may not spawn descendants unless the work order explicitly admits
greater depth. A child may not widen scope, change the practical route, select a different billing
channel, publish, or mutate canonical mission truth.

## Execute and integrate

Spawn independent lanes concurrently when the harness supports it. Give each child the minimum
context it needs. For read-only delegation, children return findings to the parent and create no
durable files. For write delegation, give children disjoint paths and serialize any shared write.

The parent remains responsible for checking child results, resolving disagreement, integrating the
final output, and producing the aOS observation and receipt. Do not preserve raw child transcripts
as canonical state; retain only bounded evidence required by the admitted work order.

## Failure boundary

Child failure does not silently authorize a new model, harness, billing route, scope, retry budget,
or delegation depth. The parent may retry only within the admitted aggregate budget; otherwise it
returns an observation for liaison judgment.
