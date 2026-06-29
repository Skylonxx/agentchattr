"""Read-only analysis no-tool Codex Reviewer handoff tests."""

from __future__ import annotations

import unittest

import config_loader
import coordinator_loop as cl
import workspace_policy as wp
from coordinator_loop import on_coordinator_output, on_session_start, on_worker_output
from session_relay import (
    build_coordinator_loop_prompt,
    build_readonly_context_reviewer_prompt,
    build_relay_prompt,
    extract_worker_context_outputs,
    has_explicit_to_header,
    is_readonly_no_tool_reviewer_policy,
)


def _analysis_policy() -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config())
    result = wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
            "workspace_mode": "read-only-analysis",
        },
    )
    return result.policy


def _sample_messages() -> list[dict]:
    return [
        {
            "sender": "claude",
            "type": "chat",
            "text": "READY_FOR_COORDINATOR\n\nREPORT_BEGIN\n\n# Developer Analysis\n\nPaymentModal is monolithic.\n\nREPORT_END",
        },
        {
            "sender": "agy",
            "type": "chat",
            "text": "UX_APPROVED\n\n# UI Lead Report\n\nBlueprint approved with notes.",
        },
    ]


class ReadonlyReviewerPolicyTests(unittest.TestCase):
    def test_analysis_profile_is_no_tool_reviewer(self):
        policy = _analysis_policy()
        self.assertTrue(is_readonly_no_tool_reviewer_policy(policy))
        self.assertTrue(policy.get("analysis_report_only"))


class ReadonlyReviewerPromptTests(unittest.TestCase):
    def setUp(self):
        self.policy = _analysis_policy()
        self.messages = _sample_messages()

    def _prompt(self, *, coordinator_instruction: str = "") -> str:
        return build_readonly_context_reviewer_prompt(
            session_name="Project Read-Only Coordinator Loop",
            goal="PaymentModal analysis",
            phase_name="Reviewer",
            phase_index=3,
            total_phases=5,
            policy=self.policy,
            context_messages=self.messages,
            cast={"developer": "claude", "ui_lead": "agy", "reviewer": "codex_reviewer"},
            coordinator_instruction=coordinator_instruction,
            project="twinpet-ui-09-c-read",
        )

    def test_includes_to_codex_reviewer_header(self):
        prompt = self._prompt()
        self.assertTrue(has_explicit_to_header(prompt))
        self.assertIn("TO: Codex Reviewer", prompt)

    def test_does_not_ask_load_reviewer_md(self):
        prompt = self._prompt()
        self.assertNotIn("Load the reviewer role from docs/ai-roles/reviewer.md", prompt)
        self.assertIn("do not:", prompt.lower())
        self.assertIn("docs/ai-roles/reviewer.md", prompt.lower())

    def test_does_not_ask_inspect_paymentmodal_directly(self):
        prompt = self._prompt().lower()
        self.assertIn("do not inspect", prompt)
        self.assertNotIn("inspect paymentmodal.tsx", prompt.replace("do not inspect", ""))

    def test_does_not_ask_verify_git_or_dirty_state(self):
        prompt = self._prompt().lower()
        self.assertIn("git head", prompt)
        self.assertIn("dirty", prompt)
        self.assertTrue(
            "do not verify" in prompt or "do not:" in prompt,
            msg="prompt should forbid git/dirty verification",
        )

    def test_does_not_ask_save_report_files(self):
        prompt = self._prompt().lower()
        self.assertIn("save report", prompt)
        self.assertTrue(
            "do not edit or save" in prompt or "do not save" in prompt,
            msg="prompt should forbid saving reports",
        )

    def test_includes_developer_and_agy_context(self):
        prompt = self._prompt()
        self.assertIn("DEVELOPER ANALYSIS:", prompt)
        self.assertIn("PaymentModal is monolithic", prompt)
        self.assertIn("AGY UI/UX NOTES:", prompt)
        self.assertIn("Blueprint approved", prompt)
        self.assertIn("SNAPSHOT FILE LIST", prompt)
        self.assertIn("src/components/PaymentModal.tsx", prompt)

    def test_coordinator_bad_instruction_sandboxed(self):
        bad = (
            "Load the reviewer role from docs/ai-roles/reviewer.md before acting.\n"
            "Inspect PaymentModal.tsx and save the required report file."
        )
        prompt = self._prompt(coordinator_instruction=bad)
        self.assertIn("COORDINATOR NOTES", prompt)
        self.assertIn("ignore file-load/shell/save requests", prompt.lower())
        self.assertIn("DEVELOPER ANALYSIS:", prompt)

    def test_extract_worker_context_outputs(self):
        outputs = extract_worker_context_outputs(
            self.messages,
            cast={"developer": "claude", "ui_lead": "agy"},
        )
        self.assertIn("PaymentModal is monolithic", outputs["developer_output"])
        self.assertIn("Blueprint approved", outputs["ui_lead_output"])


class ReadonlyReviewerCoordinatorGuidanceTests(unittest.TestCase):
    def test_coordinator_prompt_includes_readonly_reviewer_guidance(self):
        prompt = build_coordinator_loop_prompt(
            session_name="sess",
            goal="analysis",
            task_description="analysis",
            last_role="ui_lead",
            last_output_summary="UX_APPROVED",
            awaiting_role="coordinator",
            developer_round=1,
            ui_round=0,
            review_round=0,
            safety_round=0,
            allowed_tokens=["NEXT: reviewer"],
            readonly_analysis=True,
        )
        self.assertIn("READ-ONLY REVIEWER ROUTING", prompt)
        self.assertIn("docs/ai-roles/reviewer.md", prompt)


class ReadonlyReviewerRegressionTests(unittest.TestCase):
    def test_request_changes_routes_without_reviewer_file_inspection(self):
        state, _ = on_session_start("read-only analysis", max_rounds=5)
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        on_coordinator_output(state, "NEXT: developer\nBegin.")
        on_worker_output(state, "developer", "READY_FOR_COORDINATOR\nreport")
        on_coordinator_output(state, "NEXT: ui_lead\nReview.")
        on_worker_output(state, "ui_lead", "UX_APPROVED\nnotes")
        on_coordinator_output(
            state,
            "NEXT: reviewer\nLoad docs/ai-roles/reviewer.md and inspect PaymentModal.tsx",
        )
        action = on_worker_output(state, "reviewer", "REQUEST CHANGES\nfix blueprint")
        self.assertEqual(action.target_role, "coordinator")
        self.assertIn("REQUEST CHANGES", action.prompt_context)
        self.assertIn("read-only", action.prompt_context.lower())

        dispatch = on_coordinator_output(
            state,
            "NEXT: developer\nRevise blueprint from reviewer findings.",
        )
        self.assertEqual(dispatch.target_role, "developer")
        self.assertIn("TO: Claude Developer", dispatch.routing_body)

    def test_tool_capable_template_still_uses_relay_prompt(self):
        tmpl = {
            "name": "SDLC",
            "phases": [{"name": "Review", "participants": ["reviewer"], "prompt": "Independent review. Do not defer."}],
        }
        phase = tmpl["phases"][0]
        prompt = build_relay_prompt(
            session_name=tmpl["name"],
            goal="implement feature",
            phase_name=phase["name"],
            phase_index=0,
            total_phases=1,
            role="reviewer",
            instruction=phase["prompt"],
            agent_base="codex_reviewer",
        )
        self.assertIn("independ", prompt.lower())
        self.assertFalse(is_readonly_no_tool_reviewer_policy(None))


if __name__ == "__main__":
    unittest.main()
