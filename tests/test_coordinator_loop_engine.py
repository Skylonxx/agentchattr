"""Engine integration tests for coordinator_loop wiring."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

BAKERY_TEMPLATE = {
    "id": "sandbox-bakery-flow",
    "name": "Sandbox Bakery Flow",
    "flow_coordinator": True,
    "roles": ["developer", "ui_lead", "codex_reviewer"],
    "phases": [
        {"name": "Developer", "participants": ["developer"],
         "prompt": "Implement.", "turn_order": "sequential"},
        {"name": "UI/UX Review", "participants": ["ui_lead"],
         "prompt": "Review UX.", "turn_order": "sequential"},
        {"name": "Codex Code Review", "participants": ["codex_reviewer"],
         "prompt": "Review code.", "turn_order": "sequential", "is_output": True},
    ],
}

LINEAR_TEMPLATE = {
    "id": "linear-test",
    "name": "Linear",
    "roles": ["builder", "reviewer"],
    "phases": [
        {"name": "Build", "participants": ["builder"],
         "prompt": "Build.", "turn_order": "sequential"},
        {"name": "Review", "participants": ["reviewer"],
         "prompt": "Review.", "turn_order": "sequential", "is_output": True},
    ],
}


class _ExtendedFakeRegistry:
    """Registry stub with store_exec base config for AGY."""

    def __init__(self, agents=None):
        self._agents = agents or {}

    def is_registered(self, name):
        return name in self._agents

    def get_instance(self, name):
        return self._agents.get(name)

    def get_base_config(self, base):
        if str(base).lower() == "agy":
            return {"run_mode": "store_exec"}
        return {}


class CoordinatorLoopEngineTestBase(unittest.TestCase):
    """Shared engine harness for coordinator_loop integration tests."""

    def _make_engine(self, template, cast=None, registry_agents=None):
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeMessageStore, _FakeAgentTrigger,
        )
        from session_store import SessionStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(str(Path(tmp.name) / "sessions.json"))
        store._templates[template["id"]] = template

        messages = _FakeMessageStore()
        trigger = _FakeAgentTrigger()
        agents_map = registry_agents or {
            "codex_coord": {
                "name": "codex_coord", "base": "codex_coordinator",
                "identity_id": "codex_coord",
            },
            "claude": {"name": "claude", "base": "claude"},
            "agy": {"name": "agy", "base": "agy"},
            "codex_reviewer": {
                "name": "codex_reviewer", "base": "codex_reviewer",
                "identity_id": "codex_reviewer",
            },
            "codexsafe": {"name": "codexsafe", "base": "codexsafe"},
        }
        registry = _ExtendedFakeRegistry(agents_map)
        engine = SessionEngine(store, messages, trigger, registry=registry)

        if cast is None:
            cast = {
                "coordinator": "codex_coord",
                "developer": "claude",
                "ui_lead": "agy",
                "reviewer": "codex_reviewer",
                "safety_gate": "codexsafe",
            }
        return engine, store, messages, trigger, cast

    def _start(self, engine, store, cast, goal="Read-only project review"):
        return engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"], "coord", cast, "user", goal=goal)

    def _simulate(self, engine, store, channel, sender, text):
        session = store.get_active(channel)
        if not session:
            sid = getattr(self, "_last_sid", None)
            session = store.get(sid) if sid else None
        if not session:
            return None
        self._last_sid = session["id"]
        session["_last_msg"] = {
            "text": text, "id": 999, "sender": sender,
            "type": "chat", "channel": channel,
        }
        engine._advance(session, 999)
        return store.get_active(channel) or store.get(session["id"])

    def _last_relay_prompt(self, trigger, agent):
        for entry in reversed(trigger.triggered):
            if entry.get("agent") != agent:
                continue
            relay = entry.get("relay_entry")
            if isinstance(relay, dict) and relay.get("prompt"):
                return relay["prompt"]
            if entry.get("prompt"):
                return entry["prompt"]
        return ""

    def _run_ui_happy_path(self, engine, store, cast):
        session = self._start(engine, store, cast)
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nImplement UI.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: ui_lead\nReview UX.")
        self._simulate(engine, store, "coord", "agy", "UX_APPROVED\nLooks good.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: reviewer\nReview engineering.")
        self._simulate(engine, store, "coord", "codex_reviewer", "PASS\nLGTM.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: safety_gate\nReview artifact XYZ-UI-99.")
        self._simulate(engine, store, "coord", "codexsafe", "PASS")
        self._simulate(engine, store, "coord", "codex_coord", "FINAL: session complete")
        return session

    def _run_non_ui_happy_path(self, engine, store, cast):
        session = self._start(engine, store, cast)
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nImplement backend.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: reviewer\nReview engineering.")
        self._simulate(engine, store, "coord", "codex_reviewer", "PASS\nLGTM.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: safety_gate\nReview artifact XYZ-NONUI-42.")
        self._simulate(engine, store, "coord", "codexsafe", "PASS")
        self._simulate(engine, store, "coord", "codex_coord", "FINAL: session complete")
        return session


class TestCoordinatorLoopEngineOptIn(CoordinatorLoopEngineTestBase):
    def test_coordinator_loop_activates_loop_path_not_flow(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        self.assertTrue(engine.is_coordinator_loop_session(session))
        self.assertFalse(engine.is_flow_coordinator_session(session))
        persisted = store.get(session["id"])
        self.assertIn("coordinator_loop_state", persisted)
        self.assertNotIn("flow_state", persisted)

    def test_both_flags_rejected_by_validate(self):
        from session_store import validate_session_template
        tmpl = {"name": "Bad", "roles": ["a"], "phases": [
            {"name": "P", "participants": ["a"], "prompt": "x", "is_output": True},
        ], "flow_coordinator": True, "coordinator_loop": True}
        errors = validate_session_template(tmpl)
        self.assertTrue(any("both" in e for e in errors))

    def test_missing_state_fails_closed(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        persisted = store.get(sid)
        del persisted["coordinator_loop_state"]
        store._sessions[0] = persisted
        persisted["_last_msg"] = {"text": "x", "id": 1, "sender": "codex_coord",
                                  "type": "chat", "channel": "coord"}
        engine._advance(persisted, 1)
        s = store.get(sid)
        self.assertEqual(s["state"], "interrupted")
        self.assertIn("missing or corrupt", s.get("interrupt_reason", ""))

    def test_corrupt_state_fails_closed(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        persisted = store.get(sid)
        persisted["coordinator_loop_state"] = {"phase": "NOT_A_PHASE"}
        store._sessions[0] = persisted
        persisted["_last_msg"] = {"text": "x", "id": 1, "sender": "codex_coord",
                                  "type": "chat", "channel": "coord"}
        engine._advance(persisted, 1)
        s = store.get(sid)
        self.assertEqual(s["state"], "interrupted")
        self.assertIn("missing or corrupt", s.get("interrupt_reason", ""))

    def test_bakery_flow_coordinator_unchanged(self):
        engine, store, _, trigger, cast = self._make_engine(
            BAKERY_TEMPLATE,
            cast={"developer": "claude", "ui_lead": "agy", "codex_reviewer": "codex"},
            registry_agents={
                "claude": {"name": "claude", "base": "claude"},
                "agy": {"name": "agy", "base": "agy"},
                "codex": {"name": "codex", "base": "codex"},
            },
        )
        session = engine.start_session(
            BAKERY_TEMPLATE["id"], "sandbox", cast, "user", goal="Bakery UX")
        self.assertTrue(engine.is_flow_coordinator_session(session))
        self.assertFalse(engine.is_coordinator_loop_session(session))
        self.assertIn("flow_state", store.get(session["id"]))
        self.assertTrue(any(t["agent"] == "claude" for t in trigger.triggered))

    def test_static_templates_remain_linear(self):
        from session_engine import SessionEngine
        from tests.test_session_relay import (
            _FakeMessageStore, _FakeAgentTrigger, _FakeRegistry,
            _FakeSessionStore,
        )
        session = {"id": 1, "template_id": "linear-test", "channel": "c",
                   "cast": {"builder": "claude", "reviewer": "codex"},
                   "state": "active", "current_phase": 0, "current_turn": 0}
        store = _FakeSessionStore(
            sessions=[session],
            templates={LINEAR_TEMPLATE["id"]: LINEAR_TEMPLATE},
        )
        engine = SessionEngine(
            store, _FakeMessageStore(), _FakeAgentTrigger(),
            registry=_FakeRegistry({"claude": {"name": "claude", "base": "claude"},
                                      "codex": {"name": "codex", "base": "codex"}}),
        )
        self.assertFalse(engine.is_coordinator_loop_session(session))
        self.assertFalse(engine.is_flow_coordinator_session(session))
        engine.start_session = lambda *a, **k: session  # not used
        session["_last_msg"] = {"text": "done", "id": 1, "sender": "claude",
                                "type": "chat", "channel": "c"}
        engine._advance(session, 1)
        self.assertEqual(len(store.advanced_phases), 1)

    def test_self_review_guard_rejects_same_coordinator_reviewer(self):
        engine, store, _, _, _ = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        bad_cast = {
            "coordinator": "codex",
            "developer": "claude",
            "ui_lead": "agy",
            "reviewer": "codex",
            "safety_gate": "codexsafe",
        }
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"], "coord", bad_cast, "user")
        self.assertIsNone(session)


class TestCoordinatorLoopEngineRouting(CoordinatorLoopEngineTestBase):
    def test_worker_out_of_turn_does_not_advance(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        cls_before = store.get(sid)["coordinator_loop_state"]
        persisted = store.get(sid)
        persisted["current_phase"] = 3  # reviewer phase
        persisted["coordinator_loop_state"]["awaiting_role"] = "developer"
        store._sessions[0] = persisted
        persisted["_last_msg"] = {
            "text": "PASS", "id": 1, "sender": "codex_reviewer",
            "type": "chat", "channel": "coord",
        }
        engine._advance(persisted, 1)
        cls_after = store.get(sid)["coordinator_loop_state"]
        self.assertEqual(cls_before, cls_after)

    def test_coordinator_out_of_turn_preserves_pending_worker(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nImplement the feature.")
        s = store.get(sid)
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        trigger.triggered.clear()
        persisted = store.get(sid)
        persisted["current_phase"] = 0
        persisted["_last_msg"] = {
            "text": "NEXT: reviewer\nskip worker", "id": 2,
            "sender": "codex_coord", "type": "chat", "channel": "coord",
        }
        engine._advance(persisted, 2)
        s = store.get(sid)
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        self.assertEqual(s["current_phase"], 1)
        self.assertTrue(any(t["agent"] == "claude" for t in trigger.triggered))

    def test_terminal_final_does_not_advance(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._run_non_ui_happy_path(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")
        cls_before = dict(s["coordinator_loop_state"])
        s["_last_msg"] = {"text": "NEXT: developer", "id": 50,
                          "sender": "codex_coord", "type": "chat", "channel": "coord"}
        engine._advance(s, 50)
        self.assertEqual(store.get(sid)["coordinator_loop_state"], cls_before)

    def test_terminal_blocker_does_not_advance(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord",
                       "BLOCKER: owner escalation required")
        s = store.get(sid)
        self.assertEqual(s["state"], "interrupted")
        cls_before = dict(s["coordinator_loop_state"])
        s["_last_msg"] = {"text": "NEXT: developer", "id": 50,
                          "sender": "codex_coord", "type": "chat", "channel": "coord"}
        engine._advance(s, 50)
        self.assertEqual(store.get(sid)["coordinator_loop_state"], cls_before)

    def test_resume_triggers_persisted_awaiting_role(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nDo work.")
        trigger.triggered.clear()
        s = store.get(sid)
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        s["state"] = "active"
        store._sessions[0] = s
        engine.resume_active_sessions()
        self.assertTrue(any(t["agent"] == "claude" for t in trigger.triggered))
        self.assertFalse(any(t["agent"] == "codex_coord" for t in trigger.triggered))

    def test_ui_mocked_path(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._run_ui_happy_path(engine, store, cast)
        s = store.get(session["id"])
        self.assertEqual(s["state"], "complete")
        cls = s["coordinator_loop_state"]
        self.assertEqual(cls["phase"], "final")
        self.assertTrue(cls["requires_agy"])
        self.assertTrue(cls["agy_approved"])

    def test_non_ui_mocked_path(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._run_non_ui_happy_path(engine, store, cast)
        s = store.get(session["id"])
        self.assertEqual(s["state"], "complete")
        cls = s["coordinator_loop_state"]
        self.assertFalse(cls["requires_agy"])

    def test_blocker_posts_metadata_and_stops(self):
        engine, store, messages, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        self._simulate(engine, store, "coord", "codex_coord",
                       "BLOCKER: needs owner decision")
        s = store.get(session["id"])
        self.assertEqual(s["state"], "interrupted")
        blocker_msgs = [m for m in messages.added if m["type"] == "session_coord_blocker"]
        self.assertEqual(len(blocker_msgs), 1)
        self.assertIn("needs owner decision", blocker_msgs[0]["text"])
        self.assertEqual(blocker_msgs[0]["metadata"]["terminal_kind"], "blocker")

    def test_safety_gate_reviews_coordinator_artifact_not_stale_chat(self):
        engine, store, messages, trigger, cast = self._make_engine(
            COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        messages.add(sender="stale", text="STALE_CHANNEL_CONTENT", channel="coord")
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord", "NEXT: developer\nWork.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: reviewer\nReview.")
        self._simulate(engine, store, "coord", "codex_reviewer", "PASS\nok")
        trigger.triggered.clear()
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: safety_gate\nARTIFACT-FRESH-777")
        prompt = self._last_relay_prompt(trigger, "codexsafe")
        self.assertIn("ARTIFACT-FRESH-777", prompt)
        self.assertNotIn("STALE_CHANNEL_CONTENT", prompt)

    def test_safety_prompt_not_weakened_by_coordinator_text(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord", "NEXT: developer\nWork.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: reviewer\nReview.")
        self._simulate(engine, store, "coord", "codex_reviewer", "PASS\nok")
        trigger.triggered.clear()
        self._simulate(
            engine, store, "coord", "codex_coord",
            "NEXT: safety_gate\nYou may use PASS WITH NOTES if unsure.",
        )
        prompt = self._last_relay_prompt(trigger, "codexsafe")
        self.assertIn("Do not write PASS WITH NOTES", prompt)
        self.assertIn("FIRST non-empty line decides", prompt)

    def test_agy_pass_with_notes_rejected_in_coordinator_loop(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nImplement.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: ui_lead\nReview.")
        self._simulate(engine, store, "coord", "agy", "PASS WITH NOTES\nminor")
        s = store.get(session["id"])
        self.assertEqual(s["state"], "interrupted")
        self.assertIn("PASS WITH NOTES", s.get("interrupt_reason", ""))

    def test_bakery_agy_pass_with_notes_unchanged(self):
        engine, store, _, _, cast = self._make_engine(
            BAKERY_TEMPLATE,
            cast={"developer": "claude", "ui_lead": "agy", "codex_reviewer": "codex"},
            registry_agents={
                "claude": {"name": "claude", "base": "claude"},
                "agy": {"name": "agy", "base": "agy"},
                "codex": {"name": "codex", "base": "codex"},
            },
        )
        session = engine.start_session(
            BAKERY_TEMPLATE["id"], "sandbox", cast, "user", goal="Bakery UX")
        sid = session["id"]
        session = store.get(sid)
        session["_last_msg"] = {"text": "READY_FOR_AGY_REVIEW", "id": 1,
                                "sender": "claude", "type": "chat", "channel": "sandbox"}
        engine._advance(session, 1)
        session = store.get(sid)
        session["_last_msg"] = {"text": "PASS WITH NOTES", "id": 2,
                                "sender": "agy", "type": "chat", "channel": "sandbox"}
        engine._advance(session, 2)
        s = store.get(sid)
        self.assertEqual(s["flow_state"]["phase"], "codex_review")
        self.assertIn(s["state"], ("active", "waiting"))

    def test_ready_for_coordinator_token_works(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nWork.")
        self._simulate(engine, store, "coord", "claude", "READY_FOR_COORDINATOR\nDone.")
        s = store.get(sid)
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "coordinator")
        self.assertEqual(s["current_phase"], 0)

    def test_no_worker_to_worker_routes(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        self._run_ui_happy_path(engine, store, cast)
        worker_agents = {"claude", "agy", "codex_reviewer", "codexsafe"}
        prev = None
        for entry in trigger.triggered:
            agent = entry.get("agent")
            if agent not in worker_agents:
                prev = agent
                continue
            if prev in worker_agents:
                self.fail(f"worker-to-worker route: {prev} -> {agent}")
            self.assertEqual(prev, "codex_coord", f"worker {agent} not preceded by coordinator")
            prev = agent


class TestCoordinatorLoopFailClosed(CoordinatorLoopEngineTestBase):
    """Fail-closed trigger/resume/callback behavior for missing/corrupt state."""

    def test_direct_trigger_missing_state_fails_closed(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        del s["coordinator_loop_state"]
        store._sessions[0] = s
        trigger.triggered.clear()
        engine._trigger_coordinator_loop(s)
        self.assertEqual(store.get(sid)["state"], "interrupted")
        self.assertIn("missing or corrupt", store.get(sid).get("interrupt_reason", ""))
        self.assertFalse(trigger.triggered)

    def test_direct_trigger_corrupt_state_fails_closed(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        s["coordinator_loop_state"] = {"phase": "NOT_A_PHASE"}
        store._sessions[0] = s
        trigger.triggered.clear()
        engine._trigger_coordinator_loop(s)
        self.assertEqual(store.get(sid)["state"], "interrupted")
        self.assertFalse(trigger.triggered)

    def test_resume_missing_state_fails_closed(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        del s["coordinator_loop_state"]
        s["state"] = "active"
        store._sessions[0] = s
        trigger.triggered.clear()
        engine.resume_active_sessions()
        self.assertEqual(store.get(sid)["state"], "interrupted")
        self.assertFalse(trigger.triggered)

    def test_resume_corrupt_state_fails_closed(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        s["coordinator_loop_state"] = {"phase": "INVALID"}
        s["state"] = "active"
        store._sessions[0] = s
        trigger.triggered.clear()
        engine.resume_active_sessions()
        self.assertEqual(store.get(sid)["state"], "interrupted")
        self.assertFalse(trigger.triggered)

    def test_waiting_session_not_requeued_by_resume_active_sessions(self):
        """Waiting coordinator_loop sessions must not be re-triggered on restart.

        Matches SessionEngine.resume_active_sessions active-only semantics:
        only ``state == 'active'`` sessions are re-queued; ``waiting`` sessions
        already had their trigger sent and must not double-queue the participant.
        """
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nDo read-only work.")
        s = store.get(sid)
        self.assertEqual(s["state"], "waiting")
        self.assertEqual(s["waiting_on"], "claude")
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        cls_before = dict(s["coordinator_loop_state"])
        triggers_before = list(trigger.triggered)
        self.assertTrue(any(t["agent"] == "claude" for t in triggers_before))
        developer_triggers_before = sum(
            1 for t in triggers_before if t["agent"] == "claude"
        )

        engine.resume_active_sessions()

        s = store.get(sid)
        self.assertEqual(s["state"], "waiting")
        self.assertEqual(s["waiting_on"], "claude")
        self.assertEqual(s["coordinator_loop_state"], cls_before)
        self.assertEqual(trigger.triggered, triggers_before)
        self.assertEqual(
            sum(1 for t in trigger.triggered if t["agent"] == "claude"),
            developer_triggers_before,
            "resume_active_sessions must not re-queue waiting developer",
        )

    def test_wrong_sender_callback_does_not_advance_state(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nImplement.")
        cls_before = dict(store.get(sid)["coordinator_loop_state"])
        phase_before = store.get(sid)["current_phase"]
        trigger_count_before = len(trigger.triggered)
        engine._on_message({
            "sender": "codex_reviewer",
            "text": "PASS\nwrong turn",
            "type": "chat",
            "channel": "coord",
            "id": 500,
        })
        cls_after = store.get(sid)["coordinator_loop_state"]
        self.assertEqual(cls_before, cls_after)
        self.assertEqual(store.get(sid)["current_phase"], phase_before)
        self.assertEqual(len(trigger.triggered), trigger_count_before)

    def test_resume_awaiting_role_still_passes(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord", "CLASSIFY: NON_UI")
        self._simulate(engine, store, "coord", "codex_coord",
                       "NEXT: developer\nDo work.")
        trigger.triggered.clear()
        s = store.get(sid)
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        s["state"] = "active"
        store._sessions[0] = s
        engine.resume_active_sessions()
        self.assertTrue(any(t["agent"] == "claude" for t in trigger.triggered))
        self.assertFalse(any(t["agent"] == "codex_coord" for t in trigger.triggered))

    def test_terminal_final_not_retriggered(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._run_non_ui_happy_path(engine, store, cast)
        sid = session["id"]
        s = store.get(sid)
        self.assertEqual(s["state"], "complete")
        trigger.triggered.clear()
        s["state"] = "active"
        store._sessions[0] = s
        engine._trigger_coordinator_loop(s)
        self.assertFalse(trigger.triggered)
        engine.resume_active_sessions()
        self.assertFalse(trigger.triggered)

    def test_terminal_blocker_not_retriggered(self):
        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        session = self._start(engine, store, cast)
        sid = session["id"]
        self._simulate(engine, store, "coord", "codex_coord",
                       "BLOCKER: stop here")
        s = store.get(sid)
        self.assertEqual(s["state"], "interrupted")
        trigger.triggered.clear()
        s = store.get(sid)
        s["state"] = "active"
        store._sessions[0] = s
        engine._trigger_coordinator_loop(s)
        self.assertFalse(trigger.triggered)


class ReportOrchestratedWorkerDispatchTests(CoordinatorLoopEngineTestBase):
    """Report-orchestrated coordinator loop must queue workers, not idle-wait."""

    def _start_report_session(self, engine, store, cast, tmp: str):
        import config_loader
        import workspace_policy as wp

        profiles = config_loader.get_workspace_profiles(config_loader.load_config())
        policy = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-analysis",
                "workspace_mode": "read-only-analysis",
            },
        ).policy
        fields = wp.build_session_workspace_policy_fields(policy)
        ctx = engine._worker_context_from_session({
            "id": 1,
            "workspace_policy": policy,
            **fields,
        })
        roots = [tmp]
        by_role = dict(ctx.get("report_paths_by_role") or {})
        session = engine.start_session(
            COORDINATOR_LOOP_TEMPLATE["id"],
            "coord",
            cast,
            "user",
            goal="UI-09-C report flow",
            workspace_policy=policy,
            workspace_policy_hash=fields.get("workspace_policy_hash"),
            workspace_policy_version=fields.get("workspace_policy_version"),
            prompt_body="Prompt memo for report-orchestrated UI-09-C analysis.",
        )
        return session, policy, roots, by_role

    def test_ui_lead_dispatches_agy_with_handoff(self):
        from pathlib import Path
        from coordinator_loop import CoordinatorLoopState, _worker_action, on_worker_output
        from tests.test_report_orchestration import _developer_report_body, _report_ready

        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        tmp = tempfile.mkdtemp()
        dev_path = Path(tmp) / "dev.md"
        agy_path = Path(tmp) / "agy.md"
        dev_path.write_text(_developer_report_body(), encoding="utf-8")

        session, _policy, roots, by_role = self._start_report_session(
            engine, store, cast, tmp,
        )
        by_role["developer"] = str(dev_path)
        by_role["ui_lead"] = str(agy_path)
        sid = session["id"]

        s = store.get(sid)
        cls = CoordinatorLoopState.from_dict(s["coordinator_loop_state"])
        cls.awaiting_role = "developer"
        cls.classified = True
        cls.requires_agy = True
        ctx = {
            "allowed_report_roots": roots,
            "report_paths_by_role": by_role,
        }
        on_worker_output(
            cls,
            "developer",
            _report_ready(str(dev_path), status="PASS_WITH_NOTES"),
            worker_context=ctx,
        )
        store.update_coordinator_loop_state(sid, cls.to_dict())
        action = _worker_action(cls, "ui_lead", "Review UX from developer handoff.")
        store.update_coordinator_loop_state(
            sid, cls.to_dict(), worker_prompt=action.routing_body,
        )
        trigger.triggered.clear()
        engine._route_coordinator_loop_action(store.get(sid), action)

        s = store.get(sid)
        self.assertEqual(s.get("waiting_on"), "agy")
        self.assertEqual(s.get("state"), "waiting")
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "ui_lead")
        agy_triggers = [t for t in trigger.triggered if t.get("agent") == "agy"]
        self.assertEqual(len(agy_triggers), 1)
        prompt = agy_triggers[0].get("prompt") or ""
        self.assertIn("AGY UI Lead", prompt)
        self.assertIn("COORDINATOR HANDOFF", prompt)
        self.assertNotIn("REPORT CONTENT:", prompt)

    def test_missing_handoff_redirects_developer_not_idle_agy(self):
        from pathlib import Path
        from coordinator_loop import CoordinatorLoopState, _worker_action, on_worker_output
        from tests.test_report_orchestration import _report_ready

        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        tmp = tempfile.mkdtemp()
        dev_path = Path(tmp) / "dev.md"
        dev_path.write_text("# Developer\nNo handoff blocks.", encoding="utf-8")

        session, _policy, roots, by_role = self._start_report_session(
            engine, store, cast, tmp,
        )
        by_role["developer"] = str(dev_path)
        sid = session["id"]

        s = store.get(sid)
        cls = CoordinatorLoopState.from_dict(s["coordinator_loop_state"])
        cls.awaiting_role = "developer"
        cls.classified = True
        cls.requires_agy = True
        on_worker_output(
            cls,
            "developer",
            _report_ready(str(dev_path)),
            worker_context={"allowed_report_roots": roots, "report_paths_by_role": by_role},
        )
        store.update_coordinator_loop_state(sid, cls.to_dict())
        action = _worker_action(cls, "ui_lead", "Review UX.")
        store.update_coordinator_loop_state(
            sid, cls.to_dict(), worker_prompt=action.routing_body,
        )
        trigger.triggered.clear()
        engine._route_coordinator_loop_action(store.get(sid), action)

        s = store.get(sid)
        self.assertEqual(s.get("waiting_on"), "claude")
        self.assertEqual(s["coordinator_loop_state"]["awaiting_role"], "developer")
        self.assertEqual(s.get("current_phase"), 1)
        self.assertFalse(any(t.get("agent") == "agy" for t in trigger.triggered))
        self.assertTrue(any(t.get("agent") == "claude" for t in trigger.triggered))
        claude_entry = next(t for t in trigger.triggered if t.get("agent") == "claude")
        relay = claude_entry.get("relay_entry") or {}
        self.assertTrue((relay.get("relay_meta") or {}).get("handoff_repair"))
        wpc = relay.get("workspace_policy_context") or {}
        self.assertTrue(wpc.get("skip_snapshot_injection"))
        prompt = relay.get("prompt") or ""
        self.assertIn("MODE: Handoff block repair", prompt)
        self.assertIn("CURRENT REPORT CONTEXT", prompt)
        self.assertLess(len(prompt), 75_000)
        from worker_workspace import is_docs_only_snapshot_mode
        self.assertFalse(is_docs_only_snapshot_mode(relay, _policy))

    def test_handoff_repair_limit_blocks_after_max_rounds(self):
        from pathlib import Path
        from coordinator_loop import CoordinatorLoopState, _worker_action, on_worker_output
        from tests.test_report_orchestration import _report_ready

        engine, store, _, trigger, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        tmp = tempfile.mkdtemp()
        dev_path = Path(tmp) / "dev.md"
        dev_path.write_text("# Developer\nNo handoff blocks.", encoding="utf-8")

        session, _policy, roots, by_role = self._start_report_session(
            engine, store, cast, tmp,
        )
        by_role["developer"] = str(dev_path)
        sid = session["id"]

        s = store.get(sid)
        cls = CoordinatorLoopState.from_dict(s["coordinator_loop_state"])
        cls.awaiting_role = "developer"
        cls.classified = True
        cls.requires_agy = True
        on_worker_output(
            cls,
            "developer",
            _report_ready(str(dev_path)),
            worker_context={"allowed_report_roots": roots, "report_paths_by_role": by_role},
        )
        cls.max_handoff_repair_rounds_per_role = 2
        cls.handoff_repair_rounds = {"developer": 2}
        store.update_coordinator_loop_state(sid, cls.to_dict())
        action = _worker_action(cls, "ui_lead", "Review UX.")
        store.update_coordinator_loop_state(
            sid, cls.to_dict(), worker_prompt=action.routing_body,
        )
        trigger.triggered.clear()
        engine._route_coordinator_loop_action(store.get(sid), action)

        s = store.get(sid)
        self.assertEqual(s.get("state"), "interrupted")
        cls_after = CoordinatorLoopState.from_dict(s["coordinator_loop_state"])
        self.assertIn("handoff repair limit exceeded", cls_after.blocker_reason.lower())
        self.assertFalse(any(t.get("agent") == "claude" for t in trigger.triggered))

    def test_cast_maps_ui_lead_to_agy(self):
        engine, store, _, _, cast = self._make_engine(COORDINATOR_LOOP_TEMPLATE)
        self.assertEqual(cast.get("ui_lead"), "agy")


class TestCoordinatorLoopTemplateFile(unittest.TestCase):
    def test_shipped_template_validates(self):
        from session_store import validate_session_template
        path = ROOT / "session_templates" / "project-readonly-coordinator-loop.json"
        tmpl = json.loads(path.read_text("utf-8"))
        self.assertEqual(validate_session_template(tmpl), [])

    def test_shipped_template_passes_dryrun_safety(self):
        import safety_invariants as si
        path = ROOT / "session_templates" / "project-readonly-coordinator-loop.json"
        tmpl = json.loads(path.read_text("utf-8"))
        self.assertTrue(si.check_dryrun_template_safe(tmpl).ok)


if __name__ == "__main__":
    unittest.main()
