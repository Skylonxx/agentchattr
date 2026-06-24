"""Tests for sandbox flow coordinator — verdict parser, state machine, routing,
loop limits, safety blocks, and policy enforcement.

Unit tests only — no live sessions, no paid APIs, no external connections.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow_coordinator import (
    Action,
    FlowState,
    Phase,
    MAX_ENG_LOOPS,
    MAX_TOTAL_STEPS,
    MAX_UX_LOOPS,
    intake,
    on_agy_verdict,
    on_codex_verdict,
    on_developer_verdict,
    _is_blocked_path,
    _is_ui_change,
    _has_agy_pass,
)
from session_relay import (
    AGY_TOKENS,
    CODEX_REVIEWER_TOKENS,
    DEVELOPER_TOKENS,
    WorkflowVerdict,
    parse_workflow_verdict,
    parse_safety_verdict,
    RELAY_ELIGIBLE_AGENTS,
    is_relay_eligible,
)


# ---------------------------------------------------------------------------
# 1. Verdict Parser Tests
# ---------------------------------------------------------------------------

class TestWorkflowVerdictParserAGY(unittest.TestCase):
    def test_pass(self):
        v = parse_workflow_verdict("PASS", AGY_TOKENS)
        self.assertTrue(v.passed)
        self.assertFalse(v.needs_rework)
        self.assertEqual(v.token, "PASS")

    def test_pass_with_notes(self):
        v = parse_workflow_verdict("PASS WITH NOTES\nMinor spacing issue.", AGY_TOKENS)
        self.assertTrue(v.passed)
        self.assertEqual(v.token, "PASS WITH NOTES")
        self.assertIn("spacing", v.notes)

    def test_request_ux_changes(self):
        v = parse_workflow_verdict("REQUEST UX CHANGES\nButton too small.", AGY_TOKENS)
        self.assertFalse(v.passed)
        self.assertTrue(v.needs_rework)
        self.assertEqual(v.token, "REQUEST UX CHANGES")

    def test_blocked(self):
        v = parse_workflow_verdict("BLOCKED\nCannot review.", AGY_TOKENS)
        self.assertFalse(v.passed)
        self.assertFalse(v.needs_rework)
        self.assertEqual(v.token, "BLOCKED")

    def test_case_insensitive(self):
        v = parse_workflow_verdict("pass with notes", AGY_TOKENS)
        self.assertTrue(v.passed)
        self.assertEqual(v.token, "PASS WITH NOTES")

    def test_empty_output_ambiguous(self):
        v = parse_workflow_verdict("", AGY_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")
        self.assertFalse(v.passed)

    def test_none_output_ambiguous(self):
        v = parse_workflow_verdict(None, AGY_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")

    def test_unrecognised_token_ambiguous(self):
        v = parse_workflow_verdict("LOOKS GOOD TO ME", AGY_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")
        self.assertIn("unrecognised", v.notes)

    def test_conflicting_verdicts_ambiguous(self):
        v = parse_workflow_verdict("PASS\nREQUEST UX CHANGES", AGY_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")
        self.assertIn("mixed", v.notes)

    def test_leading_whitespace_stripped(self):
        v = parse_workflow_verdict("  \n  PASS\n", AGY_TOKENS)
        self.assertTrue(v.passed)

    def test_preserves_raw_output(self):
        raw = "REQUEST UX CHANGES\nFix the modal"
        v = parse_workflow_verdict(raw, AGY_TOKENS)
        self.assertEqual(v.raw_output, raw)


class TestWorkflowVerdictParserCodex(unittest.TestCase):
    def test_pass(self):
        v = parse_workflow_verdict("PASS", CODEX_REVIEWER_TOKENS)
        self.assertTrue(v.passed)

    def test_pass_with_notes(self):
        v = parse_workflow_verdict("PASS WITH NOTES\nAdd a test.", CODEX_REVIEWER_TOKENS)
        self.assertTrue(v.passed)

    def test_request_changes(self):
        v = parse_workflow_verdict("REQUEST CHANGES\nMissing validation.", CODEX_REVIEWER_TOKENS)
        self.assertFalse(v.passed)
        self.assertTrue(v.needs_rework)

    def test_blocked(self):
        v = parse_workflow_verdict("BLOCKED", CODEX_REVIEWER_TOKENS)
        self.assertFalse(v.passed)

    def test_conflicting_verdicts(self):
        v = parse_workflow_verdict("PASS\nREQUEST CHANGES", CODEX_REVIEWER_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")


class TestWorkflowVerdictParserDeveloper(unittest.TestCase):
    def test_ready_for_agy(self):
        v = parse_workflow_verdict("READY_FOR_AGY_REVIEW", DEVELOPER_TOKENS)
        self.assertTrue(v.passed)
        self.assertEqual(v.token, "READY_FOR_AGY_REVIEW")

    def test_ready_for_codex(self):
        v = parse_workflow_verdict("READY_FOR_CODEX_REVIEW", DEVELOPER_TOKENS)
        self.assertTrue(v.passed)

    def test_ready_for_review_package(self):
        v = parse_workflow_verdict("READY_FOR_REVIEW_PACKAGE", DEVELOPER_TOKENS)
        self.assertTrue(v.passed)

    def test_blocked(self):
        v = parse_workflow_verdict("BLOCKED\nDependency missing", DEVELOPER_TOKENS)
        self.assertFalse(v.passed)

    def test_unrecognised(self):
        v = parse_workflow_verdict("DONE", DEVELOPER_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")


class TestSafetyVerdictUnchanged(unittest.TestCase):
    """Existing CodexSafe safety verdict parser must remain unchanged."""

    def test_safety_pass(self):
        v = parse_safety_verdict("PASS")
        self.assertTrue(v.passed)

    def test_safety_block(self):
        v = parse_safety_verdict("BLOCK: unsafe content")
        self.assertFalse(v.passed)

    def test_safety_malformed(self):
        v = parse_safety_verdict("PASS WITH NOTES")
        self.assertFalse(v.passed)

    def test_safety_mixed(self):
        v = parse_safety_verdict("PASS\nBLOCK: actually no")
        self.assertFalse(v.passed)


# ---------------------------------------------------------------------------
# 2. Flow Coordinator State Machine Tests
# ---------------------------------------------------------------------------

class TestFlowCoordinatorIntake(unittest.TestCase):
    def test_normal_intake_routes_to_developer(self):
        state = FlowState()
        action = intake(state, "Bakery POS checkout modal UX improvement")
        self.assertEqual(action.target_role, "developer")
        self.assertEqual(state.phase, Phase.DEV_WORK)
        self.assertFalse(action.is_terminal)

    def test_blocked_path_twinpet(self):
        state = FlowState()
        action = intake(state, "Fix C:\\Users\\Narachat\\twinpet-pos checkout")
        self.assertEqual(action.target_role, "halted")
        self.assertTrue(action.is_terminal)
        self.assertEqual(state.phase, Phase.HALTED)
        self.assertIn("blocked production path", state.halt_reason)

    def test_blocked_path_twinpet_case_insensitive(self):
        state = FlowState()
        action = intake(state, "Fix TwinPet app bugs")
        self.assertTrue(action.is_terminal)
        self.assertEqual(state.phase, Phase.HALTED)


class TestFlowCoordinatorHappyPath(unittest.TestCase):
    """Test C — Happy path."""

    def test_happy_path_full_cycle(self):
        state = FlowState()

        # Intake
        action = intake(state, "Bakery POS checkout modal UX improvement")
        self.assertEqual(action.target_role, "developer")

        # Developer ready
        action = on_developer_verdict(
            state, "READY_FOR_AGY_REVIEW",
            report_path="C:\\Users\\Narachat\\OneDrive\\Ai-Report\\bakery-mock.md",
        )
        self.assertEqual(action.target_role, "agy")
        self.assertEqual(state.phase, Phase.AGY_REVIEW)

        # AGY pass
        action = on_agy_verdict(state, "PASS")
        self.assertEqual(action.target_role, "codex")
        self.assertEqual(state.phase, Phase.CODEX_REVIEW)

        # Codex pass
        action = on_codex_verdict(state, "PASS")
        self.assertEqual(action.target_role, "closure")
        self.assertTrue(action.is_terminal)
        self.assertEqual(state.phase, Phase.CLOSURE)
        self.assertEqual(state.ux_loops, 0)
        self.assertEqual(state.eng_loops, 0)
        self.assertIn("task", state.closure_summary)
        self.assertIn("report_path", state.closure_summary)


class TestFlowCoordinatorAGYFail(unittest.TestCase):
    """Test A — AGY fails first."""

    def test_agy_fail_routes_back_to_developer(self):
        state = FlowState()
        intake(state, "Bakery POS checkout modal UX improvement")

        on_developer_verdict(state, "READY_FOR_AGY_REVIEW",
                             report_path="report.md")

        # AGY fails
        action = on_agy_verdict(state, "REQUEST UX CHANGES",
                                notes="Button too small on mobile")
        self.assertEqual(action.target_role, "developer")
        self.assertEqual(state.ux_loops, 1)
        self.assertEqual(state.phase, Phase.DEV_WORK)

        # Developer fixes
        action = on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        self.assertEqual(action.target_role, "agy")

        # AGY passes
        action = on_agy_verdict(state, "PASS")
        self.assertEqual(action.target_role, "codex")

        # Codex passes
        action = on_codex_verdict(state, "PASS")
        self.assertEqual(action.target_role, "closure")
        self.assertTrue(action.is_terminal)
        self.assertEqual(state.ux_loops, 1)
        self.assertEqual(state.eng_loops, 0)

    def test_agy_fail_full_routing_order(self):
        """Assert exact order: Developer → AGY → Developer → AGY → Codex → Closure."""
        state = FlowState()
        order = []

        a = intake(state, "Bakery modal improvement")
        order.append(a.target_role)

        a = on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        order.append(a.target_role)

        a = on_agy_verdict(state, "REQUEST UX CHANGES", notes="fix spacing")
        order.append(a.target_role)

        a = on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        order.append(a.target_role)

        a = on_agy_verdict(state, "PASS")
        order.append(a.target_role)

        a = on_codex_verdict(state, "PASS")
        order.append(a.target_role)

        self.assertEqual(order,
                         ["developer", "agy", "developer", "agy", "codex", "closure"])


class TestFlowCoordinatorCodexFail(unittest.TestCase):
    """Test B — Codex fails first."""

    def test_codex_fail_engineering_only_routes_back_to_developer(self):
        state = FlowState()
        intake(state, "Bakery POS checkout modal UX improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")

        # Codex fails
        action = on_codex_verdict(state, "REQUEST CHANGES",
                                  notes="Missing boundary validation",
                                  fix_description="add input validation to amount field")
        self.assertEqual(action.target_role, "developer")
        self.assertEqual(state.eng_loops, 1)

    def test_codex_fail_engineering_fix_skips_agy(self):
        """Engineering-only fix with prior AGY pass: developer can go straight to Codex."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Missing test", fix_description="add unit test")

        # Developer fixes (engineering only) and says ready for Codex
        action = on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")
        # AGY already passed, so goes straight to Codex
        self.assertEqual(action.target_role, "codex")

    def test_codex_fail_ui_fix_must_rerun_agy(self):
        """UI/UX fix after Codex fail must re-run AGY before Codex."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")

        # Codex requests changes with UI/UX implication
        action = on_codex_verdict(
            state, "REQUEST CHANGES",
            notes="Button layout needs rework",
            fix_description="changed modal button layout and CSS spacing",
        )
        self.assertEqual(action.target_role, "developer")
        self.assertIn("AGY re-review required", action.prompt_context)

    def test_codex_fail_full_cycle_closure(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Missing test",
                         fix_description="add boundary validation test")
        on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")
        action = on_codex_verdict(state, "PASS")
        self.assertEqual(action.target_role, "closure")
        self.assertTrue(action.is_terminal)
        self.assertEqual(state.eng_loops, 1)
        self.assertEqual(state.ux_loops, 0)


class TestFlowCoordinatorSafetyBlock(unittest.TestCase):
    """Test D — Safety block."""

    def test_twinpet_path_blocked_at_intake(self):
        state = FlowState()
        action = intake(state, "Fix C:\\Users\\Narachat\\twinpet-pos\\src\\checkout.tsx")
        self.assertEqual(action.target_role, "halted")
        self.assertTrue(action.is_terminal)
        self.assertIn("blocked production path", state.halt_reason)
        # No developer / AGY / Codex dispatch should have occurred
        self.assertEqual(state.total_steps, 1)  # only the intake step
        self.assertEqual(len(state.verdicts), 0)

    def test_twinpet_in_description_blocked(self):
        state = FlowState()
        action = intake(state, "Update the twinpet customer list modal")
        self.assertTrue(action.is_terminal)

    def test_non_twinpet_not_blocked(self):
        state = FlowState()
        action = intake(state, "Bakery POS checkout modal UX improvement")
        self.assertFalse(action.is_terminal)


# ---------------------------------------------------------------------------
# 3. Loop Limit Tests
# ---------------------------------------------------------------------------

class TestLoopLimits(unittest.TestCase):
    def test_ux_loop_limit(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")

        for i in range(MAX_UX_LOOPS):
            on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
            action = on_agy_verdict(state, "REQUEST UX CHANGES",
                                    notes=f"Fix iteration {i+1}")
            self.assertEqual(action.target_role, "developer")

        # One more fail should halt
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        action = on_agy_verdict(state, "REQUEST UX CHANGES", notes="Too many")
        self.assertEqual(action.target_role, "halted")
        self.assertTrue(action.is_terminal)
        self.assertIn("max UX loops", state.halt_reason)

    def test_eng_loop_limit(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")

        for i in range(MAX_ENG_LOOPS):
            on_codex_verdict(state, "REQUEST CHANGES",
                             notes=f"Fix iteration {i+1}",
                             fix_description="add test")
            on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")

        # One more fail should halt
        action = on_codex_verdict(state, "REQUEST CHANGES",
                                  notes="Too many",
                                  fix_description="add test")
        self.assertEqual(action.target_role, "halted")
        self.assertIn("max engineering loops", state.halt_reason)

    def test_total_step_limit(self):
        state = FlowState()
        state.total_steps = MAX_TOTAL_STEPS  # artificially at limit
        action = on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        self.assertEqual(action.target_role, "halted")
        self.assertIn("max total steps", state.halt_reason)

    def test_ambiguous_verdict_halts(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        action = on_agy_verdict(state, "AMBIGUOUS", notes="garbled output")
        self.assertEqual(action.target_role, "halted")
        self.assertIn("ambiguous", state.halt_reason)

    def test_developer_blocked_halts(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        action = on_developer_verdict(state, "BLOCKED", notes="Missing dependency")
        self.assertEqual(action.target_role, "halted")
        self.assertIn("developer blocked", state.halt_reason)


# ---------------------------------------------------------------------------
# 4. UI Change Detection Tests
# ---------------------------------------------------------------------------

class TestUIChangeDetection(unittest.TestCase):
    def test_tsx_is_ui(self):
        self.assertTrue(_is_ui_change("changed modal.tsx"))

    def test_css_is_ui(self):
        self.assertTrue(_is_ui_change("updated button.css"))

    def test_layout_keyword_is_ui(self):
        self.assertTrue(_is_ui_change("fixed responsive layout"))

    def test_modal_keyword_is_ui(self):
        self.assertTrue(_is_ui_change("improved modal interaction"))

    def test_pure_test_is_not_ui(self):
        self.assertFalse(_is_ui_change("added boundary validation test"))

    def test_pure_logic_is_not_ui(self):
        self.assertFalse(_is_ui_change("fixed input validation logic"))


# ---------------------------------------------------------------------------
# 5. Blocked Path Detection Tests
# ---------------------------------------------------------------------------

class TestBlockedPathDetection(unittest.TestCase):
    def test_twinpet_blocked(self):
        self.assertTrue(_is_blocked_path("C:\\Users\\Narachat\\twinpet-pos"))

    def test_twinpet_case_insensitive(self):
        self.assertTrue(_is_blocked_path("update TwinPet app"))

    def test_bakery_not_blocked(self):
        self.assertFalse(_is_blocked_path("Bakery POS checkout modal"))

    def test_agentchattr_not_blocked(self):
        self.assertFalse(_is_blocked_path("agentchattr sandbox flow"))


# ---------------------------------------------------------------------------
# 6. Relay Eligibility Unchanged
# ---------------------------------------------------------------------------

class TestRelayEligibilityUnchanged(unittest.TestCase):
    def test_agy_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_claude_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("claude"))
        self.assertNotIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_codex_relay_eligible(self):
        self.assertTrue(is_relay_eligible("codex"))

    def test_codexsafe_relay_eligible(self):
        self.assertTrue(is_relay_eligible("codexsafe"))


# ---------------------------------------------------------------------------
# 7. Safety Invariant Integration
# ---------------------------------------------------------------------------

class TestSafetyInvariantIntegration(unittest.TestCase):
    def test_sandbox_loop_limits_pass(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=0, total_steps=1)
        self.assertTrue(r.ok)

    def test_sandbox_loop_limits_ux_exceeded(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=3, eng_loops=0, total_steps=5)
        self.assertFalse(r.ok)
        self.assertIn("ux_loops", r.reason)

    def test_sandbox_loop_limits_eng_exceeded(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=3, total_steps=5)
        self.assertFalse(r.ok)
        self.assertIn("eng_loops", r.reason)

    def test_sandbox_loop_limits_total_exceeded(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=0, total_steps=13)
        self.assertFalse(r.ok)
        self.assertIn("total_steps", r.reason)

    def test_sandbox_template_rejects_twinpet(self):
        from safety_invariants import check_sandbox_template_safe
        tmpl = {"id": "bad", "phases": [], "description": "fix twinpet checkout"}
        r = check_sandbox_template_safe(tmpl)
        self.assertFalse(r.ok)

    def test_sandbox_template_accepts_bakery(self):
        from safety_invariants import check_sandbox_template_safe
        tmpl = {"id": "good", "phases": [], "description": "bakery checkout modal"}
        r = check_sandbox_template_safe(tmpl)
        self.assertTrue(r.ok)

    def test_existing_relay_eligibility_invariant(self):
        from safety_invariants import check_relay_eligibility
        r = check_relay_eligibility()
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# 8. Sandbox Template Validation
# ---------------------------------------------------------------------------

class TestSandboxTemplate(unittest.TestCase):
    def setUp(self):
        self.tmpl_path = ROOT / "session_templates" / "sandbox-bakery-flow.json"
        self.tmpl = json.loads(self.tmpl_path.read_text("utf-8"))

    def test_template_exists(self):
        self.assertTrue(self.tmpl_path.exists())

    def test_template_id(self):
        self.assertEqual(self.tmpl["id"], "sandbox-bakery-flow")

    def test_sandbox_only_flag(self):
        self.assertTrue(self.tmpl.get("sandbox_only"))

    def test_flow_coordinator_flag(self):
        self.assertTrue(self.tmpl.get("flow_coordinator"))

    def test_roles(self):
        self.assertEqual(self.tmpl["roles"],
                         ["developer", "ui_lead", "codex_reviewer"])

    def test_phase_count(self):
        self.assertEqual(len(self.tmpl["phases"]), 3)

    def test_no_twinpet_reference(self):
        blob = json.dumps(self.tmpl).lower()
        self.assertNotIn("twinpet", blob)

    def test_final_phase_is_output(self):
        self.assertTrue(self.tmpl["phases"][-1].get("is_output"))

    def test_developer_phase_prompt_includes_status_tokens(self):
        prompt = self.tmpl["phases"][0]["prompt"]
        self.assertIn("READY_FOR_AGY_REVIEW", prompt)
        self.assertIn("BLOCKED", prompt)

    def test_agy_phase_prompt_includes_verdict_tokens(self):
        prompt = self.tmpl["phases"][1]["prompt"]
        self.assertIn("REQUEST UX CHANGES", prompt)
        self.assertIn("PASS", prompt)

    def test_codex_phase_prompt_includes_verdict_tokens(self):
        prompt = self.tmpl["phases"][2]["prompt"]
        self.assertIn("REQUEST CHANGES", prompt)
        self.assertIn("PASS", prompt)

    def test_passes_sandbox_template_safety_check(self):
        from safety_invariants import check_sandbox_template_safe
        r = check_sandbox_template_safe(self.tmpl)
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# 9. Session Engine Opt-In Hook
# ---------------------------------------------------------------------------

class TestSessionEngineOptIn(unittest.TestCase):
    def _make_engine(self, template):
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeSessionStore, _FakeMessageStore,
            _FakeAgentTrigger, _FakeRegistry,
        )
        session = {
            "id": 1, "template_id": template["id"], "channel": "sandbox",
            "cast": {}, "state": "active", "current_phase": 0, "current_turn": 0,
        }
        store = _FakeSessionStore(
            sessions=[session],
            templates={template["id"]: template},
        )
        engine = SessionEngine(store, _FakeMessageStore(), _FakeAgentTrigger())
        return engine, session

    def test_linear_template_not_flow_coordinator(self):
        tmpl = {"id": "linear", "name": "Linear", "phases": []}
        engine, session = self._make_engine(tmpl)
        self.assertFalse(engine.is_flow_coordinator_session(session))

    def test_sandbox_template_is_flow_coordinator(self):
        tmpl = {"id": "sandbox", "name": "Sandbox", "phases": [],
                "flow_coordinator": True}
        engine, session = self._make_engine(tmpl)
        self.assertTrue(engine.is_flow_coordinator_session(session))

    def test_existing_templates_not_flow_coordinator(self):
        """All 6 shipped templates must NOT activate flow coordinator."""
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeSessionStore, _FakeMessageStore,
            _FakeAgentTrigger,
        )
        import session_store as ss
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ss.SessionStore(
                str(Path(tmp) / "sessions.json"),
                templates_dir=str(ROOT / "session_templates"),
            )
            for tid, tmpl in store._templates.items():
                if tid == "sandbox-bakery-flow":
                    continue  # skip our new template
                fake_session = {"template_id": tid}
                fake_store = _FakeSessionStore(
                    sessions=[],
                    templates={tid: tmpl},
                )
                engine = SessionEngine(
                    fake_store, _FakeMessageStore(), _FakeAgentTrigger(),
                )
                self.assertFalse(
                    engine.is_flow_coordinator_session(fake_session),
                    f"shipped template {tid!r} must not be flow_coordinator",
                )


# ---------------------------------------------------------------------------
# 10. FlowState Serialisation
# ---------------------------------------------------------------------------

class TestFlowStateSerialisable(unittest.TestCase):
    def test_to_dict_round_trip(self):
        state = FlowState()
        intake(state, "Bakery checkout")
        d = state.to_dict()
        self.assertEqual(d["phase"], "dev_work")
        self.assertEqual(d["task_description"], "Bakery checkout")
        # Must be JSON-serialisable
        json.dumps(d)

    def test_closure_summary_serialisable(self):
        state = FlowState()
        intake(state, "Bakery checkout")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "PASS")
        d = state.to_dict()
        self.assertEqual(d["phase"], "closure")
        summary = d["closure_summary"]
        self.assertIn("task", summary)
        self.assertIn("report_path", summary)
        json.dumps(d)


# ---------------------------------------------------------------------------
# 11. AGY re-run policy after Codex fail (approved policy)
# ---------------------------------------------------------------------------

class TestAGYRerunPolicy(unittest.TestCase):
    def test_engineering_fix_no_prior_agy_pass_routes_to_agy(self):
        """Without a prior AGY pass, even READY_FOR_CODEX_REVIEW goes to AGY."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        action = on_developer_verdict(state, "READY_FOR_CODEX_REVIEW",
                                      report_path="r.md")
        self.assertEqual(action.target_role, "agy")

    def test_engineering_fix_with_prior_agy_pass_routes_to_codex(self):
        """With a prior AGY pass, READY_FOR_CODEX_REVIEW goes to Codex."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES", notes="fix",
                         fix_description="add test")

        action = on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")
        self.assertEqual(action.target_role, "codex")

    def test_has_agy_pass_tracks_correctly(self):
        state = FlowState()
        self.assertFalse(_has_agy_pass(state))

        state.verdicts.append({"role": "agy", "token": "REQUEST UX CHANGES",
                               "time": 0})
        self.assertFalse(_has_agy_pass(state))

        state.verdicts.append({"role": "agy", "token": "PASS", "time": 0})
        self.assertTrue(_has_agy_pass(state))


# ---------------------------------------------------------------------------
# 12. Finding 1 — Durable requires_agy_rereview enforcement
# ---------------------------------------------------------------------------

class TestRequiresAGYRereview(unittest.TestCase):
    def test_ui_codex_fail_sets_requires_agy_rereview(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Button layout wrong",
                         fix_description="changed modal button layout and CSS")
        self.assertTrue(state.requires_agy_rereview)

    def test_ready_for_codex_routes_to_agy_when_rereview_required(self):
        """Even with prior AGY pass, READY_FOR_CODEX_REVIEW routes to AGY
        when requires_agy_rereview is True."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Fix layout",
                         fix_description="changed CSS layout")
        self.assertTrue(state.requires_agy_rereview)

        action = on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")
        self.assertEqual(action.target_role, "agy")

    def test_agy_pass_clears_requires_agy_rereview(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Fix layout",
                         fix_description="changed modal button CSS")
        self.assertTrue(state.requires_agy_rereview)

        on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        on_agy_verdict(state, "PASS")
        self.assertFalse(state.requires_agy_rereview)

    def test_after_agy_clears_rereview_codex_route_works(self):
        """After AGY clears requires_agy_rereview, Developer can route to Codex."""
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Fix layout",
                         fix_description="changed modal CSS spacing")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW")
        on_agy_verdict(state, "PASS WITH NOTES")
        self.assertFalse(state.requires_agy_rereview)

        # Now Codex fails with engineering-only fix
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Missing test",
                         fix_description="add unit test")
        self.assertFalse(state.requires_agy_rereview)

        action = on_developer_verdict(state, "READY_FOR_CODEX_REVIEW")
        self.assertEqual(action.target_role, "codex")

    def test_engineering_codex_fail_does_not_set_rereview(self):
        state = FlowState()
        intake(state, "Bakery modal improvement")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES",
                         notes="Missing test",
                         fix_description="add boundary validation test")
        self.assertFalse(state.requires_agy_rereview)

    def test_rereview_persisted_in_to_dict(self):
        state = FlowState()
        state.requires_agy_rereview = True
        d = state.to_dict()
        self.assertTrue(d["requires_agy_rereview"])


# ---------------------------------------------------------------------------
# 13. Finding 2 — Cross-role verdict conflict detection
# ---------------------------------------------------------------------------

class TestCrossRoleConflictDetection(unittest.TestCase):
    def test_agy_pass_then_request_changes_ambiguous(self):
        """AGY: PASS followed by REQUEST CHANGES (Codex token) => AMBIGUOUS."""
        v = parse_workflow_verdict("PASS\nREQUEST CHANGES", AGY_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")
        self.assertIn("mixed", v.notes)

    def test_codex_pass_then_request_ux_changes_ambiguous(self):
        """Codex: PASS followed by REQUEST UX CHANGES (AGY token) => AMBIGUOUS."""
        v = parse_workflow_verdict("PASS\nREQUEST UX CHANGES", CODEX_REVIEWER_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")
        self.assertIn("mixed", v.notes)

    def test_developer_ready_then_pass_ambiguous(self):
        """Developer: READY_FOR_AGY_REVIEW followed by PASS => AMBIGUOUS."""
        v = parse_workflow_verdict("READY_FOR_AGY_REVIEW\nPASS", DEVELOPER_TOKENS)
        self.assertEqual(v.token, "AMBIGUOUS")

    def test_valid_notes_after_pass_still_accepted(self):
        """Plain notes that are not verdict tokens should still be accepted."""
        v = parse_workflow_verdict("PASS\nThe code looks good overall.", AGY_TOKENS)
        self.assertTrue(v.passed)
        self.assertEqual(v.token, "PASS")
        self.assertIn("looks good", v.notes)

    def test_safety_verdict_regression(self):
        """parse_safety_verdict must remain unchanged by this fix."""
        v = parse_safety_verdict("PASS")
        self.assertTrue(v.passed)
        v = parse_safety_verdict("BLOCK: bad")
        self.assertFalse(v.passed)
        v = parse_safety_verdict("PASS WITH NOTES")
        self.assertFalse(v.passed)
        v = parse_safety_verdict("PASS\nBLOCK: no")
        self.assertFalse(v.passed)


# ---------------------------------------------------------------------------
# 14. Finding 3 — Negative and bool counter rejection
# ---------------------------------------------------------------------------

class TestLoopLimitNegativeAndBool(unittest.TestCase):
    def test_negative_ux_loops_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=-1, eng_loops=0, total_steps=0)
        self.assertFalse(r.ok)
        self.assertIn("negative", r.reason)

    def test_negative_eng_loops_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=-1, total_steps=0)
        self.assertFalse(r.ok)
        self.assertIn("negative", r.reason)

    def test_negative_total_steps_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=0, total_steps=-1)
        self.assertFalse(r.ok)
        self.assertIn("negative", r.reason)

    def test_bool_ux_loops_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=True, eng_loops=0, total_steps=0)
        self.assertFalse(r.ok)
        self.assertIn("non-bool int", r.reason)

    def test_bool_eng_loops_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=False, total_steps=0)
        self.assertFalse(r.ok)
        self.assertIn("non-bool int", r.reason)

    def test_bool_total_steps_fails(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=0, total_steps=True)
        self.assertFalse(r.ok)
        self.assertIn("non-bool int", r.reason)

    def test_valid_zero_counters_pass(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=0, eng_loops=0, total_steps=0)
        self.assertTrue(r.ok)

    def test_valid_max_counters_pass(self):
        from safety_invariants import check_sandbox_loop_limits
        r = check_sandbox_loop_limits(ux_loops=2, eng_loops=2, total_steps=12)
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# 15. FlowState from_dict round-trip
# ---------------------------------------------------------------------------

class TestFlowStateFromDict(unittest.TestCase):
    def test_round_trip_basic(self):
        state = FlowState()
        intake(state, "Bakery checkout")
        d = state.to_dict()
        restored = FlowState.from_dict(d)
        self.assertEqual(restored.phase, Phase.DEV_WORK)
        self.assertEqual(restored.task_description, "Bakery checkout")
        self.assertEqual(restored.total_steps, 1)

    def test_round_trip_requires_agy_rereview(self):
        state = FlowState()
        intake(state, "Bakery modal")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "REQUEST CHANGES", notes="fix layout",
                         fix_description="changed modal CSS")
        self.assertTrue(state.requires_agy_rereview)

        d = state.to_dict()
        restored = FlowState.from_dict(d)
        self.assertTrue(restored.requires_agy_rereview)
        self.assertEqual(restored.eng_loops, 1)
        self.assertEqual(restored.phase, Phase.DEV_WORK)

    def test_round_trip_closure(self):
        state = FlowState()
        intake(state, "Bakery checkout")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        on_agy_verdict(state, "PASS")
        on_codex_verdict(state, "PASS")
        d = state.to_dict()
        restored = FlowState.from_dict(d)
        self.assertEqual(restored.phase, Phase.CLOSURE)
        self.assertIn("task", restored.closure_summary)

    def test_round_trip_verdicts(self):
        state = FlowState()
        intake(state, "Bakery checkout")
        on_developer_verdict(state, "READY_FOR_AGY_REVIEW", report_path="r.md")
        d = state.to_dict()
        restored = FlowState.from_dict(d)
        self.assertEqual(len(restored.verdicts), 1)
        self.assertEqual(restored.verdicts[0]["role"], "developer")

    def test_from_dict_unknown_phase_halts(self):
        restored = FlowState.from_dict({"phase": "nonexistent"})
        self.assertEqual(restored.phase, Phase.HALTED)

    def test_from_dict_empty_dict(self):
        restored = FlowState.from_dict({})
        self.assertEqual(restored.phase, Phase.INTAKE)
        self.assertFalse(restored.requires_agy_rereview)

    def test_from_dict_preserves_all_fields(self):
        state = FlowState(
            phase=Phase.AGY_REVIEW,
            ux_loops=1, eng_loops=2, total_steps=7,
            task_description="test", report_path="/r.md",
            verdicts=[{"role": "dev", "token": "PASS", "time": 0}],
            halt_reason="", closure_summary={},
            requires_agy_rereview=True,
        )
        d = state.to_dict()
        restored = FlowState.from_dict(d)
        self.assertEqual(restored.phase, Phase.AGY_REVIEW)
        self.assertEqual(restored.ux_loops, 1)
        self.assertEqual(restored.eng_loops, 2)
        self.assertEqual(restored.total_steps, 7)
        self.assertEqual(restored.task_description, "test")
        self.assertEqual(restored.report_path, "/r.md")
        self.assertEqual(len(restored.verdicts), 1)
        self.assertTrue(restored.requires_agy_rereview)


# ---------------------------------------------------------------------------
# 16. Session engine wiring integration tests
# ---------------------------------------------------------------------------

class TestFlowCoordinatorWiring(unittest.TestCase):
    """Integration tests: session engine drives flow_coordinator via _advance."""

    SANDBOX_TEMPLATE = {
        "id": "sandbox-bakery-flow",
        "name": "Sandbox Bakery Flow",
        "flow_coordinator": True,
        "sandbox_only": True,
        "roles": ["developer", "ui_lead", "codex_reviewer"],
        "phases": [
            {"name": "Developer Implementation",
             "participants": ["developer"],
             "prompt": "Implement.", "turn_order": "sequential"},
            {"name": "UI/UX Review",
             "participants": ["ui_lead"],
             "prompt": "Review UX.", "turn_order": "sequential"},
            {"name": "Codex Code Review",
             "participants": ["codex_reviewer"],
             "prompt": "Review code.", "turn_order": "sequential",
             "is_output": True},
        ],
    }

    def _make_engine(self, cast=None):
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeMessageStore, _FakeAgentTrigger, _FakeRegistry,
        )
        from session_store import SessionStore
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates[self.SANDBOX_TEMPLATE["id"]] = self.SANDBOX_TEMPLATE

        messages = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        agents_map = {
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex": {"name": "codex", "base": "codex"},
        }
        registry = _FakeRegistry(agents_map)
        engine = SessionEngine(store, messages, trigger, registry=registry)

        if cast is None:
            cast = {
                "developer": "claude",
                "ui_lead": "agy",
                "codex_reviewer": "codex",
            }

        return engine, store, messages, trigger, cast

    def _start_session(self, engine, store, cast, goal="Bakery POS checkout modal UX"):
        session = engine.start_session(
            "sandbox-bakery-flow", "sandbox", cast, "user", goal=goal)
        return session

    def _simulate_agent_message(self, engine, store, channel, sender, text):
        """Simulate an agent posting a message and the engine advancing."""
        session = store.get_active(channel)
        if not session:
            return None
        session["_last_msg"] = {"text": text, "id": 999, "sender": sender,
                                "type": "chat", "channel": channel}
        engine._advance(session, 999)
        return store.get_active(channel) or store.get(session["id"])

    def test_start_initializes_flow_state(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        self.assertIsNotNone(session)
        persisted = store.get(session["id"])
        self.assertIn("flow_state", persisted)
        fs = persisted["flow_state"]
        self.assertEqual(fs["phase"], "dev_work")
        self.assertEqual(fs["task_description"],
                         "Bakery POS checkout modal UX")

    def test_start_triggers_developer(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        self._start_session(engine, store, cast)
        self.assertTrue(any(t["agent"] == "claude" for t in trigger.triggered))

    def test_safety_block_at_intake(self):
        """Test D: Twinpet task halts at intake."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(
            engine, store, cast, goal="Fix twinpet-pos checkout")
        persisted = store.get(session["id"])
        self.assertEqual(persisted["state"], "interrupted")
        self.assertIn("blocked production path",
                      persisted.get("interrupt_reason", ""))

    def test_happy_path_full_wiring(self):
        """Test C-like: Dev → AGY → Codex → Closure through wired engine."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        # Developer responds
        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        s = store.get(sid)
        self.assertEqual(s["flow_state"]["phase"], "agy_review")
        self.assertEqual(s["current_phase"], 1)  # ui_lead phase

        # AGY responds
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")
        s = store.get(sid)
        self.assertEqual(s["flow_state"]["phase"], "codex_review")
        self.assertEqual(s["current_phase"], 2)  # codex_reviewer phase

        # Codex responds
        self._simulate_agent_message(
            engine, store, "sandbox", "codex", "PASS")
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")
        self.assertEqual(s["flow_state"]["phase"], "closure")

    def test_agy_fail_loop_wiring(self):
        """Test A: AGY fail routes back to Developer through wired engine."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        # Developer ready
        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        s = store.get(sid)
        self.assertEqual(s["current_phase"], 1)  # ui_lead

        # AGY fails
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "REQUEST UX CHANGES\nFix spacing")
        s = store.get(sid)
        self.assertEqual(s["flow_state"]["phase"], "dev_work")
        self.assertEqual(s["current_phase"], 0)  # back to developer
        self.assertEqual(s["flow_state"]["ux_loops"], 1)

        # Developer fixes
        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        s = store.get(sid)
        self.assertEqual(s["current_phase"], 1)  # ui_lead again

        # AGY passes
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")
        s = store.get(sid)
        self.assertEqual(s["current_phase"], 2)  # codex

        # Codex passes
        self._simulate_agent_message(
            engine, store, "sandbox", "codex", "PASS")
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")

    def test_codex_engineering_fail_skips_agy(self):
        """Test B: Codex eng-only fail → Developer → Codex (skip AGY)."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")

        # Codex fails with engineering-only issue
        self._simulate_agent_message(
            engine, store, "sandbox", "codex",
            "REQUEST CHANGES\nMissing boundary validation test")
        s = store.get(sid)
        self.assertEqual(s["flow_state"]["phase"], "dev_work")
        self.assertEqual(s["current_phase"], 0)

        # Developer fixes and says ready for Codex directly
        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_CODEX_REVIEW")
        s = store.get(sid)
        self.assertEqual(s["current_phase"], 2)  # straight to codex

        # Codex passes
        self._simulate_agent_message(
            engine, store, "sandbox", "codex", "PASS")
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")

    def test_codex_ui_fail_requires_agy_rereview(self):
        """Test C: Codex UI fail → Developer → AGY (re-review) → Codex."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")

        # Codex fails with UI/UX issue
        self._simulate_agent_message(
            engine, store, "sandbox", "codex",
            "REQUEST CHANGES\nButton layout needs rework with CSS spacing")
        s = store.get(sid)
        self.assertTrue(s["flow_state"]["requires_agy_rereview"])
        self.assertEqual(s["current_phase"], 0)  # back to developer

        # Developer tries to skip to Codex but must go through AGY
        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_CODEX_REVIEW")
        s = store.get(sid)
        self.assertEqual(s["current_phase"], 1)  # forced to AGY

        # AGY re-review passes
        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")
        s = store.get(sid)
        self.assertFalse(s["flow_state"]["requires_agy_rereview"])
        self.assertEqual(s["current_phase"], 2)  # now to codex

        # Codex passes
        self._simulate_agent_message(
            engine, store, "sandbox", "codex", "PASS")
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")

    def test_ambiguous_verdict_halts(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "I'm done with the work")
        s = store.get(sid)
        self.assertEqual(s["state"], "interrupted")
        self.assertIn("flow coordinator", s.get("interrupt_reason", ""))

    def test_linear_template_not_affected(self):
        """Existing linear templates must not use flow coordinator path."""
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeMessageStore, _FakeAgentTrigger, _FakeRegistry,
        )
        from session_store import SessionStore
        import tempfile

        linear_tmpl = {
            "id": "linear-test",
            "name": "Linear Test",
            "roles": ["writer"],
            "phases": [
                {"name": "Write", "participants": ["writer"],
                 "prompt": "Write.", "turn_order": "sequential",
                 "is_output": True},
            ],
        }

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates["linear-test"] = linear_tmpl
        msgs = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        reg = _FakeRegistry({"codex": {"name": "codex", "base": "codex"}})
        engine = SessionEngine(store, msgs, trigger, registry=reg)

        session = engine.start_session(
            "linear-test", "general", {"writer": "codex"}, "user")
        self.assertIsNotNone(session)
        persisted = store.get(session["id"])
        self.assertNotIn("flow_state", persisted)

    def test_flow_state_persists_across_steps(self):
        """FlowState survives through multiple advances via store persistence."""
        engine, store, msgs, trigger, cast = self._make_engine()
        session = self._start_session(engine, store, cast)
        sid = session["id"]

        self._simulate_agent_message(
            engine, store, "sandbox", "claude", "READY_FOR_AGY_REVIEW")
        s1 = store.get(sid)
        fs1 = s1["flow_state"]
        self.assertEqual(fs1["phase"], "agy_review")
        self.assertEqual(fs1["total_steps"], 2)

        self._simulate_agent_message(
            engine, store, "sandbox", "agy", "PASS")
        s2 = store.get(sid)
        fs2 = s2["flow_state"]
        self.assertEqual(fs2["phase"], "codex_review")
        self.assertEqual(fs2["total_steps"], 3)
        self.assertEqual(len(fs2["verdicts"]), 2)


# ---------------------------------------------------------------------------
# 17. Finding 1 — _route_flow_action uses fresh session record
# ---------------------------------------------------------------------------

class TestRouteFlowActionFreshSession(unittest.TestCase):
    """Verify _route_flow_action reloads the session from store so
    _trigger_current receives a record with the updated phase/turn."""

    SANDBOX_TEMPLATE = TestFlowCoordinatorWiring.SANDBOX_TEMPLATE

    def _make_engine(self):
        return TestFlowCoordinatorWiring._make_engine(self)

    def _start_and_get(self, engine, store, cast):
        session = engine.start_session(
            "sandbox-bakery-flow", "sandbox", cast, "user",
            goal="Bakery POS checkout modal UX")
        return session

    def _simulate(self, engine, store, sender, text):
        session = store.get_active("sandbox")
        if not session:
            return None
        session["_last_msg"] = {"text": text, "id": 999, "sender": sender,
                                "type": "chat", "channel": "sandbox"}
        engine._advance(session, 999)
        return store.get_active("sandbox") or store.get(session["id"])

    def test_trigger_current_sees_updated_phase_after_dev_to_agy(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        self._start_and_get(engine, store, cast)
        trigger.triggered.clear()

        self._simulate(engine, store, "claude", "READY_FOR_AGY_REVIEW")
        # The trigger must have dispatched AGY (ui_lead phase = 1)
        agy_triggers = [t for t in trigger.triggered if t["agent"] == "agy"]
        self.assertTrue(len(agy_triggers) > 0, "AGY was not triggered")
        s = store.get_active("sandbox")
        self.assertEqual(s["current_phase"], 1)

    def test_trigger_current_sees_updated_phase_agy_fail_back_to_dev(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        self._start_and_get(engine, store, cast)

        self._simulate(engine, store, "claude", "READY_FOR_AGY_REVIEW")
        trigger.triggered.clear()

        self._simulate(engine, store, "agy", "REQUEST UX CHANGES\nFix spacing")
        dev_triggers = [t for t in trigger.triggered if t["agent"] == "claude"]
        self.assertTrue(len(dev_triggers) > 0, "Developer was not triggered")
        s = store.get_active("sandbox")
        self.assertEqual(s["current_phase"], 0)

    def test_trigger_current_sees_updated_phase_codex_eng_fail_back(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        self._start_and_get(engine, store, cast)

        self._simulate(engine, store, "claude", "READY_FOR_AGY_REVIEW")
        self._simulate(engine, store, "agy", "PASS")
        trigger.triggered.clear()

        self._simulate(engine, store, "codex",
                       "REQUEST CHANGES\nMissing validation test")
        dev_triggers = [t for t in trigger.triggered if t["agent"] == "claude"]
        self.assertTrue(len(dev_triggers) > 0)
        s = store.get_active("sandbox")
        self.assertEqual(s["current_phase"], 0)

    def test_trigger_current_sees_updated_phase_codex_ui_fail_reroute(self):
        engine, store, msgs, trigger, cast = self._make_engine()
        self._start_and_get(engine, store, cast)

        self._simulate(engine, store, "claude", "READY_FOR_AGY_REVIEW")
        self._simulate(engine, store, "agy", "PASS")
        self._simulate(engine, store, "codex",
                       "REQUEST CHANGES\nButton layout needs CSS spacing fix")
        trigger.triggered.clear()

        # Developer tries READY_FOR_CODEX_REVIEW but requires_agy_rereview forces AGY
        self._simulate(engine, store, "claude", "READY_FOR_CODEX_REVIEW")
        agy_triggers = [t for t in trigger.triggered if t["agent"] == "agy"]
        self.assertTrue(len(agy_triggers) > 0, "AGY re-review was not triggered")
        s = store.get_active("sandbox")
        self.assertEqual(s["current_phase"], 1)


# ---------------------------------------------------------------------------
# 18. Finding 2 — Missing flow_state fail-closes
# ---------------------------------------------------------------------------

class TestMissingFlowStateFailClosed(unittest.TestCase):
    """If a flow_coordinator template's session is missing flow_state,
    _advance must fail closed (interrupt), not fall through to linear."""

    SANDBOX_TEMPLATE = TestFlowCoordinatorWiring.SANDBOX_TEMPLATE

    def _make_engine_and_session_without_flow_state(self):
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeMessageStore, _FakeAgentTrigger, _FakeRegistry,
        )
        from session_store import SessionStore
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates[self.SANDBOX_TEMPLATE["id"]] = self.SANDBOX_TEMPLATE

        msgs = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        agents_map = {
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex": {"name": "codex", "base": "codex"},
        }
        registry = _FakeRegistry(agents_map)
        engine = SessionEngine(store, msgs, trigger, registry=registry)

        # Manually create a session WITHOUT flow_state to simulate persistence failure
        with store._lock:
            session = {
                "id": 99,
                "template_id": "sandbox-bakery-flow",
                "template_name": "Sandbox Bakery Flow",
                "channel": "sandbox",
                "cast": {"developer": "claude", "ui_lead": "agy",
                         "codex_reviewer": "codex"},
                "state": "waiting",
                "current_phase": 0,
                "current_turn": 0,
                "started_by": "user",
                "started_at": 0, "updated_at": 0,
                "last_message_id": None,
                "output_message_id": None,
                "goal": "Bakery checkout",
                # flow_state intentionally MISSING
            }
            store._sessions.append(session)
            store._next_id = 100
            store._save()

        return engine, store, msgs, trigger

    def test_missing_flow_state_interrupts(self):
        engine, store, msgs, trigger = self._make_engine_and_session_without_flow_state()
        session = store.get(99)
        self.assertNotIn("flow_state", session)

        session["_last_msg"] = {"text": "READY_FOR_AGY_REVIEW", "id": 1,
                                "sender": "claude", "type": "chat",
                                "channel": "sandbox"}
        engine._advance(session, 1)

        s = store.get(99)
        self.assertEqual(s["state"], "interrupted")
        self.assertIn("missing flow_state", s.get("interrupt_reason", ""))

    def test_missing_flow_state_no_linear_advance(self):
        """Must NOT advance turn/phase through the linear path."""
        engine, store, msgs, trigger = self._make_engine_and_session_without_flow_state()
        session = store.get(99)
        session["_last_msg"] = {"text": "READY_FOR_AGY_REVIEW", "id": 1,
                                "sender": "claude", "type": "chat",
                                "channel": "sandbox"}
        engine._advance(session, 1)

        s = store.get(99)
        # Phase/turn must NOT have advanced (linear would increment)
        self.assertEqual(s["current_phase"], 0)
        self.assertEqual(s["current_turn"], 0)

    def test_missing_flow_state_no_agent_dispatch(self):
        """No agent should be triggered when flow_state is missing."""
        engine, store, msgs, trigger = self._make_engine_and_session_without_flow_state()
        trigger.triggered.clear()
        session = store.get(99)
        session["_last_msg"] = {"text": "READY_FOR_AGY_REVIEW", "id": 1,
                                "sender": "claude", "type": "chat",
                                "channel": "sandbox"}
        engine._advance(session, 1)

        self.assertEqual(len(trigger.triggered), 0,
                         "No agent should be triggered on missing flow_state")

    def test_non_flow_template_still_linear(self):
        """Non-flow-coordinator templates must still use standard linear advance."""
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeSessionStore, _FakeMessageStore,
            _FakeAgentTrigger, _FakeRegistry,
        )
        linear_tmpl = {
            "id": "linear",
            "name": "Linear",
            "phases": [
                {"name": "Write", "participants": ["writer"],
                 "prompt": "Write.", "is_output": True},
                {"name": "Review", "participants": ["reviewer"],
                 "prompt": "Review.", "is_output": False},
            ],
        }
        session = {
            "id": 1, "template_id": "linear", "channel": "general",
            "cast": {"writer": "codex", "reviewer": "codexsafe"},
            "state": "waiting", "current_phase": 0, "current_turn": 0,
        }
        agents_map = {
            "codex": {"name": "codex", "base": "codex"},
            "codexsafe": {"name": "codexsafe", "base": "codexsafe"},
        }
        store = _FakeSessionStore(
            sessions=[session], templates={"linear": linear_tmpl})
        trigger = _FakeAgentTrigger()
        engine = SessionEngine(store, _FakeMessageStore(), trigger,
                               registry=_FakeRegistry(agents_map))

        session["_last_msg"] = {"text": "some output", "id": 1}
        engine._advance(session, 1)
        # Linear template should advance (not interrupt)
        self.assertEqual(len(store.interrupted), 0)
        self.assertTrue(len(store.advanced_phases) > 0 or
                        len(store.advanced_turns) > 0 or
                        len(store.completed) > 0)


if __name__ == "__main__":
    unittest.main()
