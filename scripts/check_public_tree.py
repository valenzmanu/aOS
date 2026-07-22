"""Fail when files intended for publication contain common secret or runtime-data shapes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATH_PARTS = {"state", "projects", ".venv", "__pycache__"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Cursor key": re.compile(r"\bcrsr_[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def public_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / name.decode() for name in result.stdout.split(b"\0") if name]


def main() -> int:
    findings: list[str] = []
    for path in public_files():
        relative = path.relative_to(ROOT)
        if any(part in FORBIDDEN_PATH_PARTS for part in relative.parts):
            findings.append(f"runtime/private path is publishable: {relative}")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"cannot read {relative}: {exc}")
            continue
        if b"\0" in raw or len(raw) > 2_000_000:
            continue
        text = raw.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label} pattern in {relative}")
    if findings:
        print("Public-tree privacy check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"Public-tree privacy check passed: {len(public_files())} files inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
