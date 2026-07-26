"""Learning-mission thin controls from intake through deterministic verification.

Humans may speak vaguely. This module preserves the raw request, lets the liaison
refine an evaluable contract in the State Tree (interviewing when needed), and
admits authorization only when that contract is evaluable. It also gates the
landing mission's dispatch/start/finish transitions and pinned acceptance
evidence. It never launches, sequences, retries, or supervises a worker; agents
remain the runtime. Behavior here is shaped by the landing-page learning mission
under evals/learning/aos-landing/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from modules.journal import (
    ExpectedRevision,
    GateRejection,
    InitializationRequest,
    InvalidOperation,
    Invoker,
    OperationOutcome,
    OperationRequest,
    _outcome_from_record,
    _run_governed,
    append_observation,
    content_digest,
    initialize_root,
    utc_now,
)
from modules.lease import capability_digest
from modules.protocol import (
    AggregateType,
    ContractAuthorization,
    MissionState,
    ProtocolViolation,
    require_transition,
    validate_evidence_record,
)

REQUIRED_CONTRACT_HEADINGS = (
    "Outcome",
    "Acceptance",
    "Constraints",
    "Out of scope",
    "Open questions",
)
UNCHECKED_ITEM = re.compile(r"^\s*[-*]\s+\[\s\]\s+", re.MULTILINE)
CHECKED_ITEM = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+", re.MULTILINE)
HEADING_LINE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
WORKER_NOTE = re.compile(r"^\s*-\s+([A-Za-z-]+):\s*(.*?)\s*$", re.MULTILINE)
LANDING_REPOSITORY_URL = "https://github.com/valenzmanu/aOS"
LANDING_VERIFIER_SPEC = {
    "checks": [
        "single_static_page",
        "primary_brand_aos",
        "product_sentence",
        "github_primary_cta",
        "privacy_scan",
        "worker_attempt_notes",
    ],
    "report_policy": "non-sensitive-aggregates-only",
    "schema_version": 1,
    "selectors": {
        "primary_brand": "[data-primary-brand]",
        "primary_cta": "a[data-primary-cta]",
        "product_sentence": "[data-product-sentence]",
    },
    "symlink_policy": "reject",
}
LANDING_VERIFIER_SPEC_DIGEST = content_digest(LANDING_VERIFIER_SPEC)
LANDING_COMMAND_DIGEST = hashlib.sha256(b"aos-mission verify:landing-v1").hexdigest()
SECRET_SHAPED = re.compile(
    rb"(?i)(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|bearer\s+[a-z0-9._~+/=-]{20,})"
)
SECRET_ASSIGNMENT = re.compile(
    rb"""(?ix)
    ["']?
    (?:claim[_-]?token|access[_-]?token|api[_-]?key|secret|password)
    ["']?
    \s*[:=]\s*
    ["'][^"'\r\n]{8,}["']
    """
)
PRIVATE_PATH_MARKERS = (
    b"/Users/",
    b"/private/var/folders/",
    b"/var/folders/",
    b".run/missions/",
    b"missions/M-",
    b"state/liaison/",
    b"AOS_WORKER_TOKEN",
    b"AOS_LIAISON_SESSION_ID",
)


class _ValidationContext(Protocol):
    db: sqlite3.Connection
    occurred_at: datetime


class _ProjectionContext(Protocol):
    db: sqlite3.Connection
    occurred_at: str

    def append_event(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, object],
        *,
        severity: str = "info",
        payload_schema_version: int = 1,
    ) -> object: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _json_file(path: Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InvalidOperation("decision file must be a JSON object")
    return payload


class _LandingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._captures: list[tuple[str, str]] = []
        self.brand_text: list[str] = []
        self.product_text: list[str] = []
        self.primary_cta_hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if "data-primary-brand" in attributes:
            self._captures.append((tag, "brand"))
        if "data-product-sentence" in attributes:
            self._captures.append((tag, "product"))
        if tag == "a" and "data-primary-cta" in attributes:
            self.primary_cta_hrefs.append(attributes.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._captures) - 1, -1, -1):
            if self._captures[index][0] == tag:
                self._captures.pop(index)
                break

    def handle_data(self, data: str) -> None:
        for _tag, capture in self._captures:
            if capture == "brand":
                self.brand_text.append(data)
            elif capture == "product":
                self.product_text.append(data)


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _contains_unsafe_content(payload: bytes) -> bool:
    lowered = payload.lower()
    return (
        SECRET_SHAPED.search(payload) is not None
        or SECRET_ASSIGNMENT.search(payload) is not None
        or any(marker.lower() in lowered for marker in PRIVATE_PATH_MARKERS)
    )


def _site_manifest(
    site_root: Path,
) -> tuple[Path, list[dict[str, object]], str]:
    site_root = Path(site_root)
    if site_root.is_symlink():
        raise InvalidOperation("landing site root must not be a symlink")
    site_root = site_root.resolve()
    if not site_root.is_dir():
        raise InvalidOperation("landing site root is missing")
    entries: list[dict[str, object]] = []
    for path in sorted(site_root.rglob("*")):
        relative = path.relative_to(site_root).as_posix()
        if path.is_symlink():
            # Symlinks are rejected by the privacy check below. Never resolve or
            # record their targets: a target can itself be a private host path.
            entries.append({"path": relative, "type": "symlink"})
        elif path.is_file():
            entries.append(
                {
                    "digest": _sha256_bytes(path.read_bytes()),
                    "mode": path.stat().st_mode & 0o777,
                    "path": relative,
                    "size": path.stat().st_size,
                    "type": "file",
                }
            )
        elif path.is_dir():
            entries.append({"path": relative, "type": "directory"})
    return site_root, entries, content_digest(entries)


def _git_metadata(site_root: Path, manifest_digest: str) -> tuple[str, str]:
    site_root = Path(site_root).resolve()
    git_root = next(
        (candidate for candidate in (site_root, *site_root.parents) if (candidate / ".git").exists()),
        None,
    )
    if git_root is None:
        return "0" * 40, manifest_digest
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    git_head = head.stdout.strip()
    if head.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", git_head) is None:
        raise InvalidOperation("could not identify landing-site Git HEAD")
    relative = site_root.relative_to(git_root).as_posix()
    index = subprocess.run(
        ["git", "ls-files", "--stage", "--", relative],
        cwd=git_root,
        env=environment,
        check=False,
        capture_output=True,
    )
    if index.returncode != 0:
        raise InvalidOperation("could not identify landing-site Git index")
    return git_head, _sha256_bytes(index.stdout)


def _landing_acceptance_report(
    site_root: Path,
    notes_path: Path,
    *,
    mission_id: str,
) -> tuple[dict[str, object], str, str, str]:
    site_root, manifest, manifest_digest = _site_manifest(site_root)
    symlink_paths = sorted(
        str(entry["path"]) for entry in manifest if entry["type"] == "symlink"
    )
    html_files = sorted(
        entry["path"]
        for entry in manifest
        if entry["type"] == "file" and str(entry["path"]).endswith(".html")
    )
    build_manifests = {
        "package.json",
        "vite.config.js",
        "vite.config.ts",
        "webpack.config.js",
    }
    present_build_manifests = sorted(
        item for item in build_manifests if (site_root / item).exists()
    )
    index_path = site_root / "index.html"
    index_is_regular = any(
        entry["type"] == "file" and entry["path"] == "index.html"
        for entry in manifest
    )
    index_text = (
        index_path.read_text(encoding="utf-8") if index_is_regular else ""
    )
    parser = _LandingParser()
    parser.feed(index_text)
    brand_text = _normalized_text(parser.brand_text)
    product_text = _normalized_text(parser.product_text)

    unsafe_files: list[str] = []
    legacy_brand_files: list[str] = []
    for entry in manifest:
        if entry["type"] != "file":
            continue
        relative = str(entry["path"])
        payload = (site_root / relative).read_bytes()
        if _contains_unsafe_content(payload):
            unsafe_files.append(relative)
        if b"tyr" in payload.lower():
            legacy_brand_files.append(relative)

    notes_text = (
        Path(notes_path).read_text(encoding="utf-8")
        if Path(notes_path).is_file()
        else ""
    )
    notes = {key: value for key, value in WORKER_NOTE.findall(notes_text)}
    try:
        elapsed_seconds = int(notes.get("Elapsed-seconds", ""))
    except ValueError:
        elapsed_seconds = -1

    checks = [
        {
            "details": {
                "build_manifest_count": len(present_build_manifests),
                "has_index_html": "index.html" in html_files,
                "html_file_count": len(html_files),
            },
            "name": "single_static_page",
            "passed": html_files == ["index.html"] and not present_build_manifests,
        },
        {
            "details": {
                "legacy_brand_file_count": len(legacy_brand_files),
                "primary_brand_matches": brand_text == "aOS",
            },
            "name": "primary_brand_aos",
            "passed": brand_text == "aOS" and not legacy_brand_files,
        },
        {
            "details": {
                "length": len(product_text),
                "mentions_aos": "aos" in product_text.lower(),
            },
            "name": "product_sentence",
            "passed": (
                20 <= len(product_text) <= 220
                and product_text[-1:] in {".", "!", "?"}
            ),
        },
        {
            "details": {
                "primary_cta_count": len(parser.primary_cta_hrefs),
                "primary_cta_matches_repository": (
                    parser.primary_cta_hrefs == [LANDING_REPOSITORY_URL]
                ),
            },
            "name": "github_primary_cta",
            "passed": parser.primary_cta_hrefs == [LANDING_REPOSITORY_URL],
        },
        {
            "details": {
                "symlink_count": len(symlink_paths),
                "unsafe_file_count": len(unsafe_files),
            },
            "name": "privacy_scan",
            "passed": not unsafe_files and not symlink_paths,
        },
        {
            "details": {
                "elapsed_seconds": elapsed_seconds,
                "harness_recorded": bool(notes.get("Harness")),
                "model_recorded": bool(notes.get("Model")),
            },
            "name": "worker_attempt_notes",
            "passed": (
                bool(notes.get("Harness"))
                and bool(notes.get("Model"))
                and elapsed_seconds >= 0
            ),
        },
    ]
    report = {
        "checks": checks,
        "mission_id": mission_id,
        "passed": all(bool(check["passed"]) for check in checks),
        "schema_version": 1,
        "site_manifest_digest": manifest_digest,
        "verifier_spec_digest": LANDING_VERIFIER_SPEC_DIGEST,
    }
    report_digest = content_digest(report)
    git_head, index_digest = _git_metadata(site_root, manifest_digest)
    return report, report_digest, git_head, index_digest


def _recorded_verification_replay(
    root: Path,
    *,
    operation_id: str,
    mission_id: str,
    expected_revision: int,
    evidence_id: str,
) -> tuple[OperationOutcome, dict[str, object], str, str] | None:
    """Load a completed verify result without consulting mutable site files."""

    db_path = Path(root).resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT operation.status, operation.outcome, event.payload
            FROM operations AS operation
            JOIN operation_targets AS target
              ON target.operation_id=operation.operation_id
             AND target.aggregate_type='mission'
             AND target.aggregate_id=?
             AND target.expected_revision=?
             AND target.role='write'
            JOIN event_envelopes AS envelope
              ON envelope.operation_id=operation.operation_id
            JOIN events AS event
              ON event.id=envelope.event_id
             AND event.kind='evidence.recorded'
            WHERE operation.operation_id=?
              AND operation.action='mission_controls.verify'
              AND operation.status='completed'
            """,
            (mission_id, expected_revision, operation_id),
        ).fetchone()
    if row is None:
        return None
    try:
        event_payload = json.loads(str(row[2]))
        report = event_payload["verification_report"]
        report_digest = str(event_payload["report_digest"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidOperation("recorded verification result is invalid") from exc
    if (
        not isinstance(event_payload, dict)
        or not isinstance(report, dict)
        or event_payload.get("evidence_id") != evidence_id
        or report.get("mission_id") != mission_id
        or report.get("verifier_spec_digest") != LANDING_VERIFIER_SPEC_DIGEST
        or content_digest(report) != report_digest
        or not isinstance(report.get("site_manifest_digest"), str)
    ):
        raise InvalidOperation("recorded verification result is invalid")
    outcome = _outcome_from_record(operation_id, str(row[0]), str(row[1]))
    return (
        outcome,
        report,
        report_digest,
        str(report["site_manifest_digest"]),
    )


def _write_verification_projection(
    attempt_dir: Path,
    *,
    process_id: str,
    evidence_id: str,
    report: dict[str, object],
    report_digest: str,
    manifest_digest: str,
) -> None:
    evidence_dir = (
        attempt_dir
        / "processes"
        / process_id
        / "evidence"
        / evidence_id
    )
    _write_json(evidence_dir / "verification.json", report)
    _write_json(
        evidence_dir / "evidence.json",
        {
            "evidence_id": evidence_id,
            "report_digest": report_digest,
            "site_manifest_digest": manifest_digest,
            "verifier_spec_digest": LANDING_VERIFIER_SPEC_DIGEST,
        },
    )


def _human_decision(path: Path, expected_decision: str) -> tuple[str, str]:
    payload = _json_file(path)
    decision = payload.get("decision")
    principal = payload.get("human_principal")
    source_hash = payload.get("decision_source_hash")
    if decision != expected_decision:
        raise InvalidOperation(f"decision must be {expected_decision!r}")
    if not isinstance(principal, str) or not principal:
        raise InvalidOperation("human_principal is required")
    if not isinstance(source_hash, str) or len(source_hash) != 64:
        raise InvalidOperation("decision_source_hash must be a 64-char hex digest")
    return principal, source_hash


def _operation_was_completed(outcome: OperationOutcome) -> bool:
    return outcome.status == "completed" or (
        outcome.status == "replayed" and outcome.original_status == "completed"
    )


def _section_body(markdown: str, heading: str) -> str | None:
    matches = list(HEADING_LINE.finditer(markdown))
    for index, match in enumerate(matches):
        if match.group(1) != heading:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return None


def contract_evaluation_errors(markdown: str) -> tuple[str, ...]:
    """Return deterministic reasons a proposed contract is not yet evaluable."""

    errors: list[str] = []
    headings = HEADING_LINE.findall(markdown)
    for required in REQUIRED_CONTRACT_HEADINGS:
        count = sum(1 for item in headings if item == required)
        if count == 0:
            errors.append(f"missing_section:{required}")
        elif count > 1:
            errors.append(f"duplicate_section:{required}")
    if errors:
        return tuple(errors)

    acceptance = _section_body(markdown, "Acceptance") or ""
    if not CHECKED_ITEM.search(acceptance) and not UNCHECKED_ITEM.search(acceptance):
        errors.append("acceptance_needs_checkbox_items")

    open_questions = _section_body(markdown, "Open questions") or ""
    normalized = open_questions.strip().rstrip(".")
    if normalized == "None":
        return tuple(errors)
    if UNCHECKED_ITEM.search(open_questions) or not CHECKED_ITEM.search(open_questions):
        errors.append("open_questions_unresolved")
    return tuple(errors)


def unresolved_intake_questions(mission_dir: Path) -> bool:
    questions = mission_dir / "intake" / "open-questions.md"
    if not questions.is_file():
        return False
    return UNCHECKED_ITEM.search(questions.read_text(encoding="utf-8")) is not None


def initialize_controls(root: Path, operation_id: str) -> dict[str, object]:
    outcome = initialize_root(
        root,
        InitializationRequest(
            operation_id=operation_id,
            invoker_id="maintenance:mission-controls",
            expected_schema=0,
            expected_root_state="empty",
        ),
    )
    return asdict(outcome)


def _mission_revision(root: Path, mission_id: str) -> int | None:
    db_path = Path(root).resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT revision FROM aggregate_revisions
            WHERE aggregate_type='mission' AND aggregate_id=?
            """,
            (mission_id,),
        ).fetchone()
    return None if row is None else int(row[0])


def intake_mission(
    root: Path,
    missions_root: Path,
    *,
    mission_id: str,
    human_request_path: Path,
    operation_id: str,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    request_text = Path(human_request_path).read_text(encoding="utf-8")
    if not request_text.strip():
        raise InvalidOperation("human request is empty")
    request_digest = content_digest(request_text)

    def validator(context: _ValidationContext) -> GateRejection | None:
        if context.db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone():
            return GateRejection("mission_exists", {"mission_id": mission_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        event = context.append_event(
            "mission.created",
            "mission",
            mission_id,
            {
                "intake": "human_request_preserved",
                "mission_id": mission_id,
                "objective_digest": request_digest,
            },
        )
        context.db.execute(
            """
            INSERT INTO missions(
                id, project, objective, state, allowed_backends, allowed_models,
                allow_anthropic, verify_command, created_at, updated_at
            ) VALUES (?, '.', ?, 'draft', '[]', '[]', 0, 'pending', ?, ?)
            """,
            (mission_id, request_text.strip(), context.occurred_at, context.occurred_at),
        )
        if getattr(event, "event_id", None) is None:
            raise InvalidOperation("mission creation did not produce an event")

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.intake",
        liaison_session_id=liaison_session_id,
        expected_revisions=(ExpectedRevision("mission", mission_id, 0),),
        payload={"mission_id": mission_id, "objective_digest": request_digest},
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        mission_dir = Path(missions_root).resolve() / mission_id
        _write_text(mission_dir / "intake" / "human-request.md", request_text)
        _write_text(
            mission_dir / "intake" / "status.md",
            "# Intake status\n\nPhase: awaiting_evaluable_contract\n",
        )
        _write_text(
            mission_dir / "README.md",
            (
                f"# Mission {mission_id}\n\n"
                "Raw human request is under `intake/human-request.md`.\n"
                "Propose an evaluable contract before authorization.\n"
            ),
        )
    return outcome


def propose_contract(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    contract_path: Path,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Validate and publish a proposed contract without leaving draft.

    Proposal success writes State Tree files and a nonreserved observation. It does
    not bump the mission revision; authorization still expects the post-intake
    revision. Failed evaluation is a journaled gate rejection.
    """

    contract_text = Path(contract_path).read_text(encoding="utf-8")
    errors = list(contract_evaluation_errors(contract_text))
    mission_dir = Path(missions_root).resolve() / mission_id
    if unresolved_intake_questions(mission_dir):
        errors.append("intake_open_questions_unresolved")
    contract_hash = _sha256_bytes(contract_text.encode("utf-8"))
    observed_revision = _mission_revision(root, mission_id)
    if observed_revision is None:
        raise InvalidOperation("mission missing")
    if observed_revision != expected_revision:
        raise InvalidOperation(
            f"mission revision mismatch: expected {expected_revision}, observed {observed_revision}"
        )

    request = OperationRequest(
        operation_id=operation_id,
        action="mission_controls.propose_contract",
        invoker=Invoker(kind="liaison", id=liaison_session_id),
        expected_revisions=(ExpectedRevision("mission", mission_id, expected_revision),),
        payload={"contract_hash": contract_hash, "mission_id": mission_id},
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        row = context.db.execute(
            "SELECT state FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return GateRejection("mission_missing", {"mission_id": mission_id})
        if str(row[0]) != MissionState.DRAFT:
            return GateRejection(
                "mission_state_mismatch",
                {
                    "expected": str(MissionState.DRAFT),
                    "mission_id": mission_id,
                    "observed": str(row[0]),
                },
            )
        if errors:
            return GateRejection(
                "contract_not_evaluable",
                {"errors": errors, "mission_id": mission_id},
            )
        return None

    if errors:

        def noop_projector(_context: _ProjectionContext) -> None:
            return None

        return _run_governed(
            root,
            request,
            validator=validator,
            projector=noop_projector,
            clock=clock,
        )

    # Success: confirm draft, write State Tree, journal a nonreserved observation.
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        row = db.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None or str(row[0]) != MissionState.DRAFT:
            raise InvalidOperation("mission is not draft")

    _write_text(mission_dir / "contract" / "proposed.md", contract_text)
    _write_text(
        mission_dir / "intake" / "status.md",
        "# Intake status\n\nPhase: contract_proposed\n"
        f"Contract-hash: {contract_hash}\n",
    )
    return append_observation(
        root,
        OperationRequest(
            operation_id=operation_id,
            action="journal.append_observation",
            invoker=Invoker(kind="liaison", id=liaison_session_id),
            expected_revisions=(),
            payload={
                "aggregate_id": mission_id,
                "aggregate_type": "mission",
                "kind": "mission.contract_proposed",
                "payload": {
                    "contract_hash": contract_hash,
                    "mission_id": mission_id,
                },
                "severity": "info",
            },
        ),
        clock=clock,
    )


def authorize_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    decision_path: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    principal, decision_hash = _human_decision(Path(decision_path), "authorized")
    mission_dir = Path(missions_root).resolve() / mission_id
    proposed_path = mission_dir / "contract" / "proposed.md"
    if not proposed_path.is_file():
        raise InvalidOperation("proposed contract is missing; propose-contract first")
    contract_text = proposed_path.read_text(encoding="utf-8")
    errors = list(contract_evaluation_errors(contract_text))
    if unresolved_intake_questions(mission_dir):
        errors.append("intake_open_questions_unresolved")
    contract_hash = _sha256_bytes(contract_text.encode("utf-8"))
    contract_id = f"CR-{mission_id}"
    protocol_context = ContractAuthorization(
        human_principal=principal,
        decision_source_hash=decision_hash,
        current_revision=0,
        proposed_revision=1,
        attempts_created=0,
        attempt_ceiling=1,
        invalidates_prior_routing=False,
        invalidates_prior_evidence=False,
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        if errors:
            return GateRejection(
                "contract_not_evaluable",
                {"errors": errors, "mission_id": mission_id},
            )
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.DRAFT,
            target=MissionState.AUTHORIZED,
            protocol_context=protocol_context,
        )
        if rejection is not None:
            return rejection
        existing = context.db.execute(
            "SELECT 1 FROM contract_revisions WHERE id=?",
            (contract_id,),
        ).fetchone()
        if existing is not None:
            return GateRejection("contract_exists", {"contract_id": contract_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        contract_event = context.append_event(
            "contract.authorized",
            "contract_revision",
            contract_id,
            {
                "contract_hash": contract_hash,
                "contract_revision_id": contract_id,
                "decision_source_hash": decision_hash,
                "human_principal": principal,
                "mission_id": mission_id,
                "revision": 1,
            },
        )
        context.db.execute(
            """
            INSERT INTO contract_revisions(
                id, mission_id, revision, content, content_hash, created_at, attempt_ceiling
            ) VALUES (?, ?, 1, ?, ?, ?, 1)
            """,
            (
                contract_id,
                mission_id,
                contract_text,
                contract_hash,
                context.occurred_at,
            ),
        )
        mission_event = context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": "draft", "to_state": "authorized"},
        )
        context.db.execute(
            "UPDATE missions SET state='authorized', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )
        if (
            getattr(contract_event, "event_id", None) is None
            or getattr(mission_event, "event_id", None) is None
        ):
            raise InvalidOperation("authorization events are incomplete")

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.authorize",
        liaison_session_id=liaison_session_id,
        expected_revisions=(
            ExpectedRevision("mission", mission_id, expected_revision),
            ExpectedRevision("contract_revision", contract_id, 0),
        ),
        payload={
            "contract_hash": contract_hash,
            "decision_source_hash": decision_hash,
            "mission_id": mission_id,
        },
        validator=validator,
        projector=projector,
        decision=(principal, decision_hash),
        clock=clock,
    )
    if _operation_was_completed(outcome):
        authorized = contract_text if contract_text.endswith("\n") else f"{contract_text}\n"
        _write_text(mission_dir / "contract" / "authorized.md", authorized)
        _write_text(
            mission_dir / "intake" / "status.md",
            "# Intake status\n\nPhase: authorized\n"
            f"Contract-hash: {contract_hash}\n",
        )
    return outcome


def dispatch_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    work_order_path: Path,
    backend: str,
    model: str,
    workspace: str,
    worker_token: str,
    budget_seconds: int,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Admit one authored work order and a cooperative first-attempt membrane.

    This command records dispatch. It deliberately does not launch or supervise a
    worker; a harness adapter or resident liaison performs that external action
    only after this operation is admitted. The learning slice records the existing
    working tree honestly; physical worktree isolation remains later M2 work.
    """

    for label, value in (
        ("backend", backend),
        ("model", model),
        ("workspace", workspace),
    ):
        if not value or value != value.strip():
            raise InvalidOperation(f"{label} is required")
    workspace_path = Path(workspace)
    if workspace_path.is_absolute() or ".." in workspace_path.parts:
        raise InvalidOperation("workspace must be a logical relative locator")
    if not isinstance(budget_seconds, int) or not 1 <= budget_seconds <= 86_400:
        raise InvalidOperation("budget_seconds must be between 1 and 86400")

    work_order_text = Path(work_order_path).read_text(encoding="utf-8")
    if not work_order_text.strip():
        raise InvalidOperation("work order is empty")
    if _contains_unsafe_content(work_order_text.encode("utf-8")):
        raise InvalidOperation("work order contains unsafe content")
    work_order_hash = _sha256_bytes(work_order_text.encode("utf-8"))

    attempt_number = 1
    attempt_id = f"A-{mission_id}-{attempt_number:04d}"
    process_id = f"P-{mission_id}-{attempt_number:04d}"
    work_order_id = f"WO-{mission_id}-{attempt_number:04d}"
    worker_session_id = f"S-{mission_id}-{attempt_number:04d}"
    authority_epoch_id = f"AE-{mission_id}-{attempt_number:04d}"
    scope = (
        f"mission:{mission_id}:attempt:{attempt_id}:"
        f"process:{process_id}"
    )
    token_hash = capability_digest(
        worker_token,
        kind="worker",
        subject_id=process_id,
        scope=scope,
    )

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        contract = db.execute(
            """
            SELECT id, content_hash, attempt_ceiling
            FROM contract_revisions
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    if contract is None:
        raise InvalidOperation("authorized contract is missing")

    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("contract_revision", str(contract[0]), 1, role="read"),
        ExpectedRevision("attempt", attempt_id, 0),
        ExpectedRevision("authority_epoch", authority_epoch_id, 0),
        ExpectedRevision("process", process_id, 0),
        ExpectedRevision("work_order", work_order_id, 0),
        ExpectedRevision("worker_session", worker_session_id, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.AUTHORIZED,
            target=MissionState.DISPATCHED,
        )
        if rejection is not None:
            return rejection
        attempts_created = int(
            context.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE mission_id=?",
                (mission_id,),
            ).fetchone()[0]
        )
        if attempts_created >= int(contract[2]):
            return GateRejection(
                "attempt_ceiling_exhausted",
                {
                    "attempt_ceiling": int(contract[2]),
                    "attempts_created": attempts_created,
                    "mission_id": mission_id,
                },
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        expires_at = (
            datetime.fromisoformat(context.occurred_at) + timedelta(seconds=budget_seconds)
        ).isoformat()
        context.append_event(
            "attempt.created",
            "attempt",
            attempt_id,
            {
                "attempt_id": attempt_id,
                "isolation": "cooperative_existing_working_tree",
                "mission_id": mission_id,
                "number": attempt_number,
                "workspace": workspace,
            },
        )
        context.db.execute(
            """
            INSERT INTO attempts(
                id, mission_id, number, state, worktree, branch, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                attempt_id,
                mission_id,
                attempt_number,
                workspace,
                "existing-working-tree",
                context.occurred_at,
                context.occurred_at,
            ),
        )

        authority_event = context.append_event(
            "authority.claimed",
            "authority_epoch",
            authority_epoch_id,
            {
                "epoch_id": authority_epoch_id,
                "expires_at": expires_at,
                "kind": "worker",
                "scope": scope,
                "subject_id": process_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO authority_epochs(
                epoch_id, kind, subject_id, scope, token_hash, issued_at,
                expires_at, status, issued_event_id
            ) VALUES (?, 'worker', ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                authority_epoch_id,
                process_id,
                scope,
                token_hash,
                context.occurred_at,
                expires_at,
                authority_event.event_id,  # type: ignore[attr-defined]
            ),
        )

        context.append_event(
            "process.created",
            "process",
            process_id,
            {
                "attempt_id": attempt_id,
                "backend": backend,
                "mission_id": mission_id,
                "model": model,
                "process_id": process_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO processes(
                id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, checkpoint,
                routing_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, '{}', ?, ?, ?)
            """,
            (
                process_id,
                mission_id,
                attempt_number,
                backend,
                model,
                token_hash,
                expires_at,
                workspace,
                worker_session_id,
                (
                    f"liaison selected {backend}/{model} for the authorized "
                    f"{budget_seconds}-second learning-mission slice"
                ),
                context.occurred_at,
                context.occurred_at,
            ),
        )

        context.append_event(
            "work_order.created",
            "work_order",
            work_order_id,
            {
                "contract_hash": str(contract[1]),
                "contract_revision_id": str(contract[0]),
                "content_hash": work_order_hash,
                "mission_id": mission_id,
                "process_id": process_id,
                "work_order_id": work_order_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO work_orders(
                id, mission_id, process_id, revision, content, content_hash, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                work_order_id,
                mission_id,
                process_id,
                work_order_text,
                work_order_hash,
                context.occurred_at,
            ),
        )

        context.append_event(
            "worker_session.created",
            "worker_session",
            worker_session_id,
            {
                "process_id": process_id,
                "sequence": 1,
                "session_id": worker_session_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO worker_sessions(
                id, process_id, sequence, state, backend_handle,
                metadata, started_at, ended_at
            ) VALUES (?, ?, 1, 'created', NULL, ?, ?, NULL)
            """,
            (
                worker_session_id,
                process_id,
                json.dumps(
                    {
                        "backend": backend,
                        "budget_seconds": budget_seconds,
                        "model": model,
                        "workspace": workspace,
                    },
                    sort_keys=True,
                ),
                context.occurred_at,
            ),
        )

        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": "authorized", "to_state": "dispatched"},
        )
        context.db.execute(
            "UPDATE missions SET state='dispatched', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.dispatch",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": authority_epoch_id,
            "backend": backend,
            "budget_seconds": budget_seconds,
            "contract_hash": str(contract[1]),
            "mission_id": mission_id,
            "model": model,
            "token_hash": token_hash,
            "workspace": workspace,
            "work_order_hash": work_order_hash,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        attempt_dir = (
            Path(missions_root).resolve()
            / mission_id
            / "attempts"
            / f"{attempt_number:04d}"
        )
        _write_json(
            attempt_dir / "attempt.json",
            {
                "attempt_id": attempt_id,
                "authority_epoch_id": authority_epoch_id,
                "budget_seconds": budget_seconds,
                "isolation": "cooperative_existing_working_tree",
                "mission_id": mission_id,
                "number": attempt_number,
                "process_id": process_id,
                "worker_session_id": worker_session_id,
                "workspace": workspace,
            },
        )
        _write_text(
            attempt_dir / "route.md",
            (
                f"# Admitted route — {attempt_id}\n\n"
                f"- Harness: {backend}\n"
                f"- Model: {model}\n"
                f"- Budget-seconds: {budget_seconds}\n"
                f"- Contract: {contract[0]} ({contract[1]})\n"
                "- Isolation: cooperative existing working tree\n"
                f"- Workspace: {workspace}\n"
                f"- Work-order-hash: {work_order_hash}\n"
            ),
        )
        notes_path = attempt_dir / "notes.md"
        if not notes_path.exists():
            _write_text(
                notes_path,
                (
                    f"# Worker notes — {attempt_id}\n\n"
                    f"- Harness: {backend}\n"
                    f"- Model: {model}\n"
                    "- Elapsed-seconds: pending\n"
                    "- Outcome: pending\n"
                ),
            )
        _write_text(
            attempt_dir
            / "processes"
            / process_id
            / "work-orders"
            / "0001.md",
            work_order_text,
        )
        _write_json(
            attempt_dir
            / "processes"
            / process_id
            / "sessions"
            / worker_session_id
            / "session.json",
            {
                "process_id": process_id,
                "sequence": 1,
                "state": "created",
                "worker_session_id": worker_session_id,
            },
        )
    return outcome


def start_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Mark an admitted worker membrane running before external execution."""

    process_id = f"P-{mission_id}-0001"
    worker_session_id = f"S-{mission_id}-0001"
    authority_epoch_id = f"AE-{mission_id}-0001"
    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("process", process_id, 1),
        ExpectedRevision("worker_session", worker_session_id, 1),
        ExpectedRevision("attempt", f"A-{mission_id}-0001", 1, role="read"),
        ExpectedRevision("work_order", f"WO-{mission_id}-0001", 1, role="read"),
        ExpectedRevision("authority_epoch", authority_epoch_id, 1, role="read"),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.DISPATCHED,
            target=MissionState.IMPLEMENTING,
        )
        if rejection is not None:
            return rejection
        process = context.db.execute(
            "SELECT state FROM processes WHERE id=?",
            (process_id,),
        ).fetchone()
        session = context.db.execute(
            "SELECT state FROM worker_sessions WHERE id=?",
            (worker_session_id,),
        ).fetchone()
        epoch = context.db.execute(
            "SELECT status, expires_at FROM authority_epochs WHERE epoch_id=?",
            (authority_epoch_id,),
        ).fetchone()
        if process is None or session is None or epoch is None:
            return GateRejection(
                "worker_membrane_missing",
                {"mission_id": mission_id},
            )
        if str(epoch[0]) != "active":
            return GateRejection(
                "worker_authority_inactive",
                {"authority_epoch_id": authority_epoch_id},
            )
        if datetime.fromisoformat(str(epoch[1])) <= context.occurred_at:
            return GateRejection(
                "worker_authority_expired",
                {"authority_epoch_id": authority_epoch_id},
            )
        try:
            require_transition(
                AggregateType.PROCESS,
                str(process[0]),
                "running",
            )
            require_transition(
                AggregateType.WORKER_SESSION,
                str(session[0]),
                "running",
            )
        except ProtocolViolation:
            return GateRejection(
                "protocol_rejected",
                {"phase": "start"},
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        context.append_event(
            "process.transitioned",
            "process",
            process_id,
            {"from_state": "created", "to_state": "running"},
        )
        context.db.execute(
            "UPDATE processes SET state='running', updated_at=? WHERE id=?",
            (context.occurred_at, process_id),
        )
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            worker_session_id,
            {"from_state": "created", "to_state": "running"},
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state='running', started_at=?
            WHERE id=?
            """,
            (context.occurred_at, worker_session_id),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": "dispatched", "to_state": "implementing"},
        )
        context.db.execute(
            "UPDATE missions SET state='implementing', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.start",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": authority_epoch_id,
            "mission_id": mission_id,
            "process_id": process_id,
            "worker_session_id": worker_session_id,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _write_json(
            Path(missions_root).resolve()
            / mission_id
            / "attempts"
            / "0001"
            / "processes"
            / process_id
            / "sessions"
            / worker_session_id
            / "session.json",
            {
                "process_id": process_id,
                "sequence": 1,
                "state": "running",
                "worker_session_id": worker_session_id,
            },
        )
    return outcome


def finish_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    receipt_path: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Admit a bound worker receipt and close the running attempt for verification."""

    attempt_id = f"A-{mission_id}-0001"
    process_id = f"P-{mission_id}-0001"
    work_order_id = f"WO-{mission_id}-0001"
    worker_session_id = f"S-{mission_id}-0001"
    authority_epoch_id = f"AE-{mission_id}-0001"
    receipt_file = Path(receipt_path)
    receipt_bytes = receipt_file.read_bytes()
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as exc:
        raise InvalidOperation("worker receipt must be valid JSON") from exc
    if not isinstance(receipt, dict):
        raise InvalidOperation("worker receipt must be a JSON object")
    expected_receipt = {
        "attempt_id": attempt_id,
        "authority_epoch_id": authority_epoch_id,
        "mission_id": mission_id,
        "process_id": process_id,
        "schema_version": 1,
        "status": "completed",
        "work_order_id": work_order_id,
        "worker_session_id": worker_session_id,
    }
    if set(receipt) != set(expected_receipt) or any(
        receipt.get(key) != value for key, value in expected_receipt.items()
    ):
        raise InvalidOperation("worker receipt does not match the dispatched membrane")

    attempt_dir = (
        Path(missions_root).resolve() / mission_id / "attempts" / "0001"
    )
    notes_path = attempt_dir / "notes.md"
    if not notes_path.is_file():
        raise InvalidOperation("worker notes are missing")
    notes_text = notes_path.read_text(encoding="utf-8")
    notes = {key: value for key, value in WORKER_NOTE.findall(notes_text)}
    required_notes = {"Harness", "Model", "Elapsed-seconds", "Outcome"}
    if not required_notes.issubset(notes):
        raise InvalidOperation("worker notes are incomplete")
    try:
        elapsed_seconds = int(notes["Elapsed-seconds"])
    except ValueError as exc:
        raise InvalidOperation("worker elapsed seconds must be an integer") from exc
    if elapsed_seconds < 0 or notes["Outcome"] != "completed":
        raise InvalidOperation("worker notes do not report completed work")

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        route = db.execute(
            """
            SELECT backend, model
            FROM processes
            WHERE id=? AND mission_id=?
            """,
            (process_id, mission_id),
        ).fetchone()
        session_metadata = db.execute(
            "SELECT metadata FROM worker_sessions WHERE id=?",
            (worker_session_id,),
        ).fetchone()
    if route is None or session_metadata is None:
        raise InvalidOperation("dispatched membrane is missing")
    if notes["Harness"] != str(route[0]) or notes["Model"] != str(route[1]):
        raise InvalidOperation("worker notes do not match the admitted route")
    metadata = json.loads(str(session_metadata[0]))
    budget_seconds = int(metadata["budget_seconds"])
    receipt_digest = _sha256_bytes(receipt_bytes)
    notes_digest = _sha256_bytes(notes_text.encode("utf-8"))

    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("attempt", attempt_id, 1),
        ExpectedRevision("process", process_id, 2),
        ExpectedRevision("worker_session", worker_session_id, 2),
        ExpectedRevision("authority_epoch", authority_epoch_id, 1),
        ExpectedRevision("work_order", work_order_id, 1, role="read"),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.IMPLEMENTING,
            target=MissionState.VERIFYING,
        )
        if rejection is not None:
            return rejection
        process = context.db.execute(
            "SELECT state FROM processes WHERE id=?",
            (process_id,),
        ).fetchone()
        session = context.db.execute(
            "SELECT state FROM worker_sessions WHERE id=?",
            (worker_session_id,),
        ).fetchone()
        attempt = context.db.execute(
            "SELECT state FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        epoch = context.db.execute(
            "SELECT status, expires_at FROM authority_epochs WHERE epoch_id=?",
            (authority_epoch_id,),
        ).fetchone()
        if process is None or session is None or attempt is None or epoch is None:
            return GateRejection(
                "worker_membrane_missing",
                {"mission_id": mission_id},
            )
        if str(epoch[0]) != "active":
            return GateRejection(
                "worker_authority_inactive",
                {"authority_epoch_id": authority_epoch_id},
            )
        try:
            expires_at = datetime.fromisoformat(
                str(epoch[1]).replace("Z", "+00:00")
            )
        except ValueError:
            return GateRejection(
                "worker_authority_invalid",
                {"authority_epoch_id": authority_epoch_id},
            )
        if context.occurred_at >= expires_at:
            return GateRejection(
                "worker_authority_expired",
                {"authority_epoch_id": authority_epoch_id},
            )
        try:
            require_transition(
                AggregateType.PROCESS,
                str(process[0]),
                "completed",
            )
            require_transition(
                AggregateType.WORKER_SESSION,
                str(session[0]),
                "completed",
            )
            require_transition(
                AggregateType.ATTEMPT,
                str(attempt[0]),
                "completed",
            )
        except ProtocolViolation:
            return GateRejection(
                "protocol_rejected",
                {"phase": "finish"},
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        completion = {
            "authority_epoch_id": authority_epoch_id,
            "elapsed_seconds": elapsed_seconds,
            "notes_digest": notes_digest,
            "receipt_digest": receipt_digest,
            "within_budget": elapsed_seconds <= budget_seconds,
            "work_order_id": work_order_id,
        }
        context.append_event(
            "process.transitioned",
            "process",
            process_id,
            {"from_state": "running", "to_state": "completed", **completion},
        )
        context.db.execute(
            "UPDATE processes SET state='completed', updated_at=? WHERE id=?",
            (context.occurred_at, process_id),
        )
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            worker_session_id,
            {"from_state": "running", "to_state": "completed", **completion},
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state='completed', ended_at=?
            WHERE id=?
            """,
            (context.occurred_at, worker_session_id),
        )
        context.append_event(
            "attempt.transitioned",
            "attempt",
            attempt_id,
            {"from_state": "active", "to_state": "completed", **completion},
        )
        context.db.execute(
            "UPDATE attempts SET state='completed', updated_at=? WHERE id=?",
            (context.occurred_at, attempt_id),
        )
        release_event = context.append_event(
            "authority.released",
            "authority_epoch",
            authority_epoch_id,
            {
                "epoch_id": authority_epoch_id,
                "reason": "worker_completion_admitted",
                "subject_id": process_id,
            },
        )
        context.db.execute(
            """
            UPDATE authority_epochs
            SET status='released', closed_at=?, closed_event_id=?, close_reason=?
            WHERE epoch_id=?
            """,
            (
                context.occurred_at,
                release_event.event_id,  # type: ignore[attr-defined]
                "worker_completion_admitted",
                authority_epoch_id,
            ),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {
                "from_state": "implementing",
                "to_state": "verifying",
                **completion,
            },
        )
        context.db.execute(
            "UPDATE missions SET state='verifying', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.finish",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": authority_epoch_id,
            "elapsed_seconds": elapsed_seconds,
            "mission_id": mission_id,
            "notes_digest": notes_digest,
            "receipt_digest": receipt_digest,
            "within_budget": elapsed_seconds <= budget_seconds,
            "work_order_id": work_order_id,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _write_text(attempt_dir / "receipt.json", receipt_bytes.decode("utf-8"))
        attempt_metadata_path = attempt_dir / "attempt.json"
        attempt_metadata = _json_file(attempt_metadata_path)
        attempt_metadata.update(
            {
                "elapsed_seconds": elapsed_seconds,
                "state": "completed",
                "within_budget": elapsed_seconds <= budget_seconds,
            }
        )
        _write_json(attempt_metadata_path, attempt_metadata)
        _write_json(
            attempt_dir
            / "processes"
            / process_id
            / "sessions"
            / worker_session_id
            / "session.json",
            {
                "elapsed_seconds": elapsed_seconds,
                "process_id": process_id,
                "sequence": 1,
                "state": "completed",
                "worker_session_id": worker_session_id,
                "within_budget": elapsed_seconds <= budget_seconds,
            },
        )
    return outcome


def verify_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    site_root: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Run the pinned landing acceptance checks and record immutable evidence."""

    attempt_id = f"A-{mission_id}-0001"
    process_id = f"P-{mission_id}-0001"
    work_order_id = f"WO-{mission_id}-0001"
    worker_session_id = f"S-{mission_id}-0001"
    evidence_id = f"EV-{mission_id}-0001"
    attempt_dir = (
        Path(missions_root).resolve() / mission_id / "attempts" / "0001"
    )
    replay = _recorded_verification_replay(
        root,
        operation_id=operation_id,
        mission_id=mission_id,
        expected_revision=expected_revision,
        evidence_id=evidence_id,
    )
    if replay is not None:
        outcome, report, report_digest, manifest_digest = replay
        _write_verification_projection(
            attempt_dir,
            process_id=process_id,
            evidence_id=evidence_id,
            report=report,
            report_digest=report_digest,
            manifest_digest=manifest_digest,
        )
        return outcome
    report, report_digest, git_head, index_digest = _landing_acceptance_report(
        site_root,
        attempt_dir / "notes.md",
        mission_id=mission_id,
    )
    verification_passed = bool(report["passed"])
    exit_status = 0 if verification_passed else 1
    target_state = (
        MissionState.AWAITING_APPROVAL
        if verification_passed
        else MissionState.FAILED
    )
    manifest_digest = str(report["site_manifest_digest"])

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        contract = db.execute(
            """
            SELECT id, content_hash
            FROM contract_revisions
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    if contract is None:
        raise InvalidOperation("verified mission has no authorized contract")

    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("evidence", evidence_id, 0),
        ExpectedRevision("contract_revision", str(contract[0]), 1, role="read"),
        ExpectedRevision("attempt", attempt_id, 2, role="read"),
        ExpectedRevision("process", process_id, 3, role="read"),
        ExpectedRevision("worker_session", worker_session_id, 3, role="read"),
        ExpectedRevision("work_order", work_order_id, 1, role="read"),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        return _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.VERIFYING,
            target=target_state,
        )

    def projector(context: _ProjectionContext) -> None:
        evidence_record = {
            "attempt_id": attempt_id,
            "command_hash": LANDING_COMMAND_DIGEST,
            "contract_hash": str(contract[1]),
            "contract_revision_id": str(contract[0]),
            "ended_at": context.occurred_at,
            "exit_status": exit_status,
            "git_head": git_head,
            "index_digest": index_digest,
            "mission_id": mission_id,
            "mission_revision": expected_revision,
            "output_artifacts": [report_digest],
            "process_id": process_id,
            "record_schema_version": 1,
            "started_at": context.occurred_at,
            "verifier_spec_digest": LANDING_VERIFIER_SPEC_DIGEST,
            "worker_session_id": worker_session_id,
            "worktree_manifest_digest": manifest_digest,
        }
        validate_evidence_record(evidence_record)
        evidence_digest = content_digest(evidence_record)
        evidence_event = context.append_event(
            "evidence.recorded",
            "evidence",
            evidence_id,
            {
                "evidence_digest": evidence_digest,
                "evidence_id": evidence_id,
                "exit_status": exit_status,
                "mission_id": mission_id,
                "report_digest": report_digest,
                "verification_report": report,
            },
        )
        context.db.execute(
            """
            INSERT INTO evidence_records(
                digest, record_schema_version, mission_id, mission_revision,
                contract_revision_id, contract_hash, attempt_id, process_id,
                worker_session_id, git_head, index_digest,
                worktree_manifest_digest, verifier_spec_digest, command_hash,
                started_at, ended_at, exit_status, output_artifacts, source_event_id
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_digest,
                mission_id,
                expected_revision,
                str(contract[0]),
                str(contract[1]),
                attempt_id,
                process_id,
                worker_session_id,
                git_head,
                index_digest,
                manifest_digest,
                LANDING_VERIFIER_SPEC_DIGEST,
                LANDING_COMMAND_DIGEST,
                context.occurred_at,
                context.occurred_at,
                exit_status,
                json.dumps([report_digest], sort_keys=True),
                evidence_event.event_id,  # type: ignore[attr-defined]
            ),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {
                "evidence_digest": evidence_digest,
                "evidence_id": evidence_id,
                "from_state": "verifying",
                "to_state": str(target_state),
            },
        )
        context.db.execute(
            """
            UPDATE missions
            SET state=?, updated_at=?
            WHERE id=?
            """,
            (str(target_state), context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.verify",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "mission_id": mission_id,
            "report_digest": report_digest,
            "site_manifest_digest": manifest_digest,
            "verification_passed": verification_passed,
            "verifier_spec_digest": LANDING_VERIFIER_SPEC_DIGEST,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _write_verification_projection(
            attempt_dir,
            process_id=process_id,
            evidence_id=evidence_id,
            report=report,
            report_digest=report_digest,
            manifest_digest=manifest_digest,
        )
    return outcome


def mission_status(root: Path, missions_root: Path, mission_id: str) -> dict[str, object]:
    db_path = Path(root).resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT mission.state, revision.revision
            FROM missions AS mission
            JOIN aggregate_revisions AS revision
              ON revision.aggregate_type='mission'
             AND revision.aggregate_id=mission.id
            WHERE mission.id=?
            """,
            (mission_id,),
        ).fetchone()
        attempt = db.execute(
            """
            SELECT id, number, state
            FROM attempts
            WHERE mission_id=?
            ORDER BY number DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        process = db.execute(
            """
            SELECT id, state, session_id
            FROM processes
            WHERE mission_id=?
            ORDER BY attempt DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        worker_session = (
            None
            if process is None
            else db.execute(
                "SELECT state FROM worker_sessions WHERE id=?",
                (str(process[2]),),
            ).fetchone()
        )
        evidence = db.execute(
            """
            SELECT digest
            FROM evidence_records
            WHERE mission_id=?
            ORDER BY source_event_id DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    mission_dir = Path(missions_root).resolve() / mission_id
    proposed = mission_dir / "contract" / "proposed.md"
    return {
        "attempt_id": None if attempt is None else str(attempt[0]),
        "attempt_state": None if attempt is None else str(attempt[2]),
        "evidence_digest": None if evidence is None else str(evidence[0]),
        "evidence_id": (
            None if evidence is None else f"EV-{mission_id}-0001"
        ),
        "has_proposed_contract": proposed.is_file(),
        "intake_open_questions": unresolved_intake_questions(mission_dir),
        "mission_id": mission_id,
        "process_id": None if process is None else str(process[0]),
        "process_state": None if process is None else str(process[1]),
        "proposed_contract_errors": (
            list(contract_evaluation_errors(proposed.read_text(encoding="utf-8")))
            if proposed.is_file()
            else ["missing_proposed_contract"]
        ),
        "revision": None if row is None else int(row[1]),
        "state": None if row is None else str(row[0]),
        "worker_session_id": None if process is None else str(process[2]),
        "worker_session_state": (
            None if worker_session is None else str(worker_session[0])
        ),
    }


def _mission_state_rejection(
    context: _ValidationContext,
    *,
    mission_id: str,
    source: str,
    target: str,
    protocol_context: object | None = None,
) -> GateRejection | None:
    row = context.db.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
    if row is None:
        return GateRejection("mission_missing", {"mission_id": mission_id})
    if str(row[0]) != source:
        return GateRejection(
            "mission_state_mismatch",
            {"expected": source, "mission_id": mission_id, "observed": str(row[0])},
        )
    try:
        require_transition(
            AggregateType.MISSION,
            source,
            target,
            context=protocol_context,
        )
    except ProtocolViolation:
        return GateRejection("protocol_rejected", {"from": source, "to": target})
    return None


def _liaison_operation(
    root: Path,
    *,
    operation_id: str,
    action: str,
    liaison_session_id: str,
    expected_revisions: tuple[ExpectedRevision, ...],
    payload: dict[str, object],
    validator: Callable[[_ValidationContext], GateRejection | None],
    projector: Callable[[_ProjectionContext], None],
    decision: tuple[str, str] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    on_behalf_of, decision_hash = decision if decision is not None else (None, None)
    request = OperationRequest(
        operation_id=operation_id,
        action=action,
        invoker=Invoker(
            kind="liaison",
            id=liaison_session_id,
            on_behalf_of=on_behalf_of,
            decision_source_hash=decision_hash,
        ),
        expected_revisions=expected_revisions,
        payload=payload,
    )
    return _run_governed(
        root,
        request,
        validator=validator,
        projector=projector,
        clock=clock,
    )


def _outcome_json(outcome: OperationOutcome | dict[str, object]) -> dict[str, object]:
    if isinstance(outcome, dict):
        return outcome
    return {
        "admission_event_id": outcome.admission_event_id,
        "domain_event_ids": list(outcome.domain_event_ids),
        "operation_id": outcome.operation_id,
        "original_status": outcome.original_status,
        "reason": outcome.reason,
        "rejection_event_id": outcome.rejection_event_id,
        "revisions": [asdict(item) for item in outcome.revisions],
        "status": outcome.status,
    }


def _result_is_rejected(result: dict[str, object]) -> bool:
    return result.get("status") == "rejected" or (
        result.get("status") == "replayed" and result.get("original_status") == "rejected"
    )


def _root_from_env(value: str | None, environment_name: str) -> Path:
    selected = value or os.environ.get(environment_name)
    if not selected:
        raise InvalidOperation(f"{environment_name} is required")
    return Path(selected)


def _liaison_session_from_env(value: str | None) -> str:
    session_id = value or os.environ.get("AOS_LIAISON_SESSION_ID")
    if not session_id:
        raise InvalidOperation("AOS_LIAISON_SESSION_ID is required")
    return session_id


def _worker_token_from_env() -> str:
    worker_token = os.environ.get("AOS_WORKER_TOKEN")
    if not worker_token:
        raise InvalidOperation("AOS_WORKER_TOKEN is required for dispatch")
    return worker_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aos-mission")
    parser.add_argument("--state-root")
    parser.add_argument("--missions-root")
    parser.add_argument("--liaison-session")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--op", default="OP-MC-INIT")

    intake = subparsers.add_parser("intake")
    intake.add_argument("mission_id")
    intake.add_argument("--request", required=True)
    intake.add_argument("--op", required=True)

    propose = subparsers.add_parser("propose-contract")
    propose.add_argument("mission_id")
    propose.add_argument("--contract", required=True)
    propose.add_argument("--op", required=True)
    propose.add_argument("--expect", required=True, type=int)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("mission_id")
    authorize.add_argument("--decision", required=True)
    authorize.add_argument("--op", required=True)
    authorize.add_argument("--expect", required=True, type=int)

    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("mission_id")
    dispatch.add_argument("--work-order", required=True)
    dispatch.add_argument("--backend", required=True)
    dispatch.add_argument("--model", required=True)
    dispatch.add_argument("--workspace", required=True)
    dispatch.add_argument("--budget-seconds", required=True, type=int)
    dispatch.add_argument("--op", required=True)
    dispatch.add_argument("--expect", required=True, type=int)

    start = subparsers.add_parser("start")
    start.add_argument("mission_id")
    start.add_argument("--op", required=True)
    start.add_argument("--expect", required=True, type=int)

    finish = subparsers.add_parser("finish")
    finish.add_argument("mission_id")
    finish.add_argument("--receipt", required=True)
    finish.add_argument("--op", required=True)
    finish.add_argument("--expect", required=True, type=int)

    verify = subparsers.add_parser("verify")
    verify.add_argument("mission_id")
    verify.add_argument("--site-root", required=True)
    verify.add_argument("--op", required=True)
    verify.add_argument("--expect", required=True, type=int)

    status = subparsers.add_parser("status")
    status.add_argument("mission_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        root = _root_from_env(args.state_root, "AOS_STATE_ROOT")
        missions_root = _root_from_env(args.missions_root, "AOS_MISSIONS_ROOT")
        if args.command == "init":
            result = initialize_controls(root, args.op)
        elif args.command == "intake":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                intake_mission(
                    root,
                    missions_root,
                    mission_id=args.mission_id,
                    human_request_path=Path(args.request),
                    operation_id=args.op,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "propose-contract":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                propose_contract(
                    root,
                    missions_root,
                    args.mission_id,
                    contract_path=Path(args.contract),
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "authorize":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                authorize_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    decision_path=Path(args.decision),
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "dispatch":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                dispatch_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    work_order_path=Path(args.work_order),
                    backend=args.backend,
                    model=args.model,
                    workspace=args.workspace,
                    worker_token=_worker_token_from_env(),
                    budget_seconds=args.budget_seconds,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "start":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                start_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "finish":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                finish_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    receipt_path=Path(args.receipt),
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "verify":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                verify_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    site_root=Path(args.site_root),
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        else:
            result = mission_status(root, missions_root, args.mission_id)
    except InvalidOperation as exc:
        print(json.dumps({"reason": str(exc), "status": "error"}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    if args.command == "status":
        return 0 if result.get("state") is not None else 1
    if result.get("status") in {"completed", "replayed"} and not _result_is_rejected(result):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
