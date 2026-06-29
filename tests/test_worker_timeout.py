"""Tests for Claude print worker timeout resolution and diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config_loader
import workspace_policy as wp
from coordinator_loop import CoordinatorLoopState, on_worker_output
from worker_timeout import (
    DEFAULT_TIMEOUT_SECS,
    DOCS_ONLY_TIMEOUT_SECS,
    IMPLEMENTATION_TIMEOUT_SECS,
    PROMPT_MEMO_TIMEOUT_SECS,
    build_timeout_diagnostics,
    format_worker_timeout_reply,
    resolve_claude_print_timeout,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "752ed1317a5e0b83b872d563cda451c7621ed22e"


def _analysis_session_record(session_id: int = 1) -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
    result = wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
            "workspace_mode": "read-only-analysis",
            "expected_head": EXPECTED_HEAD,
        },
    )
    fields = wp.build_session_workspace_policy_fields(result.policy)
    return {
        "id": session_id,
        "prompt_body": "PROMPT ID: TEST\nMODE: READ-ONLY\n" + ("x" * 2000),
        "prompt_id": "TEST-READ-ONLY-001",
        "goal": "short",
        **fields,
    }


def _write_session_runs(data_dir: Path, sessions: list[dict]):
    (data_dir / "session_runs.json").write_text(
        json.dumps(sessions), encoding="utf-8",
    )


class TimeoutResolutionTests(unittest.TestCase):
    def test_default_timeout_for_simple_item(self):
        secs = resolve_claude_print_timeout({"prompt": "hi"}, config={})
        self.assertEqual(secs, DEFAULT_TIMEOUT_SECS)

    def test_long_prompt_without_workspace_gets_extended(self):
        secs = resolve_claude_print_timeout(
            {"prompt": "x" * 2000},
            config=config_loader.load_config(ROOT),
        )
        self.assertEqual(secs, PROMPT_MEMO_TIMEOUT_SECS)

    def test_docs_only_session_gets_extended_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_session_runs(data_dir, [_analysis_session_record()])
            item = {
                "prompt": "FULL TASK MEMO",
                "workspace_policy_context": {
                    "session_id": 1,
                    "session_role": "developer",
                    "policy_mode": "read-only",
                    "policy_id": "twinpet-ui-09-c-payment-modal-analysis",
                    "workspace_root": "C:/Users/Narachat/twinpet-pos",
                    "has_prompt_body": True,
                },
            }
            secs = resolve_claude_print_timeout(
                item, config=config_loader.load_config(ROOT), data_dir=data_dir,
            )
            self.assertEqual(secs, DOCS_ONLY_TIMEOUT_SECS)

    def test_implementation_profile_gets_900s(self):
        item = {
            "prompt": "work",
            "workspace_policy_context": {
                "session_role": "developer",
                "policy_mode": "implementation",
            },
        }
        secs = resolve_claude_print_timeout(item, config=config_loader.load_config(ROOT))
        self.assertEqual(secs, IMPLEMENTATION_TIMEOUT_SECS)

    def test_config_overrides_loaded(self):
        cfg = config_loader.load_config(ROOT)
        timeouts = config_loader.get_session_worker_timeouts(cfg)
        self.assertGreaterEqual(timeouts["prompt_memo_secs"], 600)
        self.assertGreaterEqual(timeouts["docs_only_secs"], 600)


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_include_role_profile_mode_cwd(self):
        diag = build_timeout_diagnostics(
            agent="claude",
            role="developer",
            timeout_secs=600,
            cwd="C:/Users/Narachat/twinpet-pos",
            item={
                "workspace_policy_context": {
                    "session_role": "developer",
                    "policy_mode": "read-only",
                },
            },
            session={
                "prompt_id": "TEST-001",
                "prompt_body": "memo",
                "workspace_policy": {"policy_id": "twinpet-ui-09-c-payment-modal-analysis", "mode": "read-only"},
            },
        )
        self.assertEqual(diag["status"], "WORKER_TIMEOUT")
        self.assertEqual(diag["role"], "developer")
        self.assertEqual(diag["workspace_profile"], "twinpet-ui-09-c-payment-modal-analysis")
        self.assertEqual(diag["workspace_mode"], "read-only")
        self.assertEqual(diag["cwd"], "C:/Users/Narachat/twinpet-pos")
        self.assertTrue(diag["prompt_body_mode"])

    def test_formatted_reply_starts_with_worker_timeout(self):
        diag = build_timeout_diagnostics(
            agent="claude",
            role="developer",
            timeout_secs=600,
            cwd="C:/Users/Narachat/twinpet-pos",
            item={},
            session={},
        )
        text = format_worker_timeout_reply(
            diag,
            workspace_check={"git_status_short": "(clean)", "git_head": EXPECTED_HEAD},
        )
        self.assertTrue(text.startswith("WORKER_TIMEOUT"))
        self.assertIn("timed out after 600s", text)
        self.assertIn("workspace_profile", text)
        self.assertIn("retry safe", text)
        self.assertIn(EXPECTED_HEAD, text)


class CoordinatorWorkerTimeoutTests(unittest.TestCase):
    def test_developer_worker_timeout_routes_to_coordinator_not_blocker(self):
        state = CoordinatorLoopState()
        state.awaiting_role = "developer"
        action = on_worker_output(state, "developer", "WORKER_TIMEOUT\ninfra timeout\n")
        self.assertEqual(action.target_role, "coordinator")
        self.assertIn("WORKER_TIMEOUT", action.prompt_context or "")


if __name__ == "__main__":
    unittest.main()
