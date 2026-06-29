"""Scoped on-demand snapshot requests for report-orchestrated read-only sessions."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from report_orchestration import is_report_orchestrated_policy
from worker_workspace import (
    _normalize_rel_path,
    _resolve_allowlisted_file,
    _truncate_file_content,
    is_report_only_readonly_policy,
    is_workspace_bound_queue_item,
)

log = logging.getLogger(__name__)

SNAPSHOT_REQUEST_BEGIN = "SNAPSHOT_REQUEST_BEGIN"
SNAPSHOT_REQUEST_END = "SNAPSHOT_REQUEST_END"
SNAPSHOT_FILE_BEGIN = "SNAPSHOT_FILE_BEGIN"
SNAPSHOT_FILE_END = "SNAPSHOT_FILE_END"

DEFAULT_MAX_INITIAL_REPORT_FLOW_PROMPT_CHARS = 50_000
DEFAULT_MAX_SNAPSHOT_REQUEST_PATHS = 3
DEFAULT_MAX_SNAPSHOT_RESPONSE_CHARS = 60_000
DEFAULT_MAX_SNAPSHOT_ROUNDS_PER_WORKER = 4
DEFAULT_MAX_TOTAL_SNAPSHOT_CHARS_PER_WORKER = 120_000
DEFAULT_MAX_SINGLE_FILE_SNAPSHOT_CHARS = 45_000

_FORBIDDEN_SNAPSHOT_SEGMENTS = (
    ".git/",
    ".env",
    "config.local.toml",
    "node_modules/",
)

_PATH_LINE_RE = re.compile(r"^\s*[-*]?\s*(.+?)\s*$")


@dataclass
class SnapshotBudget:
    max_request_paths: int = DEFAULT_MAX_SNAPSHOT_REQUEST_PATHS
    max_response_chars: int = DEFAULT_MAX_SNAPSHOT_RESPONSE_CHARS
    max_rounds_per_worker: int = DEFAULT_MAX_SNAPSHOT_ROUNDS_PER_WORKER
    max_total_chars_per_worker: int = DEFAULT_MAX_TOTAL_SNAPSHOT_CHARS_PER_WORKER
    max_single_file_chars: int = DEFAULT_MAX_SINGLE_FILE_SNAPSHOT_CHARS
    max_initial_prompt_chars: int = DEFAULT_MAX_INITIAL_REPORT_FLOW_PROMPT_CHARS


@dataclass
class SnapshotWorkerState:
    round: int = 0
    total_chars: int = 0
    request_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "total_chars": self.total_chars,
            "request_ids": list(self.request_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SnapshotWorkerState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            round=int(data.get("round") or 0),
            total_chars=int(data.get("total_chars") or 0),
            request_ids=list(data.get("request_ids") or []),
        )


@dataclass
class ParsedSnapshotRequest:
    reason: str
    paths: list[str]


@dataclass
class SnapshotRequestResult:
    ok: bool
    blocker: str = ""
    response_text: str = ""
    approved_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    response_chars: int = 0
    request_id: str = ""
    updated_state: SnapshotWorkerState | None = None
    diagnostics: str = ""


def get_snapshot_budget(config: dict | None) -> SnapshotBudget:
    section = (config or {}).get("report_orchestration")
    if not isinstance(section, dict):
        return SnapshotBudget()
    return SnapshotBudget(
        max_request_paths=int(section.get("max_snapshot_request_paths", DEFAULT_MAX_SNAPSHOT_REQUEST_PATHS)),
        max_response_chars=int(section.get("max_snapshot_response_chars", DEFAULT_MAX_SNAPSHOT_RESPONSE_CHARS)),
        max_rounds_per_worker=int(
            section.get("max_snapshot_rounds_per_worker", DEFAULT_MAX_SNAPSHOT_ROUNDS_PER_WORKER),
        ),
        max_total_chars_per_worker=int(
            section.get("max_total_snapshot_chars_per_worker", DEFAULT_MAX_TOTAL_SNAPSHOT_CHARS_PER_WORKER),
        ),
        max_single_file_chars=int(
            section.get("max_single_file_snapshot_chars", DEFAULT_MAX_SINGLE_FILE_SNAPSHOT_CHARS),
        ),
        max_initial_prompt_chars=int(
            section.get("max_initial_report_flow_prompt_chars", DEFAULT_MAX_INITIAL_REPORT_FLOW_PROMPT_CHARS),
        ),
    )


def is_on_demand_snapshot_enabled(policy: dict[str, Any] | None, config: dict | None = None) -> bool:
    if not isinstance(policy, dict) or not is_report_orchestrated_policy(policy):
        return False
    if policy.get("on_demand_snapshots") is False:
        return False
    section = (config or {}).get("report_orchestration")
    if isinstance(section, dict) and section.get("on_demand_snapshots_enabled") is False:
        return False
    if policy.get("on_demand_snapshots") is True:
        return True
    if policy.get("analysis_report_only"):
        return True
    return bool(isinstance(section, dict) and section.get("on_demand_snapshots_enabled", True))


def is_on_demand_snapshot_mode(
    item: dict[str, Any] | None,
    policy: dict[str, Any] | None,
    *,
    config: dict | None = None,
) -> bool:
    """True when worker should receive manifest + on-demand snapshot contract (no full auto-inject)."""
    if not is_on_demand_snapshot_enabled(policy, config):
        return False
    if isinstance(item, dict):
        relay_meta = item.get("relay_meta")
        if isinstance(relay_meta, dict) and relay_meta.get("handoff_repair"):
            return False
        wpc = item.get("workspace_policy_context")
        if isinstance(wpc, dict) and (
            wpc.get("handoff_repair") or wpc.get("skip_snapshot_injection")
        ):
            return False
    if not is_workspace_bound_queue_item(item):
        return False
    if not is_report_only_readonly_policy(policy):
        return False
    mode = (policy or {}).get("mode")
    wpc = item.get("workspace_policy_context") if isinstance(item, dict) else {}
    if not mode and isinstance(wpc, dict):
        mode = wpc.get("policy_mode")
    return str(mode or "") in ("read-only", "read-only-analysis", "docs-only")


def _allowed_read_paths(policy: dict[str, Any]) -> list[str]:
    return [
        _normalize_rel_path(p)
        for p in (policy.get("read_paths") or [])
        if isinstance(p, str) and p.strip()
    ]


def _path_role_label(rel_path: str) -> str:
    low = rel_path.lower()
    if "paymentmodal" in low.replace("/", ""):
        return "payment-modal"
    if "pospage" in low.replace("/", ""):
        return "pos-page"
    if "usecheckout" in low.replace("/", ""):
        return "checkout-hook"
    if "asynccheckout" in low.replace("/", ""):
        return "async-checkout"
    if "cartutils" in low.replace("/", ""):
        return "cart-math"
    if low.endswith(".spec.ts") or "/tests/" in low:
        return "test"
    if low.endswith(".md"):
        return "docs"
    return "source"


def build_source_file_manifest(
    workspace_root: str | Path,
    read_paths: list[str],
) -> tuple[str, int]:
    """Build manifest section (path, size, sha256, role) without file bodies."""
    root = Path(workspace_root)
    lines = [
        "SOURCE FILE MANIFEST (on-demand — bodies are NOT included until requested):",
        f"- manifest_paths: {len(read_paths)}",
    ]
    for rel in read_paths:
        norm = _normalize_rel_path(rel)
        resolved = _resolve_allowlisted_file(root, norm)
        if resolved is None or not resolved.is_file():
            lines.append(
                f"- path: {norm} | exists: no | size_bytes: 0 | sha256: (missing) | role: {_path_role_label(norm)}",
            )
            continue
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            lines.append(
                f"- path: {norm} | exists: yes | size_bytes: ? | sha256: (read error: {exc}) | role: {_path_role_label(norm)}",
            )
            continue
        digest = hashlib.sha256(raw).hexdigest()
        lines.append(
            f"- path: {norm} | exists: yes | size_bytes: {len(raw)} | sha256: {digest} | role: {_path_role_label(norm)}",
        )
    return "\n".join(lines), len(read_paths)


def build_on_demand_snapshot_contract(
    *,
    suggested_paths: list[str] | None = None,
) -> str:
    lines = [
        "ON-DEMAND SNAPSHOT CONTRACT:",
        "- snapshot_mode: on_demand",
        "- initial_snapshot_injected: false",
        "- You are running with tools disabled.",
        "- Do not emit <tool_call> XML.",
        "- To read source files, use SNAPSHOT_REQUEST_BEGIN/END (plain text, not tools).",
        "- To write external reports, use REPORT_FILE_WRITE_BEGIN/END.",
        "- Do not attempt to modify Twinpet repo files.",
        "",
        "SNAPSHOT_REQUEST_BEGIN",
        "Reason: <why these files are needed>",
        "Paths:",
        "- src/components/PaymentModal.tsx",
        "- src/components/PaymentModal.css",
        "SNAPSHOT_REQUEST_END",
        "",
        "Runtime validates paths against the manifest allowlist and returns bounded snapshots only.",
        "If more context is needed, request additional snapshots within budget.",
        "When ready, write the report using REPORT_FILE_WRITE_BEGIN/END.",
    ]
    if suggested_paths:
        lines.extend([
            "",
            "Suggested first request for PaymentModal analysis:",
        ])
        for path in suggested_paths:
            lines.append(f"- {path}")
    return "\n".join(lines)


def parse_snapshot_request(text: str | None) -> ParsedSnapshotRequest | None:
    if not text or SNAPSHOT_REQUEST_BEGIN not in text or SNAPSHOT_REQUEST_END not in text:
        return None
    start = text.index(SNAPSHOT_REQUEST_BEGIN) + len(SNAPSHOT_REQUEST_BEGIN)
    end = text.index(SNAPSHOT_REQUEST_END, start)
    body = text[start:end].strip()
    reason = ""
    reason_match = re.search(r"^\s*Reason\s*:\s*(.+?)\s*$", body, re.IGNORECASE | re.MULTILINE)
    if reason_match:
        reason = reason_match.group(1).strip()
    paths: list[str] = []
    in_paths = False
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^\s*Paths\s*:\s*$", stripped, re.IGNORECASE):
            in_paths = True
            inline = re.sub(r"^\s*Paths\s*:\s*", "", stripped, flags=re.IGNORECASE).strip()
            if inline:
                paths.append(_normalize_rel_path(inline))
            continue
        if not in_paths:
            continue
        if not stripped:
            continue
        match = _PATH_LINE_RE.match(stripped)
        if match:
            candidate = _normalize_rel_path(match.group(1).strip().strip('"').strip("'"))
            if candidate:
                paths.append(candidate)
    if not paths:
        return None
    # dedupe preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return ParsedSnapshotRequest(reason=reason, paths=unique)


def _is_forbidden_snapshot_path(rel_path: str) -> bool:
    low = rel_path.lower().replace("\\", "/")
    if low.startswith("/") or re.match(r"^[a-zA-Z]:", rel_path):
        return True
    if ".." in low.split("/"):
        return True
    return any(seg in low for seg in _FORBIDDEN_SNAPSHOT_SEGMENTS)


def validate_snapshot_paths(
    paths: list[str],
    *,
    policy: dict[str, Any],
    workspace_root: str | Path,
) -> tuple[list[str], list[str], list[str]]:
    """Return approved, denied, denial_reasons."""
    allowlist = set(_allowed_read_paths(policy))
    approved: list[str] = []
    denied: list[str] = []
    reasons: list[str] = []
    root = Path(workspace_root)
    for raw in paths:
        norm = _normalize_rel_path(raw)
        if _is_forbidden_snapshot_path(norm):
            denied.append(norm)
            reasons.append(f"{norm}: forbidden path pattern")
            continue
        if norm not in allowlist:
            denied.append(norm)
            reasons.append(f"{norm}: outside read allowlist")
            continue
        resolved = _resolve_allowlisted_file(root, norm)
        if resolved is None:
            denied.append(norm)
            reasons.append(f"{norm}: path escape rejected")
            continue
        if not resolved.is_file():
            denied.append(norm)
            reasons.append(f"{norm}: file not found")
            continue
        approved.append(norm)
    return approved, denied, reasons


def _read_bounded_snapshot_file(
    workspace_root: str | Path,
    rel_path: str,
    *,
    max_single_file_chars: int,
) -> tuple[str, int, str, int, bool]:
    root = Path(workspace_root)
    resolved = _resolve_allowlisted_file(root, rel_path)
    if resolved is None or not resolved.is_file():
        return "", 0, "", 0, False
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    body, truncated = _truncate_file_content(text, max_single_file_chars)
    if not body.strip() and len(text) > max_single_file_chars:
        return "", len(raw), digest, 0, True
    return body, len(raw), digest, len(body), truncated


def build_snapshot_response(
    *,
    request_id: str,
    workspace_root: str | Path,
    approved_paths: list[str],
    budget: SnapshotBudget,
    remaining_total_chars: int,
) -> tuple[str, int, list[str]]:
    lines = [
        "SNAPSHOT_RESPONSE",
        f"Request ID: {request_id}",
        "Allowed paths:",
    ]
    for path in approved_paths:
        lines.append(f"- {path}")
    lines.append("")
    total_chars = 0
    blocked: list[str] = []
    response_cap = min(budget.max_response_chars, remaining_total_chars)
    for rel in approved_paths:
        if total_chars >= response_cap:
            blocked.append(rel)
            continue
        per_file_cap = min(
            budget.max_single_file_chars,
            response_cap - total_chars,
        )
        content, size_bytes, digest, content_chars, truncated = _read_bounded_snapshot_file(
            workspace_root,
            rel,
            max_single_file_chars=per_file_cap,
        )
        if not content and size_bytes > budget.max_single_file_chars:
            blocked.append(rel)
            continue
        lines.extend([
            SNAPSHOT_FILE_BEGIN,
            f"Path: {rel}",
            f"Size: {size_bytes}",
            f"Sha256: {digest}",
            "---",
            content,
            SNAPSHOT_FILE_END,
            "",
        ])
        total_chars += content_chars
    if blocked:
        lines.extend([
            "BLOCKED PATHS (snapshot too large or budget exhausted):",
            *[f"- {p}" for p in blocked],
        ])
    lines.extend([
        "Continue your analysis.",
        "If more context is needed, request additional snapshots using SNAPSHOT_REQUEST_BEGIN/END.",
        "If ready, write the report using REPORT_FILE_WRITE_BEGIN/END.",
    ])
    response = "\n".join(lines)
    return response, total_chars, blocked


def format_snapshot_diagnostics(
    *,
    workspace_profile: str = "",
    workspace_mode: str = "",
    manifest_paths: int = 0,
    initial_prompt_chars: int = 0,
    request_id: str = "",
    requested_paths: list[str] | None = None,
    approved_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
    snapshot_response_chars: int = 0,
    snapshot_round: int = 0,
    total_snapshot_chars: int = 0,
    budget_remaining: int = 0,
    reason: str = "",
) -> str:
    lines = [
        "snapshot_mode=on_demand",
        "initial_snapshot_injected=false",
        f"manifest_paths={manifest_paths}",
        f"initial_prompt_chars={initial_prompt_chars}",
        f"snapshot_request_id={request_id or '(none)'}",
        f"requested_paths={', '.join(requested_paths or []) or '(none)'}",
        f"approved_paths={', '.join(approved_paths or []) or '(none)'}",
        f"denied_paths={', '.join(denied_paths or []) or '(none)'}",
        f"snapshot_response_chars={snapshot_response_chars}",
        f"snapshot_round={snapshot_round}",
        f"total_snapshot_chars={total_snapshot_chars}",
        f"budget_remaining={budget_remaining}",
        f"workspace_profile={workspace_profile or '(none)'}",
        f"workspace_mode={workspace_mode or '(none)'}",
    ]
    if reason.strip():
        lines.append(f"reason={reason.strip()}")
    return "\n".join(lines)


def process_snapshot_request(
    text: str,
    *,
    workspace_root: str | Path,
    policy: dict[str, Any],
    state: SnapshotWorkerState,
    budget: SnapshotBudget,
    workspace_profile: str = "",
    workspace_mode: str = "",
) -> SnapshotRequestResult:
    parsed = parse_snapshot_request(text)
    if parsed is None:
        return SnapshotRequestResult(
            ok=False,
            blocker=(
                "BLOCKER: worker did not request snapshot or write report\n"
                "- expected SNAPSHOT_REQUEST_BEGIN/END or REPORT_FILE_WRITE_BEGIN/END"
            ),
        )

    if state.round >= budget.max_rounds_per_worker:
        diag = format_snapshot_diagnostics(
            workspace_profile=workspace_profile,
            workspace_mode=workspace_mode,
            snapshot_round=state.round,
            total_snapshot_chars=state.total_chars,
            budget_remaining=max(0, budget.max_total_chars_per_worker - state.total_chars),
            reason="max_snapshot_rounds_per_worker exceeded",
        )
        return SnapshotRequestResult(
            ok=False,
            blocker=f"BLOCKER: snapshot budget exceeded\n{diag}",
        )

    if len(parsed.paths) > budget.max_request_paths:
        diag = format_snapshot_diagnostics(
            workspace_profile=workspace_profile,
            workspace_mode=workspace_mode,
            requested_paths=parsed.paths,
            snapshot_round=state.round,
            reason=f"too many paths (max {budget.max_request_paths})",
        )
        return SnapshotRequestResult(
            ok=False,
            blocker=f"BLOCKER: snapshot request denied\n{diag}",
        )

    approved, denied, denial_reasons = validate_snapshot_paths(
        parsed.paths,
        policy=policy,
        workspace_root=workspace_root,
    )
    if not approved:
        diag = format_snapshot_diagnostics(
            workspace_profile=workspace_profile,
            workspace_mode=workspace_mode,
            requested_paths=parsed.paths,
            denied_paths=denied,
            snapshot_round=state.round,
            reason="; ".join(denial_reasons) or "no approved paths",
        )
        return SnapshotRequestResult(
            ok=False,
            blocker=f"BLOCKER: snapshot request denied\n{diag}",
            denied_paths=denied,
        )

    remaining = budget.max_total_chars_per_worker - state.total_chars
    if remaining <= 0:
        diag = format_snapshot_diagnostics(
            workspace_profile=workspace_profile,
            workspace_mode=workspace_mode,
            total_snapshot_chars=state.total_chars,
            budget_remaining=0,
            reason="total snapshot char budget exhausted",
        )
        return SnapshotRequestResult(
            ok=False,
            blocker=f"BLOCKER: snapshot budget exceeded\n{diag}",
        )

    request_id = uuid.uuid4().hex[:12]
    response_text, response_chars, blocked = build_snapshot_response(
        request_id=request_id,
        workspace_root=workspace_root,
        approved_paths=approved,
        budget=budget,
        remaining_total_chars=remaining,
    )

    if blocked and not response_chars:
        diag = format_snapshot_diagnostics(
            workspace_profile=workspace_profile,
            workspace_mode=workspace_mode,
            requested_paths=parsed.paths,
            approved_paths=approved,
            denied_paths=blocked,
            reason="requested snapshot too large",
        )
        return SnapshotRequestResult(
            ok=False,
            blocker=f"BLOCKER: requested snapshot too large\n{diag}",
            approved_paths=approved,
            denied_paths=denied + blocked,
        )

    updated = SnapshotWorkerState(
        round=state.round + 1,
        total_chars=state.total_chars + response_chars,
        request_ids=[*state.request_ids, request_id],
    )
    diag = format_snapshot_diagnostics(
        workspace_profile=workspace_profile,
        workspace_mode=workspace_mode,
        request_id=request_id,
        requested_paths=parsed.paths,
        approved_paths=approved,
        denied_paths=denied,
        snapshot_response_chars=response_chars,
        snapshot_round=updated.round,
        total_snapshot_chars=updated.total_chars,
        budget_remaining=max(0, budget.max_total_chars_per_worker - updated.total_chars),
        reason=parsed.reason,
    )
    log.info("on-demand snapshot response\n%s", diag)
    return SnapshotRequestResult(
        ok=True,
        response_text=response_text,
        approved_paths=approved,
        denied_paths=denied,
        response_chars=response_chars,
        request_id=request_id,
        updated_state=updated,
        diagnostics=diag,
    )


def worker_output_is_terminal_report(text: str) -> bool:
    if "REPORT_FILE_WRITE_BEGIN" in (text or "") and "REPORT_FILE_WRITE_END" in (text or ""):
        return True
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip().upper()
            break
    if first in ("REPORT_READY", "REPORT_WRITE_FAILED"):
        return True
    if first.startswith("BLOCKER:") or first.startswith("BLOCKED:"):
        return True
    return False


def load_snapshot_state_from_item(item: dict[str, Any] | None) -> SnapshotWorkerState:
    if not isinstance(item, dict):
        return SnapshotWorkerState()
    wpc = item.get("workspace_policy_context")
    if isinstance(wpc, dict):
        return SnapshotWorkerState.from_dict(wpc.get("snapshot_state"))
    return SnapshotWorkerState()


def save_snapshot_state_to_item(item: dict[str, Any], state: SnapshotWorkerState) -> dict[str, Any]:
    out = dict(item)
    wpc = dict(out.get("workspace_policy_context") or {})
    wpc["snapshot_state"] = state.to_dict()
    out["workspace_policy_context"] = wpc
    return out
