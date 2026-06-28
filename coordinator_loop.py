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

_ROUTING_TOKEN_RE = re.compile(
    r"^(CLASSIFY: (?:UI|NON_UI)|NEXT: \w+|FINAL:|BLOCKER:)",
)
_SAFETY_VERDICT_LINE_RE = re.compile(
    r"^(PASS(?:\s|$)|PASS WITH NOTES|BLOCK:)",
    re.IGNORECASE,
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
    max_total_transitions: int = DEFAULT_MAX_TOTAL_TRANSITIONS
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
            max_rounds=int(data.get("max_rounds", DEFAULT_MAX_ROUNDS)),
            max_total_transitions=int(
                data.get("max_total_transitions", DEFAULT_MAX_TOTAL_TRANSITIONS)),
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
            "max_total_transitions": self.max_total_transitions,
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
    return CoordinatorAction(
        target_role=role,
        prompt_context=prompt,
        routing_body=body,
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


def on_session_start(task_description: str,
                     max_rounds: int = DEFAULT_MAX_ROUNDS) -> tuple[CoordinatorLoopState, CoordinatorAction]:
    """Initialize loop state and trigger coordinator intake first."""
    state = CoordinatorLoopState(
        task_description=task_description,
        max_rounds=max_rounds,
        phase=CoordinatorPhase.AWAIT_COORDINATOR,
        awaiting_role=COORDINATOR_ROLE,
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


def _parse_developer_verdict(text: str) -> tuple[str, str]:
    first = _first_non_empty_line(text)
    if first == "READY_FOR_COORDINATOR":
        return first, _body_after_first_line(text)
    return "AMBIGUOUS", first or "empty developer output"


def _parse_ui_lead_verdict(text: str) -> tuple[str, str]:
    first = _first_non_empty_line(text)
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


def on_worker_output(state: CoordinatorLoopState, role: str, text: str) -> CoordinatorAction:
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
        return _on_developer_output(state, text)
    if role == "ui_lead":
        return _on_ui_lead_output(state, text)
    if role == "reviewer":
        return _on_reviewer_output(state, text)
    return _on_safety_output(state, text)


def _on_developer_output(state: CoordinatorLoopState, text: str) -> CoordinatorAction:
    token, notes = _parse_developer_verdict(text)
    _append_verdict(state, "developer", token, notes)

    if token == "AMBIGUOUS":
        return _terminal_blocker(state, f"ambiguous developer output: {notes}")

    if token == "BLOCKED":
        return _terminal_blocker(state, f"developer blocked: {notes}")

    state.developer_round += 1
    if state.developer_round > state.max_rounds:
        return _terminal_blocker(state, "max developer rounds exceeded")

    return _coordinator_action(
        state,
        f"Developer reported {token}. Decide next routing.",
    )


def _on_ui_lead_output(state: CoordinatorLoopState, text: str) -> CoordinatorAction:
    token, notes = _parse_ui_lead_verdict(text)
    _append_verdict(state, "ui_lead", token, notes)

    if token == "INVALID":
        return _terminal_blocker(state, notes)
    if token == "AMBIGUOUS":
        return _terminal_blocker(state, f"ambiguous ui_lead output: {notes}")
    if token == "BLOCKED":
        return _terminal_blocker(state, f"ui_lead blocked: {notes}")

    if token == "UX_APPROVED":
        state.agy_approved = True
        state.ui_changed = False
        return _coordinator_action(state, "AGY approved UX. Route to reviewer.")

    # REQUEST UX CHANGES
    state.ui_round += 1
    state.agy_approved = False
    if state.ui_round > state.max_rounds:
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
        return _coordinator_action(state, "Reviewer passed. Route to safety_gate.")

    # REQUEST CHANGES
    state.review_round += 1
    state.reviewer_passed = False
    if state.review_round > state.max_rounds:
        return _terminal_blocker(state, "max review_round exceeded")

    ui_changed = _detect_ui_changed(notes)
    state.ui_changed = ui_changed
    if ui_changed and state.requires_agy:
        state.agy_approved = False
        return _coordinator_action(
            state,
            "Reviewer requested changes with UI impact. Re-enter AGY before reviewer.",
        )
    return _coordinator_action(
        state,
        "Reviewer requested engineering changes. Route developer correction.",
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
    if state.safety_round > state.max_rounds:
        return _terminal_blocker(state, f"safety gate blocked: {notes}")
    return _coordinator_action(
        state,
        f"Safety gate blocked ({notes}). Choose correction or BLOCKER.",
    )
