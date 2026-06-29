"""Report-orchestrated coordinator flow — reports as source of truth.

Workers emit short REPORT_READY status + path (or REPORT_BEGIN/END for inline save).
Coordinator reads allowed .md reports and builds next-agent prompts from file content.
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

_TWINPET_REPO_MARKERS = (
    r"twinpet-pos\src",
    r"twinpet-pos/tests",
    r"twinpet-pos\functions",
    r"twinpet-pos/src",
)

_REPORT_READY_RE = re.compile(r"^\s*REPORT_READY\s*$", re.IGNORECASE)

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


def report_content_fits_prompt(content: str, max_chars: int) -> tuple[bool, int]:
    total = len(content or "")
    return total <= max_chars, total


def ingest_worker_report_output(
    role: str,
    text: str,
    *,
    allowed_roots: list[str] | None = None,
    expected_report_paths: list[str] | None = None,
    max_prompt_chars: int = DEFAULT_MAX_REPORT_PROMPT_CHARS,
) -> ReportIngestResult:
    """Parse REPORT_READY or inline REPORT_BEGIN/END, validate, read, record."""
    parsed = parse_report_ready(text)
    report_path = ""

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
            summary="inline REPORT_BEGIN/END saved by coordinator",
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

    fits, total_chars = report_content_fits_prompt(content_or_err, max_prompt_chars)
    if not fits:
        return ReportIngestResult(
            ok=False,
            blocker=(
                "BLOCKER: report too large for next prompt\n\n"
                f"report_chars={total_chars}\n"
                f"max_chars={max_prompt_chars}\n\n"
                "Required action:\n"
                "Ask the report owner to rewrite the report into a review-ready bounded report."
            ),
        )

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


def build_report_owner_rewrite_prompt(*, role: str, report_path: str, report_chars: int, max_chars: int) -> str:
    return (
        f"TO: {role}\n"
        "FROM: agentchattr Coordinator\n"
        "ROLE: Report owner\n"
        "MODE: Report rewrite\n\n"
        f"Your report is too large for reviewer handoff ({report_chars} chars; max {max_chars}).\n"
        f"Path: {report_path}\n\n"
        "Rewrite it into a review-ready report that preserves all critical findings "
        "and removes only redundant prose.\n"
        "Do not omit unknowns, risks, data flow, or approval-required behavior changes.\n"
        "Return REPORT_READY with the updated .md path when done."
    )


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
) -> ReportPromptResult:
    """Build next-agent prompt from loaded report file content."""
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
        "- Review the report content supplied below only.",
        "- Do not inspect repository files.",
        "- Do not run shell.",
        "- Do not request direct file access.",
        "- Produce your own markdown report at an allowed external Ai-Report path.",
        "- Return REPORT_READY with Status, Report path, and Summary when complete.",
    ]
    if instruction.strip():
        lines.extend(["", "COORDINATOR INSTRUCTION:", instruction.strip()[:2000]])

    for record, content in source_reports:
        lines.extend([
            "",
            "SOURCE REPORT:",
            f"Path: {record.path}",
            f"SHA256: {record.sha256}",
            f"Size: {record.size_bytes} bytes",
            f"Produced by: {record.role}",
            f"Status: {record.status}",
            "",
            "REPORT CONTENT:",
            "---",
            content,
            "---",
        ])

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
    return ReportPromptResult(ok=True, prompt="\n".join(lines))


def build_reviewer_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    developer_record: ReportRecord,
    developer_content: str,
    agy_record: ReportRecord | None = None,
    agy_content: str = "",
    instruction: str = "",
) -> ReportPromptResult:
    sources: list[tuple[ReportRecord, str]] = [(developer_record, developer_content)]
    if agy_record and agy_content:
        sources.append((agy_record, agy_content))
    return build_report_review_prompt(
        target_role="reviewer",
        role_label="Codex Reviewer",
        mode="Report review only",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Review the developer report and AGY UX report (if supplied).\n"
            "Return PASS, PASS_WITH_NOTES, REQUEST_CHANGES, or FAIL.\n"
            "Save your review report to an allowed external .md path."
        ),
        source_reports=sources,
        instruction=instruction,
    )


def build_ui_lead_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    developer_record: ReportRecord,
    developer_content: str,
    instruction: str = "",
) -> ReportPromptResult:
    return build_report_review_prompt(
        target_role="ui_lead",
        role_label="AGY UI Lead",
        mode="Report review only",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Review the supplied developer report from a UI/UX and cashier workflow perspective.\n"
            "Focus on UI/UX, cashier ergonomics, visual hierarchy, responsiveness, "
            "and implementation boundaries.\n"
            "Produce your own markdown UX review report."
        ),
        source_reports=[(developer_record, developer_content)],
        instruction=instruction,
    )


def build_report_orchestrated_dispatch_prompt(
    *,
    role: str,
    report_records: list[dict[str, Any]],
    project: str,
    phase: str,
    subject: str,
    instruction: str = "",
    awaiting_developer_correction: bool = False,
    requires_agy: bool = False,
    max_chars: int = DEFAULT_MAX_REPORT_PROMPT_CHARS,
) -> ReportPromptResult:
    """Build next-worker prompt from stored report records (not channel history)."""
    dev = get_report_for_role(report_records, "developer")
    agy = get_report_for_role(report_records, "ui_lead")
    reviewer = get_report_for_role(report_records, "reviewer")

    if role == "ui_lead":
        if not dev:
            return ReportPromptResult(
                ok=False,
                blocker="BLOCKER: reviewer context missing developer analysis",
            )
        ok, content, _ = load_report_content(dev)
        if not ok:
            return ReportPromptResult(ok=False, blocker=content)
        fits, total = report_content_fits_prompt(content, max_chars)
        if not fits:
            return ReportPromptResult(
                ok=False,
                blocker=(
                    "BLOCKER: report too large for next prompt\n\n"
                    f"report_chars={total}\nmax_chars={max_chars}"
                ),
            )
        return build_ui_lead_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            developer_record=dev,
            developer_content=content,
            instruction=instruction,
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
        agy_content = ""
        if agy:
            agy_ok, agy_content, _ = load_report_content(agy)
            if not agy_ok:
                return ReportPromptResult(ok=False, blocker=agy_content)
        combined_len = len(dev_content) + len(agy_content)
        if combined_len > max_chars:
            return ReportPromptResult(
                ok=False,
                blocker=(
                    "BLOCKER: report too large for next prompt\n\n"
                    f"report_chars={combined_len}\nmax_chars={max_chars}"
                ),
            )
        return build_reviewer_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            developer_record=dev,
            developer_content=dev_content,
            agy_record=agy,
            agy_content=agy_content,
            instruction=instruction,
        )

    if role == "developer" and awaiting_developer_correction and reviewer:
        ok, rev_content, _ = load_report_content(reviewer)
        if not ok:
            return ReportPromptResult(ok=False, blocker=rev_content)
        fits, total = report_content_fits_prompt(rev_content, max_chars)
        if not fits:
            return ReportPromptResult(
                ok=False,
                blocker=(
                    "BLOCKER: report too large for next prompt\n\n"
                    f"report_chars={total}\nmax_chars={max_chars}"
                ),
            )
        return build_developer_correction_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            reviewer_record=reviewer,
            reviewer_content=rev_content,
            instruction=instruction,
        )

    return ReportPromptResult(
        ok=False,
        blocker=f"BLOCKER: no report-orchestrated prompt available for role={role}",
    )


def build_developer_correction_report_prompt(
    *,
    project: str,
    phase: str,
    subject: str,
    reviewer_record: ReportRecord,
    reviewer_content: str,
    instruction: str = "",
) -> ReportPromptResult:
    return build_report_review_prompt(
        target_role="developer",
        role_label="Claude Developer",
        mode="Report correction",
        project=project,
        phase=phase,
        subject=subject,
        task=(
            "Address reviewer findings using the supplied reviewer report.\n"
            "Update your analysis report; do not modify Twinpet product source files."
        ),
        source_reports=[(reviewer_record, reviewer_content)],
        instruction=instruction,
    )
