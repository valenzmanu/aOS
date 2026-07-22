#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
demo_root="$(mktemp -d)"
trap 'rm -rf "$demo_root"' EXIT

cp -R "$repo_root/examples/calculator/." "$demo_root/project"
git -C "$demo_root/project" init -q
git -C "$demo_root/project" config user.name "aOS Demo"
git -C "$demo_root/project" config user.email "demo@example.invalid"
git -C "$demo_root/project" add .
git -C "$demo_root/project" commit -qm "Initial calculator example"

export AOS_HOME="$demo_root/runtime/state"
uv --directory "$repo_root" run aos init >/dev/null
claim_output="$(uv --directory "$repo_root" run aos liaison claim --harness demo)"
export AOS_LIAISON_TOKEN="$(printf '%s\n' "$claim_output" | awk '/^liaison_token:/ {print $2}')"
test -n "$AOS_LIAISON_TOKEN"

mission_output="$(uv --directory "$repo_root" run aos mission create \
  --project "$demo_root/project" \
  --objective "Verify the calculator example through the governed fake backend" \
  --backend fake \
  --verify "python -m unittest discover -s tests")"
mission_id="$(printf '%s\n' "$mission_output" | awk '/^mission_id:/ {print $2}')"
test -n "$mission_id"

uv --directory "$repo_root" run aos mission authorize "$mission_id" >/dev/null
uv --directory "$repo_root" run aos dispatch "$mission_id" >/dev/null

for _ in $(seq 1 100); do
  state="$(uv --directory "$repo_root" run aos watch --once | awk -v id="$mission_id" '$1 == id {print $2; exit}')"
  if [[ "$state" == "awaiting_approval" || "$state" == "failed" ]]; then
    break
  fi
  sleep 0.05
done

uv --directory "$repo_root" run aos watch --once
if [[ "$state" != "awaiting_approval" ]]; then
  printf 'Demo failed: mission state is %s\n' "$state" >&2
  exit 1
fi
uv --directory "$repo_root" run aos approve "$mission_id" >/dev/null
printf 'Demo complete: %s was verified, approved, and delivered.\n' "$mission_id"
