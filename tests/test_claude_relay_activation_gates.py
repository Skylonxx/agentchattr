"""V2-D claude_relay activation gate tests — authorized production activation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import safety_invariants as si
import wrapper
from claude_relay import DEFAULT_OWNERSHIP_MARKER, build_claude_command, validate_scratch_cwd
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

    def test_claude_dryrun_not_in_relay_eligible_agents(self):
        self.assertNotIn("claude_dryrun", RELAY_ELIGIBLE_AGENTS)
        self.assertFalse(is_relay_eligible("claude_dryrun"))


_SEALED_TAIL = (
    "-p",
    "--output-format", "json",
    "--input-format", "text",
    "--tools", "",
    "--strict-mcp-config",
)


class ClaudeRelayWindowsExecResolutionTests(unittest.TestCase):
    def test_resolve_claude_relay_command_win32_prefers_claude_cmd(self):
        sealed = build_claude_command()
        fake_cmd = r"C:\fake\bin\claude.cmd"
        with mock.patch.object(wrapper.sys, "platform", "win32"), \
             mock.patch.object(wrapper.shutil, "which", side_effect=lambda name: fake_cmd if name == "claude.cmd" else None):
            resolved = wrapper._resolve_claude_relay_command(sealed)
        self.assertEqual(resolved[0], fake_cmd)
        self.assertEqual(resolved[1:], list(_SEALED_TAIL))

    def test_resolve_claude_relay_command_win32_fallback_to_claude(self):
        sealed = build_claude_command()
        fake_exe = r"C:\fake\bin\claude.exe"
        with mock.patch.object(wrapper.sys, "platform", "win32"), \
             mock.patch.object(wrapper.shutil, "which", side_effect=lambda name: fake_exe if name == "claude" else None):
            resolved = wrapper._resolve_claude_relay_command(sealed)
        self.assertEqual(resolved[0], fake_exe)
        self.assertEqual(resolved[1:], list(_SEALED_TAIL))

    def test_resolve_claude_relay_command_non_windows_uses_which(self):
        sealed = build_claude_command()
        fake_exe = "/usr/local/bin/claude"
        with mock.patch.object(wrapper.sys, "platform", "linux"), \
             mock.patch.object(wrapper.shutil, "which", return_value=fake_exe):
            resolved = wrapper._resolve_claude_relay_command(sealed)
        self.assertEqual(resolved[0], fake_exe)
        self.assertEqual(resolved[1:], list(_SEALED_TAIL))

    def test_resolve_claude_relay_command_missing_executable_fails_closed(self):
        sealed = build_claude_command()
        with mock.patch.object(wrapper.sys, "platform", "win32"), \
             mock.patch.object(wrapper.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                wrapper._resolve_claude_relay_command(sealed)

    def test_resolve_claude_relay_command_does_not_mutate_input(self):
        sealed = build_claude_command()
        original = list(sealed)
        fake_cmd = r"C:\fake\bin\claude.cmd"
        with mock.patch.object(wrapper.sys, "platform", "win32"), \
             mock.patch.object(wrapper.shutil, "which", return_value=fake_cmd):
            wrapper._resolve_claude_relay_command(sealed)
        self.assertEqual(sealed, original)

    def test_relay_subprocess_contract_no_shell_kwarg(self):
        """Mirror run_agent_claude_relay launch kwargs: shell must not be True."""
        import subprocess

        sealed = build_claude_command()
        with mock.patch.object(wrapper.sys, "platform", "win32"), \
             mock.patch.object(wrapper.shutil, "which", return_value=r"C:\fake\claude.cmd"):
            resolved = wrapper._resolve_claude_relay_command(sealed)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"{}", stderr=b"")
            subprocess.run(
                resolved,
                cwd=".",
                env={},
                input=b"test",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        _, call_kwargs = mock_run.call_args
        self.assertNotEqual(call_kwargs.get("shell"), True)
        self.assertEqual(mock_run.call_args[0][0][0], r"C:\fake\claude.cmd")


if __name__ == "__main__":
    unittest.main()
