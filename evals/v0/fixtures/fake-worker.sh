#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: fake-worker.sh OUTPUT_DIRECTORY" >&2
    exit 2
fi

output_directory=$1
expected_result=eval-fixture/expected-result.txt

case "$output_directory" in
    missions/M-V0/attempts/0001/worker-output)
        ;;
    *)
        echo "refusing unexpected worker output directory" >&2
        exit 2
        ;;
esac

mkdir -p "$output_directory"
cp "$expected_result" "$output_directory/result.txt"

if command -v shasum >/dev/null 2>&1; then
    result_sha256=$(shasum -a 256 "$output_directory/result.txt" | awk '{print $1}')
elif command -v sha256sum >/dev/null 2>&1; then
    result_sha256=$(sha256sum "$output_directory/result.txt" | awk '{print $1}')
else
    echo "no SHA-256 utility is available" >&2
    exit 2
fi

printf '%s\n' \
    '{' \
    '  "attempt_id": "A-V0",' \
    '  "mission_id": "M-V0",' \
    '  "process_id": "P-V0",' \
    '  "result_path": "result.txt",' \
    "  \"result_sha256\": \"$result_sha256\"," \
    '  "schema_version": 1,' \
    '  "status": "completed",' \
    '  "worker_session_id": "S-V0"' \
    '}' >"$output_directory/receipt.json"
