"""Claude print worker timeout resolution and diagnostics for session turns."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from workspace_policy_runtime import load_persisted_session_record

DEFAULT_TIMEOUT_SECS = 120
PROMPT_MEMO_TIMEOUT_SECS = 600
DOCS_ONLY_TIMEOUT_SECS = 600
IMPLEMENTATION_TIMEOUT_SECS = 900
SUBPROCESS_GRACE_SECS = 30
PROMPT_BODY_LEN_HINT = 1500


def get_session_worker_timeouts(cfg: dict | None) -> dict[str, int]:
    """Load session worker timeout settings from config (safe defaults)."""
    section = (cfg or {}).get("session_worker_timeouts")
    if not isinstance(section, dict):
        section = {}
    return {
        "default_secs": int(section.get("default_secs", DEFAULT_TIMEOUT_SECS)),
        "prompt_memo_secs": int(section.get("prompt_memo_secs", PROMPT_MEMO_TIMEOUT_SECS)),
        "docs_only_secs": int(section.get("docs_only_secs", DOCS_ONLY_TIMEOUT_SECS)),
        "implementation_secs": int(section.get("implementation_secs", IMPLEMENTATION_TIMEOUT_SECS)),
        "subprocess_grace_secs": int(section.get("subprocess_grace_secs", SUBPROCESS_GRACE_SECS)),
    }


def _session_from_item(item: dict[str, Any], *, data_dir: str | Path | None) -> dict[str, Any] | None:
    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return None
    session_id = wpc.get("session_id")
    if session_id is None or data_dir is None:
        return None
    return load_persisted_session_record(data_dir, int(session_id))


def _wpc_from_item(item: dict[str, Any] | None) -> dict[str, Any]:
    wpc = item.get("workspace_policy_context") if isinstance(item, dict) else None
    return wpc if isinstance(wpc, dict) else {}


def resolve_claude_print_timeout(
    item: dict[str, Any] | None,
    *,
    config: dict | None = None,
    data_dir: str | Path | None = None,
) -> int:
    """Resolve subprocess timeout seconds for a Claude --print queue item."""
    timeouts = get_session_worker_timeouts(config)
    default = timeouts["default_secs"]
    if not isinstance(item, dict):
        return default

    prompt = str(item.get("prompt") or "")
    wpc = _wpc_from_item(item)
    session = _session_from_item(item, data_dir=data_dir)
    policy = (session or {}).get("workspace_policy") if isinstance(session, dict) else {}
    if not isinstance(policy, dict):
        policy = {}

    mode = wpc.get("policy_mode") or policy.get("mode")
    has_prompt_body = bool(str((session or {}).get("prompt_body") or "").strip())
    if not has_prompt_body:
        has_prompt_body = bool(wpc.get("has_prompt_body"))
    long_prompt = len(prompt) >= PROMPT_BODY_LEN_HINT

    if mode == "implementation":
        return timeouts["implementation_secs"]
    if mode == "docs-only":
        return timeouts["docs_only_secs"]
    if mode == "read-only" and (has_prompt_body or long_prompt or wpc.get("workspace_root")):
        return timeouts["docs_only_secs"]
    if has_prompt_body or long_prompt:
        return timeouts["prompt_memo_secs"]
    if mode == "read-only" and wpc.get("workspace_root"):
        return timeouts["docs_only_secs"]
    return default


def subprocess_timeout_for_print(timeout_secs: int, *, config: dict | None = None) -> int:
    """Total subprocess.run timeout including grace buffer."""
    grace = get_session_worker_timeouts(config)["subprocess_grace_secs"]
    return int(timeout_secs) + int(grace)


def _retry_safe(policy_mode: str | None, *, has_prompt_body: bool) -> bool:
    if policy_mode in ("docs-only", "read-only"):
        return True
    if policy_mode == "implementation" and has_prompt_body:
        return True
    if has_prompt_body:
        return True
    return False


def post_timeout_workspace_check(cwd: str | Path | None) -> dict[str, str]:
    """Read-only git status after timeout (no mutations)."""
    if not cwd:
        return {"status": "skipped", "reason": "no cwd"}
    path = Path(cwd)
    if not path.is_dir():
        return {"status": "skipped", "reason": f"cwd not found: {path}"}

    out: dict[str, str] = {}
    try:
        st = subprocess.run(
            ["git", "status", "--short"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        out["git_status_short"] = (st.stdout or "").strip() or "(clean)"
    except Exception as exc:  # noqa: BLE001
        out["git_status_short"] = f"could not check: {exc}"

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if head.returncode == 0:
            out["git_head"] = (head.stdout or "").strip()
        else:
            out["git_head"] = f"could not check (exit {head.returncode})"
    except Exception as exc:  # noqa: BLE001
        out["git_head"] = f"could not check: {exc}"

    return out


def build_timeout_diagnostics(
    *,
    agent: str,
    role: str | None,
    timeout_secs: int,
    cwd: str | Path | None,
    item: dict[str, Any] | None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect timeout diagnostic fields for logging and chat relay."""
    wpc = _wpc_from_item(item)
    if session is None and isinstance(item, dict):
        session = {}

    policy = (session or {}).get("workspace_policy") or {}
    if not isinstance(policy, dict):
        policy = {}

    prompt_body = str((session or {}).get("prompt_body") or "")
    has_prompt_body = bool(prompt_body.strip()) or bool(wpc.get("has_prompt_body"))
    mode = wpc.get("policy_mode") or policy.get("mode")
    profile = wpc.get("policy_id") or policy.get("policy_id")
    prompt_id = str((session or {}).get("prompt_id") or wpc.get("prompt_id") or "")

    return {
        "status": "WORKER_TIMEOUT",
        "agent": agent,
        "role": role or wpc.get("session_role") or "unknown",
        "command_type": "claude --print",
        "timeout_secs": timeout_secs,
        "cwd": str(cwd) if cwd else "",
        "workspace_profile": profile or "",
        "workspace_mode": mode or "",
        "prompt_id": prompt_id,
        "prompt_body_mode": has_prompt_body,
        "retry_safe": _retry_safe(str(mode) if mode else None, has_prompt_body=has_prompt_body),
    }


def format_worker_timeout_reply(diagnostics: dict[str, Any], *, workspace_check: dict[str, str] | None = None) -> str:
    """Format a WORKER_TIMEOUT relay message with diagnostics."""
    lines = [
        "WORKER_TIMEOUT",
        f"[claude --print timed out after {diagnostics.get('timeout_secs', '?')}s]",
        "",
        "Diagnostics:",
        f"- agent role: {diagnostics.get('role', '?')}",
        f"- command type: {diagnostics.get('command_type', 'claude --print')}",
        f"- timeout seconds: {diagnostics.get('timeout_secs', '?')}",
        f"- cwd: {diagnostics.get('cwd') or '(unknown)'}",
        f"- workspace_profile: {diagnostics.get('workspace_profile') or '(none)'}",
        f"- workspace_mode: {diagnostics.get('workspace_mode') or '(none)'}",
        f"- prompt_id: {diagnostics.get('prompt_id') or '(none)'}",
        f"- prompt_body mode: {'yes' if diagnostics.get('prompt_body_mode') else 'no'}",
        f"- retry safe: {'yes' if diagnostics.get('retry_safe') else 'no'}",
        "",
        "This is a worker infrastructure timeout, not an implementation verdict.",
    ]
    if workspace_check:
        lines.extend(["", "Post-timeout workspace check (read-only):"])
        if workspace_check.get("status") == "skipped":
            lines.append(f"- could not check: {workspace_check.get('reason', 'unknown')}")
        else:
            lines.append(f"- git status --short: {workspace_check.get('git_status_short', '?')}")
            lines.append(f"- git rev-parse HEAD: {workspace_check.get('git_head', '?')}")
    return "\n".join(lines)
