"""Working-session MCP control-plane contract tests.

Validates that:
  - Repo/source tool restrictions remain intact per role.
  - Agentchattr control-plane MCP tools are explicitly allowed per role.
  - The direct-mention prompt does not blanket-block control-plane tools.
  - Codex reviewer can use allowed control-plane actions.
  - AGY ui_lead can use allowed control-plane actions.
  - Safety guard has no control-plane access.
  - Unknown roles/tools fail closed.
  - Production Claude/AGY remain relay-ineligible.
  - CodexSafe cannot be selected as a workflow persona.
  - No external workflow prompt contains 'Codex Coordinator' / 'Codex Reviewer split'.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from safety_invariants import (
    CONTROL_PLANE_TOOLS,
    ROLE_CONTROL_PLANE,
    check_control_plane_access,
    check_immutable_role_prompt,
    build_immutable_role_prompt,
    ROLE_PROMPT_ROLES,
    PRODUCTION_RELAY_INELIGIBLE,
    SAFETY_MECHANISM_IDENTITIES,
    check_codexsafe_boundary_only,
    is_production_relay_ineligible,
    INVARIANTS,
)
from session_relay import RELAY_ELIGIBLE_AGENTS


class TestControlPlaneAllowlist(unittest.TestCase):
    """INV-020: Control-plane MCP tools are explicit allowlist per role."""

    def test_control_plane_tools_is_explicit_frozenset(self):
        self.assertIsInstance(CONTROL_PLANE_TOOLS, frozenset)
        self.assertTrue(len(CONTROL_PLANE_TOOLS) > 0)

    def test_role_control_plane_covers_all_prompt_roles(self):
        for role in ROLE_PROMPT_ROLES:
            self.assertIn(role, ROLE_CONTROL_PLANE,
                          f"role '{role}' missing from ROLE_CONTROL_PLANE")

    def test_all_role_tools_are_known_control_plane_tools(self):
        for role, tools in ROLE_CONTROL_PLANE.items():
            for tool in tools:
                self.assertIn(tool, CONTROL_PLANE_TOOLS,
                              f"role '{role}' lists unknown tool '{tool}'")

    def test_safety_guard_has_no_control_plane_access(self):
        self.assertEqual(ROLE_CONTROL_PLANE["safety_guard"], frozenset())

    def test_reviewer_can_use_chat_send(self):
        r = check_control_plane_access("reviewer", "chat_send")
        self.assertTrue(r.ok, r.reason)

    def test_reviewer_can_use_chat_read(self):
        r = check_control_plane_access("reviewer", "chat_read")
        self.assertTrue(r.ok, r.reason)

    def test_reviewer_can_use_chat_propose_job(self):
        r = check_control_plane_access("reviewer", "chat_propose_job")
        self.assertTrue(r.ok, r.reason)

    def test_ui_lead_can_use_chat_send(self):
        r = check_control_plane_access("ui_lead", "chat_send")
        self.assertTrue(r.ok, r.reason)

    def test_ui_lead_can_use_chat_read(self):
        r = check_control_plane_access("ui_lead", "chat_read")
        self.assertTrue(r.ok, r.reason)

    def test_developer_can_use_all_control_plane_tools(self):
        for tool in CONTROL_PLANE_TOOLS:
            r = check_control_plane_access("developer", tool)
            self.assertTrue(r.ok, f"developer blocked from {tool}: {r.reason}")

    def test_safety_guard_blocked_from_all_control_plane_tools(self):
        for tool in CONTROL_PLANE_TOOLS:
            r = check_control_plane_access("safety_guard", tool)
            self.assertFalse(r.ok, f"safety_guard should not access {tool}")

    def test_unknown_role_fails_closed(self):
        r = check_control_plane_access("unknown_role", "chat_send")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-020")

    def test_unknown_tool_fails_closed(self):
        r = check_control_plane_access("reviewer", "shell_exec")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-020")

    def test_none_role_fails_closed(self):
        r = check_control_plane_access(None, "chat_send")
        self.assertFalse(r.ok)

    def test_empty_tool_fails_closed(self):
        r = check_control_plane_access("reviewer", "")
        self.assertFalse(r.ok)

    def test_inv_020_in_catalogue(self):
        self.assertIn("INV-020", INVARIANTS)


class TestImmutableRolePromptsControlPlane(unittest.TestCase):
    """INV-018: Immutable role prompts include CONTROL-PLANE section."""

    def test_all_role_prompts_contain_control_plane_section(self):
        for role in ROLE_PROMPT_ROLES:
            prompt = build_immutable_role_prompt(role)
            self.assertIn("CONTROL-PLANE", prompt,
                          f"role '{role}' prompt missing CONTROL-PLANE section")

    def test_reviewer_prompt_allows_chat_tools(self):
        prompt = build_immutable_role_prompt("reviewer")
        self.assertIn("chat_send", prompt)
        self.assertIn("chat_read", prompt)
        self.assertIn("chat_propose_job", prompt)

    def test_reviewer_prompt_forbids_repo_source_tools(self):
        prompt = build_immutable_role_prompt("reviewer")
        self.assertIn("FORBIDDEN", prompt)
        self.assertIn("editing files", prompt)
        self.assertIn("running shell", prompt)
        self.assertIn("committing", prompt)

    def test_ui_lead_prompt_allows_chat_tools(self):
        prompt = build_immutable_role_prompt("ui_lead")
        self.assertIn("chat_send", prompt)
        self.assertIn("chat_read", prompt)

    def test_ui_lead_prompt_forbids_slack_mcp(self):
        prompt = build_immutable_role_prompt("ui_lead")
        self.assertIn("Slack MCP", prompt)

    def test_safety_guard_prompt_has_no_tool_access(self):
        prompt = build_immutable_role_prompt("safety_guard")
        self.assertIn("CONTROL-PLANE: none", prompt)
        self.assertNotIn("chat_send", prompt)
        self.assertNotIn("chat_read", prompt)

    def test_check_immutable_role_prompt_validates_control_plane(self):
        for role in ROLE_PROMPT_ROLES:
            prompt = build_immutable_role_prompt(role)
            r = check_immutable_role_prompt(prompt, role)
            self.assertTrue(r.ok, f"role '{role}' fails check: {r.reason}")

    def test_prompt_missing_control_plane_fails(self):
        prompt = "[IMMUTABLE ROLE: reviewer]\nIMMUTABLE\nFORBIDDEN: stuff"
        r = check_immutable_role_prompt(prompt, "reviewer")
        self.assertFalse(r.ok)
        self.assertIn("control-plane", r.reason)


class TestDirectMentionPromptContract(unittest.TestCase):
    """The direct-mention prompt must not blanket-block control-plane tools."""

    def test_direct_mention_prompt_does_not_say_no_mcp_tools(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt("general", "test message")
        self.assertNotIn("Do not use MCP tools", prompt)

    def test_direct_mention_prompt_forbids_repo_source_actions(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt("general", "test message")
        self.assertIn("Do not edit files", prompt)
        self.assertIn("Do not run shell commands", prompt)
        self.assertIn("Do not run git commands", prompt)

    def test_direct_mention_prompt_with_reviewer_role_allows_control_plane(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt(
            "general", "test message", role="reviewer")
        self.assertIn("CONTROL-PLANE ALLOWED", prompt)
        self.assertIn("chat_send", prompt)
        self.assertIn("chat_read", prompt)

    def test_direct_mention_prompt_with_reviewer_role_forbids_repo(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt(
            "general", "test message", role="reviewer")
        self.assertIn("FORBIDDEN", prompt)
        self.assertIn("editing files", prompt)
        self.assertIn("running shell", prompt)

    def test_direct_mention_prompt_with_ui_lead_allows_control_plane(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt(
            "general", "test message", role="ui_lead")
        self.assertIn("CONTROL-PLANE ALLOWED", prompt)

    def test_direct_mention_prompt_with_unknown_role_fails_closed(self):
        from wrapper import _build_direct_mention_prompt
        with self.assertRaises(SystemExit):
            _build_direct_mention_prompt(
                "general", "test message", role="hacker")


class TestComposedAgyPrompt(unittest.TestCase):
    """The composed AGY/ui_lead prompt must allow control-plane, forbid repo/source."""

    def _build_agy_prompt(self):
        from wrapper import _build_direct_mention_prompt
        import tomllib
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        agy_cfg = cfg["agents"]["agy"]
        suffix = agy_cfg.get("exec_prompt_suffix", "")
        return _build_direct_mention_prompt(
            "design-review", "Review the POS layout",
            exec_prompt_suffix=suffix, role="ui_lead")

    def test_composed_agy_prompt_has_no_broad_mcp_prohibition(self):
        prompt = self._build_agy_prompt()
        self.assertNotIn("call MCP tools", prompt)
        self.assertNotIn("Do not use MCP tools", prompt)
        self.assertNotIn("calling MCP", prompt)

    def test_composed_agy_prompt_has_control_plane_allowed(self):
        prompt = self._build_agy_prompt()
        self.assertIn("CONTROL-PLANE ALLOWED", prompt)
        self.assertIn("chat_send", prompt)
        self.assertIn("chat_read", prompt)

    def test_composed_agy_prompt_forbids_slack_mcp(self):
        prompt = self._build_agy_prompt()
        self.assertIn("Slack MCP", prompt)

    def test_composed_agy_prompt_forbids_subagents(self):
        prompt = self._build_agy_prompt()
        self.assertIn("subagents", prompt)

    def test_composed_agy_prompt_forbids_target(self):
        prompt = self._build_agy_prompt()
        self.assertIn("Target:*", prompt)

    def test_composed_agy_prompt_forbids_file_edits(self):
        prompt = self._build_agy_prompt()
        self.assertIn("Do not edit files", prompt)

    def test_composed_agy_prompt_forbids_shell(self):
        prompt = self._build_agy_prompt()
        self.assertIn("Do not run shell commands", prompt)

    def test_composed_agy_prompt_forbids_permission_persistence(self):
        prompt = self._build_agy_prompt()
        self.assertIn("persist permissions", prompt)


class TestRelayPromptStillBlocksMcp(unittest.TestCase):
    """Relay prompts (session turns) must still prohibit all MCP tools."""

    def test_relay_prompt_blocks_mcp(self):
        from session_relay import build_relay_prompt
        prompt = build_relay_prompt(
            session_name="test", goal="test", phase_name="test",
            phase_index=0, total_phases=1, role="reviewer",
            instruction="review it")
        self.assertIn("Do not use MCP tools", prompt)
        self.assertIn("Do not call chat_read or chat_send", prompt)

    def test_safety_gate_prompt_blocks_mcp(self):
        from session_relay import build_safety_gate_prompt
        prompt = build_safety_gate_prompt(
            session_name="test", goal="test", phase_name="test",
            content_to_review="test content")
        self.assertIn("Do not use MCP tools", prompt)


class TestProductionRelayIneligibility(unittest.TestCase):
    """Production Claude and AGY remain relay-ineligible."""

    def test_claude_relay_ineligible(self):
        self.assertTrue(is_production_relay_ineligible("claude"))
        self.assertNotIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_agy_relay_ineligible(self):
        self.assertTrue(is_production_relay_ineligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)


class TestCodexSafeBoundaryOnly(unittest.TestCase):
    """CodexSafe remains boundary guard only, never a workflow persona."""

    def test_codexsafe_rejected_from_workflow_roles(self):
        for role in ("developer", "reviewer", "ui_lead", "coordinator"):
            r = check_codexsafe_boundary_only({role: "codexsafe"})
            self.assertFalse(r.ok,
                             f"codexsafe should not be cast as {role}")

    def test_codexsafe_allowed_as_safety_gate(self):
        r = check_codexsafe_boundary_only({"safety_gate": "codexsafe"})
        self.assertTrue(r.ok, r.reason)


class TestExternalRoleLock(unittest.TestCase):
    """External workflow role names are locked; internal split names excluded."""

    def test_no_prompt_contains_codex_coordinator_external_role(self):
        for role in ROLE_PROMPT_ROLES:
            prompt = build_immutable_role_prompt(role)
            self.assertNotIn("Codex Coordinator", prompt,
                             f"role '{role}' prompt leaks internal identity name")

    def test_no_prompt_contains_codex_reviewer_split_external_role(self):
        for role in ROLE_PROMPT_ROLES:
            prompt = build_immutable_role_prompt(role)
            self.assertNotIn("Codex Reviewer", prompt,
                             f"role '{role}' prompt leaks internal identity name")

    def test_no_prompt_contains_codexsafe_as_workflow_persona(self):
        for role in ROLE_PROMPT_ROLES:
            if role == "safety_guard":
                continue
            prompt = build_immutable_role_prompt(role)
            self.assertNotIn("CodexSafe is", prompt.replace(
                "CodexSafe is a boundary guard only", ""),
                f"role '{role}' references CodexSafe as persona")


if __name__ == "__main__":
    unittest.main()
