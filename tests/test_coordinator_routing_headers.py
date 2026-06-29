"""Coordinator explicit TO: routing header contract tests."""

from __future__ import annotations

import unittest

import config_loader
import coordinator_loop as cl
import workspace_policy as wp
from coordinator_loop import (
    COORDINATOR_ROLE,
    on_coordinator_output,
    on_session_start,
    on_worker_output,
)
from session_relay import (
    build_coordinator_loop_prompt,
    build_coordinator_loop_ui_lead_prompt,
    build_relay_prompt,
    build_safety_gate_prompt,
    ensure_explicit_routing_headers,
    has_explicit_to_header,
    resolve_routing_to_target,
)


def _classify_ui(state):
    return on_coordinator_output(state, "CLASSIFY: UI\nui scope")


def _next(state, role, body="do work"):
    return on_coordinator_output(state, f"NEXT: {role}\n{body}")


class RoutingHeaderHelperTests(unittest.TestCase):
    def test_resolve_targets_by_role(self):
        self.assertEqual(resolve_routing_to_target(role="reviewer"), "Codex Reviewer")
        self.assertEqual(resolve_routing_to_target(role="developer"), "Claude Developer")
        self.assertEqual(resolve_routing_to_target(agent_base="codex_coordinator"), "Codex Coordinator")

    def test_ensure_adds_headers_when_missing(self):
        out = ensure_explicit_routing_headers(
            "Continue reviewing PaymentModal.",
            role="reviewer",
            project="twinpet-analysis",
        )
        self.assertTrue(has_explicit_to_header(out))
        self.assertIn("TO: Codex Reviewer", out)
        self.assertIn("ROLE: reviewer", out)
        self.assertIn("Continue reviewing PaymentModal.", out)

    def test_ensure_idempotent_when_to_present(self):
        original = "TO: Codex\n\nBody text"
        self.assertEqual(
            ensure_explicit_routing_headers(original, role="reviewer"),
            original,
        )


class CoordinatorPromptHeaderTests(unittest.TestCase):
    def test_coordinator_loop_prompt_includes_to_codex_coordinator(self):
        prompt = build_coordinator_loop_prompt(
            session_name="Project Read-Only Coordinator Loop",
            goal="analysis",
            task_description="analysis",
            last_role="reviewer",
            last_output_summary="REQUEST CHANGES\nfindings",
            awaiting_role="coordinator",
            developer_round=1,
            ui_round=0,
            review_round=1,
            safety_round=0,
            allowed_tokens=["NEXT: developer", "NEXT: ui_lead"],
            instruction="Reviewer returned REQUEST CHANGES with UI impact.",
            agent_base="codex_coordinator",
            project="twinpet-ui-09-c-read",
        )
        self.assertTrue(has_explicit_to_header(prompt))
        self.assertIn("TO: Codex Coordinator", prompt)
        self.assertIn("HANDOFF ROUTING", prompt)

    def test_relay_prompt_reviewer_has_to_header(self):
        prompt = build_relay_prompt(
            session_name="sess",
            goal="analysis",
            phase_name="review",
            phase_index=2,
            total_phases=4,
            role="reviewer",
            instruction="Review the developer analysis.",
            agent_base="codex_reviewer",
        )
        self.assertIn("TO: Codex Reviewer", prompt)

    def test_relay_prompt_developer_has_to_header(self):
        prompt = build_relay_prompt(
            session_name="sess",
            goal="analysis",
            phase_name="develop",
            phase_index=0,
            total_phases=4,
            role="developer",
            instruction="Produce revised blueprint.",
            agent_base="claude",
        )
        self.assertIn("TO: Claude Developer", prompt)

    def test_ui_lead_prompt_has_to_header(self):
        prompt = build_coordinator_loop_ui_lead_prompt(
            session_name="sess",
            channel="ch",
            goal="ui review",
            phase_name="ui",
            phase_index=1,
            total_phases=4,
            instruction="Review UX blueprint.",
        )
        self.assertIn("TO: AGY UI Lead", prompt)

    def test_safety_gate_prompt_has_to_header(self):
        prompt = build_safety_gate_prompt(
            session_name="sess",
            goal="analysis",
            phase_name="safety",
            content_to_review="artifact",
            agent_base="codexsafe",
        )
        self.assertIn("TO: CodexSafe Safety Gate", prompt)


class CoordinatorHandoffHeaderTests(unittest.TestCase):
    def test_next_reviewer_wraps_body_with_to_header(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer", "READY_FOR_COORDINATOR")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "ui_lead", "review ux")
        on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        action = _next(state, "reviewer", "Review package for consistency.")
        self.assertEqual(action.target_role, "reviewer")
        self.assertIn("TO: Codex Reviewer", action.routing_body)
        self.assertTrue(has_explicit_to_header(action.routing_body))

    def test_next_developer_wraps_body_with_to_header(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = _next(state, "developer", "Begin analysis.")
        self.assertIn("TO: Claude Developer", action.routing_body)

    def test_request_changes_routes_coordinator_without_missing_to_blocker(self):
        state, _ = on_session_start("read-only analysis", max_rounds=5)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        _next(state, "reviewer")
        action = on_worker_output(
            state,
            "reviewer",
            "REQUEST CHANGES\nHigh: blueprint behavior mismatch.",
        )
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertIn("REQUEST CHANGES", action.prompt_context)
        self.assertNotIn("missing_explicit_TO", action.prompt_context)

        allowed = cl.coordinator_allowed_tokens(state)
        coord_prompt = build_coordinator_loop_prompt(
            session_name="Project Read-Only Coordinator Loop",
            goal="read-only analysis",
            task_description=state.task_description,
            last_role="reviewer",
            last_output_summary="REQUEST CHANGES",
            awaiting_role="coordinator",
            developer_round=state.developer_round,
            ui_round=state.ui_round,
            review_round=state.review_round,
            safety_round=state.safety_round,
            allowed_tokens=allowed,
            instruction=action.prompt_context,
        )
        self.assertIn("TO: Codex Coordinator", coord_prompt)
        self.assertNotIn("missing_explicit_TO_Codex_header", coord_prompt)

        dispatch = on_coordinator_output(
            state,
            "NEXT: developer\nRevise blueprint addressing reviewer findings.",
        )
        self.assertEqual(dispatch.target_role, "developer")
        self.assertIn("TO: Claude Developer", dispatch.routing_body)
        self.assertFalse(has_explicit_to_header("Continue reviewing..."))

    def test_bare_follow_up_gets_wrapped_not_emitted_raw(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = _next(state, "developer", "Continue reviewing...")
        self.assertTrue(has_explicit_to_header(action.routing_body))
        self.assertNotEqual(action.routing_body.strip(), "Continue reviewing...")


class ReadOnlyAnalysisMemoTests(unittest.TestCase):
    def test_analysis_profile_coordinator_prompt_preserves_headers(self):
        profiles = config_loader.get_workspace_profiles(config_loader.load_config())
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only-analysis",
            },
        )
        goal = "Read-only PaymentModal analysis"
        prompt = build_coordinator_loop_prompt(
            session_name="Project Read-Only Coordinator Loop",
            goal=goal,
            task_description=goal,
            last_role="reviewer",
            last_output_summary="REQUEST CHANGES",
            awaiting_role="coordinator",
            developer_round=1,
            ui_round=0,
            review_round=1,
            safety_round=0,
            allowed_tokens=["NEXT: developer", "NEXT: ui_lead"],
            instruction=(
                "Reviewer returned REQUEST CHANGES (normal verdict). "
                "Route developer with revised read-only analysis."
            ),
            project="twinpet-ui-09-c-analysis",
        )
        self.assertIn("TO: Codex Coordinator", prompt)
        self.assertIn("read-only", prompt.lower())


if __name__ == "__main__":
    unittest.main()
