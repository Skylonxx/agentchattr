"""Unit tests for coordinator_loop pure state machine (Phase 1)."""

import copy
import tempfile
import unittest
from pathlib import Path

from coordinator_loop import (
    COORDINATOR_ROLE,
    LATE_UI_SIGNAL_TOKEN,
    WORKER_ROLES,
    CoordinatorPhase,
    coordinator_allowed_tokens,
    on_coordinator_output,
    on_session_start,
    on_worker_output,
    parse_coordinator_routing,
)


def _classify_ui(state):
    return on_coordinator_output(state, "CLASSIFY: UI\nUI task scope.")


def _classify_non_ui(state):
    return on_coordinator_output(state, "CLASSIFY: NON_UI\nBackend-only scope.")


def _next(state, role, body="Do the work."):
    return on_coordinator_output(state, f"NEXT: {role}\n{body}")


def _snapshot_flags(state):
    return (
        state.agy_approved,
        state.reviewer_passed,
        state.safety_passed,
        state.ui_round,
        state.review_round,
        len(state.verdict_log),
    )


class CoordinatorLoopStartTests(unittest.TestCase):
    def test_start_always_triggers_coordinator_first(self):
        state, action = on_session_start("Build widget")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertFalse(action.is_terminal)
        self.assertEqual(state.awaiting_role, COORDINATOR_ROLE)


class CoordinatorLoopWorkerReturnTests(unittest.TestCase):
    def test_developer_output_always_returns_to_coordinator(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer")
        action = on_worker_output(state, "developer", "READY_FOR_COORDINATOR\nDone.")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertFalse(action.is_terminal)

    def test_worker_output_handlers_never_emit_worker_target_directly(self):
        flows = [
            ("developer", lambda s: (_classify_ui(s), _next(s, "developer"))),
            ("ui_lead", lambda s: (_classify_ui(s), _next(s, "ui_lead"))),
            ("reviewer", lambda s: (_classify_non_ui(s), _next(s, "reviewer"))),
            ("safety_gate", lambda s: (_classify_non_ui(s), _next(s, "safety_gate"))),
        ]
        outputs = {
            "developer": "READY_FOR_COORDINATOR\nok",
            "ui_lead": "UX_APPROVED\nok",
            "reviewer": "PASS\nok",
            "safety_gate": "PASS\nok",
        }
        for role, setup in flows:
            state, _ = on_session_start("task")
            state.reviewer_passed = role == "safety_gate"
            setup(state)
            action = on_worker_output(state, role, outputs[role])
            self.assertEqual(action.target_role, COORDINATOR_ROLE, msg=role)
            self.assertNotIn(action.target_role, WORKER_ROLES)


class CoordinatorLoopOutOfTurnWorkerTests(unittest.TestCase):
    def test_ui_lead_output_rejected_when_awaiting_coordinator(self):
        state, _ = on_session_start("task")
        before = _snapshot_flags(state)
        action = on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        self.assertTrue(action.is_terminal)
        self.assertIn("out-of-turn", action.prompt_context)
        self.assertEqual(_snapshot_flags(state)[:3], before[:3])

    def test_reviewer_output_rejected_when_awaiting_developer(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        _next(state, "developer")
        before = _snapshot_flags(state)
        action = on_worker_output(state, "reviewer", "PASS\n")
        self.assertTrue(action.is_terminal)
        self.assertIn("out-of-turn", action.prompt_context)
        self.assertEqual(_snapshot_flags(state), before)

    def test_safety_gate_output_rejected_when_awaiting_reviewer(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        _next(state, "reviewer")
        before = _snapshot_flags(state)
        action = on_worker_output(state, "safety_gate", "PASS\n")
        self.assertTrue(action.is_terminal)
        self.assertIn("out-of-turn", action.prompt_context)
        self.assertFalse(state.safety_passed)

    def test_rejected_out_of_turn_output_does_not_mutate_state(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer")
        before = copy.deepcopy(_snapshot_flags(state))
        on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        self.assertEqual(_snapshot_flags(state), before)


class CoordinatorLoopCoordinatorTurnTests(unittest.TestCase):
    def test_coordinator_output_rejected_when_awaiting_developer(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer")
        phase_before = state.phase
        action = on_coordinator_output(state, "NEXT: ui_lead\nskip")
        self.assertEqual(action.target_role, "developer")
        self.assertEqual(state.awaiting_role, "developer")
        self.assertEqual(state.phase, phase_before)

    def test_coordinator_output_rejected_when_awaiting_ui_lead(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "ui_lead")
        action = on_coordinator_output(state, "NEXT: reviewer\nskip")
        self.assertEqual(action.target_role, "ui_lead")
        self.assertEqual(state.awaiting_role, "ui_lead")
        self.assertEqual(state.phase, CoordinatorPhase.AWAIT_UI_LEAD)

    def test_coordinator_output_rejected_when_awaiting_reviewer(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        _next(state, "reviewer")
        action = on_coordinator_output(state, "NEXT: safety_gate\nskip")
        self.assertEqual(action.target_role, "reviewer")
        self.assertEqual(state.awaiting_role, "reviewer")

    def test_coordinator_output_rejected_when_awaiting_safety_gate(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_coordinator_output(state, "FINAL:\nearly")
        self.assertEqual(action.target_role, "safety_gate")
        self.assertEqual(state.awaiting_role, "safety_gate")
        self.assertFalse(state.safety_passed)


class CoordinatorLoopCoordinatorSkipRegressionTests(unittest.TestCase):
    def test_bad_coordinator_then_second_next_cannot_skip_pending_developer(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "developer")
        on_coordinator_output(state, "NEXT: ui_lead\nskip dev")
        self.assertEqual(state.awaiting_role, "developer")
        second = on_coordinator_output(state, "NEXT: ui_lead\nskip again")
        self.assertEqual(second.target_role, "developer")
        self.assertEqual(state.awaiting_role, "developer")

    def test_rejected_coordinator_output_does_not_mutate_approval_flags(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        state.agy_approved = True
        state.reviewer_passed = True
        state.safety_passed = True
        _next(state, "developer")
        before = (state.agy_approved, state.reviewer_passed, state.safety_passed)
        on_coordinator_output(state, "NEXT: safety_gate\nskip")
        self.assertEqual(
            (state.agy_approved, state.reviewer_passed, state.safety_passed),
            before,
        )


class CoordinatorLoopClassificationTests(unittest.TestCase):
    def test_next_developer_before_classification_rejected(self):
        state, _ = on_session_start("task")
        action = on_coordinator_output(state, "NEXT: developer\nearly")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)
        self.assertFalse(state.classified)

    def test_after_classify_ui_next_developer_accepted(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = _next(state, "developer", "Implement UI.")
        self.assertEqual(action.target_role, "developer")
        self.assertIn("Implement UI.", action.routing_body)
        self.assertIn("TO: Claude Developer", action.routing_body)

    def test_after_classify_non_ui_next_developer_accepted(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        action = _next(state, "developer")
        self.assertEqual(action.target_role, "developer")


class CoordinatorLoopUiFlowTests(unittest.TestCase):
    def test_ui_flow_happy_path_coordinator_between_every_step(self):
        state, action = on_session_start("UI polish")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)

        self.assertEqual(_classify_ui(state).target_role, COORDINATOR_ROLE)
        self.assertEqual(_next(state, "developer").target_role, "developer")
        self.assertEqual(
            on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n").target_role,
            COORDINATOR_ROLE,
        )
        self.assertEqual(_next(state, "ui_lead").target_role, "ui_lead")
        self.assertEqual(
            on_worker_output(state, "ui_lead", "UX_APPROVED\n").target_role,
            COORDINATOR_ROLE,
        )
        self.assertTrue(state.agy_approved)
        self.assertEqual(_next(state, "reviewer").target_role, "reviewer")
        self.assertEqual(
            on_worker_output(state, "reviewer", "PASS\n").target_role,
            COORDINATOR_ROLE,
        )
        self.assertEqual(_next(state, "safety_gate").target_role, "safety_gate")
        self.assertEqual(
            on_worker_output(state, "safety_gate", "PASS\n").target_role,
            COORDINATOR_ROLE,
        )
        final = on_coordinator_output(state, "FINAL:\nAll good.")
        self.assertTrue(final.is_terminal)
        self.assertEqual(final.terminal_kind, "final")


class CoordinatorLoopNonUiFlowTests(unittest.TestCase):
    def test_non_ui_flow_skips_agy_entirely(self):
        state, _ = on_session_start("backend fix")
        _classify_non_ui(state)
        self.assertFalse(state.requires_agy)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        bad = on_coordinator_output(state, "NEXT: ui_lead\nnope")
        self.assertEqual(bad.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)
        good = _next(state, "reviewer")
        self.assertEqual(good.target_role, "reviewer")


class CoordinatorLoopAgyLoopTests(unittest.TestCase):
    def test_agy_fail_increments_ui_round_and_loops_through_coordinator(self):
        state, _ = on_session_start("ui task", max_rounds=2)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "ui_lead")
        action = on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nfix spacing")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertEqual(state.ui_round, 1)
        self.assertFalse(state.agy_approved)

    def test_agy_max_rounds_emits_blocker(self):
        state, _ = on_session_start("ui task", max_rounds=1)
        _classify_ui(state)
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nfirst")
        _next(state, "ui_lead")
        action = on_worker_output(state, "ui_lead", "REQUEST UX CHANGES\nagain")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")
        self.assertIn("ui_round", action.prompt_context)


class CoordinatorLoopReviewerTests(unittest.TestCase):
    def test_reviewer_pass_with_notes_accepted(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        _next(state, "reviewer")
        action = on_worker_output(state, "reviewer", "PASS WITH NOTES\nminor nits")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertTrue(state.reviewer_passed)

    def test_reviewer_fail_increments_review_round(self):
        state, _ = on_session_start("task", max_rounds=3)
        _classify_non_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "reviewer")
        action = on_worker_output(state, "reviewer", "REQUEST CHANGES\nfix tests")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertEqual(state.review_round, 1)
        self.assertFalse(state.reviewer_passed)

    def test_reviewer_fail_with_ui_changed_reenters_agy_before_reviewer(self):
        state, _ = on_session_start("task", max_rounds=3)
        _classify_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        _next(state, "ui_lead")
        on_worker_output(state, "ui_lead", "UX_APPROVED\n")
        _next(state, "reviewer")
        action = on_worker_output(
            state,
            "reviewer",
            "REQUEST CHANGES\nupdate dashboard layout.tsx spacing",
        )
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertTrue(state.ui_changed)
        self.assertFalse(state.agy_approved)
        self.assertIn("AGY", action.prompt_context)


class CoordinatorLoopSafetyTests(unittest.TestCase):
    def test_safety_pass_routes_back_to_coordinator_final_path(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_worker_output(state, "safety_gate", "PASS\n")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertTrue(state.safety_passed)
        self.assertIn("FINAL", action.prompt_context)

    def test_safety_block_routes_back_to_coordinator_for_correction_or_blocker(self):
        state, _ = on_session_start("task", max_rounds=2)
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_worker_output(state, "safety_gate", "BLOCK: twinpet path referenced")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertFalse(state.safety_passed)
        self.assertEqual(state.safety_round, 1)

    def test_safety_pass_with_notes_is_invalid(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_worker_output(state, "safety_gate", "PASS WITH NOTES\nsoft pass")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")

    def test_safety_pass_then_block_rejected(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_worker_output(state, "safety_gate", "PASS\nBLOCK: no")
        self.assertTrue(action.is_terminal)
        self.assertFalse(state.safety_passed)

    def test_safety_block_then_pass_rejected(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")
        action = on_worker_output(state, "safety_gate", "BLOCK: no\nPASS")
        self.assertTrue(action.is_terminal)
        self.assertFalse(state.safety_passed)

    def test_safety_block_requires_non_empty_reason(self):
        state, _ = on_session_start("task", max_rounds=2)
        _classify_non_ui(state)
        state.reviewer_passed = True
        _next(state, "safety_gate")

        for empty_block in ("BLOCK:", "BLOCK:   "):
            with self.subTest(empty_block=empty_block):
                s, _ = on_session_start("task", max_rounds=2)
                _classify_non_ui(s)
                s.reviewer_passed = True
                _next(s, "safety_gate")
                action = on_worker_output(s, "safety_gate", empty_block)
                self.assertTrue(action.is_terminal)
                self.assertEqual(action.terminal_kind, "blocker")
                self.assertFalse(s.safety_passed)
                self.assertIn("non-empty reason", action.prompt_context)

        action = on_worker_output(state, "safety_gate", "BLOCK: real reason")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertFalse(state.safety_passed)
        self.assertEqual(state.safety_round, 1)
        self.assertIn("real reason", action.prompt_context)


class CoordinatorLoopRoutingParserTests(unittest.TestCase):
    def test_classify_ui_vs_non_ui(self):
        ui = parse_coordinator_routing("CLASSIFY: UI\nscope")
        non = parse_coordinator_routing("CLASSIFY: NON_UI\nscope")
        self.assertTrue(ui.ok and ui.kind == "classify_ui")
        self.assertTrue(non.ok and non.kind == "classify_non_ui")

    def test_first_non_empty_line_with_leading_blank_lines(self):
        parsed = parse_coordinator_routing("\n\nCLASSIFY: UI\nscope body")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.kind, "classify_ui")
        self.assertEqual(parsed.body, "scope body")

    def test_next_body_preservation(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = _next(state, "developer", "Exact prompt body")
        self.assertIn("Exact prompt body", action.routing_body)
        self.assertIn("TO: Claude Developer", action.routing_body)
        self.assertIn("Exact prompt body", action.prompt_context)

    def test_final_without_colon_rejected(self):
        parsed = parse_coordinator_routing("FINAL")
        self.assertFalse(parsed.ok)

    def test_final_colon_accepted_only_after_safety_pass(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        state.safety_passed = True
        action = on_coordinator_output(state, "FINAL:\nDone.")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "final")

    def test_coordinator_malformed_routing_fails_closed(self):
        state, _ = on_session_start("task")
        action = on_coordinator_output(state, "NEXT: wizard\nunknown")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertEqual(state.malformed_coordinator_round, 1)

    def test_next_ui_lead_rejected_when_requires_agy_false(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        action = on_coordinator_output(state, "NEXT: ui_lead\nnope")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)

    def test_next_reviewer_rejected_before_agy_approval_in_ui_flow(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        action = on_coordinator_output(state, "NEXT: reviewer\nearly")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)

    def test_next_safety_gate_rejected_before_reviewer_pass(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        action = on_coordinator_output(state, "NEXT: safety_gate\nearly")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)

    def test_final_rejected_before_safety_pass(self):
        state, _ = on_session_start("task")
        _classify_non_ui(state)
        action = on_coordinator_output(state, "FINAL:\nToo early")
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertGreater(state.malformed_coordinator_round, 0)

    def test_multiple_routing_tokens_rejected(self):
        parsed = parse_coordinator_routing("NEXT: developer\nCLASSIFY: UI")
        self.assertFalse(parsed.ok)
        self.assertIn("multiple", parsed.reason)

    def test_unknown_next_role_rejected(self):
        parsed = parse_coordinator_routing("NEXT: wizard")
        self.assertFalse(parsed.ok)

    def test_blocker_terminal_action_includes_blocker_reason(self):
        state, _ = on_session_start("task")
        action = on_coordinator_output(state, "BLOCKER: scope ambiguous")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")
        self.assertIn("scope ambiguous", action.prompt_context)

    def test_malformed_blocker_rejected(self):
        parsed = parse_coordinator_routing("BLOCKER:")
        self.assertFalse(parsed.ok)


class CoordinatorLoopTransitionCapTests(unittest.TestCase):
    def test_total_transition_cap_prevents_infinite_loop(self):
        state, _ = on_session_start("task")
        state.max_total_transitions = 3
        _classify_ui(state)
        _next(state, "developer")
        action = on_worker_output(state, "developer", "READY_FOR_COORDINATOR\n")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")
        self.assertIn("transition", action.prompt_context.lower())
        self.assertEqual(state.total_transition_count, 4)


class CoordinatorLoopAgyTokenTests(unittest.TestCase):
    def test_agy_pass_with_notes_not_accepted_as_approval(self):
        state, _ = on_session_start("task")
        _classify_ui(state)
        _next(state, "ui_lead")
        action = on_worker_output(state, "ui_lead", "PASS WITH NOTES\nsoft")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")


def _report_ready_with_ui_summary(path: str, status: str = "PASS_WITH_NOTES") -> str:
    return (
        "REPORT_READY\n\n"
        f"Status:\n{status}\n\n"
        f"Report:\n{path}\n\n"
        "Summary:\n"
        "`PaymentModal.tsx` and `PaymentModal.css` implement a portal-rendered payment modal "
        "with responsive layout, accessibility, focus, and keyboard handling.\n\n"
        "Next recommended role:\ncoordinator\n\n"
        "Notes:\nnotes\n"
    )


class LateUiReclassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev-report.md"
        self.dev_path.write_text(
            "# PaymentModal\n"
            "PaymentModal.tsx and PaymentModal.css modal layout responsive accessibility focus keyboard.",
            encoding="utf-8",
        )
        self.ctx = {
            "report_paths": [str(self.dev_path)],
            "allowed_report_roots": self.roots,
            "max_report_prompt_chars": 120_000,
        }

    def test_non_ui_developer_report_upgrades_requires_agy(self):
        state, _ = on_session_start(
            "Validate trusted CLI flow",
            session_meta={"report_orchestrated": True},
        )
        _classify_non_ui(state)
        self.assertFalse(state.requires_agy)
        on_coordinator_output(state, "NEXT: developer\nBegin.")
        action = on_worker_output(
            state,
            "developer",
            _report_ready_with_ui_summary(str(self.dev_path)),
            worker_context=self.ctx,
        )
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertTrue(state.requires_agy)
        self.assertTrue(state.ui_changed)
        self.assertFalse(state.agy_approved)
        tokens = coordinator_allowed_tokens(state)
        self.assertIn("NEXT: ui_lead", tokens)
        self.assertNotIn("NEXT: reviewer", tokens)
        late = [e for e in state.verdict_log if e.get("token") == LATE_UI_SIGNAL_TOKEN]
        self.assertEqual(len(late), 1)
        self.assertIn("upgraded requires_agy", late[0]["notes"])
        self.assertIn("Route ui_lead", action.prompt_context)
        route = on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        self.assertEqual(route.target_role, "ui_lead")
        self.assertEqual(state.malformed_coordinator_round, 0)

    def test_non_ui_without_ui_keywords_stays_non_ui(self):
        state, _ = on_session_start("Validate trusted CLI flow")
        _classify_non_ui(state)
        _next(state, "developer")
        action = on_worker_output(
            state,
            "developer",
            "READY_FOR_COORDINATOR\n"
            + ("Trusted CLI handoff block parity and reviewer bridge normalization. " * 25),
        )
        self.assertEqual(action.target_role, COORDINATOR_ROLE)
        self.assertFalse(state.requires_agy)
        self.assertFalse(state.ui_changed)
        tokens = coordinator_allowed_tokens(state)
        self.assertNotIn("NEXT: ui_lead", tokens)
        self.assertIn("NEXT: reviewer", tokens)
        self.assertNotIn("Route ui_lead", action.prompt_context)

    def test_initial_ui_classification_unchanged(self):
        state, _ = on_session_start("ui task")
        _classify_ui(state)
        self.assertTrue(state.requires_agy)
        _next(state, "developer")
        action = on_worker_output(
            state,
            "developer",
            "READY_FOR_COORDINATOR\n" + ("PaymentModal.tsx layout analysis. " * 30),
        )
        self.assertIn("Route ui_lead", action.prompt_context)
        tokens = coordinator_allowed_tokens(state)
        self.assertIn("NEXT: ui_lead", tokens)
        route = on_coordinator_output(state, "NEXT: ui_lead\nReview.")
        self.assertEqual(route.target_role, "ui_lead")

    def test_hint_no_ui_lead_when_non_ui_and_no_late_signal(self):
        state, _ = on_session_start("backend")
        _classify_non_ui(state)
        _next(state, "developer")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\nBackend-only analysis.")
        _next(state, "reviewer")
        on_worker_output(state, "reviewer", "REQUEST CHANGES\nFix routing contract.")
        _next(state, "developer")
        action = on_worker_output(
            state,
            "developer",
            "READY_FOR_COORDINATOR\n" + ("Backend routing contract fix plan. " * 20),
        )
        self.assertNotIn("Route ui_lead", action.prompt_context)
        self.assertIn("Route reviewer", action.prompt_context)

    def test_session_92_smoke_after_reviewer_correction(self):
        """Simulate session 92: NON_UI classify, UI report, reviewer REQUEST_CHANGES, dev fix."""
        rev_path = Path(self.tmp) / "rev.md"
        rev_path.write_text(
            "# Review\nPayment-flow UX/accessibility gaps in PaymentModal modal layout.",
            encoding="utf-8",
        )
        state, _ = on_session_start(
            "Validate trusted CLI flow after handoff block parity and reviewer bridge normalization fixes",
            session_meta={"report_orchestrated": True},
        )
        _classify_non_ui(state)
        on_coordinator_output(state, "NEXT: developer\nAnalyze.")
        on_worker_output(
            state,
            "developer",
            _report_ready_with_ui_summary(str(self.dev_path)),
            worker_context=self.ctx,
        )
        self.assertTrue(state.requires_agy)
        on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        on_worker_output(state, "ui_lead", "UX_APPROVED\nok")
        on_coordinator_output(state, "NEXT: reviewer\nReview.")
        rev_ctx = dict(self.ctx, report_paths=[str(rev_path)])
        on_worker_output(
            state,
            "reviewer",
            _report_ready_with_ui_summary(str(rev_path), status="REQUEST_CHANGES").replace(
                "PASS_WITH_NOTES", "REQUEST_CHANGES",
            ),
            worker_context=rev_ctx,
        )
        on_coordinator_output(state, "NEXT: developer\nFix.")
        action = on_worker_output(
            state,
            "developer",
            _report_ready_with_ui_summary(str(self.dev_path)),
            worker_context=self.ctx,
        )
        self.assertTrue(state.requires_agy)
        tokens = coordinator_allowed_tokens(state)
        self.assertIn("NEXT: ui_lead", tokens)
        self.assertIn("Route ui_lead", action.prompt_context)
        route = on_coordinator_output(state, "NEXT: ui_lead\nUX re-check.")
        self.assertEqual(route.target_role, "ui_lead")
        self.assertNotIn("ui_lead_not_allowed", route.prompt_context)


class UiLeadReportBridgeRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        agy_root = Path(self.tmp) / "agy"
        agy_root.mkdir()
        self.channel = "twinpet-ui-09-c-trus"
        self.agy_path = str(agy_root / f"{self.channel}-ux-review.md")
        self.policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "trusted_direct_repo_cli": True,
            "write_files": [],
            "external_report_write_roots": [self.tmp, str(agy_root)],
            "report_paths": [self.agy_path],
        }
        self.ctx = {
            "workspace_policy": self.policy,
            "allowed_report_roots": [self.tmp, str(agy_root)],
            "report_paths_by_role": {"ui_lead": self.agy_path},
            "max_report_prompt_chars": 120_000,
        }

    def _incomplete_bridge(self) -> str:
        return (
            "REPORT_FILE_WRITE_BEGIN\n"
            f"Path: {self.agy_path}\n"
            "Status: REQUEST_CHANGES\n"
            "Summary: UX review incomplete block.\n"
            "Next recommended role: developer\n"
            "---\n"
            "# UX Review\nMissing END marker.\n"
        )

    def _complete_bridge(self) -> str:
        return (
            "REPORT_FILE_WRITE_BEGIN\n"
            f"Path: {self.agy_path}\n"
            "Status: REQUEST_CHANGES\n"
            "Summary: UX review complete.\n"
            "Next recommended role: developer\n"
            "---\n"
            "# UX Review\nFocus and keyboard fixes needed.\n"
            "REPORT_FILE_WRITE_END\n"
        )

    def test_first_incomplete_bridge_triggers_one_repair(self):
        state, _ = on_session_start("task", session_meta={"report_orchestrated": True})
        _classify_ui(state)
        _next(state, "ui_lead")
        action = on_worker_output(
            state,
            "ui_lead",
            self._incomplete_bridge(),
            worker_context=self.ctx,
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "ui_lead")
        self.assertEqual(state.ui_lead_report_bridge_repair_rounds, 1)
        self.assertIn("REPORT_FILE_WRITE_END was missing", action.routing_body)

    def test_second_incomplete_bridge_terminal_blocks(self):
        from coordinator_loop import CoordinatorLoopState, CoordinatorPhase

        state = CoordinatorLoopState(
            phase=CoordinatorPhase.AWAIT_UI_LEAD,
            awaiting_role="ui_lead",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            ui_lead_report_bridge_repair_rounds=1,
            max_ui_lead_report_bridge_repair_rounds=1,
        )
        action = on_worker_output(
            state,
            "ui_lead",
            self._incomplete_bridge(),
            worker_context=self.ctx,
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("external report write failed", action.prompt_context)

    def test_repair_success_produces_report_ready(self):
        state, _ = on_session_start("task", session_meta={"report_orchestrated": True})
        _classify_ui(state)
        _next(state, "ui_lead")
        on_worker_output(
            state,
            "ui_lead",
            self._complete_bridge(),
            worker_context=self.ctx,
        )
        self.assertTrue(Path(self.agy_path).is_file())
        self.assertEqual(state.ui_lead_report_bridge_repair_rounds, 0)


if __name__ == "__main__":
    unittest.main()
