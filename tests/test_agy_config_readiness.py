"""Focused AGY operational-readiness tests.

These tests keep AGY useful for future UI/UX review while ensuring this phase
does not enable production AGY relay, production Claude relay, broad MCP,
Slack MCP, Target:* access, or subagent loops.
"""

import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible  # noqa: E402
from wrapper import (  # noqa: E402
    _build_agy_store_command,
    _build_direct_mention_prompt,
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
            "call MCP tools",
            "Slack MCP",
            "Target:*",
            "spawn subagents",
            "Do not coordinate workflow",
            "safety gate",
        ]
        for text in required:
            self.assertIn(text, prompt)

    def test_agy_has_no_mcp_injection_defaults(self):
        self.assertNotIn("mcp_inject", self.agy)
        self.assertEqual(_resolve_mcp_inject("agy", self.agy), {})

    def test_agy_and_claude_remain_relay_ineligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertFalse(is_relay_eligible("claude"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)
        self.assertNotIn("claude", RELAY_ELIGIBLE_AGENTS)


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
        self.assertIn("Do not use MCP tools", prompt)
        self.assertIn("AGY reviewer mode", prompt)
        self.assertIn("Do not coordinate workflow", prompt)
        self.assertIn("Return concise findings", prompt)

    def test_direct_prompt_without_suffix_preserves_existing_base_contract(self):
        prompt = _build_direct_mention_prompt("general", "hello")
        self.assertIn("Message:\n---\nhello\n---", prompt)
        self.assertIn("Do not use MCP tools", prompt)
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


if __name__ == "__main__":
    unittest.main()
