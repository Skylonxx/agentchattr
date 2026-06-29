"""Production coordinator loop — pure state machine (Phase 1).

Every worker transition is mediated by the coordinator role. This module
performs no I/O. Callers feed coordinator/worker outputs and receive
CoordinatorAction values describing the sole legal next target.

"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


COORDINATOR_ROLE = "coordinator"
WORKER_ROLES = frozenset({"developer", "ui_lead", "reviewer", "safety_gate"})

CLASSIFY_UI = "CLASSIFY: UI"
CLASSIFY_NON_UI = "CLASSIFY: NON_UI"
FINAL_PREFIX = "FINAL:"
BLOCKER_PREFIX = "BLOCKER:"
NEXT_PREFIX = "NEXT: "

VALID_NEXT_ROLES = frozenset(WORKER_ROLES)

DEFAULT_MAX_ROUNDS = 2
DEFAULT_MAX_MALFORMED_COORDINATOR = 2
DEFAULT_MAX_TOTAL_TRANSITIONS = 32
MIN_SUBSTANTIAL_DEVELOPER_CHARS = 400


@dataclass(frozen=True)
class CoordinatorLoopBudget:
    """Per-role round limits for coordinator-loop sessions."""
    developer: int = DEFAULT_MAX_ROUNDS
    ui_lead: int = DEFAULT_MAX_ROUNDS
    reviewer: int = DEFAULT_MAX_ROUNDS
    safety_gate: int = DEFAULT_MAX_ROUNDS
    max_total_transitions: int = DEFAULT_MAX_TOTAL_TRANSITIONS


PROMPT_MEMO_READONLY_BUDGET = CoordinatorLoopBudget(
    developer=4,
    ui_lead=2,
    reviewer=2,
    safety_gate=2,
    max_total_transitions=48,
)

PROMPT_MEMO_BUDGET = CoordinatorLoopBudget(
    developer=3,
    ui_lead=2,
    reviewer=2,
    safety_gate=2,
    max_total_transitions=40,
)


def resolve_loop_budget_from_session(session: dict[str, Any] | None) -> CoordinatorLoopBudget:
    """Resolve expanded bounded budgets for Prompt Memo / read-only analysis sessions."""
    if not isinstance(session, dict):
        return CoordinatorLoopBudget()
    policy = session.get("workspace_policy") or {}
    has_prompt = bool(str(session.get("prompt_body") or "").strip())
    try:
        from workspace_policy_runtime import is_report_only_readonly_policy
        readonly_analysis = is_report_only_readonly_policy(policy)
    except ImportError:
        readonly_analysis = bool(policy.get("analysis_report_only"))
    if readonly_analysis or (has_prompt and policy.get("mode") == "read-only"):
        return PROMPT_MEMO_READONLY_BUDGET
    if has_prompt:
        return PROMPT_MEMO_BUDGET
    return CoordinatorLoopBudget()

_ROUTING_TOKEN_RE = re.compile(
    r"^(CLASSIFY: (?:UI|NON_UI)|NEXT: \w+|FINAL:|BLOCKER:)",
)
_SAFETY_VERDICT_LINE_RE = re.compile(
    r"^(PASS(?:\s|$)|PASS WITH NOTES|BLOCK:)",
    re.IGNORECASE,
)

DEVELOPER_RECOGNIZED_TOKENS = frozenset({
    "READY_FOR_COORDINATOR",
    "BLOCKER",
    "BLOCKED",
    "WORKER_TIMEOUT",
    "PROGRESS",
    "PASS",
    "PASS_WITH_NOTES",
    "REQUEST_CHANGES",
    "FAIL",
})

_PROGRESS_PREFIX_RE = re.compile(
    r"^(?:starting|running)\s+prechecks?\b",
    re.IGNORECASE,
)

_PROGRESS_INDICATOR_MARKERS = (
    "verifying workspace",
    "checking workspace",
    "checking git head",
    "verifying git head",
    "checking clean working tree",
    "verifying clean working tree",
    "working tree status",
    "clean working tree",
    "reading ",
    "inspecting ",
    "analyzing ",
    "running preflight",
    "preflight",
)

_BLOCKER_INDICATOR_MARKERS = (
    "wrong cwd",
    "expected head mismatch",
    "dirty tree",
    "forbidden write",
    "policy mismatch",
    "git write command",
    "permission denied",
    "shell escape",
    "safety_gate block",
)


class CoordinatorPhase(Enum):
    INTAKE = "intake"
    AWAIT_COORDINATOR = "await_coordinator"
    AWAIT_DEVELOPER = "await_developer"
    AWAIT_UI_LEAD = "await_ui_lead"
    AWAIT_REVIEWER = "await_reviewer"
    AWAIT_SAFETY_GATE = "await_safety_gate"
    FINAL = "final"
    BLOCKER = "blocker"
    HALTED = "halted"


@dataclass
class CoordinatorLoopState:
    phase: CoordinatorPhase = CoordinatorPhase.INTAKE
    awaiting_role: str = COORDINATOR_ROLE
    requires_agy: bool = False
    classified: bool = False
    agy_approved: bool = False
    reviewer_passed: bool = False
    safety_passed: bool = False
    ui_changed: bool = False
    developer_round: int = 0
    ui_round: int = 0
    review_round: int = 0
    safety_round: int = 0
    malformed_coordinator_round: int = 0
    total_transition_count: int = 0
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_developer_rounds: int = DEFAULT_MAX_ROUNDS
    max_ui_rounds: int = DEFAULT_MAX_ROUNDS
    max_review_rounds: int = DEFAULT_MAX_ROUNDS
    max_safety_rounds: int = DEFAULT_MAX_ROUNDS
    max_total_transitions: int = DEFAULT_MAX_TOTAL_TRANSITIONS
    awaiting_developer_correction: bool = False
    developer_correction_complete: bool = False
    developer_has_substantial_output: bool = False
    last_developer_token: str = ""
    last_reviewer_verdict: str = ""
    session_prompt_id: str = ""
    session_workspace_profile: str = ""
    session_workspace_mode: str = ""
    task_description: str = ""
    last_role: str = ""
    last_output_summary: str = ""
    verdict_log: list[dict[str, Any]] = field(default_factory=list)
    halt_reason: str = ""
    blocker_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoordinatorLoopState:
        """Rehydrate persisted loop state. Raises ValueError on corrupt data."""
        if not isinstance(data, dict):
            raise ValueError("coordinator_loop_state must be a dict")
        try:
            phase = CoordinatorPhase(str(data["phase"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid coordinator_loop phase: {exc}") from exc
        legacy_max = int(data.get("max_rounds", DEFAULT_MAX_ROUNDS))
        return cls(
            phase=phase,
            awaiting_role=str(data.get("awaiting_role", COORDINATOR_ROLE)),
            requires_agy=bool(data.get("requires_agy", False)),
            classified=bool(data.get("classified", False)),
            agy_approved=bool(data.get("agy_approved", False)),
            reviewer_passed=bool(data.get("reviewer_passed", False)),
            safety_passed=bool(data.get("safety_passed", False)),
            ui_changed=bool(data.get("ui_changed", False)),
            developer_round=int(data.get("developer_round", 0)),
            ui_round=int(data.get("ui_round", 0)),
            review_round=int(data.get("review_round", 0)),
            safety_round=int(data.get("safety_round", 0)),
            malformed_coordinator_round=int(data.get("malformed_coordinator_round", 0)),
            total_transition_count=int(data.get("total_transition_count", 0)),
            max_rounds=legacy_max,
            max_developer_rounds=int(data.get("max_developer_rounds", legacy_max)),
            max_ui_rounds=int(data.get("max_ui_rounds", legacy_max)),
            max_review_rounds=int(data.get("max_review_rounds", legacy_max)),
            max_safety_rounds=int(data.get("max_safety_rounds", legacy_max)),
            max_total_transitions=int(
                data.get("max_total_transitions", DEFAULT_MAX_TOTAL_TRANSITIONS)),
            awaiting_developer_correction=bool(data.get("awaiting_developer_correction", False)),
            developer_correction_complete=bool(data.get("developer_correction_complete", False)),
            developer_has_substantial_output=bool(data.get("developer_has_substantial_output", False)),
            last_developer_token=str(data.get("last_developer_token", "")),
            last_reviewer_verdict=str(data.get("last_reviewer_verdict", "")),
            session_prompt_id=str(data.get("session_prompt_id", "")),
            session_workspace_profile=str(data.get("session_workspace_profile", "")),
            session_workspace_mode=str(data.get("session_workspace_mode", "")),
            task_description=str(data.get("task_description", "")),
            last_role=str(data.get("last_role", "")),
            last_output_summary=str(data.get("last_output_summary", "")),
            verdict_log=list(data.get("verdict_log", [])),
            halt_reason=str(data.get("halt_reason", "")),
            blocker_reason=str(data.get("blocker_reason", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "awaiting_role": self.awaiting_role,
            "requires_agy": self.requires_agy,
            "classified": self.classified,
            "agy_approved": self.agy_approved,
            "reviewer_passed": self.reviewer_passed,
            "safety_passed": self.safety_passed,
            "ui_changed": self.ui_changed,
            "developer_round": self.developer_round,
            "ui_round": self.ui_round,
            "review_round": self.review_round,
            "safety_round": self.safety_round,
            "malformed_coordinator_round": self.malformed_coordinator_round,
            "total_transition_count": self.total_transition_count,
            "max_rounds": self.max_rounds,
            "max_developer_rounds": self.max_developer_rounds,
            "max_ui_rounds": self.max_ui_rounds,
            "max_review_rounds": self.max_review_rounds,
            "max_safety_rounds": self.max_safety_rounds,
            "max_total_transitions": self.max_total_transitions,
            "awaiting_developer_correction": self.awaiting_developer_correction,
            "developer_correction_complete": self.developer_correction_complete,
            "developer_has_substantial_output": self.developer_has_substantial_output,
            "last_developer_token": self.last_developer_token,
            "last_reviewer_verdict": self.last_reviewer_verdict,
            "session_prompt_id": self.session_prompt_id,
            "session_workspace_profile": self.session_workspace_profile,
            "session_workspace_mode": self.session_workspace_mode,
            "task_description": self.task_description,
            "last_role": self.last_role,
            "last_output_summary": self.last_output_summary,
            "verdict_log": list(self.verdict_log),
            "halt_reason": self.halt_reason,
            "blocker_reason": self.blocker_reason,
        }


@dataclass
class ParsedRouting:
    ok: bool
    kind: str = ""
    next_role: str = ""
    body: str = ""
    reason: str = ""


@dataclass
class CoordinatorAction:
    target_role: str
    prompt_context: str = ""
    is_terminal: bool = False
    terminal_kind: str = ""
    routing_body: str = ""


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _body_after_first_line(text: str) -> str:
    lines = text.splitlines()
    started = False
    body_lines: list[str] = []
    for line in lines:
        if not started:
            if line.strip():
                started = True
            continue
        body_lines.append(line)
    return "\n".join(body_lines).strip()


def _count_routing_tokens(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ROUTING_TOKEN_RE.match(stripped):
            count += 1
    return count


def parse_coordinator_routing(text: str | None) -> ParsedRouting:
    """Parse coordinator routing metadata from output text."""
    if not text or not str(text).strip():
        return ParsedRouting(ok=False, reason="empty coordinator output")

    if _count_routing_tokens(text) > 1:
        return ParsedRouting(ok=False, reason="multiple routing tokens")

    first = _first_non_empty_line(text)
    body = _body_after_first_line(text)

    if first == CLASSIFY_UI:
        return ParsedRouting(ok=True, kind="classify_ui", body=body)
    if first == CLASSIFY_NON_UI:
        return ParsedRouting(ok=True, kind="classify_non_ui", body=body)

    if first.startswith(NEXT_PREFIX):
        role = first[len(NEXT_PREFIX):].strip()
        if role not in VALID_NEXT_ROLES:
            return ParsedRouting(ok=False, reason=f"unknown NEXT role: {role}")
        return ParsedRouting(ok=True, kind="next", next_role=role, body=body)

    if first.startswith(FINAL_PREFIX):
        final_body = first[len(FINAL_PREFIX):].strip()
        if final_body:
            body = f"{final_body}\n{body}".strip() if body else final_body
        return ParsedRouting(ok=True, kind="final", body=body)

    if first.startswith(BLOCKER_PREFIX):
        reason = first[len(BLOCKER_PREFIX):].strip()
        if not reason:
            return ParsedRouting(ok=False, reason="malformed BLOCKER (missing reason)")
        if body:
            reason = f"{reason}\n{body}".strip()
        return ParsedRouting(ok=True, kind="blocker", reason=reason, body=body)

    return ParsedRouting(ok=False, reason="unrecognized routing token")


def _append_verdict(state: CoordinatorLoopState, role: str, token: str,
                    notes: str = "") -> None:
    state.verdict_log.append({
        "role": role,
        "token": token,
        "notes": notes,
        "time": time.time(),
    })


def _increment_transition(state: CoordinatorLoopState) -> CoordinatorAction | None:
    state.total_transition_count += 1
    if state.total_transition_count > state.max_total_transitions:
        return _terminal_blocker(state, "max total transitions exceeded")
    return None


def _terminal_blocker(state: CoordinatorLoopState, reason: str) -> CoordinatorAction:
    state.phase = CoordinatorPhase.BLOCKER
    state.blocker_reason = reason
    state.awaiting_role = ""
    return CoordinatorAction(
        target_role=COORDINATOR_ROLE,
        prompt_context=reason,
        is_terminal=True,
        terminal_kind="blocker",
    )


def _terminal_final(state: CoordinatorLoopState, body: str) -> CoordinatorAction:
    state.phase = CoordinatorPhase.FINAL
    state.awaiting_role = ""
    return CoordinatorAction(
        target_role=COORDINATOR_ROLE,
        prompt_context=body or "session complete",
        is_terminal=True,
        terminal_kind="final",
        routing_body=body,
    )


def _coordinator_action(state: CoordinatorLoopState, prompt: str) -> CoordinatorAction:
    state.phase = CoordinatorPhase.AWAIT_COORDINATOR
    state.awaiting_role = COORDINATOR_ROLE
    return CoordinatorAction(
        target_role=COORDINATOR_ROLE,
        prompt_context=prompt,
    )


def _wrap_worker_handoff_prompt(role: str, prompt: str, body: str = "") -> tuple[str, str]:
    """Ensure coordinator worker handoffs include explicit TO: routing headers."""
    from session_relay import ensure_explicit_routing_headers

    text = (body or prompt or f"Coordinator prompt for {role}").strip()
    wrapped = ensure_explicit_routing_headers(
        text,
        role=role,
        from_source="agentchattr-coordinator-loop",
        subject="coordinator-worker-handoff",
    )
    return wrapped, wrapped


def _worker_action(state: CoordinatorLoopState, role: str, prompt: str,
                   body: str = "") -> CoordinatorAction:
    phase_map = {
        "developer": CoordinatorPhase.AWAIT_DEVELOPER,
        "ui_lead": CoordinatorPhase.AWAIT_UI_LEAD,
        "reviewer": CoordinatorPhase.AWAIT_REVIEWER,
        "safety_gate": CoordinatorPhase.AWAIT_SAFETY_GATE,
    }
    state.phase = phase_map[role]
    state.awaiting_role = role
    wrapped_prompt, wrapped_body = _wrap_worker_handoff_prompt(role, prompt, body)
    return CoordinatorAction(
        target_role=role,
        prompt_context=wrapped_prompt,
        routing_body=wrapped_body,
    )


def _malformed_coordinator(state: CoordinatorLoopState, reason: str) -> CoordinatorAction:
    state.malformed_coordinator_round += 1
    if state.malformed_coordinator_round > DEFAULT_MAX_MALFORMED_COORDINATOR:
        return _terminal_blocker(state, f"max malformed coordinator rounds exceeded: {reason}")
    return _coordinator_action(
        state,
        f"Malformed routing ({reason}). Re-emit a single valid first-line token.",
    )


def _reject_coordinator_while_awaiting_worker(state: CoordinatorLoopState) -> CoordinatorAction:
    """Reject coordinator routing without reopening coordinator or dropping pending worker."""
    pending = state.awaiting_role
    return CoordinatorAction(
        target_role=pending,
        prompt_context=(
            f"Coordinator output rejected while awaiting {pending!r}. "
            "Pending worker must respond before coordinator may route again."
        ),
    )


def _reject_out_of_turn_worker(state: CoordinatorLoopState, role: str) -> CoordinatorAction:
    return _terminal_blocker(
        state,
        f"out-of-turn worker output: expected {state.awaiting_role!r}, got {role!r}",
    )


def _validate_next_role(state: CoordinatorLoopState, role: str) -> str | None:
    if role == "ui_lead" and not state.requires_agy:
        return "NEXT: ui_lead rejected when requires_agy is false"
    if role == "reviewer" and state.requires_agy and not state.agy_approved:
        return "NEXT: reviewer rejected before AGY approval in UI flow"
    if role == "safety_gate" and not state.reviewer_passed:
        return "NEXT: safety_gate rejected before reviewer pass"
    if role == "developer":
        if state.awaiting_developer_correction:
            return None
        if state.developer_correction_complete and state.review_round > 0 and not state.reviewer_passed:
            return "NEXT: developer rejected — correction delivered; route reviewer for re-check"
        if (
            state.developer_has_substantial_output
            and state.last_developer_token == "READY_FOR_COORDINATOR"
            and state.developer_round >= state.max_developer_rounds
        ):
            return "NEXT: developer rejected — round budget reached with usable analysis"
        if (
            state.requires_agy
            and not state.agy_approved
            and state.last_developer_token == "READY_FOR_COORDINATOR"
            and state.awaiting_developer_correction is False
            and state.developer_correction_complete
        ):
            return "NEXT: developer rejected — route ui_lead for UX re-review"
    return None


def coordinator_allowed_tokens(state: CoordinatorLoopState) -> list[str]:
    """Return routing tokens the coordinator may emit in the current state."""
    tokens: list[str] = []
    if not state.classified:
        tokens.extend([CLASSIFY_UI, CLASSIFY_NON_UI])
        return tokens
    for role in sorted(VALID_NEXT_ROLES):
        if _validate_next_role(state, role) is None:
            tokens.append(f"{NEXT_PREFIX}{role}")
    if state.safety_passed:
        tokens.append(FINAL_PREFIX)
    tokens.append(f"{BLOCKER_PREFIX}<reason>")
    return tokens


def on_session_start(
    task_description: str,
    *,
    loop_budget: CoordinatorLoopBudget | None = None,
    max_rounds: int | None = None,
    session_meta: dict[str, Any] | None = None,
) -> tuple[CoordinatorLoopState, CoordinatorAction]:
    """Initialize loop state and trigger coordinator intake first."""
    budget = loop_budget or CoordinatorLoopBudget()
    if max_rounds is not None:
        budget = CoordinatorLoopBudget(
            developer=max_rounds,
            ui_lead=max_rounds,
            reviewer=max_rounds,
            safety_gate=max_rounds,
            max_total_transitions=budget.max_total_transitions,
        )
    meta = session_meta or {}
    state = CoordinatorLoopState(
        task_description=task_description,
        max_rounds=budget.developer,
        max_developer_rounds=budget.developer,
        max_ui_rounds=budget.ui_lead,
        max_review_rounds=budget.reviewer,
        max_safety_rounds=budget.safety_gate,
        max_total_transitions=budget.max_total_transitions,
        phase=CoordinatorPhase.AWAIT_COORDINATOR,
        awaiting_role=COORDINATOR_ROLE,
        session_prompt_id=str(meta.get("prompt_id") or ""),
        session_workspace_profile=str(meta.get("workspace_profile") or ""),
        session_workspace_mode=str(meta.get("workspace_mode") or ""),
    )
    state.total_transition_count = 1
    action = CoordinatorAction(
        target_role=COORDINATOR_ROLE,
        prompt_context=f"Intake task: {task_description}",
    )
    return state, action


def on_coordinator_output(state: CoordinatorLoopState, text: str) -> CoordinatorAction:
    """Parse coordinator routing and emit the sole legal next target."""
    if blocked := _increment_transition(state):
        return blocked

    if state.awaiting_role != COORDINATOR_ROLE:
        return _reject_coordinator_while_awaiting_worker(state)

    state.last_role = COORDINATOR_ROLE
    state.last_output_summary = (text or "")[:500]
    parsed = parse_coordinator_routing(text)
    if not parsed.ok:
        return _malformed_coordinator(state, parsed.reason)

    if parsed.kind == "classify_ui":
        state.classified = True
        state.requires_agy = True
        return _coordinator_action(state, "Scope classified UI. Prompt the developer.")

    if parsed.kind == "classify_non_ui":
        state.classified = True
        state.requires_agy = False
        state.agy_approved = False
        return _coordinator_action(state, "Scope classified NON_UI. Prompt the developer.")

    if parsed.kind == "next":
        if not state.classified:
            return _malformed_coordinator(state, "classification required before worker dispatch")
        err = _validate_next_role(state, parsed.next_role)
        if err:
            return _malformed_coordinator(state, err)
        prompt = parsed.body or f"Coordinator prompt for {parsed.next_role}"
        return _worker_action(state, parsed.next_role, prompt, parsed.body)

    if parsed.kind == "final":
        if not state.safety_passed:
            return _malformed_coordinator(state, "FINAL rejected before safety pass")
        return _terminal_final(state, parsed.body)

    if parsed.kind == "blocker":
        return _terminal_blocker(state, parsed.reason)

    return _malformed_coordinator(state, "unhandled routing kind")


def _looks_like_blocker_line(first: str) -> bool:
    """Heuristic blocker lines — must win over progress normalization."""
    if not first:
        return False
    low = first.lower().strip()
    if low.startswith("blocker:") or low.startswith("blocked:"):
        return True
    return any(marker in low for marker in _BLOCKER_INDICATOR_MARKERS)


def _looks_like_progress_line(first: str) -> bool:
    """Case-insensitive interim worker status during long Prompt Memo tasks."""
    if not first or _looks_like_blocker_line(first):
        return False
    low = first.lower().strip()
    if low in ("progress", "in progress"):
        return True
    if low.startswith("progress:") or low.startswith("progress "):
        return True
    if _PROGRESS_PREFIX_RE.match(first.strip()):
        return True
    return any(marker in low for marker in _PROGRESS_INDICATOR_MARKERS)


def _looks_like_legacy_progress(first: str) -> bool:
    """Backward-compatible alias for diagnostics and callers."""
    return _looks_like_progress_line(first)


def _normalize_worker_token(first: str) -> str:
    """Normalize spaced/human variants to canonical worker tokens."""
    if first == "PASS WITH NOTES":
        return "PASS_WITH_NOTES"
    if first.startswith("BLOCKER:"):
        return "BLOCKER"
    if first == "BLOCKER":
        return "BLOCKER"
    return first


def build_ambiguous_worker_diagnostic(
    role: str,
    first_line: str,
    *,
    worker_context: dict[str, Any] | None = None,
    full_text: str = "",
) -> str:
    """Build diagnostic text when worker output must be classified ambiguous."""
    ctx = worker_context if isinstance(worker_context, dict) else {}
    policy = ctx.get("workspace_policy") if isinstance(ctx.get("workspace_policy"), dict) else {}
    lines = [
        "Ambiguous output diagnostics:",
        f"- role: {role}",
        f"- first line: {first_line[:200] if first_line else '(empty)'}",
        f"- workspace_profile: {policy.get('policy_id') or ctx.get('policy_id') or '(none)'}",
        f"- workspace_mode: {policy.get('mode') or ctx.get('policy_mode') or '(none)'}",
        f"- prompt_id: {ctx.get('prompt_id') or '(none)'}",
        f"- prompt_body present: {'yes' if ctx.get('has_prompt_body') else 'no'}",
        f"- looked like progress: {'yes' if _looks_like_legacy_progress(first_line) else 'no'}",
    ]
    if full_text and len(full_text) > len(first_line):
        lines.append(f"- output preview: {full_text[:300]}")
    return "\n".join(lines)


def _parse_developer_verdict(text: str) -> tuple[str, str]:
    first = _first_non_empty_line(text)
    first_norm = _normalize_worker_token(first)

    if first_norm in DEVELOPER_RECOGNIZED_TOKENS:
        if first_norm == "BLOCKER":
            reason = first[len("BLOCKER:"):].strip() if first.startswith("BLOCKER:") else _body_after_first_line(text)
            return "BLOCKER", reason or first
        return first_norm, _body_after_first_line(text)

    if _looks_like_blocker_line(first):
        return "BLOCKER", first if first else _body_after_first_line(text)

    if _looks_like_progress_line(first):
        return "PROGRESS", first if first else _body_after_first_line(text)

    return "AMBIGUOUS", first or "empty developer output"


def _parse_ui_lead_verdict(text: str) -> tuple[str, str]:
    first = _first_non_empty_line(text)
    first_norm = _normalize_worker_token(first)
    if first_norm == "WORKER_TIMEOUT":
        return first_norm, _body_after_first_line(text)
    if first_norm == "PROGRESS" or _looks_like_progress_line(first):
        return "PROGRESS", first if first else _body_after_first_line(text)
    if first_norm == "BLOCKER" or _looks_like_blocker_line(first):
        reason = first[len("BLOCKER:"):].strip() if first.startswith("BLOCKER:") else _body_after_first_line(text)
        return "BLOCKER", reason or first
    if first in ("UX_APPROVED", "REQUEST UX CHANGES", "BLOCKED"):
        return first, _body_after_first_line(text)
    if first == "PASS WITH NOTES":
        return "INVALID", "PASS WITH NOTES is not valid AGY approval"
    return "AMBIGUOUS", first or "empty ui_lead output"


def _parse_reviewer_verdict(text: str) -> tuple[str, str]:
    first = _first_non_empty_line(text)
    if first in ("PASS", "PASS WITH NOTES", "REQUEST CHANGES", "BLOCKED"):
        return first, _body_after_first_line(text)
    return "AMBIGUOUS", first or "empty reviewer output"


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _parse_safety_verdict(text: str) -> tuple[str, str]:
    lines = _non_empty_lines(text)
    if not lines:
        return "AMBIGUOUS", "empty safety_gate output"

    verdict_lines = [line for line in lines if _SAFETY_VERDICT_LINE_RE.match(line)]
    if len(verdict_lines) > 1:
        return "MIXED", "mixed safety verdict"

    first = lines[0]
    if first == "PASS WITH NOTES" or first.startswith("PASS WITH NOTES "):
        return "INVALID", "PASS WITH NOTES is invalid for safety_gate"

    if first == "PASS":
        for extra in lines[1:]:
            if _SAFETY_VERDICT_LINE_RE.match(extra):
                return "MIXED", "mixed safety verdict"
        return "PASS", _body_after_first_line(text)

    if first.startswith("BLOCK:"):
        for extra in lines[1:]:
            if _SAFETY_VERDICT_LINE_RE.match(extra):
                return "MIXED", "mixed safety verdict"
        reason = first[len("BLOCK:"):].strip()
        if not reason:
            return "INVALID", "malformed safety verdict: BLOCK requires non-empty reason"
        return "BLOCK", reason

    return "AMBIGUOUS", first


def _detect_ui_changed(notes: str) -> bool:
    low = notes.lower()
    keywords = (
        ".tsx", ".jsx", ".css", "layout", "modal", "button", "responsive",
        "ui ", " ux", "tailwind", "flowbite", "screen", "dashboard",
    )
    return any(k in low for k in keywords)


def on_worker_output(
    state: CoordinatorLoopState,
    role: str,
    text: str,
    *,
    worker_context: dict[str, Any] | None = None,
) -> CoordinatorAction:
    """Handle worker verdict — always returns coordinator (or terminal blocker)."""
    if role not in WORKER_ROLES:
        return _terminal_blocker(state, f"unknown worker role: {role}")

    if blocked := _increment_transition(state):
        return blocked

    if state.awaiting_role == COORDINATOR_ROLE or state.awaiting_role != role:
        return _reject_out_of_turn_worker(state, role)

    state.last_role = role
    state.last_output_summary = (text or "")[:500]

    if role == "developer":
        return _on_developer_output(state, text, worker_context=worker_context)
    if role == "ui_lead":
        return _on_ui_lead_output(state, text, worker_context=worker_context)
    if role == "reviewer":
        return _on_reviewer_output(state, text)
    return _on_safety_output(state, text)


def _has_substantial_developer_content(text: str, notes: str) -> bool:
    """True when developer output contains enough analysis to continue routing."""
    body = (notes or _body_after_first_line(text) or "").strip()
    if len(body) >= MIN_SUBSTANTIAL_DEVELOPER_CHARS:
        return True
    markers = ("report_begin", "## ", "### ", "paymentmodal", "data flow", "findings")
    low = body.lower()
    return any(marker in low for marker in markers) and len(body) >= 120


def _last_reviewer_verdict_from_log(state: CoordinatorLoopState) -> str:
    for entry in reversed(state.verdict_log):
        if entry.get("role") == "reviewer":
            return str(entry.get("token") or "")
    return state.last_reviewer_verdict


def _next_role_hint_after_developer_ready(
    state: CoordinatorLoopState,
    *,
    budget_exceeded: bool = False,
) -> str:
    if state.requires_agy and not state.agy_approved:
        return (
            "Developer analysis/correction is READY_FOR_COORDINATOR with substantial content. "
            "Route ui_lead (AGY) for UX review with explicit TO: header. "
            "Do NOT route developer again unless a new reviewer REQUEST CHANGES requires it."
        )
    if state.review_round > 0 and not state.reviewer_passed:
        return (
            "Developer correction pass is complete after reviewer REQUEST CHANGES. "
            "Route reviewer for re-check with explicit TO: header. "
            "Do NOT route developer again unless reviewer issues new REQUEST CHANGES."
        )
    if budget_exceeded:
        return (
            "Developer round budget reached but analysis is substantial. "
            "Emit FINAL synthesis with PASS_WITH_NOTES or route reviewer/ui_lead if still required. "
            "Do NOT issue a tooling BLOCKER for max developer rounds."
        )
    if state.requires_agy and state.agy_approved and not state.reviewer_passed:
        return "Developer analysis ready. Route reviewer with explicit TO: header."
    return "Developer reported READY_FOR_COORDINATOR. Decide next routing."


def _coordinator_action_after_developer_ready(
    state: CoordinatorLoopState,
    *,
    budget_exceeded: bool = False,
) -> CoordinatorAction:
    if state.awaiting_developer_correction:
        state.awaiting_developer_correction = False
        state.developer_correction_complete = True
    return _coordinator_action(
        state,
        _next_role_hint_after_developer_ready(state, budget_exceeded=budget_exceeded),
    )


def _build_max_developer_rounds_diagnostics(
    state: CoordinatorLoopState,
    token: str,
    text: str,
    notes: str,
) -> str:
    substantial = _has_substantial_developer_content(text, notes)
    next_role = "reviewer"
    if state.requires_agy and not state.agy_approved:
        next_role = "ui_lead"
    lines = [
        "max developer rounds exceeded",
        f"role=developer",
        f"max_rounds={state.max_developer_rounds}",
        f"actual_rounds={state.developer_round}",
        f"last_first_line_token={token}",
        f"ready_for_coordinator={'yes' if token == 'READY_FOR_COORDINATOR' else 'no'}",
        f"substantial_content={'yes' if substantial else 'no'}",
        f"last_reviewer_verdict={_last_reviewer_verdict_from_log(state) or '(none)'}",
        f"next_intended_role={next_role}",
        f"prompt_id={state.session_prompt_id or '(none)'}",
        f"workspace_profile={state.session_workspace_profile or '(none)'}",
        f"workspace_mode={state.session_workspace_mode or '(none)'}",
    ]
    return "\n".join(lines)


def _on_developer_output(
    state: CoordinatorLoopState,
    text: str,
    *,
    worker_context: dict[str, Any] | None = None,
) -> CoordinatorAction:
    token, notes = _parse_developer_verdict(text)
    _append_verdict(state, "developer", token, notes)
    state.last_developer_token = token

    if token == "AMBIGUOUS":
        first = _first_non_empty_line(text)
        diag = build_ambiguous_worker_diagnostic(
            "developer", first, worker_context=worker_context, full_text=text or "",
        )
        return _terminal_blocker(state, f"ambiguous developer output: {notes}\n{diag}")

    if token == "WORKER_TIMEOUT":
        return _coordinator_action(
            state,
            "Developer worker timed out (WORKER_TIMEOUT — infrastructure, not implementation failure). "
            f"Coordinator may retry or issue BLOCKER. Details: {notes[:400]}",
        )

    if token in ("BLOCKER", "BLOCKED"):
        return _terminal_blocker(state, f"developer blocked: {notes}")

    if token == "PROGRESS":
        return _coordinator_action(
            state,
            "Developer reported PROGRESS (work in flight, not final). "
            f"Status: {notes[:400]}. Coordinator may NEXT: developer to continue or wait.",
        )

    state.developer_round += 1
    substantial = token == "READY_FOR_COORDINATOR" and _has_substantial_developer_content(text, notes)
    if substantial:
        state.developer_has_substantial_output = True

    over_budget = state.developer_round > state.max_developer_rounds
    if over_budget:
        if substantial:
            return _coordinator_action_after_developer_ready(state, budget_exceeded=True)
        return _terminal_blocker(
            state,
            _build_max_developer_rounds_diagnostics(state, token, text, notes),
        )

    if token == "READY_FOR_COORDINATOR" and substantial:
        return _coordinator_action_after_developer_ready(state)

    return _coordinator_action(
        state,
        f"Developer reported {token}. Decide next routing.",
    )


def _on_ui_lead_output(
    state: CoordinatorLoopState,
    text: str,
    *,
    worker_context: dict[str, Any] | None = None,
) -> CoordinatorAction:
    token, notes = _parse_ui_lead_verdict(text)
    _append_verdict(state, "ui_lead", token, notes)

    if token == "INVALID":
        return _terminal_blocker(state, notes)
    if token == "AMBIGUOUS":
        first = _first_non_empty_line(text)
        diag = build_ambiguous_worker_diagnostic(
            "ui_lead", first, worker_context=worker_context, full_text=text or "",
        )
        return _terminal_blocker(state, f"ambiguous ui_lead output: {notes}\n{diag}")
    if token == "WORKER_TIMEOUT":
        return _coordinator_action(
            state,
            "UI lead worker timed out (WORKER_TIMEOUT). Coordinator may retry or BLOCKER.",
        )
    if token in ("BLOCKER", "BLOCKED"):
        return _terminal_blocker(state, f"ui_lead blocked: {notes}")
    if token == "PROGRESS":
        return _coordinator_action(
            state,
            f"UI lead reported PROGRESS. Status: {notes[:400]}",
        )

    if token == "UX_APPROVED":
        state.agy_approved = True
        state.ui_changed = False
        return _coordinator_action(state, "AGY approved UX. Route to reviewer.")

    # REQUEST UX CHANGES
    state.ui_round += 1
    state.agy_approved = False
    state.awaiting_developer_correction = True
    state.developer_correction_complete = False
    if state.ui_round > state.max_ui_rounds:
        return _terminal_blocker(state, "max ui_round exceeded")
    return _coordinator_action(
        state,
        f"AGY requested UX changes (ui_round={state.ui_round}). Route developer correction.",
    )


def _on_reviewer_output(state: CoordinatorLoopState, text: str) -> CoordinatorAction:
    token, notes = _parse_reviewer_verdict(text)
    _append_verdict(state, "reviewer", token, notes)

    if token == "AMBIGUOUS":
        return _terminal_blocker(state, f"ambiguous reviewer output: {notes}")
    if token == "BLOCKED":
        return _terminal_blocker(state, f"reviewer blocked: {notes}")

    if token in ("PASS", "PASS WITH NOTES"):
        state.reviewer_passed = True
        state.last_reviewer_verdict = token
        return _coordinator_action(state, "Reviewer passed. Route to safety_gate.")

    # REQUEST CHANGES
    state.review_round += 1
    state.reviewer_passed = False
    state.last_reviewer_verdict = token
    state.awaiting_developer_correction = True
    state.developer_correction_complete = False
    if state.review_round > state.max_review_rounds:
        return _terminal_blocker(state, "max review_round exceeded")

    ui_changed = _detect_ui_changed(notes)
    state.ui_changed = ui_changed
    if ui_changed and state.requires_agy:
        state.agy_approved = False
        return _coordinator_action(
            state,
            "Reviewer returned REQUEST CHANGES with UI impact (normal verdict). "
            "Route ui_lead (AGY) with explicit TO: header for UX re-review before reviewer.",
        )
    return _coordinator_action(
        state,
        "Reviewer returned REQUEST CHANGES (normal verdict, not a tooling failure). "
        "Route developer with explicit TO: header and a revised read-only analysis/blueprint "
        "addressing reviewer findings. Preserve read-only boundaries.",
    )


def _on_safety_output(state: CoordinatorLoopState, text: str) -> CoordinatorAction:
    token, notes = _parse_safety_verdict(text)
    _append_verdict(state, "safety_gate", token, notes)

    if token == "INVALID":
        return _terminal_blocker(state, notes)
    if token == "MIXED":
        return _terminal_blocker(state, notes)
    if token == "AMBIGUOUS":
        return _terminal_blocker(state, f"ambiguous safety_gate output: {notes}")

    if token == "PASS":
        state.safety_passed = True
        return _coordinator_action(state, "Safety gate passed. Emit FINAL report.")

    # BLOCK
    state.safety_round += 1
    state.safety_passed = False
    if state.safety_round > state.max_safety_rounds:
        return _terminal_blocker(state, f"safety gate blocked: {notes}")
    return _coordinator_action(
        state,
        f"Safety gate blocked ({notes}). Choose correction or BLOCKER.",
    )
