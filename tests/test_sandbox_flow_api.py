"""Tests for V2-B sandbox flow start API — localhost/config-gated POST /api/sandbox/flow/start."""

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from safety_invariants import (  # noqa: E402
    check_sandbox_config,
    is_loopback_client,
    normalize_sandbox_config,
    resolve_sandbox_flow_cast,
)
from session_relay import RELAY_ELIGIBLE_AGENTS, is_relay_eligible  # noqa: E402


def _load_base_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def _enabled_sandbox_config(data_dir: Path) -> dict:
    cfg = _load_base_config()
    cfg.setdefault("server", {})["data_dir"] = str(data_dir)
    cfg["sandbox"] = {
        "flow_start_enabled": True,
        "flow_start_require_loopback": True,
        "flow_start_template_allowlist": ["sandbox-bakery-flow"],
        "flow_start_channel_prefix": "sandbox-flow",
        "flow_start_max_active": 1,
        "flow_start_rate_limit_per_minute": 20,
        "flow_start_output_root": str(data_dir / "reports"),
    }
    return cfg


SANDBOX_TEMPLATE = {
    "id": "sandbox-bakery-flow",
    "name": "Sandbox Bakery Flow",
    "flow_coordinator": True,
    "sandbox_only": True,
    "roles": ["developer", "ui_lead", "codex_reviewer"],
    "phases": [
        {"name": "Dev", "participants": ["developer"], "prompt": "x",
         "turn_order": "sequential"},
        {"name": "AGY", "participants": ["ui_lead"], "prompt": "x",
         "turn_order": "sequential"},
        {"name": "Codex", "participants": ["codex_reviewer"], "prompt": "x",
         "turn_order": "sequential"},
    ],
}


class LoopbackHelperTests(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))

    def test_ipv6_loopback(self):
        self.assertTrue(is_loopback_client("::1"))

    def test_ipv4_mapped_ipv6_loopback(self):
        self.assertTrue(is_loopback_client("::ffff:127.0.0.1"))

    def test_localhost_string(self):
        self.assertTrue(is_loopback_client("localhost"))

    def test_lan_rejected(self):
        self.assertFalse(is_loopback_client("192.168.1.10"))

    def test_none_rejected(self):
        self.assertFalse(is_loopback_client(None))


class RelayEligibilityInvariantTests(unittest.TestCase):
    def test_agy_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("agy"))
        self.assertNotIn("agy", RELAY_ELIGIBLE_AGENTS)

    def test_claude_relay_eligible(self):
        self.assertTrue(is_relay_eligible("claude"))
        self.assertIn("claude", RELAY_ELIGIBLE_AGENTS)

    def test_claude_dryrun_not_relay_eligible(self):
        self.assertFalse(is_relay_eligible("claude_dryrun"))
        self.assertNotIn("claude_dryrun", RELAY_ELIGIBLE_AGENTS)

    def test_codex_reviewer_still_eligible(self):
        self.assertTrue(is_relay_eligible("codex_reviewer"))
        self.assertIn("codex_reviewer", RELAY_ELIGIBLE_AGENTS)


class SandboxFlowApiTests(unittest.TestCase):
    """Integration tests against the FastAPI app (single configure in setUpClass)."""

    tmp: tempfile.TemporaryDirectory | None = None
    data_dir: Path | None = None
    client = None
    session_token = "unit-test-session-token-do-not-persist"

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient
        import app as app_module

        cls.tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.tmp.name) / "data"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        (cls.data_dir / "reports").mkdir(parents=True, exist_ok=True)

        cfg = _enabled_sandbox_config(cls.data_dir)
        app_module.configure(cfg, session_token=cls.session_token)
        cls.client = TestClient(app_module.app, client=("127.0.0.1", 50000))
        cls._app = app_module

    @classmethod
    def tearDownClass(cls):
        if cls.tmp:
            cls.tmp.cleanup()

    def setUp(self):
        sb = dict(_enabled_sandbox_config(self.data_dir)["sandbox"])
        self._app.config["sandbox"] = sb
        # Clear sessions between tests
        if self._app.session_store:
            self._app.session_store._sessions = []
            self._app.session_store._next_id = 1
            self._app.session_store._save()
        self._app.room_settings["channels"] = ["general"]
        self._app._save_settings()
        self._app._sandbox_flow_rate.clear()
        # Register workflow agents so SessionEngine triggers queue entries
        if self._app.registry:
            self._app.registry._instances.clear()
            for base in ("claude", "agy", "codex_reviewer"):
                self._app.registry.register(base)

    def _post(self, body=None, *, confirm=True, client_addr=("127.0.0.1", 50000)):
        from starlette.testclient import TestClient
        headers = {"Content-Type": "application/json"}
        if confirm:
            headers["X-Sandbox-Flow-Confirm"] = "1"
        payload = body if body is not None else {
            "task": "Bakery POS checkout modal UX improvement mock task only",
            "phase": "v2-d",
        }
        c = TestClient(self._app.app, client=client_addr)
        return c.post("/api/sandbox/flow/start", headers=headers, json=payload)

    def test_01_flag_off_reject(self):
        self._app.config["sandbox"]["flow_start_enabled"] = False
        r = self._post()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "SANDBOX_FLOW_DISABLED")

    def test_02_non_loopback_reject(self):
        r = self._post(client_addr=("192.168.1.10", 50000))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "NOT_LOOPBACK")

    def test_02b_require_loopback_false_rejected_at_endpoint(self):
        """Runtime config tampering cannot disable loopback requirement (V2-B fail-closed)."""
        self._app.config["sandbox"]["flow_start_require_loopback"] = False
        r = self._post(client_addr=("127.0.0.1", 50000))
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["code"], "INVALID_SANDBOX_CONFIG")
        self.assertIn("flow_start_require_loopback", r.json()["error"])

    def test_02c_spoofed_forwarded_headers_do_not_bypass_loopback(self):
        from starlette.testclient import TestClient

        headers = {
            "Content-Type": "application/json",
            "X-Sandbox-Flow-Confirm": "1",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
            "Host": "localhost",
        }
        c = TestClient(self._app.app, client=("192.168.1.10", 50000))
        r = c.post(
            "/api/sandbox/flow/start",
            headers=headers,
            json={"task": "ok", "phase": "v2-d"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "NOT_LOOPBACK")

    def test_04_missing_confirm_reject(self):
        r = self._post(confirm=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "CONFIRM_REQUIRED")

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_03_loopback_flag_on_confirm_allow(self, _mock_online):
        r = self._post()
        self.assertEqual(r.status_code, 201, r.text)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["cast"]["developer"], "claude")
        self.assertEqual(data["cast"]["ui_lead"], "agy")
        self.assertEqual(data["cast"]["codex_reviewer"], "codex_reviewer")

    def test_05_template_not_allowlisted(self):
        r = self._post({"task": "ok", "template_id": "code-review", "phase": "v2-d"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "TEMPLATE_NOT_ALLOWLISTED")

    def test_06_template_missing_flow_coordinator(self):
        bad = dict(SANDBOX_TEMPLATE)
        bad["id"] = "sandbox-bad-no-fc"
        bad["flow_coordinator"] = False
        self._app.session_store._templates[bad["id"]] = bad
        self._app.config["sandbox"]["flow_start_template_allowlist"].append(bad["id"])
        r = self._post({"task": "ok", "template_id": bad["id"], "phase": "v2-d"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "MISSING_FLOW_COORDINATOR")

    def test_07_template_missing_sandbox_only(self):
        bad = dict(SANDBOX_TEMPLATE)
        bad["id"] = "sandbox-bad-no-so"
        bad["sandbox_only"] = False
        self._app.session_store._templates[bad["id"]] = bad
        self._app.config["sandbox"]["flow_start_template_allowlist"].append(bad["id"])
        r = self._post({"task": "ok", "template_id": bad["id"], "phase": "v2-d"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "MISSING_SANDBOX_ONLY")

    def test_08_twinpet_task_reject(self):
        r = self._post({"task": "Fix twinpet-pos checkout", "phase": "v2-d"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "TASK_BLOCKED")
        self.assertEqual(len(self._app.session_store.list_all()), 0)

    @patch("app._sandbox_agent_is_online", return_value=False)
    def test_09_roster_offline(self, _mock_online):
        r = self._post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "ROSTER_OFFLINE")

    def test_10_client_cast_spoof_reject(self):
        body = {
            "task": "ok",
            "phase": "v2-d",
            "cast": {"developer": "claude"},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("cast", r.json()["error"].lower())

    def test_11_client_channel_spoof_reject(self):
        body = {"task": "ok", "phase": "v2-d", "channel": "evil"}
        r = self._post(body)
        self.assertEqual(r.status_code, 400)

    def test_12_dry_run_reject(self):
        body = {"task": "ok", "phase": "v2-d", "dry_run": True}
        r = self._post(body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("dry_run", r.json()["error"])

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_13_channel_created_with_pattern(self, _mock_online):
        r = self._post()
        self.assertEqual(r.status_code, 201)
        channel = r.json()["channel"]
        self.assertRegex(channel, r"^sandbox-flow-v2-d-\d{6}-\d{4}-\d+$")
        self.assertIn(channel, self._app.room_settings["channels"])

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_14_session_started_with_roster_cast(self, _mock_online):
        r = self._post()
        self.assertEqual(r.status_code, 201)
        sid = r.json()["session_id"]
        session = self._app.session_store.get(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["cast"]["developer"], "claude")
        self.assertEqual(session["cast"]["codex_reviewer"], "codex_reviewer")
        q = self.data_dir / "claude_queue.jsonl"
        self.assertTrue(q.exists())
        lines = q.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(lines)
        entry = json.loads(lines[-1])
        self.assertIn("prompt", entry)

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_16_no_session_token_file_written(self, _mock_online):
        r = self._post()
        self.assertEqual(r.status_code, 201)
        for p in self.data_dir.glob("*session*token*"):
            self.fail(f"unexpected token file: {p}")
        for p in self.data_dir.glob(".session_token*"):
            self.fail(f"unexpected token file: {p}")

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_17_second_active_sandbox_flow_409(self, _mock_online):
        r1 = self._post()
        self.assertEqual(r1.status_code, 201)
        r2 = self._post({"task": "second task", "phase": "v2-d"})
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["code"], "SANDBOX_FLOW_ACTIVE")

    def test_sessions_start_still_requires_session_token(self):
        r = self.client.post(
            "/api/sessions/start",
            headers={"Content-Type": "application/json"},
            json={"template_id": "sandbox-bakery-flow", "channel": "general", "goal": "x"},
        )
        self.assertEqual(r.status_code, 403)

    def test_sandbox_exemption_does_not_broaden_other_apis(self):
        r = self.client.get("/api/sessions/templates")
        self.assertEqual(r.status_code, 403)

    def test_validate_channel_name_rejects_sandbox_flow_prefix(self):
        self.assertFalse(self._app._validate_channel_name("sandbox-flow-evil"))
        self.assertFalse(self._app._validate_channel_name("sandbox-flow-x"))
        self.assertTrue(self._app._validate_channel_name("dev-team"))

    def test_validate_sandbox_channel_name_accepts_generated_pattern(self):
        self.assertTrue(
            self._app._validate_sandbox_channel_name("sandbox-flow-v2-d-250624-1200-1"))
        self.assertFalse(self._app._validate_sandbox_channel_name("general"))

    @patch("app.broadcast_status", new_callable=AsyncMock)
    def test_ws_channel_create_rejects_sandbox_flow_namespace(self, _mock_status):
        with self.client.websocket_connect(
                f"/ws?token={self.session_token}") as ws:
            ws.send_text(json.dumps({"type": "channel_create", "name": "sandbox-flow-evil"}))
        self.assertNotIn("sandbox-flow-evil", self._app.room_settings["channels"])

    @patch("app.broadcast_status", new_callable=AsyncMock)
    def test_ws_channel_rename_rejects_sandbox_flow_namespace(self, _mock_status):
        self._app.room_settings["channels"] = ["general", "devteam"]
        self._app._save_settings()
        with self.client.websocket_connect(
                f"/ws?token={self.session_token}") as ws:
            ws.send_text(json.dumps({
                "type": "channel_rename",
                "old_name": "devteam",
                "new_name": "sandbox-flow-evil",
            }))
        self.assertIn("devteam", self._app.room_settings["channels"])
        self.assertNotIn("sandbox-flow-evil", self._app.room_settings["channels"])


    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_endpoint_rejects_non_default_channel_prefix(self, _mock_online):
        self._app.config["sandbox"]["flow_start_channel_prefix"] = "evil-prefix"
        r = self._post()
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["code"], "INVALID_SANDBOX_CONFIG")
        self.assertIn('flow_start_channel_prefix must be "sandbox-flow"', r.json()["error"])

    # --- V2-E2C: fail-closed auto-prune of completed sandbox-flow channels ---

    def _sandbox_channel(self, n: int) -> str:
        return f"sandbox-flow-v2-d-260625-1134-{n}"

    def _set_channels(self, channels: list[str]):
        self._app.room_settings["channels"] = list(channels)
        self._app._save_settings()

    def _add_session(self, channel: str, *, state: str = "complete", session_id: int = 1):
        self._app.session_store._sessions.append({
            "id": session_id,
            "template_id": "sandbox-bakery-flow",
            "template_name": "Sandbox Bakery Flow",
            "channel": channel,
            "state": state,
            "cast": {
                "developer": "claude",
                "ui_lead": "agy",
                "codex_reviewer": "codex_reviewer",
            },
            "started_at": "2026-06-25T11:34:00",
        })
        self._app.session_store._save()

    def test_list_prunable_includes_terminal_sandbox_only(self):
        ch = self._sandbox_channel(22)
        self._add_session(ch, state="complete", session_id=22)
        self._set_channels(["general", "relay-dryrun", "dev-team", ch])
        prunable = self._app._list_prunable_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(prunable, [ch])

    def test_protected_channels_include_sdlc_dryrun(self):
        protected = self._app._PROTECTED_CHANNEL_NAMES
        self.assertIn("general", protected)
        self.assertIn("relay-dryrun", protected)
        self.assertIn("sdlc-dryrun", protected)

    def test_general_relay_custom_never_prunable(self):
        ch = self._sandbox_channel(30)
        self._add_session(ch, state="complete", session_id=30)
        self._set_channels(["general", "relay-dryrun", "dev-team", ch])
        pruned = self._app._prune_completed_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(pruned, [ch])
        self.assertIn("general", self._app.room_settings["channels"])
        self.assertIn("relay-dryrun", self._app.room_settings["channels"])
        self.assertIn("dev-team", self._app.room_settings["channels"])

    def test_sdlc_dryrun_channel_never_prunable(self):
        ch = self._sandbox_channel(31)
        self._add_session(ch, state="complete", session_id=31)
        self._set_channels(["general", "relay-dryrun", "sdlc-dryrun", "dev-team", ch])
        pruned = self._app._prune_completed_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(pruned, [ch])
        self.assertIn("sdlc-dryrun", self._app.room_settings["channels"])
        self.assertIn("general", self._app.room_settings["channels"])
        self.assertIn("relay-dryrun", self._app.room_settings["channels"])

    def test_active_waiting_paused_sandbox_not_prunable(self):
        for state, sid in (("active", 40), ("waiting", 41), ("paused", 42)):
            ch = f"sandbox-flow-v2-d-state-{sid}"
            self._add_session(ch, state=state, session_id=sid)
        channels = ["general"] + [
            f"sandbox-flow-v2-d-state-{sid}" for _, sid in
            (("active", 40), ("waiting", 41), ("paused", 42))]
        self._set_channels(channels)
        prunable = self._app._list_prunable_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(prunable, [])

    def test_orphan_sandbox_without_session_not_prunable(self):
        ch = "sandbox-flow-v2-d-orphan-99"
        self._set_channels(["general", ch])
        prunable = self._app._list_prunable_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(prunable, [])

    def test_ambiguous_multiple_sessions_not_prunable(self):
        ch = self._sandbox_channel(50)
        self._add_session(ch, state="complete", session_id=50)
        self._add_session(ch, state="interrupted", session_id=51)
        self._set_channels(["general", ch])
        prunable = self._app._list_prunable_sandbox_channels(
            self._app.room_settings, self._app.session_store)
        self.assertEqual(prunable, [])

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_channel_limit_when_none_prunable(self, _mock_online):
        channels = [
            "general", "relay-dryrun", "dev-team",
            "team-a", "team-b", "team-c", "team-d", "team-e",
        ]
        self._set_channels(channels)
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "CHANNEL_LIMIT")
        self.assertEqual(self._app.room_settings["channels"], channels)

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_prune_completed_sandbox_enables_start(self, _mock_online):
        channels = ["general", "relay-dryrun"]
        for i in range(22, 28):
            ch = self._sandbox_channel(i)
            channels.append(ch)
            self._add_session(ch, state="complete", session_id=i)
        self._set_channels(channels)
        self.assertEqual(len(self._app.room_settings["channels"]), 8)
        r = self._post()
        self.assertEqual(r.status_code, 201, r.text)
        remaining_sandbox = [
            c for c in self._app.room_settings["channels"]
            if c.startswith("sandbox-flow-")]
        self.assertEqual(len(remaining_sandbox), 1)
        self.assertEqual(remaining_sandbox[0], r.json()["channel"])

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_prune_does_not_call_delete_channel(self, _mock_online):
        channels = ["general", "relay-dryrun"]
        for i in range(22, 28):
            ch = self._sandbox_channel(i)
            channels.append(ch)
            self._add_session(ch, state="interrupted", session_id=i)
        self._set_channels(channels)
        with patch.object(self._app.store, "delete_channel") as mock_delete:
            r = self._post()
            self.assertEqual(r.status_code, 201, r.text)
            mock_delete.assert_not_called()

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_session_create_failure_rolls_back_new_channel(self, _mock_online):
        with patch.object(self._app.session_engine, "start_session", return_value=None):
            r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "SESSION_CREATE_FAILED")
        sandbox_channels = [
            c for c in self._app.room_settings["channels"]
            if c.startswith("sandbox-flow-")]
        self.assertEqual(sandbox_channels, [])

    @patch("app._sandbox_agent_is_online", return_value=True)
    def test_sandbox_flow_active_still_enforced_after_prune(self, _mock_online):
        ch = self._sandbox_channel(60)
        self._add_session(ch, state="active", session_id=60)
        self._set_channels(["general", ch])
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "SANDBOX_FLOW_ACTIVE")


class RosterCastResolutionTests(unittest.TestCase):
    def test_generic_codex_rejected_for_reviewer(self):
        tmpl = dict(SANDBOX_TEMPLATE)
        roster = {
            "developer": "claude",
            "ui_lead": "agy",
            "runtime_reviewer": "codex",
            "reviewer": "codex",
            "safety_guard": "codexsafe",
            "runtime_coordinator": "codex_coordinator",
        }
        cast, err, _details = resolve_sandbox_flow_cast(
            tmpl, roster, is_online_fn=lambda _a: True)
        self.assertIsNone(cast)
        self.assertEqual(err, "ROSTER_INVALID")


class ConfigLoaderSandboxMergeTests(unittest.TestCase):
    def test_normalize_defaults_off(self):
        sb = normalize_sandbox_config({})
        self.assertFalse(sb["flow_start_enabled"])

    def test_check_sandbox_config_rejects_enabled_without_loopback(self):
        sb = normalize_sandbox_config({
            "sandbox": {
                "flow_start_enabled": True,
                "flow_start_require_loopback": False,
            },
        })
        result = check_sandbox_config(sb)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "INV-022")
        self.assertIn("flow_start_require_loopback", result.reason)

    def test_check_sandbox_config_rejects_non_default_channel_prefix(self):
        sb = normalize_sandbox_config({
            "sandbox": {
                "flow_start_enabled": True,
                "flow_start_channel_prefix": "evil-prefix",
            },
        })
        result = check_sandbox_config(sb)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "INV-022")
        self.assertIn('flow_start_channel_prefix must be "sandbox-flow"', result.reason)

    def test_check_sandbox_config_accepts_default_channel_prefix(self):
        sb = normalize_sandbox_config({
            "sandbox": {
                "flow_start_enabled": True,
                "flow_start_channel_prefix": "sandbox-flow",
            },
        })
        result = check_sandbox_config(sb)
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
