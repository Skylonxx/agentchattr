"""Focused AGY operational-readiness tests.

These tests keep AGY useful for future UI/UX review while ensuring this phase
does not enable production AGY relay, broad MCP, Slack MCP, Target:* access,
or subagent loops. Claude is authorized relay-eligible via claude_relay only.
"""

import json
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible  # noqa: E402
import wrapper  # noqa: E402
from wrapper import (  # noqa: E402
    _build_agy_store_command,
    _build_direct_mention_prompt,
    _extract_agy_reply,
    _is_agy_directory_listing_output,
    _agy_first_verdict_token,
    _queue_watcher,
    _resolve_mcp_inject,
)


def _load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


class AgyConfigReadinessTests(unittest.TestCase):
    def setUp(self):
        self.config = _load_config()
        self.agy = self.config["agents"]["agy"]

    def test_agy_stays_store_exec_and_not_relay_mode(self):
        self.assertEqual(self.agy["command"], "agy")
        self.assertEqual(self.agy["run_mode"], "store_exec")
        self.assertNotEqual(self.agy.get("run_mode"), "claude_relay")

    def test_agy_reviewer_prompt_is_ui_ux_bounded(self):
        prompt = self.agy["exec_prompt_suffix"]
        required = [
            "UI/UX",
            "responsive layout",
            "accessibility",
            "Do not edit files",
            "run shell commands",
            "Slack MCP",
            "Target:*",
            "spawn subagents",
            "Do not coordinate workflow",
            "safety gate",
        ]
        for text in required:
            self.assertIn(text, prompt)

    def test_agy_suffix_no_broad_mcp_prohibition(self):
        prompt = self.agy["exec_prompt_suffix"]
        self.assertNotIn("call MCP tools", prompt)
        self.assertNotIn("Do not use MCP tools", prompt)
        self.assertNotIn("calling MCP", prompt)

    def test_agy_has_no_mcp_injection_defaults(self):
        self.assertNotIn("mcp_inject", self.agy)
        self.assertEqual(_resolve_mcp_inject("agy", self.agy), {})

    def test_agy_remains_relay_ineligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_claude_authorized_relay_eligible(self):
        self.assertTrue(is_relay_eligible("claude"))
        self.assertIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_claude_dryrun_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("claude_dryrun"))
        self.assertNotIn("claude_dryrun", RELAY_ELIGIBLE_AGENTS)


class AgyPromptReadinessTests(unittest.TestCase):
    def test_direct_prompt_includes_base_and_agy_constraints(self):
        suffix = _load_config()["agents"]["agy"]["exec_prompt_suffix"]
        prompt = _build_direct_mention_prompt(
            "design-review",
            "Please inspect the POS customer list on mobile.",
            exec_prompt_suffix=suffix,
        )
        self.assertIn("You received a mention in agentchattr #design-review", prompt)
        self.assertIn("Please inspect the POS customer list on mobile.", prompt)
        self.assertIn("Output ONLY your reply text", prompt)
        self.assertIn("Do not edit files", prompt)
        self.assertIn("Do not run shell commands", prompt)
        self.assertIn("AGY reviewer mode", prompt)
        self.assertIn("Do not coordinate workflow", prompt)
        self.assertIn("Return concise findings", prompt)

    def test_direct_prompt_without_suffix_preserves_existing_base_contract(self):
        prompt = _build_direct_mention_prompt("general", "hello")
        self.assertIn("Message:\n---\nhello\n---", prompt)
        self.assertIn("Do not edit files", prompt)
        self.assertIn("Do not run shell commands", prompt)
        self.assertNotIn("AGY reviewer mode", prompt)


class AgyStoreCommandTests(unittest.TestCase):
    def test_store_command_includes_safe_launcher_model_args(self):
        cmd = _build_agy_store_command(
            "agy",
            "review prompt",
            120,
            store_args=["--model", "Gemini 3.1 Pro (High)"],
        )
        self.assertEqual(
            cmd,
            [
                "agy",
                "--model",
                "Gemini 3.1 Pro (High)",
                "--print",
                "review prompt",
                "--print-timeout",
                "120s",
            ],
        )

    def test_store_command_rejects_non_allowlisted_args(self):
        bad_args = [
            ["--allowedTools", "Target:*"],
            ["--mcp-config", "x"],
            ["--tool", "Slack"],
            ["--spawn-subagent"],
            ["--unsafe"],
            ["--yolo"],
            ["--workspace", "write"],
            ["--edit"],
            ["--approval", "never"],
        ]
        for args in bad_args:
            with self.subTest(args=args):
                with self.assertRaises(SystemExit):
                    _build_agy_store_command("agy", "prompt", 120, store_args=args)

    def test_store_command_rejects_model_flag_without_value(self):
        with self.assertRaises(SystemExit):
            _build_agy_store_command("agy", "prompt", 120, store_args=["--model"])


class StoreExecInjectRegressionTests(unittest.TestCase):
    """Regression: _queue_watcher passes channel=; store_exec enqueue must accept it."""

    def test_store_exec_enqueue_contract_accepts_channel_kwarg(self):
        """Mirror run_agent_store_exec._enqueue — must accept channel= from watcher."""
        import queue as _queue

        work: _queue.Queue[dict] = _queue.Queue()
        running_flag = [False]

        def _enqueue(text, channel="", **kwargs):
            if running_flag is not None:
                running_flag[0] = True
            work.put({"prompt": text, "channel": channel or "general"})

        _enqueue("flattened prompt", channel="design-review")
        item = work.get_nowait()
        self.assertTrue(running_flag[0])
        self.assertEqual(item["prompt"], "flattened prompt")
        self.assertEqual(item["channel"], "design-review")

    def test_queue_watcher_inject_fn_receives_channel_kwarg(self):
        received = []

        def inject_fn(text, channel="", **kwargs):
            received.append({"text": text, "channel": channel})

        with tempfile.TemporaryDirectory() as tmp:
            qf = Path(tmp) / "agy_queue.jsonl"
            qf.write_text(
                json.dumps(
                    {
                        "sender": "user",
                        "text": "user: @agy Reply exactly: PING_OK",
                        "time": "12:00:00",
                        "channel": "design-review",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            identity = lambda: ("agy", qf)

            with patch.object(wrapper, "_fetch_role", return_value=""), \
                 patch.object(wrapper, "_fetch_active_rules", return_value=None), \
                 patch.object(wrapper, "_report_rule_sync"):
                t = threading.Thread(
                    target=_queue_watcher,
                    args=(identity, inject_fn),
                    kwargs={
                        "suppress_identity_hint": True,
                        "exec_prompt_suffix": "",
                    },
                    daemon=True,
                )
                t.start()
                deadline = time.time() + 5
                while time.time() < deadline and not received:
                    time.sleep(0.05)

        self.assertTrue(received, "inject_fn was never called")
        self.assertEqual(received[0]["channel"], "design-review")
        self.assertIn("@agy Reply exactly: PING_OK", received[0]["text"])

    def test_store_exec_enqueue_signature_present_in_wrapper(self):
        src = (ROOT / "wrapper.py").read_text(encoding="utf-8")
        self.assertIn('def _enqueue(text, channel="", **kwargs):', src)


class AgyReplyExtractionTests(unittest.TestCase):
    """Hardened _extract_agy_reply: verdict preference and listing rejection."""

    def _write_transcript(self, tmp: Path, conv_id: str, model_contents: list[str]):
        tdir = (
            tmp / "brain" / conv_id / ".system_generated" / "logs"
        )
        tdir.mkdir(parents=True)
        lines = []
        for content in model_contents:
            lines.append(json.dumps({"source": "MODEL", "content": content}))
        (tdir / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_prefers_verdict_over_directory_listing(self):
        listing = (
            "Created At: 2026-06-26\n"
            '{"name": "codex-cwd", "isDir": true, "sizeBytes": 0}\n'
            "Summary: This directory contains server.log and data/"
        )
        verdict = "PASS WITH NOTES\nMobile layout looks good."
        with tempfile.TemporaryDirectory() as tmp:
            conv = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            self._write_transcript(Path(tmp), conv, [listing, verdict])
            reply = _extract_agy_reply(conv, agy_data_dir=tmp)
        self.assertEqual(reply, verdict)

    def test_rejects_directory_listing_only_transcript(self):
        listing = (
            "Created At: 2026-06-26\n"
            '{"name": "server.log", "isDir": false, "sizeBytes": 99}\n'
            "Summary: This directory contains codex-cwd"
        )
        with tempfile.TemporaryDirectory() as tmp:
            conv = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
            self._write_transcript(Path(tmp), conv, [listing])
            reply = _extract_agy_reply(conv, agy_data_dir=tmp)
        self.assertEqual(reply, "")

    def test_directory_listing_heuristic_positive(self):
        text = (
            "Created At: x\n"
            '{"name": "data/", "isDir": true}\n'
            "Summary: This directory contains files"
        )
        self.assertTrue(_is_agy_directory_listing_output(text))

    def test_verdict_token_detection(self):
        self.assertEqual(_agy_first_verdict_token("PASS\nnotes"), "PASS")
        self.assertEqual(
            _agy_first_verdict_token("REQUEST UX CHANGES\nfix tap targets"),
            "REQUEST UX CHANGES",
        )
        self.assertIsNone(_agy_first_verdict_token("Created At: now"))


if __name__ == "__main__":
    unittest.main()
