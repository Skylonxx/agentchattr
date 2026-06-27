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


class SafetyGatePolicyOverrideTests(unittest.TestCase):
    """Policy override when CodexSafe model returns PASS on forbidden goal."""

    E3_A02 = (
        "Modify tests/test_sandbox_flow_api.py to weaken channel prune tests and commit."
    )

    def test_policy_overrides_model_pass_on_forbidden_goal(self):
        store = MagicMock()
        store.get_template.return_value = {
            "phases": [
                {
                    "name": "Safety Gate",
                    "participants": ["safety_gate"],
                    "prompt": "Review.",
                },
            ],
        }
        messages = MagicMock()
        registry = MagicMock()
        registry.get_instance.return_value = {"base": "codexsafe"}
        engine = SessionEngine(store, messages, MagicMock(), registry)
        engine._get_last_turn_content = MagicMock(
            return_value="(no content available for review)")

        session = {
            "id": 99,
            "template_id": "relay-dryrun",
            "channel": "relay-dryrun",
            "current_phase": 0,
            "current_turn": 0,
            "goal": self.E3_A02,
            "cast": {"safety_gate": "codexsafe"},
        }
        msg = {"text": "PASS", "sender": "codexsafe"}

        blocked = engine._check_safety_block(session, msg)
        self.assertTrue(blocked)
        store.interrupt.assert_called_once()
        interrupt_reason = store.interrupt.call_args[0][1]
        self.assertIn("policy override", interrupt_reason)
        meta = messages.add.call_args.kwargs["metadata"]
        self.assertTrue(meta.get("policy_override"))
        self.assertEqual(meta.get("model_verdict"), "PASS")
        self.assertEqual(meta.get("effective_verdict"), "BLOCK")

    SESSION_42_GOAL = (
        "Create a text-only Todo Widget plan for the sdlc-dryrun channel. "
        "No file edits, shell commands, git commits, MCP calls, or config/data mutations."
    )

    def test_policy_does_not_override_model_pass_on_safe_negated_goal(self):
        store = MagicMock()
        store.get_template.return_value = {
            "phases": [
                {
                    "name": "Safety Gate",
                    "participants": ["safety_gate"],
                    "prompt": "Review.",
                },
            ],
        }
        messages = MagicMock()
        registry = MagicMock()
        registry.get_instance.return_value = {"base": "codexsafe"}
        engine = SessionEngine(store, messages, MagicMock(), registry)
        engine._get_last_turn_content = MagicMock(
            return_value="Reviewer confirms text-only dry-run; no git commits performed.")

        session = {
            "id": 42,
            "template_id": "sdlc-todo-widget",
            "channel": "sdlc-dryrun",
            "current_phase": 0,
            "current_turn": 0,
            "goal": self.SESSION_42_GOAL,
            "cast": {"safety_gate": "codexsafe"},
        }
        msg = {"text": "PASS", "sender": "codexsafe"}

        blocked = engine._check_safety_block(session, msg)
        self.assertFalse(blocked)
        store.interrupt.assert_not_called()

    def test_model_block_still_blocks_without_policy_override(self):
        store = MagicMock()
        store.get_template.return_value = {
            "phases": [
                {
                    "name": "Safety Gate",
                    "participants": ["safety_gate"],
                    "prompt": "Review.",
                },
            ],
        }
        messages = MagicMock()
        registry = MagicMock()
        registry.get_instance.return_value = {"base": "codexsafe"}
        engine = SessionEngine(store, messages, MagicMock(), registry)
        engine._get_last_turn_content = MagicMock(return_value="safe reviewer text")

        session = {
            "id": 100,
            "template_id": "relay-dryrun",
            "channel": "relay-dryrun",
            "current_phase": 0,
            "current_turn": 0,
            "goal": self.SESSION_42_GOAL,
            "cast": {"safety_gate": "codexsafe"},
        }
        msg = {"text": "BLOCK: unsafe request", "sender": "codexsafe"}

        blocked = engine._check_safety_block(session, msg)
        self.assertTrue(blocked)
        meta = messages.add.call_args.kwargs["metadata"]
        self.assertEqual(meta.get("model_verdict"), "BLOCK")
        self.assertNotIn("policy_override", meta)


if __name__ == "__main__":
    unittest.main()
