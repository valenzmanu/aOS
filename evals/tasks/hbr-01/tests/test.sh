#!/bin/sh
set -eu

python3 /tests/grader.py \
  --submission /app/submission.json \
  --output-dir /logs/verifier
