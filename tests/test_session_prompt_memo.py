"""Prompt memo workspace launcher tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config_loader
import session_memo as sm
import workspace_policy as wp
from session_engine import SessionEngine
from session_relay import build_relay_prompt, build_scoped_write_worker_prompt
from session_store import SessionStore
from tests.test_coordinator_loop_engine import COORDINATOR_LOOP_TEMPLATE, _ExtendedFakeRegistry
from tests.test_session_relay import _FakeAgentTrigger, _FakeMessageStore

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "752ed1317a5e0b83b872d563cda451c7621ed22e"

READ_ONLY_MEMO = """PROMPT ID: GEMINI-AGENTCHATTR-TWINPET-SCOPED-READ-ANALYSIS-AND-UI-09-C-BLUEPRINT-001
MODE: READ-ONLY
PROJECT: Twinpet POS
PHASE: UI-09-C Analysis
SUBJECT: PaymentModal analysis
Do not modify src or tests.
"""

SCOPED_WRITE_MEMO = """PROMPT ID: TWINPET-UI-09-C-PAYMENT-MODAL-SCOPED-WRITE-IMPLEMENTATION-001
MODE: scoped-write implementation
PROJECT: Twinpet POS
Implement PaymentModal polish only.
"""


class MemoParseTests(unittest.TestCase):
    def test_parse_headers(self):
        parsed = sm.parse_memo_headers(READ_ONLY_MEMO)
        self.assertEqual(
            parsed.prompt_id,
            "GEMINI-AGENTCHATTR-TWINPET-SCOPED-READ-ANALYSIS-AND-UI-09-C-BLUEPRINT-001",
        )
        self.assertEqual(parsed.headers.get("MODE"), "READ-ONLY")

    def test_slugify_channel(self):
        ch = sm.slugify_channel_from_prompt_id(
            "GEMINI-AGENTCHATTR-TWINPET-SCOPED-READ-ANALYSIS-AND-UI-09-C-BLUEPRINT-001",
        )
        self.assertTrue(ch)
        self.assertLessEqual(len(ch), 20)


class MemoSuggestionTests(unittest.TestCase):
    def setUp(self):
        cfg = config_loader.load_config(ROOT)
        self.profiles = config_loader.get_workspace_profiles(cfg)
        self.presets = config_loader.get_workspace_presets_enriched(cfg)

    def test_read_only_memo_suggests_analysis_profile(self):
        s = sm.suggest_from_memo(
            READ_ONLY_MEMO, profiles=self.profiles, presets=self.presets,
        )
        self.assertEqual(s.workspace_profile, "twinpet-ui-09-c-payment-modal-analysis")
        self.assertIn(s.workspace_mode, ("read-only-analysis", "read-only"))

    def test_scoped_write_memo_suggests_write_profile(self):
        s = sm.suggest_from_memo(
            SCOPED_WRITE_MEMO, profiles=self.profiles, presets=self.presets,
        )
        self.assertEqual(s.workspace_profile, "twinpet-ui-09-c-payment-modal-write")
        self.assertEqual(s.workspace_mode, "scoped-write")


class MemoSafetyTests(unittest.TestCase):
    def setUp(self):
        cfg = config_loader.load_config(ROOT)
        self.profiles = config_loader.get_workspace_profiles(cfg)

    def test_readonly_memo_blocks_scoped_write_mode(self):
        r = sm.validate_memo_start(
            READ_ONLY_MEMO,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-write",
                "workspace_mode": "scoped-write",
            },
            profiles=self.profiles,
            policy=None,
        )
        self.assertFalse(r.ok)
        self.assertTrue(any("READ-ONLY" in e for e in r.errors))

    def test_missing_profile_blocks_repo_memo(self):
        r = sm.validate_memo_start(
            READ_ONLY_MEMO,
            start_body={"workspace_mode": "read-only-analysis"},
            profiles=self.profiles,
            policy=None,
        )
        self.assertFalse(r.ok)

    def test_analysis_profile_resolves_docs_only(self):
        result = wp.resolve_session_workspace_policy(
            profiles=self.profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only-analysis",
                "expected_head": EXPECTED_HEAD,
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "read-only")
        self.assertEqual(result.policy.get("write_files") or [], [])


class PromptBodyPersistenceTests(unittest.TestCase):
    def test_long_prompt_body_persisted_separate_from_goal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates["t"] = {"id": "t", "name": "T", "roles": [], "phases": []}
        long_memo = "X" * 2000
        session = store.create(
            "t", "ch", {}, "user",
            goal="short goal",
            prompt_body=long_memo,
            prompt_id="TEST-001",
        )
        self.assertEqual(session["goal"], "short goal")
        self.assertEqual(len(session["prompt_body"]), 2000)
        self.assertEqual(session["prompt_id"], "TEST-001")

    def test_goal_only_backward_compatible(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates["t"] = {"id": "t", "name": "T", "roles": [], "phases": []}
        session = store.create("t", "ch", {}, "user", goal="legacy goal only")
        self.assertEqual(session["goal"], "legacy goal only")
        self.assertEqual(session.get("prompt_body"), "")


class PromptBodyRoutingTests(unittest.TestCase):
    def test_relay_prompt_includes_full_memo(self):
        memo = "FULL\n" * 100
        prompt = build_relay_prompt(
            session_name="S",
            goal="short",
            phase_name="Dev",
            phase_index=0,
            total_phases=1,
            role="developer",
            instruction="go",
            prompt_body=memo,
        )
        self.assertIn("FULL TASK MEMO (authoritative):", prompt)
        self.assertIn("FULL\nFULL", prompt)
        self.assertNotIn("GOAL: short", prompt)

    def test_scoped_prompt_includes_full_memo(self):
        fields = wp.build_session_workspace_policy_fields(
            wp.resolve_session_workspace_policy(
                profiles=config_loader.get_workspace_profiles(config_loader.load_config(ROOT)),
                start_body={
                    "workspace_profile": "twinpet-ui-09-c-payment-modal-write",
                    "workspace_mode": "scoped-write",
                    "expected_head": EXPECTED_HEAD,
                },
            ).policy,
        )
        prompt = build_scoped_write_worker_prompt(
            session_name="S",
            goal="short",
            role="developer",
            policy=fields["workspace_policy"],
            prompt_body="LONG MEMO BODY",
        )
        self.assertIn("LONG MEMO BODY", prompt)
        self.assertIn("WORKSPACE CONTRACT", prompt)

    def test_engine_passes_prompt_body_to_developer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates[COORDINATOR_LOOP_TEMPLATE["id"]] = COORDINATOR_LOOP_TEMPLATE
        trigger = _FakeAgentTrigger()
        agents = {
            "codex_coordinator": {"name": "codex_coordinator", "base": "codex_coordinator"},
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex_reviewer": {"name": "codex_reviewer", "base": "codex_reviewer"},
            "codexsafe": {"name": "codexsafe", "base": "codexsafe"},
        }
        engine = SessionEngine(
            store, _FakeMessageStore(), trigger, registry=_ExtendedFakeRegistry(agents),
        )
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        policy = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-write",
                "workspace_mode": "scoped-write",
                "expected_head": EXPECTED_HEAD,
            },
        )
        fields = wp.build_session_workspace_policy_fields(policy.policy)
        memo = SCOPED_WRITE_MEMO + "\n" + ("detail " * 200)
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"],
            "memo-test",
            {"coordinator": "codex_coordinator", "developer": "claude",
             "ui_lead": "agy", "reviewer": "codex_reviewer", "safety_gate": "codexsafe"},
            "user",
            goal="short summary",
            prompt_body=memo,
            workspace_policy=fields["workspace_policy"],
            workspace_policy_hash=fields["workspace_policy_hash"],
            workspace_policy_version=fields["workspace_policy_version"],
        )
        self.assertIsNotNone(session)
        persisted = store.get(session["id"])
        self.assertIn("PAYMENT-MODAL-SCOPED-WRITE", persisted["prompt_body"])
        s = persisted
        s["current_phase"] = 1
        s["current_turn"] = 0
        cls = dict(s.get("coordinator_loop_state") or {})
        cls["phase"] = "await_developer"
        cls["awaiting_role"] = "developer"
        s["coordinator_loop_state"] = cls
        store._sessions[0] = s
        trigger.triggered.clear()
        engine._trigger_coordinator_loop(s)
        dev = [t for t in trigger.triggered if t.get("agent") == "claude"]
        self.assertTrue(dev)
        entry = dev[-1].get("relay_entry") or dev[-1]
        self.assertIn("PAYMENT-MODAL-SCOPED-WRITE", entry.get("prompt", ""))
        self.assertIn("workspace_policy_context", entry)


class ConfigProfilePresenceTests(unittest.TestCase):
    def test_analysis_profile_in_config(self):
        cfg = config_loader.load_config(ROOT)
        profiles = config_loader.get_workspace_profiles(cfg)
        presets = config_loader.get_workspace_presets_enriched(cfg)
        self.assertIn("twinpet-ui-09-c-payment-modal-analysis", profiles)
        ids = [p["id"] for p in presets]
        self.assertIn("twinpet-ui-09-c-payment-modal-analysis", ids)
        self.assertIn("twinpet-ui-09-c-payment-modal-write", ids)


if __name__ == "__main__":
    unittest.main()
