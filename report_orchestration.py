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
DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE = 2
DEFAULT_MAX_HANDOFF_REPAIR_REPORT_CONTEXT_CHARS = 60_000
DEFAULT_MAX_HANDOFF_REPAIR_PROMPT_CHARS = 75_000
DEFAULT_HANDOFF_REPAIR_EXCERPT_FRONT_CHARS = 8_000
DEFAULT_HANDOFF_REPAIR_EXCERPT_TAIL_CHARS = 20_000
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
    handoff_repair: bool = False
    handoff_repair_owner_role: str = ""
    handoff_repair_missing_blocks: list[str] = field(default_factory=list)
    handoff_repair_invalid_blocks: list[str] = field(default_factory=list)
    handoff_repair_report_context_chars: int = 0
    handoff_repair_report_context_injected: bool = False
    handoff_repair_report_hash: str = ""
    handoff_repair_report_chars: int = 0
    handoff_validation_failed: bool = False
    intended_next_role: str = ""
    owner_role: str = ""
    owner_agent: str = ""
    report_path: str = ""
    report_hash_before: str = ""
    report_hash_after: str = ""
    report_chars: int = 0
    found_marker_names: list[str] = field(default_factory=list)
    parser_expected_marker_names: list[str] = field(default_factory=list)
    repair_round: int = 0
    max_repair_rounds: int = DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE
    using_cached_report: bool = False
    report_reread_after_repair: bool = False
    handoff_validation_reason: str = ""
    refreshed_report_records: list[dict[str, Any]] | None = None


PARSER_EXPECTED_HANDOFF_MARKER_NAMES: tuple[str, ...] = (
    HANDOFF_FOR_AGY_BEGIN,
    HANDOFF_FOR_AGY_END,
    HANDOFF_FOR_CODEX_REVIEWER_BEGIN,
    HANDOFF_FOR_CODEX_REVIEWER_END,
    HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN,
    HANDOFF_FOR_DEVELOPER_CORRECTION_END,
    HANDOFF_FOR_FINAL_BEGIN,
    HANDOFF_FOR_FINAL_END,
)


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


_REQUIRED_HANDOFF_MARKERS_BY_OWNER: dict[str, list[tuple[str, str]]] = {
    "developer": [
        (HANDOFF_FOR_AGY_BEGIN, HANDOFF_FOR_AGY_END),
        (HANDOFF_FOR_CODEX_REVIEWER_BEGIN, HANDOFF_FOR_CODEX_REVIEWER_END),
    ],
    "ui_lead": [
        (HANDOFF_FOR_CODEX_REVIEWER_BEGIN, HANDOFF_FOR_CODEX_REVIEWER_END),
    ],
    "reviewer": [
        (HANDOFF_FOR_FINAL_BEGIN, HANDOFF_FOR_FINAL_END),
    ],
}

_CORRECTION_HANDOFF_MARKER_PAIR = (
    HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN,
    HANDOFF_FOR_DEVELOPER_CORRECTION_END,
)


def _format_required_handoff_markers(
    owner_role: str,
    *,
    include_correction_handoff: bool = False,
) -> str:
    pairs = list(_REQUIRED_HANDOFF_MARKERS_BY_OWNER.get(owner_role, []))
    if include_correction_handoff:
        pairs.append(_CORRECTION_HANDOFF_MARKER_PAIR)
    lines: list[str] = []
    for begin, end in pairs:
        lines.extend([begin, "...", end, ""])
    return "\n".join(lines).rstrip()


_ALL_HANDOFF_MARKER_PAIRS: tuple[tuple[str, str], ...] = (
    (HANDOFF_FOR_AGY_BEGIN, HANDOFF_FOR_AGY_END),
    (HANDOFF_FOR_CODEX_REVIEWER_BEGIN, HANDOFF_FOR_CODEX_REVIEWER_END),
    (HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN, HANDOFF_FOR_DEVELOPER_CORRECTION_END),
    (HANDOFF_FOR_FINAL_BEGIN, HANDOFF_FOR_FINAL_END),
)


def _extract_markdown_headings(content: str, *, max_headings: int = 40) -> str:
    headings: list[str] = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.append(stripped)
        if len(headings) >= max_headings:
            break
    return "\n".join(headings)


def build_bounded_handoff_repair_report_context(
    content: str,
    *,
    max_chars: int = DEFAULT_MAX_HANDOFF_REPAIR_REPORT_CONTEXT_CHARS,
    front_chars: int = DEFAULT_HANDOFF_REPAIR_EXCERPT_FRONT_CHARS,
    tail_chars: int = DEFAULT_HANDOFF_REPAIR_EXCERPT_TAIL_CHARS,
) -> tuple[str, str]:
    """Return bounded report body for handoff repair and mode: full|excerpt."""
    body = content or ""
    total = len(body)
    if total <= max_chars:
        return body, "full"
    parts = [
        "=== REPORT EXCERPT (original exceeds repair context cap; preserve all analysis when rewriting) ===",
        f"Original report size: {total} chars (cap {max_chars}).",
        "",
        "--- FRONT MATTER ---",
        body[:front_chars],
    ]
    for begin, end in _ALL_HANDOFF_MARKER_PAIRS:
        block = extract_handoff_block(body, begin, end)
        if block is not None:
            parts.extend([
                "",
                f"--- EXISTING {begin} (repair if present/invalid) ---",
                block,
                f"--- {end} ---",
            ])
    headings = _extract_markdown_headings(body)
    if headings:
        parts.extend(["", "--- SECTION HEADINGS ---", headings])
    parts.extend(["", "--- REPORT TAIL ---", body[-tail_chars:]])
    excerpt = "\n".join(parts)
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 64].rstrip() + "\n...[repair context truncated to cap]..."
    return excerpt, "excerpt"


def format_handoff_repair_context_blocker(
    *,
    reason: str,
    report_path: str,
    report_chars: int = 0,
    report_hash: str = "",
    report_context_chars: int = 0,
    prompt_chars: int = 0,
    missing_blocks: list[str] | None = None,
    invalid_blocks: list[str] | None = None,
) -> str:
    """BLOCKER when handoff repair cannot include safe bounded report context."""
    title = (
        "BLOCKER: handoff repair report context too large"
        if "too large" in reason.lower()
        else "BLOCKER: handoff repair report context unavailable"
    )
    lines = [
        title,
        f"- reason: {reason}",
        f"- report_path: {report_path}",
        f"- report_chars: {report_chars}",
        f"- report_hash: {report_hash or '(none)'}",
        f"- report_context_chars: {report_context_chars}",
        f"- prompt_chars: {prompt_chars}",
        f"- missing_blocks: {', '.join(missing_blocks or []) or '(none)'}",
        f"- invalid_blocks: {', '.join(invalid_blocks or []) or '(none)'}",
        "- snapshots_injected: false",
        "- report_context_injected: false",
    ]
    return "\n".join(lines)


def format_handoff_repair_limit_blocker(
    *,
    role: str,
    owner_agent: str,
    missing_blocks: list[str],
    invalid_blocks: list[str],
    repair_rounds: int,
    report_path: str,
    report_chars: int,
    last_report_status: str,
    prompt_chars: int,
    snapshots_injected: bool,
    workspace_profile: str,
    workspace_mode: str,
    session_id: int | str,
    channel: str,
    template: str,
) -> str:
    """BLOCKER when handoff repair round budget is exhausted."""
    lines = [
        "BLOCKER: coordinator handoff repair limit exceeded",
        f"- role: {role}",
        f"- owner_agent: {owner_agent}",
        f"- missing_blocks: {', '.join(missing_blocks) or '(none)'}",
        f"- invalid_blocks: {', '.join(invalid_blocks) or '(none)'}",
        f"- repair_rounds: {repair_rounds}",
        f"- report_path: {report_path}",
        f"- report_chars: {report_chars}",
        f"- last_report_status: {last_report_status or '(none)'}",
        f"- prompt_chars: {prompt_chars}",
        f"- snapshots_injected: {'true' if snapshots_injected else 'false'}",
        f"- workspace_profile: {workspace_profile or '(none)'}",
        f"- workspace_mode: {workspace_mode or '(none)'}",
        f"- session_id: {session_id}",
        f"- channel: {channel}",
        f"- template: {template or '(unknown)'}",
    ]
    return "\n".join(lines)


def build_handoff_repair_prompt(
    *,
    owner_role: str,
    report_path: str,
    missing_blocks: list[str],
    invalid_blocks: list[str] | None = None,
    reason: str = "",
    expected_output_path: str = "",
    report_sha256: str = "",
    report_size_bytes: int = 0,
    last_report_status: str = "",
    include_correction_handoff: bool = False,
    report_context: str = "",
    report_context_mode: str = "full",
) -> str:
    """Ask report owner to add or repair required coordinator handoff blocks."""
    role_label = _REPORT_OWNER_ROLE_LABELS.get(owner_role, owner_role)
    target_path = expected_output_path or report_path
    missing = [b for b in (missing_blocks or []) if b]
    invalid = [b for b in (invalid_blocks or []) if b]
    issue_lines: list[str] = []
    if reason.strip():
        issue_lines.append(reason.strip())
    if missing:
        issue_lines.append("Missing blocks:")
        issue_lines.extend(f"- {name}" for name in missing)
    if invalid:
        issue_lines.append("Invalid or insufficient blocks:")
        issue_lines.extend(f"- {name}" for name in invalid)
    if not issue_lines:
        detail = reason.strip() or "Required coordinator handoff blocks missing or invalid."
        issue_lines = [detail]
    meta_lines = [f"Your previous report exists at:\n{report_path}"]
    if report_sha256:
        meta_lines.append(f"Report hash: {report_sha256}")
    if report_size_bytes:
        meta_lines.append(f"Report size: {report_size_bytes} bytes")
    if last_report_status:
        meta_lines.append(f"Last report status: {last_report_status}")
    marker_section = _format_required_handoff_markers(
        owner_role,
        include_correction_handoff=include_correction_handoff,
    )
    context_section = ""
    if report_context.strip():
        mode_label = report_context_mode if report_context_mode in ("full", "excerpt") else "bounded"
        context_section = (
            f"\nCURRENT REPORT CONTEXT ({mode_label}, tools disabled — do not read files):\n"
            f"{report_context.rstrip()}\n"
        )
    return (
        f"TO: {role_label}\n"
        "FROM: agentchattr Coordinator\n"
        f"ROLE: {role_label}\n"
        "MODE: Handoff block repair\n\n"
        f"{chr(10).join(meta_lines)}\n\n"
        "The report is missing or has invalid coordinator handoff blocks:\n"
        f"{chr(10).join(issue_lines)}\n\n"
        "You are running with tools disabled.\n"
        "Do not use tool calls.\n"
        "Do not emit <tool_call> XML.\n"
        "Do not attempt to read files.\n"
        "The current report content/excerpt is provided below.\n"
        "Use only the provided report context.\n"
        "If insufficient, return exactly:\n"
        "BLOCKER: insufficient report context for handoff repair\n\n"
        "Rewrite the same report file using REPORT_FILE_WRITE_BEGIN / REPORT_FILE_WRITE_END.\n"
        "Keep your existing analysis.\n"
        "Do not reanalyze source files.\n"
        "Do not request source snapshots.\n"
        "Do not modify the Twinpet repo.\n"
        "Add or fix only the required coordinator handoff blocks.\n\n"
        "Required exact markers:\n\n"
        f"{marker_section}\n"
        f"{context_section}\n"
        "REPORT_FILE_WRITE_BEGIN / REPORT_FILE_WRITE_END FORMAT:\n"
        "  REPORT_FILE_WRITE_BEGIN\n"
        f"  Path: {target_path}\n"
        "  Status: PASS_WITH_NOTES\n"
        "  Summary: <short summary>\n"
        "  Next recommended role: coordinator\n"
        "  ---\n"
        "  <markdown report body with required handoff blocks>\n"
        "  REPORT_FILE_WRITE_END\n\n"
        "Return REPORT_READY after the runtime confirms the report file exists.\n"
    )


def _annotate_handoff_repair_result(
    result: ReportPromptResult,
    *,
    intended_next_role: str,
    owner_role: str,
    report_path: str,
    report_hash_before: str,
    report_hash_after: str,
    report_chars: int,
    missing_blocks: list[str],
    invalid_blocks: list[str],
    found_marker_names: list[str],
    using_cached_report: bool = False,
    report_reread_after_repair: bool = False,
    reason: str = "",
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE,
) -> ReportPromptResult:
    result.handoff_validation_failed = True
    result.intended_next_role = intended_next_role
    result.owner_role = owner_role
    result.report_path = report_path
    result.report_hash_before = report_hash_before
    result.report_hash_after = report_hash_after
    result.report_chars = report_chars
    result.found_marker_names = list(found_marker_names)
    result.parser_expected_marker_names = list(PARSER_EXPECTED_HANDOFF_MARKER_NAMES)
    result.repair_round = repair_round
    result.max_repair_rounds = max_repair_rounds
    result.using_cached_report = using_cached_report
    result.report_reread_after_repair = report_reread_after_repair
    result.handoff_validation_reason = reason.strip()
    if missing_blocks:
        result.handoff_repair_missing_blocks = list(missing_blocks)
    if invalid_blocks:
        result.handoff_repair_invalid_blocks = list(invalid_blocks)
    return result


def build_handoff_repair_result(
    *,
    owner_role: str,
    report_path: str,
    missing_blocks: list[str] | None = None,
    invalid_blocks: list[str] | None = None,
    reason: str = "",
    expected_output_path: str = "",
    report_sha256: str = "",
    report_size_bytes: int = 0,
    last_report_status: str = "",
    include_correction_handoff: bool = False,
    allowed_roots: list[str] | None = None,
    report_content: str | None = None,
    max_report_context_chars: int = DEFAULT_MAX_HANDOFF_REPAIR_REPORT_CONTEXT_CHARS,
    max_prompt_chars: int = DEFAULT_MAX_HANDOFF_REPAIR_PROMPT_CHARS,
    intended_next_role: str = "",
    report_hash_before: str = "",
    using_cached_report: bool = False,
    report_reread_after_repair: bool = False,
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE,
    found_marker_names: list[str] | None = None,
) -> ReportPromptResult:
    """Route back to report owner to repair missing/insufficient handoff blocks."""
    path = expected_output_path or report_path
    missing = list(missing_blocks or [])
    invalid = list(invalid_blocks or [])
    try:
        resolved = Path(str(path).strip()).resolve()
    except OSError as exc:
        blocker = format_handoff_repair_context_blocker(
            reason=str(exc),
            report_path=path,
            missing_blocks=missing,
            invalid_blocks=invalid,
        )
        return ReportPromptResult(ok=False, blocker=blocker, dispatch_role=owner_role)

    content = report_content
    digest = report_sha256
    size_bytes = report_size_bytes

    if content is None:
        ok_path, path_reason, validated = validate_report_path(
            str(resolved),
            allowed_roots=allowed_roots,
        )
        if not ok_path:
            blocker = format_handoff_repair_context_blocker(
                reason=path_reason,
                report_path=str(resolved),
                missing_blocks=missing,
                invalid_blocks=invalid,
            )
            return ReportPromptResult(ok=False, blocker=blocker, dispatch_role=owner_role)
        if validated is not None:
            resolved = validated
        ok_read, content, digest, size_bytes = read_report_file(resolved)
        if not ok_read:
            blocker = format_handoff_repair_context_blocker(
                reason=content,
                report_path=str(resolved),
                missing_blocks=missing,
                invalid_blocks=invalid,
            )
            return ReportPromptResult(ok=False, blocker=blocker, dispatch_role=owner_role)
    elif not resolved.is_file():
        blocker = format_handoff_repair_context_blocker(
            reason=f"BLOCKER: report file not found: {resolved}",
            report_path=str(resolved),
            missing_blocks=missing,
            invalid_blocks=invalid,
        )
        return ReportPromptResult(ok=False, blocker=blocker, dispatch_role=owner_role)
    report_context, context_mode = build_bounded_handoff_repair_report_context(
        content,
        max_chars=max_report_context_chars,
    )
    prompt = build_handoff_repair_prompt(
        owner_role=owner_role,
        report_path=str(resolved),
        missing_blocks=missing,
        invalid_blocks=invalid,
        reason=reason,
        expected_output_path=str(resolved),
        report_sha256=digest,
        report_size_bytes=size_bytes,
        last_report_status=last_report_status,
        include_correction_handoff=include_correction_handoff,
        report_context=report_context,
        report_context_mode=context_mode,
    )
    if len(prompt) > max_prompt_chars:
        blocker = format_handoff_repair_context_blocker(
            reason=(
                f"repair prompt {len(prompt)} chars exceeds cap {max_prompt_chars}"
            ),
            report_path=str(resolved),
            report_chars=len(content),
            report_hash=digest,
            report_context_chars=len(report_context),
            prompt_chars=len(prompt),
            missing_blocks=missing,
            invalid_blocks=invalid,
        )
        return ReportPromptResult(ok=False, blocker=blocker, dispatch_role=owner_role)
    return _annotate_handoff_repair_result(
        ReportPromptResult(
            ok=True,
            prompt=prompt,
            dispatch_role=owner_role,
            handoff_repair=True,
            handoff_repair_owner_role=owner_role,
            handoff_repair_missing_blocks=missing,
            handoff_repair_invalid_blocks=invalid,
            handoff_repair_report_context_chars=len(report_context),
            handoff_repair_report_context_injected=bool(report_context.strip()),
            handoff_repair_report_hash=digest,
            handoff_repair_report_chars=len(content),
        ),
        intended_next_role=intended_next_role,
        owner_role=owner_role,
        report_path=str(resolved),
        report_hash_before=report_hash_before or digest,
        report_hash_after=digest,
        report_chars=len(content),
        missing_blocks=missing,
        invalid_blocks=invalid,
        found_marker_names=found_marker_names or scan_found_handoff_marker_names(content),
        using_cached_report=using_cached_report,
        report_reread_after_repair=report_reread_after_repair,
        reason=reason,
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
    )


def build_oversized_handoff_rewrite_result(
    *,
    owner_role: str,
    report_path: str,
    handoff_block: str,
    handoff_chars: int,
    max_chars: int,
    expected_output_path: str = "",
    report_sha256: str = "",
    report_size_bytes: int = 0,
    last_report_status: str = "",
    allowed_roots: list[str] | None = None,
    report_content: str | None = None,
) -> ReportPromptResult:
    """Route report owner to shrink an oversized handoff block (not the full report)."""
    return build_handoff_repair_result(
        owner_role=owner_role,
        report_path=report_path,
        missing_blocks=[],
        invalid_blocks=[handoff_block],
        reason=(
            f"Handoff block too large for dispatch ({handoff_chars} chars; max {max_chars}). "
            "Rewrite only the handoff block at the same report path."
        ),
        expected_output_path=expected_output_path,
        report_sha256=report_sha256,
        report_size_bytes=report_size_bytes,
        last_report_status=last_report_status,
        allowed_roots=allowed_roots,
        report_content=report_content,
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


def upsert_report_record(
    records: list[dict[str, Any]],
    record: ReportRecord,
) -> list[dict[str, Any]]:
    """Replace the latest same role+path record, or append when new."""
    out = list(records or [])
    for idx in range(len(out) - 1, -1, -1):
        entry = out[idx]
        if (
            str(entry.get("role") or "") == record.role
            and str(entry.get("path") or "") == record.path
        ):
            out[idx] = record.to_dict()
            return out
    out.append(record.to_dict())
    return out


def refresh_report_record_from_disk(record: ReportRecord) -> tuple[ReportRecord, bool]:
    """Re-read report file; refresh sha256/size when disk content changed."""
    ok, _content, sha256, size_bytes = read_report_file(record.path)
    if not ok or sha256 == record.sha256:
        return record, False
    return (
        ReportRecord(
            role=record.role,
            path=record.path,
            sha256=sha256,
            size_bytes=size_bytes,
            status=record.status,
            summary=record.summary,
            created_at=record.created_at,
        ),
        True,
    )


def refresh_report_records_from_disk(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Re-read every stored report path; update metadata when file content changed."""
    out: list[dict[str, Any]] = []
    any_refreshed = False
    for entry in records or []:
        record, refreshed = refresh_report_record_from_disk(ReportRecord.from_dict(entry))
        out.append(record.to_dict())
        any_refreshed = any_refreshed or refreshed
    return out, any_refreshed


def scan_found_handoff_marker_names(content: str) -> list[str]:
    """Return begin/end marker names present in report content."""
    found: list[str] = []
    for begin, end in _ALL_HANDOFF_MARKER_PAIRS:
        if begin in (content or "") and end in (content or ""):
            found.extend([begin, end])
    return found


def collect_owner_handoff_validation(
    content: str,
    owner_role: str,
    *,
    include_correction_handoff: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Return missing_blocks, invalid_blocks, found_marker_names for an owner report."""
    pairs = list(_REQUIRED_HANDOFF_MARKERS_BY_OWNER.get(owner_role, []))
    if include_correction_handoff:
        pairs.append(_CORRECTION_HANDOFF_MARKER_PAIR)
    missing: list[str] = []
    invalid: list[str] = []
    for begin, end in pairs:
        label = f"{begin} / {end}"
        block = extract_handoff_block(content, begin, end)
        if block is None:
            missing.append(label)
        elif not is_handoff_sufficient(block):
            invalid.append(label)
    return missing, invalid, scan_found_handoff_marker_names(content)


def format_handoff_validation_diagnostics(
    *,
    intended_next_role: str,
    dispatch_role: str,
    owner_role: str,
    owner_agent: str = "",
    report_path: str = "",
    report_hash_before: str = "",
    report_hash_after: str = "",
    report_chars: int = 0,
    missing_blocks: list[str] | None = None,
    invalid_blocks: list[str] | None = None,
    found_marker_names: list[str] | None = None,
    parser_expected_marker_names: list[str] | None = None,
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE,
    using_cached_report: bool = False,
    report_reread_after_repair: bool = False,
    reason: str = "",
) -> str:
    """Human-readable diagnostics when handoff validation redirects away from intended role."""
    expected = parser_expected_marker_names or list(PARSER_EXPECTED_HANDOFF_MARKER_NAMES)
    lines = [
        "handoff_validation_failed=true",
        f"intended_next_role={intended_next_role or '(none)'}",
        f"dispatch_role={dispatch_role or '(none)'}",
        f"owner_role={owner_role or '(none)'}",
        f"owner_agent={owner_agent or '(none)'}",
        f"report_path={report_path or '(none)'}",
        f"report_hash_before={report_hash_before or '(none)'}",
        f"report_hash_after={report_hash_after or '(none)'}",
        f"report_chars={report_chars}",
        f"missing_blocks={', '.join(missing_blocks or []) or '(none)'}",
        f"invalid_blocks={', '.join(invalid_blocks or []) or '(none)'}",
        f"found_marker_names={', '.join(found_marker_names or []) or '(none)'}",
        f"parser_expected_marker_names={', '.join(expected)}",
        f"repair_round={repair_round}",
        f"max_repair_rounds={max_repair_rounds}",
        f"using_cached_report={'true' if using_cached_report else 'false'}",
        f"report_reread_after_repair={'true' if report_reread_after_repair else 'false'}",
    ]
    if reason.strip():
        lines.append(f"reason={reason.strip()}")
    return "\n".join(lines)


def load_report_content(
    record: ReportRecord,
) -> tuple[bool, str, str, ReportRecord | None, bool]:
    """Load report body from disk; refresh record metadata when file hash changed."""
    ok, content, sha256, size_bytes = read_report_file(record.path)
    if not ok:
        return False, content, "", None, False
    if sha256 != record.sha256:
        refreshed = ReportRecord(
            role=record.role,
            path=record.path,
            sha256=sha256,
            size_bytes=size_bytes,
            status=record.status,
            summary=record.summary,
            created_at=record.created_at,
        )
        return True, content, sha256, refreshed, True
    return True, content, sha256, None, False


def apply_refreshed_report_record(
    records: list[dict[str, Any]],
    refreshed: ReportRecord,
) -> list[dict[str, Any]]:
    """Update stored report metadata after a disk refresh."""
    return upsert_report_record(records, refreshed)


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
        ok, rev_content, _, _, _ = load_report_content(reviewer)
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
    report_sha256: str = "",
    report_size_bytes: int = 0,
    last_report_status: str = "",
    include_correction_handoff: bool = False,
    allowed_roots: list[str] | None = None,
    report_content: str | None = None,
    intended_next_role: str = "",
    report_hash_before: str = "",
    using_cached_report: bool = False,
    report_reread_after_repair: bool = False,
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_HANDOFF_REPAIR_ROUNDS_PER_ROLE,
) -> ReportPromptResult | str:
    """Return repair/rewrite result, or handoff text when valid."""
    found_marker_names = scan_found_handoff_marker_names(report_content or "")
    repair_kwargs = {
        "intended_next_role": intended_next_role,
        "report_hash_before": report_hash_before or report_sha256,
        "using_cached_report": using_cached_report,
        "report_reread_after_repair": report_reread_after_repair,
        "repair_round": repair_round,
        "max_repair_rounds": max_repair_rounds,
        "found_marker_names": found_marker_names,
    }
    if handoff is None:
        return build_handoff_repair_result(
            owner_role=owner_role,
            report_path=report_path,
            missing_blocks=[block_name],
            reason=f"REQUEST_CHANGES: report missing {block_name}",
            expected_output_path=expected_output_path,
            report_sha256=report_sha256,
            report_size_bytes=report_size_bytes,
            last_report_status=last_report_status,
            include_correction_handoff=include_correction_handoff,
            allowed_roots=allowed_roots,
            report_content=report_content,
            **repair_kwargs,
        )
    if not is_handoff_sufficient(handoff):
        return build_handoff_repair_result(
            owner_role=owner_role,
            report_path=report_path,
            invalid_blocks=[block_name],
            reason=f"REQUEST_CHANGES: report has invalid or insufficient {block_name}",
            expected_output_path=expected_output_path,
            report_sha256=report_sha256,
            report_size_bytes=report_size_bytes,
            last_report_status=last_report_status,
            include_correction_handoff=include_correction_handoff,
            allowed_roots=allowed_roots,
            report_content=report_content,
            **repair_kwargs,
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
            report_sha256=report_sha256,
            report_size_bytes=report_size_bytes,
            last_report_status=last_report_status,
            allowed_roots=allowed_roots,
            report_content=report_content,
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
    records_in = list(report_records or [])
    records_in, reread = refresh_report_records_from_disk(records_in)
    dev = get_report_for_role(records_in, "developer")
    agy = get_report_for_role(records_in, "ui_lead")
    reviewer = get_report_for_role(records_in, "reviewer")
    roots = list(external_report_write_roots or resolve_external_report_write_roots(policy))

    def _with_refreshed_records(result: ReportPromptResult) -> ReportPromptResult:
        result.refreshed_report_records = records_in
        if reread:
            result.report_reread_after_repair = True
        return result

    def _load_record_content(record: ReportRecord) -> tuple[bool, str, ReportRecord]:
        nonlocal records_in, reread
        ok, content, _sha, refreshed, did_reread = load_report_content(record)
        if not ok:
            return False, content, record
        if refreshed is not None:
            records_in = apply_refreshed_report_record(records_in, refreshed)
            reread = reread or did_reread
            record = refreshed
        return True, content, record

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
            return _with_refreshed_records(ReportPromptResult(
                ok=False,
                blocker="BLOCKER: ui_lead context missing developer report",
            ))
        hash_before = dev.sha256
        ok, dev_content, dev = _load_record_content(dev)
        if not ok:
            return _with_refreshed_records(ReportPromptResult(ok=False, blocker=dev_content))
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
            report_sha256=dev.sha256,
            report_size_bytes=dev.size_bytes,
            last_report_status=dev.status,
            allowed_roots=roots,
            report_content=dev_content,
            intended_next_role="ui_lead",
            report_hash_before=hash_before,
            report_reread_after_repair=reread,
        )
        if isinstance(validated, ReportPromptResult):
            return _with_refreshed_records(validated)
        return _with_refreshed_records(build_ui_lead_report_prompt(
            project=project,
            phase=phase,
            subject=subject,
            developer_record=dev,
            developer_handoff=validated,
            instruction=instruction,
            expected_output_path=expected_output_path,
            external_report_write_roots=external_report_write_roots,
        ))

    if role == "reviewer":
        if not dev:
            return _with_refreshed_records(ReportPromptResult(
                ok=False,
                blocker="BLOCKER: reviewer context missing developer analysis",
            ))
        dev_hash_before = dev.sha256
        ok, dev_content, dev = _load_record_content(dev)
        if not ok:
            return _with_refreshed_records(ReportPromptResult(ok=False, blocker=dev_content))
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
            report_sha256=dev.sha256,
            report_size_bytes=dev.size_bytes,
            last_report_status=dev.status,
            allowed_roots=roots,
            report_content=dev_content,
            intended_next_role="reviewer",
            report_hash_before=dev_hash_before,
            report_reread_after_repair=reread,
        )
        if isinstance(validated_dev, ReportPromptResult):
            return _with_refreshed_records(validated_dev)
        agy_handoff = ""
        if agy:
            agy_hash_before = agy.sha256
            agy_ok, agy_content, agy = _load_record_content(agy)
            if not agy_ok:
                return _with_refreshed_records(ReportPromptResult(ok=False, blocker=agy_content))
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
                report_sha256=agy.sha256,
                report_size_bytes=agy.size_bytes,
                last_report_status=agy.status,
                include_correction_handoff=agy.status in ("REQUEST_CHANGES", "FAIL"),
                allowed_roots=roots,
                report_content=agy_content,
                intended_next_role="reviewer",
                report_hash_before=agy_hash_before,
                report_reread_after_repair=reread,
            )
            if isinstance(validated_agy, ReportPromptResult):
                return _with_refreshed_records(validated_agy)
            agy_handoff = validated_agy
        return _with_refreshed_records(build_reviewer_report_prompt(
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
        ))

    if role == "developer" and awaiting_developer_correction:
        correction_source = (developer_correction_source or "").strip().lower()
        if correction_source == "ui_lead" and agy:
            agy_ok, agy_content, agy = _load_record_content(agy)
            if not agy_ok:
                return _with_refreshed_records(ReportPromptResult(ok=False, blocker=agy_content))
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
                    report_sha256=agy.sha256,
                    report_size_bytes=agy.size_bytes,
                    last_report_status=agy.status,
                    include_correction_handoff=True,
                    allowed_roots=roots,
                    report_content=agy_content,
                )
                if isinstance(validated, ReportPromptResult):
                    return _with_refreshed_records(validated)
                correction_handoff = validated
            return _with_refreshed_records(build_developer_ui_lead_correction_report_prompt(
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
            ))
        if reviewer:
            rev_ok, rev_content, reviewer = _load_record_content(reviewer)
            if not rev_ok:
                return _with_refreshed_records(ReportPromptResult(ok=False, blocker=rev_content))
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
                    report_sha256=reviewer.sha256,
                    report_size_bytes=reviewer.size_bytes,
                    last_report_status=reviewer.status,
                    include_correction_handoff=True,
                    allowed_roots=roots,
                    report_content=rev_content,
                )
                if isinstance(validated, ReportPromptResult):
                    return _with_refreshed_records(validated)
                correction_handoff = validated
            return _with_refreshed_records(build_developer_correction_report_prompt(
                project=project,
                phase=phase,
                subject=subject,
                reviewer_record=reviewer,
                correction_handoff=correction_handoff or "NONE",
                developer_record=dev,
                instruction=instruction,
                expected_output_path=expected_output_path,
                external_report_write_roots=external_report_write_roots,
            ))

    return _with_refreshed_records(ReportPromptResult(
        ok=False,
        blocker=f"BLOCKER: no report-orchestrated prompt available for role={role}",
    ))


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
