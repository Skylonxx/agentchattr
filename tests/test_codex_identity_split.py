"""Tests for the codex_coordinator / codex_reviewer identity split and the
same-package anti-self-review guard.

Covers (per AGENTCHATTR-CODEX-SPLIT-ANTI-SELF-REVIEW-IMPLEMENTATION):
  * config.toml defines codex_coordinator and codex_reviewer (Codex-equivalent)
  * MCP/default behaviour parity for the new Codex-derived keys
  * relay eligibility includes the split identities, excludes claude/agy/dry-run
  * anti-self-review guard (pure + via SessionEngine.start_session)
  * branch-contamination safeguards (no dry-run artifacts on main)
"""

import json
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wrapper import _resolve_mcp_inject, _BUILTIN_DEFAULTS  # noqa: E402
from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible  # noqa: E402
from session_engine import (  # noqa: E402
    SessionEngine,
    validate_no_self_review,
)


def _load_config_toml() -> dict:
    """Read the committed config.toml directly (no config.local.toml merge)."""
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# 1. Config identities
# ---------------------------------------------------------------------------

class ConfigIdentityTests(unittest.TestCase):
    def setUp(self):
        self.agents = _load_config_toml()["agents"]

    def test_codex_coordinator_defined_as_codex_exec(self):
        cfg = self.agents["codex_coordinator"]
        self.assertEqual(cfg["command"], "codex")
        self.assertEqual(cfg["run_mode"], "exec")

    def test_codex_reviewer_defined_as_codex_exec(self):
        cfg = self.agents["codex_reviewer"]
        self.assertEqual(cfg["command"], "codex")
        self.assertEqual(cfg["run_mode"], "exec")

    def test_coordinator_and_reviewer_are_distinct_keys(self):
        self.assertIn("codex_coordinator", self.agents)
        self.assertIn("codex_reviewer", self.agents)
        self.assertNotEqual("codex_coordinator", "codex_reviewer")

    def test_legacy_codex_still_present_and_unchanged(self):
        cfg = self.agents["codex"]
        self.assertEqual(cfg["command"], "codex")
        self.assertEqual(cfg["run_mode"], "exec")


# ---------------------------------------------------------------------------
# 2. MCP / default behaviour parity
# ---------------------------------------------------------------------------

class McpDefaultParityTests(unittest.TestCase):
    def test_codex_coordinator_inherits_codex_defaults(self):
        coord = _resolve_mcp_inject("codex_coordinator", {"command": "codex", "run_mode": "exec"})
        base = _resolve_mcp_inject("codex", {"command": "codex", "run_mode": "exec"})
        self.assertEqual(coord, base)
        self.assertEqual(coord.get("mcp_inject"), "proxy_flag")

    def test_codex_reviewer_inherits_codex_defaults(self):
        rev = _resolve_mcp_inject("codex_reviewer", {"command": "codex", "run_mode": "exec"})
        base = _resolve_mcp_inject("codex", {"command": "codex", "run_mode": "exec"})
        self.assertEqual(rev, base)
        self.assertEqual(rev.get("mcp_inject"), "proxy_flag")

    def test_legacy_codex_behaviour_not_broken(self):
        # Name-keyed lookup still wins for the base key, unchanged from before.
        self.assertEqual(
            _resolve_mcp_inject("codex", {"command": "codex"}),
            dict(_BUILTIN_DEFAULTS["codex"]),
        )

    def test_explicit_mcp_inject_still_overrides_command_fallback(self):
        explicit = _resolve_mcp_inject(
            "codex_coordinator",
            {"command": "codex", "mcp_inject": "flag", "mcp_flag": "--mcp-config"},
        )
        self.assertEqual(explicit["mcp_inject"], "flag")

    def test_other_providers_unaffected_by_command_fallback(self):
        # A name that matches a built-in default still resolves by name first.
        self.assertEqual(
            _resolve_mcp_inject("gemini", {"command": "gemini"}),
            dict(_BUILTIN_DEFAULTS["gemini"]),
        )

    def test_unknown_command_gets_no_inject(self):
        self.assertEqual(_resolve_mcp_inject("mystery", {"command": "mystery"}), {})


# ---------------------------------------------------------------------------
# 3. Relay eligibility
# ---------------------------------------------------------------------------

class RelayEligibilityTests(unittest.TestCase):
    def test_split_codex_identities_are_relay_eligible(self):
        self.assertTrue(is_relay_eligible("codex_coordinator"))
        self.assertTrue(is_relay_eligible("codex_reviewer"))

    def test_existing_codex_and_codexsafe_still_eligible(self):
        self.assertTrue(is_relay_eligible("codex"))
        self.assertTrue(is_relay_eligible("codexsafe"))

    def test_production_claude_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("claude"))
        self.assertNotIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_agy_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_branch_only_dryrun_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("claude_dryrun"))
        self.assertNotIn("claude_dryrun", RELAY_ELIGIBLE_AGENTS)


# ---------------------------------------------------------------------------
# 4. Anti-self-review guard (pure)
# ---------------------------------------------------------------------------

class AntiSelfReviewPureTests(unittest.TestCase):
    def test_separate_identities_allowed(self):
        res = validate_no_self_review({
            "coordinator": "codex_coordinator",
            "reviewer": "codex_reviewer",
        })
        self.assertTrue(res.ok)

    def test_same_identity_for_both_rejected(self):
        res = validate_no_self_review({
            "coordinator": "codex_x",
            "reviewer": "codex_x",
        })
        self.assertFalse(res.ok)
        self.assertEqual(res.identity, "codex_x")

    def test_legacy_codex_used_for_both_rejected(self):
        res = validate_no_self_review({
            "coordinator": "codex",
            "reviewer": "codex",
        })
        self.assertFalse(res.ok)

    def test_role_matching_is_case_insensitive(self):
        res = validate_no_self_review({
            "Coordinator": "codex",
            "Reviewer": "codex",
        })
        self.assertFalse(res.ok)

    def test_non_review_roles_with_same_identity_allowed(self):
        # builder + synthesiser sharing an identity is not a self-review collision
        res = validate_no_self_review({
            "builder": "codex",
            "synthesiser": "codex",
        })
        self.assertTrue(res.ok)

    def test_invalid_cast_type_fails_closed(self):
        self.assertFalse(validate_no_self_review(None).ok)
        self.assertFalse(validate_no_self_review([("coordinator", "x")]).ok)


# ---------------------------------------------------------------------------
# 4b. Anti-self-review via SessionEngine.start_session
# ---------------------------------------------------------------------------

class _FakeMessages:
    def on_message(self, cb):
        self._cb = cb


class _FakeStore:
    def __init__(self):
        self.created = []

    def get_active(self, channel):
        return None

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {"id": 1, "template_name": "t", "current_phase": 0,
                "current_turn": 0, "template_id": "t", **kwargs}

    def get_template(self, template_id):
        # Empty phases so _trigger_current returns immediately (no real trigger).
        return {"name": "t", "phases": []}

    def set_waiting(self, *a, **k):
        pass


class _FakeTrigger:
    def __init__(self):
        self.triggered = []

    def trigger_sync(self, agent, **kwargs):
        self.triggered.append(agent)


class AntiSelfReviewSessionTests(unittest.TestCase):
    def _engine(self):
        store = _FakeStore()
        trigger = _FakeTrigger()
        engine = SessionEngine(store, _FakeMessages(), trigger, registry=None)
        return engine, store, trigger

    def test_session_refused_when_same_identity_is_coordinator_and_reviewer(self):
        engine, store, _ = self._engine()
        result = engine.start_session(
            template_id="t", channel="general",
            cast={"coordinator": "codex", "reviewer": "codex"},
            started_by="ben",
        )
        self.assertIsNone(result)
        self.assertEqual(store.created, [])

    def test_session_allowed_when_identities_are_separate(self):
        engine, store, _ = self._engine()
        result = engine.start_session(
            template_id="t", channel="general",
            cast={"coordinator": "codex_coordinator", "reviewer": "codex_reviewer"},
            started_by="ben",
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(store.created), 1)


# ---------------------------------------------------------------------------
# 4c. Reviewer PASS is not commit authorization
# ---------------------------------------------------------------------------

class ReviewerVerdictScopeTests(unittest.TestCase):
    def test_session_engine_has_no_commit_authorization_logic(self):
        # A reviewer/safety verdict must never translate into commit/merge
        # authorization. The session engine deliberately contains no such logic;
        # this guards against scope creep adding it silently.
        src = (ROOT / "session_engine.py").read_text("utf-8").lower()
        for forbidden in ("git commit", "authorize_commit", "git merge",
                          "def commit", "git push"):
            self.assertNotIn(forbidden, src,
                             f"unexpected commit-auth token in session_engine.py: {forbidden!r}")


# ---------------------------------------------------------------------------
# 5. Branch-contamination safeguards (fail on main if dry-run artifacts appear)
# ---------------------------------------------------------------------------

class BranchContaminationTests(unittest.TestCase):
    def setUp(self):
        self.config = _load_config_toml()
        self.agents = self.config["agents"]

    def test_no_claude_dryrun_config_identity(self):
        self.assertNotIn("claude_dryrun", self.agents)

    def test_production_claude_has_no_claude_relay_run_mode(self):
        self.assertNotEqual(self.agents["claude"].get("run_mode"), "claude_relay")

    def test_no_agent_uses_claude_relay_run_mode(self):
        for name, cfg in self.agents.items():
            self.assertNotEqual(cfg.get("run_mode"), "claude_relay",
                                f"agent {name} unexpectedly uses claude_relay run_mode")

    def test_production_claude_not_relay_eligible(self):
        self.assertNotIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_agy_not_relay_eligible(self):
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_relay_eligibility_has_no_branch_only_dryrun(self):
        self.assertNotIn("claude_dryrun", RELAY_ELIGIBLE_AGENTS)

    def test_no_session_template_references_claude_relay_or_dryrun(self):
        templates_dir = ROOT / "session_templates"
        for path in templates_dir.glob("*.json"):
            text = path.read_text("utf-8")
            self.assertNotIn("claude_relay", text,
                             f"{path.name} references claude_relay")
            self.assertNotIn("claude_dryrun", text,
                             f"{path.name} references claude_dryrun")
            tmpl = json.loads(text)
            self.assertNotEqual(tmpl.get("id"), "claude-relay-dryrun",
                                f"{path.name} is a claude-relay-dryrun template")


if __name__ == "__main__":
    unittest.main()
