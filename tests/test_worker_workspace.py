"""Workspace worker context, cwd, and tool-call leakage tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config_loader
import workspace_policy as wp
import workspace_policy_runtime as wpr
from worker_timeout import (
    DOCS_ONLY_TIMEOUT_SECS,
    build_timeout_diagnostics,
    resolve_claude_print_timeout,
)
from worker_workspace import (
    detect_tool_call_leakage,
    format_tool_call_leakage_blocker,
    is_workspace_bound_queue_item,
    resolve_workspace_exec_cwd_or_blocker,
    run_workspace_precheck,
)

ROOT = Path(__file__).resolve().parents[1]
TWINPET = "C:/Users/Narachat/twinpet-pos"
EXPECTED_HEAD = "752ed1317a5e0b83b872d563cda451c7621ed22e"
SCRATCH = "C:/tools/agentchattr-scratch"


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
        "prompt_body": "PROMPT ID: TEST\n" + ("x" * 2000),
        "prompt_id": "TWINPET-UI-09-C-READONLY-ANALYSIS-BLUEPRINT-001",
        "goal": "short",
        **fields,
    }


def _analysis_queue_item(session_id: int = 1, *, data_dir: Path | None = None) -> dict:
    session = _analysis_session_record(session_id)
    if data_dir is not None:
        (data_dir / "session_runs.json").write_text(json.dumps([session]), encoding="utf-8")
    ctx = wpr.build_session_queue_workspace_context(session, "developer", 0, 0)
    return {
        "prompt": "FULL TASK MEMO",
        "channel": "twinpet-ui-09-c-payment-modal-analysis",
        "relay_meta": {
            "kind": "session_turn",
            "session_id": session_id,
            "phase": 0,
            "turn": 0,
            "role": "developer",
            "channel": "twinpet-ui-09-c-payment-modal-analysis",
            "relay_mode": True,
            "disable_mcp": True,
        },
        "workspace_policy_context": ctx,
    }


class WorkspaceBoundItemTests(unittest.TestCase):
    def test_analysis_profile_item_is_workspace_bound(self):
        item = _analysis_queue_item()
        self.assertTrue(is_workspace_bound_queue_item(item))

    def test_goal_only_item_not_workspace_bound(self):
        self.assertFalse(is_workspace_bound_queue_item({"prompt": "hi"}))


class WorkspaceTimeoutTests(unittest.TestCase):
    def test_docs_only_prompt_memo_resolves_600s_without_session_file(self):
        item = _analysis_queue_item()
        secs = resolve_claude_print_timeout(item, config=config_loader.load_config(ROOT))
        self.assertEqual(secs, DOCS_ONLY_TIMEOUT_SECS)

    def test_timeout_diagnostics_use_denormalized_wpc_when_no_session(self):
        item = _analysis_queue_item()
        diag = build_timeout_diagnostics(
            agent="claude",
            role=None,
            timeout_secs=600,
            cwd=TWINPET,
            item=item,
            session=None,
        )
        self.assertEqual(diag["role"], "developer")
        self.assertEqual(diag["workspace_profile"], "twinpet-ui-09-c-payment-modal-analysis")
        self.assertEqual(diag["workspace_mode"], "docs-only")
        self.assertEqual(diag["prompt_id"], "TWINPET-UI-09-C-READONLY-ANALYSIS-BLUEPRINT-001")
        self.assertTrue(diag["prompt_body_mode"])


class WorkspaceCwdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        self.cfg = config_loader.load_config(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_twinpet_root_for_verified_session(self):
        item = _analysis_queue_item(data_dir=self.data_dir)
        cwd, blocker = resolve_workspace_exec_cwd_or_blocker(
            item,
            data_dir=self.data_dir,
            config=self.cfg,
            default_cwd=SCRATCH,
            profiles=self.profiles,
        )
        self.assertIsNone(blocker)
        self.assertEqual(Path(cwd), Path(TWINPET).resolve())

    def test_workspace_bound_no_silent_scratch_fallback(self):
        item = _analysis_queue_item(data_dir=self.data_dir)
        bad_ctx = dict(item["workspace_policy_context"])
        bad_ctx["policy_hash"] = "0" * 64
        item["workspace_policy_context"] = bad_ctx
        cwd, blocker = resolve_workspace_exec_cwd_or_blocker(
            item,
            data_dir=self.data_dir,
            config=self.cfg,
            default_cwd=SCRATCH,
            profiles=self.profiles,
        )
        self.assertIsNone(cwd)
        self.assertEqual(blocker, "BLOCKER: workspace runner context missing")

    def test_non_workspace_item_uses_scratch_compat(self):
        item = {"prompt": "hello"}
        cwd, blocker = resolve_workspace_exec_cwd_or_blocker(
            item,
            data_dir=self.data_dir,
            config=self.cfg,
            default_cwd=SCRATCH,
            profiles=self.profiles,
        )
        self.assertIsNone(blocker)
        self.assertEqual(cwd, SCRATCH)


class ToolCallLeakageTests(unittest.TestCase):
    SAMPLE = (
        '<tool_call>\n<tool_name>Bash</tool_name>\n<parameters>\n'
        '<command>cd "C:/Users/Narachat/twinpet-pos" && git rev-parse HEAD</command>\n'
        "</parameters>\n</tool_call>"
    )

    def test_detects_tool_call_markup(self):
        info = detect_tool_call_leakage(self.SAMPLE)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["tool_name"], "Bash")
        self.assertIn("git rev-parse", info["command"])

    def test_blocker_format_includes_diagnostics(self):
        info = detect_tool_call_leakage(self.SAMPLE)
        assert info is not None
        text = format_tool_call_leakage_blocker(
            role="developer",
            cwd=TWINPET,
            workspace_profile="twinpet-ui-09-c-payment-modal-analysis",
            workspace_mode="docs-only",
            prompt_id="TEST-001",
            leakage=info,
        )
        self.assertTrue(text.startswith("BLOCKER: tool-call markup leaked"))
        self.assertIn("tool_name: Bash", text)
        self.assertIn("twinpet-ui-09-c-payment-modal-analysis", text)

    def test_wrapper_processes_leakage_as_blocker(self):
        import wrapper

        out = wrapper._process_claude_worker_output(
            self.SAMPLE,
            queue_item=_analysis_queue_item(),
            cwd=TWINPET,
        )
        self.assertTrue(out.startswith("BLOCKER: tool-call markup leaked"))


class PrecheckTests(unittest.TestCase):
    def test_precheck_runs_on_twinpet_if_present(self):
        if not Path(TWINPET).is_dir():
            self.skipTest("Twinpet workspace not present")
        text = run_workspace_precheck(TWINPET, expected_head=EXPECTED_HEAD)
        self.assertIn("AUTOMATED PRECHECK RESULTS", text)
        self.assertIn("git rev-parse HEAD", text)


if __name__ == "__main__":
    unittest.main()
