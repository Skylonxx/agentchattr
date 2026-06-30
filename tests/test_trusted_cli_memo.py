"""Trusted direct repo CLI (Phase 1) mode + execution memo tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config_loader
import workspace_policy as wp
from report_orchestration import build_report_orchestrated_dispatch_prompt, is_report_orchestrated_policy
from trusted_cli_memo import (
    REQUIRED_MEMO_SECTIONS,
    build_trusted_cli_execution_memo,
)
from worker_workspace import is_docs_only_snapshot_mode, is_trusted_direct_repo_cli_mode
from on_demand_snapshots import is_on_demand_snapshot_mode
from workspace_policy_runtime import is_trusted_direct_repo_cli_policy

TRUSTED_PROFILE = "twinpet-ui-09-c-payment-modal-trusted-cli"
ANALYSIS_PROFILE = "twinpet-ui-09-c-payment-modal-analysis"
TWINPET_ROOT = "C:/Users/Narachat/twinpet-pos"


def _trusted_policy() -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config())
    res = wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": TRUSTED_PROFILE,
            "workspace_mode": "trusted_direct_repo_cli",
        },
    )
    assert res.ok, res.errors
    return res.policy


def _analysis_policy() -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config())
    return wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": ANALYSIS_PROFILE,
            "workspace_mode": "read-only-analysis",
        },
    ).policy


def _trusted_item(policy: dict) -> dict:
    return {
        "workspace_policy_context": {
            "relay_kind": "session_turn",
            "session_id": 1,
            "session_role": "developer",
            "policy_mode": "read-only",
            "workspace_root": policy["workspace"]["root"],
            "trusted_direct_repo_cli": True,
        },
    }


class TrustedModeConfigTests(unittest.TestCase):
    def test_mode_alias_normalizes_to_read_only(self):
        self.assertEqual(wp.normalize_workspace_mode("trusted_direct_repo_cli"), "read-only")
        self.assertEqual(wp.normalize_workspace_mode("trusted-direct-repo-cli"), "read-only")

    def test_profile_loads_with_trusted_flag(self):
        policy = _trusted_policy()
        self.assertTrue(policy.get("trusted_direct_repo_cli"))
        self.assertEqual(policy.get("mode"), "read-only")
        self.assertTrue(is_trusted_direct_repo_cli_policy(policy))
        self.assertTrue(is_report_orchestrated_policy(policy))

    def test_profile_resolves_cwd_to_real_twinpet_root(self):
        policy = _trusted_policy()
        root = (policy.get("workspace") or {}).get("root")
        self.assertEqual(str(root).replace("\\", "/"), TWINPET_ROOT)

    def test_trusted_profile_has_no_write_files(self):
        policy = _trusted_policy()
        self.assertEqual(list(policy.get("write_files") or []), [])


class TrustedSnapshotSkipTests(unittest.TestCase):
    def test_trusted_skips_docs_only_snapshot(self):
        policy = _trusted_policy()
        item = _trusted_item(policy)
        config = config_loader.load_config()
        self.assertTrue(is_trusted_direct_repo_cli_mode(item, policy))
        self.assertFalse(is_docs_only_snapshot_mode(item, policy, config=config))

    def test_trusted_skips_on_demand_snapshot(self):
        policy = _trusted_policy()
        item = _trusted_item(policy)
        config = config_loader.load_config()
        self.assertFalse(is_on_demand_snapshot_mode(item, policy, config=config))

    def test_analysis_profile_still_uses_on_demand(self):
        policy = _analysis_policy()
        item = {
            "workspace_policy_context": {
                "relay_kind": "session_turn",
                "session_id": 2,
                "session_role": "developer",
                "policy_mode": "read-only",
                "workspace_root": policy["workspace"]["root"],
            },
        }
        config = config_loader.load_config()
        self.assertFalse(is_trusted_direct_repo_cli_mode(item, policy))
        self.assertTrue(is_on_demand_snapshot_mode(item, policy, config=config))


class TrustedCommandBuilderTests(unittest.TestCase):
    def test_trusted_command_has_no_tools_seal(self):
        import wrapper
        cmd, payload = wrapper._build_claude_trusted_command("claude", "hi")
        self.assertNotIn("--tools", cmd)
        self.assertIn("--print", cmd)
        self.assertEqual(payload, b"hi")

    def test_legacy_command_still_seals_tools(self):
        import wrapper
        cmd, _ = wrapper._build_claude_print_command("claude", "hi")
        self.assertIn("--tools", cmd)
        idx = cmd.index("--tools")
        self.assertEqual(cmd[idx + 1], "")


class TrustedMemoTests(unittest.TestCase):
    def _memo(self):
        policy = _trusted_policy()
        roots = list(policy.get("external_report_write_roots") or [])
        report_path = (policy.get("report_paths") or [""])[0]
        return build_trusted_cli_execution_memo(
            project="twinpet",
            phase="analysis",
            subject="UI-09-C PaymentModal",
            workspace_root=str(policy["workspace"]["root"]),
            expected_head=str((policy.get("workspace") or {}).get("expected_head") or ""),
            prompt_memo_body="Analyze PaymentModal read-only for UI-09-C checkout flow.",
            read_paths=list(policy.get("read_paths") or []),
            primary_paths=list(policy.get("trusted_cli_primary_paths") or []),
            forbidden_paths=list(policy.get("forbidden_paths") or []),
            expected_output_path=report_path,
            external_report_write_roots=roots,
        ), report_path, str(policy["workspace"]["root"])

    def test_memo_includes_all_required_sections(self):
        memo, _, _ = self._memo()
        self.assertTrue(memo.ok, memo.blocker)
        lines = memo.prompt.splitlines()
        for section in REQUIRED_MEMO_SECTIONS:
            self.assertTrue(
                any(ln.startswith(section) for ln in lines),
                f"missing section: {section}",
            )

    def test_memo_includes_workdir_and_report_path(self):
        memo, report_path, root = self._memo()
        self.assertIn("WORKDIR:", memo.prompt)
        self.assertIn(root, memo.prompt)
        self.assertIn("REPORT PATH:", memo.prompt)
        self.assertIn(report_path, memo.prompt)

    def test_memo_includes_red_zones_and_stop_conditions(self):
        memo, _, _ = self._memo()
        self.assertIn("FORBIDDEN FILES / RED ZONES:", memo.prompt)
        self.assertIn("POSPage", memo.prompt)
        self.assertIn("STOP CONDITIONS:", memo.prompt)
        self.assertIn("BLOCKER", memo.prompt)

    def test_memo_does_not_inject_source_snapshots(self):
        memo, _, _ = self._memo()
        self.assertNotIn("SNAPSHOT_FILE_BEGIN", memo.prompt)
        self.assertNotIn("READ-ONLY SNAPSHOTS:", memo.prompt)
        self.assertNotIn("```", memo.prompt)

    def test_memo_forbids_native_write_for_report(self):
        memo, _, _ = self._memo()
        self.assertIn("REPORT OUTPUT METHOD", memo.prompt)
        self.assertIn("Do NOT use Claude Code Write/Edit tools", memo.prompt)
        self.assertIn("REPORT_FILE_WRITE_BEGIN", memo.prompt)
        self.assertIn("REPORT_FILE_WRITE_END", memo.prompt)
        self.assertIn("BLOCKER: trusted CLI report bridge output failed", memo.prompt)

    def test_memo_requires_report_bridge_section(self):
        memo, _, _ = self._memo()
        lines = memo.prompt.splitlines()
        self.assertTrue(any(ln.startswith("REPORT OUTPUT METHOD") for ln in lines))


class TrustedDispatchTests(unittest.TestCase):
    def test_developer_dispatch_uses_memo_not_manifest(self):
        policy = _trusted_policy()
        roots = list(policy.get("external_report_write_roots") or [])
        report_path = (policy.get("report_paths") or [""])[0]
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="UI-09-C",
            prompt_memo_body="Analyze PaymentModal read-only for UI-09-C.",
            policy=policy,
            expected_output_path=report_path,
            external_report_write_roots=roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("MODE: trusted_direct_repo_cli", result.prompt)
        self.assertNotIn("ON-DEMAND SOURCE ACCESS", result.prompt)
        self.assertNotIn("SNAPSHOT_REQUEST_BEGIN", result.prompt)
        self.assertLess(len(result.prompt), 20_000)

    def test_trusted_preflight_does_not_require_read_paths(self):
        from report_orchestration import validate_initial_developer_preflight
        policy = _trusted_policy()
        policy = dict(policy)
        policy["read_paths"] = []
        roots = list(policy.get("external_report_write_roots") or [])
        ok, blocker = validate_initial_developer_preflight(
            policy,
            prompt_memo_body="Analyze read-only.",
            expected_output_path=(policy.get("report_paths") or [""])[0],
            external_report_write_roots=roots,
        )
        self.assertTrue(ok, blocker)


class TrustedCliNativeWriteRepairTests(unittest.TestCase):
    def test_repair_prompt_includes_bridge_contract(self):
        from worker_workspace import build_trusted_cli_report_bridge_repair_prompt

        report_path = "C:/tmp/trusted-cli-report.md"
        prior = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory.\n\n"
            "# PaymentModal Analysis\nFindings here."
        )
        prompt = build_trusted_cli_report_bridge_repair_prompt(
            previous_output=prior,
            report_path=report_path,
            repair_round=1,
            max_repair_rounds=1,
        )
        self.assertIn("TRUSTED CLI REPORT BRIDGE CORRECTION", prompt)
        self.assertIn("REPORT_FILE_WRITE_BEGIN", prompt)
        self.assertIn("REPORT_FILE_WRITE_END", prompt)
        self.assertIn(report_path, prompt)
        self.assertIn("Do not use Write/Edit tools", prompt)
        self.assertIn("PaymentModal Analysis", prompt)

    def test_first_native_write_triggers_developer_repair_not_terminal(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile="twinpet-ui-09-c-payment-modal-trusted-cli",
            session_workspace_mode="read-only",
        )
        policy = _trusted_policy()
        report_path = (policy.get("report_paths") or [""])[0]
        native = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory. Could you approve the write?\n\n"
            "# PaymentModal\nAnalysis summary."
        )
        action = on_worker_output(
            state,
            "developer",
            native,
            worker_context={
                "workspace_policy": policy,
                "policy_id": policy.get("policy_id"),
                "report_paths": list(policy.get("report_paths") or []),
                "report_paths_by_role": {"developer": report_path},
            },
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "developer")
        self.assertEqual(state.trusted_cli_report_bridge_repair_rounds, 1)
        self.assertIn("REPORT_FILE_WRITE_BEGIN", action.routing_body)

    def test_second_native_write_emits_terminal_blocker(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile="twinpet-ui-09-c-payment-modal-trusted-cli",
            session_workspace_mode="read-only",
            trusted_cli_report_bridge_repair_rounds=1,
            max_trusted_cli_report_bridge_repair_rounds=1,
        )
        policy = _trusted_policy()
        report_path = (policy.get("report_paths") or [""])[0]
        native = (
            "Could you approve the write permission for the external report path?"
        )
        action = on_worker_output(
            state,
            "developer",
            native,
            worker_context={
                "workspace_policy": policy,
                "policy_id": policy.get("policy_id"),
                "report_paths": list(policy.get("report_paths") or []),
                "report_paths_by_role": {"developer": report_path},
            },
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("trusted CLI used native write instead of report bridge", action.prompt_context)
        self.assertIn("repair_round: 1", action.prompt_context)

    def test_repair_then_bridge_report_ready_routes_ui_lead(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output
        from worker_workspace import (
            REPORT_FILE_WRITE_BEGIN_MARKER,
            REPORT_FILE_WRITE_END_MARKER,
        )

        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile="twinpet-ui-09-c-payment-modal-trusted-cli",
            session_workspace_mode="read-only",
        )
        policy = _trusted_policy()
        tmp = tempfile.mkdtemp()
        report_path = str(Path(tmp) / "trusted-cli-report.md")
        policy = dict(policy)
        policy["external_report_write_roots"] = [tmp]
        policy["report_paths"] = [report_path]
        native = "Could you approve the write for the external report?"
        on_worker_output(
            state,
            "developer",
            native,
            worker_context={
                "workspace_policy": policy,
                "policy_id": policy.get("policy_id"),
                "allowed_report_roots": [tmp],
                "report_paths": [report_path],
                "report_paths_by_role": {"developer": report_path},
            },
        )
        bridge = (
            f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
            f"Path: {report_path}\n"
            "Status: PASS_WITH_NOTES\n"
            "Summary: Re-emitted through bridge.\n"
            "Next recommended role: coordinator\n"
            "---\n"
            "# Report\nDone.\n"
            f"{REPORT_FILE_WRITE_END_MARKER}"
        )
        from report_orchestration import parse_report_ready
        from worker_workspace import process_claude_worker_report_output

        ready = process_claude_worker_report_output(bridge, policy)
        assert ready is not None
        action = on_worker_output(
            state,
            "developer",
            ready,
            worker_context={
                "workspace_policy": policy,
                "policy_id": policy.get("policy_id"),
                "allowed_report_roots": [tmp],
                "report_paths": [report_path],
                "report_paths_by_role": {"developer": report_path},
            },
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")
        self.assertTrue(Path(report_path).is_file())
        self.assertIsNotNone(parse_report_ready(ready))


if __name__ == "__main__":
    unittest.main()
