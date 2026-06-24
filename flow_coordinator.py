"""Sandbox flow coordinator — pure state machine for Developer → AGY → Codex loop.

Drives the sandbox orchestration flow without any I/O. All decisions are
deterministic from (current state + verdict) → next state. The caller
(session engine or test harness) is responsible for triggering agents and
feeding verdicts back.

This module is sandbox-only. It does NOT alter the relay-eligible allowlist,
does NOT add AGY or Claude to relay, and does NOT bypass any existing safety
invariant.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    INTAKE = "intake"
    DEV_WORK = "dev_work"
    AGY_REVIEW = "agy_review"
    CODEX_REVIEW = "codex_review"
    CLOSURE = "closure"
    HALTED = "halted"


@dataclass
class FlowState:
    """Serialisable state of a sandbox orchestration run."""
    phase: Phase = Phase.INTAKE
    ux_loops: int = 0
    eng_loops: int = 0
    total_steps: int = 0
    task_description: str = ""
    report_path: str = ""
    verdicts: list[dict] = field(default_factory=list)
    halt_reason: str = ""
    closure_summary: dict = field(default_factory=dict)
    requires_agy_rereview: bool = False

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "ux_loops": self.ux_loops,
            "eng_loops": self.eng_loops,
            "total_steps": self.total_steps,
            "task_description": self.task_description,
            "report_path": self.report_path,
            "verdicts": list(self.verdicts),
            "halt_reason": self.halt_reason,
            "closure_summary": dict(self.closure_summary),
            "requires_agy_rereview": self.requires_agy_rereview,
        }

    @staticmethod
    def from_dict(d: dict) -> "FlowState":
        phase_str = d.get("phase", "intake")
        try:
            phase = Phase(phase_str)
        except ValueError:
            phase = Phase.HALTED
        return FlowState(
            phase=phase,
            ux_loops=d.get("ux_loops", 0),
            eng_loops=d.get("eng_loops", 0),
            total_steps=d.get("total_steps", 0),
            task_description=d.get("task_description", ""),
            report_path=d.get("report_path", ""),
            verdicts=list(d.get("verdicts", [])),
            halt_reason=d.get("halt_reason", ""),
            closure_summary=dict(d.get("closure_summary", {})),
            requires_agy_rereview=bool(d.get("requires_agy_rereview", False)),
        )


MAX_UX_LOOPS = 2
MAX_ENG_LOOPS = 2
MAX_TOTAL_STEPS = 12

# Paths that must never be touched — production repos.
_BLOCKED_PATH_PATTERNS = (
    re.compile(r"twinpet", re.IGNORECASE),
    re.compile(r"(?i)\\twinpet-pos"),
    re.compile(r"(?i)/twinpet-pos"),
)

# File types that indicate a UI/UX change (triggers AGY re-review after Codex fail).
_UI_CHANGE_EXTENSIONS = frozenset({
    ".tsx", ".jsx", ".css", ".scss", ".less", ".html", ".svg",
})
_UI_CHANGE_KEYWORDS = frozenset({
    "layout", "modal", "button", "responsive", "animation", "transition",
    "font", "color", "spacing", "padding", "margin", "grid", "flex",
    "breakpoint", "media query", "accessibility", "aria", "ux", "ui",
    "interaction", "hover", "focus", "z-index", "opacity",
})


def _is_blocked_path(text: str) -> bool:
    """True if text references a production/blocked path."""
    return any(p.search(text) for p in _BLOCKED_PATH_PATTERNS)


def _is_ui_change(description: str) -> bool:
    """True if the description suggests a UI/UX change that needs AGY re-review."""
    low = description.lower()
    if any(ext in low for ext in _UI_CHANGE_EXTENSIONS):
        return True
    return any(kw in low for kw in _UI_CHANGE_KEYWORDS)


@dataclass
class Action:
    """What the caller should do next."""
    target_role: str  # "developer", "agy", "codex", "closure", "halted"
    prompt_context: str = ""
    is_terminal: bool = False


def intake(state: FlowState, task_description: str) -> Action:
    """Accept a task and transition to developer work, or halt on blocked paths."""
    state.task_description = task_description
    state.total_steps += 1

    if _is_blocked_path(task_description):
        state.phase = Phase.HALTED
        state.halt_reason = "task references a blocked production path"
        return Action(
            target_role="halted",
            prompt_context=state.halt_reason,
            is_terminal=True,
        )

    state.phase = Phase.DEV_WORK
    return Action(
        target_role="developer",
        prompt_context=f"Implement: {task_description}",
    )


def on_developer_verdict(state: FlowState, token: str, report_path: str = "",
                         notes: str = "") -> Action:
    """Process a developer's status token and route to AGY or halt."""
    state.total_steps += 1
    state.verdicts.append({
        "role": "developer", "token": token, "time": time.time(),
    })

    if state.total_steps > MAX_TOTAL_STEPS:
        return _halt(state, "max total steps exceeded")

    if token == "BLOCKED":
        return _halt(state, f"developer blocked: {notes}")

    if token not in ("READY_FOR_AGY_REVIEW", "READY_FOR_CODEX_REVIEW",
                     "READY_FOR_REVIEW_PACKAGE"):
        return _halt(state, f"ambiguous developer token: {token}")

    state.report_path = report_path or state.report_path

    if token == "READY_FOR_CODEX_REVIEW" or token == "READY_FOR_REVIEW_PACKAGE":
        # Developer says go straight to Codex (allowed after AGY already passed
        # AND no pending AGY re-review requirement from a UI/UX Codex fail)
        if _has_agy_pass(state) and not state.requires_agy_rereview:
            state.phase = Phase.CODEX_REVIEW
            return Action(
                target_role="codex",
                prompt_context=f"Review package: {state.report_path}",
            )
        # No prior AGY pass or AGY re-review required — must route through AGY
        state.phase = Phase.AGY_REVIEW
        return Action(
            target_role="agy",
            prompt_context=f"UI/UX review: {state.report_path}",
        )

    # READY_FOR_AGY_REVIEW
    state.phase = Phase.AGY_REVIEW
    return Action(
        target_role="agy",
        prompt_context=f"UI/UX review: {state.report_path}",
    )


def on_agy_verdict(state: FlowState, token: str, notes: str = "") -> Action:
    """Process AGY's verdict and route to Codex or back to Developer."""
    state.total_steps += 1
    state.verdicts.append({
        "role": "agy", "token": token, "time": time.time(),
    })

    if state.total_steps > MAX_TOTAL_STEPS:
        return _halt(state, "max total steps exceeded")

    if token == "BLOCKED":
        return _halt(state, f"AGY blocked: {notes}")

    if token == "AMBIGUOUS":
        return _halt(state, f"ambiguous AGY output: {notes}")

    if token in ("PASS", "PASS WITH NOTES"):
        state.requires_agy_rereview = False
        state.phase = Phase.CODEX_REVIEW
        return Action(
            target_role="codex",
            prompt_context=f"Review package: {state.report_path}",
        )

    if token == "REQUEST UX CHANGES":
        state.ux_loops += 1
        if state.ux_loops > MAX_UX_LOOPS:
            return _halt(state, f"max UX loops ({MAX_UX_LOOPS}) exceeded")
        state.phase = Phase.DEV_WORK
        return Action(
            target_role="developer",
            prompt_context=f"AGY requested UX changes: {notes}",
        )

    return _halt(state, f"unrecognised AGY token: {token}")


def on_codex_verdict(state: FlowState, token: str, notes: str = "",
                     fix_description: str = "") -> Action:
    """Process Codex's verdict and close or route back to Developer."""
    state.total_steps += 1
    state.verdicts.append({
        "role": "codex", "token": token, "time": time.time(),
    })

    if state.total_steps > MAX_TOTAL_STEPS:
        return _halt(state, "max total steps exceeded")

    if token == "BLOCKED":
        return _halt(state, f"Codex blocked: {notes}")

    if token == "AMBIGUOUS":
        return _halt(state, f"ambiguous Codex output: {notes}")

    if token in ("PASS", "PASS WITH NOTES"):
        return _close(state, notes)

    if token == "REQUEST CHANGES":
        state.eng_loops += 1
        if state.eng_loops > MAX_ENG_LOOPS:
            return _halt(state, f"max engineering loops ({MAX_ENG_LOOPS}) exceeded")
        state.phase = Phase.DEV_WORK

        # Approved policy: if fix touches UI/UX, must re-run AGY before Codex.
        if _is_ui_change(fix_description or notes):
            state.requires_agy_rereview = True
            return Action(
                target_role="developer",
                prompt_context=(
                    f"Codex requested changes (UI/UX touched — AGY re-review "
                    f"required after fix): {notes}"
                ),
            )
        return Action(
            target_role="developer",
            prompt_context=f"Codex requested changes (engineering only): {notes}",
        )

    return _halt(state, f"unrecognised Codex token: {token}")


def _has_agy_pass(state: FlowState) -> bool:
    """True if AGY has already passed at least once in this run."""
    return any(
        v["role"] == "agy" and v["token"] in ("PASS", "PASS WITH NOTES")
        for v in state.verdicts
    )


def _halt(state: FlowState, reason: str) -> Action:
    state.phase = Phase.HALTED
    state.halt_reason = reason
    return Action(target_role="halted", prompt_context=reason, is_terminal=True)


def _close(state: FlowState, final_notes: str = "") -> Action:
    state.phase = Phase.CLOSURE
    state.closure_summary = {
        "task": state.task_description,
        "report_path": state.report_path,
        "ux_loops": state.ux_loops,
        "eng_loops": state.eng_loops,
        "total_steps": state.total_steps,
        "verdicts": list(state.verdicts),
        "final_notes": final_notes,
        "closed_at": time.time(),
    }
    return Action(target_role="closure", prompt_context="all reviews passed",
                  is_terminal=True)
