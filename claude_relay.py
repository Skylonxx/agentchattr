"""claude_relay.py — DORMANT Claude relay runner helpers (NOT wired into runtime).

Pure, side-effect-free helpers for a *future* Claude relay runner. Nothing here
is invoked by the live relay path: Claude is NOT in
``session_relay.RELAY_ELIGIBLE_AGENTS`` and this module is not imported by
``wrapper.py`` or ``session_engine.py``. These helpers exist so the
command-construction, JSON-capture, environment-stripping, and scratch-validation
contracts can be unit-tested in isolation BEFORE any activation gate.

Design basis: agentchattr-claude-relay-design-gate-1.md (Codex review: PASS WITH NOTES).

Safety invariants encoded here (see also session_relay.py / session_engine.py):
  * CodexSafe remains the sole terminal safety gate. Nothing in this module
    parses a PASS/BLOCK verdict; Claude output is reply text only and can never
    override a CodexSafe BLOCK.
  * Claude stays NOT relay-eligible. This module does not touch
    RELAY_ELIGIBLE_AGENTS and must not be wired into the runtime in this phase.
  * Fail-closed everywhere: every outcome maps to exactly one non-empty marker
    or reply, so a relay turn can never silently post nothing.

No real Claude process is spawned by this module. Subprocess wiring belongs to a
later, explicitly-authorized activation gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Bounded exec runtime for a future Claude relay turn. Mirrors wrapper.py's
# EXEC_TIMEOUT_SECS=120 so the subprocess timeout and the "[timed out after Ns]"
# marker never drift once wired up.
CLAUDE_EXEC_TIMEOUT_SECS = 120

# Max chars of reply text relayed to chat (parallels wrapper._format_relay_reply).
_REPLY_TRUNCATE_AT = 2000

# Max chars of raw-envelope evidence kept for telemetry (parallels the
# raw_output[:500] bound used by session_engine._check_safety_block).
_EVIDENCE_BOUND = 500


# ---------------------------------------------------------------------------
# Relay outcome markers — reuse the EXACT vocabulary from wrapper.py so Claude
# outcomes are indistinguishable in shape from codex/codexsafe outcomes and the
# session engine treats them uniformly.
# ---------------------------------------------------------------------------

MARKER_NO_REPLY = "[no reply]"
MARKER_INVALID_JSON = "[failed (invalid json)]"
MARKER_CLAUDE_ERROR = "[failed (claude error)]"
MARKER_PERMISSION_DENIED = "[failed (permission denied)]"
MARKER_EXEC_ERROR = "[failed (exec error)]"


def marker_exit(returncode: int) -> str:
    return f"[failed (exit {returncode})]"


def marker_timeout(timeout_secs: int) -> str:
    return f"[timed out after {timeout_secs}s]"


# ---------------------------------------------------------------------------
# 1. Claude command construction
# ---------------------------------------------------------------------------

# Flags that must NEVER appear in the relay command (paid-API / unsafe / MCP).
# Retained as a documented denylist and as an explicit assertion target for tests;
# the sealed builder below has no extension point, so none of these can be added
# by a caller in the first place.
FORBIDDEN_CLAUDE_FLAGS = frozenset({
    "--bare",
    "--mcp-config",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
})

# The one and only relay command. Sealed and immutable — there is deliberately
# NO caller-supplied-argument path in this phase. Codex review (BLOCKED finding,
# claude_relay.py:97-105) flagged that an ``extra_args`` extension point let a
# caller append a second ``--tools`` (or other tool/MCP/auth/permission flag)
# AFTER the required ``--tools ""``, undermining the no-tools invariant. The fix
# removes the extension point entirely: any future flag must be added here only
# after it is explicitly authorized and tested. The sealed prompt is delivered
# via stdin (NOT argv), so no prompt is ever appended.
SEALED_CLAUDE_COMMAND = (
    "claude",
    "-p",
    "--output-format", "json",
    "--input-format", "text",
    "--tools", "",
    "--strict-mcp-config",
)


def build_claude_command() -> list[str]:
    """Return the sealed Claude relay command (a fresh argv list copy).

        claude -p --output-format json --input-format text --tools "" --strict-mcp-config

    Takes NO arguments: there is no extension point through which a caller could
    append a tool-enabling, MCP, auth, permission-bypass, cwd/project, or
    output/input override. The sealed prompt is delivered via stdin by the future
    runner, never as argv. Returns a new list each call so a caller mutating the
    result cannot affect the canonical ``SEALED_CLAUDE_COMMAND``.
    """
    return list(SEALED_CLAUDE_COMMAND)


# ---------------------------------------------------------------------------
# 2. JSON capture / marker mapping
# ---------------------------------------------------------------------------

@dataclass
class ClaudeReplyOutcome:
    """Resolved outcome of one Claude relay turn.

    ``text`` is ALWAYS non-empty: either the model reply (success) or a failure
    marker. ``ok`` is True only on a clean success with a non-empty result.
    ``evidence`` is a bounded, scrubbed copy of raw stdout kept for telemetry —
    it must never be forwarded to chat as the user-facing reply.
    """
    text: str
    ok: bool = False
    failure_kind: str | None = None
    stderr_warning: str | None = None
    evidence: str = ""


# Redact obvious Anthropic key material if it ever appears in captured output.
_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def scrub_evidence(raw: str | None, *, bound: int = _EVIDENCE_BOUND) -> str:
    """Return a bounded, secret-scrubbed copy of raw output for telemetry."""
    if not raw:
        return ""
    scrubbed = _SECRET_RE.sub("[REDACTED]", raw)
    if len(scrubbed) > bound:
        return scrubbed[:bound] + f"... [truncated, {len(scrubbed)} chars total]"
    return scrubbed


def _format_claude_reply(text: str) -> str:
    """Provider-neutral reply formatter (parallels wrapper._format_relay_reply).

    Uses Claude-accurate marker text — never the misleading ``[codex error: ...]``
    string that wrapper._format_relay_reply emits.
    """
    text = text.strip()
    if text.startswith("Traceback"):
        return f"[claude error: {text[:100]}]"
    if len(text) > _REPLY_TRUNCATE_AT:
        return text[:_REPLY_TRUNCATE_AT] + f"... [truncated, {len(text)} chars total]"
    return text


def resolve_claude_reply(
    *,
    timed_out: bool = False,
    errored: bool = False,
    returncode: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    timeout_secs: int = CLAUDE_EXEC_TIMEOUT_SECS,
) -> ClaudeReplyOutcome:
    """Map a captured Claude process outcome to a relay reply or failure marker.

    Precedence (fail-closed, matches the design memo §6):
        timeout -> exec error -> nonzero exit -> invalid JSON -> is_error true
        -> bad subtype -> permission_denials non-empty -> empty result -> success

    Only ``.result`` is ever forwarded as reply content; the raw envelope is kept
    (bounded + scrubbed) as ``evidence`` only.
    """
    evidence = scrub_evidence(stdout)

    if timed_out:
        return ClaudeReplyOutcome(text=marker_timeout(timeout_secs),
                                  failure_kind="timeout", evidence=evidence)
    if errored:
        return ClaudeReplyOutcome(text=MARKER_EXEC_ERROR,
                                  failure_kind="exec_error", evidence=evidence)
    if returncode is not None and returncode != 0:
        return ClaudeReplyOutcome(text=marker_exit(returncode),
                                  failure_kind="nonzero_exit", evidence=evidence)

    # Parse stdout as a single JSON object.
    raw = (stdout or "").strip()
    try:
        envelope = json.loads(raw)
    except (ValueError, TypeError):
        return ClaudeReplyOutcome(text=MARKER_INVALID_JSON,
                                  failure_kind="invalid_json", evidence=evidence)
    if not isinstance(envelope, dict):
        return ClaudeReplyOutcome(text=MARKER_INVALID_JSON,
                                  failure_kind="invalid_json", evidence=evidence)

    if envelope.get("is_error") is True:
        return ClaudeReplyOutcome(text=MARKER_CLAUDE_ERROR,
                                  failure_kind="claude_error", evidence=evidence)
    if envelope.get("subtype") != "success":
        return ClaudeReplyOutcome(text=MARKER_CLAUDE_ERROR,
                                  failure_kind="bad_subtype", evidence=evidence)

    denials = envelope.get("permission_denials")
    if denials:  # non-empty list/obj => no-tools contract violated
        return ClaudeReplyOutcome(text=MARKER_PERMISSION_DENIED,
                                  failure_kind="permission_denied", evidence=evidence)

    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        return ClaudeReplyOutcome(text=MARKER_NO_REPLY,
                                  failure_kind="empty_result", evidence=evidence)

    # Success. Non-empty stderr on an otherwise-clean turn is anomalous (probes
    # showed empty stderr on success): preserve the reply, record a warning.
    warning = None
    if stderr and stderr.strip():
        warning = scrub_evidence(stderr, bound=_EVIDENCE_BOUND)

    return ClaudeReplyOutcome(
        text=_format_claude_reply(result),
        ok=True,
        failure_kind=None,
        stderr_warning=warning,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 3. Child environment stripping
# ---------------------------------------------------------------------------

# Env keys stripped from the Claude child process. Prefixes catch families;
# exact keys cover known relay-leak vars (see wrapper.py:851-855). No secret
# values are ever logged by this helper.
_STRIP_PREFIXES = ("MCP_", "ANTHROPIC_")
_STRIP_CONTAINS = ("MCP",)
_STRIP_EXACT = frozenset({
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
    "KILO_CONFIG_CONTENT",
})


def _should_strip_env_key(key: str) -> bool:
    if key in _STRIP_EXACT:
        return True
    if any(key.startswith(p) for p in _STRIP_PREFIXES):
        return True
    if any(token in key for token in _STRIP_CONTAINS):
        return True
    return False


def build_claude_child_env(base_env: dict) -> dict:
    """Return a child env with MCP_*, ANTHROPIC_*, MCP-bearing, and known
    server-auth/proxy keys removed. Never injects ANTHROPIC_API_KEY (relies on
    existing subscription auth). Pure: returns a new dict, does not mutate input,
    does not log secret values.
    """
    return {k: v for k, v in base_env.items() if not _should_strip_env_key(k)}


# ---------------------------------------------------------------------------
# 4. Dedicated empty scratch CWD validator (fail-closed, no deletion)
# ---------------------------------------------------------------------------

# Default ownership marker file that designates a directory as an owned,
# disposable relay scratch dir (per Codex note: prefer owned fresh child dirs;
# never broad-delete shared paths). This validator NEVER deletes anything.
DEFAULT_OWNERSHIP_MARKER = ".agentchattr-relay-scratch"

_FORBIDDEN_ENTRIES = frozenset({".git", ".mcp.json", ".claude"})


@dataclass
class ScratchCheck:
    ok: bool
    reason: str = ""
    rejected_entry: str | None = None


def validate_scratch_cwd(
    path,
    *,
    repo_path=None,
    twinpet_path=None,
    home_path=None,
    ownership_marker: str = DEFAULT_OWNERSHIP_MARKER,
) -> ScratchCheck:
    """Fail-closed validation that ``path`` is a safe, dedicated relay scratch dir.

    Rejects (does NOT delete) when the path:
      * does not exist or is not a directory,
      * is, contains, or is contained by the agentchattr repo / Twinpet repo / home,
      * contains a .git, .mcp.json, or .claude entry (git worktree / MCP / project),
      * contains a project config artifact (config*.toml) or a log file,
      * is polluted: contains any entry other than the ownership marker.

    An empty directory, or a directory containing ONLY the ownership marker, passes.
    """
    p = Path(path).resolve()

    if not p.exists():
        return ScratchCheck(False, f"scratch path does not exist: {p}")
    if not p.is_dir():
        return ScratchCheck(False, f"scratch path is not a directory: {p}")

    # Reject identity/containment against protected roots (either direction).
    for label, ref in (("agentchattr repo", repo_path),
                        ("twinpet repo", twinpet_path),
                        ("user home", home_path)):
        if ref is None:
            continue
        ref_resolved = Path(ref).resolve()
        if p == ref_resolved or _is_relative_to(p, ref_resolved) or _is_relative_to(ref_resolved, p):
            return ScratchCheck(False, f"scratch path overlaps {label}: {p}")

    allowed = {ownership_marker}
    for child in p.iterdir():
        name = child.name
        if name in allowed:
            continue
        if name in _FORBIDDEN_ENTRIES:
            return ScratchCheck(False, f"forbidden entry present: {name}",
                                rejected_entry=name)
        if name.endswith(".log"):
            return ScratchCheck(False, f"log artifact present: {name}",
                                rejected_entry=name)
        if name.startswith("config") and name.endswith(".toml"):
            return ScratchCheck(False, f"project config artifact present: {name}",
                                rejected_entry=name)
        # Any other entry => polluted scratch dir.
        return ScratchCheck(False, f"polluted scratch dir (unexpected entry): {name}",
                            rejected_entry=name)

    return ScratchCheck(True, "")


def _is_relative_to(a: Path, b: Path) -> bool:
    """Backport of Path.is_relative_to for older Pythons."""
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False
