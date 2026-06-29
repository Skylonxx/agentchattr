"""UI/API scoped-write session start tests (Twinpet UI-09-C preset path)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config_loader
import workspace_policy as wp
import workspace_policy_runtime as wpr
from session_relay import (
    build_scoped_write_worker_prompt,
    role_uses_headless_scoped_workspace,
    role_uses_scoped_workspace,
)

ROOT = Path(__file__).resolve().parents[1]
TWINPET = "C:/Users/Narachat/twinpet-pos"
EXPECTED_HEAD = "752ed1317a5e0b83b872d563cda451c7621ed22e"
TWINPET_WRITE_FILES = [
    "src/components/PaymentModal.tsx",
    "src/components/PaymentModal.css",
    "tests/pos-human-checkout.spec.ts",
    "Task.md",
    "Context.md",
    "docs/reports/latest-report.md",
]

TWINPET_UI_PAYLOAD = {
    "template_id": "project-readonly-coordinator-loop",
    "channel": "twinpet-ui-09-c-payment-modal-write",
    "workspace_profile": "twinpet-ui-09-c-payment-modal-write",
    "workspace_mode": "scoped-write",
    "expected_head": EXPECTED_HEAD,
    "cast": {
        "coordinator": "codex_coordinator",
        "developer": "claude",
        "ui_lead": "agy",
        "reviewer": "codex_reviewer",
        "safety_gate": "codexsafe",
    },
    "goal": "UI-09-C PaymentModal scoped-write implementation goal",
}

COORDINATOR_LOOP_TEMPLATE = {
    "id": "project-readonly-coordinator-loop",
    "name": "Project Read-Only Coordinator Loop",
    "coordinator_loop": True,
    "roles": ["coordinator", "developer", "ui_lead", "reviewer", "safety_gate"],
    "phases": [
        {"name": "Coordinator", "participants": ["coordinator"],
         "prompt": "Route.", "turn_order": "sequential"},
        {"name": "Developer", "participants": ["developer"],
         "prompt": "Implement.", "turn_order": "sequential"},
        {"name": "UI Lead", "participants": ["ui_lead"],
         "prompt": "Review UX.", "turn_order": "sequential"},
        {"name": "Reviewer", "participants": ["reviewer"],
         "prompt": "Review code.", "turn_order": "sequential"},
        {"name": "Safety Gate", "participants": ["safety_gate"],
         "prompt": "Safety gate.", "turn_order": "sequential", "is_output": True},
    ],
}


def _twinpet_policy_fields():
    profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
    result = wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": TWINPET_UI_PAYLOAD["workspace_profile"],
            "workspace_mode": TWINPET_UI_PAYLOAD["workspace_mode"],
            "expected_head": TWINPET_UI_PAYLOAD["expected_head"],
        },
    )
    assert result.ok, result.errors
    return wp.build_session_workspace_policy_fields(result.policy)


class WorkspacePresetConfigTests(unittest.TestCase):
    def test_workspace_presets_loaded_from_config(self):
        cfg = config_loader.load_config(ROOT)
        presets = config_loader.get_workspace_presets(cfg)
        self.assertIn("twinpet-ui-09-c-payment-modal-write", presets)
        preset = presets["twinpet-ui-09-c-payment-modal-write"]
        self.assertEqual(preset["template_id"], "project-readonly-coordinator-loop")
        self.assertEqual(preset["workspace_mode"], "scoped-write")
        self.assertEqual(preset["expected_head"], EXPECTED_HEAD)

    def test_workspace_presets_enriched_includes_write_files(self):
        enriched = config_loader.get_workspace_presets_enriched(
            config_loader.load_config(ROOT),
        )
        twinpet = next(p for p in enriched if p["id"] == "twinpet-ui-09-c-payment-modal-write")
        self.assertEqual(twinpet["workspace_root"], TWINPET)
        self.assertEqual(twinpet["write_files"], TWINPET_WRITE_FILES)
        self.assertEqual(twinpet["cast"]["developer"], "claude")


class ScopedWritePolicyTests(unittest.TestCase):
    def test_ui_payload_maps_scoped_write_to_implementation(self):
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": TWINPET_UI_PAYLOAD["workspace_profile"],
                "workspace_mode": TWINPET_UI_PAYLOAD["workspace_mode"],
                "expected_head": TWINPET_UI_PAYLOAD["expected_head"],
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "implementation")
        self.assertEqual(result.policy["write_files"], TWINPET_WRITE_FILES)

    def test_template_only_start_stays_scratch_readonly(self):
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={"template_id": "project-readonly-coordinator-loop"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.policy["mode"], "scratch-readonly")
        dev = wp.role_permission_for(result.policy, "developer")
        self.assertEqual(dev["filesystem"], "none")

    def test_forbidden_file_blocked_in_runtime(self):
        fields = _twinpet_policy_fields()
        policy = fields["workspace_policy"]
        result = wpr.verify_dirty_set(
            porcelain_output=" M src/pages/POSPage.tsx\n",
            policy=policy,
        )
        self.assertFalse(result.ok)

    def test_git_write_commands_blocked(self):
        fields = _twinpet_policy_fields()
        policy = fields["workspace_policy"]
        for cmd in ("git add file.txt", "git commit -m x", "git push"):
            guard = wpr.check_command_guard(cmd, policy=policy)
            self.assertFalse(guard.ok, cmd)

    def test_developer_cwd_resolves_to_twinpet(self):
        if not Path(TWINPET).is_dir():
            self.skipTest("Twinpet path not present on this machine")
        fields = _twinpet_policy_fields()
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        cwd = wpr.resolve_role_cwd(
            fields["workspace_policy"],
            "developer",
            enforcement_enabled=True,
            profiles=profiles,
        )
        self.assertEqual(Path(cwd), Path(TWINPET).resolve())


class ScopedWriteRelayTests(unittest.TestCase):
    def test_role_uses_headless_scoped_workspace_for_developer(self):
        fields = _twinpet_policy_fields()
        session = {"workspace_policy": fields["workspace_policy"]}
        self.assertTrue(role_uses_scoped_workspace(session, "developer"))
        self.assertTrue(role_uses_headless_scoped_workspace(session, "developer"))
        self.assertTrue(role_uses_headless_scoped_workspace(session, "ui_lead"))
        self.assertFalse(role_uses_headless_scoped_workspace(session, "coordinator"))
        self.assertFalse(role_uses_headless_scoped_workspace(session, "reviewer"))

    def test_scoped_prompt_includes_cwd_allowlist_and_preflight(self):
        fields = _twinpet_policy_fields()
        prompt = build_scoped_write_worker_prompt(
            session_name="Twinpet UI-09-C",
            goal="Polish PaymentModal",
            role="developer",
            policy=fields["workspace_policy"],
        )
        self.assertIn(TWINPET, prompt)
        self.assertIn("pwd", prompt)
        self.assertIn("git status --short", prompt)
        self.assertIn("git rev-parse HEAD", prompt)
        self.assertIn("src/components/PaymentModal.tsx", prompt)
        self.assertNotIn("Do not run shell commands. Do not edit files.", prompt)

    def test_coordinator_relay_prompt_stays_read_only_without_policy(self):
        from session_relay import build_relay_prompt

        prompt = build_relay_prompt(
            session_name="Test",
            goal="g",
            phase_name="Developer",
            phase_index=1,
            total_phases=5,
            role="developer",
            instruction="Implement.",
        )
        self.assertIn("Do not run shell commands", prompt)
        self.assertIn("Do not edit files", prompt)


class ScopedWriteEngineTests(unittest.TestCase):
    def _make_engine(self):
        from session_engine import SessionEngine
        from session_store import SessionStore
        from tests.test_coordinator_loop_engine import _ExtendedFakeRegistry
        from tests.test_session_relay import _FakeAgentTrigger, _FakeMessageStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates[COORDINATOR_LOOP_TEMPLATE["id"]] = COORDINATOR_LOOP_TEMPLATE
        messages = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        agents_map = {
            "codex_coordinator": {
                "name": "codex_coordinator", "base": "codex_coordinator",
            },
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex_reviewer": {"name": "codex_reviewer", "base": "codex_reviewer"},
            "codexsafe": {"name": "codexsafe", "base": "codexsafe"},
        }
        registry = _ExtendedFakeRegistry(agents_map)
        engine = SessionEngine(store, messages, trigger, registry=registry)
        return engine, store, trigger

    def _trigger_developer_turn(self, engine, store, trigger, session):
        sid = session["id"]
        s = store.get(sid)
        s["current_phase"] = 1
        s["current_turn"] = 0
        cls = dict(s.get("coordinator_loop_state") or {})
        cls["phase"] = "await_developer"
        cls["awaiting_role"] = "developer"
        s["coordinator_loop_state"] = cls
        store._sessions[0] = s
        trigger.triggered.clear()
        engine._trigger_coordinator_loop(s)
        return store.get(sid)

    def test_scoped_session_persists_workspace_policy(self):
        engine, store, _ = self._make_engine()
        fields = _twinpet_policy_fields()
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"],
            TWINPET_UI_PAYLOAD["channel"],
            TWINPET_UI_PAYLOAD["cast"],
            "user",
            goal=TWINPET_UI_PAYLOAD["goal"],
            workspace_policy=fields["workspace_policy"],
            workspace_policy_hash=fields["workspace_policy_hash"],
            workspace_policy_version=fields["workspace_policy_version"],
        )
        self.assertIsNotNone(session)
        persisted = store.get(session["id"])
        policy = persisted["workspace_policy"]
        self.assertEqual(policy["mode"], "implementation")
        self.assertEqual(policy["policy_id"], "twinpet-ui-09-c-payment-modal-write")
        self.assertEqual(policy["write_files"], TWINPET_WRITE_FILES)
        self.assertEqual(
            policy["workspace"]["expected_head"],
            EXPECTED_HEAD,
        )

    def test_scoped_developer_uses_headless_prompt_not_relay(self):
        engine, store, trigger = self._make_engine()
        fields = _twinpet_policy_fields()
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"],
            "twinpet-scoped-test",
            TWINPET_UI_PAYLOAD["cast"],
            "user",
            goal=TWINPET_UI_PAYLOAD["goal"],
            workspace_policy=fields["workspace_policy"],
            workspace_policy_hash=fields["workspace_policy_hash"],
            workspace_policy_version=fields["workspace_policy_version"],
        )
        self._trigger_developer_turn(engine, store, trigger, session)
        dev_triggers = [t for t in trigger.triggered if t.get("agent") == "claude"]
        self.assertTrue(dev_triggers)
        last = dev_triggers[-1]
        self.assertNotIn("relay_entry", last)
        prompt = last.get("prompt", "")
        self.assertIn("WORKSPACE CONTRACT (scoped-write", prompt)
        self.assertIn(TWINPET, prompt)
        self.assertIn("src/components/PaymentModal.tsx", prompt)

    def test_template_only_session_developer_gets_relay_not_scoped(self):
        engine, store, trigger = self._make_engine()
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"],
            "readonly-test",
            TWINPET_UI_PAYLOAD["cast"],
            "user",
            goal="Read-only review",
        )
        self._trigger_developer_turn(engine, store, trigger, session)
        dev_triggers = [t for t in trigger.triggered if t.get("agent") == "claude"]
        self.assertTrue(dev_triggers)
        last = dev_triggers[-1]
        self.assertIn("relay_entry", last)
        relay_prompt = last["relay_entry"]["prompt"]
        self.assertIn("Do not run shell commands", relay_prompt)


class UiPayloadShapeTests(unittest.TestCase):
    """Prove the UI-generated JSON body matches server expectations."""

    def test_ui_payload_fields_complete(self):
        body = dict(TWINPET_UI_PAYLOAD)
        required = (
            "template_id", "channel", "workspace_profile", "workspace_mode",
            "expected_head", "cast", "goal",
        )
        for key in required:
            self.assertIn(key, body)
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        result = wp.resolve_session_workspace_policy(profiles=profiles, start_body=body)
        self.assertTrue(result.ok, result.errors)


if __name__ == "__main__":
    unittest.main()
