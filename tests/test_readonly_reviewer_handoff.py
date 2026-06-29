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
    build_readonly_reviewer_context_packet,
    build_relay_prompt,
    compress_developer_analysis_for_reviewer,
    extract_reviewer_context_from_verdict_log,
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


def _substantial_developer_text(extra: str = "") -> str:
    body = (
        "# PaymentModal analysis\n\n"
        "Current implementation uses monolithic props/state for payment data flow.\n"
        "Blueprint constraints: split tender UI from inventory risk handling.\n"
        "Critical risks: duplicate source-of-truth across Firestore transaction paths.\n"
        f"{extra}"
    )
    return f"READY_FOR_COORDINATOR\n\n{body}"


def _sample_messages() -> list[dict]:
    return [
        {
            "sender": "claude",
            "type": "chat",
            "text": _substantial_developer_text("PaymentModal is monolithic."),
        },
        {
            "sender": "agy",
            "type": "chat",
            "text": "UX_APPROVED\n\n# UI Lead Report\n\nBlueprint approved with notes.",
        },
    ]


def _packet_kwargs(**overrides):
    policy = _analysis_policy()
    base = dict(
        session_name="Project Read-Only Coordinator Loop",
        goal="PaymentModal analysis",
        phase_name="Reviewer",
        phase_index=3,
        total_phases=5,
        policy=policy,
        context_messages=_sample_messages(),
        cast={"developer": "claude", "ui_lead": "agy", "reviewer": "codex_reviewer"},
        project="twinpet-ui-09-c-read",
    )
    base.update(overrides)
    return base


class ReadonlyReviewerPolicyTests(unittest.TestCase):
    def test_analysis_profile_is_no_tool_reviewer(self):
        policy = _analysis_policy()
        self.assertTrue(is_readonly_no_tool_reviewer_policy(policy))
        self.assertTrue(policy.get("analysis_report_only"))


class ReadonlyReviewerPromptTests(unittest.TestCase):
    def setUp(self):
        self.policy = _analysis_policy()
        self.messages = _sample_messages()

    def _prompt(self, *, coordinator_instruction: str = "", **kwargs) -> str:
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                coordinator_instruction=coordinator_instruction,
                **kwargs,
            )
        )
        self.assertTrue(packet.ok, msg=packet.blocker)
        return packet.prompt

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
        self.assertIn("SNAPSHOT SUMMARY:", prompt)
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


class ReadonlyReviewerContextPacketTests(unittest.TestCase):
    def test_ready_developer_included_from_verdict_log(self):
        dev = _substantial_developer_text("verdict log body")
        verdict_log = [
            {"role": "developer", "token": "READY_FOR_COORDINATOR", "notes": dev.split("\n", 1)[1]},
            {"role": "ui_lead", "token": "UX_APPROVED", "notes": "AGY notes from log"},
        ]
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                context_messages=[],
                verdict_log=verdict_log,
            )
        )
        self.assertTrue(packet.ok)
        self.assertIn("DEVELOPER ANALYSIS:", packet.prompt)
        self.assertIn("verdict log body", packet.prompt)
        self.assertIn("AGY UI/UX NOTES:", packet.prompt)
        self.assertIn("AGY notes from log", packet.prompt)
        self.assertTrue(packet.diagnostics["developer_analysis_found"])

    def test_agy_does_not_replace_developer_analysis(self):
        messages = [
            {
                "sender": "agy",
                "type": "chat",
                "text": "UX_APPROVED\n\nDeveloper addressed all concerns.",
            },
        ]
        dev = _substantial_developer_text("underlying developer analysis")
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                context_messages=messages,
                stored_developer_analysis=dev,
            )
        )
        self.assertTrue(packet.ok)
        self.assertIn("underlying developer analysis", packet.prompt)
        self.assertIn("Developer addressed all concerns", packet.prompt)

    def test_blocks_when_developer_analysis_missing(self):
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                context_messages=[
                    {
                        "sender": "agy",
                        "type": "chat",
                        "text": "UX_APPROVED\n\nDeveloper addressed issues.",
                    },
                ],
                verdict_log=[],
                stored_developer_analysis="",
            )
        )
        self.assertFalse(packet.ok)
        self.assertIn("BLOCKER: reviewer context missing developer analysis", packet.blocker)
        self.assertFalse(packet.diagnostics["developer_analysis_found"])
        self.assertTrue(packet.diagnostics["agy_notes_found"])

    def test_long_developer_analysis_compressed_not_omitted(self):
        long_body = _substantial_developer_text("x" * 30000)
        compressed, truncated = compress_developer_analysis_for_reviewer(long_body, max_chars=4000)
        self.assertTrue(truncated)
        self.assertIn("[DEVELOPER ANALYSIS COMPRESSED FROM FULL OUTPUT]", compressed)
        self.assertIn("PaymentModal", compressed)
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                context_messages=[],
                stored_developer_analysis=long_body,
            )
        )
        self.assertTrue(packet.ok)
        self.assertTrue(packet.diagnostics["truncated"])
        self.assertIn("DEVELOPER ANALYSIS:", packet.prompt)
        self.assertIn("COMPRESSED FROM FULL OUTPUT", packet.prompt)

    def test_rereview_after_agy_includes_developer_from_log(self):
        dev_notes = _substantial_developer_text("persistent analysis").split("\n", 1)[1]
        verdict_log = [
            {"role": "developer", "token": "READY_FOR_COORDINATOR", "notes": dev_notes},
            {"role": "ui_lead", "token": "UX_APPROVED", "notes": "round 1 AGY"},
            {"role": "reviewer", "token": "REQUEST CHANGES", "notes": "fix blueprint"},
            {"role": "ui_lead", "token": "UX_APPROVED", "notes": "round 2 AGY after fixes"},
        ]
        packet = build_readonly_reviewer_context_packet(
            **_packet_kwargs(
                context_messages=[
                    {
                        "sender": "agy",
                        "type": "chat",
                        "text": "UX_APPROVED\n\nround 2 AGY after fixes",
                    },
                ],
                verdict_log=verdict_log,
            )
        )
        self.assertTrue(packet.ok)
        self.assertIn("persistent analysis", packet.prompt)
        self.assertIn("REVIEW HISTORY:", packet.prompt)
        self.assertIn("REQUEST CHANGES", packet.prompt)

    def test_extract_reviewer_context_from_verdict_log(self):
        dev_notes = _substantial_developer_text().split("\n", 1)[1]
        ctx = extract_reviewer_context_from_verdict_log([
            {"role": "developer", "token": "READY_FOR_COORDINATOR", "notes": dev_notes},
            {"role": "reviewer", "token": "REQUEST CHANGES", "notes": "more detail"},
        ])
        self.assertIn("PaymentModal", ctx["developer_analysis"])
        self.assertEqual(len(ctx["review_history"]), 1)

    def test_legacy_prompt_wrapper_returns_blocker_when_incomplete(self):
        prompt = build_readonly_context_reviewer_prompt(
            **_packet_kwargs(
                context_messages=[],
                stored_developer_analysis="",
                verdict_log=[],
            )
        )
        self.assertIn("BLOCKER: reviewer context missing developer analysis", prompt)


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
        on_worker_output(state, "developer", _substantial_developer_text("report body"))
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
        self.assertTrue(state.last_developer_analysis)

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
