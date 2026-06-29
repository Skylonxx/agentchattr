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
    external_cwd_enabled_for_mode,
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


def is_docs_only_snapshot_mode(
    item: dict[str, Any] | None,
    policy: dict[str, Any] | None,
) -> bool:
    """True when worker should receive automated precheck + file snapshots."""
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
        porcelain = (status.stdout or "").strip()
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

    lines.extend([
        "",
        "SNAPSHOT WORKER CONTRACT:",
        "- You have NO tools in this worker path (claude --print --tools \"\").",
        "- Do NOT output <tool_call> XML markup or request Bash/Read tools.",
        "- Analyze ONLY from AUTOMATED PRECHECK RESULTS and READ-ONLY FILE SNAPSHOT below.",
        "- If the snapshot is insufficient, return: BLOCKER: insufficient snapshot",
        "- For final report markdown, wrap content between REPORT_BEGIN and REPORT_END lines.",
    ])
    return PrecheckResult(
        ok=True, text="\n".join(lines), head=head_val, porcelain=porcelain,
    )


def run_workspace_precheck(cwd: str | Path, *, expected_head: str = "") -> str:
    """Legacy text-only precheck (non-blocking). Prefer run_workspace_precheck_structured."""
    result = run_workspace_precheck_structured(cwd, expected_head=expected_head)
    return result.text


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


def try_save_docs_only_report(
    text: str,
    policy: dict[str, Any],
    workspace_root: str | Path,
) -> list[str]:
    """Save REPORT_BEGIN/END block to allowed workspace report paths. Returns status lines."""
    block = extract_report_block(text)
    if not block:
        return []
    mode = policy.get("mode")
    if mode not in ("docs-only", "read-only"):
        return []
    write_files = list(policy.get("write_files") or [])
    report_candidates = [
        p for p in write_files
        if isinstance(p, str) and p.replace("\\", "/").endswith(".md")
    ]
    if not report_candidates:
        return []
    root = Path(workspace_root)
    saved: list[str] = []
    for rel in report_candidates:
        norm = _normalize_rel_path(rel)
        target = _resolve_allowlisted_file(root, norm)
        if target is None:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(block, encoding="utf-8")
            saved.append(f"- saved report to workspace: {norm}")
        except OSError as exc:
            saved.append(f"- could not save {norm}: {exc}")
    ext_paths = list(policy.get("report_paths") or [])
    for ext in ext_paths:
        if not isinstance(ext, str) or not ext.strip():
            continue
        try:
            p = Path(ext)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(block, encoding="utf-8")
            saved.append(f"- saved report to: {ext}")
        except OSError as exc:
            saved.append(f"- could not save external report {ext}: {exc}")
    return saved


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
