#!/bin/sh
set -eu

printf '%s\n' '{"message":"harbor-oracle-compatible","schema_version":1}' \
  > /app/submission.json
