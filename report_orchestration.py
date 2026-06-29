"""Report-orchestrated coordinator flow — reports as source of truth.

Workers emit short REPORT_READY status + path (or REPORT_BEGIN/END for inline save).
Coordinator reads full report files internally, then builds next-agent prompts from
bounded COORDINATOR_HANDOFF_* blocks — not accumulated full report content.
No chunking or silent truncation in this phase.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker_workspace import REPORT_BEGIN_MARKER, REPORT_END_MARKER, extract_report_block

DEFAULT_ALLOWED_REPORT_ROOTS: tuple[str, ...] = (
    r"C:\Users\Narachat\OneDrive\Ai-Report\claude",
    r"C:\Users\Narachat\OneDrive\Ai-Report\codex",
    r"C:\Users\Narachat\OneDrive\Ai-Report\agy",
    r"C:\Users\Narachat\OneDrive\Ai-Report",
    r"C:\Users\Narachat\Desktop\Ai-Report",
)

DEFAULT_MAX_REPORT_PROMPT_CHARS = 120_000
DEFAULT_MAX_HANDOFF_CHARS = 12_000
MIN_HANDOFF_CHARS = 80

HANDOFF_FOR_AGY_BEGIN = "COORDINATOR_HANDOFF_FOR_AGY_BEGIN"
HANDOFF_FOR_AGY_END = "COORDINATOR_HANDOFF_FOR_AGY_END"
HANDOFF_FOR_CODEX_REVIEWER_BEGIN = "COORDINATOR_HANDOFF_FOR_CODEX_REVIEWER_BEGIN"
HANDOFF_FOR_CODEX_REVIEWER_END = "COORDINATOR_HANDOFF_FOR_CODEX_REVIEWER_END"
HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN = "COORDINATOR_HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN"
HANDOFF_FOR_DEVELOPER_CORRECTION_END = "COORDINATOR_HANDOFF_FOR_DEVELOPER_CORRECTION_END"
HANDOFF_FOR_FINAL_BEGIN = "COORDINATOR_HANDOFF_FOR_FINAL_BEGIN"
HANDOFF_FOR_FINAL_END = "COORDINATOR_HANDOFF_FOR_FINAL_END"

_TWINPET_REPO_MARKERS = (
    r"twinpet-pos\src",
    r"twinpet-pos/tests",
    r"twinpet-pos\functions",
    r"twinpet-pos/src",
)

_REPORT_READY_RE = re.compile(r"^\s*REPORT_READY\s*$", re.IGNORECASE)
_REPORT_WRITE_FAILED_RE = re.compile(r"^\s*REPORT_WRITE_FAILED\s*$", re.IGNORECASE)

_FIELD_PATTERNS = {
    "status": re.compile(r"^\s*Status\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "report": re.compile(r"^\s*Report\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "summary": re.compile(r"^\s*Summary\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "next_role": re.compile(
        r"^\s*Next recommended role\s*:\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "notes": re.compile(r"^\s*Notes\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
}


@dataclass
class ReportRecord:
    role: str
    path: str
    sha256: str
    size_bytes: int
    status: str
    summary: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "summary": self.summary,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReportRecord:
        return cls(
            role=str(data.get("role") or ""),
            path=str(data.get("path") or ""),
            sha256=str(data.get("sha256") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            status=str(data.get("status") or ""),
            summary=str(data.get("summary") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )


@dataclass
class ParsedReportReady:
    status: str
    report_path: str
    summary: str
    next_role: str
    notes: str


@dataclass
class ParsedReportWriteFailed:
    reason: str
    expected_report_path: str
    status: str


@dataclass
class ReportIngestResult:
    ok: bool
    blocker: str = ""
    record: ReportRecord | None = None
    parsed: ParsedReportReady | None = None


@dataclass
class ReportPromptResult:
    ok: bool
    prompt: str = ""
    blocker: str = ""
    dispatch_role: str = ""


def resolve_external_report_write_roots(policy: dict | None) -> list[str]:
    """Return configured external report write allowlist roots."""
    if not isinstance(policy, dict):
        return list(DEFAULT_ALLOWED_REPORT_ROOTS)
    return list(policy.get("external_report_write_roots") or [])


def is_report_orchestrated_policy(policy: dict | None) -> bool:
    """True when session should use report-orchestrated coordinator flow."""
    if not isinstance(policy, dict):
        return False
    if policy.get("analysis_report_only"):
        return True
    mode = str(policy.get("mode") or "")
    return mode in ("read-only", "read-only-analysis", "docs-only")


def normalize_report_status(raw: str) -> str:
    token = (raw or "").strip().upper().replace(" ", "_")
    aliases = {
        "PASS_WITH_NOTES": "PASS_WITH_NOTES",
        "PASSWITHNOTES": "PASS_WITH_NOTES",
        "REQUEST_CHANGES": "REQUEST_CHANGES",
        "REQUESTCHANGES": "REQUEST_CHANGES",
        "REQUEST_UX_CHANGES": "REQUEST_CHANGES",
        "UX_APPROVED": "PASS",
        "BLOCKED": "BLOCKER",
        "BLOCKER": "BLOCKER",
        "FAIL": "FAIL",
        "PASS": "PASS",
    }
    return aliases.get(token, token)


def _first_non_empty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def parse_report_ready(text: str | None) -> ParsedReportReady | None:
    """Parse REPORT_READY worker output."""
    if not text or not str(text).strip():
        return None
    if not _REPORT_READY_RE.match(_first_non_empty_line(text)):
        return None
    body = str(text)
    fields: dict[str, str] = {}
    for key, pattern in _FIELD_PATTERNS.items():
        match = pattern.search(body)
        fields[key] = match.group(1).strip() if match else ""
    if not fields.get("report"):
        return None
    return ParsedReportReady(
        status=normalize_report_status(fields.get("status", "")),
        report_path=fields["report"].strip().strip('"').strip("'"),
        summary=fields.get("summary", ""),
        next_role=fields.get("next_role", "").strip().lower().replace(" ", "_"),
        notes=fields.get("notes", ""),
    )


def parse_report_write_failed(text: str | None) -> ParsedReportWriteFailed | None:
    """Parse REPORT_WRITE_FAILED worker output."""
    if not text or not str(text).strip():
        return None
    if not _REPORT_WRITE_FAILED_RE.match(_first_non_empty_line(text)):
        return None
    reason_match = re.search(r"^\s*Reason\s*:\s*(.+?)\s*$", str(text), re.IGNORECASE | re.MULTILINE)
    expected_match = re.search(
        r"^\s*Expected report\s*:\s*(.+?)\s*$",
        str(text),
        re.IGNORECASE | re.MULTILINE,
    )
    status_match = re.search(r"^\s*Status\s*:\s*(.+?)\s*$", str(text), re.IGNORECASE | re.MULTILINE)
    return ParsedReportWriteFailed(
        reason=(reason_match.group(1).strip() if reason_match else ""),
        expected_report_path=(expected_match.group(1).strip() if expected_match else ""),
        status=normalize_report_status(status_match.group(1).strip() if status_match else "FAIL"),
    )


def _normalize_roots(roots: list[str] | None) -> list[Path]:
    chosen = list(roots) if roots else list(DEFAULT_ALLOWED_REPORT_ROOTS)
    normalized: list[Path] = []
    for root in chosen:
        if not root or not str(root).strip():
            continue
        try:
            normalized.append(Path(str(root)).resolve())
        except OSError:
            normalized.append(Path(str(root)))
    return normalized


def is_twinpet_repo_path(path: str | Path) -> bool:
    low = str(path).lower().replace("/", "\\")
    return any(marker.lower().replace("/", "\\") in low for marker in _TWINPET_REPO_MARKERS)


def validate_report_path(
    raw_path: str,
    *,
    allowed_roots: list[str] | None = None,
) -> tuple[bool, str, Path | None]:
    """Validate report path: absolute, .md only, under allowed roots, not Twinpet repo."""
    if not raw_path or not str(raw_path).strip():
        return False, "empty path", None
    candidate = Path(str(raw_path).strip().strip('"').strip("'"))
    if not candidate.is_absolute():
        return False, "path must be absolute", None
    if ".." in candidate.parts:
        return False, "path traversal rejected", None
    if is_twinpet_repo_path(candidate):
        return False, "Twinpet repo path rejected", None
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return False, f"cannot resolve path: {exc}", None
    if resolved.suffix.lower() != ".md":
        return False, "only .md report files are allowed", None
    target_lower = str(resolved).lower()
    under_root = False
    for root in _normalize_roots(allowed_roots):
        root_lower = str(root).lower()
        if target_lower == root_lower or target_lower.startswith(root_lower + os.sep):
            under_root = True
            break
    if not under_root:
        return False, "BLOCKER: report path outside allowed roots", None
    return True, str(resolved), resolved


def read_report_file(path: str | Path) -> tuple[bool, str, str, int]:
    """Read report file; return ok, content, sha256, size_bytes."""
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        return False, f"BLOCKER: cannot resolve report path: {exc}", "", 0
    if not resolved.is_file():
        return False, f"BLOCKER: report file not found: {resolved}", "", 0
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return False, f"BLOCKER: cannot read report file: {exc}", "", 0
    content = data.decode("utf-8", errors="replace")
    digest = hashlib.sha256(data).hexdigest()
    return True, content, digest, len(data)


def save_inline_report_to_path(
    text: str,
    target_path: str,
    *,
    allowed_roots: list[str] | None = None,
) -> tuple[bool, str, str]:
    """Save REPORT_BEGIN/END block to target path. Returns ok, path, blocker."""
    block = extract_report_block(text)
    if not block:
        return False, "", "BLOCKER: report save failed (no REPORT_BEGIN/REPORT_END block)"
    ok, reason, resolved = validate_report_path(target_path, allowed_roots=allowed_roots)
    if not ok:
        return False, "", reason if reason.startswith("BLOCKER:") else f"BLOCKER: {reason}"
    assert resolved is not None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(block, encoding="utf-8")
    except OSError as exc:
        return False, "", f"BLOCKER: report save failed: {exc}"
    return True, str(resolved), ""


def verify_report_write_permission(
    policy: dict | None,
    *,
    expected_report_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Verify report-orchestrated sessions have external report write permission."""
    if not is_report_orchestrated_policy(policy):
        return True, ""
    if not isinstance(policy, dict):
        return False, "BLOCKER: external report write permission not enabled"
    roots = resolve_external_report_write_roots(policy)
    if not roots:
        return False, "BLOCKER: external report write permission not enabled"
    if list(policy.get("write_files") or []):
        return False, "BLOCKER: Twinpet workspace write allowlist must remain none"
    for raw_root in roots:
        ok, reason, _resolved = validate_report_path(
            str(Path(raw_root) / "permission-check.md"),
            allowed_roots=roots,
        )
        if not ok and "outside allowed roots" not in reason:
            return False, f"BLOCKER: external report write permission not enabled ({reason})"
    for report_path in expected_report_paths or list(policy.get("report_paths") or []):
        ok, reason, _resolved = validate_report_path(report_path, allowed_roots=roots)
        if not ok:
            blocker = reason if reason.startswith("BLOCKER:") else f"BLOCKER: {reason}"
            return False, blocker
    return True, ""


def report_content_fits_prompt(content: str, max_chars: int) -> tuple[bool, int]:
    total = len(content or "")
    return total <= max_chars, total


def extract_handoff_block(content: str, begin_marker: str, end_marker: str) -> str | None:
    """Extract bounded coordinator handoff section from a full report."""
    if not content:
        return None
    pattern = re.compile(
        rf"^\s*{re.escape(begin_marker)}\s*$\s*(.*?)^\s*{re.escape(end_marker)}\s*$",
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.group(1).strip()


def is_handoff_sufficient(handoff: str | None, *, min_chars: int = MIN_HANDOFF_CHARS) -> bool:
    """True when handoff block is present and bounded-complete for dispatch."""
    if handoff is None:
        return False
    stripped = handoff.strip()
    if not stripped:
        return False
    if stripped.upper() == "NONE":
        return True
    return len(stripped) >= min_chars


def handoff_fits_prompt(handoff: str, max_chars: int) -> tuple[bool, int]:
    total = len(handoff or "")
    return total <= max_chars, total


def build_handoff_repair_prompt(
    *,
    owner_role: str,
    report_path: str,
    missing_block: str,
    reason: str = "",
    expected_output_path: str = "",
) -> str:
    """Ask report owner to add or repair a required handoff block."""
    role_label = _REPORT_OWNER_ROLE_LABELS.get(owner_role, owner_role)
    target_path = expected_output_path or report_path
    detail = reason.strip() or f"Required block missing or insufficient: {missing_block}"
    return (
        f"TO: {role_label}\n"
        "FROM: agentchattr Coordinator\n"
        f"ROLE: {role_label}\n"
        "MODE: Report handoff repair\n\n"
        "REQUEST_CHANGES: report missing required coordinator handoff block.\n"
        f"{detail}\n"
        f"Report path: {report_path}\n\n"
        "Update the report at the same path.\n"
        "Add or expand the required handoff block; keep the full report as source of truth.\n"
        "Do not omit required findings in the main report body.\n"
        "Do not chunk.\n"
        "Do not rely on prior chat context.\n"
        "Return REPORT_READY after writing the revised report.\n\n"
        f"REQUIRED HANDOFF BLOCK:\n{missing_block}\n\n"
        f"EXPECTED REPORT OUTPUT PATH:\n{target_path}\n"
    )


def build_handoff_repair_result(
    *,
    owner_role: str,
    report_path: str,
    missing_block: str,
    reason: str = "",
    expected_output_path: str = "",
) -> ReportPromptResult:
    """Route back to report owner to repair a missing/insufficient handoff block."""
    path = expected_output_path or report_path
    return ReportPromptResult(
        ok=True,
        prompt=build_handoff_repair_prompt(
            owner_role=owner_role,
            report_path=report_path,
            missing_block=missing_block,
            reason=reason,
            expected_output_path=path,
        ),
        dispatch_role=owner_role,
    )


def build_oversized_handoff_rewrite_result(
    *,
    owner_role: str,
    report_path: str,
    handoff_block: str,
    handoff_chars: int,
    max_chars: int,
    expected_output_path: str = "",
) -> ReportPromptResult:
    """Route report owner to shrink an oversized handoff block (not the full report)."""
    role_label = _REPORT_OWNER_ROLE_LABELS.get(owner_role, owner_role)
    target_path = expected_output_path or report_path
    return ReportPromptResult(
        ok=True,
        prompt=(
            f"TO: {role_label}\n"
            "FROM: agentchattr Coordinator\n"
            f"ROLE: {role_label}\n"
            "MODE: Report handoff rewrite\n\n"
            "Your coordinator handoff block is too large for next-agent dispatch.\n"
            f"Handoff size: {handoff_chars} chars (max {max_chars}).\n"
            f"Report path: {report_path}\n"
            f"Oversized block: {handoff_block}\n\n"
            "Rewrite only the handoff block at the same report path.\n"
            "Keep it bounded but complete for the target role.\n"
            "Include UNKNOWN FROM SNAPSHOT items where applicable.\n"
            "Do not chunk.\n"
            "Do not rely on prior chat context.\n"
            "Return REPORT_READY after writing the revised report.\n\n"
            f"EXPECTED REPORT OUTPUT PATH:\n{target_path}\n"
        ),
        dispatch_role=owner_role,
    )


def ingest_worker_report_output(
    role: str,
    text: str,
    *,
    allowed_roots: list[str] | None = None,
    expected_report_paths: list[str] | None = None,
    max_prompt_chars: int = DEFAULT_MAX_REPORT_PROMPT_CHARS,
) -> ReportIngestResult:
    """Parse REPORT_READY or inline REPORT_BEGIN/END, validate, read, record."""
    failed = parse_report_write_failed(text)
    if failed is not None:
        reason = failed.reason or "worker could not write report"
        exp = failed.expected_report_path or "(missing)"
        return ReportIngestResult(
            ok=False,
            blocker=(
                "BLOCKER: external report write failed\n"
                f"reason={reason}\n"
                f"expected_report={exp}\n"
                f"status={failed.status or 'FAIL'}"
            ),
        )

    parsed = parse_report_ready(text)

    if parsed is None and REPORT_BEGIN_MARKER in (text or ""):
        if not expected_report_paths:
            return ReportIngestResult(
                ok=False,
                blocker="BLOCKER: report save failed (no expected report path configured)",
            )
        saved_ok, saved_path, blocker = save_inline_report_to_path(
            text,
            expected_report_paths[0],
            allowed_roots=allowed_roots,
        )
        if not saved_ok:
            return ReportIngestResult(ok=False, blocker=blocker or "BLOCKER: report save failed")
        parsed = ParsedReportReady(
            status="PASS_WITH_NOTES",
            report_path=saved_path,
            summary="inline REPORT_BEGIN/END fallback saved by coordinator",
            next_role="coordinator",
            notes="",
        )

    if parsed is None:
        return ReportIngestResult(ok=False, blocker="")

    ok, reason, resolved = validate_report_path(
        parsed.report_path,
        allowed_roots=allowed_roots,
    )
    if not ok:
        blocker = reason if reason.startswith("BLOCKER:") else f"BLOCKER: {reason}"
        return ReportIngestResult(ok=False, blocker=blocker)

    assert resolved is not None
    read_ok, content_or_err, sha256, size_bytes = read_report_file(resolved)
    if not read_ok:
        return ReportIngestResult(ok=False, blocker=content_or_err)

    record = ReportRecord(
        role=role,
        path=str(resolved),
        sha256=sha256,
        size_bytes=size_bytes,
        status=parsed.status,
        summary=parsed.summary,
    )
    return ReportIngestResult(ok=True, record=record, parsed=parsed)


def get_report_for_role(records: list[dict[str, Any]], role: str) -> ReportRecord | None:
    for entry in reversed(records or []):
        if str(entry.get("role") or "") == role:
            return ReportRecord.from_dict(entry)
    return None


def load_report_content(record: ReportRecord) -> tuple[bool, str, str]:
    ok, content, sha256, _size = read_report_file(record.path)
    if not ok:
        return False, content, ""
    if sha256 != record.sha256:
        return False, f"BLOCKER: report hash mismatch for {record.path}", ""
    return True, content, sha256


_REPORT_OWNER_ROLE_LABELS = {
    "developer": "Claude Developer",
    "ui_lead": "AGY UI Lead",
    "reviewer": "Codex Reviewer",
}


def build_report_owner_rewrite_prompt(
    *,
    owner_role: str,
    report_path: str,
    report_chars: int,
    max_chars: int,
    expected_output_path: str = "",
) -> str:
    """Ask the report owner to rewrite an oversized report at the same path."""
    role_label = _REPORT_OWNER_ROLE_LABELS.get(owner_role, owner_role)
    target_path = expected_output_path or report_path
    return (
        f"TO: {role_label}\n"
        "FROM: agentchattr Coordinator\n"
        f"ROLE: {role_label}\n"
        "MODE: Report rewrite\n\n"
        "Your report is too large for next-agent handoff.\n"
        f"Report size: {report_chars} chars (max {max_chars}).\n"
        f"Path: {report_path}\n\n"
        "Rewrite the report at the same path into a bounded review-ready report.\n"
        "Do not omit required findings.\n"
        "Do not omit UNKNOWN FROM SNAPSHOT items.\n"
        "Do not omit risks, data flow, approval-required behavior changes, or verdict.\n"
        "Do not chunk.\n"
        "Do not rely on prior chat context.\n"
        "Return REPORT_READY after writing the revised report.\n\n"
        f"EXPECTED REPORT OUTPUT PATH:\n{target_path}\n"
    )


def build_oversized_report_rewrite_result(
    *,
    owner_role: str,
    report_path: str,
    report_chars: int,
    max_chars: int,
    expected_output_path: str = "",
) -> ReportPromptResult:
    """Route an oversized report back to its owner for bounded rewrite (no chunking)."""
    path = expected_output_path or report_path
    return ReportPromptResult(
        ok=True,
        prompt=build_report_owner_rewrite_prompt(
            owner_role=owner_role,
            report_path=report_path,
            report_chars=report_chars,
            max_chars=max_chars,
            expected_output_path=path,
        ),
        dispatch_role=owner_role,
    )


def build_report_orchestrated_final_attachment(
    report_records: list[dict[str, Any]],
) -> str:
    """Build FINAL footer with report paths, hashes, and reviewer final handoff."""
    dev = get_report_for_role(report_records, "developer")
    agy = get_report_for_role(report_records, "ui_lead")
    reviewer = get_report_for_role(report_records, "reviewer")
    lines = [
        "",
        "REPORT ORCHESTRATION SUMMARY (from registered report files; not channel history):",
        "",
        "Claude report:",
        dev.path if dev else "NONE",
        "",
        "AGY report:",
        agy.path if agy else "NONE",
        "",
        "Codex report:",
        reviewer.path if reviewer else "NONE",
        "",
        "Report hashes:",
    ]
    for record, label in (
        (dev, "developer"),
        (agy, "ui_lead"),
        (reviewer, "reviewer"),
    ):
        if record:
            lines.append(f"  {label}: {record.sha256}")
        else:
            lines.append(f"  {label}: NONE")
    if reviewer:
        ok, rev_content, _ = load_report_content(reviewer)
        if ok:
            final_handoff = extract_handoff_block(
                rev_content, HANDOFF_FOR_FINAL_BEGIN, HANDOFF_FOR_FINAL_END,
            )
            if final_handoff:
                lines.extend([
                    "",
                    "FINAL HANDOFF (from Codex reviewer report):",
                    "---",
                    final_handoff,
                    "---",
                ])
    lines.extend([
        "",
        "Files changed inside Twinpet repo:",
        "NONE",
        "",
        "Twinpet source modified:",
        "NO",
    ])
    return "\n".join(lines)


def _report_metadata_lines(record: ReportRecord) -> list[str]:
    return [
        "SOURCE REPORT METADATA:",
        f"Path: {record.path}",
        f"SHA256: {record.sha256}",
        f"Size: {record.size_bytes} bytes",
        f"Produced by: {record.role}",
        f"Status: {record.status}",
    ]


def _handoff_section_lines(label: str, handoff: str) -> list[str]:
    return [
        "",
        label,
        "---",
        handoff,
        "---",
    ]


def _report_write_contract_lines(
    expected_output_path: str,
    external_report_write_roots: list[str] | None,
) -> list[str]:
    lines = [
        "- You are allowed to write markdown report files only under the configured external Ai-Report report paths.",
        "- You are not allowed to write inside the Twinpet workspace.",
        "- Create the report folder if missing.",
        "- Write your report to the exact expected path.",
        "- Return REPORT_READY with Status, Report path, and Summary when complete.",
        "- Include required COORDINATOR_HANDOFF_* blocks in your report (see OUTPUT CONTRACT).",
    ]
    roots = list(external_report_write_roots or [])
    if roots:
        lines.extend(["", "EXTERNAL REPORT WRITE ALLOWLIST:"])
        for root in roots:
            lines.append(f"- {root}")
    if expected_output_path:
        lines.extend(["", "EXPECTED REPORT OUTPUT PATH:", expected_output_path])
    lines.extend([
        "",
        "If you cannot write the report file despite permission being configured, return exactly:",
        "REPORT_WRITE_FAILED",
        "",
        "Reason:",
        "<short reason>",
        "",
        "Expected report:",
        expected_output_path or "<path>",
        "",
        "Status:",
        "FAIL",
    ])
    return lines


def build_handoff_dispatch_prompt(
    *,
    target_role: str,
    role_label: str,
    mode: str,
    project: str,
    phase: str,
    subject: str,
    task: str,
    source_handoffs: list[tuple[ReportRecord, str, str]],
    instruction: str = "",
    from_source: str = "agentchattr Coordinator",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
    output_handoff_instructions: str = "",
) -> ReportPromptResult:
    """Build next-agent prompt from report metadata + bounded handoff blocks only."""
    lines = [
        f"TO: {role_label}",
        f"FROM: {from_source}",
        f"ROLE: {role_label}",
        f"MODE: {mode}",
        f"PROJECT: {project}",
        f"PHASE: {phase}",
        f"SUBJECT: {subject}",
        "",
        "TASK:",
        task,
        "",
        "RULES:",
        "- Review only the coordinator handoff brief(s) supplied below.",
        "- Full prior reports are on disk; paths and hashes are listed for reference.",
        "- Do not inspect repository files.",
        "- Do not run shell.",
        "- Do not request direct file access.",
    ]
    lines.extend(_report_write_contract_lines(expected_output_path, external_report_write_roots))
    if instruction.strip():
        lines.extend(["", "COORDINATOR INSTRUCTION:", instruction.strip()[:2000]])

    for record, handoff_label, handoff in source_handoffs:
        lines.extend(_report_metadata_lines(record))
        lines.extend(_handoff_section_lines(handoff_label, handoff))

    lines.extend([
        "",
        "OUTPUT CONTRACT (first non-empty line is authoritative):",
        "  REPORT_READY",
        "",
        "Status:",
        "  <PASS / PASS_WITH_NOTES / REQUEST_CHANGES / FAIL>",
        "",
        "Report:",
        "  <absolute .md path under allowed Ai-Report roots>",
        "",
        "Summary:",
        "  <short summary>",
    ])
    if output_handoff_instructions.strip():
        lines.extend(["", "REQUIRED HANDOFF BLOCKS IN YOUR REPORT:", output_handoff_instructions.strip()])
    return ReportPromptResult(ok=True, prompt="\n".join(lines))


def build_report_review_prompt(
    *,
    target_role: str,
    role_label: str,
    mode: str,
    project: str,
    phase: str,
    subject: str,
    task: str,
    source_reports: list[tuple[ReportRecord, str]],
    instruction: str = "",
    from_source: str = "agentchattr Coordinator",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
    handoff_labels: list[str] | None = None,
    output_handoff_instructions: str = "",
) -> ReportPromptResult:
    """Build next-agent prompt from report metadata and handoff blocks (not full reports)."""
    labels = handoff_labels or ["COORDINATOR HANDOFF BRIEF:"] * len(source_reports)
    source_handoffs = [
        (record, label, content)
        for (record, content), label in zip(source_reports, labels, strict=False)
    ]
    return build_handoff_dispatch_prompt(
        target_role=target_role,
        role_label=role_label,
        mode=mode,
        project=project,
        phase=phase,
        subject=subject,
        task=task,
        source_handoffs=source_handoffs,
        instruction=instruction,
        from_source=from_source,
        expected_output_path=expected_output_path,
        external_report_write_roots=external_report_write_roots,
        output_handoff_instructions=output_handoff_instructions,
    )


def build_reviewer_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    developer_record: ReportRecord,
    developer_handoff: str,
    agy_record: ReportRecord | None = None,
    agy_handoff: str = "",
    instruction: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> ReportPromptResult:
    sources: list[tuple[ReportRecord, str]] = [(developer_record, developer_handoff)]
    labels = ["DEVELOPER COORDINATOR HANDOFF FOR CODEX REVIEWER:"]
    if agy_record and agy_handoff:
        sources.append((agy_record, agy_handoff))
        labels.append("AGY COORDINATOR HANDOFF FOR CODEX REVIEWER:")
    return build_report_review_prompt(
        target_role="reviewer",
        role_label="Codex Reviewer",
        mode="Report review only",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Review using the developer and AGY coordinator handoff briefs below.\n"
            "Return PASS, PASS_WITH_NOTES, REQUEST_CHANGES, or FAIL.\n"
            "Save your review report to an allowed external .md path.\n"
            "Include COORDINATOR_HANDOFF_FOR_DEVELOPER_CORRECTION and "
            "COORDINATOR_HANDOFF_FOR_FINAL blocks in your report."
        ),
        source_reports=sources,
        handoff_labels=labels,
        instruction=instruction,
        expected_output_path=expected_output_path,
        external_report_write_roots=external_report_write_roots,
        output_handoff_instructions=(
            f"{HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN}\n"
            "<exact requested changes for developer if REQUEST_CHANGES; otherwise NONE>\n"
            f"{HANDOFF_FOR_DEVELOPER_CORRECTION_END}\n\n"
            f"{HANDOFF_FOR_FINAL_BEGIN}\n"
            "<final verdict summary and next gate recommendation>\n"
            f"{HANDOFF_FOR_FINAL_END}"
        ),
    )


def build_ui_lead_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    developer_record: ReportRecord,
    developer_handoff: str,
    instruction: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> ReportPromptResult:
    return build_report_review_prompt(
        target_role="ui_lead",
        role_label="AGY UI Lead",
        mode="Report review only",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Review the developer coordinator handoff brief from a UI/UX and cashier workflow perspective.\n"
            "Focus on UI/UX, cashier ergonomics, visual hierarchy, responsiveness, "
            "and implementation boundaries.\n"
            "Produce your own markdown UX review report."
        ),
        source_reports=[(developer_record, developer_handoff)],
        handoff_labels=["DEVELOPER COORDINATOR HANDOFF FOR AGY:"],
        instruction=instruction,
        expected_output_path=expected_output_path,
        external_report_write_roots=external_report_write_roots,
        output_handoff_instructions=(
            f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}\n"
            "<AGY verdict, UX risks, concerns Codex should consider, approval notes>\n"
            f"{HANDOFF_FOR_CODEX_REVIEWER_END}\n\n"
            "If Status is REQUEST_CHANGES, also include:\n"
            f"{HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN}\n"
            "<exact UX changes requested for developer>\n"
            f"{HANDOFF_FOR_DEVELOPER_CORRECTION_END}"
        ),
    )


def validate_initial_developer_preflight(
    policy: dict | None,
    *,
    prompt_memo_body: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> tuple[bool, str]:
    """Preflight before initial developer dispatch in report-orchestrated sessions."""
    if not (prompt_memo_body or "").strip():
        return False, "BLOCKER: developer initial prompt missing prompt memo"
    if not isinstance(policy, dict):
        return False, "BLOCKER: developer initial prompt missing source snapshots"
    read_paths = [
        p for p in (policy.get("read_paths") or [])
        if isinstance(p, str) and p.strip()
    ]
    if not read_paths:
        return False, "BLOCKER: developer initial prompt missing source snapshots"
    roots = list(external_report_write_roots or resolve_external_report_write_roots(policy))
    if not roots:
        return False, "BLOCKER: external report write permission not enabled"
    if list(policy.get("write_files") or []):
        return False, "BLOCKER: Twinpet workspace write allowlist must remain none"
    expected_paths: list[str] = []
    if (expected_output_path or "").strip():
        expected_paths.append(expected_output_path.strip())
    for path in policy.get("report_paths") or []:
        if isinstance(path, str) and path.strip():
            expected_paths.append(path.strip())
    ok, blocker = verify_report_write_permission(
        policy,
        expected_report_paths=expected_paths,
    )
    if not ok:
        return False, blocker
    return True, ""


def build_initial_developer_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    workspace_root: str,
    expected_head: str = "",
    read_paths: list[str],
    prompt_memo_body: str,
    instruction: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> ReportPromptResult:
    """Build first-turn developer prompt from Prompt Memo + snapshot paths (no prior report)."""
    lines = [
        "TO: Claude Developer",
        "FROM: agentchattr Coordinator",
        "ROLE: Developer / Technical Analyst",
        "MODE: read-only analysis with external report output",
        f"PROJECT: {project}",
        f"PHASE: {phase}",
        f"SUBJECT: {subject}",
        "",
        "WORKSPACE:",
        workspace_root,
    ]
    if expected_head:
        lines.extend(["", "EXPECTED HEAD:", expected_head])
    lines.extend([
        "",
        "READ-ONLY SNAPSHOTS:",
        "agentchattr injects AUTOMATED PRECHECK RESULTS and READ-ONLY FILE SNAPSHOT",
        "before this turn. Use injected snapshot content only; do not inspect the repo directly.",
        "",
        "Configured snapshot source paths:",
    ])
    for path in read_paths:
        lines.append(f"  - {path}")
    lines.extend([
        "",
        "PROMPT MEMO:",
        prompt_memo_body.strip(),
        "",
        "RULES:",
        "- You may write only the external markdown report under allowed Ai-Report roots.",
        "- You may not write inside the Twinpet workspace.",
        "- Use injected snapshots only.",
        "- Do not modify product source, tests, backend files, mobile files, tracker docs, or hidden agent folders.",
        "- Create the report folder if missing.",
        "- Do not emit <tool_call> XML or use Write/Read/Bash tools.",
        "- To write your external report, output exactly one REPORT_FILE_WRITE_BEGIN / REPORT_FILE_WRITE_END block.",
        "- The worker runtime validates the path and creates the .md file; then your output becomes REPORT_READY.",
        "- Do not claim REPORT_READY unless the runtime confirms the report file exists.",
        "- Do not use MCP tools. Do not call chat_read or chat_send.",
        "- Channel carries short status only; the report file is the source of truth.",
    ])
    roots = list(external_report_write_roots or [])
    if roots:
        lines.extend(["", "EXTERNAL REPORT WRITE ALLOWLIST:"])
        for root in roots:
            lines.append(f"- {root}")
    if expected_output_path:
        lines.extend([
            "",
            "REPORT OUTPUT:",
            "Write markdown report to:",
            expected_output_path,
        ])
    lines.extend([
        "",
        "If you cannot write the report file despite permission being configured, return exactly:",
        "REPORT_WRITE_FAILED",
        "",
        "Reason:",
        "<short reason>",
        "",
        "Expected report:",
        expected_output_path or "<path>",
        "",
        "Status:",
        "FAIL",
    ])
    if instruction.strip():
        lines.extend(["", "COORDINATOR INSTRUCTION:", instruction.strip()[:2000]])
    lines.extend([
        "",
        "OUTPUT CONTRACT (first non-empty line is authoritative):",
        "  REPORT_READY",
        "",
        "Status:",
        "  <PASS / PASS_WITH_NOTES / REQUEST_CHANGES / FAIL>",
        "",
        "Report:",
        "  <absolute .md path under allowed Ai-Report roots>",
        "",
        "Summary:",
        "  <short summary>",
        "",
        "Return REPORT_READY only after the report exists on disk.",
        "",
        "REQUIRED HANDOFF BLOCKS IN YOUR REPORT:",
        f"{HANDOFF_FOR_AGY_BEGIN}",
        "<UI/UX-relevant summary, cashier workflow risks, visual/layout concerns, exact questions for AGY>",
        f"{HANDOFF_FOR_AGY_END}",
        "",
        f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}",
        "<technical evidence for Codex review: data flow, payment payload, risks, unknowns, boundaries, review questions>",
        f"{HANDOFF_FOR_CODEX_REVIEWER_END}",
        "",
        "AGY handoff must include: current UI structure, visual hierarchy concerns, cashier workflow concerns,",
        "responsive/layout risks, keyboard/focus concerns, UI-only vs behavior-change boundary, questions for AGY.",
        "",
        "Codex reviewer handoff must include: payment data flow summary, real function/prop/state names,",
        "payload sent to checkout/parent, underpayment/overpayment behavior, split/non-cash behavior if present,",
        "confirm/async/double-submit risks, Firestore/receipt/stock implications if known,",
        "UNKNOWN FROM SNAPSHOT items, behavior changes requiring Product Owner approval,",
        "exact files allowed/off-limits for future UI work.",
        "",
        "If a required item is unknown, state: UNKNOWN FROM SNAPSHOT: <exact missing item>",
        "",
        "REPORT_FILE_WRITE_BEGIN/END FORMAT:",
        "  REPORT_FILE_WRITE_BEGIN",
        "  Path: <absolute .md under allowed Ai-Report roots>",
        "  Status: PASS",
        "  Summary: <short summary>",
        "  Next recommended role: coordinator",
        "  ---",
        "  <markdown report body>",
        "  REPORT_FILE_WRITE_END",
    ])
    return ReportPromptResult(ok=True, prompt="\n".join(lines))


def _handoff_max_chars(max_chars: int) -> int:
    return min(max_chars, DEFAULT_MAX_HANDOFF_CHARS)


def _validate_handoff_for_dispatch(
    handoff: str | None,
    *,
    owner_role: str,
    report_path: str,
    block_name: str,
    max_chars: int,
    expected_output_path: str = "",
) -> ReportPromptResult | str:
    """Return repair/rewrite result, or handoff text when valid."""
    if not is_handoff_sufficient(handoff):
        return build_handoff_repair_result(
            owner_role=owner_role,
            report_path=report_path,
            missing_block=block_name,
            reason=f"REQUEST_CHANGES: report missing {block_name}",
            expected_output_path=expected_output_path,
        )
    assert handoff is not None
    limit = _handoff_max_chars(max_chars)
    fits, total = handoff_fits_prompt(handoff, limit)
    if not fits:
        return build_oversized_handoff_rewrite_result(
            owner_role=owner_role,
            report_path=report_path,
            handoff_block=block_name,
            handoff_chars=total,
            max_chars=limit,
            expected_output_path=expected_output_path,
        )
    return handoff


def build_report_orchestrated_dispatch_prompt(
    *,
    role: str,
    report_records: list[dict[str, Any]],
    project: str,
    phase: str,
    subject: str,
    instruction: str = "",
    awaiting_developer_correction: bool = False,
    developer_correction_source: str = "",
    requires_agy: bool = False,
    max_chars: int = DEFAULT_MAX_REPORT_PROMPT_CHARS,
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
    prompt_memo_body: str = "",
    policy: dict[str, Any] | None = None,
) -> ReportPromptResult:
    """Build next-worker prompt from stored report records (not channel history)."""
    dev = get_report_for_role(report_records, "developer")
    agy = get_report_for_role(report_records, "ui_lead")
    reviewer = get_report_for_role(report_records, "reviewer")

    if role == "developer" and not awaiting_developer_correction:
        ok, blocker = validate_initial_developer_preflight(
            policy,
            prompt_memo_body=prompt_memo_body,
            expected_output_path=expected_output_path,
            external_report_write_roots=external_report_write_roots,
        )
        if not ok:
            return ReportPromptResult(ok=False, blocker=blocker)
        workspace = (policy or {}).get("workspace") or {}
        return build_initial_developer_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            workspace_root=str(workspace.get("root") or ""),
            expected_head=str(workspace.get("expected_head") or ""),
            read_paths=[
                p for p in ((policy or {}).get("read_paths") or [])
                if isinstance(p, str) and p.strip()
            ],
            prompt_memo_body=prompt_memo_body,
            instruction=instruction,
            expected_output_path=expected_output_path,
            external_report_write_roots=external_report_write_roots,
        )

    if role == "ui_lead":
        if not dev:
            return ReportPromptResult(
                ok=False,
                blocker="BLOCKER: ui_lead context missing developer report",
            )
        ok, dev_content, _ = load_report_content(dev)
        if not ok:
            return ReportPromptResult(ok=False, blocker=dev_content)
        agy_handoff = extract_handoff_block(
            dev_content, HANDOFF_FOR_AGY_BEGIN, HANDOFF_FOR_AGY_END,
        )
        validated = _validate_handoff_for_dispatch(
            agy_handoff,
            owner_role="developer",
            report_path=dev.path,
            block_name=f"{HANDOFF_FOR_AGY_BEGIN} / {HANDOFF_FOR_AGY_END}",
            max_chars=max_chars,
            expected_output_path=dev.path,
        )
        if isinstance(validated, ReportPromptResult):
            return validated
        return build_ui_lead_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            developer_record=dev,
            developer_handoff=validated,
            instruction=instruction,
            expected_output_path=expected_output_path,
            external_report_write_roots=external_report_write_roots,
        )

    if role == "reviewer":
        if not dev:
            return ReportPromptResult(
                ok=False,
                blocker="BLOCKER: reviewer context missing developer analysis",
            )
        ok, dev_content, _ = load_report_content(dev)
        if not ok:
            return ReportPromptResult(ok=False, blocker=dev_content)
        dev_handoff = extract_handoff_block(
            dev_content, HANDOFF_FOR_CODEX_REVIEWER_BEGIN, HANDOFF_FOR_CODEX_REVIEWER_END,
        )
        validated_dev = _validate_handoff_for_dispatch(
            dev_handoff,
            owner_role="developer",
            report_path=dev.path,
            block_name=f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN} / {HANDOFF_FOR_CODEX_REVIEWER_END}",
            max_chars=max_chars,
            expected_output_path=dev.path,
        )
        if isinstance(validated_dev, ReportPromptResult):
            return validated_dev
        agy_handoff = ""
        if agy:
            agy_ok, agy_content, _ = load_report_content(agy)
            if not agy_ok:
                return ReportPromptResult(ok=False, blocker=agy_content)
            agy_handoff = extract_handoff_block(
                agy_content,
                HANDOFF_FOR_CODEX_REVIEWER_BEGIN,
                HANDOFF_FOR_CODEX_REVIEWER_END,
            )
            validated_agy = _validate_handoff_for_dispatch(
                agy_handoff,
                owner_role="ui_lead",
                report_path=agy.path,
                block_name=f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN} / {HANDOFF_FOR_CODEX_REVIEWER_END}",
                max_chars=max_chars,
                expected_output_path=agy.path,
            )
            if isinstance(validated_agy, ReportPromptResult):
                return validated_agy
            agy_handoff = validated_agy
        return build_reviewer_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            developer_record=dev,
            developer_handoff=validated_dev,
            agy_record=agy,
            agy_handoff=agy_handoff,
            instruction=instruction,
            expected_output_path=expected_output_path,
            external_report_write_roots=external_report_write_roots,
        )

    if role == "developer" and awaiting_developer_correction:
        correction_source = (developer_correction_source or "").strip().lower()
        if correction_source == "ui_lead" and agy:
            agy_ok, agy_content, _ = load_report_content(agy)
            if not agy_ok:
                return ReportPromptResult(ok=False, blocker=agy_content)
            correction_handoff = extract_handoff_block(
                agy_content,
                HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN,
                HANDOFF_FOR_DEVELOPER_CORRECTION_END,
            )
            if agy.status in ("REQUEST_CHANGES", "FAIL"):
                validated = _validate_handoff_for_dispatch(
                    correction_handoff,
                    owner_role="ui_lead",
                    report_path=agy.path,
                    block_name=f"{HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN} / {HANDOFF_FOR_DEVELOPER_CORRECTION_END}",
                    max_chars=max_chars,
                    expected_output_path=agy.path,
                )
                if isinstance(validated, ReportPromptResult):
                    return validated
                correction_handoff = validated
            return build_developer_ui_lead_correction_report_prompt(
                project=project,
                phase=phase,
                subject=subject,
                agy_record=agy,
                correction_handoff=correction_handoff or "NONE",
                developer_record=dev,
                prompt_memo_body=prompt_memo_body,
                instruction=instruction,
                expected_output_path=expected_output_path,
                external_report_write_roots=external_report_write_roots,
            )
        if reviewer:
            rev_ok, rev_content, _ = load_report_content(reviewer)
            if not rev_ok:
                return ReportPromptResult(ok=False, blocker=rev_content)
            correction_handoff = extract_handoff_block(
                rev_content,
                HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN,
                HANDOFF_FOR_DEVELOPER_CORRECTION_END,
            )
            if reviewer.status in ("REQUEST_CHANGES", "FAIL"):
                validated = _validate_handoff_for_dispatch(
                    correction_handoff,
                    owner_role="reviewer",
                    report_path=reviewer.path,
                    block_name=f"{HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN} / {HANDOFF_FOR_DEVELOPER_CORRECTION_END}",
                    max_chars=max_chars,
                    expected_output_path=reviewer.path,
                )
                if isinstance(validated, ReportPromptResult):
                    return validated
                correction_handoff = validated
            return build_developer_correction_report_prompt(
                project=project,
                phase=phase,
                subject=subject,
                reviewer_record=reviewer,
                correction_handoff=correction_handoff or "NONE",
                developer_record=dev,
                instruction=instruction,
                expected_output_path=expected_output_path,
                external_report_write_roots=external_report_write_roots,
            )

    return ReportPromptResult(
        ok=False,
        blocker=f"BLOCKER: no report-orchestrated prompt available for role={role}",
    )


def build_developer_ui_lead_correction_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    agy_record: ReportRecord,
    correction_handoff: str,
    developer_record: ReportRecord | None = None,
    prompt_memo_body: str = "",
    instruction: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> ReportPromptResult:
    sources: list[tuple[ReportRecord, str]] = [
        (agy_record, correction_handoff),
    ]
    labels = ["AGY CORRECTION HANDOFF FOR DEVELOPER:"]
    if developer_record:
        sources.append((developer_record, "(see prior developer report on disk; path listed below)"))
        labels.append("PRIOR DEVELOPER REPORT METADATA:")
    result = build_report_review_prompt(
        target_role="developer",
        role_label="Claude Developer",
        mode="Report correction from UI Lead review",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Address AGY UX findings using the correction handoff brief below.\n"
            "Update your analysis report; do not modify Twinpet product source files.\n"
            "Refresh COORDINATOR_HANDOFF_FOR_AGY and COORDINATOR_HANDOFF_FOR_CODEX_REVIEWER blocks."
        ),
        source_reports=sources,
        handoff_labels=labels,
        instruction=instruction,
        expected_output_path=expected_output_path,
        external_report_write_roots=external_report_write_roots,
        output_handoff_instructions=(
            f"{HANDOFF_FOR_AGY_BEGIN}\n"
            "<updated UI/UX handoff for AGY>\n"
            f"{HANDOFF_FOR_AGY_END}\n\n"
            f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}\n"
            "<updated technical handoff for Codex reviewer>\n"
            f"{HANDOFF_FOR_CODEX_REVIEWER_END}"
        ),
    )
    if prompt_memo_body.strip():
        result = ReportPromptResult(
            ok=result.ok,
            prompt=f"{result.prompt}\n\nORIGINAL PROMPT MEMO (reference only):\n{prompt_memo_body.strip()[:2000]}",
            blocker=result.blocker,
            dispatch_role=result.dispatch_role,
        )
    return result


def build_developer_correction_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    reviewer_record: ReportRecord,
    correction_handoff: str,
    developer_record: ReportRecord | None = None,
    instruction: str = "",
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
) -> ReportPromptResult:
    sources: list[tuple[ReportRecord, str]] = [
        (reviewer_record, correction_handoff),
    ]
    labels = ["CODEX REVIEWER CORRECTION HANDOFF FOR DEVELOPER:"]
    if developer_record:
        sources.append((developer_record, "(see prior developer report on disk; path listed below)"))
        labels.append("PRIOR DEVELOPER REPORT METADATA:")
    return build_report_review_prompt(
        target_role="developer",
        role_label="Claude Developer",
        mode="Report correction",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Address reviewer findings using the correction handoff brief below.\n"
            "Update your analysis report; do not modify Twinpet product source files.\n"
            "Refresh COORDINATOR_HANDOFF_FOR_AGY and COORDINATOR_HANDOFF_FOR_CODEX_REVIEWER blocks."
        ),
        source_reports=sources,
        handoff_labels=labels,
        instruction=instruction,
        expected_output_path=expected_output_path,
        external_report_write_roots=external_report_write_roots,
        output_handoff_instructions=(
            f"{HANDOFF_FOR_AGY_BEGIN}\n"
            "<updated UI/UX handoff for AGY>\n"
            f"{HANDOFF_FOR_AGY_END}\n\n"
            f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}\n"
            "<updated technical handoff for Codex reviewer>\n"
            f"{HANDOFF_FOR_CODEX_REVIEWER_END}"
        ),
    )
