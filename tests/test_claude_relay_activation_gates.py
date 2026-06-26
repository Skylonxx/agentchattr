"""V2-D claude_relay activation gate tests — authorized production activation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import safety_invariants as si
import wrapper
from claude_relay import DEFAULT_OWNERSHIP_MARKER, validate_scratch_cwd
from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible


_AUTHORIZED_RELAY_SET = frozenset({
    "claude",
    "codex",
    "codexsafe",
    "codex_coordinator",
    "codex_reviewer",
})

_VALID_SANDBOX_META = {
    "relay_mode": True,
    "disable_mcp": True,
    "channel": "sandbox-flow-v2-d-260625-1900-01",
}


class ClaudeRelayActivationGateTests(unittest.TestCase):
    def test_wrapper_activation_flag_is_true(self):
        self.assertTrue(wrapper.CLAUDE_RELAY_ACTIVATED)

    def test_claude_in_relay_eligible_agents(self):
        self.assertIn("claude", RELAY_ELIGIBLE_AGENTS)
        self.assertTrue(is_relay_eligible("claude"))

    def test_agy_not_in_relay_eligible_agents(self):
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)
        self.assertFalse(is_relay_eligible("agy"))

    def test_safety_invariants_permit_authorized_claude_activation(self):
        self.assertTrue(si.check_relay_eligibility(RELAY_ELIGIBLE_AGENTS).ok)
        self.assertFalse(si.is_production_relay_ineligible("claude"))
        self.assertTrue(
            si.require_live_relay_activation(True, "claude").ok,
        )

    def test_safety_invariants_block_agy_relay_activation(self):
        self.assertTrue(si.is_production_relay_ineligible("agy"))
        r = si.require_live_relay_activation(True, "agy")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-002")
        self.assertFalse(si.check_relay_eligibility(RELAY_ELIGIBLE_AGENTS | {"agy"}).ok)

    def test_meta_ok_rejects_missing_relay_meta(self):
        self.assertFalse(wrapper._claude_relay_meta_ok(None))

    def test_meta_ok_rejects_missing_channel(self):
        self.assertFalse(wrapper._claude_relay_meta_ok({
            "relay_mode": True,
            "disable_mcp": True,
        }))
        self.assertFalse(wrapper._claude_relay_meta_ok({
            "relay_mode": True,
            "disable_mcp": True,
            "channel": "",
        }))

    def test_meta_ok_rejects_general_fallback(self):
        self.assertFalse(wrapper._claude_relay_meta_ok({
            "relay_mode": True,
            "disable_mcp": True,
            "channel": "general",
        }))
        self.assertFalse(wrapper._claude_relay_meta_ok({
            "relay_mode": True,
            "disable_mcp": True,
            "channel": "#general",
        }))

    def test_meta_ok_accepts_valid_sandbox_channel(self):
        self.assertTrue(wrapper._claude_relay_meta_ok(_VALID_SANDBOX_META))
        self.assertEqual(
            wrapper._claude_relay_channel(_VALID_SANDBOX_META),
            "sandbox-flow-v2-d-260625-1900-01",
        )

    def test_scratch_cwd_rejects_polluted_dir_and_accepts_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "turn-1"
            child.mkdir()
            (child / "stray.txt").write_text("x", encoding="utf-8")
            res = validate_scratch_cwd(child)
            self.assertFalse(res.ok)

            child2 = root / "turn-2"
            child2.mkdir()
            (child2 / DEFAULT_OWNERSHIP_MARKER).write_text("", encoding="utf-8")
            res = validate_scratch_cwd(child2)
            self.assertTrue(res.ok)

            child3 = root / "turn-3"
            child3.mkdir()
            res = validate_scratch_cwd(child3)
            self.assertTrue(res.ok)

    def test_authorized_relay_set_matches_expected(self):
        self.assertEqual(RELAY_ELIGIBLE_AGENTS, _AUTHORIZED_RELAY_SET)


if __name__ == "__main__":
    unittest.main()
