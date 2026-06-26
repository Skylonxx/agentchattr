"""Working-session MCP control-plane contract tests.

Validates that:
  - Repo/source tool restrictions remain intact per role.
  - Agentchattr control-plane MCP tools are explicitly allowed per role.
  - The direct-mention prompt does not blanket-block control-plane tools.
  - Codex reviewer can use allowed control-plane actions.
  - AGY ui_lead can use allowed control-plane actions.
  - Safety guard has no control-plane access.
  - Unknown roles/tools fail closed.
  - Production Claude is relay-eligible via authorized claude_relay activation.
  - AGY remains relay-ineligible.
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
    """Production AGY remains relay-ineligible; Claude is authorized relay-eligible."""

    def test_claude_relay_eligible(self):
        self.assertFalse(is_production_relay_ineligible("claude"))
        self.assertIn("claude", RELAY_ELIGIBLE_AGENTS)

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


class TestMcpAutoApproval(unittest.TestCase):
    """Codex proxy_flag MCP injection uses native per-tool approval (INV-021)."""

    def _get_proxy_flag_args(self, auto_approve=True):
        import tempfile
        from pathlib import Path
        from wrapper import _apply_mcp_inject
        inject_cfg = {"mcp_inject": "proxy_flag",
                      "mcp_proxy_flag_template": '-c mcp_servers.{server}.url="{url}"',
                      "mcp_auto_approve": auto_approve}
        with tempfile.TemporaryDirectory() as td:
            args, _, _ = _apply_mcp_inject(
                inject_cfg, "codex", Path(td), "http://127.0.0.1:9999/mcp")
        return args

    def test_proxy_flag_does_not_emit_requires_approval(self):
        joined = " ".join(self._get_proxy_flag_args())
        self.assertNotIn("requires_approval", joined)

    def test_proxy_flag_emits_enabled_tools(self):
        joined = " ".join(self._get_proxy_flag_args())
        self.assertIn("enabled_tools", joined)
        self.assertIn("chat_read", joined)
        self.assertIn("chat_send", joined)
        self.assertIn("chat_propose_job", joined)

    def test_proxy_flag_emits_per_tool_approval_mode_auto(self):
        joined = " ".join(self._get_proxy_flag_args())
        for tool in ("chat_read", "chat_send", "chat_propose_job"):
            self.assertIn(f"tools.{tool}.approval_mode", joined)
        self.assertIn('"auto"', joined)

    def test_proxy_flag_only_enables_3_working_session_tools(self):
        from safety_invariants import CODEX_MCP_AUTO_APPROVE_TOOLS
        self.assertEqual(CODEX_MCP_AUTO_APPROVE_TOOLS,
                         frozenset({"chat_read", "chat_send", "chat_propose_job"}))

    def test_proxy_flag_auto_approve_can_be_disabled(self):
        args = self._get_proxy_flag_args(auto_approve=False)
        joined = " ".join(args)
        self.assertNotIn("enabled_tools", joined)
        self.assertNotIn("approval_mode", joined)
        self.assertNotIn("requires_approval", joined)

    def test_proxy_flag_args_pass_inv021_validation(self):
        from wrapper import SERVER_NAME
        from safety_invariants import validate_mcp_config_overrides
        args = self._get_proxy_flag_args()
        r = validate_mcp_config_overrides(args, SERVER_NAME)
        self.assertTrue(r.ok, r.reason)


class TestMcpConfigValidation(unittest.TestCase):
    """INV-021: MCP config overrides must be limited to safe keys AND values."""

    def _v(self, *overrides):
        from safety_invariants import validate_mcp_config_overrides
        args = []
        for o in overrides:
            args += ["-c", o]
        return validate_mcp_config_overrides(args, "agentchattr")

    # --- enabled_tools: safe values ---

    def test_enabled_tools_exact_set_accepted(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["chat_propose_job","chat_read","chat_send"]')
        self.assertTrue(r.ok, r.reason)

    def test_enabled_tools_subset_accepted(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["chat_read","chat_send"]')
        self.assertTrue(r.ok, r.reason)

    def test_enabled_tools_single_accepted(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["chat_read"]')
        self.assertTrue(r.ok, r.reason)

    # --- enabled_tools: unsafe values ---

    def test_enabled_tools_unknown_tool_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["shell_exec"]')
        self.assertFalse(r.ok)
        self.assertIn("unknown tool", r.reason)

    def test_enabled_tools_mixed_allowed_and_unknown_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["chat_read","shell_exec"]')
        self.assertFalse(r.ok)
        self.assertIn("unknown tool", r.reason)

    def test_enabled_tools_superset_with_extra_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=["chat_read","chat_send","chat_propose_job","extra_tool"]')
        self.assertFalse(r.ok)

    def test_enabled_tools_empty_array_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=[]')
        self.assertFalse(r.ok)
        self.assertIn("empty", r.reason)

    def test_enabled_tools_bare_string_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools="chat_read"')
        self.assertFalse(r.ok)
        self.assertIn("array", r.reason.lower())

    def test_enabled_tools_malformed_rejected(self):
        r = self._v('mcp_servers.agentchattr.enabled_tools=not_an_array')
        self.assertFalse(r.ok)

    # --- default_tools_approval_mode: safe values ---

    def test_default_approval_mode_auto_accepted(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="auto"')
        self.assertTrue(r.ok, r.reason)

    def test_default_approval_mode_prompt_accepted(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="prompt"')
        self.assertTrue(r.ok, r.reason)

    def test_default_approval_mode_approve_accepted(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="approve"')
        self.assertTrue(r.ok, r.reason)

    # --- default_tools_approval_mode: unsafe values ---

    def test_default_approval_mode_never_rejected(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="never"')
        self.assertFalse(r.ok)
        self.assertIn("unsafe", r.reason)

    def test_default_approval_mode_always_rejected(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="always"')
        self.assertFalse(r.ok)

    def test_default_approval_mode_true_rejected(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="true"')
        self.assertFalse(r.ok)

    def test_default_approval_mode_danger_rejected(self):
        r = self._v('mcp_servers.agentchattr.default_tools_approval_mode="danger-full-access"')
        self.assertFalse(r.ok)

    # --- per-tool approval: safe ---

    def test_per_tool_approval_auto_accepted(self):
        r = self._v('mcp_servers.agentchattr.tools.chat_read.approval_mode="auto"')
        self.assertTrue(r.ok, r.reason)

    def test_per_tool_approval_prompt_accepted(self):
        r = self._v('mcp_servers.agentchattr.tools.chat_send.approval_mode="prompt"')
        self.assertTrue(r.ok, r.reason)

    def test_per_tool_approval_approve_accepted(self):
        r = self._v('mcp_servers.agentchattr.tools.chat_propose_job.approval_mode="approve"')
        self.assertTrue(r.ok, r.reason)

    # --- per-tool approval: unsafe ---

    def test_per_tool_unknown_tool_rejected(self):
        r = self._v('mcp_servers.agentchattr.tools.shell_exec.approval_mode="auto"')
        self.assertFalse(r.ok)

    def test_per_tool_approval_never_rejected(self):
        r = self._v('mcp_servers.agentchattr.tools.chat_read.approval_mode="never"')
        self.assertFalse(r.ok)

    # --- other keys ---

    def test_url_accepted(self):
        r = self._v('mcp_servers.agentchattr.url="http://127.0.0.1:8200/mcp"')
        self.assertTrue(r.ok, r.reason)

    def test_fake_requires_approval_rejected(self):
        r = self._v('mcp_servers.agentchattr.requires_approval="never"')
        self.assertFalse(r.ok)

    def test_command_key_rejected(self):
        r = self._v('mcp_servers.agentchattr.command="/bin/evil"')
        self.assertFalse(r.ok)

    def test_wrong_server_name_rejected(self):
        from safety_invariants import validate_mcp_config_overrides
        args = ["-c", 'mcp_servers.evil_server.enabled_tools=["chat_read"]']
        r = validate_mcp_config_overrides(args, "agentchattr")
        self.assertFalse(r.ok)

    def test_non_mcp_override_rejected(self):
        r = self._v('sandbox_mode="danger-full-access"')
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-021")

    def test_global_approval_policy_rejected(self):
        r = self._v('approval_policy="never"')
        self.assertFalse(r.ok)

    def test_non_c_flags_ignored(self):
        from safety_invariants import validate_mcp_config_overrides
        args = ["--sandbox", "read-only", "--ephemeral"]
        r = validate_mcp_config_overrides(args, "agentchattr")
        self.assertTrue(r.ok, r.reason)

    def test_generated_wrapper_config_passes(self):
        from wrapper import SERVER_NAME
        from safety_invariants import validate_mcp_config_overrides
        import tempfile
        from pathlib import Path
        from wrapper import _apply_mcp_inject
        inject_cfg = {"mcp_inject": "proxy_flag",
                      "mcp_proxy_flag_template": '-c mcp_servers.{server}.url="{url}"'}
        with tempfile.TemporaryDirectory() as td:
            args, _, _ = _apply_mcp_inject(
                inject_cfg, "codex", Path(td), "http://127.0.0.1:9999/mcp")
        r = validate_mcp_config_overrides(args, SERVER_NAME)
        self.assertTrue(r.ok, r.reason)

    def test_inv021_in_catalogue(self):
        self.assertIn("INV-021", INVARIANTS)


class TestDangerousBypassRejected(unittest.TestCase):
    """Dangerous sandbox bypass must never appear in defaults or allowlists."""

    def test_default_codex_exec_args_use_read_only_sandbox(self):
        from wrapper import _build_codex_exec_args
        from pathlib import Path
        args = _build_codex_exec_args({}, Path("."), "codex")
        self.assertIn("--sandbox", args)
        self.assertIn("read-only", args)

    def test_default_codex_exec_args_no_dangerous_bypass(self):
        from wrapper import _build_codex_exec_args
        from pathlib import Path
        args = _build_codex_exec_args({}, Path("."), "codex")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        for arg in args:
            self.assertNotIn("danger", arg.lower(),
                             f"dangerous flag found: {arg}")

    def test_dangerous_bypass_not_in_allowed_bool_flags(self):
        from safety_invariants import CODEX_EXEC_ALLOWED_BOOL_FLAGS
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         CODEX_EXEC_ALLOWED_BOOL_FLAGS)

    def test_dangerous_bypass_rejected_by_validator(self):
        from safety_invariants import validate_codex_exec_args
        r = validate_codex_exec_args(["--dangerously-bypass-approvals-and-sandbox"])
        self.assertFalse(r.ok)

    def test_danger_full_access_rejected_by_validator(self):
        from safety_invariants import validate_codex_exec_args
        r = validate_codex_exec_args(["--sandbox", "danger-full-access"])
        self.assertFalse(r.ok)

    def test_unsafe_marker_dangerously_still_detected(self):
        from safety_invariants import contains_unsafe_arg
        self.assertTrue(len(contains_unsafe_arg(
            ["--dangerously-bypass-approvals-and-sandbox"])) > 0)

    def test_unsafe_marker_bypass_still_detected(self):
        from safety_invariants import contains_unsafe_arg
        self.assertTrue(len(contains_unsafe_arg(["--bypass-safety"])) > 0)


class TestReplyChannelRouting(unittest.TestCase):
    """Direct-mention exec responses route to the source channel, not #general."""

    def test_work_item_channel_used_for_reply(self):
        item = {"prompt": "test", "relay_meta": None, "channel": "scratch-review"}
        relay_meta = item.get("relay_meta")
        reply_channel = (
            (relay_meta or {}).get("channel")
            or item.get("channel", "")
            or "general"
        )
        self.assertEqual(reply_channel, "scratch-review")

    def test_relay_meta_channel_takes_precedence(self):
        item = {"prompt": "test", "relay_meta": {"channel": "relay-ch"},
                "channel": "mention-ch"}
        relay_meta = item.get("relay_meta")
        reply_channel = (
            (relay_meta or {}).get("channel")
            or item.get("channel", "")
            or "general"
        )
        self.assertEqual(reply_channel, "relay-ch")

    def test_fallback_to_general_when_no_channel(self):
        item = {"prompt": "test", "relay_meta": None, "channel": ""}
        relay_meta = item.get("relay_meta")
        reply_channel = (
            (relay_meta or {}).get("channel")
            or item.get("channel", "")
            or "general"
        )
        self.assertEqual(reply_channel, "general")

    def test_queue_watcher_inject_passes_channel(self):
        from wrapper import _build_direct_mention_prompt
        prompt = _build_direct_mention_prompt("my-channel", "test")
        self.assertIn("#my-channel", prompt)


class TestCodexTextRelayOperationalModel(unittest.TestCase):
    """Codex Reviewer text-relay: sealed context in, text verdict out.

    No safe non-bypass codex exec MCP runtime path is currently proven for
    this project.  These tests pin the accepted operational model: relay
    turns provide all context as text, Codex responds as text, the wrapper
    routes the text to the source channel.  Direct MCP tool execution by
    Codex is not required.
    """

    def test_relay_prompt_forbids_mcp_tools(self):
        from session_relay import build_relay_prompt
        prompt = build_relay_prompt(
            session_name="s", goal="g", phase_name="p",
            phase_index=0, total_phases=1, role="reviewer",
            instruction="review this", agent_base="codex",
        )
        self.assertIn("Do not use MCP tools", prompt)
        self.assertIn("Do not call chat_read or chat_send", prompt)

    def test_relay_prompt_contains_all_context(self):
        from session_relay import build_relay_prompt
        prompt = build_relay_prompt(
            session_name="test-session", goal="verify text-relay",
            phase_name="Review", phase_index=0, total_phases=2,
            role="reviewer", instruction="check the code",
            context_messages=[{"sender": "claude", "text": "here is the diff"}],
            agent_base="codex",
        )
        self.assertIn("test-session", prompt)
        self.assertIn("verify text-relay", prompt)
        self.assertIn("Review", prompt)
        self.assertIn("reviewer", prompt)
        self.assertIn("check the code", prompt)
        self.assertIn("here is the diff", prompt)

    def test_mcp_stripped_for_relay_turns(self):
        from wrapper import _should_disable_mcp
        meta = {"relay_mode": True, "disable_mcp": True}
        self.assertTrue(_should_disable_mcp("sealed prompt text", meta))

    def test_native_mcp_config_still_emitted_for_compatibility(self):
        from safety_invariants import CODEX_MCP_AUTO_APPROVE_TOOLS
        self.assertEqual(CODEX_MCP_AUTO_APPROVE_TOOLS,
                         frozenset({"chat_read", "chat_send", "chat_propose_job"}))

    def test_codex_exec_defaults_enforce_read_only_sandbox(self):
        from wrapper import _build_codex_exec_args
        from pathlib import Path
        args = _build_codex_exec_args({}, Path("."), "codex")
        idx = args.index("--sandbox")
        self.assertEqual(args[idx + 1], "read-only")

    def test_codex_reviewer_is_relay_eligible(self):
        from session_relay import is_relay_eligible
        for agent in ("codex", "codex_reviewer", "codex_coordinator"):
            self.assertTrue(is_relay_eligible(agent), f"{agent} should be relay-eligible")

    def test_text_relay_does_not_require_mcp_execution(self):
        """The text-relay flow is: sealed prompt via stdin -> text output ->
        wrapper routes to channel.  No MCP tool call is part of this path."""
        from session_relay import build_relay_prompt
        from wrapper import _should_disable_mcp, _resolve_relay_reply
        prompt = build_relay_prompt(
            session_name="s", goal="g", phase_name="p",
            phase_index=0, total_phases=1, role="reviewer",
            instruction="review", agent_base="codex",
        )
        meta = {"relay_mode": True, "disable_mcp": True, "channel": "relay-ch"}
        self.assertTrue(_should_disable_mcp(prompt, meta))
        reply = _resolve_relay_reply(
            timed_out=False, errored=False, returncode=0,
            captured="LGTM — no issues found",
        )
        self.assertEqual(reply, "LGTM — no issues found")


if __name__ == "__main__":
    unittest.main()
