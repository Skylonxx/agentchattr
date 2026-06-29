"""Phase 3B runtime workspace policy helpers (default-off, fail-closed scaffolding).

Queue denormalized fields are audit/context only. Canonical persisted session
policy is the sole authority. Prompt/goal/chat text never expands policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import workspace_policy as wp

DEFAULT_SCRATCH_CWD = "C:/tools/agentchattr-scratch"

SESSION_ROLE_ALIASES = {
    "codex_coordinator": "coordinator",
    "workflow_coordinator": "coordinator",
    "codex_reviewer": "reviewer",
    "independent_reviewer": "reviewer",
    "safety_gate": "safety_gate",
    "safety_guard": "safety_gate",
    "codexsafe": "safety_gate",
}

GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "stash-list"})
GIT_DENY_SUBCOMMANDS = frozenset({
    "add", "commit", "push", "reset", "checkout", "clean", "stash",
    "restore", "switch", "rm", "mv", "worktree", "apply", "merge",
    "rebase", "cherry-pick", "revert", "fetch", "pull",
})

CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|;|\|\|)\s*")
PIPE_SPLIT_RE = re.compile(r"\s*\|\s*")
REDIRECT_RE = re.compile(r"(?<![0-9])>>?|2>")
GIT_DIR_RE = re.compile(r"\bGIT_DIR=|\bGIT_WORK_TREE=", re.IGNORECASE)
POWERSHELL_MUTATING = re.compile(
    r"\b(Set-Content|Out-File|Remove-Item|Move-Item|Copy-Item|New-Item)\b",
    re.IGNORECASE,
)
SHELL_ESCAPE_RE = re.compile(
    r"\b(Start-Process|cmd\s+/c|powershell\s+-Command)\b",
    re.IGNORECASE,
)
INTERPRETER_ESCAPE_RE = re.compile(
    r"\b(python|py|node|bash)\s+(-c|-e|-lc)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RuntimeGuardResult:
    ok: bool
    blocker: str | None = None
    reason: str = ""
    diagnostics: dict[str, Any] | None = None


REPORT_ONLY_ANALYSIS_PROFILE_IDS = frozenset({
    "twinpet-ui-09-c-payment-modal-analysis",
})


def normalize_session_role(role: str) -> str:
    key = (role or "").strip().lower()
    return SESSION_ROLE_ALIASES.get(key, key)


def is_runtime_enforcement_enabled(cfg: dict | None) -> bool:
    section = (cfg or {}).get("workspace_policy")
    if not isinstance(section, dict):
        return False
    return bool(section.get("runtime_enforcement_enabled"))


def is_read_only_external_cwd_enabled(cfg: dict | None) -> bool:
    section = (cfg or {}).get("workspace_policy")
    if not isinstance(section, dict):
        return False
    return bool(section.get("read_only_external_cwd_enabled"))


def is_scoped_write_external_cwd_enabled(cfg: dict | None) -> bool:
    section = (cfg or {}).get("workspace_policy")
    if not isinstance(section, dict):
        return False
    return bool(section.get("scoped_write_external_cwd_enabled"))


def external_cwd_enabled_for_mode(cfg: dict | None, mode: str | None) -> bool:
    """Return True when external cwd routing is enabled for the policy mode."""
    if mode == "read-only":
        return is_read_only_external_cwd_enabled(cfg)
    if mode == "implementation":
        return is_scoped_write_external_cwd_enabled(cfg)
    if mode == "docs-only":
        return (
            is_scoped_write_external_cwd_enabled(cfg)
            or is_read_only_external_cwd_enabled(cfg)
        )
    return False


def normalize_workspace_root(path: str) -> str:
    """Normalize an absolute workspace root for comparison and subprocess cwd."""
    if not path or not isinstance(path, str):
        return ""
    text = path.strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve())
    except (OSError, ValueError):
        return text.replace("\\", "/")


def is_session_triggered_work(
    *,
    relay_meta: dict[str, Any] | None = None,
    workspace_policy_context: dict[str, Any] | None = None,
) -> bool:
    """Return True when queue item is session-triggered (trusted metadata only)."""
    if isinstance(workspace_policy_context, dict):
        if workspace_policy_context.get("relay_kind") == "session_turn":
            return True
        if workspace_policy_context.get("session_id") is not None:
            return True
    if isinstance(relay_meta, dict):
        if relay_meta.get("relay_mode") and relay_meta.get("disable_mcp"):
            return True
        if relay_meta.get("kind") == "session_turn":
            return True
        if relay_meta.get("session_id") is not None:
            return True
    return False


def build_session_queue_workspace_context(
    session: dict[str, Any],
    session_role: str,
    phase_index: int,
    turn_index: int,
    *,
    relay_kind: str = "session_turn",
) -> dict[str, Any]:
    """Build audit/context metadata for a session-triggered queue item."""
    policy = session.get("workspace_policy")
    if not isinstance(policy, dict):
        policy = wp.default_scratch_readonly_policy()
    workspace = policy.get("workspace") or {}
    return {
        "session_id": session.get("id"),
        "session_role": session_role,
        "phase_index": phase_index,
        "turn_index": turn_index,
        "policy_hash": session.get("workspace_policy_hash"),
        "workspace_policy_version": session.get("workspace_policy_version"),
        "policy_mode": policy.get("mode"),
        "policy_id": policy.get("policy_id"),
        "workspace_root": workspace.get("root"),
        "prompt_id": session.get("prompt_id"),
        "has_prompt_body": bool(str(session.get("prompt_body") or "").strip()),
        "relay_kind": relay_kind,
    }


def load_persisted_session_record(data_dir: str | Path, session_id: int) -> dict[str, Any] | None:
    path = Path(data_dir) / "session_runs.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, list):
        return None
    for record in raw:
        if isinstance(record, dict) and record.get("id") == session_id:
            return record
    return None


def verify_queue_workspace_policy(
    *,
    queue_context: dict[str, Any] | None,
    data_dir: str | Path | None = None,
    load_session_fn: Callable[[int], dict[str, Any] | None] | None = None,
    enforcement_enabled: bool = True,
) -> RuntimeGuardResult:
    """Verify queue metadata against canonical persisted session policy."""
    if not enforcement_enabled:
        return RuntimeGuardResult(True)
    if not queue_context:
        return RuntimeGuardResult(True)

    relay_kind = queue_context.get("relay_kind")
    session_id = queue_context.get("session_id")

    if relay_kind == "session_turn" and session_id is None:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_context_missing",
            reason="session_turn queue item missing session_id",
        )

    if relay_kind and relay_kind != "session_turn":
        return RuntimeGuardResult(True)

    if session_id is None:
        return RuntimeGuardResult(True)

    policy_hash = queue_context.get("policy_hash")
    if not policy_hash or not isinstance(policy_hash, str):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_hash_missing",
            reason="session queue item missing policy_hash",
        )

    loader = load_session_fn
    if loader is None:
        if data_dir is None:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:policy_snapshot_missing",
                reason="no data_dir or loader for policy verification",
            )

        def loader(sid: int) -> dict[str, Any] | None:
            return load_persisted_session_record(data_dir, sid)

    record = loader(int(session_id))
    if not record:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_snapshot_missing",
            reason=f"session {session_id} not found",
        )

    snapshot = record.get("workspace_policy")
    if not isinstance(snapshot, dict):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_snapshot_missing",
            reason=f"session {session_id} missing workspace_policy snapshot",
        )

    canonical_hash = record.get("workspace_policy_hash")
    if not canonical_hash:
        canonical_hash = wp.compute_workspace_policy_hash(snapshot)

    recomputed = wp.compute_workspace_policy_hash(snapshot)
    if recomputed != canonical_hash:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_snapshot_missing",
            reason=f"session {session_id} persisted policy hash is corrupt",
        )

    if policy_hash != recomputed:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_hash_mismatch",
            reason="queue policy_hash does not match canonical persisted snapshot",
        )

    return RuntimeGuardResult(True)


def verify_session_workspace_policy(
    *,
    relay_meta: dict[str, Any] | None = None,
    workspace_policy_context: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
    load_session_fn: Callable[[int], dict[str, Any] | None] | None = None,
    enforcement_enabled: bool = True,
) -> RuntimeGuardResult:
    """Verify session-triggered work; non-session mentions are allowed through."""
    if not enforcement_enabled:
        return RuntimeGuardResult(True)
    if not is_session_triggered_work(
        relay_meta=relay_meta,
        workspace_policy_context=workspace_policy_context,
    ):
        return RuntimeGuardResult(True)
    if not workspace_policy_context:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:policy_context_missing",
            reason="session-triggered work missing workspace_policy_context",
        )
    return verify_queue_workspace_policy(
        queue_context=workspace_policy_context,
        data_dir=data_dir,
        load_session_fn=load_session_fn,
        enforcement_enabled=True,
    )


def canonical_policy_from_queue_context(
    *,
    queue_context: dict[str, Any] | None,
    data_dir: str | Path | None = None,
    load_session_fn: Callable[[int], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """Load canonical policy for a verified session queue context."""
    if not queue_context or queue_context.get("session_id") is None:
        return None
    verify = verify_queue_workspace_policy(
        queue_context=queue_context,
        data_dir=data_dir,
        load_session_fn=load_session_fn,
        enforcement_enabled=True,
    )
    if not verify.ok:
        return None
    sid = int(queue_context["session_id"])
    loader = load_session_fn
    if loader is None:
        if data_dir is None:
            return None
        record = load_persisted_session_record(data_dir, sid)
    else:
        record = loader(sid)
    if not record:
        return None
    policy = record.get("workspace_policy")
    return dict(policy) if isinstance(policy, dict) else None


def resolve_role_cwd(
    policy: dict[str, Any] | None,
    session_role: str,
    *,
    enforcement_enabled: bool = False,
    default_scratch: str = DEFAULT_SCRATCH_CWD,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Resolve cwd for a session role from canonical policy (fail-closed)."""
    if not enforcement_enabled:
        return default_scratch

    canonical = policy if isinstance(policy, dict) else wp.default_scratch_readonly_policy()
    mode = canonical.get("mode")

    if mode == "scratch-readonly":
        return default_scratch

    if mode not in ("read-only", "implementation", "docs-only"):
        return default_scratch

    workspace = canonical.get("workspace") or {}
    root = workspace.get("root")
    if not root or not isinstance(root, str):
        return default_scratch

    role = normalize_session_role(session_role)
    perms = wp.role_permission_for(canonical, role)
    if not perms:
        return default_scratch

    fs = perms.get("filesystem", "none")
    if mode == "read-only":
        if fs not in ("read", "none"):
            return default_scratch
    elif mode == "docs-only":
        if fs == "none":
            return default_scratch
        if fs not in ("read", "write_allowlist"):
            return default_scratch
    elif mode == "implementation":
        if fs == "none":
            return default_scratch
        if fs not in ("read", "write_allowlist"):
            return default_scratch

    normalized_root = normalize_workspace_root(root)
    if not normalized_root:
        return default_scratch

    profile_id = canonical.get("policy_id")
    if profiles and profile_id:
        profile = profiles.get(profile_id)
        if not profile:
            return default_scratch
        expected = profile.get("workspace_root")
        if expected and normalize_workspace_root(str(expected)) != normalized_root:
            return default_scratch

    if not Path(normalized_root).is_dir():
        return default_scratch

    return normalized_root


def resolve_exec_cwd_for_item(
    item: dict[str, Any] | None,
    *,
    data_dir: str | Path | None,
    config: dict | None,
    default_cwd: str,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Resolve subprocess cwd for a queue item (verified external profile roots)."""
    if not isinstance(item, dict):
        return default_cwd

    wpc = item.get("workspace_policy_context")
    if not isinstance(wpc, dict):
        return default_cwd

    verify = verify_session_workspace_policy(
        relay_meta=item.get("relay_meta"),
        workspace_policy_context=wpc,
        data_dir=data_dir,
        enforcement_enabled=True,
    )
    if not verify.ok:
        return default_cwd

    policy = canonical_policy_from_queue_context(
        queue_context=wpc,
        data_dir=data_dir,
    )
    if not policy:
        return default_cwd

    mode = policy.get("mode")
    if not external_cwd_enabled_for_mode(config, mode):
        return default_cwd

    role = wpc.get("session_role") or "developer"
    return resolve_role_cwd(
        policy,
        str(role),
        enforcement_enabled=True,
        default_scratch=default_cwd,
        profiles=profiles,
    )


def _split_command_segments(command_line: str) -> list[str]:
    segments: list[str] = []
    for chain_part in CHAIN_SPLIT_RE.split(command_line.strip()):
        if not chain_part.strip():
            continue
        for pipe_part in PIPE_SPLIT_RE.split(chain_part):
            part = pipe_part.strip()
            if part:
                segments.append(part)
    return segments


def _parse_git_invocation(segment: str) -> tuple[bool, str | None, bool]:
    tokens = segment.strip().split()
    if not tokens or tokens[0].lower() != "git":
        return False, None, False
    idx = 1
    used_c = False
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            idx += 1
            break
        if tok.startswith("-") and tok not in ("-C",):
            idx += 1
            continue
        if tok == "-C":
            used_c = True
            idx += 2
            continue
        if tok.startswith("-c"):
            idx += 1
            continue
        return True, tok.lower(), used_c
    return True, None, used_c


def check_command_guard(command_line: str, *, policy: dict[str, Any] | None = None) -> RuntimeGuardResult:
    """Fail-closed command guard (allowlist-oriented). Policy reserved for future mode gates."""
    _ = policy
    if not command_line or not str(command_line).strip():
        return RuntimeGuardResult(True)

    text = str(command_line).strip()
    if REDIRECT_RE.search(text):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="shell redirection is not allowed",
        )
    if GIT_DIR_RE.search(text):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="GIT_DIR/GIT_WORK_TREE escape is not allowed",
        )
    if POWERSHELL_MUTATING.search(text):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="PowerShell mutating cmdlet is not allowed",
        )
    if SHELL_ESCAPE_RE.search(text):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="shell/process escape is not allowed",
        )
    if INTERPRETER_ESCAPE_RE.search(text):
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="interpreter escape is not allowed",
        )

    segments = _split_command_segments(text)
    if not segments:
        return RuntimeGuardResult(True)

    saw_git = False
    for segment in segments:
        is_git, subcommand, used_c = _parse_git_invocation(segment)
        if not is_git:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:command_guard_denied",
                reason=f"non-git command not allowlisted: {segment[:80]!r}",
            )
        saw_git = True
        if used_c:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:command_guard_denied",
                reason="git -C working tree escape is not allowed",
            )
        if subcommand is None:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:command_guard_denied",
                reason="git invocation missing subcommand",
            )
        if subcommand in GIT_DENY_SUBCOMMANDS:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:command_guard_denied",
                reason=f"git {subcommand} is not allowed",
            )
        if subcommand not in GIT_READ_SUBCOMMANDS:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:command_guard_denied",
                reason=f"git {subcommand} is not in read-only allowlist",
            )

    if not saw_git:
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:command_guard_denied",
            reason="command is not an allowlisted git read invocation",
        )
    return RuntimeGuardResult(True)


def parse_git_porcelain(porcelain_output: str) -> list[dict[str, str]]:
    """Parse `git status --porcelain` into normalized entries."""
    entries: list[dict[str, str]] = []
    for raw_line in (porcelain_output or "").splitlines():
        line = raw_line.rstrip("\r")
        if len(line) < 3 or line.startswith("#"):
            continue
        if len(line) >= 3 and line[2] == " ":
            xy = line[:2]
            path_part = line[3:].strip()
        elif len(line) >= 2 and line[1] == " ":
            # Recover when leading column space was trimmed (e.g. strip() turned " M Context.md" into "M Context.md").
            xy = " " + line[0]
            path_part = line[2:].strip()
        else:
            continue
        if " -> " in path_part:
            path = path_part.split(" -> ", 1)[1].strip()
        else:
            path = path_part
        path = path.replace("\\", "/")
        entries.append({"status": xy, "path": path})
    return entries


def _dirty_path_allowed(path: str, policy: dict[str, Any]) -> bool:
    write_files = set(policy.get("write_files") or [])
    forbidden = policy.get("forbidden_paths") or []
    norm = path.replace("\\", "/")
    for pattern in forbidden:
        if isinstance(pattern, str) and wp.path_matches_forbidden(norm, pattern):
            return False
    return norm in write_files


def enrich_policy_for_dirty_verification(
    policy: dict[str, Any] | None,
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Merge profile registry hints so stale session snapshots use report-only dirty rules."""
    if not isinstance(policy, dict):
        return None
    out = dict(policy)
    profile_id = out.get("policy_id")
    if profile_id in REPORT_ONLY_ANALYSIS_PROFILE_IDS:
        out["analysis_report_only"] = True
    elif profiles and profile_id and profile_id in profiles:
        prof = profiles[profile_id]
        if prof.get("analysis_report_only"):
            out["analysis_report_only"] = True
    return out


def git_head_at_cwd(cwd: str) -> str:
    """Best-effort ``git rev-parse HEAD`` for dirty-tree diagnostics."""
    import subprocess

    if not cwd:
        return ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def verify_dirty_set(
    *,
    porcelain_output: str,
    policy: dict[str, Any],
    profiles: dict[str, dict[str, Any]] | None = None,
) -> RuntimeGuardResult:
    """Compare porcelain dirty set against policy write_files allowlist."""
    effective = enrich_policy_for_dirty_verification(policy, profiles=profiles) or policy
    if is_report_only_readonly_policy(effective):
        return verify_dirty_set_report_only_analysis(
            porcelain_output=porcelain_output,
            policy=effective,
        )
    mode = effective.get("mode")
    write_files = effective.get("write_files") or []
    if mode in ("scratch-readonly", "read-only") or not write_files:
        entries = parse_git_porcelain(porcelain_output)
        if entries:
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:unauthorized_dirty_tree",
                reason="dirty tree not allowed in read-only/scratch mode",
            )
        return RuntimeGuardResult(True)

    for entry in parse_git_porcelain(porcelain_output):
        path = entry["path"]
        if not _dirty_path_allowed(path, effective):
            return RuntimeGuardResult(
                False,
                blocker="BLOCKER:unauthorized_dirty_tree",
                reason=f"path not in write_files allowlist: {path!r}",
            )
    return RuntimeGuardResult(True)


def _dirty_set_diagnostics(
    *,
    policy: dict[str, Any],
    porcelain_output: str,
    git_commit: str,
    guard_source: str,
    policy_source: str,
) -> dict[str, Any]:
    docs_dirty: list[str] = []
    blocking_dirty: list[str] = []
    if is_report_only_readonly_policy(policy):
        docs_dirty, blocking_dirty = classify_dirty_entries_report_only_analysis(
            porcelain_output,
            policy=policy,
        )
    dirty_paths = [
        _normalize_dirty_path(entry["path"])
        for entry in parse_git_porcelain(porcelain_output)
    ]
    if not is_report_only_readonly_policy(policy) and dirty_paths:
        blocking_dirty = list(dirty_paths)
    mode = policy.get("mode") or ""
    return {
        "workspace_profile": policy.get("policy_id") or "",
        "workspace_mode": mode,
        "canonical_mode": wp.normalize_workspace_mode(mode) or mode,
        "analysis_report_only": is_report_only_readonly_policy(policy),
        "dirty_paths": dirty_paths,
        "allowed_docs_dirty": docs_dirty,
        "blocking_dirty": blocking_dirty,
        "git_commit": git_commit,
        "policy_source": policy_source,
        "guard_source": guard_source,
    }


def format_unauthorized_dirty_tree_blocker(
    result: RuntimeGuardResult,
    diagnostics: dict[str, Any],
) -> str:
    """Format relay-safe unauthorized_dirty_tree blocker with policy diagnostics."""
    def _csv(paths: list[str] | tuple[str, ...] | None) -> str:
        items = [p for p in (paths or []) if p]
        return ",".join(items) if items else "(none)"

    lines = [
        "BLOCKER:unauthorized_dirty_tree",
        f"workspace_profile={diagnostics.get('workspace_profile', '')}",
        f"workspace_mode={diagnostics.get('workspace_mode', '')}",
        f"canonical_mode={diagnostics.get('canonical_mode', '')}",
        f"analysis_report_only={str(bool(diagnostics.get('analysis_report_only'))).lower()}",
        f"dirty_paths={_csv(diagnostics.get('dirty_paths'))}",
        f"allowed_docs_dirty={_csv(diagnostics.get('allowed_docs_dirty'))}",
        f"blocking_dirty={_csv(diagnostics.get('blocking_dirty'))}",
        f"git_commit={diagnostics.get('git_commit') or '(unknown)'}",
        f"policy_source={diagnostics.get('policy_source', '')}",
        f"guard_source={diagnostics.get('guard_source', '')}",
    ]
    if result.reason:
        lines.append(f"reason={result.reason}")
    return "\n".join(lines)


def verify_dirty_set_with_diagnostics(
    *,
    porcelain_output: str,
    policy: dict[str, Any],
    profiles: dict[str, dict[str, Any]] | None = None,
    git_commit: str = "",
    guard_source: str = "verify_dirty_set",
    policy_source: str = "canonical_session_policy",
) -> RuntimeGuardResult:
    """Verify dirty set and attach formatted blocker diagnostics when blocked."""
    effective = enrich_policy_for_dirty_verification(policy, profiles=profiles) or policy
    diagnostics = _dirty_set_diagnostics(
        policy=effective,
        porcelain_output=porcelain_output,
        git_commit=git_commit,
        guard_source=guard_source,
        policy_source=policy_source,
    )
    result = verify_dirty_set(
        porcelain_output=porcelain_output,
        policy=effective,
        profiles=profiles,
    )
    if result.ok:
        return RuntimeGuardResult(True, diagnostics=diagnostics)
    blocker = format_unauthorized_dirty_tree_blocker(result, diagnostics)
    return RuntimeGuardResult(
        False,
        blocker=blocker,
        reason=result.reason,
        diagnostics=diagnostics,
    )


DOCS_TRACKER_PATHS = frozenset({
    "task.md",
    "context.md",
    "docs/reports/latest-report.md",
})

PRODUCT_DIR_PREFIXES = (
    "src/",
    "tests/",
    "functions/",
    "android/",
    "ios/",
    ".claude/",
)


def is_report_only_readonly_policy(policy: dict[str, Any] | None) -> bool:
    """True for strict read-only analysis profiles with no repo writes."""
    if not isinstance(policy, dict):
        return False
    if policy.get("analysis_report_only"):
        return True
    return policy.get("policy_id") in REPORT_ONLY_ANALYSIS_PROFILE_IDS


def _normalize_dirty_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _is_docs_tracker_dirty_path(path: str) -> bool:
    norm = _normalize_dirty_path(path).lower()
    return norm in DOCS_TRACKER_PATHS


def _is_product_area_dirty_path(path: str, policy: dict[str, Any]) -> bool:
    norm = _normalize_dirty_path(path)
    low = norm.lower()
    if any(low.startswith(prefix) for prefix in PRODUCT_DIR_PREFIXES):
        return True
    forbidden = policy.get("forbidden_paths") or []
    for pattern in forbidden:
        if isinstance(pattern, str) and wp.path_matches_forbidden(norm, pattern):
            if pattern.replace("\\", "/").lower() in (
                "task.md", "context.md", "docs/reports/latest-report.md",
            ):
                continue
            return True
    return False


def classify_dirty_entries_report_only_analysis(
    porcelain_output: str,
    *,
    policy: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return (docs_tracker_dirty, blocking_dirty) path lists."""
    docs_dirty: list[str] = []
    blocking: list[str] = []
    for entry in parse_git_porcelain(porcelain_output):
        path = entry["path"]
        if _is_docs_tracker_dirty_path(path):
            docs_dirty.append(_normalize_dirty_path(path))
        elif _is_product_area_dirty_path(path, policy):
            blocking.append(_normalize_dirty_path(path))
        else:
            blocking.append(_normalize_dirty_path(path))
    return docs_dirty, blocking


def verify_dirty_set_report_only_analysis(
    *,
    porcelain_output: str,
    policy: dict[str, Any],
) -> RuntimeGuardResult:
    """Allow pre-existing docs tracker dirt; block product/code/test dirt."""
    docs_dirty, blocking = classify_dirty_entries_report_only_analysis(
        porcelain_output, policy=policy,
    )
    if blocking:
        paths = ", ".join(blocking[:5])
        suffix = f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else ""
        return RuntimeGuardResult(
            False,
            blocker="BLOCKER:unauthorized_dirty_tree",
            reason=f"dirty product/code paths before analysis: {paths}{suffix}",
        )
    return RuntimeGuardResult(True)


def policy_from_prompt_text(_prompt: str) -> dict[str, Any] | None:
    """Prompt/goal text must never authorize workspace policy (always None)."""
    return None
