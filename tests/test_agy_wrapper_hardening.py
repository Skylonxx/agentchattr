"""Focused E5B AGY wrapper hardening tests (fail-closed extraction and relay)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible  # noqa: E402
import wrapper  # noqa: E402
from worker_workspace import (  # noqa: E402
    REPORT_FILE_WRITE_BEGIN_MARKER,
    REPORT_FILE_WRITE_END_MARKER,
)
from wrapper import (  # noqa: E402
    _AGY_FAIL_CONV_ID,
    _AGY_FAIL_EMPTY,
    _AGY_FAIL_SENSITIVE,
    _AGY_FAIL_TRANSCRIPT,
    _AGY_FAIL_UNSAFE_OUTPUT,
    _build_agy_store_command,
    _extract_agy_reply,
    _extract_conversation_id_from_log,
    _is_valid_agy_conversation_id,
    _normalize_agy_store_report_output,
    _prepare_agy_relay_text,
    _relay_agy_prepared_reply,
    run_agent_store_exec,
)


def _agy_test_policy(tmp: str, channel: str = "twinpet-ui-09-c-trus") -> tuple[dict, str]:
    agy_root = Path(tmp) / "agy"
    agy_root.mkdir(parents=True, exist_ok=True)
    report_path = str(agy_root / f"{channel}-ux-review.md")
    policy = {
        "mode": "read-only",
        "analysis_report_only": True,
        "trusted_direct_repo_cli": True,
        "write_files": [],
        "external_report_write_roots": [tmp, str(agy_root)],
        "report_paths": [report_path],
    }
    return policy, report_path


def _agy_bridge_block(path: str, body: str, *, status: str = "REQUEST_CHANGES") -> str:
    return (
        f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
        f"Path: {path}\n"
        f"Status: {status}\n"
        "Summary: UX review complete.\n"
        "Next recommended role: developer\n"
        "---\n"
        f"{body}\n"
        f"{REPORT_FILE_WRITE_END_MARKER}"
    )


def _agy_queue_item(policy: dict, channel: str = "twinpet-ui-09-c-trus") -> dict:
    agy_root = next(
        r for r in policy["external_report_write_roots"]
        if str(r).replace("\\", "/").lower().endswith("/agy")
    )
    report_path = str(Path(agy_root) / f"{channel}-ux-review.md")
    return {
        "prompt": "review",
        "channel": channel,
        "workspace_policy_context": {
            "session_id": 93,
            "session_role": "ui_lead",
            "channel": channel,
            "report_paths_by_role": {"ui_lead": report_path},
            "allowed_report_roots": policy["external_report_write_roots"],
            "workspace_policy": policy,
        },
    }


def _write_transcript(base: Path, conv_id: str, model_contents: list[str]) -> None:
    tdir = base / "brain" / conv_id / ".system_generated" / "logs"
    tdir.mkdir(parents=True)
    lines = [json.dumps({"source": "MODEL", "content": c}) for c in model_contents]
    (tdir / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


class AgyBoundaryPreservationTests(unittest.TestCase):
    def test_agy_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_store_command_rejects_dangerous_flags(self):
        for args in (["--yolo"], ["--spawn-subagent"], ["--allowedTools", "Target:*"]):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit):
                    _build_agy_store_command("agy", "p", 60, store_args=args)


class AgyConversationIdValidationTests(unittest.TestCase):
    def test_valid_uuid_accepted(self):
        self.assertTrue(
            _is_valid_agy_conversation_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        )

    def test_invalid_conversation_id_rejected_for_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(Path(tmp), "not-a-uuid", ["PASS\nok"])
            self.assertEqual(_extract_agy_reply("not-a-uuid", agy_data_dir=tmp), "")

    def test_log_extraction_rejects_malformed_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "cli-2026.log").write_text(
                "Print mode: conversation=not-a-valid-uuid-here-xxxxxxxxxx, sending\n",
                encoding="utf-8",
            )
            self.assertEqual(_extract_conversation_id_from_log(str(log_dir)), "")


class AgyTranscriptExtractionFailClosedTests(unittest.TestCase):
    def test_missing_transcript_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), "")

    def test_malformed_jsonl_skipped_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            tdir = Path(tmp) / "brain" / conv / ".system_generated" / "logs"
            tdir.mkdir(parents=True)
            (tdir / "transcript.jsonl").write_text("{bad json\nnot verdict\n", encoding="utf-8")
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), "")

    def test_safe_non_verdict_plain_text_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
            _write_transcript(Path(tmp), conv, ["AGY_GENERAL_OK"])
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), "AGY_GENERAL_OK")

    def test_unsafe_non_verdict_plain_text_not_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
            listing = (
                "Created At: 2026\n"
                '{"name": "codex-cwd", "isDir": true, "sizeBytes": 0}\n'
            )
            _write_transcript(Path(tmp), conv, [listing])
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), "")

    def test_verdict_reply_still_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee"
            verdict = "PASS WITH NOTES\nLayout OK."
            _write_transcript(Path(tmp), conv, [verdict])
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), verdict)

    def test_bridge_reply_preferred_over_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            conv = "eeeeeeee-bbbb-cccc-dddd-eeeeeeeeeeee"
            bridge = _agy_bridge_block(
                "C:/tmp/agy/report.md",
                "# UX Review\nFocus and keyboard gaps.",
            )
            _write_transcript(
                Path(tmp),
                conv,
                ["PASS WITH NOTES\nLooks fine.", bridge],
            )
            self.assertEqual(_extract_agy_reply(conv, agy_data_dir=tmp), bridge)


class AgyNormalizeBeforeRelayTests(unittest.TestCase):
    def test_long_bridge_normalizes_before_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, report_path = _agy_test_policy(tmp)
            body = "UX Review Report: Twinpet UI-09-C Payment Modal\n" + ("detail " * 300)
            raw = _agy_bridge_block(report_path, body)
            self.assertGreater(len(raw), 2000)
            item = _agy_queue_item(policy)
            normalized = _normalize_agy_store_report_output(raw, item, data_dir=None)
            self.assertIsNotNone(normalized)
            assert normalized is not None
            self.assertTrue(normalized.startswith("REPORT_READY"))
            self.assertTrue(Path(report_path).is_file())
            relay_text, marker = _prepare_agy_relay_text(normalized)
            self.assertIsNone(marker)
            self.assertNotIn("[truncated", relay_text or "")

    def test_session_93_class_end_marker_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, report_path = _agy_test_policy(tmp)
            header = (
                f"{REPORT_FILE_WRITE_BEGIN_MARKER}\n"
                f"Path: {report_path}\n"
                "Status: REQUEST_CHANGES\n"
                "Summary: UX review complete; changes requested.\n"
                "Next recommended role: developer\n"
                "---\n"
                "UX Review Report: Twinpet UI-09-C Payment Modal (Trusted CLI)\n"
            )
            footer = f"\n{REPORT_FILE_WRITE_END_MARKER}"
            pad_len = 2033 - len(header) - len(footer)
            self.assertGreater(pad_len, 0)
            raw = header + ("x" * pad_len) + footer
            self.assertEqual(len(raw), 2033)
            item = _agy_queue_item(policy)
            normalized = _normalize_agy_store_report_output(raw, item, data_dir=None)
            self.assertIsNotNone(normalized)
            assert normalized is not None
            self.assertTrue(normalized.startswith("REPORT_READY"))
            self.assertTrue(Path(report_path).is_file())


class AgyStoreExecRelayTests(unittest.TestCase):
    def _run_one_turn(self, *, stdout=b"", returncode=0, conv_id="", extract_reply="",
                      workspace_policy_context=None):
        relay_calls = []

        def fake_relay(port, token, message, channel="general"):
            relay_calls.append({"message": message, "channel": channel})

        def fake_watcher(enqueue_fn):
            if workspace_policy_context is not None:
                enqueue_fn(
                    "review prompt",
                    channel="design-review",
                    workspace_policy_context=workspace_policy_context,
                )
            else:
                enqueue_fn("review prompt", channel="design-review")

        proc = MagicMock(returncode=returncode, stdout=stdout, stderr=b"")

        with patch("subprocess.run", return_value=proc), \
             patch("wrapper._extract_conversation_id_from_log", return_value=conv_id), \
             patch("wrapper._extract_agy_reply", return_value=extract_reply), \
             patch("wrapper._relay_to_chat", side_effect=fake_relay):
            run_agent_store_exec(
                "agy", str(ROOT), {}, "agy", fake_watcher,
                no_restart=True, get_token_fn=lambda: "tok",
            )
        return relay_calls

    def test_long_bridge_relay_emits_report_ready_not_truncated_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, report_path = _agy_test_policy(tmp)
            raw = _agy_bridge_block(report_path, "UX body\n" + ("y" * 2200))
            wpc = _agy_queue_item(policy)["workspace_policy_context"]
            calls = self._run_one_turn(
                conv_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                extract_reply=raw,
                workspace_policy_context=wpc,
            )
            self.assertTrue(calls)
            self.assertTrue(calls[0]["message"].startswith("REPORT_READY"))
            self.assertNotIn("REPORT_FILE_WRITE_BEGIN", calls[0]["message"])
            self.assertNotIn("[truncated", calls[0]["message"])
            self.assertTrue(Path(report_path).is_file())

    def test_missing_conversation_id_emits_fail_marker(self):
        calls = self._run_one_turn()
        self.assertTrue(calls)
        self.assertEqual(calls[0]["message"], _AGY_FAIL_CONV_ID)
        self.assertEqual(calls[0]["channel"], "design-review")

    def test_empty_transcript_emits_fail_marker(self):
        calls = self._run_one_turn(
            conv_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            extract_reply="",
        )
        self.assertTrue(calls)
        self.assertEqual(calls[0]["message"], _AGY_FAIL_TRANSCRIPT)
        self.assertEqual(calls[0]["channel"], "design-review")

    def test_unsafe_stdout_not_relayed_raw(self):
        listing = (
            b"Created At: x\n"
            b'{"name": "codex-cwd", "isDir": true, "sizeBytes": 0}\n'
            b"Summary: This directory contains server.log"
        )
        calls = self._run_one_turn(stdout=listing)
        self.assertEqual(calls[0]["message"], _AGY_FAIL_UNSAFE_OUTPUT)
        self.assertNotIn("codex-cwd", calls[0]["message"])

    def test_safe_stdout_relayed_to_explicit_channel_not_general(self):
        relay_calls = []

        def fake_relay(port, token, message, channel="general"):
            relay_calls.append({"message": message, "channel": channel})

        def fake_watcher(enqueue_fn):
            enqueue_fn("p", channel="ux-review")

        proc = MagicMock(returncode=0, stdout=b"PASS WITH NOTES\nOK", stderr=b"")

        with patch("subprocess.run", return_value=proc), \
             patch("wrapper._relay_to_chat", side_effect=fake_relay):
            run_agent_store_exec(
                "agy", str(ROOT), {}, "agy", fake_watcher,
                no_restart=True, get_token_fn=lambda: "tok",
            )

        self.assertEqual(relay_calls[0]["channel"], "ux-review")
        self.assertIn("PASS WITH NOTES", relay_calls[0]["message"])


class AgyPrepareRelayTests(unittest.TestCase):
    def test_empty_output_fail_closed(self):
        text, marker = _prepare_agy_relay_text("")
        self.assertIsNone(text)
        self.assertEqual(marker, _AGY_FAIL_EMPTY)

    def test_directory_listing_rejected(self):
        listing = (
            "Created At: 2026\n"
            '{"name": "codex-cwd", "isDir": true, "sizeBytes": 0}\n'
            "Summary: This directory contains server.log"
        )
        text, marker = _prepare_agy_relay_text(listing)
        self.assertIsNone(text)
        self.assertEqual(marker, _AGY_FAIL_UNSAFE_OUTPUT)

    def test_codexsafe_block_verdict_format_rejected(self):
        text, marker = _prepare_agy_relay_text("BLOCK: forbidden git operation")
        self.assertIsNone(text)
        self.assertEqual(marker, _AGY_FAIL_UNSAFE_OUTPUT)

    def test_agy_ux_pass_verdict_allowed(self):
        text, marker = _prepare_agy_relay_text("PASS WITH NOTES\nLooks good.")
        self.assertIsNone(marker)
        self.assertIn("PASS WITH NOTES", text or "")

    def test_report_ready_not_truncated(self):
        payload = (
            "REPORT_READY\n\n"
            "Status:\nREQUEST_CHANGES\n\n"
            "Report:\nC:/Users/Narachat/OneDrive/Ai-Report/agy/twinpet-ui-09-c-trus-ux-review.md\n\n"
            "Summary:\n" + ("x" * 2500) + "\n\n"
            "Next recommended role:\ncoordinator\n"
        )
        text, marker = _prepare_agy_relay_text(payload)
        self.assertIsNone(marker)
        self.assertNotIn("[truncated", text or "")
        self.assertIn("REQUEST_CHANGES", text or "")

    def test_report_write_failed_not_truncated(self):
        payload = (
            "REPORT_WRITE_FAILED\n\n"
            "Reason:\n" + ("incomplete " * 400) + "\n\n"
            "Expected report:\nC:/tmp/report.md\n\n"
            "Status:\nFAIL\n"
        )
        text, marker = _prepare_agy_relay_text(payload)
        self.assertIsNone(marker)
        self.assertNotIn("[truncated", text or "")

    def test_token_like_strings_redacted(self):
        raw = "PASS\nBearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        text, marker = _prepare_agy_relay_text(raw)
        self.assertIsNone(marker)
        self.assertIn("[REDACTED]", text or "")
        self.assertNotIn("sk-ant-api03", text or "")

    def test_unredactable_secret_fails_closed(self):
        raw = "PASS\nsecret=sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        with patch.object(wrapper, "_prepare_agy_relay_text", wraps=_prepare_agy_relay_text):
            text, marker = _prepare_agy_relay_text(raw)
        if marker == _AGY_FAIL_SENSITIVE:
            self.assertIsNone(text)
        else:
            self.assertNotIn("sk-ant-api03", text or "")


class AgyRelayHelperTests(unittest.TestCase):
    def test_relay_helper_applies_redaction(self):
        captured = {}

        def fake_relay(port, token, message, channel="general"):
            captured["message"] = message

        with patch("wrapper._relay_to_chat", side_effect=fake_relay):
            ok = _relay_agy_prepared_reply(
                8300, lambda: "tok", "PASS\nx-api-key: abcdefghijklmnopqrstuvwxyz123456",
                "design-review",
            )
        self.assertTrue(ok)
        self.assertIn("[REDACTED]", captured["message"])


if __name__ == "__main__":
    unittest.main()
