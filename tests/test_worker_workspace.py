"""Workspace worker context, snapshots, cwd, and tool-call leakage tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_loader
import workspace_policy as wp
import workspace_policy_runtime as wpr
from worker_timeout import (
    DOCS_ONLY_TIMEOUT_SECS,
    build_timeout_diagnostics,
    resolve_claude_print_timeout,
)
from worker_workspace import (
    SnapshotMeta,
    build_docs_only_worker_augmentation,
    build_read_only_file_snapshots,
    detect_tool_call_leakage,
    extract_report_block,
    extract_report_file_write_block,
    format_tool_call_leakage_blocker,
    is_docs_only_snapshot_mode,
    is_workspace_bound_queue_item,
    process_claude_worker_report_output,
    read_allowlisted_file_snapshot,
    resolve_workspace_exec_cwd_or_blocker,
    run_workspace_precheck_structured,
    try_process_scoped_worker_report_output,
    try_recover_write_tool_call_leakage,
    try_save_external_analysis_report,
    write_validated_external_report,
    REPORT_FILE_WRITE_BEGIN_MARKER,
    REPORT_FILE_WRITE_END_MARKER,
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

    def test_docs_only_snapshot_mode(self):
        session = _analysis_session_record()
        item = _analysis_queue_item()
        config = config_loader.load_config(ROOT)
        self.assertFalse(
            is_docs_only_snapshot_mode(item, session["workspace_policy"], config=config)
        )

    def test_legacy_read_only_without_on_demand_uses_full_snapshots(self):
        session = _analysis_session_record()
        item = _analysis_queue_item()
        policy = dict(session["workspace_policy"])
        policy["on_demand_snapshots"] = False
        policy["analysis_report_only"] = False
        self.assertTrue(is_docs_only_snapshot_mode(item, policy))

    def test_handoff_repair_skips_snapshot_injection(self):
        session = _analysis_session_record()
        item = _analysis_queue_item()
        item["relay_meta"]["handoff_repair"] = True
        item["workspace_policy_context"]["handoff_repair"] = True
        item["workspace_policy_context"]["skip_snapshot_injection"] = True
        self.assertFalse(is_docs_only_snapshot_mode(item, session["workspace_policy"]))

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
        self.assertEqual(diag["workspace_mode"], "read-only")
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


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src" / "components").mkdir(parents=True)
        (self.root / "src" / "components" / "PaymentModal.tsx").write_text(
            "export function PaymentModal() { return null; }\n", encoding="utf-8",
        )
        (self.root / "src" / "components" / "PaymentModal.css").write_text(
            ".modal { color: red; }\n", encoding="utf-8",
        )
        (self.root / "secret").write_text("nope", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.root, capture_output=True, check=False)
        subprocess.run(["git", "add", "."], cwd=self.root, capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-m", "init", "--allow-empty"],
            cwd=self.root, capture_output=True, check=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_allowlisted_files_only(self):
        snap = read_allowlisted_file_snapshot(
            self.root, "src/components/PaymentModal.tsx", max_chars_per_file=10000,
        )
        self.assertTrue(snap["exists"])
        self.assertIn("PaymentModal", snap["content"])

    def test_rejects_path_outside_allowlist_resolution(self):
        snap = read_allowlisted_file_snapshot(
            self.root, "../secret", max_chars_per_file=10000,
        )
        self.assertFalse(snap["exists"])
        self.assertIn("rejected", snap["content"])

    def test_snapshot_section_includes_payment_modal_files(self):
        text, meta = build_read_only_file_snapshots(
            self.root,
            ["src/components/PaymentModal.tsx", "src/components/PaymentModal.css", "missing.md"],
            max_chars_per_file=10000,
        )
        self.assertIn("READ-ONLY FILE SNAPSHOT", text)
        self.assertIn("PaymentModal.tsx", text)
        self.assertIn("PaymentModal.css", text)
        self.assertIn("(missing)", text)
        self.assertEqual(meta.file_count, 2)

    def test_truncation_marker_for_large_file(self):
        big = "x" * 100_000
        (self.root / "big.txt").write_text(big, encoding="utf-8")
        snap = read_allowlisted_file_snapshot(self.root, "big.txt", max_chars_per_file=1000)
        self.assertTrue(snap["truncated"])
        self.assertIn("[TRUNCATED", snap["content"])


class PrecheckBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.root, capture_output=True, check=False)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "e"], cwd=self.root, capture_output=True, check=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_head_mismatch_blocks(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True,
        ).stdout.strip()
        wrong = "0" * 40 if head != "0" * 40 else "f" * 40
        result = run_workspace_precheck_structured(self.root, expected_head=wrong)
        self.assertFalse(result.ok)
        self.assertIn("expected head mismatch", result.blocker or "")

    def test_dirty_tree_blocks_read_only(self):
        (self.root / "dirty.txt").write_text("x", encoding="utf-8")
        policy = wp.default_scratch_readonly_policy()
        policy = dict(policy)
        policy["mode"] = "read-only"
        result = run_workspace_precheck_structured(self.root, policy=policy)
        self.assertFalse(result.ok)
        self.assertIn("dirty tree", (result.blocker or "").lower())

    def test_docs_only_augmentation_includes_snapshots(self):
        session = _analysis_session_record()
        policy = session["workspace_policy"]
        item = _analysis_queue_item()
        with mock.patch("worker_workspace.run_workspace_precheck_structured") as m_pre:
            m_pre.return_value = type("R", (), {
                "ok": True, "blocker": None,
                "text": "AUTOMATED PRECHECK RESULTS\n- ok",
                "head": EXPECTED_HEAD, "porcelain": "",
            })()
            with mock.patch("worker_workspace.build_read_only_file_snapshots") as m_snap:
                m_snap.return_value = ("READ-ONLY FILE SNAPSHOT\n### foo", SnapshotMeta(injected=True, file_count=1, paths=["foo"]))
                text, blocker, meta = build_docs_only_worker_augmentation(
                    TWINPET, item, policy, config=config_loader.load_config(ROOT),
                )
        self.assertIsNone(blocker)
        self.assertIn("AUTOMATED PRECHECK", text or "")
        self.assertIn("READ-ONLY FILE SNAPSHOT", text or "")
        self.assertEqual(meta.file_count, 1)


class ToolCallLeakageTests(unittest.TestCase):
    SAMPLE = (
        '<tool_call>\n<tool_name>Read</tool_name>\n<parameters>\n'
        '<command>cd "C:/Users/Narachat/twinpet-pos" && git rev-parse HEAD</command>\n'
        "</parameters>\n</tool_call>"
    )

    def test_detects_tool_call_markup(self):
        info = detect_tool_call_leakage(self.SAMPLE)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["tool_name"], "Read")

    def test_snapshot_mode_blocker_message(self):
        info = detect_tool_call_leakage(self.SAMPLE)
        assert info is not None
        text = format_tool_call_leakage_blocker(
            role="developer",
            cwd=TWINPET,
            workspace_profile="twinpet-ui-09-c-payment-modal-analysis",
            workspace_mode="docs-only",
            prompt_id="TEST-001",
            leakage=info,
            snapshot_meta=SnapshotMeta(injected=True, file_count=3),
            snapshot_mode=True,
        )
        self.assertIn("despite snapshot mode", text)
        self.assertIn("files injected: 3", text)

    def test_wrapper_processes_leakage_as_blocker(self):
        import wrapper

        item = _analysis_queue_item()
        item["_snapshot_meta"] = {"injected": True, "file_count": 2}
        out = wrapper._process_claude_worker_output(self.SAMPLE, queue_item=item, cwd=TWINPET)
        self.assertTrue(out.startswith("BLOCKER: tool-call markup leaked"))


class ReadOnlyAnalysisPolicyTests(unittest.TestCase):
    def test_porcelain_parser_recovers_trimmed_leading_space(self):
        entries = wpr.parse_git_porcelain("M Context.md\n M Task.md\n")
        paths = [e["path"] for e in entries]
        self.assertIn("Context.md", paths)
        self.assertNotIn("ontext.md", paths)

    def test_analysis_profile_has_no_repo_write_files(self):
        session = _analysis_session_record()
        policy = session["workspace_policy"]
        self.assertEqual(policy["mode"], "read-only")
        self.assertEqual(policy.get("write_files") or [], [])
        self.assertTrue(policy.get("analysis_report_only"))
        self.assertGreater(len(policy.get("external_report_write_roots") or []), 0)

    def test_read_only_analysis_mode_alias(self):
        self.assertEqual(wp.normalize_workspace_mode("read-only-analysis"), "read-only")


class ReadOnlyDirtyTreeTests(unittest.TestCase):
    def setUp(self):
        self.policy = _analysis_session_record()["workspace_policy"]

    def test_docs_dirty_does_not_block(self):
        porcelain = " M Task.md\n M Context.md\n"
        docs, blocking = wpr.classify_dirty_entries_report_only_analysis(
            porcelain, policy=self.policy,
        )
        self.assertEqual(len(blocking), 0)
        self.assertGreaterEqual(len(docs), 1)
        result = wpr.verify_dirty_set_report_only_analysis(
            porcelain_output=porcelain, policy=self.policy,
        )
        self.assertTrue(result.ok)

    def test_src_dirty_blocks(self):
        porcelain = " M src/components/PaymentModal.tsx\n"
        _, blocking = wpr.classify_dirty_entries_report_only_analysis(
            porcelain, policy=self.policy,
        )
        self.assertIn("src/components/PaymentModal.tsx", blocking)
        result = wpr.verify_dirty_set_report_only_analysis(
            porcelain_output=porcelain, policy=self.policy,
        )
        self.assertFalse(result.ok)

    def test_tests_dirty_blocks(self):
        porcelain = " M tests/pos-human-checkout.spec.ts\n"
        result = wpr.verify_dirty_set_report_only_analysis(
            porcelain_output=porcelain, policy=self.policy,
        )
        self.assertFalse(result.ok)


class ExternalReportSaveTests(unittest.TestCase):
    def test_extract_report_block(self):
        text = "intro\nREPORT_BEGIN\n# Title\nbody\nREPORT_END\n"
        self.assertEqual(extract_report_block(text), "# Title\nbody")

    def test_saves_outside_repo_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "twinpet"
        root.mkdir()
        ext = Path(tmp.name) / "report-out.md"
        policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "write_files": [],
            "report_paths": [str(ext)],
        }
        body = "REPORT_BEGIN\n# Analysis\nDone.\nREPORT_END"
        result = try_save_external_analysis_report(body, policy, root)
        self.assertTrue(result.saved)
        self.assertTrue(ext.exists())
        self.assertFalse((root / "docs").exists())

    def test_external_save_failure_falls_back_with_notes(self):
        policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "write_files": [],
            "report_paths": ["Z:/nonexistent_drive/report.md"],
        }
        body = "REPORT_BEGIN\n# Analysis\nDone.\nREPORT_END"
        with mock.patch("worker_workspace.Path.write_text", side_effect=OSError("denied")):
            with mock.patch("worker_workspace.Path.mkdir"):
                result = try_save_external_analysis_report(
                    body, policy, "C:/Users/Narachat/twinpet-pos",
                )
        self.assertFalse(result.saved)
        self.assertTrue(any("PASS WITH NOTES" in n for n in result.notes))


class PromptContractTests(unittest.TestCase):
    def test_scoped_prompt_mentions_no_tools_for_read_only(self):
        from session_relay import build_scoped_write_worker_prompt

        session = _analysis_session_record()
        prompt = build_scoped_write_worker_prompt(
            session_name="test",
            goal="g",
            role="developer",
            policy=session["workspace_policy"],
            prompt_body="PROMPT ID: X\nmemo body",
        )
        self.assertIn("FULL TASK MEMO", prompt)
        self.assertIn("REPORT-ONLY ANALYSIS", prompt)
        self.assertIn("no generic tools", prompt.lower())
        self.assertIn("REPORT_FILE_WRITE_BEGIN", prompt)
        self.assertIn("No Twinpet repo writes", prompt)
        self.assertIn("EXTERNAL REPORT WRITE ALLOWLIST", prompt)
        self.assertNotIn("Write your report to the exact expected path", prompt)


class TwinpetSmokeTests(unittest.TestCase):
    def test_live_twinpet_snapshots_if_present(self):
        if not Path(TWINPET).is_dir():
            self.skipTest("Twinpet not present")
        session = _analysis_session_record()
        policy = session["workspace_policy"]
        item = _analysis_queue_item()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        (data_dir / "session_runs.json").write_text(json.dumps([session]), encoding="utf-8")
        item = _analysis_queue_item(data_dir=data_dir)
        cfg = config_loader.load_config(ROOT)
        profiles = config_loader.get_workspace_profiles(cfg)
        cwd, blocker = resolve_workspace_exec_cwd_or_blocker(
            item, data_dir=data_dir, config=cfg, default_cwd=SCRATCH, profiles=profiles,
        )
        self.assertIsNone(blocker)
        augment, pre_blocker, meta = build_docs_only_worker_augmentation(
            cwd, item, policy, config=cfg,
        )
        self.assertIsNone(pre_blocker, msg=pre_blocker)
        assert augment is not None
        self.assertIn("AUTOMATED PRECHECK RESULTS", augment)
        self.assertIn("PaymentModal.tsx", augment)
        self.assertIn("PaymentModal.css", augment)
        self.assertGreater(meta.file_count, 0)
        self.assertIn("pre-existing docs tracker dirty files", augment)
        st = subprocess.run(["git", "status", "--short"], cwd=TWINPET, capture_output=True, text=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=TWINPET, capture_output=True, text=True)
        porcelain = st.stdout or ""
        self.assertNotIn("src/", porcelain)
        self.assertNotIn("tests/", porcelain)
        self.assertEqual(head.stdout.strip(), EXPECTED_HEAD)


class WorkerReportWriteBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "write_files": [],
            "external_report_write_roots": self.roots,
            "report_paths": [str(Path(self.tmp) / "analysis-report.md")],
        }
        self.report_path = str(Path(self.tmp) / "analysis-report.md")

    def _bridge_body(self, path: str | None = None, content: str = "# Report\nDone.") -> str:
        target = path or self.report_path
        return (
            f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
            f"Path: {target}\n"
            "Status: PASS\n"
            "Summary: short summary\n"
            "Next recommended role: coordinator\n"
            "---\n"
            f"{content}\n"
            f"{REPORT_FILE_WRITE_END_MARKER}"
        )

    def test_extract_report_file_write_block(self):
        parsed = extract_report_file_write_block(self._bridge_body())
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["path"], self.report_path)
        self.assertIn("# Report", parsed["content"])

    def test_bridge_writes_md_under_ai_report_root(self):
        out = try_process_scoped_worker_report_output(self._bridge_body(), self.policy)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertTrue(Path(self.report_path).is_file())
        self.assertIn(self.report_path, out)

    def test_bridge_rejects_twinpet_repo_path(self):
        twinpet_path = "C:/Users/Narachat/twinpet-pos/docs/report.md"
        out = try_process_scoped_worker_report_output(
            self._bridge_body(path=twinpet_path),
            self.policy,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_WRITE_FAILED"))

    def test_bridge_rejects_outside_root(self):
        outside = "C:/outside/report.md"
        ok, _path, err = write_validated_external_report(
            outside, "# x", self.policy,
        )
        self.assertFalse(ok)
        self.assertIn("outside allowed roots", err)

    def test_bridge_rejects_non_md(self):
        bad = str(Path(self.tmp) / "report.txt")
        ok, _path, err = write_validated_external_report(bad, "# x", self.policy)
        self.assertFalse(ok)
        self.assertIn("only .md", err)

    def test_write_tool_call_to_allowed_path_recovered(self):
        xml = (
            "<tool_call>\n<tool_name>Write</tool_name>\n<parameters>\n"
            f'<parameter name="file_path">{self.report_path}</parameter>\n'
            '<parameter name="content"># Recovered\nbody</parameter>\n'
            "</parameters>\n</tool_call>"
        )
        out = process_claude_worker_report_output(xml, policy=self.policy)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertTrue(Path(self.report_path).is_file())

    def test_write_tool_call_to_twinpet_hard_blocks(self):
        xml = (
            "<tool_call>\n<tool_name>Write</tool_name>\n<parameters>\n"
            '<parameter name="file_path">C:/Users/Narachat/twinpet-pos/src/x.md</parameter>\n'
            '<parameter name="content"># bad</parameter>\n'
            "</parameters>\n</tool_call>"
        )
        out = process_claude_worker_report_output(xml, policy=self.policy)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("BLOCKER:"))
        self.assertIn("Twinpet workspace", out)

    def test_generic_tool_call_still_blocks(self):
        xml = (
            "<tool_call>\n<tool_name>Read</tool_name>\n<parameters>\n"
            '<parameter name="path">src/foo.ts</parameter>\n'
            "</parameters>\n</tool_call>"
        )
        self.assertIsNone(try_recover_write_tool_call_leakage(xml, self.policy))
        info = detect_tool_call_leakage(xml)
        self.assertIsNotNone(info)
        blocker = format_tool_call_leakage_blocker(
            role="developer",
            cwd=TWINPET,
            workspace_profile="twinpet-ui-09-c-payment-modal-analysis",
            workspace_mode="read-only",
            prompt_id="test",
            leakage=info or {},
            snapshot_mode=True,
        )
        self.assertIn("tool-call markup leaked", blocker)

    def test_augmentation_mentions_report_write_bridge(self):
        session = _analysis_session_record()
        item = _analysis_queue_item()
        augment, blocker, _meta = build_docs_only_worker_augmentation(
            TWINPET, item, session["workspace_policy"],
        )
        self.assertIsNone(blocker)
        assert augment is not None
        self.assertIn("REPORT_FILE_WRITE_BEGIN", augment)
        self.assertIn("Do not emit <tool_call>", augment)


class TrustedCliReportBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.report_path = str(Path(self.tmp) / "trusted-cli-report.md")
        self.policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "trusted_direct_repo_cli": True,
            "policy_id": "twinpet-ui-09-c-payment-modal-trusted-cli",
            "write_files": [],
            "external_report_write_roots": [self.tmp],
            "report_paths": [self.report_path],
        }
        self.item = {
            "workspace_policy_context": {
                "relay_kind": "session_turn",
                "session_id": 99,
                "session_role": "developer",
                "policy_id": self.policy["policy_id"],
                "policy_mode": "read-only",
                "workspace_root": TWINPET,
                "trusted_direct_repo_cli": True,
            },
        }

    def _bridge_body(self, path: str | None = None) -> str:
        target = path or self.report_path
        return (
            f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
            f"Path: {target}\n"
            "Status: PASS_WITH_NOTES\n"
            "Summary: Trusted CLI analysis complete.\n"
            "Next recommended role: coordinator\n"
            "---\n"
            "# Trusted CLI PaymentModal Analysis\n\nDone.\n"
            f"{REPORT_FILE_WRITE_END_MARKER}"
        )

    def test_trusted_cli_bridge_saves_and_returns_report_ready(self):
        out = process_claude_worker_report_output(
            self._bridge_body(),
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertTrue(Path(self.report_path).is_file())

    def test_trusted_cli_native_write_passes_through_for_coordinator_repair(self):
        prompt_text = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory. Could you approve the write?"
        )
        out = process_claude_worker_report_output(
            prompt_text,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNone(out)

    def test_trusted_cli_native_write_terminal_blocker_helper(self):
        from worker_workspace import format_trusted_cli_native_write_blocker

        prompt_text = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory."
        )
        out = format_trusted_cli_native_write_blocker(
            text=prompt_text,
            policy=self.policy,
            cwd=TWINPET,
            workspace_profile=self.policy["policy_id"],
            workspace_mode="read-only",
            report_path=self.report_path,
            repair_round=1,
            max_repair_rounds=1,
        )
        self.assertTrue(out.startswith("BLOCKER: trusted CLI used native write"))
        self.assertIn("repair_round: 1", out)
        self.assertIn("contains_native_write_permission_prompt: true", out)

    def test_trusted_cli_stdout_markdown_saves_and_returns_report_ready(self):
        body = (
            "# Twinpet UI-09-C PaymentModal Trusted CLI Read-Only Analysis\n\n"
            "Status: PASS_WITH_NOTES\n\n"
            "## Summary\n"
            "PaymentModal analysis complete for trusted CLI validation.\n\n"
            "## Files inspected\n"
            "- src/components/PaymentModal.tsx\n"
            "- src/components/PaymentModal.css\n\n"
            "## Findings\n"
            "PaymentModal builds payment splits and delegates confirmation. "
            + ("Additional review notes. " * 40)
            + "\n\n"
            "## Red-zone confirmation\n"
            "No product/source/test/config files were modified.\n\n"
            "## Recommended next step\n"
            "Route to AGY UI Lead.\n"
        )
        out = process_claude_worker_report_output(
            body,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertTrue(Path(self.report_path).is_file())

    def test_trusted_cli_prompt_injection_refusal_blocker(self):
        refusal = "This message has hallmarks of prompt injection and I cannot comply."
        out = process_claude_worker_report_output(
            refusal,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("trusted CLI refused report-output contract", out)

    def _resolver_outcome(self, stdout, *, policy=None, repair_rounds_used=0, worker_context=None):
        from worker_workspace import resolve_trusted_cli_report_outcome

        return resolve_trusted_cli_report_outcome(
            stdout,
            policy or self.policy,
            queue_item=self.item,
            worker_context=worker_context,
            cwd=TWINPET,
            repair_rounds_used=repair_rounds_used,
            max_repair_rounds=1,
        )

    def test_trusted_cli_incomplete_stdout_blocker(self):
        out = process_claude_worker_report_output(
            "Short non-report developer reply without enough structure.",
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNone(out)
        outcome = self._resolver_outcome(
            "Short non-report developer reply without enough structure.",
            repair_rounds_used=1,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("trusted CLI report stdout incomplete", outcome.text)
        self.assertIn("trusted_cli_report_salvage_attempted: true", outcome.text)
        self.assertIn("file_exists: False", outcome.text)

    def _valid_salvage_report_body(self) -> str:
        return (
            "# Twinpet UI-09-C PaymentModal Trusted CLI Read-Only Analysis\n\n"
            "Status: PASS\n\n"
            "## Summary\n"
            "PaymentModal analysis complete for trusted CLI validation.\n\n"
            "## Files inspected\n"
            "- src/components/PaymentModal.tsx\n"
            "- src/components/PaymentModal.css\n\n"
            "## Findings\n"
            "PaymentModal builds payment splits and delegates confirmation. "
            + ("Additional review notes. " * 40)
            + "\n\n"
            "## Red-zone confirmation\n"
            "No product/source/test/config files were modified.\n\n"
            "## Recommended next step\n"
            "Route to AGY UI Lead.\n"
        )

    def test_trusted_cli_salvages_existing_report_when_stdout_short(self):
        Path(self.report_path).write_text(self._valid_salvage_report_body(), encoding="utf-8")
        stdout = "Status unchanged. Here's the analysis summary in a short completion note."
        out = process_claude_worker_report_output(
            stdout,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertIn("trusted_cli_report_salvaged=true", out)
        self.assertIn("Status:\nPASS", out)
        self.assertIn(self.report_path.replace("\\", "/"), out.replace("\\", "/"))

    def test_trusted_cli_salvage_missing_file_still_blocks(self):
        out = process_claude_worker_report_output(
            "Status unchanged. Here's the analysis.",
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNone(out)
        outcome = self._resolver_outcome(
            "Status unchanged. Here's the analysis.",
            repair_rounds_used=1,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("trusted CLI report stdout incomplete", outcome.text)
        self.assertIn("salvage_failure_reason: report file not found", outcome.text)

    def test_trusted_cli_salvage_rejects_disallowed_path(self):
        policy = dict(self.policy)
        policy["report_paths"] = ["C:/outside/disallowed-report.md"]
        policy["external_report_write_roots"] = [self.tmp]
        out = process_claude_worker_report_output(
            "Status unchanged. Here's the analysis.",
            policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNone(out)
        outcome = self._resolver_outcome(
            "Status unchanged. Here's the analysis.",
            policy=policy,
            repair_rounds_used=1,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("trusted CLI report stdout incomplete", outcome.text)
        self.assertIn("outside allowed roots", outcome.text)

    def test_trusted_cli_salvage_rejects_too_short_report_file(self):
        Path(self.report_path).write_text("# Too short\nStatus: PASS\n", encoding="utf-8")
        outcome = self._resolver_outcome(
            "Status unchanged. Here's the analysis.",
            repair_rounds_used=1,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("report too short", outcome.text)

    def test_trusted_cli_salvage_rejects_report_missing_sections(self):
        body = (
            "# Report\n\nStatus: PASS\n\n"
            + ("x" * 900)
            + "\n"
        )
        Path(self.report_path).write_text(body, encoding="utf-8")
        outcome = self._resolver_outcome(
            "Status unchanged. Here's the analysis.",
            repair_rounds_used=1,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("missing files inspected or evidence section", outcome.text)

    def test_trusted_cli_native_write_with_valid_report_salvages(self):
        Path(self.report_path).write_text(self._valid_salvage_report_body(), encoding="utf-8")
        native = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory."
        )
        out = process_claude_worker_report_output(
            native,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertIn("native_write_prompt_with_valid_report=true", out)

    def test_attempt_salvage_extracts_status_and_summary(self):
        from worker_workspace import attempt_salvage_trusted_cli_existing_report

        Path(self.report_path).write_text(self._valid_salvage_report_body(), encoding="utf-8")
        result = attempt_salvage_trusted_cli_existing_report(
            self.policy,
            "short stdout",
            queue_item=self.item,
            cwd=TWINPET,
        )
        self.assertTrue(result.salvaged)
        self.assertIn("Status:\nPASS", result.report_ready)
        self.assertIn("PaymentModal analysis complete", result.report_ready)

    def test_wrapper_coordinator_parity_short_stdout_with_valid_file(self):
        Path(self.report_path).write_text(self._valid_salvage_report_body(), encoding="utf-8")
        stdout = "Status unchanged. " + ("x" * 400)
        wrapper_out = process_claude_worker_report_output(
            stdout, self.policy, queue_item=self.item, cwd=TWINPET,
        )
        resolver = self._resolver_outcome(stdout)
        self.assertTrue((wrapper_out or "").startswith("REPORT_READY"))
        self.assertEqual(resolver.kind, "report_ready")
        self.assertTrue(resolver.report_ready.startswith("REPORT_READY"))

    def test_wrapper_coordinator_parity_missing_file_correction_then_blocker(self):
        stdout = "Status unchanged. " + ("x" * 400)
        wrapper_out = process_claude_worker_report_output(
            stdout, self.policy, queue_item=self.item, cwd=TWINPET,
        )
        correction = self._resolver_outcome(stdout, repair_rounds_used=0)
        terminal = self._resolver_outcome(stdout, repair_rounds_used=1)
        self.assertIsNone(wrapper_out)
        self.assertEqual(correction.kind, "correction_prompt")
        self.assertEqual(terminal.kind, "blocker")

    def test_findings_heuristic_rejects_skeletal_stub(self):
        body = (
            "# Report\n\nStatus: PASS\n\n"
            "## Files inspected\n- a.tsx\n\n"
            "## Red-zone confirmation\nNo modifications.\n\n"
            "## Recommended next step\nRoute to coordinator.\n\n"
            + "\n\n".join(f"Short note {i}." for i in range(120))
        )
        from worker_workspace import validate_trusted_cli_existing_report_file

        ok, reason = validate_trusted_cli_existing_report_file(body)
        self.assertFalse(ok)
        self.assertIn("findings", reason)

    def test_threshold_boundary_799_rejected_800_accepted(self):
        from worker_workspace import validate_trusted_cli_existing_report_file

        base = (
            "# Report\n\nStatus: PASS\n\n## Files inspected\n- a.tsx\n\n"
            "## Findings\n"
        )
        tail = (
            "\n\n## Red-zone confirmation\nNo modifications.\n\n"
            "## Recommended next step\nRoute to coordinator.\n"
        )
        base_len = len((base + tail).strip())
        findings_799 = "x" * max(0, 799 - base_len)
        findings_800 = "x" * max(0, 800 - base_len)
        ok799, _ = validate_trusted_cli_existing_report_file(base + findings_799 + tail)
        ok800, reason800 = validate_trusted_cli_existing_report_file(base + findings_800 + tail)
        self.assertFalse(ok799)
        self.assertTrue(ok800, reason800)

    def test_salvaged_report_with_fail_status_still_report_ready(self):
        body = self._valid_salvage_report_body().replace("Status: PASS", "Status: FAIL")
        Path(self.report_path).write_text(body, encoding="utf-8")
        outcome = self._resolver_outcome("short stdout")
        self.assertEqual(outcome.kind, "report_ready")
        self.assertIn("Status:\nFAIL", outcome.report_ready)


class TrustedCliCoordinatorSalvageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.report_path = str(Path(self.tmp) / "trusted-cli-report.md")
        self.policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "trusted_direct_repo_cli": True,
            "policy_id": "twinpet-ui-09-c-payment-modal-trusted-cli",
            "write_files": [],
            "external_report_write_roots": [self.tmp],
            "report_paths": [self.report_path],
            "workspace": {"root": TWINPET},
        }
        self.worker_context = {
            "workspace_policy": self.policy,
            "policy_id": self.policy["policy_id"],
            "allowed_report_roots": [self.tmp],
            "report_paths": [self.report_path],
            "report_paths_by_role": {"developer": self.report_path},
        }

    def _valid_report_body(self) -> str:
        return (
            "# Twinpet UI-09-C PaymentModal Trusted CLI Read-Only Analysis\n\n"
            "Status: PASS\n\n"
            "## Summary\n"
            "PaymentModal analysis complete for trusted CLI validation.\n\n"
            "## Files inspected\n"
            "- src/components/PaymentModal.tsx\n\n"
            "## Findings\n"
            "PaymentModal builds payment splits and delegates confirmation. "
            + ("Additional review notes. " * 40)
            + "\n\n"
            "## Red-zone confirmation\n"
            "No product/source/test/config files were modified.\n\n"
            "## Recommended next step\n"
            "Route to AGY UI Lead.\n"
        )

    def test_coordinator_salvages_short_stdout_with_valid_file(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        Path(self.report_path).write_text(self._valid_report_body(), encoding="utf-8")
        stdout = "Status unchanged. " + ("x" * 400)
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
        )
        action = on_worker_output(
            state, "developer", stdout, worker_context=self.worker_context,
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")

    def test_coordinator_native_write_with_valid_file_salvages(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        Path(self.report_path).write_text(self._valid_report_body(), encoding="utf-8")
        native = (
            "The report write requires your explicit approval since the path is "
            "outside the repo working directory."
        )
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
        )
        action = on_worker_output(
            state, "developer", native, worker_context=self.worker_context,
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")

    def test_coordinator_fail_status_in_file_is_terminal(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        body = self._valid_report_body().replace("Status: PASS", "Status: FAIL")
        Path(self.report_path).write_text(body, encoding="utf-8")
        stdout = "Status unchanged. " + ("x" * 400)
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
        )
        action = on_worker_output(
            state, "developer", stdout, worker_context=self.worker_context,
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("FAIL", action.prompt_context or "")

    def test_coordinator_exhausted_repair_terminal_incomplete(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        stdout = "Status unchanged. " + ("x" * 400)
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
            trusted_cli_report_bridge_repair_rounds=1,
            max_trusted_cli_report_bridge_repair_rounds=1,
        )
        action = on_worker_output(
            state, "developer", stdout, worker_context=self.worker_context,
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("trusted CLI report stdout incomplete", action.prompt_context or "")

    def test_coordinator_salvaged_blocker_status_is_terminal(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output

        body = self._valid_report_body().replace("Status: PASS", "Status: BLOCKER")
        Path(self.report_path).write_text(body, encoding="utf-8")
        stdout = "Status unchanged. " + ("x" * 400)
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
        )
        action = on_worker_output(
            state, "developer", stdout, worker_context=self.worker_context,
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("BLOCKER", action.prompt_context or "")


class TrustedCliReportPathMembershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.report_path = str(Path(self.tmp) / "trusted-cli-report.md")
        self.arbitrary_path = str(Path(self.tmp) / "arbitrary.md")
        self.policy = {
            "mode": "read-only",
            "analysis_report_only": True,
            "trusted_direct_repo_cli": True,
            "policy_id": "twinpet-ui-09-c-payment-modal-trusted-cli",
            "write_files": [],
            "external_report_write_roots": [self.tmp],
            "report_paths": [self.report_path],
        }
        self.worker_context = {
            "workspace_policy": self.policy,
            "allowed_report_roots": [self.tmp],
            "report_paths": [self.report_path],
            "report_paths_by_role": {"developer": self.report_path},
        }
        self.item = {
            "workspace_policy_context": {
                "relay_kind": "session_turn",
                "session_id": 99,
                "session_role": "developer",
                "policy_id": self.policy["policy_id"],
                "policy_mode": "read-only",
                "workspace_root": TWINPET,
                "trusted_direct_repo_cli": True,
            },
        }

    def test_report_ready_arbitrary_path_blocked(self):
        from tests.test_report_orchestration import _report_ready
        from worker_workspace import resolve_trusted_cli_report_outcome

        Path(self.arbitrary_path).write_text("# Arbitrary\n", encoding="utf-8")
        ready = _report_ready(self.arbitrary_path)
        outcome = resolve_trusted_cli_report_outcome(
            ready,
            self.policy,
            worker_context=self.worker_context,
        )
        self.assertEqual(outcome.kind, "blocker")
        self.assertIn("not an expected report path", outcome.text)

    def test_report_ready_expected_path_accepted(self):
        from tests.test_report_orchestration import _report_ready
        from worker_workspace import resolve_trusted_cli_report_outcome

        Path(self.report_path).write_text(
            "# Report\n\nStatus: PASS\n\n" + ("x" * 900),
            encoding="utf-8",
        )
        ready = _report_ready(self.report_path)
        outcome = resolve_trusted_cli_report_outcome(
            ready,
            self.policy,
            worker_context=self.worker_context,
        )
        self.assertEqual(outcome.kind, "report_ready")

    def test_bridge_arbitrary_path_blocked(self):
        arbitrary = self.arbitrary_path
        body = (
            f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
            f"Path: {arbitrary}\n"
            "Status: PASS\n"
            "Summary: test\n"
            "Next recommended role: coordinator\n"
            "---\n"
            "# Report\n\nDone.\n"
            f"{REPORT_FILE_WRITE_END_MARKER}"
        )
        out = process_claude_worker_report_output(
            body,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
            worker_context=self.worker_context,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("not an expected report path", out)
        self.assertFalse(Path(arbitrary).is_file())

    def test_bridge_expected_path_accepted(self):
        body = (
            f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
            f"Path: {self.report_path}\n"
            "Status: PASS\n"
            "Summary: test\n"
            "Next recommended role: coordinator\n"
            "---\n"
            "# Report\n\nDone.\n"
            f"{REPORT_FILE_WRITE_END_MARKER}"
        )
        out = process_claude_worker_report_output(
            body,
            self.policy,
            queue_item=self.item,
            cwd=TWINPET,
            worker_context=self.worker_context,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("REPORT_READY"))
        self.assertTrue(Path(self.report_path).is_file())

    def test_wrapper_formatted_refusal_blocker_is_terminal(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output
        from worker_workspace import format_trusted_cli_refusal_blocker

        blocker = format_trusted_cli_refusal_blocker(
            text="refusal",
            policy=self.policy,
            workspace_profile=self.policy["policy_id"],
            workspace_mode="read-only",
            report_path=self.report_path,
        )
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
        )
        action = on_worker_output(
            state,
            "developer",
            blocker,
            worker_context=self.worker_context,
        )
        self.assertTrue(action.is_terminal)
        self.assertIn("trusted CLI refused report-output contract", action.prompt_context or "")

    def test_wrapper_formatted_incomplete_with_valid_report_salvages(self):
        from coordinator_loop import CoordinatorLoopState, on_worker_output
        from worker_workspace import format_trusted_cli_incomplete_report_blocker

        body = (
            "# Twinpet Analysis\n\nStatus: PASS\n\n## Summary\nDone.\n\n"
            "## Files inspected\n- a.tsx\n\n## Findings\n"
            + ("Review notes. " * 80)
            + "\n\n## Red-zone confirmation\nNo modifications.\n\n"
            "## Recommended next step\nRoute coordinator.\n"
        )
        Path(self.report_path).write_text(body, encoding="utf-8")
        blocker = format_trusted_cli_incomplete_report_blocker(
            text="short stdout",
            policy=self.policy,
            workspace_profile=self.policy["policy_id"],
            workspace_mode="read-only",
            report_path=self.report_path,
            repair_round=1,
            max_repair_rounds=1,
        )
        state = CoordinatorLoopState(
            phase=__import__("coordinator_loop").CoordinatorPhase.AWAIT_DEVELOPER,
            awaiting_role="developer",
            report_orchestrated=True,
            classified=True,
            requires_agy=True,
            session_workspace_profile=self.policy["policy_id"],
            session_workspace_mode="read-only",
            trusted_cli_report_bridge_repair_rounds=1,
            max_trusted_cli_report_bridge_repair_rounds=1,
        )
        action = on_worker_output(
            state,
            "developer",
            blocker,
            worker_context=self.worker_context,
        )
        self.assertFalse(action.is_terminal)
        self.assertEqual(action.target_role, "coordinator")


if __name__ == "__main__":
    unittest.main()
