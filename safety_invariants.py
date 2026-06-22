"""Centralized safety-invariant validation for agentchattr tooling.

Single source of truth for the resilience/safety-hardening invariants. Every
validator here is PURE and FAILS CLOSED: unknown, missing, ambiguous, or
malformed input is rejected, never accepted by default. Allowlists are preferred
over denylists; denylist checks exist only as defense-in-depth on top of an
allowlist, never as the sole gate.

This module is additive and does not change any live execution path. It imports
the existing canonical constants/validators (relay eligibility, self-review
guard, safety-verdict parser) so the invariants are enforced against the SAME
objects the runtime uses, rather than duplicated copies. Wiring existing
call-sites to delegate here is a recommended, separately-scoped follow-up.

Invariant catalogue (see INVARIANTS):
  INV-001 Production Claude relay-ineligible unless separately approved.
  INV-002 AGY production relay-ineligible unless separately approved.
  INV-003 CodexSafe is a boundary guard only, never a workflow persona.
  INV-004 RELAY_ELIGIBLE_AGENTS is an explicit allowlist only.
  INV-005 store_exec args are an explicit allowlist only.
  INV-006 unsafe args fail closed.
  INV-007 coordinator/reviewer self-review rejected (internal runtime identity).
  INV-008 safety-verdict parsing scoped to safety roles only.
  INV-009 dry-run templates cannot activate production identities.
  INV-010 live relay activation requires an explicit activation flag.
  INV-011 missing/unknown run_mode fails closed.
  INV-012 duplicate agent identity fails closed.
  INV-013 shell/edit/MCP/subagent/Slack/Target injection rejected (reviewer-only).
  INV-014 no secrets/PATs/.env contents are emitted.
  INV-015 push automation requires clean tree and fast-forward only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical runtime objects — imported so invariants bind to the SAME values the
# runtime uses (no duplicated allowlists drifting out of sync).
from session_relay import RELAY_ELIGIBLE_AGENTS, parse_safety_verdict
from session_engine import (
    validate_no_self_review,
    _SAFETY_GATE_ROLES,
)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

INVARIANTS: dict[str, str] = {
    "INV-001": "Production Claude remains relay-ineligible unless separately approved.",
    "INV-002": "AGY remains production relay-ineligible unless separately approved.",
    "INV-003": "CodexSafe remains boundary guard only, not a workflow persona.",
    "INV-004": "RELAY_ELIGIBLE_AGENTS must be an explicit allowlist only.",
    "INV-005": "store_exec args must be an explicit allowlist only.",
    "INV-006": "unsafe args fail closed.",
    "INV-007": "coordinator/reviewer self-review is rejected (internal runtime identity).",
    "INV-008": "safety verdict parsing is scoped to safety roles only.",
    "INV-009": "dry-run templates cannot activate production identities.",
    "INV-010": "live relay activation requires an explicit activation flag.",
    "INV-011": "missing/unknown run_mode fails closed.",
    "INV-012": "duplicate agent identity fails closed.",
    "INV-013": "shell/edit/MCP/subagent/Slack/Target injection rejected for reviewer-only modes.",
    "INV-014": "no secrets/PATs/.env contents are emitted in reports.",
    "INV-015": "push automation requires a clean tree and fast-forward only.",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InvariantResult:
    """Outcome of a single invariant check. ``ok`` is False on any violation."""
    ok: bool
    code: str
    reason: str = ""
    detail: tuple = ()

    def __bool__(self) -> bool:  # truthy iff the invariant holds
        return self.ok


def _ok(code: str) -> InvariantResult:
    return InvariantResult(True, code, "")


def _fail(code: str, reason: str, detail=()) -> InvariantResult:
    return InvariantResult(False, code, reason, tuple(detail))


# ---------------------------------------------------------------------------
# Canonical allowlists / sets (explicit, fail-closed)
# ---------------------------------------------------------------------------

# Recognised run modes. Anything else (including None / "") fails closed.
KNOWN_RUN_MODES = frozenset({"tui", "exec", "store_exec", "claude_relay"})
# When an agent config omits run_mode entirely, the wrapper defaults to "tui".
DEFAULT_RUN_MODE = "tui"

# Identities that must NEVER be relay-eligible without a separate approved gate.
PRODUCTION_RELAY_INELIGIBLE = frozenset({"claude", "agy"})
# Branch-only / dry-run identities that must never appear in a main relay set.
BRANCH_ONLY_IDENTITIES = frozenset({"claude_dryrun"})
# Existing safety-mechanism (boundary-guard) identities — not workflow personas.
SAFETY_MECHANISM_IDENTITIES = frozenset({"codexsafe"})

# Explicit allowlist for AGY store_exec args (mirrors wrapper._build_agy_store_command).
STORE_EXEC_ALLOWED_VALUE_FLAGS = frozenset({"--model"})
STORE_EXEC_ALLOWED_BOOL_FLAGS: frozenset = frozenset()

# Defense-in-depth denylist applied ON TOP OF the allowlist (never the sole gate).
# Lowercased substring match.
UNSAFE_ARG_MARKERS = frozenset({
    "dangerously", "bypass", "yolo", "skip-permissions", "auto-approve",
    "danger-full-access", "workspace-write", "--mcp-config", "allowedtools",
    "target:", "slack", "subagent", "--tool", "--tools", "--edit",
    "--approval", "--unsafe", "persist",
})

# Injection markers rejected for reviewer-only modes (INV-013). Lowercased.
INJECTION_MARKERS = frozenset({
    "target:", "mcp", "slack mcp", "slack", "subagent", "shell", "edit files",
    "edit file", "approval bypass", "bypass approval", "permission persistence",
    "persist permission", "yolo", "unsafe", "bypass",
})


# ---------------------------------------------------------------------------
# INV-011 — run mode known / fail closed
# ---------------------------------------------------------------------------

def check_run_mode_known(run_mode, *, allow_default_missing: bool = False) -> InvariantResult:
    """Validate a run_mode value. Missing/unknown fails closed (INV-011).

    ``allow_default_missing=True`` treats a None/absent value as the wrapper's
    implicit "tui" default (used when validating raw config where omission is
    legitimate). An empty string or any unrecognised value always fails.
    """
    if run_mode is None:
        if allow_default_missing:
            return _ok("INV-011")
        return _fail("INV-011", "run_mode is missing")
    if not isinstance(run_mode, str) or not run_mode:
        return _fail("INV-011", f"run_mode is empty/invalid: {run_mode!r}")
    if run_mode not in KNOWN_RUN_MODES:
        return _fail("INV-011", f"unknown run_mode: {run_mode!r}")
    return _ok("INV-011")


# ---------------------------------------------------------------------------
# INV-001 / INV-002 / INV-004 / INV-009 — relay eligibility allowlist
# ---------------------------------------------------------------------------

def check_relay_eligibility(relay_set=RELAY_ELIGIBLE_AGENTS) -> InvariantResult:
    """Validate the relay-eligibility allowlist (INV-004, INV-001, INV-002).

    Fails closed if: the set is not a concrete set/frozenset (INV-004 requires an
    explicit allowlist, not a wildcard/callable), or contains any production
    relay-ineligible identity (claude/agy) or any branch-only dry-run identity.
    """
    if not isinstance(relay_set, (set, frozenset)):
        return _fail("INV-004", "relay eligibility must be an explicit set allowlist")
    lowered = {str(a).lower() for a in relay_set}
    prod = sorted(lowered & PRODUCTION_RELAY_INELIGIBLE)
    if prod:
        code = "INV-001" if "claude" in prod else "INV-002"
        return _fail(code, f"production relay-ineligible identity present: {prod}", prod)
    branch = sorted(lowered & BRANCH_ONLY_IDENTITIES)
    if branch:
        return _fail("INV-009", f"branch-only dry-run identity present: {branch}", branch)
    return _ok("INV-004")


def is_production_relay_ineligible(agent: str) -> bool:
    """True if ``agent`` is one that must stay relay-ineligible (claude/agy)."""
    return str(agent).lower() in PRODUCTION_RELAY_INELIGIBLE


# ---------------------------------------------------------------------------
# INV-003 — CodexSafe boundary-only
# ---------------------------------------------------------------------------

def check_codexsafe_boundary_only(role_to_agent: dict) -> InvariantResult:
    """Reject casting a safety-mechanism identity into a non-safety workflow role.

    CodexSafe (and any SAFETY_MECHANISM_IDENTITIES member) may only occupy a
    safety-gate role. Cast into any other (workflow persona) role it fails closed.
    """
    if not isinstance(role_to_agent, dict):
        return _fail("INV-003", "cast must be a role->agent mapping")
    for role, agent in role_to_agent.items():
        if not isinstance(role, str):
            return _fail("INV-003", f"invalid role key: {role!r}")
        if str(agent).lower() in SAFETY_MECHANISM_IDENTITIES and \
                role.lower() not in _SAFETY_GATE_ROLES:
            return _fail("INV-003",
                         f"safety mechanism '{agent}' cast into workflow role '{role}'",
                         (role, agent))
    return _ok("INV-003")


# ---------------------------------------------------------------------------
# INV-005 / INV-006 — store_exec args allowlist + unsafe fail-closed
# ---------------------------------------------------------------------------

def validate_store_exec_args(args, *,
                             allowed_value_flags=STORE_EXEC_ALLOWED_VALUE_FLAGS,
                             allowed_bool_flags=STORE_EXEC_ALLOWED_BOOL_FLAGS) -> InvariantResult:
    """Validate store_exec args against an explicit allowlist (INV-005/006).

    Fails closed on any flag not in the allowlists, on a value flag missing its
    value, and (defense-in-depth) on any unsafe marker. Allowlist is the primary
    gate; the denylist is secondary and can only reject, never permit.
    """
    if args is None:
        return _ok("INV-005")
    if not isinstance(args, (list, tuple)):
        return _fail("INV-005", "store_exec args must be a list/tuple")
    items = [str(a) for a in args]

    # Defense-in-depth denylist first (cannot permit anything; only rejects).
    unsafe = contains_unsafe_arg(items)
    if unsafe:
        return _fail("INV-006", f"unsafe arg marker(s): {unsafe}", tuple(unsafe))

    i = 0
    while i < len(items):
        arg = items[i]
        if arg in allowed_value_flags:
            if i + 1 >= len(items) or items[i + 1].startswith("-"):
                return _fail("INV-005", f"{arg} requires a value")
            i += 2
            continue
        if arg in allowed_bool_flags:
            i += 1
            continue
        return _fail("INV-005", f"unsupported argument: {arg!r}", (arg,))
    return _ok("INV-005")


def contains_unsafe_arg(args) -> list:
    """Return the unsafe markers found in ``args`` (defense-in-depth, INV-006)."""
    if not isinstance(args, (list, tuple)):
        return []
    blob = " ".join(str(a).lower() for a in args)
    return sorted({m for m in UNSAFE_ARG_MARKERS if m in blob})


# ---------------------------------------------------------------------------
# INV-007 — coordinator/reviewer self-review (delegates to canonical guard)
# ---------------------------------------------------------------------------

def check_no_self_review(role_to_identity: dict) -> InvariantResult:
    """Reject coordinator and reviewer resolving to the same identity (INV-007).

    Delegates to the canonical session_engine.validate_no_self_review so this
    module enforces the SAME rule the runtime uses.
    """
    res = validate_no_self_review(role_to_identity)
    if res.ok:
        return _ok("INV-007")
    return _fail("INV-007", res.reason, (getattr(res, "identity", None),))


# ---------------------------------------------------------------------------
# INV-008 — safety verdict role scoping
# ---------------------------------------------------------------------------

def check_safety_verdict_role(role: str) -> InvariantResult:
    """True only when ``role`` is an authorised safety-gate role (INV-008).

    Verdict parsing must be applied ONLY to safety-gate roles; calling code uses
    this to decide whether a turn's output may be interpreted as PASS/BLOCK.
    """
    if not isinstance(role, str) or not role:
        return _fail("INV-008", "role must be a non-empty string")
    if role.lower() not in _SAFETY_GATE_ROLES:
        return _fail("INV-008", f"role '{role}' is not a safety-gate role")
    return _ok("INV-008")


def parse_verdict_if_safety_role(role: str, output):
    """Fail-closed wrapper: parse a safety verdict only for a safety role.

    Returns the parsed SafetyVerdict when ``role`` is a safety-gate role, else
    None (the output must then be treated as ordinary content, never a verdict).
    """
    if not check_safety_verdict_role(role).ok:
        return None
    return parse_safety_verdict(output)


# ---------------------------------------------------------------------------
# INV-009 — dry-run template cannot activate production identities
# ---------------------------------------------------------------------------

def check_dryrun_template_safe(template: dict) -> InvariantResult:
    """Reject a session template that could activate production/dry-run identities.

    Templates define ROLES, not agent identities; any embedded production/dry-run
    identity, claude_relay run_mode, or relay-activation directive fails closed.
    """
    if not isinstance(template, dict):
        return _fail("INV-009", "template must be a mapping")
    blob = repr(template).lower()
    for marker in ("claude_relay", "claude_dryrun"):
        if marker in blob:
            return _fail("INV-009", f"template references {marker}", (marker,))
    # No production-ineligible identity may be pinned anywhere in the template.
    for ident in PRODUCTION_RELAY_INELIGIBLE:
        if re.search(rf"\b{re.escape(ident)}\b", blob):
            return _fail("INV-009", f"template pins production identity '{ident}'", (ident,))
    return _ok("INV-009")


# ---------------------------------------------------------------------------
# INV-010 — live relay activation requires an explicit flag
# ---------------------------------------------------------------------------

def require_live_relay_activation(activated, agent, relay_set=RELAY_ELIGIBLE_AGENTS) -> InvariantResult:
    """Gate live relay activation (INV-010).

    Fails closed unless the explicit activation flag is exactly True AND the agent
    is in the relay allowlist AND is not a production relay-ineligible identity.
    A falsy/None/non-bool flag never activates.
    """
    if activated is not True:
        return _fail("INV-010", "live relay activation flag is not explicitly True")
    if is_production_relay_ineligible(agent):
        return _fail("INV-001" if str(agent).lower() == "claude" else "INV-002",
                     f"agent '{agent}' is production relay-ineligible")
    if str(agent).lower() not in {str(a).lower() for a in relay_set}:
        return _fail("INV-010", f"agent '{agent}' is not in the relay allowlist")
    return _ok("INV-010")


# ---------------------------------------------------------------------------
# INV-012 — duplicate / unknown identity
# ---------------------------------------------------------------------------

def check_no_duplicate_identities(names) -> InvariantResult:
    """Fail closed if any identity name appears more than once (INV-012)."""
    if not isinstance(names, (list, tuple)):
        return _fail("INV-012", "names must be a list/tuple")
    seen: set = set()
    dups: list = []
    for n in names:
        key = str(n).lower()
        if key in seen and key not in dups:
            dups.append(key)
        seen.add(key)
    if dups:
        return _fail("INV-012", f"duplicate identities: {sorted(dups)}", tuple(sorted(dups)))
    return _ok("INV-012")


# ---------------------------------------------------------------------------
# INV-013 — injection rejection for reviewer-only modes
# ---------------------------------------------------------------------------

def check_no_injection(text, *, markers=INJECTION_MARKERS) -> InvariantResult:
    """Reject reviewer-only input containing tool/permission injection markers."""
    if text is None:
        return _ok("INV-013")
    if not isinstance(text, str):
        return _fail("INV-013", "text must be a string")
    low = text.lower()
    hits = sorted({m for m in markers if m in low})
    if hits:
        return _fail("INV-013", f"injection marker(s) detected: {hits}", tuple(hits))
    return _ok("INV-013")


# ---------------------------------------------------------------------------
# INV-014 — secret redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\b\s*[:=]\s*\S+"),
)

_REDACTED = "[REDACTED]"


def redact_secrets(text) -> str:
    """Return ``text`` with obvious secrets/PATs/tokens redacted (INV-014)."""
    if not text:
        return ""
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub(_REDACTED, s)
    return s


def contains_secret(text) -> bool:
    """True if ``text`` appears to contain a secret/PAT/token (INV-014)."""
    if not text:
        return False
    s = str(text)
    return any(pat.search(s) for pat in _SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# INV-015 — push preconditions (clean tree + fast-forward only)
# ---------------------------------------------------------------------------

def check_push_preconditions(*, clean_tree: bool, fast_forward: bool,
                             behind: int = 0) -> InvariantResult:
    """Gate push automation (INV-015): clean tree AND fast-forward only.

    Fails closed unless the working tree is clean, the push is fast-forward, and
    the local branch is not behind the remote (which would require a non-ff merge).
    """
    if clean_tree is not True:
        return _fail("INV-015", "working tree is not clean")
    if fast_forward is not True:
        return _fail("INV-015", "push is not fast-forward")
    if not isinstance(behind, int) or behind != 0:
        return _fail("INV-015", f"local branch is behind remote by {behind}")
    return _ok("INV-015")
