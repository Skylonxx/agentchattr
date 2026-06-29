"""Report-orchestrated coordinator flow tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import config_loader
import coordinator_loop as cl
import workspace_policy as wp
from coordinator_loop import (
    CoordinatorPhase,
    on_coordinator_output,
    on_session_start,
    on_worker_output,
)
from report_orchestration import (
    DEFAULT_MAX_REPORT_PROMPT_CHARS,
    HANDOFF_FOR_AGY_BEGIN,
    HANDOFF_FOR_AGY_END,
    HANDOFF_FOR_CODEX_REVIEWER_BEGIN,
    HANDOFF_FOR_CODEX_REVIEWER_END,
    HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN,
    HANDOFF_FOR_DEVELOPER_CORRECTION_END,
    HANDOFF_FOR_FINAL_BEGIN,
    HANDOFF_FOR_FINAL_END,
    build_report_orchestrated_dispatch_prompt,
    build_report_orchestrated_final_attachment,
    extract_handoff_block,
    ingest_worker_report_output,
    is_report_orchestrated_policy,
    is_twinpet_repo_path,
    parse_report_ready,
    parse_report_write_failed,
    read_report_file,
    report_content_fits_prompt,
    save_inline_report_to_path,
    validate_initial_developer_preflight,
    validate_report_path,
    verify_report_write_permission,
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


def _agy_handoff(text: str | None = None) -> str:
    default = (
        "Current UI structure: PaymentModal header, line items, tender controls. "
        "Visual hierarchy concerns: primary CTA competes with discount row. "
        "Cashier workflow: confirm step may be skipped under load. "
        "Questions for AGY: is split-tender layout clear on 1366px?"
    )
    return (
        f"{HANDOFF_FOR_AGY_BEGIN}\n"
        f"{text or default}\n"
        f"{HANDOFF_FOR_AGY_END}"
    )


def _codex_handoff(text: str | None = None) -> str:
    default = (
        "Payment data flow: PaymentModal -> useCheckout -> asyncCheckout payload. "
        "Risks: double-submit on slow network, underpayment not blocked in UI. "
        "UNKNOWN FROM SNAPSHOT: Firestore receipt write timing. "
        "Review questions: are off-repo UI edits bounded to PaymentModal.css only?"
    )
    return (
        f"{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}\n"
        f"{text or default}\n"
        f"{HANDOFF_FOR_CODEX_REVIEWER_END}"
    )


def _correction_handoff(text: str | None = None) -> str:
    default = (
        "Add missing split-payment analysis, document async checkout race, "
        "and clarify which behavior changes need Product Owner approval."
    )
    return (
        f"{HANDOFF_FOR_DEVELOPER_CORRECTION_BEGIN}\n"
        f"{text or default}\n"
        f"{HANDOFF_FOR_DEVELOPER_CORRECTION_END}"
    )


def _final_handoff(text: str = "PASS_WITH_NOTES — proceed to safety gate.") -> str:
    return (
        f"{HANDOFF_FOR_FINAL_BEGIN}\n"
        f"{text}\n"
        f"{HANDOFF_FOR_FINAL_END}"
    )


def _developer_report_body(
    *,
    main: str = "# Developer\nPaymentModal analysis",
    agy: str | None = None,
    codex: str | None = None,
) -> str:
    parts = [main, _agy_handoff(agy)]
    parts.append(_codex_handoff(codex))
    return "\n\n".join(parts)


def _agy_report_body(
    *,
    main: str = "# AGY\nUX notes",
    codex: str | None = None,
    correction: str | None = None,
) -> str:
    default_codex = (
        "AGY UX verdict: PASS_WITH_NOTES. Cashier ergonomics concern on tender keypad spacing. "
        "Concerns for Codex: confirm CSS-only edits do not change checkout payload behavior. "
        "Approval notes: responsive layout risks on 1366px need explicit guardrails."
    )
    parts = [main, _codex_handoff(codex or default_codex)]
    if correction is not None:
        parts.append(_correction_handoff(correction))
    return "\n\n".join(parts)


def _reviewer_report_body(
    *,
    main: str = "# Reviewer\nREQUEST changes",
    correction: str | None = None,
    final: str | None = None,
) -> str:
    parts = [main]
    parts.append(_correction_handoff(correction))
    parts.append(_final_handoff(final or "REQUEST_CHANGES pending developer correction."))
    return "\n\n".join(parts)


class ReportReadyParsingTests(unittest.TestCase):
    def test_parse_report_ready(self):
        parsed = parse_report_ready(_report_ready(r"C:\x\report.md"))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.status, "PASS")
        self.assertEqual(parsed.report_path, r"C:\x\report.md")
        self.assertEqual(parsed.summary, "short summary")

    def test_parse_report_write_failed(self):
        parsed = parse_report_write_failed(
            "REPORT_WRITE_FAILED\n\n"
            "Reason:\ndenied\n\n"
            "Expected report:\nC:\\x\\report.md\n\n"
            "Status:\nFAIL\n"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.reason, "denied")
        self.assertEqual(parsed.expected_report_path, r"C:\x\report.md")


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

    def test_report_write_failed_blocks(self):
        ingest = ingest_worker_report_output(
            "developer",
            "REPORT_WRITE_FAILED\n\n"
            "Reason:\npermission denied\n\n"
            f"Expected report:\n{self.path}\n\n"
            "Status:\nFAIL\n",
            allowed_roots=self.roots,
        )
        self.assertFalse(ingest.ok)
        self.assertIn("external report write failed", ingest.blocker)

    def test_report_too_large_ingests_then_handoff_repair_at_dispatch(self):
        huge = Path(self.tmp) / "huge.md"
        huge.write_text("x" * 5000, encoding="utf-8")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(huge)),
            allowed_roots=self.roots,
            max_prompt_chars=1000,
        )
        self.assertTrue(ingest.ok)
        result = build_report_orchestrated_dispatch_prompt(
            role="ui_lead",
            report_records=[ingest.record.to_dict()],
            project="twinpet",
            phase="UX",
            subject="review",
            max_chars=1000,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "developer")
        self.assertIn("missing required coordinator handoff block", result.prompt)
        self.assertIn("Do not chunk", result.prompt)
        self.assertNotIn("COMPRESSED", result.prompt)

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

    def test_no_host_save_as_default_path(self):
        target = str(Path(self.tmp) / "missing-default.md")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(target),
            allowed_roots=self.roots,
            expected_report_paths=[target],
        )
        self.assertFalse(ingest.ok)
        self.assertIn("not found", ingest.blocker)


class ReportPromptConstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev.md"
        self.dev_path.write_text(_developer_report_body(), encoding="utf-8")
        self.agy_path = Path(self.tmp) / "agy.md"
        self.agy_path.write_text(_agy_report_body(), encoding="utf-8")
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

    def test_agy_prompt_uses_handoff_not_full_developer_report(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="ui_lead",
            report_records=self.records,
            project="twinpet",
            phase="UX",
            subject="review",
            expected_output_path=str(Path(self.tmp) / "agy-out.md"),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("DEVELOPER COORDINATOR HANDOFF FOR AGY:", result.prompt)
        self.assertIn("Questions for AGY", result.prompt)
        self.assertNotIn("REPORT CONTENT:", result.prompt)
        self.assertNotIn("# Developer\nPaymentModal analysis", result.prompt)
        self.assertIn("TO: AGY UI Lead", result.prompt)
        self.assertIn("EXPECTED REPORT OUTPUT PATH", result.prompt)

    def test_reviewer_prompt_uses_handoffs_not_full_reports(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=self.records,
            project="twinpet",
            phase="Review",
            subject="codex review",
            expected_output_path=str(Path(self.tmp) / "codex-out.md"),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("DEVELOPER COORDINATOR HANDOFF FOR CODEX REVIEWER:", result.prompt)
        self.assertIn("AGY COORDINATOR HANDOFF FOR CODEX REVIEWER:", result.prompt)
        self.assertIn("AGY UX verdict", result.prompt)
        self.assertNotIn("REPORT CONTENT:", result.prompt)
        self.assertNotIn("# Developer\nPaymentModal analysis", result.prompt)
        self.assertNotIn("# AGY\nUX notes", result.prompt)
        self.assertIn("TO: Codex Reviewer", result.prompt)
        self.assertIn("do not inspect repository files", result.prompt.lower())
        self.assertIn("REPORT_WRITE_FAILED", result.prompt)

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

    def test_report_missing_handoff_routes_developer_repair(self):
        bare = Path(self.tmp) / "bare.md"
        bare.write_text("# Dev\nNo handoff blocks", encoding="utf-8")
        ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(bare)),
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
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "developer")
        self.assertIn("REQUEST_CHANGES: report missing required coordinator handoff block", result.prompt)
        self.assertIn(HANDOFF_FOR_AGY_BEGIN, result.prompt)
        self.assertNotIn("REPORT CONTENT:", result.prompt)

    def test_oversized_handoff_routes_handoff_rewrite(self):
        big_handoff = "x" * 5000
        big = Path(self.tmp) / "big.md"
        big.write_text(_developer_report_body(agy=big_handoff), encoding="utf-8")
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
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "developer")
        self.assertIn("handoff block is too large", result.prompt)
        self.assertIn("Do not chunk", result.prompt)


class InitialDeveloperPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.policy = _analysis_policy()
        self.roots = list(self.policy.get("external_report_write_roots") or [])
        self.report_path = str((self.policy.get("report_paths") or [""])[0])
        self.prompt_memo = (
            "Analyze PaymentModal for UI-09-C scope. Map data flow and risks."
        )

    def test_initial_developer_prompt_without_prior_reports(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="Twinpet POS",
            phase="UI-09-C PaymentModal analysis",
            subject="Create initial technical analysis report",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertNotIn("no report-orchestrated prompt available", result.blocker)
        self.assertIn("TO: Claude Developer", result.prompt)
        self.assertIn("MODE: read-only analysis with external report output", result.prompt)

    def test_initial_developer_includes_prompt_memo(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("PROMPT MEMO:", result.prompt)
        self.assertIn("PaymentModal for UI-09-C", result.prompt)

    def test_initial_developer_includes_snapshot_paths(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("READ-ONLY SNAPSHOTS:", result.prompt)
        self.assertIn("PaymentModal.tsx", result.prompt)

    def test_initial_developer_includes_expected_report_path(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("REPORT OUTPUT:", result.prompt)
        self.assertIn(self.report_path, result.prompt)

    def test_initial_developer_includes_external_write_permission(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("EXTERNAL REPORT WRITE ALLOWLIST:", result.prompt)
        self.assertTrue(any(root in result.prompt for root in self.roots))

    def test_initial_developer_forbids_twinpet_writes(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        low = result.prompt.lower()
        self.assertIn("may not write inside the twinpet workspace", low)
        self.assertIn("do not modify product source", low)

    def test_initial_developer_includes_report_ready_contract(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("REPORT_READY", result.prompt)
        self.assertIn("REPORT_WRITE_FAILED", result.prompt)
        self.assertIn(HANDOFF_FOR_AGY_BEGIN, result.prompt)
        self.assertIn(HANDOFF_FOR_CODEX_REVIEWER_BEGIN, result.prompt)

    def test_initial_developer_requires_handoff_blocks(self):
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet",
            phase="analysis",
            subject="initial",
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
            expected_output_path=self.report_path,
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok)
        self.assertIn("REQUIRED HANDOFF BLOCKS IN YOUR REPORT", result.prompt)
        self.assertIn("UNKNOWN FROM SNAPSHOT", result.prompt)

    def test_missing_snapshots_blocks_with_clear_diagnostic(self):
        policy = dict(self.policy)
        policy["read_paths"] = []
        ok, blocker = validate_initial_developer_preflight(
            policy,
            prompt_memo_body=self.prompt_memo,
            expected_output_path=self.report_path,
        )
        self.assertFalse(ok)
        self.assertEqual(blocker, "BLOCKER: developer initial prompt missing source snapshots")

    def test_missing_prompt_memo_blocks_with_clear_diagnostic(self):
        ok, blocker = validate_initial_developer_preflight(
            self.policy,
            prompt_memo_body="",
            expected_output_path=self.report_path,
        )
        self.assertFalse(ok)
        self.assertEqual(blocker, "BLOCKER: developer initial prompt missing prompt memo")

    def test_developer_correction_uses_handoff_not_full_reports(self):
        rev_path = Path(self.tmp) / "rev.md"
        rev_path.write_text(_reviewer_report_body(), encoding="utf-8")
        dev_path = Path(self.tmp) / "prior-dev.md"
        dev_path.write_text(_developer_report_body(main="# Dev\nPrior analysis"), encoding="utf-8")
        correction_roots = [self.tmp]
        rev_ingest = ingest_worker_report_output(
            "reviewer",
            _report_ready(str(rev_path), status="REQUEST_CHANGES"),
            allowed_roots=correction_roots,
        )
        dev_ingest = ingest_worker_report_output(
            "developer",
            _report_ready(str(dev_path)),
            allowed_roots=correction_roots,
        )
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[dev_ingest.record.to_dict(), rev_ingest.record.to_dict()],
            project="twinpet",
            phase="correction",
            subject="address reviewer",
            awaiting_developer_correction=True,
            developer_correction_source="reviewer",
            expected_output_path=str(dev_path),
            external_report_write_roots=correction_roots,
            prompt_memo_body=self.prompt_memo,
            policy=self.policy,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("Report correction", result.prompt)
        self.assertIn("CODEX REVIEWER CORRECTION HANDOFF", result.prompt)
        self.assertIn("split-payment analysis", result.prompt)
        self.assertNotIn("REPORT CONTENT:", result.prompt)
        self.assertNotIn("# Dev\nPrior analysis", result.prompt)


class ReportOrchestrationCoordinatorLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev.md"
        self.dev_path.write_text(_developer_report_body(main="# Dev\nPaymentModal"), encoding="utf-8")

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
        self.assertIn("REPORT_WRITE_FAILED", contract)

    def test_preflight_blocks_when_report_write_permission_missing(self):
        policy = _analysis_policy()
        policy = dict(policy)
        policy["external_report_write_roots"] = []
        ok, blocker = verify_report_write_permission(policy)
        self.assertFalse(ok)
        self.assertIn("not enabled", blocker)

    def test_preflight_passes_when_report_write_permission_configured(self):
        policy = _analysis_policy()
        ok, blocker = verify_report_write_permission(policy)
        self.assertTrue(ok)
        self.assertEqual(blocker, "")


class ReportOrchestrationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev = Path(self.tmp) / "dev.md"
        self.dev.write_text(_developer_report_body(main="# Dev\nPaymentModal"), encoding="utf-8")
        self.rev = Path(self.tmp) / "rev.md"
        self.rev.write_text(_reviewer_report_body(main="# Reviewer\nREQUEST changes to blueprint"), encoding="utf-8")

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
        agy.write_text(_agy_report_body(main="# AGY\nok"), encoding="utf-8")
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


def _worker_ctx(tmp: str, dev: Path, agy: Path | None = None, rev: Path | None = None) -> dict:
    by_role = {"developer": str(dev)}
    if agy:
        by_role["ui_lead"] = str(agy)
    if rev:
        by_role["reviewer"] = str(rev)
    return {
        "allowed_report_roots": [tmp],
        "report_paths": [str(dev)],
        "report_paths_by_role": by_role,
        "max_report_prompt_chars": DEFAULT_MAX_REPORT_PROMPT_CHARS,
    }


class AgyOnlyCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev_path = Path(self.tmp) / "dev.md"
        self.dev_path.write_text(_developer_report_body(main="# Dev\nPrior analysis"), encoding="utf-8")
        self.agy_path = Path(self.tmp) / "agy.md"
        self.agy_path.write_text(
            _agy_report_body(
                main="# AGY\nREQUEST UX changes",
                correction=(
                    "Improve visual hierarchy on PaymentModal header and clarify "
                    "keyboard focus order for cashier speed."
                ),
            ),
            encoding="utf-8",
        )
        self.policy = _analysis_policy()
        self.memo = "Prompt memo for AGY correction path."

    def test_agy_request_changes_routes_developer_correction(self):
        state, _ = on_session_start(
            "analysis",
            session_meta={"report_orchestrated": True},
        )
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        ctx = _worker_ctx(self.tmp, self.dev_path, self.agy_path)
        on_coordinator_output(state, "NEXT: developer\nBegin.")
        on_worker_output(state, "developer", _report_ready(str(self.dev_path)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        on_worker_output(
            state,
            "ui_lead",
            _report_ready(str(self.agy_path), status="REQUEST_CHANGES"),
            worker_context=ctx,
        )
        self.assertTrue(state.awaiting_developer_correction)
        self.assertEqual(state.developer_correction_source, "ui_lead")
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=state.report_records,
            project="twinpet",
            phase="correction",
            subject="agy correction",
            awaiting_developer_correction=True,
            developer_correction_source="ui_lead",
            prompt_memo_body=self.memo,
            policy=self.policy,
            expected_output_path=str(self.dev_path),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(result.ok, result.blocker)
        self.assertIn("Report correction from UI Lead review", result.prompt)
        self.assertIn("AGY CORRECTION HANDOFF", result.prompt)
        self.assertIn("visual hierarchy", result.prompt)
        self.assertNotIn("REPORT CONTENT:", result.prompt)
        self.assertNotIn("no report-orchestrated prompt available", result.blocker)

    def test_after_agy_correction_routes_ui_lead_recheck(self):
        state, _ = on_session_start(
            "analysis",
            session_meta={"report_orchestrated": True},
        )
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        ctx = _worker_ctx(self.tmp, self.dev_path, self.agy_path)
        on_coordinator_output(state, "NEXT: developer\nBegin.")
        on_worker_output(state, "developer", _report_ready(str(self.dev_path)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        on_worker_output(
            state,
            "ui_lead",
            _report_ready(str(self.agy_path), status="REQUEST_CHANGES"),
            worker_context=ctx,
        )
        on_coordinator_output(state, "NEXT: developer\nCorrect per AGY.")
        action = on_worker_output(
            state,
            "developer",
            _report_ready(str(self.dev_path), status="PASS"),
            worker_context=ctx,
        )
        self.assertEqual(action.target_role, "coordinator")
        self.assertIn("ui_lead", action.prompt_context.lower())


class OversizedReportRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]

    def test_oversized_agy_handoff_routes_ui_lead_rewrite(self):
        dev = Path(self.tmp) / "dev.md"
        dev.write_text(_developer_report_body(), encoding="utf-8")
        agy = Path(self.tmp) / "agy.md"
        agy.write_text(
            _agy_report_body(codex="a" * 5000),
            encoding="utf-8",
        )
        dev_ingest = ingest_worker_report_output(
            "developer", _report_ready(str(dev)), allowed_roots=self.roots,
        )
        agy_ingest = ingest_worker_report_output(
            "ui_lead", _report_ready(str(agy)), allowed_roots=self.roots,
        )
        result = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=[dev_ingest.record.to_dict(), agy_ingest.record.to_dict()],
            project="twinpet",
            phase="review",
            subject="review",
            max_chars=1000,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "ui_lead")
        self.assertIn("handoff block is too large", result.prompt)

    def test_oversized_reviewer_correction_handoff_routes_reviewer_rewrite(self):
        rev = Path(self.tmp) / "rev.md"
        rev.write_text(
            _reviewer_report_body(correction="r" * 5000),
            encoding="utf-8",
        )
        dev = Path(self.tmp) / "dev.md"
        dev.write_text(_developer_report_body(), encoding="utf-8")
        rev_ingest = ingest_worker_report_output(
            "reviewer", _report_ready(str(rev), status="REQUEST_CHANGES"), allowed_roots=self.roots,
        )
        dev_ingest = ingest_worker_report_output(
            "developer", _report_ready(str(dev)), allowed_roots=self.roots,
        )
        result = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[dev_ingest.record.to_dict(), rev_ingest.record.to_dict()],
            project="twinpet",
            phase="correction",
            subject="correction",
            awaiting_developer_correction=True,
            developer_correction_source="reviewer",
            max_chars=1000,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "reviewer")
        self.assertIn("handoff block is too large", result.prompt)


class FinalReportAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.dev = Path(self.tmp) / "dev.md"
        self.dev.write_text("# dev", encoding="utf-8")
        self.rev = Path(self.tmp) / "rev.md"
        self.rev.write_text("# rev", encoding="utf-8")

    def test_final_auto_includes_report_paths(self):
        ctx = _worker_ctx(self.tmp, self.dev, rev=self.rev)
        state, _ = on_session_start("g", session_meta={"report_orchestrated": True})
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        state.agy_approved = True
        on_coordinator_output(state, "NEXT: developer\nAnalyze.")
        on_worker_output(state, "developer", _report_ready(str(self.dev)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: reviewer\nReview.")
        on_worker_output(state, "reviewer", _report_ready(str(self.rev)), worker_context=ctx)
        on_coordinator_output(state, "NEXT: safety_gate\nGate.")
        on_worker_output(state, "safety_gate", "PASS\nok")
        final = on_coordinator_output(state, "FINAL:\nAll reports filed.")
        self.assertTrue(final.is_terminal)
        body = final.routing_body or final.prompt_context
        self.assertIn("Claude report:", body)
        self.assertIn(str(self.dev), body)
        self.assertIn("Codex report:", body)
        self.assertIn(str(self.rev), body)
        self.assertIn("AGY report:", body)
        self.assertIn("NONE", body)
        self.assertIn("Twinpet source modified:", body)
        self.assertIn("NO", body)

    def test_final_attachment_includes_final_handoff_not_full_report(self):
        rev = Path(self.tmp) / "rev.md"
        rev.write_text(_reviewer_report_body(final="PASS_WITH_NOTES for safety gate."), encoding="utf-8")
        records = []
        for role, path in (("developer", self.dev), ("reviewer", rev)):
            ingest = ingest_worker_report_output(
                role, _report_ready(str(path)), allowed_roots=self.roots,
            )
            records.append(ingest.record.to_dict())
        attachment = build_report_orchestrated_final_attachment(records)
        self.assertIn(str(self.dev), attachment)
        self.assertIn("FINAL HANDOFF", attachment)
        self.assertIn("PASS_WITH_NOTES for safety gate", attachment)
        self.assertNotIn("# Reviewer", attachment)

    def test_final_attachment_without_channel_history(self):
        records = []
        for role, path in (("developer", self.dev), ("reviewer", self.rev)):
            ingest = ingest_worker_report_output(
                role, _report_ready(str(path)), allowed_roots=self.roots,
            )
            records.append(ingest.record.to_dict())
        attachment = build_report_orchestrated_final_attachment(records)
        self.assertIn(str(self.dev), attachment)
        self.assertIn("AGY report:\nNONE", attachment)
        self.assertIn("from registered report files", attachment)


class ReportWriteBridgeCoordinatorTests(unittest.TestCase):
    def test_coordinator_ingests_bridge_report_ready(self):
        tmp = tempfile.mkdtemp()
        report = Path(tmp) / "dev.md"
        report.write_text("# Dev\nbody", encoding="utf-8")
        from coordinator_loop import on_coordinator_output, on_session_start, on_worker_output

        state, _ = on_session_start("g", session_meta={"report_orchestrated": True})
        on_coordinator_output(state, "CLASSIFY: UI\nui")
        on_coordinator_output(state, "NEXT: developer\nBegin analysis.")
        ctx = {
            "allowed_report_roots": [tmp],
            "report_paths_by_role": {"developer": str(report)},
            "max_report_prompt_chars": 120000,
        }
        action = on_worker_output(
            state,
            "developer",
            (
                "REPORT_READY\n\nStatus:\nPASS\n\n"
                f"Report:\n{report}\n\n"
                "Summary:\nbridge summary\n"
            ),
            worker_context=ctx,
        )
        self.assertEqual(action.target_role, "coordinator")
        self.assertEqual(len(state.report_records), 1)


class ReportHandoffExtractionTests(unittest.TestCase):
    def test_extract_handoff_block(self):
        body = _developer_report_body()
        agy = extract_handoff_block(body, HANDOFF_FOR_AGY_BEGIN, HANDOFF_FOR_AGY_END)
        self.assertIsNotNone(agy)
        assert agy is not None
        self.assertIn("Questions for AGY", agy)

    def test_missing_codex_handoff_routes_developer_on_reviewer_dispatch(self):
        tmp = tempfile.mkdtemp()
        roots = [tmp]
        dev = Path(tmp) / "dev.md"
        dev.write_text(_developer_report_body(codex=""), encoding="utf-8")
        # empty codex handoff between markers still present but insufficient
        dev.write_text(
            f"# Dev\n{_agy_handoff()}\n\n{HANDOFF_FOR_CODEX_REVIEWER_BEGIN}\nshort\n{HANDOFF_FOR_CODEX_REVIEWER_END}",
            encoding="utf-8",
        )
        ingest = ingest_worker_report_output(
            "developer", _report_ready(str(dev)), allowed_roots=roots,
        )
        result = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=[ingest.record.to_dict()],
            project="twinpet",
            phase="review",
            subject="review",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatch_role, "developer")
        self.assertIn(HANDOFF_FOR_CODEX_REVIEWER_BEGIN, result.prompt)


class ReportOrchestrationE2EFlowTests(unittest.TestCase):
    """End-to-end report-flow: dev → AGY → reviewer → correction → re-check → safety → FINAL."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roots = [self.tmp]
        self.policy = _analysis_policy()
        self.memo = "UI-09-C PaymentModal read-only analysis E2E memo."
        self.dev = Path(self.tmp) / "dev.md"
        self.agy = Path(self.tmp) / "agy.md"
        self.rev = Path(self.tmp) / "rev.md"
        self.rev2 = Path(self.tmp) / "rev-pass.md"

    def _write_reports(self):
        self.dev.write_text(_developer_report_body(), encoding="utf-8")
        self.agy.write_text(_agy_report_body(), encoding="utf-8")
        self.rev.write_text(_reviewer_report_body(main="# Reviewer\nREQUEST changes to blueprint"), encoding="utf-8")
        self.rev2.write_text(
            _reviewer_report_body(main="# Reviewer\nPASS on re-check", final="PASS for safety gate."),
            encoding="utf-8",
        )

    def test_full_report_flow_role_path(self):
        self._write_reports()
        ctx = _worker_ctx(self.tmp, self.dev, self.agy, self.rev)
        ctx["report_paths_by_role"]["reviewer"] = str(self.rev)
        policy_path = str((self.policy.get("report_paths") or [""])[0])
        policy_roots = list(self.policy.get("external_report_write_roots") or [])

        initial = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=[],
            project="twinpet-ui-09-c-analysis",
            phase="Developer",
            subject="initial",
            prompt_memo_body=self.memo,
            policy=self.policy,
            expected_output_path=policy_path,
            external_report_write_roots=policy_roots,
        )
        self.assertTrue(initial.ok, initial.blocker)
        self.assertIn("PROMPT MEMO:", initial.prompt)

        state, _ = on_session_start(
            "UI-09-C analysis",
            session_meta={"report_orchestrated": True, "workspace_mode": "read-only"},
        )
        on_coordinator_output(state, "CLASSIFY: UI\nui")

        on_coordinator_output(state, "NEXT: developer\nBegin analysis.")
        dev_action = on_worker_output(
            state, "developer", _report_ready(str(self.dev)), worker_context=ctx,
        )
        self.assertEqual(dev_action.target_role, "coordinator")
        self.assertEqual(len(state.report_records), 1)

        ui_prompt = build_report_orchestrated_dispatch_prompt(
            role="ui_lead",
            report_records=state.report_records,
            project="twinpet",
            phase="UI Lead",
            subject="ux",
            expected_output_path=str(self.agy),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(ui_prompt.ok)
        self.assertIn("DEVELOPER COORDINATOR HANDOFF FOR AGY:", ui_prompt.prompt)
        self.assertIn("Questions for AGY", ui_prompt.prompt)
        self.assertNotIn("REPORT CONTENT:", ui_prompt.prompt)
        self.assertLess(len(ui_prompt.prompt), 20000)

        on_coordinator_output(state, "NEXT: ui_lead\nReview UX.")
        on_worker_output(
            state, "ui_lead", _report_ready(str(self.agy)), worker_context=ctx,
        )
        self.assertEqual(len(state.report_records), 2)

        rev_prompt = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=state.report_records,
            project="twinpet",
            phase="Reviewer",
            subject="review",
            expected_output_path=str(self.rev),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(rev_prompt.ok)
        self.assertIn("DEVELOPER COORDINATOR HANDOFF FOR CODEX REVIEWER:", rev_prompt.prompt)
        self.assertIn("AGY COORDINATOR HANDOFF FOR CODEX REVIEWER:", rev_prompt.prompt)
        self.assertNotIn("REPORT CONTENT:", rev_prompt.prompt)
        self.assertNotIn("# Developer", rev_prompt.prompt)
        self.assertNotIn("# AGY", rev_prompt.prompt)
        self.assertIn("do not inspect repository files", rev_prompt.prompt.lower())
        combined = ui_prompt.prompt + rev_prompt.prompt
        self.assertLess(len(combined), 40000)

        on_coordinator_output(state, "NEXT: reviewer\nReview.")
        on_worker_output(
            state,
            "reviewer",
            _report_ready(str(self.rev), status="REQUEST_CHANGES"),
            worker_context=ctx,
        )
        self.assertTrue(state.awaiting_developer_correction)
        self.assertEqual(state.developer_correction_source, "reviewer")

        corr_prompt = build_report_orchestrated_dispatch_prompt(
            role="developer",
            report_records=state.report_records,
            project="twinpet",
            phase="Correction",
            subject="fix",
            awaiting_developer_correction=True,
            developer_correction_source="reviewer",
            prompt_memo_body=self.memo,
            policy=self.policy,
            expected_output_path=str(self.dev),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(corr_prompt.ok, corr_prompt.blocker)
        self.assertIn("Report correction", corr_prompt.prompt)
        self.assertIn("CODEX REVIEWER CORRECTION HANDOFF", corr_prompt.prompt)
        self.assertNotIn("REPORT CONTENT:", corr_prompt.prompt)
        self.assertNotIn("# Developer", corr_prompt.prompt)

        on_coordinator_output(state, "NEXT: developer\nCorrect.")
        on_worker_output(
            state,
            "developer",
            _report_ready(str(self.dev), status="PASS"),
            worker_context=ctx,
        )
        self.assertTrue(state.developer_correction_complete)

        recheck_prompt = build_report_orchestrated_dispatch_prompt(
            role="reviewer",
            report_records=state.report_records,
            project="twinpet",
            phase="Re-check",
            subject="re-check",
            expected_output_path=str(self.rev2),
            external_report_write_roots=self.roots,
        )
        self.assertTrue(recheck_prompt.ok)

        on_coordinator_output(state, "NEXT: reviewer\nRe-check.")
        ctx["report_paths_by_role"]["reviewer"] = str(self.rev2)
        on_worker_output(
            state,
            "reviewer",
            _report_ready(str(self.rev2), status="PASS"),
            worker_context=ctx,
        )
        self.assertTrue(state.reviewer_passed)

        on_coordinator_output(state, "NEXT: safety_gate\nFinal gate.")
        on_worker_output(state, "safety_gate", "PASS\nboundaries ok")

        final = on_coordinator_output(state, "FINAL:\nUI-09-C analysis complete.")
        self.assertEqual(state.phase, CoordinatorPhase.FINAL)
        body = final.routing_body or ""
        self.assertIn(str(self.dev), body)
        self.assertIn(str(self.agy), body)
        roles = {r.get("role") for r in state.report_records}
        self.assertEqual(roles, {"developer", "ui_lead", "reviewer"})
        paths = {r.get("path") for r in state.report_records}
        self.assertIn(str(self.dev), paths)
        self.assertIn(str(self.agy), paths)


if __name__ == "__main__":
    unittest.main()
