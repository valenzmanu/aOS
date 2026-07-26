# aOS evaluations

This directory is the eval-only boundary. Harbor is not an aOS runtime dependency. Harbor-backed
tasks use the locked wrapper below; agent-behavior scenarios own small runners that launch one
fresh harness process, wait, and grade retained private trial artifacts.

## Pinned Harbor contract

- Python: `3.12`
- Harbor: `0.20.0`
- Harbor task schema: `1.3`
- Deterministic M1 agent: Harbor's built-in `oracle`
- Runtime and verifier network: disabled
- Canonical grades: separate `governance` and `mission` scores in `grades.json`
- Harbor selection signal: `reward = mission` only when governance is `1`, otherwise `0`

For Harbor scenarios, `run.py` is the supported entry point. It always delegates through:

```text
uv run --project evals --locked --python 3.12 harbor ...
```

Direct Harbor invocation is unsupported because it bypasses content materialization, unique run
directories, credential filtering, retained-grade validation, and fail-closed result parsing.

## HBR-01

`tasks/hbr-01` is the compatibility probe. Its Oracle solution writes one declared artifact. Harbor
must transfer that artifact to a separate verifier image built from `tests/Dockerfile`; that image
bakes `/tests/test.sh` and `grader.py` rather than relying on a verifier-time upload.

The verifier always retains numeric `reward.json` and the canonical `grades.json`. A missing
declared artifact is `infrastructure_invalid`. A present but incorrect fixture is a valid run with
governance `1`, mission `0`, and reward `0`; the wrapper still fails because HBR-01's configured
mission minimum is `1`.

## Commands

Offline checks that do not install Harbor or invoke Docker:

```sh
UV_CACHE_DIR=/tmp/aos-uv-cache uv run pytest -q evals/tests
UV_CACHE_DIR=/tmp/aos-uv-cache uv run ruff check evals
```

After `evals/uv.lock` has been generated and checked in with network access explicitly authorized,
run the compatibility gate:

```sh
UV_CACHE_DIR=/tmp/aos-uv-cache uv run python evals/run.py --scenario HBR-01
```

`--all` currently selects every configured scenario (HBR-01 in this slice). `--parse-job PATH`
validates retained Harbor output without Docker.

Every generated context, job, manifest, and verifier log lives under a unique `evals/.runs/`
directory and is ignored by `evals/.gitignore`.

## Agent-behavior scenarios

```sh
uv run python evals/routing/run.py
uv run python evals/delegation/run.py --mode both
uv run python evals/launch/grade.py \
  --matrix evals/launch/fixtures/matrix.json \
  --output /tmp/aos-launch-grade.json
```

`E2E-ROUTING-01` grades layered model and practical-route judgment.
`E2E-DELEGATION-01` compares parent-only and bounded native read-parallel work.
`E2E-LAUNCH-01-CONTRACT` proves the four liaison/worker combinations share one adapter boundary;
it is not a live-provider matrix result.

## Materialization boundary

Each task's `materialize.json` names every task file and repository source file allowed into the
Docker build context. `materialize.py` rejects traversal, symlinks, duplicate entries, protected
state and run paths, unknown allowlist schemas, and tampered digest directories. Repository source
is copied only beneath `environment/aos-source/`, because a Harbor task's `environment/` directory
is its Docker build context.

Before claiming HBR-01 as passing, retain proof of the locked Python/Harbor environment, successful
task-schema load, both Docker image builds and resolved image digests, Oracle execution, artifact
transfer, numeric rewards, canonical grades, and wrapper parsing. Static/unit checks alone are not
that proof.
