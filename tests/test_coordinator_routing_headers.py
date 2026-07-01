"""Coordinator explicit TO: routing header contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
    ROLE_DOC_ALIASES,
    build_coordinator_loop_prompt,
    build_coordinator_loop_ui_lead_prompt,
    build_relay_prompt,
    build_role_context_block,
    build_safety_gate_prompt,
    ensure_explicit_routing_headers,
    has_explicit_to_header,
    load_bounded_role_doc,
    resolve_routing_to_target,
)


def _classify_ui(state):
    return on_coordinator_output(state, "CLASSIFY: UI\nui scope")


def _next(state, role, body="do work"):
    return on_coordinator_output(state, f"NEXT: {role}\n{body}")


class RoutingHeaderHelperTests(unittest.TestCase):
    def test_resolve_targets_by_role_first(self):
        self.assertEqual(resolve_routing_to_target(role="reviewer"), "Reviewer")
        self.assertEqual(resolve_routing_to_target(role="developer"), "Developer")
        self.assertEqual(resolve_routing_to_target(role="ui_lead"), "UI Lead")
        self.assertEqual(resolve_routing_to_target(role="coordinator"), "Coordinator")
        self.assertEqual(resolve_routing_to_target(role="safety_gate"), "Safety Gate")

    def test_role_wins_over_agent_base(self):
        self.assertEqual(
            resolve_routing_to_target(role="reviewer", agent_base="claude"),
            "Reviewer",
        )
        self.assertEqual(
            resolve_routing_to_target(role="ui_lead", agent_base="codex"),
            "UI Lead",
        )

    def test_agent_base_fallback_when_no_role(self):
        self.assertEqual(
            resolve_routing_to_target(agent_base="codex_coordinator"),
            "Coordinator",
        )

    def test_ensure_adds_role_first_headers_when_missing(self):
        out = ensure_explicit_routing_headers(
            "Continue reviewing PaymentModal.",
            role="reviewer",
            agent_base="codex_reviewer",
            project="twinpet-analysis",
        )
        self.assertTrue(has_explicit_to_header(out))
        self.assertIn("TO: Reviewer", out)
        self.assertNotIn("TO: Codex Reviewer", out)
        self.assertIn("ROLE: reviewer", out)
        self.assertIn("ROLE_ID: reviewer", out)
        self.assertIn(f"ROLE_DOC: {ROLE_DOC_ALIASES['reviewer']}", out)
        self.assertIn("assigned_agent: codex_reviewer", out)
        self.assertIn("Continue reviewing PaymentModal.", out)

    def test_ensure_normalizes_stale_brand_mixed_to(self):
        original = "TO: Codex Reviewer\n\nBody text"
        out = ensure_explicit_routing_headers(original, role="reviewer")
        self.assertIn("TO: Reviewer", out)
        self.assertNotIn("TO: Codex Reviewer", out)
        self.assertIn("Body text", out)

    def test_ensure_idempotent_pure_to_unchanged(self):
        original = "TO: Codex\n\nBody text"
        self.assertEqual(
            ensure_explicit_routing_headers(original, role=""),
            original,
        )


class RoleDocInjectionTests(unittest.TestCase):
    def test_developer_prompt_embeds_bounded_role_context(self):
        out = ensure_explicit_routing_headers(
            "Implement PaymentModal fix.",
            role="developer",
            agent_base="claude",
        )
        self.assertIn("ROLE_CONTEXT:", out)
        self.assertIn(f"- source: {ROLE_DOC_ALIASES['developer']}", out)
        self.assertIn("- content:", out)
        self.assertIn("# Developer", out)
        self.assertNotIn("Load docs/ai-roles/developer.md before acting", out)

    def test_relay_prompt_includes_role_context_server_side(self):
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
        self.assertIn("ROLE_CONTEXT:", prompt)
        self.assertIn("# Developer", prompt)
        self.assertIn("TO: Developer", prompt)
        self.assertNotIn("TO: Claude Developer", prompt)
        self.assertIn("assigned_agent: claude", prompt)

    def test_ui_lead_prompt_includes_ux_lead_role_context(self):
        prompt = build_coordinator_loop_ui_lead_prompt(
            session_name="sess",
            channel="ch",
            goal="ui review",
            phase_name="ui",
            phase_index=1,
            total_phases=4,
            instruction="Review UX blueprint.",
        )
        self.assertIn("ROLE_CONTEXT:", prompt)
        self.assertIn("# UX Lead", prompt)
        self.assertIn("TO: UI Lead", prompt)
        self.assertIn("assigned_agent: agy", prompt)

    def test_missing_role_doc_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = build_role_context_block("developer", repo_root=Path(tmp))
        self.assertIn("ROLE_CONTEXT:", block)
        self.assertIn("- status: ROLE_DOC_MISSING", block)
        self.assertIn(f"- source: {ROLE_DOC_ALIASES['developer']}", block)

    def test_long_role_doc_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = Path(tmp) / "docs" / "ai-roles"
            doc_path.mkdir(parents=True)
            long_body = "X" * 500
            (doc_path / "developer.md").write_text(long_body, encoding="utf-8")
            loaded = load_bounded_role_doc(
                "developer",
                max_chars=100,
                repo_root=Path(tmp),
            )
            self.assertEqual(loaded.status, "truncated")
            self.assertIn("[role doc truncated, 500 chars total]", loaded.content)
            block = build_role_context_block(
                "developer",
                max_chars=100,
                repo_root=Path(tmp),
            )
            self.assertIn("- status: bounded (truncated)", block)

    def test_stale_to_header_gets_role_context_injected(self):
        original = "TO: Codex Reviewer\n\nBody text"
        out = ensure_explicit_routing_headers(original, role="reviewer")
        self.assertIn("TO: Reviewer", out)
        self.assertIn("ROLE_CONTEXT:", out)
        self.assertIn("# Reviewer", out)
        self.assertIn("Body text", out)

    def test_role_context_not_duplicated(self):
        out = ensure_explicit_routing_headers("Task body.", role="reviewer")
        self.assertEqual(out.count("ROLE_CONTEXT:"), 1)


class CoordinatorPromptHeaderTests(unittest.TestCase):
    def test_coordinator_loop_prompt_includes_to_coordinator(self):
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
        self.assertIn("TO: Coordinator", prompt)
        self.assertNotIn("TO: Codex Coordinator", prompt)
        self.assertIn("ROLE_ID: coordinator", prompt)
        self.assertIn("assigned_agent: codex_coordinator", prompt)
        self.assertIn("HANDOFF ROUTING", prompt)

    def test_relay_prompt_reviewer_has_role_first_to_header(self):
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
        self.assertIn("TO: Reviewer", prompt)
        self.assertNotIn("TO: Codex Reviewer", prompt)
        self.assertIn("assigned_agent: codex_reviewer", prompt)

    def test_relay_prompt_developer_has_role_first_to_header(self):
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
        self.assertIn("TO: Developer", prompt)
        self.assertNotIn("TO: Claude Developer", prompt)
        self.assertIn("assigned_agent: claude", prompt)

    def test_ui_lead_prompt_has_role_first_to_header(self):
        prompt = build_coordinator_loop_ui_lead_prompt(
            session_name="sess",
            channel="ch",
            goal="ui review",
            phase_name="ui",
            phase_index=1,
            total_phases=4,
            instruction="Review UX blueprint.",
        )
        self.assertIn("TO: UI Lead", prompt)
        self.assertNotIn("TO: AGY UI Lead", prompt)
        self.assertIn("ROLE_ID: ui_lead", prompt)
        self.assertIn(f"ROLE_DOC: {ROLE_DOC_ALIASES['ui_lead']}", prompt)
        self.assertIn("assigned_agent: agy", prompt)
        self.assertIn("transport: store_exec", prompt)

    def test_safety_gate_prompt_has_role_first_to_header(self):
        prompt = build_safety_gate_prompt(
            session_name="sess",
            goal="analysis",
            phase_name="safety",
            content_to_review="artifact",
            agent_base="codexsafe",
        )
        self.assertIn("TO: Safety Gate", prompt)
        self.assertNotIn("TO: CodexSafe Safety Gate", prompt)
        self.assertIn("assigned_agent: codexsafe", prompt)


class CoordinatorHandoffHeaderTests(unittest.TestCase):
    def test_next_reviewer_wraps_body_with_role_first_to_header(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer", "READY_FOR_COORDINATOR")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "ui_lead", "review ux")
        on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        action = _next(state, "reviewer", "Review package for consistency.")
        self.assertEqual(action.target_role, "reviewer")
        self.assertIn("TO: Reviewer", action.routing_body)
        self.assertNotIn("TO: Codex Reviewer", action.routing_body)
        self.assertTrue(has_explicit_to_header(action.routing_body))

    def test_next_developer_wraps_body_with_role_first_to_header(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = _next(state, "developer", "Begin analysis.")
        self.assertIn("TO: Developer", action.routing_body)
        self.assertNotIn("TO: Claude Developer", action.routing_body)

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
        self.assertIn("TO: Coordinator", coord_prompt)
        self.assertNotIn("TO: Codex Coordinator", coord_prompt)
        self.assertNotIn("missing_explicit_TO_Codex_header", coord_prompt)

        dispatch = on_coordinator_output(
            state,
            "NEXT: developer\nRevise blueprint addressing reviewer findings.",
        )
        self.assertEqual(dispatch.target_role, "developer")
        self.assertIn("TO: Developer", dispatch.routing_body)
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
        self.assertIn("TO: Coordinator", prompt)
        self.assertNotIn("TO: Codex Coordinator", prompt)
        self.assertIn("read-only", prompt.lower())


if __name__ == "__main__":
    unittest.main()
