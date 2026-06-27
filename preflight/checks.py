"""Read-only preflight check implementations."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from preflight.redaction import scrub_preflight_output
from preflight_manifest import PhaseManifest

VERDICT_PASS = "PASS"
VERDICT_BLOCKED = "BLOCKED"
STATUS_PASS = "PASS"
STATUS_BLOCKED = "BLOCKED"

PROTECTED_CHANNELS = frozenset({"general", "relay-dryrun", "sdlc-dryrun"})
_ACTIVE_SESSION_DEFAULT_STATES = frozenset({"active", "waiting", "paused"})

_PROTECTED_COMMIT_PREFIXES = (
    "config.toml",
    "config.local.toml",
    "data/",
    "session_templates/",
    "docs/ai-roles/",
)


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def _porcelain_path_sets(snap: GitSnapshot) -> tuple[set[str], set[str]]:
    """Return (tracked_or_modified_paths, untracked_paths) from porcelain."""
    tracked: set[str] = set()
    untracked: set[str] = set()
    for ln in snap.porcelain.splitlines():
        if not ln or ln.startswith("!!"):
            continue
        path = _normalize_repo_path(ln[3:])
        if ln.startswith("??"):
            untracked.add(path)
        else:
            tracked.add(path)
    return tracked, untracked


def _path_matches_protected_prefix(path: str) -> bool:
    norm = _normalize_repo_path(path)
    return any(
        norm == prefix or norm.startswith(prefix)
        for prefix in _PROTECTED_COMMIT_PREFIXES
    )


@dataclass
class CheckResult:
    id: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": scrub_preflight_output(self.detail),
        }


@dataclass
class GitSnapshot:
    branch: str = ""
    head: str = ""
    remote_head: str = ""
    porcelain: str = ""
    staged_names: list[str] = field(default_factory=list)
    ahead_lines: list[str] = field(default_factory=list)
    behind_lines: list[str] = field(default_factory=list)
    config_local_ignored_line: str = ""
    error: str | None = None


@dataclass
class ProcessInfo:
    pid: int
    command_line: str


@dataclass
class PortListener:
    port: int
    pid: int
    state: str = "LISTENING"


@dataclass
class PreflightContext:
    repo_root: Path
    data_dir: Path | None = None
    git_snapshot: GitSnapshot | None = None
    processes: list[ProcessInfo] | None = None
    port_listeners: list[PortListener] | None = None
    config_toml_path: Path | None = None
    config_local_path: Path | None = None

    def resolve_data_dir(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir
        return self.repo_root / "data"


class GitRunner(Protocol):
    def capture(self, *args: str) -> tuple[int, str, str]:
        ...


def _pass(check_id: str, detail: str) -> CheckResult:
    return CheckResult(check_id, STATUS_PASS, detail)


def _block(check_id: str, detail: str) -> CheckResult:
    return CheckResult(check_id, STATUS_BLOCKED, detail)


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    if not path.is_file():
        return None, f"file not found: {path.name}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"failed to read {path.name}: {exc}"


def capture_git_snapshot(repo_root: Path, runner: GitRunner) -> GitSnapshot:
    snap = GitSnapshot()
    try:
        code, out, err = runner.capture("-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            snap.error = err or out or "rev-parse branch failed"
            return snap
        snap.branch = out.strip()

        code, out, _ = runner.capture("-C", str(repo_root), "rev-parse", "--short", "HEAD")
        snap.head = out.strip() if code == 0 else ""

        code, out, _ = runner.capture("-C", str(repo_root), "rev-parse", "--short", "origin/main")
        snap.remote_head = out.strip() if code == 0 else ""

        code, out, _ = runner.capture("-C", str(repo_root), "status", "--porcelain")
        snap.porcelain = out if code == 0 else ""

        code, out, _ = runner.capture("-C", str(repo_root), "diff", "--cached", "--name-only")
        snap.staged_names = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []

        code, out, _ = runner.capture(
            "-C", str(repo_root), "log", "--oneline", "HEAD..origin/main",
        )
        snap.behind_lines = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []

        code, out, _ = runner.capture(
            "-C", str(repo_root), "log", "--oneline", "origin/main..HEAD",
        )
        snap.ahead_lines = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []

        code, out, _ = runner.capture(
            "-C", str(repo_root), "status", "--ignored", "--short", "config.local.toml",
        )
        snap.config_local_ignored_line = out.strip() if code == 0 else ""
    except Exception as exc:  # noqa: BLE001 — snapshot must not crash preflight
        snap.error = str(exc)
    return snap


def _parse_wrapper_agents(processes: list[ProcessInfo]) -> tuple[set[str], bool]:
    """Return (running_wrapper_agents, server_seen)."""
    wrappers: set[str] = set()
    server = False
    wrapper_re = re.compile(r"wrapper\.py\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
    for proc in processes:
        cmd = proc.command_line
        if re.search(r"\brun\.py\b", cmd, re.IGNORECASE):
            server = True
        m = wrapper_re.search(cmd)
        if m:
            wrappers.add(m.group(1).lower())
    return wrappers, server


_WRAPPER_AGENT_RE = re.compile(r"wrapper\.py\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
# Path-less Windows venv server child: python(.exe) then run.py only — no extra args/flags.
_PATHLESS_VENV_SERVER_CHILD_RE = re.compile(
    r'^(?:'
    r'(?:"[^"]*python(?:\.exe)?"\s+run\.py\s*)'
    r'|(?:python(?:\.exe)?\s+run\.py\s*)'
    r')$',
    re.IGNORECASE,
)


def _is_pathless_venv_server_child(cmd: str) -> bool:
    """Match observed Windows venv re-exec server child; fail closed on extra tokens."""
    if "agentchattr" in cmd.lower():
        return False
    if re.search(r"wrapper\.py", cmd, re.IGNORECASE):
        return False
    return bool(_PATHLESS_VENV_SERVER_CHILD_RE.match(cmd.strip()))


def _is_pathless_authorized_runtime_process(cmd: str, manifest: PhaseManifest) -> bool:
    """Recognize Windows venv child run/wrapper shapes that omit the repo path."""
    if _is_pathless_venv_server_child(cmd):
        return True
    if "agentchattr" in cmd.lower():
        return False
    m = _WRAPPER_AGENT_RE.search(cmd)
    if m:
        agent = m.group(1).lower()
        allowed = {a.lower() for a in manifest.wrappers["allowed"]}
        return agent in allowed
    return False


def _is_stale_agentchattr_process(cmd: str, manifest: PhaseManifest) -> bool:
    """True when a path-less run/wrapper process looks like a stale agentchattr instance."""
    if not re.search(r"\b(run|wrapper)\.py\b", cmd, re.IGNORECASE):
        return False
    if "agentchattr" in cmd.lower():
        return False
    if str(manifest.phase_id) in cmd:
        return False
    if _is_pathless_authorized_runtime_process(cmd, manifest):
        return False
    return True


def _parse_config_sandbox_flow_enabled(
    config_path: Path,
    local_path: Path | None,
) -> tuple[bool | None, str]:
    """Read sandbox.flow_start_enabled from committed + local merge (keys only)."""
    if not config_path.is_file():
        return None, "config.toml not found"

    try:
        base = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"config.toml parse error: {exc}"

    enabled = bool(base.get("sandbox", {}).get("flow_start_enabled", False))
    if local_path and local_path.is_file():
        try:
            local = tomllib.loads(local_path.read_text(encoding="utf-8"))
            if "sandbox" in local and isinstance(local["sandbox"], dict):
                if "flow_start_enabled" in local["sandbox"]:
                    enabled = bool(local["sandbox"]["flow_start_enabled"])
        except (OSError, tomllib.TOMLDecodeError):
            return None, "config.local.toml parse error"
    return enabled, ""


def check_manifest_known(manifest: PhaseManifest | None, error: str | None) -> CheckResult:
    if manifest is None:
        return _block("manifest.known_phase", error or "unknown phase")
    return _pass("manifest.known_phase", f"loaded manifest for {manifest.phase_id}")


def check_git_state(manifest: PhaseManifest, snap: GitSnapshot) -> list[CheckResult]:
    results: list[CheckResult] = []
    git_cfg = manifest.git

    if snap.error:
        return [_block("git.snapshot", f"git snapshot failed: {snap.error}")]

    expected_branch = git_cfg["expected_branch"]
    if snap.branch != expected_branch:
        results.append(_block(
            "git.branch",
            f"expected branch {expected_branch!r}, got {snap.branch!r}",
        ))
    else:
        results.append(_pass("git.branch", f"on branch {snap.branch}"))

    if git_cfg["require_clean_tree"]:
        dirty_lines = [
            ln for ln in snap.porcelain.splitlines()
            if ln and not ln.startswith("!!")
        ]
        if dirty_lines:
            untracked = sum(1 for ln in dirty_lines if ln.startswith("??"))
            tracked = len(dirty_lines) - untracked
            parts = []
            if tracked:
                parts.append(f"{tracked} tracked change(s)")
            if untracked:
                parts.append(f"{untracked} untracked non-ignored file(s)")
            results.append(_block(
                "git.clean_tree",
                f"working tree not clean ({', '.join(parts)})",
            ))
        else:
            results.append(_pass("git.clean_tree", "working tree clean"))
    elif git_cfg.get("approved_dirty_paths") is not None:
        results.extend(_check_dirty_path_allowlist(git_cfg, snap))
    elif git_cfg.get("require_exact_dirty_set"):
        results.extend(_check_exact_dirty_set(git_cfg, snap))

    if git_cfg["require_no_staged"]:
        if snap.staged_names:
            results.append(_block(
                "git.staged",
                f"staged files present: {len(snap.staged_names)}",
            ))
        else:
            results.append(_pass("git.staged", "no staged files"))

    if git_cfg.get("require_ahead_of_remote"):
        expected_ahead = int(git_cfg.get("expected_ahead_count", 1))
        ahead_count = len(snap.ahead_lines)
        if git_cfg.get("require_head_not_behind_origin", True) and snap.behind_lines:
            results.append(_block(
                "git.sync",
                f"local behind {git_cfg['expected_remote_ref']} by {len(snap.behind_lines)} commit(s)",
            ))
        elif ahead_count != expected_ahead:
            results.append(_block(
                "git.ahead",
                f"expected {expected_ahead} commit(s) ahead of {git_cfg['expected_remote_ref']}, got {ahead_count}",
            ))
        elif snap.head and snap.remote_head and ahead_count == 0 and snap.head != snap.remote_head:
            results.append(_block(
                "git.head",
                f"HEAD {snap.head} != {git_cfg['expected_remote_ref']} {snap.remote_head}",
            ))
        else:
            results.append(_pass(
                "git.ahead",
                f"local ahead of {git_cfg['expected_remote_ref']} by {ahead_count} commit(s)",
            ))
    elif git_cfg["require_synced_with_remote"]:
        if snap.ahead_lines:
            results.append(_block(
                "git.sync",
                f"local ahead of {git_cfg['expected_remote_ref']} by {len(snap.ahead_lines)} commit(s)",
            ))
        elif snap.behind_lines:
            results.append(_block(
                "git.sync",
                f"local behind {git_cfg['expected_remote_ref']} by {len(snap.behind_lines)} commit(s)",
            ))
        elif snap.head and snap.remote_head and snap.head != snap.remote_head:
            results.append(_block(
                "git.head",
                f"HEAD {snap.head} != {git_cfg['expected_remote_ref']} {snap.remote_head}",
            ))
        else:
            results.append(_pass("git.sync", f"synced with {git_cfg['expected_remote_ref']}"))

    if git_cfg["require_config_local_ignored"]:
        line = snap.config_local_ignored_line
        if line.startswith("!!"):
            results.append(_pass("git.config_local_ignored", "config.local.toml ignored"))
        else:
            results.append(_block(
                "git.config_local_ignored",
                "config.local.toml is not ignored/uncommitted as expected",
            ))

    return results


def _check_dirty_path_allowlist(git_cfg: dict[str, Any], snap: GitSnapshot) -> list[CheckResult]:
    allowlist = {_normalize_repo_path(p) for p in git_cfg.get("approved_dirty_paths", [])}
    if not allowlist:
        return [_block("git.dirty_allowlist", "approved_dirty_paths is empty")]

    tracked, untracked = _porcelain_path_sets(snap)
    dirty = tracked | untracked
    unauthorized = sorted(dirty - allowlist)
    if unauthorized:
        return [_block(
            "git.dirty_allowlist",
            f"unauthorized dirty file(s): {', '.join(unauthorized[:5])}",
        )]
    if not dirty:
        return [_pass("git.dirty_allowlist", "no dirty files (allowlist not needed)")]
    return [_pass(
        "git.dirty_allowlist",
        f"dirty files within allowlist ({len(dirty)} file(s))",
    )]


def _check_exact_dirty_set(git_cfg: dict[str, Any], snap: GitSnapshot) -> list[CheckResult]:
    allowlist = {_normalize_repo_path(p) for p in git_cfg.get("approved_file_allowlist", [])}
    if not allowlist:
        return [_block("git.exact_dirty_set", "approved_file_allowlist is empty")]

    tracked, untracked = _porcelain_path_sets(snap)
    dirty = tracked | untracked
    for path in dirty:
        if _path_matches_protected_prefix(path) and path not in allowlist:
            return [_block(
                "git.protected_path",
                f"protected path modified without allowlist entry: {path}",
            )]

    if dirty != allowlist:
        extra = sorted(dirty - allowlist)
        missing = sorted(allowlist - dirty)
        detail_parts = []
        if extra:
            detail_parts.append(f"extra: {', '.join(extra[:5])}")
        if missing:
            detail_parts.append(f"missing: {', '.join(missing[:5])}")
        return [_block(
            "git.exact_dirty_set",
            f"dirty set mismatch ({'; '.join(detail_parts)})",
        )]
    return [_pass("git.exact_dirty_set", f"dirty set matches allowlist ({len(dirty)} file(s))")]


def check_process_and_port(
    manifest: PhaseManifest,
    processes: list[ProcessInfo],
    listeners: list[PortListener],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    net = manifest.network
    port = int(net["expected_port"])
    max_listeners = int(net["max_listeners"])

    port_listeners = [ln for ln in listeners if ln.port == port and ln.state.upper().startswith("LISTEN")]
    if len(port_listeners) > max_listeners:
        results.append(_block(
            "port.listeners",
            f"port {port} has {len(port_listeners)} listener(s), max allowed {max_listeners}",
        ))
    elif len(port_listeners) == 0:
        results.append(_pass("port.listeners", f"no listener on port {port}"))
    else:
        pids = ", ".join(str(ln.pid) for ln in port_listeners)
        results.append(_pass("port.listeners", f"port {port} listener PID(s): {pids}"))

    running_wrappers, server_seen = _parse_wrapper_agents(processes)
    wrapper_cfg = manifest.wrappers

    forbidden_running = sorted(running_wrappers & {f.lower() for f in wrapper_cfg["forbidden"]})
    if forbidden_running:
        results.append(_block(
            "wrappers.forbidden",
            f"forbidden wrapper(s) running: {', '.join(forbidden_running)}",
        ))
    else:
        results.append(_pass("wrappers.forbidden", "no forbidden wrappers detected"))

    allowed = {a.lower() for a in wrapper_cfg["allowed"]}
    missing_allowed = sorted(allowed - running_wrappers)
    if wrapper_cfg["require_all_allowed_running"]:
        if missing_allowed:
            results.append(_block(
                "wrappers.required",
                f"required wrapper(s) not running: {', '.join(missing_allowed)}",
            ))
        else:
            results.append(_pass(
                "wrappers.required",
                f"all allowed wrappers running: {', '.join(sorted(running_wrappers & allowed))}",
            ))

    unexpected = sorted(running_wrappers - allowed - {f.lower() for f in wrapper_cfg["forbidden"]})
    if unexpected:
        results.append(_block(
            "wrappers.unexpected",
            f"unexpected wrapper(s) running: {', '.join(unexpected)}",
        ))
    elif running_wrappers:
        results.append(_pass("wrappers.unexpected", "no unexpected wrappers"))

    if net["require_server_when_wrappers_required"] and wrapper_cfg["require_all_allowed_running"]:
        if running_wrappers and not server_seen:
            results.append(_block(
                "process.server",
                "wrappers running but no run.py server process detected",
            ))
        elif server_seen:
            results.append(_pass("process.server", "run.py server process detected"))
        else:
            results.append(_pass("process.server", "no server required (no wrappers running)"))

    stale_candidates = [
        p for p in processes
        if _is_stale_agentchattr_process(p.command_line, manifest)
    ]
    if stale_candidates:
        results.append(_block(
            "process.stale_candidates",
            f"{len(stale_candidates)} python process(es) may be stale agentchattr instances",
        ))
    else:
        results.append(_pass("process.stale_candidates", "no obvious stale agentchattr processes"))

    return results


def check_active_sessions(manifest: PhaseManifest, data_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    path = data_dir / "session_runs.json"
    data, err = _read_json(path)
    if err and "not found" in err:
        results.append(_pass("sessions.active_count", "no session_runs.json (count=0)"))
        return results
    if err:
        return [_block("sessions.read", err)]

    sessions = data if isinstance(data, list) else []
    active_states = set(manifest.sessions["active_states"])
    active = [
        s for s in sessions
        if isinstance(s, dict) and s.get("state") in active_states
    ]
    count = len(active)
    max_allowed = int(manifest.sessions["max_active_count"])

    if count > max_allowed:
        ids = [str(s.get("id", "?")) for s in active[:5]]
        results.append(_block(
            "sessions.active_count",
            f"active session count {count} exceeds max {max_allowed}; ids={','.join(ids)}",
        ))
    else:
        results.append(_pass(
            "sessions.active_count",
            f"active session count {count} within max {max_allowed}",
        ))

    if active:
        states = ", ".join(sorted({str(s.get("state")) for s in active}))
        results.append(_pass("sessions.states", f"observed states: {states}"))
    return results


def check_channel_hygiene(manifest: PhaseManifest, data_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    path = data_dir / "settings.json"
    data, err = _read_json(path)
    if err:
        for ch in manifest.channels["required"]:
            results.append(_block("channels.required", f"cannot verify channel {ch!r}: {err}"))
        return results

    channels = data.get("channels", []) if isinstance(data, dict) else []
    if not isinstance(channels, list):
        channels = []

    for ch in manifest.channels["required"]:
        if ch in channels:
            results.append(_pass("channels.required", f"required channel {ch!r} present"))
        else:
            results.append(_block("channels.required", f"required channel {ch!r} missing"))

    expected_protected = set(manifest.channels["protected_expectation"])
    if expected_protected != PROTECTED_CHANNELS:
        results.append(_block(
            "channels.protected_expectation",
            "protected channel expectation mismatch with runtime constant",
        ))
    else:
        results.append(_pass(
            "channels.protected_expectation",
            "protected channel expectation matches",
        ))

    if manifest.channels["forbid_general_session_leak_count"] and manifest.general_fallback_forbidden:
        leak_count = _count_general_session_leaks(data_dir)
        if leak_count > 0:
            results.append(_block(
                "channels.general_leak",
                f"detected {leak_count} log line(s) with session linkage on #general",
            ))
        else:
            results.append(_pass("channels.general_leak", "no #general session leak metadata in log tail"))
    return results


def _count_general_session_leaks(data_dir: Path, *, tail_lines: int = 500) -> int:
    log_path = data_dir / "agentchattr_log.jsonl"
    if not log_path.is_file():
        return 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines[-tail_lines:]:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        channel = str(msg.get("channel", "")).lstrip("#").lower()
        if channel != "general":
            continue
        if msg.get("session_id") is not None or msg.get("session_turn") is not None:
            count += 1
    return count


def check_sandbox_flow(
    manifest: PhaseManifest,
    ctx: PreflightContext,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    sb_cfg = manifest.sandbox
    config_path = ctx.config_toml_path or (ctx.repo_root / "config.toml")
    local_path = ctx.config_local_path or (ctx.repo_root / "config.local.toml")

    enabled, err = _parse_config_sandbox_flow_enabled(config_path, local_path)
    if enabled is None:
        return [_block("sandbox.config", err or "sandbox config unreadable")]

    if sb_cfg["forbid_flow_enabled"] and enabled:
        results.append(_block("sandbox.flow_enabled", "sandbox flow_start_enabled is true"))
    else:
        results.append(_pass("sandbox.flow_enabled", "sandbox flow_start_enabled is false"))

    audit_path = ctx.resolve_data_dir() / "sandbox_flow_audit.jsonl"
    audit_count = 0
    if audit_path.is_file():
        try:
            audit_count = sum(1 for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip())
        except OSError:
            results.append(_block("sandbox.audit", "failed to read sandbox_flow_audit.jsonl"))
            return results

    if sb_cfg["forbid_audit_activity"] and audit_count > 0:
        results.append(_block(
            "sandbox.audit",
            f"sandbox_flow_audit.jsonl has {audit_count} record(s)",
        ))
    else:
        results.append(_pass("sandbox.audit", f"sandbox audit count {audit_count}"))

    return results


def check_redaction(manifest: PhaseManifest) -> list[CheckResult]:
    if not manifest.redaction.get("require_self_test"):
        return [_pass("redaction.self_test", "self-test not required")]
    from preflight.redaction import redaction_self_test

    ok, detail = redaction_self_test()
    if ok:
        return [_pass("redaction.self_test", detail)]
    return [_block("redaction.self_test", detail)]


def check_general_fallback_policy(manifest: PhaseManifest) -> CheckResult:
    if manifest.general_fallback_forbidden:
        return _pass("policy.general_fallback", "#general fallback forbidden for this phase")
    return _block("policy.general_fallback", "manifest allows general fallback")


def run_all_checks(
    manifest: PhaseManifest,
    ctx: PreflightContext,
    git_snap: GitSnapshot,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks.append(check_general_fallback_policy(manifest))
    checks.extend(check_git_state(manifest, git_snap))

    if manifest.require_runtime_checks:
        processes = ctx.processes if ctx.processes is not None else []
        listeners = ctx.port_listeners if ctx.port_listeners is not None else []
        checks.extend(check_process_and_port(manifest, processes, listeners))
        checks.extend(check_active_sessions(manifest, ctx.resolve_data_dir()))
        checks.extend(check_channel_hygiene(manifest, ctx.resolve_data_dir()))
        checks.extend(check_sandbox_flow(manifest, ctx))
    else:
        checks.append(_pass("runtime.skipped", "runtime checks skipped per manifest"))
        if manifest.sandbox.get("forbid_flow_enabled"):
            checks.extend(check_sandbox_flow(manifest, ctx))

    checks.extend(check_redaction(manifest))
    return checks


def aggregate_verdict(checks: list[CheckResult]) -> str:
    if any(c.status == STATUS_BLOCKED for c in checks):
        return VERDICT_BLOCKED
    return VERDICT_PASS


def blocked_reasons(checks: list[CheckResult]) -> list[str]:
    return [
        scrub_preflight_output(f"{c.id}: {c.detail}")
        for c in checks
        if c.status == STATUS_BLOCKED
    ]
