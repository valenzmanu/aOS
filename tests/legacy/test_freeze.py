from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

SOURCE_REVISION = "e920ff2"
SOURCE_PATH = "tests/test_acceptance.py"
FROZEN_PATH = Path(__file__).with_name("test_v01_characterization.py")
EXPECTED_SHA256 = "2ffa39a6a461581532c5c6c555d4d9557497b4dece0e001aa8b1df1f0e822622"
EXPECTED_TEST_COUNT = 20


def test_legacy_characterization_matches_source_revision() -> None:
    repository = Path(__file__).resolve().parents[2]
    frozen_bytes = FROZEN_PATH.read_bytes()
    source_bytes = subprocess.run(
        ["git", "-C", str(repository), "show", f"{SOURCE_REVISION}:{SOURCE_PATH}"],
        check=True,
        capture_output=True,
    ).stdout

    assert hashlib.sha256(source_bytes).hexdigest() == EXPECTED_SHA256
    assert hashlib.sha256(frozen_bytes).hexdigest() == EXPECTED_SHA256
    assert frozen_bytes == source_bytes

    module = ast.parse(frozen_bytes)
    test_names = [
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert len(test_names) == EXPECTED_TEST_COUNT
