"""Workspace-bound Claude worker helpers (cwd, precheck, snapshots, tool-call leakage)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workspace_policy_runtime import (
    DEFAULT_SCRATCH_CWD,
    canonical_policy_from_queue_context,
    classify_dirty_entries_report_only_analysis,
    external_cwd_enabled_for_mode,
    is_report_only_readonly_policy,
    is_trusted_direct_repo_cli_policy,
    normalize_workspace_root,
    resolve_exec_cwd_for_item,
    verify_dirty_set,
)

TOOL_CALL_TAG_RE = re.compile(r"<\s*tool_call\b", re.IGNORECASE)
TOOL_NAME_RE = re.compile(r"<\s*tool_name\s*>([^<]+)</\s*tool_name\s*>", re.IGNORECASE)
TOOL_COMMAND_RE = re.compile(
    r"<\s*command\s*>([^<]+)</\s*command\s*>",
    re.IGNORECASE | re.DOTALL,
)
REPORT_BEGIN_MARKER = "REPORT_BEGIN"
REPORT_END_MARKER = "REPORT_END"
REPORT_FILE_WRITE_BEGIN_MARKER = "REPORT_FILE_WRITE_BEGIN"
REPORT_FILE_WRITE_END_MARKER = "REPORT_FILE_WRITE_END"

_WRITE_PATH_PATTERNS = (
    re.compile(
        r"<\s*parameter\s+name=[\"']file_path[\"']\s*>([^<]+)</\s*parameter\s*>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<\s*parameter\s+name=[\"']path[\"']\s*>([^<]+)</\s*parameter\s*>",
        re.IGNORECASE,
    ),
    re.compile(r"<\s*file_path\s*>([^<]+)</\s*file_path\s*>", re.IGNORECASE),
)
_WRITE_CONTENT_PATTERNS = (
    re.compile(
        r"<\s*parameter\s+name=[\"']content[\"']\s*>(.*?)</\s*parameter\s*>",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<\s*parameter\s+name=[\"']file_content[\"']\s*>(.*?)</\s*parameter\s*>",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"<\s*content\s*>(.*?)</\s*content\s*>", re.IGNORECASE | re.DOTALL),
)
_REPORT_FILE_WRITE_FIELD_PATTERNS = {
    "path": re.compile(r"^\s*Path\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "status": re.compile(r"^\s*Status\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "summary": re.compile(r"^\s*Summary\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    "next_role": re.compile(
        r"^\s*Next recommended role\s*:\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
}

REPORT_WRITE_BRIDGE_INSTRUCTION = (
    "SCOPED REPORT WRITE (read-only report-flow sessions):\n"
    "- You do not have generic file tools or shell.\n"
    "- Do not emit <tool_call> XML or request Write/Read/Bash tools.\n"
    "- To create your external .md report, output exactly one block:\n"
    "  REPORT_FILE_WRITE_BEGIN\n"
    "  Path: <absolute .md path under allowed Ai-Report roots>\n"
    "  Status: PASS\n"
    "  Summary: <short summary>\n"
    "  Next recommended role: coordinator\n"
    "  ---\n"
    "  <markdown report body>\n"
    "  REPORT_FILE_WRITE_END\n"
    "- The worker runtime validates the path and writes the file.\n"
    "- After the runtime confirms the file exists, your output becomes REPORT_READY.\n"
    "- Do not claim REPORT_READY unless the runtime confirms the report file exists.\n"
    "- Twinpet workspace writes are forbidden."
)

TRUSTED_CLI_REPORT_BRIDGE_INSTRUCTION = (
    "REPORT OUTPUT METHOD (trusted_direct_repo_cli):\n"
    "- Tools are enabled for reading/searching the repo only.\n"
    "- Do NOT use Claude Code Write/Edit tools for the external report.\n"
    "- Do NOT ask for permission to write the report file.\n"
    "- The runtime saves your report from stdout.\n"
    "- Output exactly one REPORT_FILE_WRITE_BEGIN / REPORT_FILE_WRITE_END block:\n"
    "  REPORT_FILE_WRITE_BEGIN\n"
    "  Path: <absolute .md path under allowed Ai-Report roots>\n"
    "  Status: PASS\n"
    "  Summary: <short summary>\n"
    "  Next recommended role: coordinator\n"
    "  ---\n"
    "  <markdown report body>\n"
    "  REPORT_FILE_WRITE_END\n"
    "- The runtime validates the path, writes the file, and emits REPORT_READY.\n"
    "- If you cannot output the bridge, return: BLOCKER: trusted CLI report bridge output failed"
)

TRUSTED_CLI_NATIVE_WRITE_BLOCKER_PREFIX = (
    "BLOCKER: trusted CLI used native write instead of report bridge"
)
TRUSTED_CLI_REFUSED_BLOCKER_PREFIX = (
    "BLOCKER: trusted CLI refused report-output contract"
)
TRUSTED_CLI_INCOMPLETE_BLOCKER_PREFIX = (
    "BLOCKER: trusted CLI report stdout incomplete"
)
TRUSTED_CLI_UNEXPECTED_PATH_BLOCKER_PREFIX = (
    "BLOCKER: trusted CLI report path is not an expected report path"
)
DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS = 1
TRUSTED_CLI_REPORT_BRIDGE_REPAIR_EXCERPT_CHARS = 4000
TRUSTED_CLI_MIN_REPORT_CHARS = 800
TRUSTED_CLI_REPORT_FILE_REREAD_ATTEMPTS = 3
TRUSTED_CLI_REPORT_FILE_REREAD_DELAY_SEC = 0.25

_NATIVE_WRITE_PERMISSION_PROMPT_RES = (
    re.compile(r"explicit approval", re.IGNORECASE),
    re.compile(r"write permission", re.IGNORECASE),
    re.compile(r"approve the write", re.IGNORECASE),
    re.compile(r"outside the repo working directory", re.IGNORECASE),
    re.compile(r"permission prompt should have appeared", re.IGNORECASE),
    re.compile(r"could you approve", re.IGNORECASE),
)
_PROMPT_INJECTION_REFUSAL_RES = (
    re.compile(r"hallmarks of prompt injection", re.IGNORECASE),
    re.compile(r"prompt injection", re.IGNORECASE),
    re.compile(r"fake.*bridge", re.IGNORECASE),
    re.compile(r"fake.*protocol", re.IGNORECASE),
    re.compile(r"role-play.*routing", re.IGNORECASE),
    re.compile(r"REPORT_FILE_WRITE_BEGIN/END as a fake", re.IGNORECASE),
    re.compile(r"cannot follow (these|those|this) instruction", re.IGNORECASE),
    re.compile(r"refuse to (follow|comply|emit)", re.IGNORECASE),
)
_TRUSTED_CLI_REPORT_SECTION_MARKERS = (
    "## summary",
    "## files inspected",
    "## findings",
    "## evidence",
    "## red-zone",
    "## red zone",
    "## recommended next step",
)
_TRUSTED_CLI_REPORT_EVIDENCE_MARKERS = (
    "## files inspected",
    "files inspected",
    "## evidence",
    "git rev-parse",
    "git status",
    "head:",
)
_TRUSTED_CLI_REPORT_REDZONE_MARKERS = (
    "red-zone",
    "red zone",
    "no product",
    "no modification",
    "were not modified",
    "unchanged",
    "not modified",
)
_TRUSTED_CLI_REPORT_NEXT_STEP_MARKERS = (
    "recommended next",
    "next recommended",
    "next step",
    "route to agy",
    "route to ui",
)

DEFAULT_SNAPSHOT_MAX_CHARS_PER_FILE = 48_000
SNAPSHOT_HEAD_CHARS = 24_000
SNAPSHOT_TAIL_CHARS = 12_000


@dataclass
class PrecheckResult:
    ok: bool
    blocker: str | None = None
    text: str = ""
    head: str = ""
    porcelain: str = ""


@dataclass
class ReportSaveResult:
    saved: bool = False
    path: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class TrustedCliSalvageResult:
    salvaged: bool = False
    report_ready: str = ""
    report_path: str = ""
    failure_reason: str = ""
    file_exists: bool = False
    report_chars: int = 0
    stdout_chars: int = 0


@dataclass
class TrustedCliReportOutcome:
    """Typed result from the shared trusted CLI report-output resolver."""

    kind: str  # report_ready | correction_prompt | blocker | none
    text: str = ""
    report_ready: str = ""
    repair_reason: str = ""
    salvage: TrustedCliSalvageResult | None = None


@dataclass
class SnapshotMeta:
    injected: bool = False
    file_count: int = 0
    paths: list[str] = field(default_factory=list)


def get_snapshot_max_chars(config: dict | None) -> int:
    section = (config or {}).get("session_worker_timeouts")
    if isinstance(section, dict):
        try:
            return int(section.get("snapshot_max_chars_per_file", DEFAULT_SNAPSHOT_MAX_CHARS_PER_FILE))
        except (TypeError, ValueError):
            pass
    return DEFAULT_SNAPSHOT_MAX_CHARS_PER_FILE


def is_workspace_bound_queue_item(item: dict[str, Any] | None) -> bool:
    """True when queue item is a session workspace-bound worker turn."""
    if not isinstance(item, dict):
        return False
    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return False
    if wpc.get("relay_kind") != "session_turn":
        return False
    if wpc.get("session_id") is None:
        return False
    role = str(wpc.get("session_role") or "").lower()
    if role not in ("developer", "ui_lead"):
        return False
    mode = wpc.get("policy_mode")
    if mode not in ("docs-only", "implementation", "read-only"):
        return False
    root = wpc.get("workspace_root")
    return bool(root and str(root).strip())


def is_trusted_direct_repo_cli_mode(
    item: dict[str, Any] | None,
    policy: dict[str, Any] | None,
) -> bool:
    """True when worker runs in trusted direct repo CLI mode (tools enabled, no snapshots)."""
    if is_trusted_direct_repo_cli_policy(policy):
        return True
    if isinstance(item, dict):
        wpc = item.get("workspace_policy_context")
        if isinstance(wpc, dict) and wpc.get("trusted_direct_repo_cli"):
            return True
    return False


def policy_uses_report_write_bridge(policy: dict[str, Any] | None) -> bool:
    """True when worker output may use REPORT_FILE_WRITE bridge handling."""
    if not isinstance(policy, dict):
        return False
    if is_report_only_readonly_policy(policy):
        return True
    return is_trusted_direct_repo_cli_policy(policy)


def detect_native_write_permission_prompt(text: str) -> bool:
    """True when Claude stdout asks for interactive external file write approval."""
    sample = (text or "").strip()
    if not sample:
        return False
    if REPORT_FILE_WRITE_BEGIN_MARKER in sample and REPORT_FILE_WRITE_END_MARKER in sample:
        return False
    if sample.startswith("REPORT_READY"):
        return False
    return any(pattern.search(sample) for pattern in _NATIVE_WRITE_PERMISSION_PROMPT_RES)


def detect_trusted_cli_prompt_injection_refusal(text: str) -> bool:
    """True when Claude refuses coordinator-style or bridge-like output instructions."""
    sample = (text or "").strip()
    if not sample:
        return False
    return any(pattern.search(sample) for pattern in _PROMPT_INJECTION_REFUSAL_RES)


def parse_trusted_cli_report_status(text: str) -> str:
    """Extract Status: line from trusted CLI markdown stdout."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("status:"):
            value = stripped.split(":", 1)[1].strip().upper()
            for token in ("PASS_WITH_NOTES", "REQUEST_CHANGES", "BLOCKER", "FAIL", "PASS"):
                if value.startswith(token):
                    return token
            return value.split()[0] if value else "PASS_WITH_NOTES"
    return "PASS_WITH_NOTES"


def parse_trusted_cli_report_summary(text: str) -> str:
    """Derive a short summary from trusted CLI markdown stdout."""
    lines = (text or "").splitlines()
    for idx, line in enumerate(lines):
        if line.strip().lower().startswith("## summary"):
            for follow in lines[idx + 1:]:
                body = follow.strip()
                if body.startswith("#"):
                    break
                if body:
                    return body[:240]
    for line in lines:
        if line.strip().startswith("#"):
            return line.strip().lstrip("#").strip()[:240]
    return "trusted CLI markdown report captured from stdout"


def is_trusted_cli_stdout_markdown_report(text: str) -> bool:
    """True when stdout looks like a complete trusted CLI analysis report."""
    body = (text or "").strip()
    if len(body) < TRUSTED_CLI_MIN_REPORT_CHARS:
        return False
    if detect_native_write_permission_prompt(body):
        return False
    if detect_trusted_cli_prompt_injection_refusal(body):
        return False
    if not (body.startswith("#") or "\n# " in body or "\n## " in body):
        return False
    low = body.lower()
    section_hits = sum(1 for marker in _TRUSTED_CLI_REPORT_SECTION_MARKERS if marker in low)
    has_status = "status:" in low
    return section_hits >= 2 or (section_hits >= 1 and has_status)


def format_trusted_cli_refusal_blocker(
    *,
    text: str,
    policy: dict[str, Any] | None = None,
    workspace_profile: str = "",
    workspace_mode: str = "",
    report_path: str = "",
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS,
) -> str:
    if not report_path and isinstance(policy, dict):
        paths = list(policy.get("report_paths") or [])
        report_path = paths[0] if paths else ""
    lines = [
        TRUSTED_CLI_REFUSED_BLOCKER_PREFIX,
        "",
        f"workspace_profile: {workspace_profile or (policy or {}).get('policy_id', '')}",
        f"workspace_mode: {workspace_mode or (policy or {}).get('mode', '')}",
        "trusted_direct_repo_cli: true",
        f"report_path: {report_path}",
        f"repair_round: {repair_round}",
        f"max_repair_rounds: {max_repair_rounds}",
        "contains_prompt_injection_refusal: true",
        f"contains_report_bridge: {REPORT_FILE_WRITE_BEGIN_MARKER in (text or '')}",
        f"output_chars: {len((text or '').strip())}",
    ]
    return "\n".join(lines)


def format_trusted_cli_incomplete_report_blocker(
    *,
    text: str,
    policy: dict[str, Any] | None = None,
    workspace_profile: str = "",
    workspace_mode: str = "",
    report_path: str = "",
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS,
    salvage: TrustedCliSalvageResult | None = None,
) -> str:
    if not report_path and isinstance(policy, dict):
        paths = list(policy.get("report_paths") or [])
        report_path = paths[0] if paths else ""
    if salvage and salvage.report_path:
        report_path = salvage.report_path
    lines = [
        TRUSTED_CLI_INCOMPLETE_BLOCKER_PREFIX,
        "",
        f"workspace_profile: {workspace_profile or (policy or {}).get('policy_id', '')}",
        f"workspace_mode: {workspace_mode or (policy or {}).get('mode', '')}",
        "trusted_direct_repo_cli: true",
        f"report_path: {report_path}",
        f"repair_round: {repair_round}",
        f"max_repair_rounds: {max_repair_rounds}",
        f"output_chars: {len((text or '').strip())}",
        f"min_report_chars: {TRUSTED_CLI_MIN_REPORT_CHARS}",
        "trusted_cli_report_salvage_attempted: true",
        f"salvage_failure_reason: {(salvage.failure_reason if salvage else 'not attempted')}",
        f"file_exists: {salvage.file_exists if salvage else False}",
        f"report_chars: {salvage.report_chars if salvage else 0}",
        f"stdout_chars: {salvage.stdout_chars if salvage else len((text or '').strip())}",
        "Action: return a complete Markdown report in your final response.",
    ]
    return "\n".join(lines)


def is_trusted_cli_native_write_blocker(text: str) -> bool:
    """True when output is the structured trusted CLI native-write terminal blocker."""
    return TRUSTED_CLI_NATIVE_WRITE_BLOCKER_PREFIX in (text or "")


def _bounded_trusted_cli_analysis_excerpt(text: str, *, max_chars: int | None = None) -> str:
    """Return bounded prior-analysis excerpt for bridge correction prompts."""
    limit = max_chars or TRUSTED_CLI_REPORT_BRIDGE_REPAIR_EXCERPT_CHARS
    body = (text or "").strip()
    if not body:
        return ""
    if is_trusted_cli_native_write_blocker(body):
        lines = body.splitlines()
        body = "\n".join(
            ln for ln in lines
            if not ln.strip().startswith(("workspace_", "contains_", "trusted_", "report_path:", "cwd:", "session_id:", "channel:", "Action:"))
        ).strip()
    return body[:limit]


def build_trusted_cli_markdown_report_repair_prompt(
    *,
    previous_output: str,
    report_path: str = "",
    repair_round: int = 1,
    max_repair_rounds: int = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS,
    reason: str = "native_write",
) -> str:
    """Short correction prompt asking for plain Markdown final report (trusted CLI)."""
    excerpt = _bounded_trusted_cli_analysis_excerpt(previous_output)
    reason_line = {
        "native_write": (
            "Your previous turn attempted to create the report file or asked for write permission."
        ),
        "refusal": (
            "Your previous turn refused the analysis task or questioned the instructions."
        ),
        "incomplete": (
            "Your previous response was not a complete Markdown analysis report."
        ),
    }.get(reason, "Your previous response was not a complete Markdown analysis report.")
    lines = [
        "TRUSTED CLI REPORT CORRECTION",
        "",
        reason_line,
        "",
        "Please do not create or edit files for the report.",
        "Do not ask for file-write permission.",
        "Do not inspect source again unless absolutely necessary.",
        "",
        "Return the complete analysis report as Markdown in your final response.",
        "Include at minimum: title, Status line, Summary, Files inspected, Findings,",
        "Red-zone confirmation, and Recommended next step.",
        "",
        f"repair_round: {repair_round}",
        f"max_repair_rounds: {max_repair_rounds}",
    ]
    if report_path:
        lines.extend(["", f"Reference report path (do not write this file yourself): {report_path}"])
    if excerpt.strip():
        lines.extend([
            "",
            "PRIOR ANALYSIS EXCERPT (reuse; do not re-read repo unless required):",
            excerpt.strip(),
        ])
    return "\n".join(lines)


def build_trusted_cli_report_bridge_repair_prompt(
    *,
    previous_output: str,
    report_path: str,
    repair_round: int = 1,
    max_repair_rounds: int = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS,
) -> str:
    """Backward-compatible alias — trusted CLI repair now requests Markdown stdout."""
    reason = "native_write"
    if detect_trusted_cli_prompt_injection_refusal(previous_output):
        reason = "refusal"
    elif not is_trusted_cli_stdout_markdown_report(previous_output):
        reason = "incomplete"
    return build_trusted_cli_markdown_report_repair_prompt(
        previous_output=previous_output,
        report_path=report_path,
        repair_round=repair_round,
        max_repair_rounds=max_repair_rounds,
        reason=reason,
    )


def format_trusted_cli_native_write_blocker(
    *,
    text: str,
    policy: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    queue_item: dict[str, Any] | None = None,
    workspace_profile: str = "",
    workspace_mode: str = "",
    report_path: str = "",
    session_id: str = "",
    channel: str = "",
    repair_round: int = 0,
    max_repair_rounds: int = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS,
) -> str:
    """Structured blocker when trusted CLI used native Write instead of report bridge."""
    ctx = worker_context_from_queue_item(queue_item)
    if not report_path:
        report_paths = list((policy or {}).get("report_paths") or [])
        report_path = report_paths[0] if report_paths else ""
    if not workspace_profile:
        workspace_profile = str(ctx.get("policy_id") or (policy or {}).get("policy_id") or "")
    if not workspace_mode:
        workspace_mode = str(ctx.get("policy_mode") or (policy or {}).get("mode") or "")
    if not session_id or not channel:
        wpc = queue_item.get("workspace_policy_context") if isinstance(queue_item, dict) else {}
        if isinstance(wpc, dict):
            channel = channel or str(wpc.get("channel") or "")
            session_id = session_id or str(wpc.get("session_id") or "")
    has_bridge = (
        REPORT_FILE_WRITE_BEGIN_MARKER in (text or "")
        and REPORT_FILE_WRITE_END_MARKER in (text or "")
    )
    lines = [
        TRUSTED_CLI_NATIVE_WRITE_BLOCKER_PREFIX,
        "",
        f"workspace_profile: {workspace_profile}",
        f"workspace_mode: {workspace_mode}",
        "trusted_direct_repo_cli: true",
        f"report_path: {report_path}",
        f"repair_round: {repair_round}",
        f"max_repair_rounds: {max_repair_rounds}",
        f"contains_report_bridge: {has_bridge}",
        f"contains_report_ready: {str(text or '').lstrip().startswith('REPORT_READY')}",
        "contains_native_write_permission_prompt: true",
        f"cwd: {cwd or ctx.get('workspace_root') or ''}",
        f"session_id: {session_id}",
        f"channel: {channel}",
        "",
        "Action: return the complete Markdown report in your final response. "
        "Do not create or edit report files.",
    ]
    return "\n".join(lines)


def try_capture_trusted_cli_stdout_report(
    text: str,
    policy: dict[str, Any] | None,
) -> str | None:
    """Save a complete trusted CLI markdown report from stdout and emit REPORT_READY."""
    if not isinstance(policy, dict) or not is_trusted_direct_repo_cli_policy(policy):
        return None
    if not is_trusted_cli_stdout_markdown_report(text):
        return None
    targets = [p for p in (policy.get("report_paths") or []) if isinstance(p, str) and p.strip()]
    if not targets:
        return None
    body = (text or "").strip()
    status = parse_trusted_cli_report_status(body)
    summary = parse_trusted_cli_report_summary(body)
    ok, saved_path, err = write_validated_external_report(targets[0], body, policy)
    if ok:
        return format_report_ready_after_worker_write(
            path=saved_path,
            status=status,
            summary=summary,
            notes="Trusted CLI markdown report captured from stdout.",
        )
    return format_report_write_failed_reply(path=targets[0], reason=err)


def try_recover_trusted_cli_stdout_report(
    text: str,
    policy: dict[str, Any] | None,
) -> str | None:
    """Backward-compatible alias for trusted CLI stdout capture."""
    return try_capture_trusted_cli_stdout_report(text, policy)


def validate_trusted_cli_existing_report_file(content: str) -> tuple[bool, str]:
    """Validate on-disk trusted CLI report content for salvage acceptance."""
    body = (content or "").strip()
    if len(body) < TRUSTED_CLI_MIN_REPORT_CHARS:
        return False, f"report too short ({len(body)} < {TRUSTED_CLI_MIN_REPORT_CHARS})"
    if not (body.startswith("#") or "\n# " in body or "\n## " in body):
        return False, "missing markdown heading"
    low = body.lower()
    has_status = "status:" in low or any(
        token in low for token in ("pass_with_notes", "request_changes", "status: pass")
    )
    if not has_status:
        return False, "missing status or verdict"
    if not any(marker in low for marker in _TRUSTED_CLI_REPORT_EVIDENCE_MARKERS):
        return False, "missing files inspected or evidence section"
    if not any(marker in low for marker in _TRUSTED_CLI_REPORT_REDZONE_MARKERS):
        return False, "missing red-zone or no-modification confirmation"
    if not any(marker in low for marker in _TRUSTED_CLI_REPORT_NEXT_STEP_MARKERS):
        return False, "missing recommended next step"
    if not _trusted_cli_has_findings_signal(body, low):
        return False, "missing findings or analysis section"
    return True, ""


def _trusted_cli_has_findings_signal(body: str, low: str | None = None) -> bool:
    """True when report has a findings heading or a substantial analysis paragraph."""
    low = low if low is not None else body.lower()
    if "## findings" in low:
        return True
    paragraph: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if paragraph:
                if len(" ".join(paragraph).strip()) >= 200:
                    return True
                paragraph = []
            continue
        if stripped:
            paragraph.append(stripped)
        elif paragraph:
            if len(" ".join(paragraph).strip()) >= 200:
                return True
            paragraph = []
    return len(" ".join(paragraph).strip()) >= 200


def resolve_expected_trusted_cli_report_path(
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    worker_context: dict[str, Any] | None = None,
) -> str:
    """Unified expected report path: by_role -> context paths -> policy paths."""
    ctx = worker_context if isinstance(worker_context, dict) else {}
    by_role = ctx.get("report_paths_by_role") or {}
    if isinstance(by_role, dict):
        role_path = by_role.get(role) or by_role.get(str(role))
        if role_path:
            return str(role_path)
    ctx_paths = [p for p in (ctx.get("report_paths") or []) if isinstance(p, str) and p.strip()]
    if ctx_paths:
        return str(ctx_paths[0])
    if isinstance(policy, dict):
        policy_paths = [p for p in (policy.get("report_paths") or []) if isinstance(p, str) and p.strip()]
        if policy_paths:
            return str(policy_paths[0])
    return ""


def _normalize_trusted_cli_report_path_key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).strip().lower()


def trusted_cli_report_path_is_expected(
    raw_path: str,
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    worker_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """True when raw_path canonicalizes to the trusted session expected report path."""
    expected = resolve_expected_trusted_cli_report_path(
        policy,
        role=role,
        worker_context=worker_context,
    )
    if not expected:
        return False, "expected report path not configured"
    if _normalize_trusted_cli_report_path_key(raw_path) != _normalize_trusted_cli_report_path_key(expected):
        return False, expected
    return True, expected


def format_trusted_cli_unexpected_report_path_blocker(
    *,
    actual_path: str,
    expected_path: str,
    policy: dict[str, Any] | None = None,
    workspace_profile: str = "",
    workspace_mode: str = "",
) -> str:
    lines = [
        TRUSTED_CLI_UNEXPECTED_PATH_BLOCKER_PREFIX,
        "",
        f"workspace_profile: {workspace_profile or (policy or {}).get('policy_id', '')}",
        f"workspace_mode: {workspace_mode or (policy or {}).get('mode', '')}",
        "trusted_direct_repo_cli: true",
        f"report_path: {expected_path}",
        f"actual_path: {actual_path}",
        f"expected_path: {expected_path}",
    ]
    return "\n".join(lines)


def _trusted_cli_wrapper_blocker_kind(sample: str) -> str | None:
    """Classify trusted CLI wrapper-formatted blocker text."""
    if TRUSTED_CLI_REFUSED_BLOCKER_PREFIX in sample:
        return "refusal"
    if TRUSTED_CLI_UNEXPECTED_PATH_BLOCKER_PREFIX in sample:
        return "unexpected_path"
    if is_trusted_cli_native_write_blocker(sample):
        return "native_write"
    if TRUSTED_CLI_INCOMPLETE_BLOCKER_PREFIX in sample:
        return "incomplete"
    return None


def _trusted_cli_salvage_diagnostics_lines(
    *,
    salvaged: bool,
    report_path: str,
    report_chars: int,
    stdout_chars: int,
    status: str,
    summary: str,
    policy: dict[str, Any],
    queue_item: dict[str, Any] | None,
    cwd: str | Path | None,
    native_write_warning: bool = False,
) -> str:
    ctx = worker_context_from_queue_item(queue_item)
    wpc = queue_item.get("workspace_policy_context") if isinstance(queue_item, dict) else {}
    session_id = str((wpc or {}).get("session_id") or "")
    channel = str((wpc or {}).get("channel") or "")
    lines = [
        "trusted_cli_report_salvaged=true",
        f"report_path={report_path}",
        f"report_chars={report_chars}",
        f"stdout_chars={stdout_chars}",
        f"status={status}",
        f"summary={summary[:120]}",
        f"workspace_profile={policy.get('policy_id') or ctx.get('policy_id') or ''}",
        f"workspace_mode={policy.get('mode') or ctx.get('policy_mode') or ''}",
        "trusted_direct_repo_cli=true",
        f"cwd={cwd or ctx.get('workspace_root') or ''}",
        f"session_id={session_id}",
        f"channel={channel}",
    ]
    if native_write_warning:
        lines.append("native_write_prompt_with_valid_report=true")
    return "\n".join(lines)


def resolve_trusted_cli_repair_rounds(
    queue_item: dict[str, Any] | None = None,
    *,
    repair_rounds_used: int | None = None,
    max_repair_rounds: int | None = None,
) -> tuple[int, int]:
    """Read trusted CLI repair counters from explicit args or queue session context."""
    used = repair_rounds_used
    max_r = max_repair_rounds
    if isinstance(queue_item, dict):
        wpc = queue_item.get("workspace_policy_context")
        if isinstance(wpc, dict):
            if used is None:
                try:
                    used = int(wpc.get("trusted_cli_report_bridge_repair_rounds") or 0)
                except (TypeError, ValueError):
                    used = 0
            if max_r is None:
                try:
                    max_r = int(
                        wpc.get("max_trusted_cli_report_bridge_repair_rounds")
                        or DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS
                    )
                except (TypeError, ValueError):
                    max_r = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS
    if used is None:
        used = 0
    if max_r is None:
        max_r = DEFAULT_MAX_TRUSTED_CLI_REPORT_BRIDGE_REPAIR_ROUNDS
    return int(used), int(max_r)


def _attempt_salvage_trusted_cli_existing_report_once(
    policy: dict[str, Any],
    stdout_text: str,
    *,
    role: str,
    queue_item: dict[str, Any] | None,
    worker_context: dict[str, Any] | None,
    cwd: str | Path | None,
    native_write_warning: bool,
) -> TrustedCliSalvageResult:
    """Single read of the expected trusted CLI report path."""
    from report_orchestration import read_report_file, validate_report_path

    result = TrustedCliSalvageResult(stdout_chars=len((stdout_text or "").strip()))
    if not isinstance(policy, dict) or not is_trusted_direct_repo_cli_policy(policy):
        result.failure_reason = "not trusted CLI policy"
        return result
    raw_path = resolve_expected_trusted_cli_report_path(
        policy,
        role=role,
        worker_context=worker_context,
    )
    if not raw_path:
        result.failure_reason = "expected report path not configured"
        return result
    result.report_path = raw_path

    roots = _external_report_roots(policy)
    ok, reason, resolved = validate_report_path(raw_path, allowed_roots=roots)
    if not ok or resolved is None:
        result.failure_reason = reason or "report path outside allowed roots"
        return result
    result.report_path = str(resolved)
    result.file_exists = resolved.is_file()
    if not result.file_exists:
        result.failure_reason = "report file not found"
        return result
    read_ok, content, _sha, _size = read_report_file(resolved)
    if not read_ok:
        result.failure_reason = content or "cannot read report file"
        return result
    result.report_chars = len(content.strip())
    valid, why = validate_trusted_cli_existing_report_file(content)
    if not valid:
        result.failure_reason = why
        return result
    status = parse_trusted_cli_report_status(content)
    summary = parse_trusted_cli_report_summary(content)
    diag = _trusted_cli_salvage_diagnostics_lines(
        salvaged=True,
        report_path=str(resolved),
        report_chars=result.report_chars,
        stdout_chars=result.stdout_chars,
        status=status,
        summary=summary,
        policy=policy,
        queue_item=queue_item,
        cwd=cwd,
        native_write_warning=native_write_warning,
    )
    notes = "Trusted CLI existing report salvaged from expected path."
    if native_write_warning:
        notes += " Native write permission prompt ignored because valid report file exists."
    result.salvaged = True
    result.report_ready = format_report_ready_after_worker_write(
        path=str(resolved),
        status=status,
        summary=summary,
        notes=f"{notes}\n\n{diag}",
    )
    return result


def attempt_salvage_trusted_cli_existing_report(
    policy: dict[str, Any],
    stdout_text: str = "",
    *,
    role: str = "developer",
    queue_item: dict[str, Any] | None = None,
    worker_context: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    native_write_warning: bool = False,
    reread_attempts: int = 1,
    reread_delay_sec: float = 0.0,
) -> TrustedCliSalvageResult:
    """Try to accept an existing expected Ai-Report .md file before incomplete blocker."""
    import time

    attempts = max(1, int(reread_attempts or 1))
    delay = max(0.0, float(reread_delay_sec or 0.0))
    last: TrustedCliSalvageResult | None = None
    for attempt_idx in range(attempts):
        if attempt_idx > 0 and delay > 0:
            time.sleep(delay)
        last = _attempt_salvage_trusted_cli_existing_report_once(
            policy,
            stdout_text,
            role=role,
            queue_item=queue_item,
            worker_context=worker_context,
            cwd=cwd,
            native_write_warning=native_write_warning,
        )
        if last.salvaged:
            return last
    assert last is not None
    return last


def is_docs_only_snapshot_mode(
    item: dict[str, Any] | None,
    policy: dict[str, Any] | None,
    *,
    config: dict | None = None,
) -> bool:
    """True when worker should receive automated precheck + full file snapshots."""
    from on_demand_snapshots import is_on_demand_snapshot_mode

    if is_trusted_direct_repo_cli_mode(item, policy):
        return False
    if is_on_demand_snapshot_mode(item, policy, config=config):
        return False
    if isinstance(item, dict):
        relay_meta = item.get("relay_meta")
        if isinstance(relay_meta, dict) and (
            relay_meta.get("handoff_repair") or relay_meta.get("trusted_cli_report_bridge_repair")
        ):
            return False
        wpc = item.get("workspace_policy_context")
        if isinstance(wpc, dict) and (
            wpc.get("handoff_repair")
            or wpc.get("trusted_cli_report_bridge_repair")
            or wpc.get("skip_snapshot_injection")
        ):
            return False
    if not is_workspace_bound_queue_item(item):
        return False
    mode = (policy or {}).get("mode") if isinstance(policy, dict) else None
    if not mode:
        wpc = item.get("workspace_policy_context") if isinstance(item, dict) else {}
        mode = wpc.get("policy_mode") if isinstance(wpc, dict) else None
    return mode in ("docs-only", "read-only")


def load_canonical_policy_for_item(
    item: dict[str, Any] | None,
    *,
    data_dir: str | Path | None,
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return None
    policy = canonical_policy_from_queue_context(queue_context=wpc, data_dir=data_dir)
    if isinstance(policy, dict):
        return policy
    return None


def resolve_workspace_exec_cwd_or_blocker(
    item: dict[str, Any] | None,
    *,
    data_dir: str | Path | None,
    config: dict | None,
    default_cwd: str,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve workspace subprocess cwd or return a BLOCKER token."""
    if not isinstance(item, dict):
        return default_cwd, None

    wpc = item.get("workspace_policy_context")
    if not is_workspace_bound_queue_item(item):
        cwd = resolve_exec_cwd_for_item(
            item,
            data_dir=data_dir,
            config=config,
            default_cwd=default_cwd,
            profiles=profiles,
        )
        return cwd, None

    assert isinstance(wpc, dict)
    expected_root = normalize_workspace_root(str(wpc.get("workspace_root") or ""))
    mode = wpc.get("policy_mode")

    if not external_cwd_enabled_for_mode(config, str(mode) if mode else None):
        return None, "BLOCKER: workspace runner context missing"

    cwd = resolve_exec_cwd_for_item(
        item,
        data_dir=data_dir,
        config=config,
        default_cwd=default_cwd,
        profiles=profiles,
    )
    normalized = normalize_workspace_root(cwd)
    if expected_root and normalized == expected_root and Path(normalized).is_dir():
        return cwd, None

    return None, "BLOCKER: workspace runner context missing"


def _normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/")


def _resolve_allowlisted_file(workspace_root: Path, rel_path: str) -> Path | None:
    norm = _normalize_rel_path(rel_path)
    if not norm or norm.endswith("/"):
        return None
    root = workspace_root.resolve()
    candidate = (root / norm).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _truncate_file_content(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    if max_chars < SNAPSHOT_HEAD_CHARS + SNAPSHOT_TAIL_CHARS + 64:
        return content[:max_chars] + "\n[TRUNCATED]", True
    head = content[:SNAPSHOT_HEAD_CHARS]
    tail = content[-SNAPSHOT_TAIL_CHARS:]
    omitted = len(content) - SNAPSHOT_HEAD_CHARS - SNAPSHOT_TAIL_CHARS
    return (
        f"{head}\n\n[TRUNCATED: {omitted} chars omitted from middle]\n\n{tail}",
        True,
    )


def read_allowlisted_file_snapshot(
    workspace_root: str | Path,
    rel_path: str,
    *,
    max_chars_per_file: int,
) -> dict[str, Any]:
    """Read one allowlisted relative path; never reads outside workspace root."""
    root = Path(workspace_root)
    norm = _normalize_rel_path(rel_path)
    resolved = _resolve_allowlisted_file(root, norm)
    entry: dict[str, Any] = {
        "path": norm,
        "exists": False,
        "size_bytes": 0,
        "line_count": 0,
        "truncated": False,
        "content": "",
    }
    if resolved is None:
        entry["content"] = "(path rejected — outside workspace or invalid)"
        return entry
    if not resolved.is_file():
        entry["content"] = "(missing)"
        return entry
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        entry["content"] = f"(read error: {exc})"
        return entry
    entry["exists"] = True
    entry["size_bytes"] = len(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    entry["line_count"] = text.count("\n") + (1 if text else 0)
    body, truncated = _truncate_file_content(text, max_chars_per_file)
    entry["truncated"] = truncated
    entry["content"] = body
    return entry


def build_read_only_file_snapshots(
    workspace_root: str | Path,
    read_paths: list[str],
    *,
    max_chars_per_file: int = DEFAULT_SNAPSHOT_MAX_CHARS_PER_FILE,
) -> tuple[str, SnapshotMeta]:
    """Build READ-ONLY FILE SNAPSHOT section from profile allowlist only."""
    allowlist = [_normalize_rel_path(p) for p in read_paths if isinstance(p, str) and p.strip()]
    lines = [
        "READ-ONLY FILE SNAPSHOT (authoritative — analyze from this text only):",
    ]
    meta = SnapshotMeta(injected=True, paths=[])
    for rel in allowlist:
        snap = read_allowlisted_file_snapshot(
            workspace_root, rel, max_chars_per_file=max_chars_per_file,
        )
        lines.append(f"\n### {snap['path']}")
        lines.append(f"- exists: {'yes' if snap['exists'] else 'no'}")
        lines.append(f"- size_bytes: {snap['size_bytes']}")
        lines.append(f"- line_count: {snap['line_count']}")
        if snap["truncated"]:
            lines.append("- truncated: yes [TRUNCATED]")
        else:
            lines.append("- truncated: no")
        lines.append("```")
        lines.append(snap["content"] or "(empty)")
        lines.append("```")
        if snap["exists"]:
            meta.file_count += 1
            meta.paths.append(snap["path"])
    return "\n".join(lines), meta


def run_workspace_precheck_structured(
    cwd: str | Path,
    *,
    expected_head: str = "",
    policy: dict[str, Any] | None = None,
    workspace_profile: str = "",
    workspace_mode: str = "",
) -> PrecheckResult:
    """Run git prechecks; return blocker before Claude when policy violated."""
    path = Path(cwd)
    lines = [
        "AUTOMATED PRECHECK RESULTS (authoritative — do not re-run via tool markup):",
        f"- cwd: {path}",
    ]
    if workspace_profile:
        lines.append(f"- workspace_profile: {workspace_profile}")
    if workspace_mode:
        lines.append(f"- workspace_mode: {workspace_mode}")

    if not path.is_dir():
        blocker = f"BLOCKER: workspace runner context missing\n- cwd not found: {path}"
        return PrecheckResult(ok=False, blocker=blocker, text="\n".join(lines))

    head_val = ""
    porcelain = ""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        head_val = (head.stdout or "").strip() if head.returncode == 0 else ""
        if head.returncode != 0:
            blocker = (
                "BLOCKER: expected head mismatch\n"
                f"- git rev-parse HEAD failed (exit {head.returncode})"
            )
            lines.append(f"- git rev-parse HEAD: (error exit {head.returncode})")
            return PrecheckResult(ok=False, blocker=blocker, text="\n".join(lines), head=head_val)
        lines.append(f"- git rev-parse HEAD: {head_val}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        blocker = f"BLOCKER: workspace precheck failed\n- git rev-parse HEAD: {exc}"
        lines.append(f"- git rev-parse HEAD: could not check ({exc})")
        return PrecheckResult(ok=False, blocker=blocker, text="\n".join(lines))

    if expected_head and head_val and head_val != expected_head:
        blocker = (
            "BLOCKER: expected head mismatch\n"
            f"- expected: {expected_head}\n"
            f"- actual: {head_val}"
        )
        lines.append(f"- expected HEAD mismatch: expected {expected_head}")
        return PrecheckResult(
            ok=False, blocker=blocker, text="\n".join(lines), head=head_val, porcelain=porcelain,
        )

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        porcelain = (status.stdout or "").rstrip("\n\r")
        lines.append(f"- git status --porcelain: {porcelain or '(clean)'}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        blocker = f"BLOCKER: workspace precheck failed\n- git status: {exc}"
        lines.append(f"- git status --porcelain: could not check ({exc})")
        return PrecheckResult(ok=False, blocker=blocker, text="\n".join(lines), head=head_val)

    if isinstance(policy, dict):
        dirty = verify_dirty_set(porcelain_output=porcelain, policy=policy)
        if not dirty.ok:
            reason = dirty.reason or "dirty tree not allowed"
            blocker = f"BLOCKER: dirty tree before analysis\n- {reason}"
            return PrecheckResult(
                ok=False, blocker=blocker, text="\n".join(lines),
                head=head_val, porcelain=porcelain,
            )
        if is_report_only_readonly_policy(policy) and porcelain:
            docs_dirty, _blocking = classify_dirty_entries_report_only_analysis(
                porcelain, policy=policy,
            )
            if docs_dirty:
                lines.append(
                    "- pre-existing docs tracker dirty files (non-blocking): "
                    + ", ".join(docs_dirty)
                )

    lines.extend([
        "",
        "SNAPSHOT WORKER CONTRACT:",
        "- You have NO generic tools in this worker path (claude --print --tools \"\").",
        "- Do NOT output <tool_call> XML markup or request Bash/Read/Write tools.",
        "- Analyze ONLY from AUTOMATED PRECHECK RESULTS and READ-ONLY FILE SNAPSHOT below.",
        "- If the snapshot is insufficient, return: BLOCKER: insufficient snapshot",
        "- Do NOT write Task.md, Context.md, or any file inside the Twinpet repo.",
    ])
    if is_report_only_readonly_policy(policy):
        lines.extend([
            "",
            REPORT_WRITE_BRIDGE_INSTRUCTION,
        ])
    else:
        lines.extend([
            "- For final report markdown, wrap content between REPORT_BEGIN and REPORT_END lines.",
            "- Report may be saved outside the repo by agentchattr when possible.",
        ])
    return PrecheckResult(
        ok=True, text="\n".join(lines), head=head_val, porcelain=porcelain,
    )


def run_workspace_precheck(cwd: str | Path, *, expected_head: str = "") -> str:
    """Legacy text-only precheck (non-blocking). Prefer run_workspace_precheck_structured."""
    result = run_workspace_precheck_structured(cwd, expected_head=expected_head)
    return result.text


def build_on_demand_worker_augmentation(
    cwd: str | Path,
    item: dict[str, Any],
    policy: dict[str, Any] | None,
    *,
    config: dict | None = None,
) -> tuple[str | None, str | None, SnapshotMeta]:
    """Precheck + source manifest only (no automatic file body injection)."""
    from on_demand_snapshots import (
        build_on_demand_snapshot_contract,
        build_source_file_manifest,
        get_snapshot_budget,
    )

    wpc = item.get("workspace_policy_context") if isinstance(item, dict) else {}
    if not isinstance(wpc, dict):
        wpc = {}
    if not isinstance(policy, dict):
        return None, "BLOCKER: workspace policy snapshot missing", SnapshotMeta()

    workspace = policy.get("workspace") or {}
    expected_head = str(workspace.get("expected_head") or "")
    profile = str(policy.get("policy_id") or wpc.get("policy_id") or "")
    mode = str(policy.get("mode") or wpc.get("policy_mode") or "")

    pre = run_workspace_precheck_structured(
        cwd,
        expected_head=expected_head,
        policy=policy,
        workspace_profile=profile,
        workspace_mode=mode,
    )
    if not pre.ok:
        return None, pre.blocker, SnapshotMeta()

    read_paths = list(policy.get("read_paths") or [])
    manifest_text, manifest_count = build_source_file_manifest(cwd, read_paths)
    suggested = list(policy.get("suggested_initial_snapshot_paths") or [
        "src/components/PaymentModal.tsx",
        "src/components/PaymentModal.css",
    ])
    contract = build_on_demand_snapshot_contract(suggested_paths=suggested)
    budget = get_snapshot_budget(config)
    pre_text = pre.text.replace(
        "Analyze ONLY from AUTOMATED PRECHECK RESULTS and READ-ONLY FILE SNAPSHOT below.",
        "Analyze from AUTOMATED PRECHECK RESULTS and on-demand snapshots you request.",
    ).replace(
        "- If the snapshot is insufficient, return: BLOCKER: insufficient snapshot",
        "- If you need source content, request snapshots via SNAPSHOT_REQUEST_BEGIN/END.",
    )
    sections = [
        pre_text,
        "",
        manifest_text,
        "",
        contract,
        "",
        f"- max_snapshot_rounds_per_worker: {budget.max_rounds_per_worker}",
        f"- max_total_snapshot_chars_per_worker: {budget.max_total_chars_per_worker}",
        f"- manifest_paths: {manifest_count}",
    ]
    return "\n".join(sections), None, SnapshotMeta(injected=False, file_count=0, paths=read_paths)


def build_docs_only_worker_augmentation(
    cwd: str | Path,
    item: dict[str, Any],
    policy: dict[str, Any] | None,
    *,
    config: dict | None = None,
) -> tuple[str | None, str | None, SnapshotMeta]:
    """Build precheck + file snapshot injection. Returns (text, blocker, meta)."""
    wpc = item.get("workspace_policy_context") if isinstance(item, dict) else {}
    if not isinstance(wpc, dict):
        wpc = {}
    if not isinstance(policy, dict):
        return None, "BLOCKER: workspace policy snapshot missing", SnapshotMeta()

    workspace = policy.get("workspace") or {}
    expected_head = str(workspace.get("expected_head") or "")
    profile = str(policy.get("policy_id") or wpc.get("policy_id") or "")
    mode = str(policy.get("mode") or wpc.get("policy_mode") or "")

    pre = run_workspace_precheck_structured(
        cwd,
        expected_head=expected_head,
        policy=policy,
        workspace_profile=profile,
        workspace_mode=mode,
    )
    if not pre.ok:
        return None, pre.blocker, SnapshotMeta()

    read_paths = list(policy.get("read_paths") or [])
    max_chars = get_snapshot_max_chars(config)
    snapshot_text, meta = build_read_only_file_snapshots(
        cwd, read_paths, max_chars_per_file=max_chars,
    )

    sections = [pre.text, "", snapshot_text]
    return "\n".join(sections), None, meta


def detect_tool_call_leakage(text: str) -> dict[str, str] | None:
    """Return leakage diagnostics when output contains literal tool-call markup."""
    if not text or not TOOL_CALL_TAG_RE.search(text):
        return None
    tool_name = ""
    command = ""
    m_name = TOOL_NAME_RE.search(text)
    if m_name:
        tool_name = m_name.group(1).strip()
    m_cmd = TOOL_COMMAND_RE.search(text)
    if m_cmd:
        command = " ".join(m_cmd.group(1).split())
        if len(command) > 300:
            command = command[:300] + "..."
    return {
        "tool_name": tool_name or "(unknown)",
        "command": command,
    }


def format_tool_call_leakage_blocker(
    *,
    role: str,
    cwd: str | Path | None,
    workspace_profile: str,
    workspace_mode: str,
    prompt_id: str,
    leakage: dict[str, str],
    snapshot_meta: SnapshotMeta | None = None,
    snapshot_mode: bool = False,
) -> str:
    """Structured blocker when Claude emitted tool-call markup."""
    title = (
        "BLOCKER: tool-call markup leaked despite snapshot mode"
        if snapshot_mode
        else "BLOCKER: tool-call markup leaked instead of execution"
    )
    meta = snapshot_meta or SnapshotMeta()
    lines = [
        title,
        "",
        "Diagnostics:",
        f"- role: {role or 'unknown'}",
        f"- cwd: {cwd or '(unknown)'}",
        f"- workspace_profile: {workspace_profile or '(none)'}",
        f"- workspace_mode: {workspace_mode or '(none)'}",
        f"- prompt_id: {prompt_id or '(none)'}",
        f"- first leaked tool_name: {leakage.get('tool_name', '(unknown)')}",
        f"- file snapshot injected: {'yes' if meta.injected else 'no'}",
        f"- files injected: {meta.file_count}",
    ]
    cmd = leakage.get("command")
    if cmd:
        lines.append(f"- first leaked command: {cmd}")
    lines.extend([
        "",
        "Claude --print runs with tools disabled. Use injected snapshots only.",
        "For external report output in read-only report-flow sessions, use",
        "REPORT_FILE_WRITE_BEGIN/END (see worker contract).",
        "Do not emit <tool_call> XML. Return plain-text analysis or BLOCKER: insufficient snapshot.",
    ])
    return "\n".join(lines)


def extract_report_block(text: str) -> str | None:
    """Extract markdown between REPORT_BEGIN and REPORT_END markers."""
    if REPORT_BEGIN_MARKER not in text or REPORT_END_MARKER not in text:
        return None
    start = text.index(REPORT_BEGIN_MARKER) + len(REPORT_BEGIN_MARKER)
    end = text.index(REPORT_END_MARKER, start)
    block = text[start:end].strip()
    return block or None


def extract_report_file_write_block(text: str) -> dict[str, str] | None:
    """Parse REPORT_FILE_WRITE_BEGIN/END scoped worker report-write block."""
    if REPORT_FILE_WRITE_BEGIN_MARKER not in text or REPORT_FILE_WRITE_END_MARKER not in text:
        return None
    start = text.index(REPORT_FILE_WRITE_BEGIN_MARKER) + len(REPORT_FILE_WRITE_BEGIN_MARKER)
    end = text.index(REPORT_FILE_WRITE_END_MARKER, start)
    body = text[start:end].strip()
    if not body:
        return None
    header, _, markdown = body.partition("\n---\n")
    if not markdown.strip():
        if body.startswith("#"):
            markdown = body
            header = ""
        else:
            return None
    fields: dict[str, str] = {}
    for key, pattern in _REPORT_FILE_WRITE_FIELD_PATTERNS.items():
        match = pattern.search(header)
        fields[key] = match.group(1).strip() if match else ""
    path = fields.get("path", "").strip().strip('"').strip("'")
    if not path:
        return None
    return {
        "path": path,
        "status": fields.get("status") or "PASS",
        "summary": fields.get("summary") or "report written via worker report-write bridge",
        "next_role": fields.get("next_role") or "coordinator",
        "content": markdown.strip(),
    }


def format_report_ready_after_worker_write(
    *,
    path: str,
    status: str = "PASS",
    summary: str = "",
    notes: str = "",
) -> str:
    return (
        "REPORT_READY\n\n"
        f"Status:\n{status}\n\n"
        f"Report:\n{path}\n\n"
        f"Summary:\n{summary or 'report written'}\n\n"
        "Next recommended role:\ncoordinator\n\n"
        f"Notes:\n{notes or 'Report written through scoped worker report-write bridge.'}\n"
    )


def format_report_write_failed_reply(*, path: str, reason: str) -> str:
    return (
        "REPORT_WRITE_FAILED\n\n"
        f"Reason:\n{reason}\n\n"
        f"Expected report:\n{path}\n\n"
        "Status:\nFAIL\n"
    )


def format_report_write_retry_instruction(*, path: str = "") -> str:
    lines = [
        "REPORT_WRITE_RETRY",
        "",
        "Reason:",
        "Write tool-call markup cannot be executed in read-only report-flow mode.",
        "",
        "Action:",
        "Re-emit your report using exactly one REPORT_FILE_WRITE_BEGIN / REPORT_FILE_WRITE_END block.",
        "Do not emit <tool_call> XML.",
    ]
    if path:
        lines.extend(["", f"Expected report:\n{path}"])
    return "\n".join(lines)


def _external_report_roots(policy: dict[str, Any]) -> list[str]:
    from report_orchestration import resolve_external_report_write_roots
    return list(resolve_external_report_write_roots(policy))


def write_validated_external_report(
    raw_path: str,
    content: str,
    policy: dict[str, Any],
) -> tuple[bool, str, str]:
    """Validate path under Ai-Report roots and write .md content. Returns ok, path, error."""
    from report_orchestration import validate_report_path

    if not (content or "").strip():
        return False, raw_path, "empty report content"
    roots = _external_report_roots(policy)
    ok, reason, resolved = validate_report_path(raw_path, allowed_roots=roots)
    if not ok:
        blocker = reason if str(reason).startswith("BLOCKER:") else f"BLOCKER: {reason}"
        return False, raw_path, blocker
    assert resolved is not None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        return False, str(resolved), f"report write failed: {exc}"
    return True, str(resolved), ""


def extract_write_tool_call_intent(text: str) -> dict[str, str] | None:
    """Extract path/content from a leaked Write tool-call block when possible."""
    if not TOOL_CALL_TAG_RE.search(text or ""):
        return None
    if not re.search(r"<\s*tool_name\s*>\s*Write\s*</\s*tool_name\s*>", text, re.IGNORECASE):
        return None
    path = ""
    for pattern in _WRITE_PATH_PATTERNS:
        match = pattern.search(text)
        if match:
            path = match.group(1).strip().strip('"').strip("'")
            break
    if not path:
        return None
    content = ""
    for pattern in _WRITE_CONTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            content = match.group(1).strip()
            break
    return {"path": path, "content": content}


def try_process_scoped_worker_report_output(
    text: str,
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    worker_context: dict[str, Any] | None = None,
) -> str | None:
    """Handle REPORT_FILE_WRITE bridge / legacy REPORT_BEGIN for report-only sessions."""
    if not isinstance(policy, dict) or not policy_uses_report_write_bridge(policy):
        return None
    from report_orchestration import parse_report_ready, read_report_file, validate_report_path

    trusted_cli = is_trusted_direct_repo_cli_policy(policy)
    expected_path = resolve_expected_trusted_cli_report_path(
        policy,
        role=role,
        worker_context=worker_context,
    )

    parsed = parse_report_ready(text)
    if parsed:
        roots = _external_report_roots(policy)
        ok, _reason, resolved = validate_report_path(parsed.report_path, allowed_roots=roots)
        if ok and resolved:
            if expected_path:
                matches, _expected = trusted_cli_report_path_is_expected(
                    parsed.report_path,
                    policy,
                    role=role,
                    worker_context=worker_context,
                )
                if not matches:
                    if trusted_cli:
                        return format_trusted_cli_unexpected_report_path_blocker(
                            actual_path=parsed.report_path,
                            expected_path=expected_path,
                            policy=policy,
                        )
                    return format_report_write_failed_reply(
                        path=parsed.report_path,
                        reason=f"path must be {expected_path}",
                    )
            read_ok, _, _, _ = read_report_file(resolved)
            if read_ok:
                return text.strip()

    bridge = extract_report_file_write_block(text)
    if bridge:
        bridge_path = bridge["path"]
        if expected_path:
            matches, _expected = trusted_cli_report_path_is_expected(
                bridge_path,
                policy,
                role=role,
                worker_context=worker_context,
            )
            if not matches:
                if trusted_cli:
                    return format_trusted_cli_unexpected_report_path_blocker(
                        actual_path=bridge_path,
                        expected_path=expected_path,
                        policy=policy,
                    )
                return format_report_write_failed_reply(
                    path=bridge_path,
                    reason=f"path must be {expected_path}",
                )
        ok, saved_path, err = write_validated_external_report(
            bridge_path, bridge["content"], policy,
        )
        if ok:
            return format_report_ready_after_worker_write(
                path=saved_path,
                status=bridge.get("status") or "PASS",
                summary=bridge.get("summary") or "",
            )
        return format_report_write_failed_reply(path=bridge_path, reason=err)

    legacy = extract_report_block(text)
    if legacy:
        targets = [
            p for p in (policy.get("report_paths") or [])
            if isinstance(p, str) and p.strip()
        ]
        if targets:
            ok, saved_path, err = write_validated_external_report(
                targets[0], legacy, policy,
            )
            if ok:
                return format_report_ready_after_worker_write(path=saved_path)
            return format_report_write_failed_reply(path=targets[0], reason=err)
    return None


def build_report_orchestrated_worker_context(
    policy: dict[str, Any] | None,
    *,
    channel: str = "general",
    worker_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge session report paths/roots for report-orchestrated normalization."""
    ctx = dict(worker_context or {})
    if not isinstance(policy, dict):
        return ctx
    report_paths = list(ctx.get("report_paths") or policy.get("report_paths") or [])
    report_roots = list(
        ctx.get("allowed_report_roots")
        or policy.get("external_report_write_roots")
        or [],
    )
    ai_base = next(
        (
            root for root in report_roots
            if str(root).replace("\\", "/").lower().endswith("/ai-report")
        ),
        r"C:\Users\Narachat\OneDrive\Ai-Report",
    )
    agy_root = next(
        (
            root for root in report_roots
            if str(root).replace("\\", "/").lower().endswith("/ai-report/agy")
        ),
        f"{ai_base}\\agy",
    )
    codex_root = next(
        (
            root for root in report_roots
            if str(root).replace("\\", "/").lower().endswith("/ai-report/codex")
        ),
        f"{ai_base}\\codex",
    )
    by_role = dict(ctx.get("report_paths_by_role") or {})
    if not by_role.get("developer") and report_paths:
        by_role.setdefault("developer", str(report_paths[0]))
    by_role.setdefault("ui_lead", f"{agy_root}\\{channel}-ux-review.md")
    by_role.setdefault("reviewer", f"{codex_root}\\{channel}-codex-review.md")
    ctx.update({
        "workspace_policy": policy,
        "policy_id": policy.get("policy_id"),
        "policy_mode": policy.get("mode"),
        "report_paths": report_paths,
        "allowed_report_roots": report_roots,
        "report_paths_by_role": by_role,
    })
    return ctx


def normalize_report_orchestrated_worker_output(
    text: str,
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    worker_context: dict[str, Any] | None = None,
    channel: str = "general",
) -> str | None:
    """Normalize REPORT_FILE_WRITE / REPORT_READY output for report-orchestrated roles."""
    if not isinstance(policy, dict) or not policy_uses_report_write_bridge(policy):
        return None
    sample = (text or "").strip()
    if not sample:
        return None
    if sample.startswith("REPORT_WRITE_FAILED") or sample.startswith("BLOCKER:"):
        return sample

    ctx = build_report_orchestrated_worker_context(
        policy,
        channel=channel,
        worker_context=worker_context,
    )
    expected_path = resolve_expected_trusted_cli_report_path(
        policy,
        role=role,
        worker_context=ctx,
    )

    if (
        REPORT_FILE_WRITE_BEGIN_MARKER in sample
        and REPORT_FILE_WRITE_END_MARKER not in sample
    ):
        return format_report_write_failed_reply(
            path=expected_path or "(unknown)",
            reason="incomplete REPORT_FILE_WRITE block (missing REPORT_FILE_WRITE_END)",
        )

    if (
        REPORT_FILE_WRITE_BEGIN_MARKER in sample
        or sample.startswith("REPORT_READY")
        or REPORT_BEGIN_MARKER in sample
    ):
        normalized = try_process_scoped_worker_report_output(
            sample,
            policy,
            role=role,
            worker_context=ctx,
        )
        if normalized is not None:
            return normalized
        if REPORT_FILE_WRITE_BEGIN_MARKER in sample:
            return format_report_write_failed_reply(
                path=expected_path or "(unknown)",
                reason="invalid or unparsable REPORT_FILE_WRITE block",
            )
    return None


def process_relay_worker_report_output(
    text: str,
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    worker_context: dict[str, Any] | None = None,
    channel: str = "general",
) -> str | None:
    """Codex relay alias for report-orchestrated output normalization."""
    return normalize_report_orchestrated_worker_output(
        text,
        policy,
        role=role,
        worker_context=worker_context,
        channel=channel,
    )


def try_recover_write_tool_call_leakage(
    text: str,
    policy: dict[str, Any] | None,
) -> str | None:
    """Translate allowed Write tool-call leaks into scoped report writes when safe."""
    if not isinstance(policy, dict) or not policy_uses_report_write_bridge(policy):
        return None
    from report_orchestration import is_twinpet_repo_path, validate_report_path

    intent = extract_write_tool_call_intent(text)
    if not intent:
        return None
    raw_path = intent["path"]
    if is_twinpet_repo_path(raw_path):
        return (
            "BLOCKER: report write targets Twinpet workspace (forbidden)\n\n"
            f"path={raw_path}\n"
            "Use REPORT_FILE_WRITE_BEGIN/END with an external Ai-Report .md path only."
        )
    roots = _external_report_roots(policy)
    ok, reason, _resolved = validate_report_path(raw_path, allowed_roots=roots)
    if not ok:
        blocker = reason if str(reason).startswith("BLOCKER:") else f"BLOCKER: {reason}"
        return blocker
    content = intent.get("content") or ""
    if not content.strip():
        return format_report_write_retry_instruction(path=raw_path)
    ok, saved_path, err = write_validated_external_report(raw_path, content, policy)
    if ok:
        return format_report_ready_after_worker_write(
            path=saved_path,
            notes="Recovered from Write tool-call intent via scoped worker report-write bridge.",
        )
    return format_report_write_failed_reply(path=raw_path, reason=err)


def resolve_trusted_cli_report_outcome(
    captured: str,
    policy: dict[str, Any] | None,
    *,
    role: str = "developer",
    queue_item: dict[str, Any] | None = None,
    worker_context: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    repair_rounds_used: int | None = None,
    max_repair_rounds: int | None = None,
) -> TrustedCliReportOutcome:
    """Shared trusted CLI report-output resolver for wrapper and coordinator."""
    from report_orchestration import parse_report_ready

    if not isinstance(policy, dict) or not is_trusted_direct_repo_cli_policy(policy):
        return TrustedCliReportOutcome(kind="none")

    sample = (captured or "").strip()
    if not sample:
        return TrustedCliReportOutcome(kind="none")

    repair_used, repair_max = resolve_trusted_cli_repair_rounds(
        queue_item,
        repair_rounds_used=repair_rounds_used,
        max_repair_rounds=max_repair_rounds,
    )

    profile = str(policy.get("policy_id") or "")
    mode = str(policy.get("mode") or "")
    report_path = resolve_expected_trusted_cli_report_path(
        policy,
        role=role,
        worker_context=worker_context,
    )
    common_blocker = dict(
        text=sample,
        policy=policy,
        workspace_profile=profile,
        workspace_mode=mode,
        report_path=report_path,
        repair_round=repair_used,
        max_repair_rounds=repair_max,
    )
    salvage_kwargs = dict(
        role=role,
        queue_item=queue_item,
        worker_context=worker_context,
        cwd=cwd,
    )

    def _salvage_outcome(
        salvage: TrustedCliSalvageResult,
    ) -> TrustedCliReportOutcome:
        return TrustedCliReportOutcome(
            kind="report_ready",
            report_ready=salvage.report_ready,
            salvage=salvage,
        )

    # Already synthesized REPORT_READY / legacy REPORT_BEGIN passthrough.
    if parse_report_ready(sample) is not None or sample.startswith("REPORT_READY"):
        parsed = parse_report_ready(sample)
        if parsed:
            matches, expected_or_reason = trusted_cli_report_path_is_expected(
                parsed.report_path,
                policy,
                role=role,
                worker_context=worker_context,
            )
            if not matches:
                return TrustedCliReportOutcome(
                    kind="blocker",
                    repair_reason="unexpected_path",
                    text=format_trusted_cli_unexpected_report_path_blocker(
                        actual_path=parsed.report_path,
                        expected_path=report_path or expected_or_reason,
                        policy=policy,
                        workspace_profile=profile,
                        workspace_mode=mode,
                    ),
                )
        return TrustedCliReportOutcome(kind="report_ready", report_ready=sample)
    if REPORT_BEGIN_MARKER in sample:
        return TrustedCliReportOutcome(kind="report_ready", report_ready=sample)

    native_write = detect_native_write_permission_prompt(sample)
    salvage: TrustedCliSalvageResult | None = None

    # 1) Refusal without a valid expected file -> terminal BLOCKER.
    if detect_trusted_cli_prompt_injection_refusal(sample):
        salvage = attempt_salvage_trusted_cli_existing_report(
            policy,
            sample,
            native_write_warning=native_write,
            reread_attempts=1,
            **salvage_kwargs,
        )
        if salvage.salvaged:
            return _salvage_outcome(salvage)
        return TrustedCliReportOutcome(
            kind="blocker",
            repair_reason="refusal",
            text=format_trusted_cli_refusal_blocker(**common_blocker),
            salvage=salvage,
        )

    # 2) File-first: immediate read of expected policy/session report path.
    salvage = attempt_salvage_trusted_cli_existing_report(
        policy,
        sample,
        native_write_warning=native_write,
        reread_attempts=1,
        **salvage_kwargs,
    )
    if salvage.salvaged:
        return _salvage_outcome(salvage)

    # 3) Complete stdout Markdown -> runtime save -> REPORT_READY.
    captured_report = try_capture_trusted_cli_stdout_report(captured, policy)
    if captured_report is not None:
        return TrustedCliReportOutcome(kind="report_ready", report_ready=captured_report)

    # 4) Legacy REPORT_FILE_WRITE bridge at expected path (compatibility only).
    bridge = try_process_scoped_worker_report_output(
        captured,
        policy,
        role=role,
        worker_context=worker_context,
    )
    if bridge is not None:
        if bridge.lstrip().startswith("BLOCKER:"):
            repair_reason = "unexpected_path"
            if TRUSTED_CLI_UNEXPECTED_PATH_BLOCKER_PREFIX in bridge:
                repair_reason = "unexpected_path"
            return TrustedCliReportOutcome(
                kind="blocker",
                repair_reason=repair_reason,
                text=bridge,
            )
        return TrustedCliReportOutcome(kind="report_ready", report_ready=bridge)

    # 5) Bounded expected-path re-read before repair/terminal.
    salvage = attempt_salvage_trusted_cli_existing_report(
        policy,
        sample,
        native_write_warning=native_write,
        reread_attempts=TRUSTED_CLI_REPORT_FILE_REREAD_ATTEMPTS,
        reread_delay_sec=TRUSTED_CLI_REPORT_FILE_REREAD_DELAY_SEC,
        **salvage_kwargs,
    )
    if salvage.salvaged:
        return _salvage_outcome(salvage)

    wrapper_blocker = _trusted_cli_wrapper_blocker_kind(sample)
    if wrapper_blocker == "refusal" or wrapper_blocker == "unexpected_path":
        return TrustedCliReportOutcome(
            kind="blocker",
            repair_reason=wrapper_blocker,
            text=sample,
            salvage=salvage,
        )
    if wrapper_blocker in ("native_write", "incomplete"):
        repair_reason = wrapper_blocker
        if repair_used < repair_max:
            return TrustedCliReportOutcome(
                kind="correction_prompt",
                repair_reason=repair_reason,
                salvage=salvage,
            )
        if repair_reason == "native_write":
            blocker = format_trusted_cli_native_write_blocker(
                cwd=cwd,
                queue_item=queue_item,
                **common_blocker,
            )
        else:
            blocker = format_trusted_cli_incomplete_report_blocker(
                **common_blocker,
                salvage=salvage,
            )
        return TrustedCliReportOutcome(
            kind="blocker",
            repair_reason=repair_reason,
            text=blocker,
            salvage=salvage,
        )

    repair_reason = "native_write" if native_write else "incomplete"
    if repair_used < repair_max:
        return TrustedCliReportOutcome(
            kind="correction_prompt",
            repair_reason=repair_reason,
            salvage=salvage,
        )

    if repair_reason == "native_write":
        blocker = format_trusted_cli_native_write_blocker(
            cwd=cwd,
            queue_item=queue_item,
            **common_blocker,
        )
    else:
        blocker = format_trusted_cli_incomplete_report_blocker(**common_blocker, salvage=salvage)
    return TrustedCliReportOutcome(
        kind="blocker",
        repair_reason=repair_reason,
        text=blocker,
        salvage=salvage,
    )


def process_trusted_cli_worker_report_output(
    captured: str,
    policy: dict[str, Any] | None,
    *,
    queue_item: dict[str, Any] | None = None,
    worker_context: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    repair_rounds_used: int | None = None,
    max_repair_rounds: int | None = None,
) -> str | None:
    """Trusted CLI wrapper adapter — delegates to shared resolver."""
    ctx = worker_context
    if ctx is None and isinstance(queue_item, dict) and isinstance(policy, dict):
        ctx = {
            "workspace_policy": policy,
            "policy_id": policy.get("policy_id"),
            "report_paths": list(policy.get("report_paths") or []),
        }
    used, max_r = resolve_trusted_cli_repair_rounds(
        queue_item,
        repair_rounds_used=repair_rounds_used,
        max_repair_rounds=max_repair_rounds,
    )
    outcome = resolve_trusted_cli_report_outcome(
        captured,
        policy,
        queue_item=queue_item,
        worker_context=ctx,
        cwd=cwd,
        repair_rounds_used=used,
        max_repair_rounds=max_r,
    )
    if outcome.kind == "report_ready":
        return outcome.report_ready
    if outcome.kind == "blocker":
        return outcome.text
    return None


def process_claude_worker_report_output(
    captured: str,
    policy: dict[str, Any] | None,
    *,
    queue_item: dict[str, Any] | None = None,
    worker_context: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
    repair_rounds_used: int | None = None,
    max_repair_rounds: int | None = None,
) -> str | None:
    """Return transformed worker output when report-write bridge handles it."""
    if isinstance(policy, dict) and is_trusted_direct_repo_cli_policy(policy):
        handled = process_trusted_cli_worker_report_output(
            captured,
            policy,
            queue_item=queue_item,
            worker_context=worker_context,
            cwd=cwd,
            repair_rounds_used=repair_rounds_used,
            max_repair_rounds=max_repair_rounds,
        )
        if handled is not None:
            return handled
        return None
    handled = try_process_scoped_worker_report_output(
        captured,
        policy,
        worker_context=worker_context,
    )
    if handled is not None:
        return handled
    leakage = detect_tool_call_leakage(captured)
    if leakage and str(leakage.get("tool_name", "")).lower() == "write":
        return try_recover_write_tool_call_leakage(captured, policy)
    return None


def _path_outside_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve().relative_to(workspace_root.resolve())
        return False
    except ValueError:
        return True


def try_save_external_analysis_report(
    text: str,
    policy: dict[str, Any],
    workspace_root: str | Path,
) -> ReportSaveResult:
    """Save REPORT_BEGIN/END block to external report paths only (never Twinpet repo)."""
    block = extract_report_block(text)
    if not block:
        return ReportSaveResult()
    if not isinstance(policy, dict):
        return ReportSaveResult(
            notes=["PASS WITH NOTES: report in REPORT_BEGIN/REPORT_END above (no policy context)."],
        )
    mode = policy.get("mode")
    if mode not in ("docs-only", "read-only"):
        return ReportSaveResult()
    if not is_report_only_readonly_policy(policy) and list(policy.get("write_files") or []):
        return try_save_legacy_docs_report(text, policy, workspace_root)

    root = Path(workspace_root).resolve()
    ext_paths = list(policy.get("report_paths") or [])
    if not ext_paths:
        return ReportSaveResult(
            notes=[
                "PASS WITH NOTES: external report path not configured; "
                "report available in REPORT_BEGIN/REPORT_END above.",
            ],
        )
    notes: list[str] = []
    for ext in ext_paths:
        if not isinstance(ext, str) or not ext.strip():
            continue
        try:
            target = Path(ext).resolve()
        except (OSError, ValueError):
            notes.append(f"PASS WITH NOTES: invalid external report path: {ext}")
            continue
        if not _path_outside_workspace(target, root):
            notes.append(f"skipped in-repo report path (read-only analysis): {ext}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(block, encoding="utf-8")
            return ReportSaveResult(saved=True, path=str(target), notes=[f"- saved report to: {target}"])
        except OSError as exc:
            notes.append(f"PASS WITH NOTES: could not save external report {ext}: {exc}")
    if not notes:
        notes.append(
            "PASS WITH NOTES: external report save unavailable; "
            "report available in REPORT_BEGIN/REPORT_END above.",
        )
    return ReportSaveResult(notes=notes)


def try_save_legacy_docs_report(
    text: str,
    policy: dict[str, Any],
    workspace_root: str | Path,
) -> ReportSaveResult:
    """Legacy docs-only save (workspace + external paths) for non-report-only profiles."""
    block = extract_report_block(text)
    if not block:
        return ReportSaveResult()
    root = Path(workspace_root)
    saved_lines: list[str] = []
    for rel in list(policy.get("write_files") or []):
        if not isinstance(rel, str) or not rel.endswith(".md"):
            continue
        norm = _normalize_rel_path(rel)
        target = _resolve_allowlisted_file(root, norm)
        if target is None:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(block, encoding="utf-8")
            saved_lines.append(f"- saved report to workspace: {norm}")
        except OSError as exc:
            saved_lines.append(f"- could not save {norm}: {exc}")
    for ext in list(policy.get("report_paths") or []):
        if not isinstance(ext, str) or not ext.strip():
            continue
        try:
            p = Path(ext)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(block, encoding="utf-8")
            return ReportSaveResult(saved=True, path=str(p), notes=saved_lines + [f"- saved report to: {ext}"])
        except OSError as exc:
            saved_lines.append(f"- could not save external report {ext}: {exc}")
    if saved_lines:
        return ReportSaveResult(saved=any("saved report" in s for s in saved_lines), notes=saved_lines)
    return ReportSaveResult()


def try_save_docs_only_report(
    text: str,
    policy: dict[str, Any],
    workspace_root: str | Path,
) -> list[str]:
    """Backward-compatible wrapper returning status lines."""
    result = try_save_external_analysis_report(text, policy, workspace_root)
    return result.notes if result.notes else (
        [f"- saved report to: {result.path}"] if result.saved else []
    )


def worker_context_from_queue_item(item: dict[str, Any] | None) -> dict[str, Any]:
    """Extract worker context fields from queue item (denormalized wpc + session)."""
    if not isinstance(item, dict):
        return {}
    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return {}
    snap = item.get("_snapshot_meta")
    snap_count = 0
    snap_injected = False
    if isinstance(snap, dict):
        snap_injected = bool(snap.get("injected"))
        snap_count = int(snap.get("file_count") or 0)
    elif isinstance(snap, SnapshotMeta):
        snap_injected = snap.injected
        snap_count = snap.file_count
    return {
        "role": wpc.get("session_role"),
        "policy_id": wpc.get("policy_id"),
        "policy_mode": wpc.get("policy_mode"),
        "prompt_id": wpc.get("prompt_id"),
        "has_prompt_body": bool(wpc.get("has_prompt_body")),
        "workspace_root": wpc.get("workspace_root"),
        "session_id": wpc.get("session_id"),
        "snapshot_injected": snap_injected,
        "snapshot_file_count": snap_count,
    }


def snapshot_meta_from_item(item: dict[str, Any] | None) -> SnapshotMeta:
    if not isinstance(item, dict):
        return SnapshotMeta()
    raw = item.get("_snapshot_meta")
    if isinstance(raw, SnapshotMeta):
        return raw
    if isinstance(raw, dict):
        return SnapshotMeta(
            injected=bool(raw.get("injected")),
            file_count=int(raw.get("file_count") or 0),
            paths=list(raw.get("paths") or []),
        )
    return SnapshotMeta()


def default_scratch_cwd() -> str:
    return DEFAULT_SCRATCH_CWD
