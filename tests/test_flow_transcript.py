"""Tests for V2-C sandbox flow transcript/closure export."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import flow_transcript as ft  # noqa: E402
import package_relay as pr  # noqa: E402


class _ApprovedRootsMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.onedrive_root = self.root / "OneDrive" / "Ai-Report"
        self.desktop_root = self.root / "Desktop" / "Ai-Report"
        self.onedrive_root.mkdir(parents=True)
        self.desktop_root.mkdir(parents=True)
        self.output_root = self.onedrive_root / "claude"
        self.output_root.mkdir(parents=True)

        self._orig_roots = pr.APPROVED_ROOTS
        pr.APPROVED_ROOTS = (self.onedrive_root, self.desktop_root)

    def tearDown(self):
        pr.APPROVED_ROOTS = self._orig_roots


class ReportPathParsingTests(_ApprovedRootsMixin, unittest.TestCase):
    def test_parse_report_path_from_line_2(self):
        text = (
            "READY_FOR_AGY_REVIEW\n"
            f"REPORT_PATH: {self.onedrive_root / 'claude' / 'report.md'}\n"
            "optional body"
        )
        parsed = ft.parse_report_path(text)
        self.assertIsNotNone(parsed)
        self.assertTrue(str(parsed).endswith("report.md"))

    def test_missing_report_path_returns_none(self):
        self.assertIsNone(ft.parse_report_path("READY_FOR_AGY_REVIEW\nno path here"))
        self.assertIsNone(ft.parse_report_path(""))
        self.assertIsNone(ft.parse_report_path(None))

    def test_report_path_case_insensitive(self):
        text = f"READY\nreport_path: {self.onedrive_root / 'x.md'}"
        self.assertIsNotNone(ft.parse_report_path(text))


class ReportPathValidationTests(_ApprovedRootsMixin, unittest.TestCase):
    def test_onedrive_path_accepted(self):
        path = self.onedrive_root / "claude" / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, resolved = ft.validate_approved_report_path(str(path))
        self.assertTrue(ok, resolved)
        self.assertTrue(str(resolved).endswith("report.md"))

    def test_desktop_path_accepted(self):
        path = self.desktop_root / "claude" / "report.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, resolved = ft.validate_approved_report_path(str(path))
        self.assertTrue(ok, resolved)

    def test_outside_approved_roots_rejected(self):
        outside = self.root / "outside" / "x.md"
        outside.parent.mkdir(parents=True)
        ok, reason = ft.validate_approved_report_path(str(outside))
        self.assertFalse(ok)
        self.assertIn("approved", reason.lower())

    def test_path_traversal_rejected(self):
        bad = str(self.onedrive_root / "claude" / ".." / ".." / "secret.md")
        ok, reason = ft.validate_approved_report_path(bad)
        self.assertFalse(ok)

    def test_relative_path_rejected(self):
        ok, reason = ft.validate_approved_report_path("claude/report.md")
        self.assertFalse(ok)
        self.assertIn("absolute", reason.lower())


class OutputRootValidationTests(_ApprovedRootsMixin, unittest.TestCase):
    def test_output_root_under_approved(self):
        ok, resolved = ft.validate_output_root(str(self.output_root))
        self.assertTrue(ok, resolved)

    def test_output_root_outside_rejected(self):
        outside = self.root / "exports"
        outside.mkdir()
        ok, reason = ft.validate_output_root(str(outside))
        self.assertFalse(ok)


class RedactionTests(_ApprovedRootsMixin, unittest.TestCase):
    def test_redact_secrets_and_outside_paths(self):
        outside = str(self.root / "secrets" / "key.txt")
        text = f"token=sk-abc123xyz789012345678901234567890 PAT ghp_abcdefghij1234567890 path {outside}"
        redacted = ft.redact_export_text(text)
        self.assertNotIn("sk-abc123", redacted)
        self.assertNotIn("ghp_abcd", redacted)
        self.assertIn("[REDACTED_PATH]", redacted)

    def test_redact_forward_slash_drive_path(self):
        text = "C:/Users/Name/Secret/key.txt"
        redacted = ft.redact_export_text(text)
        self.assertEqual(redacted, "[REDACTED_PATH]")
        self.assertNotIn("key.txt", redacted)

    def test_redact_unc_path(self):
        text = r"\\server\share\secret.txt"
        redacted = ft.redact_export_text(text)
        self.assertEqual(redacted, "[REDACTED_PATH]")
        self.assertNotIn("secret.txt", redacted)

    def test_redact_path_with_spaces_no_suffix_leak(self):
        text = r"C:\Users\Name\Secret Folder\key.txt"
        redacted = ft.redact_export_text(text)
        self.assertEqual(redacted, "[REDACTED_PATH]")
        self.assertNotIn("Folder", redacted)
        self.assertNotIn("key.txt", redacted)

    def test_preserve_onedrive_approved_path(self):
        approved = str(self.onedrive_root / "claude" / "report.md")
        redacted = ft.redact_export_text(f"report at {approved}")
        self.assertIn(approved, redacted)
        self.assertNotIn("[REDACTED_PATH]", redacted)

    def test_preserve_desktop_approved_path(self):
        approved = str(self.desktop_root / "claude" / "report.txt")
        redacted = ft.redact_export_text(f"report at {approved}")
        self.assertIn(approved, redacted)
        self.assertNotIn("[REDACTED_PATH]", redacted)


class TranscriptExportTests(_ApprovedRootsMixin, unittest.TestCase):
    def _session(self) -> dict:
        return {
            "id": 7,
            "channel": "sandbox-flow-test-7",
            "template_id": "sandbox-bakery-flow",
            "goal": "Test bakery flow",
            "cast": {"developer": "dev"},
            "started_at": 1710000000.0,
            "updated_at": 1710000100.0,
            "state": "complete",
            "waiting_on": None,
        }

    def _flow_state_pass(self) -> dict:
        return {
            "phase": "closure",
            "ux_loops": 1,
            "eng_loops": 0,
            "total_steps": 3,
            "verdicts": [{"role": "developer", "token": "READY_FOR_AGY_REVIEW", "time": 1.0}],
            "report_path": str(self.onedrive_root / "claude" / "report.md"),
            "closure_summary": {"final_notes": "done"},
        }

    def test_transcript_export_writes_markdown(self):
        path, err = ft.export_sandbox_flow_transcript(
            session=self._session(),
            template={"name": "Sandbox Bakery Flow"},
            flow_state=self._flow_state_pass(),
            messages=[{"sender": "system", "type": "chat", "text": "hello", "channel": "sandbox-flow-test-7"}],
            output_root=str(self.output_root),
            data_dir=str(self.root / "data"),
        )
        self.assertEqual(err, "")
        self.assertIsNotNone(path)
        p = Path(path)
        self.assertTrue(p.is_file())
        body = p.read_text(encoding="utf-8")
        self.assertIn("# Sandbox Flow Transcript", body)
        self.assertIn("session_id: 7", body)
        self.assertIn("sandbox-flow-7-transcript.md", str(p))


class ClosureExportTests(_ApprovedRootsMixin, unittest.TestCase):
    def _session(self) -> dict:
        return {
            "id": 9,
            "channel": "sandbox-flow-test-9",
            "goal": "Closure test",
            "cast": {"developer": "dev"},
        }

    def test_closure_export_pass(self):
        fs = {
            "phase": "closure",
            "ux_loops": 0,
            "eng_loops": 0,
            "total_steps": 2,
            "verdicts": [{"role": "codex", "token": "PASS"}],
            "report_path": str(self.onedrive_root / "claude" / "r.md"),
        }
        t_path = str(self.output_root / "sandbox-flow-9-transcript.md")
        Path(t_path).write_text("# t", encoding="utf-8")
        c_path, err = ft.export_sandbox_flow_closure(
            session=self._session(),
            flow_state=fs,
            output_root=str(self.output_root),
            transcript_path=t_path,
        )
        self.assertEqual(err, "")
        self.assertIsNotNone(c_path)
        body = Path(c_path).read_text(encoding="utf-8")
        self.assertIn("Final Status: PASS", body)
        self.assertIn("transcript_path:", body)

    def test_closure_export_halted(self):
        fs = {
            "phase": "halted",
            "halt_reason": "invalid report_path: path outside approved Ai-Report roots",
            "ux_loops": 0,
            "eng_loops": 0,
            "total_steps": 1,
            "verdicts": [],
        }
        t_path = str(self.output_root / "sandbox-flow-9-transcript.md")
        Path(t_path).write_text("# t", encoding="utf-8")
        c_path, err = ft.export_sandbox_flow_closure(
            session=self._session(),
            flow_state=fs,
            output_root=str(self.output_root),
            transcript_path=t_path,
            last_output_snippet="READY_FOR_AGY_REVIEW\nbad path",
        )
        self.assertEqual(err, "")
        body = Path(c_path).read_text(encoding="utf-8")
        self.assertIn("Final Status: HALTED", body)
        self.assertIn("Halt Reason", body)

    def test_closure_export_blocked(self):
        fs = {
            "phase": "halted",
            "halt_reason": "blocked: invalid report_path: path outside approved Ai-Report roots",
            "ux_loops": 0,
            "eng_loops": 0,
            "total_steps": 1,
            "verdicts": [{"role": "developer", "token": "BLOCKED"}],
        }
        t_path = str(self.output_root / "sandbox-flow-9-transcript.md")
        Path(t_path).write_text("# t", encoding="utf-8")
        c_path, err = ft.export_sandbox_flow_closure(
            session=self._session(),
            flow_state=fs,
            output_root=str(self.output_root),
            transcript_path=t_path,
        )
        self.assertEqual(err, "")
        body = Path(c_path).read_text(encoding="utf-8")
        self.assertIn("Final Status: BLOCKED", body)

    def test_closure_export_invalid_report_path_blocked(self):
        fs = {
            "phase": "halted",
            "halt_reason": "blocked: invalid report_path: path outside approved Ai-Report roots",
            "ux_loops": 0,
            "eng_loops": 0,
            "total_steps": 1,
            "verdicts": [{
                "role": "developer",
                "token": "BLOCKED",
                "notes": "invalid report_path: path outside approved Ai-Report roots",
            }],
        }
        t_path = str(self.output_root / "sandbox-flow-9-transcript.md")
        Path(t_path).write_text("# t", encoding="utf-8")
        c_path, err = ft.export_sandbox_flow_closure(
            session=self._session(),
            flow_state=fs,
            output_root=str(self.output_root),
            transcript_path=t_path,
            last_output_snippet="READY_FOR_AGY_REVIEW\nREPORT_PATH: C:\\outside\\bad.md",
        )
        self.assertEqual(err, "")
        body = Path(c_path).read_text(encoding="utf-8")
        self.assertIn("Final Status: BLOCKED", body)
        self.assertIn("invalid report_path", body)


class ArtifactExportTests(_ApprovedRootsMixin, unittest.TestCase):
    def test_export_artifacts_paths_under_output_root(self):
        session = {
            "id": 11,
            "channel": "sandbox-flow-11",
            "goal": "artifact test",
            "cast": {},
        }
        fs = {"phase": "closure", "verdicts": [], "ux_loops": 0, "eng_loops": 0, "total_steps": 1}
        result = ft.export_sandbox_flow_artifacts(
            session=session,
            template={"name": "T"},
            flow_state=fs,
            messages=[],
            output_root=str(self.output_root),
        )
        self.assertTrue(result.ok, result.error)
        self.assertIsNotNone(result.transcript_path)
        self.assertIsNotNone(result.closure_path)
        for p in (result.transcript_path, result.closure_path):
            resolved = Path(p).resolve()
            out_resolved = self.output_root.resolve()
            self.assertTrue(
                str(resolved).lower().startswith(str(out_resolved).lower()),
                f"{resolved} not under {out_resolved}",
            )

    def test_format_system_message_pass(self):
        msg = ft.format_flow_export_system_message(
            session={"id": 7, "channel": "sandbox-flow-v2-d-250624-1530-7"},
            flow_state={"phase": "closure"},
            transcript_path=str(self.output_root / "sandbox-flow-7-transcript.md"),
            closure_path=str(self.output_root / "sandbox-flow-7-closure.md"),
        )
        self.assertIn("SANDBOX FLOW CLOSURE — PASS", msg)
        self.assertIn("Transcript:", msg)
        self.assertIn("Closure:", msg)


class InvalidReportPathBlockedTests(_ApprovedRootsMixin, unittest.TestCase):
    """Invalid REPORT_PATH must produce BLOCKED status and developer verdict history."""

    SANDBOX_TEMPLATE = {
        "id": "sandbox-bakery-flow",
        "name": "Sandbox Bakery Flow",
        "flow_coordinator": True,
        "sandbox_only": True,
        "roles": ["developer", "ui_lead", "codex_reviewer"],
        "phases": [
            {"name": "Dev", "participants": ["developer"],
             "prompt": "x", "turn_order": "sequential"},
            {"name": "AGY", "participants": ["ui_lead"],
             "prompt": "x", "turn_order": "sequential"},
            {"name": "Codex", "participants": ["codex_reviewer"],
             "prompt": "x", "turn_order": "sequential"},
        ],
    }

    def _make_engine(self):
        from session_engine import SessionEngine
        from session_store import SessionStore
        from tests.test_session_relay import (
            _FakeAgentTrigger, _FakeMessageStore, _FakeRegistry,
        )

        store = SessionStore(str(self.root / "sessions.json"))
        store._templates[self.SANDBOX_TEMPLATE["id"]] = self.SANDBOX_TEMPLATE
        messages = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        registry = _FakeRegistry({
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex": {"name": "codex", "base": "codex"},
        })
        sb_cfg = {
            "flow_start_output_root": str(self.output_root),
        }
        engine = SessionEngine(
            store, messages, trigger, registry=registry,
            sandbox_config=sb_cfg,
            data_dir=str(self.root / "data"),
        )
        return engine, store, messages

    def test_invalid_report_path_blocked_status_and_verdict(self):
        engine, store, messages = self._make_engine()
        cast = {
            "developer": "claude",
            "ui_lead": "agy",
            "codex_reviewer": "codex",
        }
        session = engine.start_session(
            "sandbox-bakery-flow", "sandbox-flow-test", cast, "user",
            goal="Test invalid report path",
        )
        sid = session["id"]
        bad_path = str(self.root / "outside" / "bad.md")
        output = f"READY_FOR_AGY_REVIEW\nREPORT_PATH: {bad_path}\nbody"
        active = store.get_active("sandbox-flow-test") or store.get(sid)
        active["_last_msg"] = {
            "text": output, "id": 100, "sender": "claude",
            "type": "chat", "channel": "sandbox-flow-test",
        }
        engine._advance(active, 100)
        persisted = store.get(sid)
        fs = persisted["flow_state"]
        self.assertEqual(persisted["state"], "interrupted")
        self.assertIn("blocked", fs.get("halt_reason", "").lower())
        self.assertIn("invalid report_path", fs.get("halt_reason", ""))
        dev_verdicts = [v for v in fs.get("verdicts", []) if v.get("role") == "developer"]
        self.assertTrue(any(v.get("token") == "BLOCKED" for v in dev_verdicts))
        self.assertEqual(ft._final_status(fs), "BLOCKED")
        export_msgs = [m for m in messages.added if m.get("type") == "session_flow_export"]
        self.assertTrue(export_msgs)
        self.assertIn("BLOCKED", export_msgs[-1]["text"])


class SessionEngineExportFailureTests(unittest.TestCase):
    def test_pass_closure_export_failure_does_not_complete(self):
        from flow_coordinator import Action, FlowState, Phase
        from flow_transcript import FlowExportResult
        from session_engine import SessionEngine
        from unittest.mock import MagicMock

        store = MagicMock()
        messages = MagicMock()
        trigger = MagicMock()
        engine = SessionEngine(store, messages, trigger)
        fs = FlowState()
        fs.phase = Phase.CLOSURE
        session = {"id": 42, "channel": "sandbox-flow-42", "goal": "g"}
        action = Action(target_role="closure", prompt_context="done", is_terminal=True)

        engine._export_flow_artifacts = MagicMock(return_value=FlowExportResult(
            None, None, False, "output root outside approved Ai-Report roots",
        ))

        engine._handle_flow_terminal(session, fs, 999, action)

        store.complete.assert_not_called()
        error_calls = [
            c for c in messages.add.call_args_list
            if c.kwargs.get("msg_type") == "session_flow_export_error"
        ]
        self.assertEqual(len(error_calls), 1)
        self.assertIn("SANDBOX FLOW EXPORT FAILED", error_calls[0].kwargs["text"])
        success_calls = [
            c for c in messages.add.call_args_list
            if c.kwargs.get("msg_type") == "session_flow_export"
        ]
        self.assertEqual(len(success_calls), 0)
        store.interrupt.assert_called_once()
        self.assertIn("flow export failed", store.interrupt.call_args[0][1])

    def test_success_export_emits_artifact_paths(self):
        from flow_coordinator import Action, FlowState, Phase
        from flow_transcript import FlowExportResult
        from session_engine import SessionEngine
        from unittest.mock import MagicMock

        store = MagicMock()
        messages = MagicMock()
        trigger = MagicMock()
        engine = SessionEngine(store, messages, trigger)
        fs = FlowState()
        fs.phase = Phase.CLOSURE
        session = {"id": 43, "channel": "sandbox-flow-43", "goal": "g"}
        action = Action(target_role="closure", prompt_context="done", is_terminal=True)
        t_path = r"C:\Users\Narachat\OneDrive\Ai-Report\claude\sandbox-flow-43-transcript.md"
        c_path = r"C:\Users\Narachat\OneDrive\Ai-Report\claude\sandbox-flow-43-closure.md"

        engine._export_flow_artifacts = MagicMock(return_value=FlowExportResult(
            t_path, c_path, True,
        ))

        engine._handle_flow_terminal(session, fs, 999, action)

        store.complete.assert_called_once_with(43, 999)
        export_calls = [
            c for c in messages.add.call_args_list
            if c.kwargs.get("msg_type") == "session_flow_export"
        ]
        self.assertEqual(len(export_calls), 1)
        self.assertIn("Transcript:", export_calls[0].kwargs["text"])
        self.assertIn("Closure:", export_calls[0].kwargs["text"])
        error_calls = [
            c for c in messages.add.call_args_list
            if c.kwargs.get("msg_type") == "session_flow_export_error"
        ]
        self.assertEqual(len(error_calls), 0)


if __name__ == "__main__":
    unittest.main()
