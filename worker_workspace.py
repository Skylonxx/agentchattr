"""Workspace-bound Claude worker helpers (cwd, precheck, tool-call leakage)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from workspace_policy_runtime import (
    DEFAULT_SCRATCH_CWD,
    external_cwd_enabled_for_mode,
    normalize_workspace_root,
    resolve_exec_cwd_for_item,
)

TOOL_CALL_TAG_RE = re.compile(r"<\s*tool_call\b", re.IGNORECASE)
TOOL_NAME_RE = re.compile(r"<\s*tool_name\s*>([^<]+)</\s*tool_name\s*>", re.IGNORECASE)
TOOL_COMMAND_RE = re.compile(
    r"<\s*command\s*>([^<]+)</\s*command\s*>",
    re.IGNORECASE | re.DOTALL,
)


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


def run_workspace_precheck(cwd: str | Path, *, expected_head: str = "") -> str:
    """Run read-only git prechecks in workspace cwd; return text for prompt injection."""
    path = Path(cwd)
    lines = ["AUTOMATED PRECHECK RESULTS (authoritative — do not re-run via tool markup):"]
    if not path.is_dir():
        lines.append(f"- workspace cwd missing: {path}")
        return "\n".join(lines)

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        head_val = (head.stdout or "").strip() if head.returncode == 0 else f"(error exit {head.returncode})"
        lines.append(f"- git rev-parse HEAD: {head_val}")
        if expected_head and head_val and head_val != expected_head:
            lines.append(f"- expected HEAD mismatch: expected {expected_head}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"- git rev-parse HEAD: could not check ({exc})")

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        short = (status.stdout or "").strip()
        lines.append(f"- git status --porcelain: {short or '(clean)'}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"- git status --porcelain: could not check ({exc})")

    lines.append(
        "Use these results directly. Do NOT emit <tool_call> markup. "
        "Reply with PROGRESS while reading files, then READY_FOR_COORDINATOR or final tokens."
    )
    return "\n".join(lines)


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
) -> str:
    """Structured blocker when Claude emitted tool-call markup instead of executing."""
    lines = [
        "BLOCKER: tool-call markup leaked instead of execution",
        "",
        "Diagnostics:",
        f"- role: {role or 'unknown'}",
        f"- cwd: {cwd or '(unknown)'}",
        f"- workspace_profile: {workspace_profile or '(none)'}",
        f"- workspace_mode: {workspace_mode or '(none)'}",
        f"- prompt_id: {prompt_id or '(none)'}",
        f"- first leaked tool_name: {leakage.get('tool_name', '(unknown)')}",
    ]
    cmd = leakage.get("command")
    if cmd:
        lines.append(f"- first leaked command: {cmd}")
    lines.extend([
        "",
        "Claude --print runs with tools disabled. Do not emit <tool_call> XML.",
        "Use AUTOMATED PRECHECK RESULTS and plain-text PROGRESS / READY_FOR_COORDINATOR.",
    ])
    return "\n".join(lines)


def worker_context_from_queue_item(item: dict[str, Any] | None) -> dict[str, Any]:
    """Extract worker context fields from queue item (denormalized wpc + session)."""
    if not isinstance(item, dict):
        return {}
    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return {}
    return {
        "role": wpc.get("session_role"),
        "policy_id": wpc.get("policy_id"),
        "policy_mode": wpc.get("policy_mode"),
        "prompt_id": wpc.get("prompt_id"),
        "has_prompt_body": bool(wpc.get("has_prompt_body")),
        "workspace_root": wpc.get("workspace_root"),
        "session_id": wpc.get("session_id"),
    }


def default_scratch_cwd() -> str:
    return DEFAULT_SCRATCH_CWD
