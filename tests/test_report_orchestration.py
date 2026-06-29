"""Report-orchestrated coordinator flow tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import config_loader
import coordinator_loop as cl
import workspace_policy as wp
from coordinator_loop import on_coordinator_output, on_session_start, on_worker_output
from report_orchestration import (
    DEFAULT_MAX_REPORT_PROMPT_CHARS,
    build_report_orchestrated_dispatch_prompt,
    ingest_worker_report_output,
    is_report_orchestrated_policy,
    is_twinpet_repo_path,
    parse_report_ready,
    read_report_file,
    report_content_fits_prompt,
    save_inline_report_to_path,
    validate_report_path,
)
from session_relay import coordinator_loop_worker_output_contract


def _analysis_policy() -> dict:
    profiles = config_loader.get_workspace_profiles(config_loader.load_config())
    return wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
            "workspace_mode": "read-only-analysis",
        },
    ).policy


def _report_ready(path: str, status: str = "PASS") -> str:
    return (
        "REPORT_READY\n\n"
        f"Status:\n{status}\n\n"
        f"Report:\n{path}\n\n"
        "Summary:\nshort summary\n\n"
        "Next recommended role:\ncoordinator\n\n"
        "Notes:\nnotes\n"
    )


class ReportReadyParsingTests(unittest.TestCase):
    def test_parse_report_ready(self):
        parsed = parse_report_ready(_report_ready(r"C:\x\report.md"))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.status, "PASS")
        self.assertEqual(parsed.report_path, r"C:\x\report.md")
        self.assertEqual(parsed.summary, "short summary")


class ReportPathValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]

    def test_md_under_allowed_root(self):
        path = str(Path(self.tmp) / "report.md")
        Path(path).write_text("# ok", encoding="utf-8")
        ok, reason, resolved = validate_report_path(path, allowed_roots=self.roots)
        self.assertTrue(ok)
        self.assertIsNotNone(resolved)

    def test_rejects_outside_root(self):
        ok, reason, _ = validate_report_path(
            r"C:\Users\Narachat\Desktop\outside-report.md",
            allowed_roots=self.roots,
        )
        self.assertFalse(ok)
        self.assertIn("outside allowed roots", reason)

    def test_rejects_non_md(self):
        path = str(Path(self.tmp) / "report.txt")
        ok, reason, _ = validate_report_path(path, allowed_roots=self.roots)
        self.assertFalse(ok)
        self.assertIn(".md", reason)

    def test_rejects_twinpet_repo_path(self):
        self.assertTrue(
            is_twinpet_repo_path(r"C:\Users\Narachat\twinpet-pos\src\PaymentModal.tsx")
        )
        ok, reason, _ = validate_report_path(
            r"C:\Users\Narachat\twinpet-pos\src\report.md",
            allowed_roots=[r"C:\Users\Narachat\twinpet-pos\src"],
        )
        self.assertFalse(ok)
        self.assertIn("Twinpet", reason)


class ReportReadAndIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.path = Path(self.tmp) / "dev.md"
        self.body = "# Developer\n\nPaymentModal data flow blueprint risks."
        self.path.write_text(self.body, encoding="utf-8")

    def test_read_records_sha256_and_size(self):
        file_bytes = self.path.read_bytes()
        ok, content, sha, size = read_report_file(self.path)
        self.assertTrue(ok)
        self.assertIn("PaymentModal data flow", content.replace("\r\n", "\n"))
        self.assertEqual(sha, hashlib.sha256(file_bytes).hexdigest())
        self.assertEqual(size, len(file_bytes))

    def test_ingest_report_ready(self):
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(self.path)),
            allowed_roots=self.roots,
        )
        self.assertTrue(ingest.ok)
        assert ingest.record is not None
        self.assertEqual(ingest.record.role, "developer")
        self.assertEqual(ingest.record.path, str(self.path.resolve()))

    def test_missing_file_blocks(self):
        missing = str(Path(self.tmp) / "missing.md")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(missing),
            allowed_roots=self.roots,
        )
        self.assertFalse(ingest.ok)
        self.assertIn("not found", ingest.blocker)

    def test_report_too_large_blocks_not_chunks(self):
        huge = Path(self.tmp) / "huge.md"
        huge.write_text("x" * 5000, encoding="utf-8")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(huge)),
            allowed_roots=self.roots,
            max_prompt_chars=1000,
        )
        self.assertFalse(ingest.ok)
        self.assertIn("too large", ingest.blocker)
        self.assertNotIn("COMPRESSED", ingest.blocker)

    def test_inline_report_begin_end_saved(self):
        target = str(Path(self.tmp) / "inline.md")
        text = (
            "REPORT_READY\n\nStatus:\nPASS\n\n"
            "REPORT_BEGIN\n# inline report\nPaymentModal risks\nREPORT_END"
        )
        # force inline path by not using REPORT_READY report field with existing file
        text = (
            "REPORT_BEGIN\n# inline report\nPaymentModal risks\nREPORT_END"
        )
        ingest = ingest_worker_report_output(
            "developer",
            text,
            allowed_roots=self.roots,
            expected_report_paths=[target],
        )
        self.assertTrue(ingest.ok)
        self.assertTrue(Path(target).is_file())


class ReportPromptConstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev.md"
        self.dev_path.write_text("# Dev\nPaymentModal analysis", encoding="utf-8")
        self.agy_path = Path(self.tmp) / "agy.md"
        self.agy_path.write_text("# AGY\nUX notes", encoding="utf-8")
        dev_ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(self.dev_path)),
            allowed_roots=self.roots,
        )
        agy_ingest = ingest_worker_report_output(
            "ui_lead",
            _report_ready(str(self.agy_path)),
            allowed_roots=self.roots,
        )
        self.records = [dev_ingest.record.to_dict(), agy_ingest.record.to_dict()]

    def test_agy_prompt_contains_developer_report(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="ui_lead",
            report_records=self.records,
            project="twinpet",
            phase="UX",
            subject="review",
        )
        self.assertTrue(result.ok)
        self.assertIn("REPORT CONTENT:", result.prompt)
        self.assertIn("PaymentModal analysis", result.prompt)
        self.assertIn("TO: AGY UI Lead", result.prompt)

    def test_reviewer_prompt_contains_developer_and_agy(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=self.records,
            project="twinpet",
            phase="Review",
            subject="codex review",
        )
        self.assertTrue(result.ok)
        self.assertIn("PaymentModal analysis", result.prompt)
        self.assertIn("UX notes", result.prompt)
        self.assertIn("TO: Codex Reviewer", result.prompt)
        self.assertIn("do not inspect repository files", result.prompt.lower())

    def test_reviewer_blocks_without_developer_report(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=[self.records[1]],
            project="twinpet",
            phase="Review",
            subject="codex review",
        )
        self.assertFalse(result.ok)
        self.assertIn("missing developer analysis", result.blocker)

    def test_oversize_blocks(self):
        big = Path(self.tmp) / "big.md"
        big.write_text("y" * 8000, encoding="utf-8")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(big)),
            allowed_roots=self.roots,
        )
        result = build_report_orchestrated_dispatch_prompt(
            role="ui_lead",
            report_records=[ingest.record.to_dict()],
            project="twinpet",
            phase="UX",
            subject="review",
            max_chars=1000,
        )
        self.assertFalse(result.ok)
        self.assertIn("too large", result.blocker)


class ReportOrchestrationCoordinatorLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev.md"
        self.dev_path.write_text("# Dev\nPaymentModal", encoding="utf-8")

    def test_developer_report_ready_routes_coordinator(self):
        state, _ = on_session_start(
            "analysis",
            session_meta={"report_orchestrated": True},
        )
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        on_coordinator_output(state, "NEXT: developer\nBegin analysis.")
        ctx = {
            "report_paths": [str(self.dev_path)],
            "allowed_report_roots": self.roots,
            "max_report_prompt_chars": DEFAULT_MAX_REPORT_PROMPT_CHARS,
        }
        action = on_worker_output(
            state,
            "developer",
            _report_ready(str(self.dev_path)),
            worker_context=ctx,
        )
        self.assertEqual(action.target_role, "coordinator")
        self.assertTrue(state.report_records)
        self.assertTrue(state.developer_has_substantial_output)

    def test_analysis_policy_enables_report_orchestration(self):
        policy = _analysis_policy()
        self.assertTrue(is_report_orchestrated_policy(policy))

    def test_worker_contract_report_ready(self):
        contract = coordinator_loop_worker_output_contract(
            "developer",
            workspace_bound=True,
            report_orchestrated=True,
        )
        self.assertIn("REPORT_READY", contract)


class ReportOrchestrationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev = Path(self.tmp) / "dev.md"
        self.dev.write_text("# Dev\nPaymentModal", encoding="utf-8")
        self.rev = Path(self.tmp) / "rev.md"
        self.rev.write_text("# Reviewer\nREQUEST changes to blueprint", encoding="utf-8")

    def test_request_changes_routes_back_with_reviewer_report(self):
        state, _ = on_session_start(
            "analysis",
            session_meta={"report_orchestrated": True, "workspace_mode": "read-only"},
        )
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        on_coordinator_output(state, "NEXT: developer\nBegin analysis.")
        ctx = {
            "allowed_report_roots": self.roots,
            "report_paths": [str(self.dev)],
            "max_report_prompt_chars": DEFAULT_MAX_REPORT_PROMPT_CHARS,
        }
        on_worker_output(state, "developer", _report_ready(str(self.dev)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        agy = Path(self.tmp) / "agy.md"
        agy.write_text("# AGY\nok", encoding="utf-8")
        on_worker_output(state, "ui_lead", _report_ready(str(agy)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: reviewer\nReview.")
        on_worker_output(
            state,
            "reviewer",
            _report_ready(str(self.rev), status="REQUEST_CHANGES"),
            worker_context=ctx,
        )
        self.assertTrue(state.awaiting_developer_correction)
        self.assertEqual(state.last_reviewer_verdict, "REQUEST_CHANGES")


if __name__ == "__main__":
    unittest.main()
