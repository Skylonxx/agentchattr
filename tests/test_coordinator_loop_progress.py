"""Coordinator loop progress output handling tests."""

from __future__ import annotations

import unittest

from coordinator_loop import (
    CoordinatorLoopState,
    _parse_developer_verdict,
    build_ambiguous_worker_diagnostic,
    on_worker_output,
)


class DeveloperProgressParsingTests(unittest.TestCase):
    def test_explicit_progress_accepted(self):
        token, notes = _parse_developer_verdict("PROGRESS\nRunning prechecks.\n")
        self.assertEqual(token, "PROGRESS")
        self.assertIn("Running prechecks", notes)

    def test_starting_prechecks_legacy_phrase_is_progress(self):
        text = "Starting prechecks: verifying workspace, Git HEAD, and working tree status."
        token, notes = _parse_developer_verdict(text)
        self.assertEqual(token, "PROGRESS")
        self.assertIn("Starting prechecks", notes)

    def test_ready_for_coordinator_still_works(self):
        token, _ = _parse_developer_verdict("READY_FOR_COORDINATOR\nDone.\n")
        self.assertEqual(token, "READY_FOR_COORDINATOR")

    def test_blocker_still_blocks(self):
        token, notes = _parse_developer_verdict("BLOCKER: wrong cwd\n")
        self.assertEqual(token, "BLOCKER")
        self.assertIn("wrong cwd", notes)

    def test_worker_timeout_still_recognized(self):
        token, _ = _parse_developer_verdict("WORKER_TIMEOUT\ninfra\n")
        self.assertEqual(token, "WORKER_TIMEOUT")

    def test_nonsense_still_ambiguous(self):
        token, notes = _parse_developer_verdict("???\n")
        self.assertEqual(token, "AMBIGUOUS")
        self.assertEqual(notes, "???")

    def test_empty_still_ambiguous(self):
        token, notes = _parse_developer_verdict("")
        self.assertEqual(token, "AMBIGUOUS")
        self.assertEqual(notes, "empty developer output")


class DeveloperProgressRoutingTests(unittest.TestCase):
    def _dev_state(self) -> CoordinatorLoopState:
        state = CoordinatorLoopState()
        state.awaiting_role = "developer"
        return state

    def test_progress_routes_to_coordinator_not_terminal(self):
        state = self._dev_state()
        action = on_worker_output(
            state,
            "developer",
            "Starting prechecks: verifying workspace, Git HEAD, and working tree status.",
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")
        self.assertIn("PROGRESS", action.prompt_context)
        self.assertEqual(state.developer_round, 0)

    def test_explicit_progress_routes_safely(self):
        state = self._dev_state()
        action = on_worker_output(state, "developer", "PROGRESS\nReading PaymentModal.tsx\n")
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")

    def test_blocker_terminates(self):
        state = self._dev_state()
        action = on_worker_output(state, "developer", "BLOCKER: expected HEAD mismatch\n")
        self.assertTrue(action.is_terminal)
        self.assertEqual(action.terminal_kind, "blocker")

    def test_ambiguous_includes_diagnostics(self):
        state = self._dev_state()
        ctx = {
            "policy_id": "twinpet-ui-09-c-payment-modal-analysis",
            "policy_mode": "docs-only",
            "prompt_id": "TEST-001",
            "has_prompt_body": True,
            "workspace_policy": {
                "policy_id": "twinpet-ui-09-c-payment-modal-analysis",
                "mode": "docs-only",
            },
        }
        action = on_worker_output(state, "developer", "???", worker_context=ctx)
        self.assertTrue(action.is_terminal)
        self.assertIn("workspace_profile", action.prompt_context)
        self.assertIn("twinpet-ui-09-c-payment-modal-analysis", action.prompt_context)
        self.assertIn("prompt_body present: yes", action.prompt_context)

    def test_read_only_analysis_precheck_text_not_ambiguous(self):
        state = self._dev_state()
        memo_progress = (
            "Starting prechecks: verifying workspace, Git HEAD, and working tree status.\n"
            "Reading src/components/PaymentModal.tsx"
        )
        action = on_worker_output(
            state,
            "developer",
            memo_progress,
            worker_context={
                "workspace_policy": {
                    "policy_id": "twinpet-ui-09-c-payment-modal-analysis",
                    "mode": "docs-only",
                },
                "has_prompt_body": True,
            },
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")


class AmbiguousDiagnosticTests(unittest.TestCase):
    def test_looks_like_progress_flag(self):
        diag = build_ambiguous_worker_diagnostic(
            "developer",
            "Starting prechecks: verifying workspace",
            worker_context={"has_prompt_body": True},
        )
        self.assertIn("looked like progress: yes", diag)


if __name__ == "__main__":
    unittest.main()
