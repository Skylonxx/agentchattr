"""Unit tests for session_engine prompt assembly (store_exec vs MCP/TUI paths)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from session_engine import SessionEngine, build_store_exec_session_prompt  # noqa: E402


class _FakeRegistry:
    def __init__(self, bases: dict):
        self._bases = bases

    def get_instance(self, name: str):
        return {"base": name, "name": name}

    def get_base_config(self, base: str):
        return dict(self._bases.get(base, {}))


class _FakeMessages:
    def __init__(self, recent: list[dict]):
        self._recent = recent

    def get_recent(self, count=10, channel="general"):
        return self._recent[:count]

    def on_message(self, _cb):
        pass


class StoreExecPromptTests(unittest.TestCase):
    def setUp(self):
        self.session = {
            "id": 1,
            "channel": "sandbox-flow-v2-d-test",
            "goal": "Validate bakery POS UX",
            "current_phase": 1,
            "current_turn": 0,
            "template_id": "sandbox-bakery-flow",
            "cast": {"ui_lead": "agy", "developer": "claude"},
        }
        self.tmpl = {
            "name": "Sandbox Bakery Flow",
            "phases": [
                {"name": "developer", "participants": ["developer"], "prompt": "Build UI."},
                {
                    "name": "ui_review",
                    "participants": ["ui_lead"],
                    "prompt": "Review mobile layout and accessibility.",
                },
            ],
        }
        self.phase = self.tmpl["phases"][1]
        self.recent = [
            {"sender": "claude", "text": "READY_FOR_REVIEW_PACKAGE\n\nPayment modal done."},
            {"sender": "system", "text": "phase advance"},
        ]
        registry = _FakeRegistry({"agy": {"run_mode": "store_exec", "command": "agy"}})
        messages = _FakeMessages(self.recent)
        self.engine = SessionEngine(
            MagicMock(), messages, MagicMock(), registry,
        )

    def test_store_exec_prompt_excludes_chat_send(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        self.assertNotIn("chat_send", prompt)

    def test_store_exec_prompt_excludes_chat_read(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        self.assertNotIn("chat_read", prompt)

    def test_store_exec_prompt_excludes_mcp_instructions(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        lowered = prompt.lower()
        self.assertNotIn("mcp read", lowered)
        self.assertNotIn("mcp tool", lowered)
        self.assertNotIn("use tools", lowered.replace("do not use tools", ""))

    def test_store_exec_prompt_includes_verdict_token_requirement(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        for token in ("PASS", "PASS WITH NOTES", "REQUEST UX CHANGES", "BLOCKED"):
            self.assertIn(token, prompt)
        self.assertIn("First line MUST be exactly one of:", prompt)

    def test_store_exec_prompt_includes_plain_text_and_tool_bans(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        self.assertIn("Output ONLY plain text", prompt)
        self.assertIn("Do not list directories", prompt)
        self.assertIn("Do not use MCP", prompt)
        self.assertIn("Do not create or edit files", prompt)

    def test_store_exec_prompt_inlines_developer_context(self):
        prompt = self.engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="agy",
        )
        self.assertIn("CHANNEL: #sandbox-flow-v2-d-test", prompt)
        self.assertIn("GOAL: Validate bakery POS UX", prompt)
        self.assertIn("PHASE: ui_review", prompt)
        self.assertIn("UI/UX reviewer", prompt)
        self.assertIn("READY_FOR_REVIEW_PACKAGE", prompt)
        self.assertIn("[claude]:", prompt)

    def test_tui_agent_prompt_unchanged(self):
        registry = _FakeRegistry({"gemini": {"run_mode": "tui", "command": "gemini"}})
        engine = SessionEngine(MagicMock(), _FakeMessages([]), MagicMock(), registry)
        prompt = engine._assemble_prompt(
            self.session, self.tmpl, self.phase, "ui_lead", agent="gemini",
        )
        self.assertIn("chat_send", prompt)
        self.assertIn("chat_read", prompt)
        self.assertIn("mcp read", prompt.lower())

    def test_build_store_exec_session_prompt_standalone_contract(self):
        prompt = build_store_exec_session_prompt(
            session_name="Test",
            channel="chan",
            goal="g",
            phase_name="p",
            phase_index=0,
            total_phases=2,
            role="ui_lead",
            instruction="Review.",
            context_messages=[{"sender": "dev", "text": "output"}],
        )
        self.assertIn("[dev]: output", prompt)
        self.assertNotIn("chat_send", prompt)


if __name__ == "__main__":
    unittest.main()
