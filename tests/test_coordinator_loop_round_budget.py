"""Coordinator loop round budget and READY_FOR_COORDINATOR handoff tests."""

from __future__ import annotations

import unittest

import config_loader
import workspace_policy as wp
from coordinator_loop import (
    COORDINATOR_ROLE,
    PROMPT_MEMO_READONLY_BUDGET,
    CoordinatorLoopBudget,
    on_coordinator_output,
    on_session_start,
    on_worker_output,
    parse_coordinator_routing,
    resolve_loop_budget_from_session,
)


def _classify_ui(state):
    return on_coordinator_output(state, "CLASSIFY: UI\nui scope")


def _next(state, role, body="work"):
    return on_coordinator_output(state, f"NEXT: {role}\n{body}")


CORRECTION_PASS = """READY_FOR_COORDINATOR

Developer Correction Pass: PaymentModal Analysis

### 1. Current Implementation Structure (What Exists Today)

PaymentModal.tsx (681 lines) is a single-file component with two render branches.
The real data contract between modal and parent is narrow.
Payment data flows from UI input through onConfirm to useCheckout.confirmSale.
"""


INITIAL_REPORT = """READY_FOR_COORDINATOR

REPORT_BEGIN

# Twinpet UI-09-C — PaymentModal Developer Analysis Report

## FINDINGS

### 1. Component Structure
PaymentModal.tsx (681 lines) monolithic component.
"""


class LoopBudgetResolutionTests(unittest.TestCase):
    def test_readonly_analysis_profile_gets_expanded_budget(self):
        profiles = config_loader.get_workspace_profiles(config_loader.load_config())
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only-analysis",
            },
        )
        session = {
            "prompt_body": "PROMPT ID: TEST\n" + ("x" * 2000),
            "workspace_policy": result.policy,
        }
        budget = resolve_loop_budget_from_session(session)
        self.assertEqual(budget, PROMPT_MEMO_READONLY_BUDGET)
        self.assertGreaterEqual(budget.developer, 4)

    def test_simple_session_keeps_conservative_budget(self):
        budget = resolve_loop_budget_from_session({"goal": "simple"})
        self.assertEqual(budget.developer, 2)


class ReadyDeveloperHandoffTests(unittest.TestCase):
    def test_ready_correction_pass_does_not_block_at_default_budget(self):
        state, _ = on_session_start("analysis", max_rounds=2)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", INITIAL_REPORT)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nlayout risks")
        _next(state, "developer")
        on_worker_output(state, "developer", CORRECTION_PASS)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nmore layout")
        _next(state, "developer")
        action = on_worker_output(state, "developer", CORRECTION_PASS)
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertIn("Route ui_lead", action.prompt_context)
        self.assertNotIn("max developer rounds exceeded", action.prompt_context)

    def test_session_69_regression_flow(self):
        state, _ = on_session_start(
            "PaymentModal analysis",
            loop_budget=PROMPT_MEMO_READONLY_BUDGET,
            session_meta={
                "prompt_id": "TWINPET-UI-09-C-READONLY-ANALYSIS-BLUEPRINT-002",
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only",
            },
        )
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", INITIAL_REPORT)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nlayout")
        _next(state, "developer")
        on_worker_output(state, "developer", CORRECTION_PASS)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nblueprint")
        _next(state, "developer")
        action = on_worker_output(state, "developer", CORRECTION_PASS)
        self.assertFalse(action.is_terminal)
        self.assertEqual(state.developer_round, 3)
        self.assertTrue(state.developer_has_substantial_output)

    def test_reviewer_request_changes_then_ready_routes_reviewer_not_developer(self):
        state, _ = on_session_start("analysis", loop_budget=PROMPT_MEMO_READONLY_BUDGET)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", INITIAL_REPORT)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "UX_APPROVED\nok")
        _next(state, "reviewer")
        on_worker_output(state, "reviewer", "REQUEST CHANGES\nfix blueprint")
        self.assertTrue(state.awaiting_developer_correction)
        _next(state, "developer")
        action = on_worker_output(state, "developer", CORRECTION_PASS)
        self.assertIn("Route reviewer", action.prompt_context)
        self.assertTrue(state.developer_correction_complete)
        blocked = on_coordinator_output(state, "NEXT: developer\nagain")
        self.assertEqual(blocked.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)

    def test_max_rounds_without_usable_output_still_blocks(self):
        state, _ = on_session_start("analysis", max_rounds=1)
        _classify_ui(state)
        _next(state, "developer")
        action = on_worker_output(state, "developer", "READY_FOR_COORDINATOR\nok")
        self.assertFalse(action.is_terminal)
        _next(state, "developer")
        action = on_worker_output(state, "developer", "READY_FOR_COORDINATOR\nshort")
        self.assertTrue(action.is_terminal)
        self.assertIn("max developer rounds exceeded", action.prompt_context)
        self.assertIn("substantial_content=no", action.prompt_context)

    def test_max_rounds_diagnostics_include_profile_and_token(self):
        state, _ = on_session_start(
            "analysis",
            max_rounds=1,
            session_meta={
                "prompt_id": "TEST-PROMPT",
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only",
            },
        )
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\ntiny")
        _next(state, "developer")
        action = on_worker_output(state, "developer", "PASS\nno body")
        self.assertTrue(action.is_terminal)
        self.assertIn("prompt_id=TEST-PROMPT", action.prompt_context)
        self.assertIn("workspace_profile=twinpet-ui-09-c-payment-modal-analysis", action.prompt_context)
        self.assertIn("last_first_line_token=PASS", action.prompt_context)

    def test_repeated_request_changes_terminates_safely(self):
        state, _ = on_session_start("analysis", max_rounds=1)
        _classify_non_ui(state)
        _next(state, "reviewer")
        action = on_worker_output(state, "reviewer", "REQUEST CHANGES\nonce")
        self.assertFalse(action.is_terminal)
        _next(state, "reviewer")
        action = on_worker_output(state, "reviewer", "REQUEST CHANGES\nagain")
        self.assertTrue(action.is_terminal)
        self.assertIn("max review_round exceeded", action.prompt_context)


def _classify_non_ui(state):
    return on_coordinator_output(state, "CLASSIFY: NON_UI\nbackend")


class ReadonlyAnalysisSmokeFlow(unittest.TestCase):
    def test_full_smoke_to_reviewer_after_correction(self):
        state, _ = on_session_start("analysis", loop_budget=PROMPT_MEMO_READONLY_BUDGET)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", INITIAL_REPORT)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "UX_APPROVED\nnotes")
        _next(state, "reviewer")
        on_worker_output(state, "reviewer", "REQUEST CHANGES\nblueprint gaps")
        _next(state, "developer")
        dev_action = on_worker_output(state, "developer", CORRECTION_PASS)
        self.assertFalse(dev_action.is_terminal)
        dispatch = on_coordinator_output(
            state,
            "NEXT: reviewer\nRe-check corrected analysis.",
        )
        parsed = parse_coordinator_routing(
            "NEXT: reviewer\nRe-check corrected analysis.",
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(dispatch.target_role, "reviewer")


if __name__ == "__main__":
    unittest.main()
