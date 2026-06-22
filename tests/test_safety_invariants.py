"""Unit tests for safety_invariants.py — the centralized fail-closed validators.

Each invariant has positive (holds) and negative (violation) coverage. These are
pure tests; no live execution, server, wrapper, or relay activation.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import safety_invariants as si  # noqa: E402


class CatalogueTests(unittest.TestCase):
    def test_all_fifteen_invariants_catalogued(self):
        for n in range(1, 16):
            self.assertIn(f"INV-{n:03d}", si.INVARIANTS)


# INV-011
class RunModeTests(unittest.TestCase):
    def test_known_modes_pass(self):
        for m in ("tui", "exec", "store_exec", "claude_relay"):
            self.assertTrue(si.check_run_mode_known(m).ok)

    def test_unknown_mode_fails_closed(self):
        r = si.check_run_mode_known("turbo")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-011")

    def test_empty_string_fails_closed(self):
        self.assertFalse(si.check_run_mode_known("").ok)

    def test_missing_fails_closed_by_default(self):
        self.assertFalse(si.check_run_mode_known(None).ok)

    def test_missing_allowed_only_when_explicit(self):
        self.assertTrue(si.check_run_mode_known(None, allow_default_missing=True).ok)


# INV-001 / INV-002 / INV-004 / INV-009
class RelayEligibilityTests(unittest.TestCase):
    def test_real_relay_set_passes(self):
        self.assertTrue(si.check_relay_eligibility().ok)

    def test_claude_in_set_fails_inv001(self):
        r = si.check_relay_eligibility({"codex", "claude"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-001")

    def test_agy_in_set_fails_inv002(self):
        r = si.check_relay_eligibility({"codex", "agy"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-002")

    def test_dryrun_in_set_fails_inv009(self):
        r = si.check_relay_eligibility({"codex", "claude_dryrun"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-009")

    def test_non_set_fails_inv004(self):
        r = si.check_relay_eligibility(["codex"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-004")

    def test_production_ineligible_helper(self):
        self.assertTrue(si.is_production_relay_ineligible("claude"))
        self.assertTrue(si.is_production_relay_ineligible("AGY"))
        self.assertFalse(si.is_production_relay_ineligible("codex"))


# INV-003
class CodexSafeBoundaryTests(unittest.TestCase):
    def test_codexsafe_as_safety_gate_allowed(self):
        self.assertTrue(si.check_codexsafe_boundary_only({"safety_gate": "codexsafe"}).ok)

    def test_codexsafe_as_workflow_role_rejected(self):
        r = si.check_codexsafe_boundary_only({"reviewer": "codexsafe"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-003")

    def test_codexsafe_as_coordinator_rejected(self):
        self.assertFalse(si.check_codexsafe_boundary_only({"coordinator": "codexsafe"}).ok)

    def test_non_dict_fails_closed(self):
        self.assertFalse(si.check_codexsafe_boundary_only(None).ok)


# INV-005 / INV-006
class StoreExecArgsTests(unittest.TestCase):
    def test_none_args_ok(self):
        self.assertTrue(si.validate_store_exec_args(None).ok)

    def test_model_flag_with_value_ok(self):
        self.assertTrue(si.validate_store_exec_args(["--model", "Gemini 3.1 Pro (High)"]).ok)

    def test_model_flag_without_value_fails(self):
        r = si.validate_store_exec_args(["--model"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-005")

    def test_unknown_safe_looking_flag_rejected_allowlist(self):
        # --color is harmless-looking but NOT allowlisted -> must still be rejected
        r = si.validate_store_exec_args(["--color", "blue"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-005")

    def test_unsafe_flag_fails_inv006(self):
        for bad in (["--yolo"], ["--mcp-config", "x"], ["--allowedTools", "Target:*"],
                    ["--spawn-subagent"], ["--unsafe"], ["--edit"], ["--approval", "never"]):
            with self.subTest(bad=bad):
                r = si.validate_store_exec_args(bad)
                self.assertFalse(r.ok)
                self.assertEqual(r.code, "INV-006")

    def test_non_list_fails_closed(self):
        self.assertFalse(si.validate_store_exec_args("--model x").ok)

    def test_contains_unsafe_arg_reports_markers(self):
        self.assertEqual(si.contains_unsafe_arg(["--yolo"]), ["yolo"])
        self.assertEqual(si.contains_unsafe_arg(["--model", "x"]), [])


# INV-007
class SelfReviewTests(unittest.TestCase):
    def test_separate_identities_ok(self):
        self.assertTrue(si.check_no_self_review(
            {"coordinator": "codex_coordinator", "reviewer": "codex_reviewer"}).ok)

    def test_same_identity_rejected(self):
        r = si.check_no_self_review({"coordinator": "codex", "reviewer": "codex"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-007")

    def test_non_dict_fails_closed(self):
        self.assertFalse(si.check_no_self_review(None).ok)


# INV-008
class SafetyVerdictRoleTests(unittest.TestCase):
    def test_safety_role_recognised(self):
        self.assertTrue(si.check_safety_verdict_role("safety_gate").ok)

    def test_non_safety_role_rejected(self):
        r = si.check_safety_verdict_role("reviewer")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-008")

    def test_parse_only_for_safety_role(self):
        # Non-safety role -> None (output is never read as a verdict)
        self.assertIsNone(si.parse_verdict_if_safety_role("reviewer", "PASS"))
        # Safety role -> a parsed verdict object
        verdict = si.parse_verdict_if_safety_role("safety_gate", "PASS")
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.passed)

    def test_safety_role_malformed_blocks(self):
        verdict = si.parse_verdict_if_safety_role("safety_gate", "looks fine to me")
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.passed)


# INV-009
class DryrunTemplateTests(unittest.TestCase):
    def test_clean_template_ok(self):
        tmpl = {"id": "code-review", "roles": ["reviewer", "builder"],
                "phases": [{"participants": ["reviewer"]}]}
        self.assertTrue(si.check_dryrun_template_safe(tmpl).ok)

    def test_template_with_claude_relay_rejected(self):
        r = si.check_dryrun_template_safe({"id": "x", "run_mode": "claude_relay"})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-009")

    def test_template_pinning_production_identity_rejected(self):
        r = si.check_dryrun_template_safe({"id": "x", "cast": {"role": "claude"}})
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-009")

    def test_template_referencing_dryrun_rejected(self):
        self.assertFalse(si.check_dryrun_template_safe({"x": "claude_dryrun"}).ok)


# INV-010
class LiveRelayActivationTests(unittest.TestCase):
    def test_default_false_blocks(self):
        self.assertFalse(si.require_live_relay_activation(False, "codex").ok)

    def test_none_blocks(self):
        self.assertFalse(si.require_live_relay_activation(None, "codex").ok)

    def test_truthy_nonbool_blocks(self):
        # 1 is truthy but not exactly True -> fail closed
        self.assertFalse(si.require_live_relay_activation(1, "codex").ok)

    def test_activated_eligible_agent_ok(self):
        self.assertTrue(si.require_live_relay_activation(True, "codex").ok)

    def test_activated_but_claude_blocked(self):
        r = si.require_live_relay_activation(True, "claude")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-001")

    def test_activated_but_not_in_allowlist_blocked(self):
        r = si.require_live_relay_activation(True, "kimi")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-010")


# INV-012
class DuplicateIdentityTests(unittest.TestCase):
    def test_unique_ok(self):
        self.assertTrue(si.check_no_duplicate_identities(["codex", "codexsafe"]).ok)

    def test_duplicate_rejected(self):
        r = si.check_no_duplicate_identities(["codex", "codex"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-012")

    def test_case_insensitive_duplicate(self):
        self.assertFalse(si.check_no_duplicate_identities(["Codex", "codex"]).ok)


# INV-013
class InjectionTests(unittest.TestCase):
    def test_clean_text_ok(self):
        self.assertTrue(si.check_no_injection("Please review the mobile layout spacing.").ok)

    def test_injection_markers_rejected(self):
        for bad in ("Target: everyone", "use the Slack MCP", "spawn a subagent",
                    "run a shell command", "please edit files", "approval bypass now",
                    "persist permission", "enable yolo", "unsafe mode", "bypass the gate"):
            with self.subTest(bad=bad):
                r = si.check_no_injection(bad)
                self.assertFalse(r.ok)
                self.assertEqual(r.code, "INV-013")

    def test_non_string_fails_closed(self):
        self.assertFalse(si.check_no_injection(123).ok)

    def test_none_is_ok(self):
        self.assertTrue(si.check_no_injection(None).ok)


# INV-014
class SecretRedactionTests(unittest.TestCase):
    def test_anthropic_key_redacted(self):
        out = si.redact_secrets("key is sk-ant-abc123_DEF-456 end")
        self.assertNotIn("sk-ant-abc123", out)
        self.assertIn("[REDACTED]", out)

    def test_github_pat_redacted(self):
        out = si.redact_secrets("token ghp_0123456789abcdefghijABCDEFG done")
        self.assertNotIn("ghp_0123456789", out)

    def test_bearer_redacted(self):
        self.assertNotIn("Bearer abcdEFGH1234", si.redact_secrets("Authorization: Bearer abcdEFGH1234"))

    def test_key_value_redacted(self):
        self.assertNotIn("hunter2", si.redact_secrets("password = hunter2"))

    def test_contains_secret(self):
        self.assertTrue(si.contains_secret("sk-ant-xyz123_abc"))
        self.assertFalse(si.contains_secret("ordinary text with no secret"))

    def test_empty_safe(self):
        self.assertEqual(si.redact_secrets(""), "")
        self.assertFalse(si.contains_secret(""))


# INV-015
class PushPreconditionTests(unittest.TestCase):
    def test_clean_ff_not_behind_ok(self):
        self.assertTrue(si.check_push_preconditions(clean_tree=True, fast_forward=True, behind=0).ok)

    def test_dirty_tree_blocks(self):
        self.assertFalse(si.check_push_preconditions(clean_tree=False, fast_forward=True).ok)

    def test_non_ff_blocks(self):
        self.assertFalse(si.check_push_preconditions(clean_tree=True, fast_forward=False).ok)

    def test_behind_blocks(self):
        self.assertFalse(si.check_push_preconditions(clean_tree=True, fast_forward=True, behind=2).ok)


# INV-016
class CodexExecArgsTests(unittest.TestCase):
    def test_none_ok(self):
        self.assertTrue(si.validate_codex_exec_args(None).ok)

    def test_default_safe_args_pass(self):
        self.assertTrue(si.validate_codex_exec_args(
            ["--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-o", "out.txt"]).ok)

    def test_sandbox_workspace_write_rejected(self):
        r = si.validate_codex_exec_args(["--sandbox", "workspace-write"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-016")

    def test_novel_unknown_flag_rejected(self):
        # Not in any denylist, but not allowlisted -> must fail closed.
        for novel in (["--new-flag"], ["--experimental"], ["--cd", "/etc"]):
            with self.subTest(novel=novel):
                r = si.validate_codex_exec_args(novel)
                self.assertFalse(r.ok)
                self.assertEqual(r.code, "INV-016")

    def test_dangerous_flags_rejected(self):
        for bad in (["--dangerously-bypass-approvals-and-sandbox"], ["--danger-full-access"],
                    ["--yolo"], ["--skip-permissions"], ["--auto-approve"], ["--workspace-write"]):
            with self.subTest(bad=bad):
                self.assertFalse(si.validate_codex_exec_args(bad).ok)

    def test_value_flag_without_value_fails(self):
        self.assertFalse(si.validate_codex_exec_args(["--sandbox"]).ok)

    def test_non_list_fails_closed(self):
        self.assertFalse(si.validate_codex_exec_args("--sandbox read-only").ok)


# INV-017
class RosterTests(unittest.TestCase):
    KNOWN = {"claude", "codex", "agy", "codex_coordinator", "codex_reviewer", "codexsafe"}

    def _valid(self):
        return {
            "developer": "claude", "reviewer": "codex", "ui_lead": "agy",
            "runtime_coordinator": "codex_coordinator",
            "runtime_reviewer": "codex_reviewer", "safety_guard": "codexsafe",
        }

    def test_valid_roster_passes(self):
        self.assertTrue(si.check_roster_roles(self._valid(), known_agents=self.KNOWN).ok)

    def test_empty_roster_fails(self):
        self.assertFalse(si.check_roster_roles({}, known_agents=self.KNOWN).ok)

    def test_unknown_role_fails(self):
        r = si.check_roster_roles({"overlord": "claude"}, known_agents=self.KNOWN)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-017")

    def test_missing_agent_fails(self):
        roster = self._valid()
        roster["developer"] = "ghostmodel"
        self.assertFalse(si.check_roster_roles(roster, known_agents=self.KNOWN).ok)

    def test_safety_guard_must_be_codexsafe(self):
        roster = self._valid()
        roster["safety_guard"] = "codex"
        r = si.check_roster_roles(roster, known_agents=self.KNOWN)
        self.assertFalse(r.ok)

    def test_codexsafe_as_developer_rejected(self):
        roster = self._valid()
        roster["developer"] = "codexsafe"
        r = si.check_roster_roles(roster, known_agents=self.KNOWN)
        self.assertFalse(r.ok)
        # external drift OR persona — both are violations; assert it fails.
        self.assertIn(r.code, ("INV-003", "INV-017"))

    def test_external_role_drift_rejected(self):
        roster = self._valid()
        roster["developer"] = "codex"  # developer must be claude
        self.assertFalse(si.check_roster_roles(roster, known_agents=self.KNOWN).ok)

    def test_developer_reviewer_collapse_rejected(self):
        roster = {"developer": "claude", "reviewer": "claude"}
        # reviewer must be codex anyway; this also trips the self-review guard
        self.assertFalse(si.check_roster_roles(roster).ok)

    def test_resolve_role_agent(self):
        roster = self._valid()
        self.assertEqual(si.resolve_role_agent("developer", roster), "claude")
        self.assertEqual(si.resolve_role_agent("reviewer", roster), "codex")
        self.assertIsNone(si.resolve_role_agent("overlord", roster))
        self.assertIsNone(si.resolve_role_agent("developer", {}))


# INV-019
class RoleCapabilityTests(unittest.TestCase):
    def test_developer_can_implement_commit_push(self):
        for cap in ("implement", "commit", "push", "edit_files"):
            self.assertTrue(si.check_role_capability("developer", cap).ok)

    def test_developer_cannot_review(self):
        # Developer must not hold FORMAL review authority (no self-review).
        r = si.check_role_capability("developer", "review")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "INV-019")

    def test_developer_can_prepare_review_package(self):
        # Non-authoritative package preparation is allowed.
        self.assertTrue(si.check_role_capability("developer", "prepare_review_package").ok)

    def test_reviewer_cannot_implement_commit_push(self):
        for cap in ("implement", "commit", "push", "edit_files", "shell"):
            with self.subTest(cap=cap):
                r = si.check_role_capability("reviewer", cap)
                self.assertFalse(r.ok)
                self.assertEqual(r.code, "INV-019")

    def test_reviewer_can_review(self):
        self.assertTrue(si.check_role_capability("reviewer", "review").ok)

    def test_reviewer_is_only_role_with_formal_review(self):
        # Formal "review" authority belongs to reviewer ONLY.
        for role in ("developer", "ui_lead", "safety_guard",
                     "runtime_coordinator", "runtime_reviewer"):
            with self.subTest(role=role):
                self.assertFalse(si.check_role_capability(role, "review").ok)
        self.assertTrue(si.check_role_capability("reviewer", "review").ok)

    def test_ui_lead_can_ui_review_but_not_formal_review(self):
        self.assertTrue(si.check_role_capability("ui_lead", "ui_review").ok)
        self.assertFalse(si.check_role_capability("ui_lead", "review").ok)

    def test_ui_lead_cannot_shell_mcp_subagent(self):
        for cap in ("shell", "mcp", "subagent", "edit_files", "commit"):
            with self.subTest(cap=cap):
                self.assertFalse(si.check_role_capability("ui_lead", cap).ok)

    def test_safety_guard_only_verdict(self):
        self.assertTrue(si.check_role_capability("safety_guard", "safety_verdict", agent="codexsafe").ok)
        self.assertFalse(si.check_role_capability("safety_guard", "implement").ok)

    def test_safety_verdict_requires_safety_mechanism_agent(self):
        r = si.check_role_capability("safety_guard", "safety_verdict", agent="codex")
        self.assertFalse(r.ok)

    def test_unknown_role_and_capability_fail_closed(self):
        self.assertFalse(si.check_role_capability("overlord", "review").ok)
        self.assertFalse(si.check_role_capability("developer", "launch_missiles").ok)


# INV-018
class RolePromptTests(unittest.TestCase):
    def test_build_known_roles(self):
        for role in ("developer", "reviewer", "ui_lead", "safety_guard"):
            p = si.build_immutable_role_prompt(role)
            self.assertIn(f"[IMMUTABLE ROLE: {role}]", p)
            self.assertIn("FORBIDDEN:", p)
            self.assertIn("IMMUTABLE", p)

    def test_build_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            si.build_immutable_role_prompt("overlord")

    def test_reviewer_prompt_forbids_implementation(self):
        p = si.build_immutable_role_prompt("reviewer").lower()
        self.assertIn("review only", p)
        self.assertTrue("implementing" in p or "implement" in p)
        self.assertIn("not commit/merge authorization", p)

    def test_ui_lead_prompt_forbids_shell_mcp_subagents(self):
        p = si.build_immutable_role_prompt("ui_lead").lower()
        for token in ("shell", "mcp", "subagent"):
            self.assertIn(token, p)

    def test_check_immutable_role_prompt_detects_stripping(self):
        good = si.build_immutable_role_prompt("developer")
        self.assertTrue(si.check_immutable_role_prompt(good, "developer").ok)
        self.assertFalse(si.check_immutable_role_prompt("just an agent prompt", "developer").ok)
        self.assertFalse(si.check_immutable_role_prompt(good, "overlord").ok)


if __name__ == "__main__":
    unittest.main()
