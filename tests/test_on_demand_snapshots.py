"""On-demand scoped snapshot request/response tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_loader
import workspace_policy as wp
from on_demand_snapshots import (
    SnapshotBudget,
    SnapshotWorkerState,
    build_snapshot_response,
    build_source_file_manifest,
    get_snapshot_budget,
    is_on_demand_snapshot_mode,
    parse_snapshot_request,
    process_snapshot_request,
    validate_snapshot_paths,
    worker_output_is_terminal_report,
)
from report_orchestration import build_initial_developer_report_prompt, build_report_orchestrated_dispatch_prompt
from worker_workspace import (
    build_on_demand_worker_augmentation,
    detect_tool_call_leakage,
    format_tool_call_leakage_blocker,
    is_docs_only_snapshot_mode,
)


def _analysis_policy() -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config())
    return wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
            "workspace_mode": "read-only-analysis",
        },
    ).policy


def _snapshot_request(*paths: str, reason: str = "Need PaymentModal source") -> str:
    lines = [
        "SNAPSHOT_REQUEST_BEGIN",
        f"Reason: {reason}",
        "Paths:",
    ]
    lines.extend(f"- {p}" for p in paths)
    lines.append("SNAPSHOT_REQUEST_END")
    return "\n".join(lines)


class OnDemandSnapshotParserTests(unittest.TestCase):
    def test_parse_snapshot_request(self):
        parsed = parse_snapshot_request(_snapshot_request(
            "src/components/PaymentModal.tsx",
            "src/components/PaymentModal.css",
        ))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(len(parsed.paths), 2)
        self.assertIn("PaymentModal", parsed.reason)

    def test_terminal_report_detection(self):
        self.assertTrue(worker_output_is_terminal_report("REPORT_READY\n\nStatus:\nPASS"))
        self.assertTrue(worker_output_is_terminal_report(
            "REPORT_FILE_WRITE_BEGIN\nPath: /tmp/x.md\n---\nbody\nREPORT_FILE_WRITE_END"
        ))
        self.assertFalse(worker_output_is_terminal_report(_snapshot_request("src/a.ts")))


class OnDemandSnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.policy = _analysis_policy()

    def test_allowed_paths_approved(self):
        rel = "src/components/PaymentModal.tsx"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export default function PaymentModal() {}", encoding="utf-8")
        approved, denied, _ = validate_snapshot_paths(
            [rel],
            policy=self.policy,
            workspace_root=self.root,
        )
        self.assertEqual(approved, [rel])
        self.assertEqual(denied, [])

    def test_outside_allowlist_denied(self):
        approved, denied, reasons = validate_snapshot_paths(
            ["src/secret/Other.tsx"],
            policy=self.policy,
            workspace_root=self.root,
        )
        self.assertEqual(approved, [])
        self.assertEqual(len(denied), 1)
        self.assertTrue(any("allowlist" in r for r in reasons))

    def test_absolute_path_escape_denied(self):
        approved, denied, _ = validate_snapshot_paths(
            ["C:/Windows/System32/cmd.exe"],
            policy=self.policy,
            workspace_root=self.root,
        )
        self.assertEqual(approved, [])
        self.assertTrue(denied)


class OnDemandSnapshotResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.policy = _analysis_policy()
        rel = "src/components/PaymentModal.tsx"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export default function PaymentModal() { return null; }", encoding="utf-8")
        self.rel = rel

    def test_snapshot_response_includes_only_requested_files(self):
        text, chars, blocked = build_snapshot_response(
            request_id="req1",
            workspace_root=self.root,
            approved_paths=[self.rel],
            budget=SnapshotBudget(),
            remaining_total_chars=120_000,
        )
        self.assertGreater(chars, 0)
        self.assertIn("SNAPSHOT_FILE_BEGIN", text)
        self.assertIn(self.rel, text)
        self.assertEqual(blocked, [])

    def test_large_file_bounded(self):
        rel = "src/components/PaymentModal.css"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * 100_000, encoding="utf-8")
        text, chars, blocked = build_snapshot_response(
            request_id="req2",
            workspace_root=self.root,
            approved_paths=[rel],
            budget=SnapshotBudget(max_single_file_chars=1000),
            remaining_total_chars=120_000,
        )
        self.assertGreater(chars, 0)
        self.assertLess(chars, 2000)
        self.assertIn("TRUNCATED", text)

    def test_process_snapshot_request_round_budget(self):
        state = SnapshotWorkerState(round=4, total_chars=0)
        result = process_snapshot_request(
            _snapshot_request(self.rel),
            workspace_root=self.root,
            policy=self.policy,
            state=state,
            budget=SnapshotBudget(max_rounds_per_worker=4),
        )
        self.assertFalse(result.ok)
        self.assertIn("snapshot budget exceeded", result.blocker.lower())


class OnDemandInitialPromptTests(unittest.TestCase):
    def setUp(self):
        self.policy = _analysis_policy()
        self.memo = "Analyze PaymentModal for UI-09-C."
        self.report_path = "C:/Users/Narachat/OneDrive/Ai-Report/claude/test-report.md"
        self.roots = ["C:/Users/Narachat/OneDrive/Ai-Report/claude"]

    def test_initial_prompt_uses_manifest_not_full_snapshots(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("ON-DEMAND SOURCE ACCESS", result.prompt)
        self.assertNotIn("READ-ONLY SNAPSHOTS:", result.prompt)
        self.assertIn("SNAPSHOT_REQUEST_BEGIN", result.prompt)
        self.assertLess(len(result.prompt), 50_000)

    def test_initial_prompt_under_cap(self):
        result = build_initial_developer_report_prompt(
            project="twinpet",
            phase="analysis",
            subject="initial",
            workspace_root=str(self.policy.get("workspace", {}).get("root") or ""),
            read_paths=list(self.policy.get("read_paths") or []),
            prompt_memo_body=self.memo,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertLess(len(result.prompt), 35_000)

    def test_on_demand_mode_disables_full_snapshot_injection(self):
        item = {
            "workspace_policy_context": {
                "relay_kind": "session_turn",
                "session_id": 1,
                "session_role": "developer",
                "policy_mode": "read-only",
                "workspace_root": self.policy["workspace"]["root"],
            },
        }
        config = config_loader.load_config()
        self.assertTrue(is_on_demand_snapshot_mode(item, self.policy, config=config))
        self.assertFalse(is_docs_only_snapshot_mode(item, self.policy, config=config))

    def test_on_demand_augmentation_has_manifest_not_file_bodies(self):
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        rel = "src/components/PaymentModal.tsx"
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("component", encoding="utf-8")
        policy = dict(self.policy)
        policy["read_paths"] = [rel]
        policy["workspace"] = {"root": str(root), "expected_head": ""}
        item = {
            "workspace_policy_context": {
                "relay_kind": "session_turn",
                "session_id": 1,
                "session_role": "developer",
                "policy_mode": "read-only",
                "workspace_root": str(root),
            },
        }
        with mock.patch("worker_workspace.run_workspace_precheck_structured") as m_pre:
            m_pre.return_value = mock.Mock(ok=True, blocker="", text="AUTOMATED PRECHECK RESULTS")
            text, blocker, meta = build_on_demand_worker_augmentation(
                root, item, policy, config=config_loader.load_config(),
            )
        self.assertIsNone(blocker)
        self.assertIn("SOURCE FILE MANIFEST", text)
        self.assertIn("sha256:", text)
        self.assertNotIn("```", text)
        self.assertFalse(meta.injected)
        self.assertEqual(meta.file_count, 0)

    def test_tool_call_xml_still_blocked(self):
        leakage = detect_tool_call_leakage('<tool_call>{"name":"Read"}</tool_call>')
        self.assertIsNotNone(leakage)
        blocker = format_tool_call_leakage_blocker(
            role="developer",
            cwd=".",
            workspace_profile="twinpet-ui-09-c-payment-modal-analysis",
            workspace_mode="read-only",
            prompt_id="",
            leakage=leakage,
            snapshot_mode=True,
        )
        self.assertIn("tool-call markup", blocker.lower())


class OnDemandManifestTests(unittest.TestCase):
    def test_manifest_includes_hash_and_size(self):
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        rel = "src/components/PaymentModal.tsx"
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = "export default function X() {}"
        target.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        text, count = build_source_file_manifest(root, [rel])
        self.assertEqual(count, 1)
        self.assertIn(digest, text)
        self.assertIn("size_bytes", text)


if __name__ == "__main__":
    unittest.main()
