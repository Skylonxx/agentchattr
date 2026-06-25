"""Sandbox flow transcript and closure export (V2-C).

Writes redacted markdown artifacts under approved Ai-Report roots only.
Pure helpers are deterministic and unit-testable; export functions perform I/O.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from package_relay import ALLOWED_EXTENSIONS
import package_relay as _package_relay
from safety_invariants import redact_secrets

REPORT_PATH_RE = re.compile(
    r"^\s*report_path\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_SENSITIVE_PATH_MARKERS = (
    ".env",
    "config.local.toml",
    "id_rsa",
    "credentials",
    "secret",
)

_MAX_SNIPPET_CHARS = 500


@dataclass(frozen=True)
class FlowExportResult:
    transcript_path: str | None
    closure_path: str | None
    ok: bool
    error: str = ""


def _normalize_roots() -> list[Path]:
    roots: list[Path] = []
    for root in _package_relay.APPROVED_ROOTS:
        try:
            roots.append(root.resolve())
        except OSError:
            roots.append(root)
    return roots


def _is_under_approved_root(resolved: Path) -> bool:
    target_lower = str(resolved).lower()
    for root in _normalize_roots():
        root_lower = str(root).lower()
        if target_lower == root_lower:
            return True
        if target_lower.startswith(root_lower + os.sep):
            return True
    return False


def parse_report_path(text: str | None) -> str | None:
    """Extract REPORT_PATH from workflow output (typically line 2)."""
    if not text or not str(text).strip():
        return None
    match = REPORT_PATH_RE.search(str(text))
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def validate_approved_report_path(raw_path: str) -> tuple[bool, str]:
    """Validate REPORT_PATH is absolute, under approved roots, safe extension."""
    if not raw_path or not str(raw_path).strip():
        return False, "empty path"
    candidate = Path(str(raw_path).strip().strip('"').strip("'"))
    if not candidate.is_absolute():
        return False, "path must be absolute"
    if ".." in candidate.parts:
        return False, "path traversal rejected"
    low = str(candidate).lower()
    if any(marker in low for marker in _SENSITIVE_PATH_MARKERS):
        return False, "sensitive path rejected"
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return False, f"cannot resolve path: {exc}"
    if not _is_under_approved_root(resolved):
        return False, f"path outside approved Ai-Report roots: {resolved}"
    suffix = resolved.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return False, f"unsupported extension {suffix!r} (allowed: .md, .txt)"
    return True, str(resolved)


def validate_output_root(raw_root: str) -> tuple[bool, str]:
    """Ensure flow_start_output_root is under approved Ai-Report roots."""
    if not raw_root or not str(raw_root).strip():
        return False, "empty output root"
    candidate = Path(str(raw_root).strip())
    if not candidate.is_absolute():
        return False, "output root must be absolute"
    if ".." in candidate.parts:
        return False, "path traversal rejected"
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return False, f"cannot resolve output root: {exc}"
    if not _is_under_approved_root(resolved):
        return False, f"output root outside approved Ai-Report roots: {resolved}"
    return True, str(resolved)


def _candidate_to_resolved_path(raw: str) -> Path | None:
    """Parse a path candidate from export text; None if not absolute."""
    p = raw.strip().rstrip(".,;:)")
    if not p:
        return None
    if p.startswith("\\\\"):
        return Path(p)
    if len(p) >= 2 and p[1] == ":":
        return Path(p)
    return None


def _is_approved_absolute_path(raw: str) -> bool:
    """True when an absolute path candidate resolves under approved Ai-Report roots."""
    candidate = _candidate_to_resolved_path(raw)
    if candidate is None:
        return False
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return _is_under_approved_root(resolved)


# UNC paths and drive-letter absolutes (backslash, forward slash, spaces in segments).
_ABS_PATH_PATTERNS = (
    re.compile(r"\\\\[^\"\'\r\n<>|]+"),
    re.compile(r"[A-Za-z]:(?:[/\\][^\"\'\r\n<>|]+)+"),
)


def redact_export_text(text: str) -> str:
    """Redact secrets and paths outside approved roots for export."""
    s = redact_secrets(text or "")

    def _repl(m: re.Match) -> str:
        if _is_approved_absolute_path(m.group(0)):
            return m.group(0)
        return "[REDACTED_PATH]"

    for pat in _ABS_PATH_PATTERNS:
        s = pat.sub(_repl, s)
    return s


def _artifact_paths(output_root: str, session_id: int) -> tuple[Path, Path]:
    root = Path(output_root)
    transcript = root / f"sandbox-flow-{session_id}-transcript.md"
    closure = root / f"sandbox-flow-{session_id}-closure.md"
    return transcript, closure


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _final_status(flow_state: dict) -> str:
    phase = str(flow_state.get("phase", "")).lower()
    if phase == "closure":
        return "PASS"
    reason = str(flow_state.get("halt_reason", "")).lower()
    if "blocked" in reason:
        return "BLOCKED"
    return "HALTED"


def _cap_snippet(text: str, limit: int = _MAX_SNIPPET_CHARS) -> str:
    s = redact_export_text(text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def build_transcript_markdown(
    *,
    session: dict,
    template: dict | None,
    flow_state: dict,
    messages: list[dict],
    data_dir: str | None = None,
) -> str:
    """Build redacted transcript markdown (no file I/O)."""
    session_id = session.get("id", "?")
    channel = session.get("channel", "general")
    tmpl_name = (template or {}).get("name") or session.get("template_name", "?")
    tmpl_id = session.get("template_id", "?")
    goal = session.get("goal", "")
    cast = session.get("cast", {})
    fs = flow_state or {}
    started = session.get("started_at")
    ended = session.get("updated_at") or time.time()

    lines = [
        f"# Sandbox Flow Transcript — Session {session_id}",
        "",
        "## Session",
        f"- session_id: {session_id}",
        f"- channel: {channel}",
        f"- template_id: {tmpl_id}",
        f"- template_name: {tmpl_name}",
        f"- task / goal: {redact_export_text(goal)}",
        f"- cast: {cast}",
        f"- started_at: {_fmt_time(started)}",
        f"- ended_at: {_fmt_time(ended)}",
        f"- final phase: {fs.get('phase', '—')}",
        f"- final status: {_final_status(fs)}",
        "",
        "## Loop Counters",
        f"- ux_loops: {fs.get('ux_loops', 0)}",
        f"- eng_loops: {fs.get('eng_loops', 0)}",
        f"- total_steps: {fs.get('total_steps', 0)}",
        "",
    ]
    if fs.get("halt_reason"):
        lines.extend([
            "## Halt Reason",
            redact_export_text(str(fs.get("halt_reason"))),
            "",
        ])

    lines.extend(["## Verdict History", ""])
    for v in fs.get("verdicts") or []:
        role = v.get("role", "?")
        token = v.get("token", "?")
        ts = _fmt_time(v.get("time"))
        lines.append(f"- [{ts}] **{role}**: {token}")
    lines.append("")

    lines.extend(["## Timeline", ""])
    for msg in messages:
        sender = msg.get("sender", "?")
        mtype = msg.get("type", msg.get("msg_type", "chat"))
        ts = msg.get("time") or msg.get("timestamp") or ""
        text = redact_export_text(str(msg.get("text", "")))
        if mtype in (
            "session_start", "session_phase", "session_flow_verdict",
            "session_flow_closure", "session_flow_halted", "session_end",
            "system", "chat",
        ) or str(channel) == str(msg.get("channel", channel)):
            lines.append(f"### [{mtype}] {sender} {ts}".strip())
            lines.append(text if text else "_(empty)_")
            lines.append("")

    lines.extend(["## Debug Footer", ""])
    lines.append(f"- waiting_on: {session.get('waiting_on', '—')}")
    lines.append(f"- state: {session.get('state', '—')}")
    if data_dir:
        sid = session_id
        ch = channel
        lines.append(f"- data_dir: {redact_export_text(data_dir)}")
        lines.append(f"- queue hints: *_queue.jsonl under data_dir for cast agents")
        lines.append(f"- audit: sandbox_flow_audit.jsonl (if sandbox start used)")
    lines.append(f"- report_path (flow state): {redact_export_text(str(fs.get('report_path', '')))}")
    lines.append("")
    return "\n".join(lines)


def build_closure_markdown(
    *,
    session: dict,
    flow_state: dict,
    transcript_path: str | None,
    closure_path: str | None,
    last_output_snippet: str = "",
) -> str:
    """Build redacted closure markdown (no file I/O)."""
    fs = flow_state or {}
    status = _final_status(fs)
    session_id = session.get("id", "?")
    channel = session.get("channel", "general")
    cast = session.get("cast", {})
    report_path = fs.get("report_path") or (fs.get("closure_summary") or {}).get("report_path", "")

    lines = [
        f"# Sandbox Flow Closure — Session {session_id}",
        "",
        f"## Final Status: {status}",
        "",
        "## Summary",
        f"- task: {redact_export_text(session.get('goal', ''))}",
        f"- channel: {channel}",
        f"- session_id: {session_id}",
        f"- cast: {cast}",
        f"- final phase: {fs.get('phase', '—')}",
        f"- ux_loops: {fs.get('ux_loops', 0)}",
        f"- eng_loops: {fs.get('eng_loops', 0)}",
        f"- total_steps: {fs.get('total_steps', 0)}",
        "",
        "## Verdicts",
    ]
    for v in fs.get("verdicts") or []:
        lines.append(f"- {v.get('role', '?')}: {v.get('token', '?')}")
    lines.append("")

    if report_path:
        lines.extend([
            "## Report Path",
            redact_export_text(str(report_path)),
            "",
        ])

    lines.extend([
        "## Artifact Paths",
        f"- transcript_path: {transcript_path or '—'}",
        f"- closure_path: {closure_path or '—'}",
        "",
    ])

    summary = fs.get("closure_summary") or {}
    if summary.get("final_notes"):
        lines.extend([
            "## Final Notes",
            _cap_snippet(str(summary.get("final_notes"))),
            "",
        ])
    if fs.get("halt_reason"):
        lines.extend([
            "## Halt Reason",
            _cap_snippet(str(fs.get("halt_reason"))),
            "",
        ])
    if last_output_snippet:
        lines.extend([
            "## Last Relevant Output (redacted, capped)",
            _cap_snippet(last_output_snippet),
            "",
        ])

    return "\n".join(lines)


def export_sandbox_flow_transcript(
    *,
    session: dict,
    template: dict | None,
    flow_state: dict,
    messages: list[dict],
    output_root: str,
    data_dir: str | None = None,
) -> tuple[str | None, str]:
    ok, root_or_err = validate_output_root(output_root)
    if not ok:
        return None, root_or_err
    path, _ = _artifact_paths(root_or_err, int(session.get("id", 0)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_transcript_markdown(
            session=session,
            template=template,
            flow_state=flow_state,
            messages=messages,
            data_dir=data_dir,
        )
        path.write_text(body, encoding="utf-8")
        return str(path), ""
    except OSError as exc:
        return None, str(exc)


def export_sandbox_flow_closure(
    *,
    session: dict,
    flow_state: dict,
    output_root: str,
    transcript_path: str | None,
    last_output_snippet: str = "",
) -> tuple[str | None, str]:
    ok, root_or_err = validate_output_root(output_root)
    if not ok:
        return None, root_or_err
    _, path = _artifact_paths(root_or_err, int(session.get("id", 0)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_closure_markdown(
            session=session,
            flow_state=flow_state,
            transcript_path=transcript_path,
            closure_path=str(path),
            last_output_snippet=last_output_snippet,
        )
        path.write_text(body, encoding="utf-8")
        return str(path), ""
    except OSError as exc:
        return None, str(exc)


def export_sandbox_flow_artifacts(
    *,
    session: dict,
    template: dict | None,
    flow_state: dict,
    messages: list[dict],
    output_root: str,
    data_dir: str | None = None,
    last_output_snippet: str = "",
) -> FlowExportResult:
    """Write transcript and closure markdown under validated output_root."""
    t_path, t_err = export_sandbox_flow_transcript(
        session=session,
        template=template,
        flow_state=flow_state,
        messages=messages,
        output_root=output_root,
        data_dir=data_dir,
    )
    if not t_path:
        return FlowExportResult(None, None, False, t_err or "transcript export failed")

    c_path, c_err = export_sandbox_flow_closure(
        session=session,
        flow_state=flow_state,
        output_root=output_root,
        transcript_path=t_path,
        last_output_snippet=last_output_snippet,
    )
    if not c_path:
        return FlowExportResult(t_path, None, False, c_err or "closure export failed")

    return FlowExportResult(t_path, c_path, True)


def format_flow_export_error_message(
    *,
    session: dict,
    error: str,
) -> str:
    """System channel message when sandbox flow artifact export fails."""
    session_id = session.get("id", "?")
    channel = session.get("channel", "general")
    lines = [
        "SANDBOX FLOW EXPORT FAILED",
        f"Session: {session_id} | Channel: {channel}",
        f"Reason: {redact_export_text(error or 'unknown export error')}",
    ]
    return "\n".join(lines)


def format_flow_export_system_message(
    *,
    session: dict,
    flow_state: dict,
    transcript_path: str | None,
    closure_path: str | None,
) -> str:
    """Human-readable system channel message with safe artifact paths."""
    status = _final_status(flow_state)
    session_id = session.get("id", "?")
    channel = session.get("channel", "general")
    if status == "PASS":
        headline = f"SANDBOX FLOW CLOSURE — PASS"
    elif status == "BLOCKED":
        headline = f"SANDBOX FLOW BLOCKED — {flow_state.get('halt_reason', 'blocked')}"
    else:
        headline = f"SANDBOX FLOW HALTED — {flow_state.get('halt_reason', 'halted')}"

    lines = [
        headline,
        f"Session: {session_id} | Channel: {channel}",
    ]
    if status != "PASS" and flow_state.get("halt_reason"):
        lines.append(f"Reason: {redact_export_text(str(flow_state.get('halt_reason')))}")
    if closure_path:
        lines.append(f"Closure: {closure_path}")
    if transcript_path:
        lines.append(f"Transcript: {transcript_path}")
    return "\n".join(lines)
