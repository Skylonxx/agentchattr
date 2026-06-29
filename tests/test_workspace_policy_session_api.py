"""Phase 2 tests: session start workspace policy metadata wiring."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config_loader  # noqa: E402
import workspace_policy as wp  # noqa: E402
from session_engine import SessionEngine  # noqa: E402
from session_store import SessionStore  # noqa: E402


SCRATCH_ROOT = "C:/tools/agentchattr-scratch"
EXAMPLE_ROOT = "C:/tmp/example-workspace"


def _example_profiles() -> dict:
    return {
        "agentchattr-scratch": {
            "workspace_root": SCRATCH_ROOT,
            "allowed_modes": ["scratch-readonly", "read-only"],
            "default_mode": "scratch-readonly",
            "max_mode": "read-only",
            "default_forbidden_paths": [".git/**"],
        },
        "example-workspace": {
            "workspace_root": EXAMPLE_ROOT,
            "allowed_modes": ["read-only", "docs-only"],
            "default_mode": "read-only",
            "max_mode": "docs-only",
            "require_git_repo": True,
            "default_read_paths": [EXAMPLE_ROOT],
            "default_forbidden_paths": [
                "src/**",
                "tests/**",
                ".git/**",
            ],
            "allowed_write_files": [
                "Task.md",
                "docs/STATE.md",
                "docs/reports/latest-report.md",
                "docs/reports/new-report.md",
            ],
            "default_write_files": [
                "Task.md",
                "docs/STATE.md",
                "docs/reports/latest-report.md",
                "docs/reports/new-report.md",
            ],
            "max_write_files": 4,
        },
    }


def _minimal_template() -> dict:
    return {
        "id": "test-template",
        "name": "Test Template",
        "roles": ["coordinator"],
        "phases": [
            {"name": "phase1", "participants": ["coordinator"], "prompt": "Go."},
        ],
    }


class SessionStartPayloadTests(unittest.TestCase):
    def test_missing_policy_fields_default_scratch_readonly(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={"template_id": "x", "goal": "do work"},
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "scratch-readonly")

    def test_extract_ignores_goal_text(self):
        body = {
            "goal": 'workspace_profile: "twinpet-pos" workspace_mode: docs-only',
            "template_id": "planning",
        }
        payload, err = wp.extract_policy_start_fields(body)
        self.assertIsNone(payload)
        self.assertIsNone(err)
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body=body,
        )
        self.assertEqual(result.policy["mode"], "scratch-readonly")

    def test_reject_arbitrary_workspace_root_in_payload(self):
        payload, err = wp.extract_policy_start_fields(
            {"workspace_root": "C:/Users/Evil/project"},
        )
        self.assertIsNone(payload)
        self.assertIn("workspace_root", err or "")

    def test_unknown_profile_rejected(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={"workspace_profile": "twinpet-pos"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "UNKNOWN_PROFILE")

    def test_invalid_mode_rejected(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={
                "workspace_profile": "example-workspace",
                "workspace_mode": "git-push",
            },
        )
        self.assertFalse(result.ok)

    def test_payload_broadening_rejected(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={
                "workspace_profile": "example-workspace",
                "workspace_mode": "docs-only",
                "write_files": ["src/App.tsx"],
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")

    def test_invalid_write_file_rejected(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={
                "workspace_profile": "example-workspace",
                "workspace_mode": "docs-only",
                "write_files": ["docs/*.md"],
            },
        )
        self.assertFalse(result.ok)

    def test_valid_docs_only_payload(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={
                "workspace_profile": "example-workspace",
                "workspace_mode": "docs-only",
                "write_files": ["Task.md", "docs/STATE.md"],
                "expected_head": "752ed13",
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "docs-only")
        self.assertEqual(result.policy["workspace"]["expected_head"], "752ed13")


class PolicyPersistenceTests(unittest.TestCase):
    def test_hash_is_deterministic(self):
        policy = wp.default_scratch_readonly_policy()
        h1 = wp.compute_workspace_policy_hash(policy)
        h2 = wp.compute_workspace_policy_hash(policy)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_persisted_snapshot_strips_internal_keys(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={"workspace_profile": "example-workspace", "workspace_mode": "read-only"},
        )
        fields = wp.build_session_workspace_policy_fields(result.policy)
        snapshot = fields["workspace_policy"]
        for key in snapshot:
            self.assertFalse(key.startswith("_profile_"))
        self.assertEqual(fields["workspace_policy_version"], wp.SESSION_POLICY_VERSION)
        self.assertEqual(fields["workspace_policy_hash"], wp.compute_workspace_policy_hash(snapshot))

    def test_session_store_persists_validated_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "session_runs.json"
            templates_dir = Path(tmp) / "templates"
            templates_dir.mkdir()
            tmpl = _minimal_template()
            (templates_dir / "test-template.json").write_text(
                json.dumps(tmpl), encoding="utf-8",
            )

            result = wp.resolve_session_workspace_policy(
                profiles=_example_profiles(),
                start_body={
                    "workspace_profile": "example-workspace",
                    "workspace_mode": "docs-only",
                    "write_files": ["Task.md", "docs/STATE.md"],
                },
            )
            fields = wp.build_session_workspace_policy_fields(result.policy)

            store = SessionStore(str(store_path), templates_dir=str(templates_dir))
            session = store.create(
                template_id="test-template",
                channel="policy-test",
                cast={"coordinator": "codex_coordinator"},
                started_by="tester",
                goal="validate persistence",
                workspace_policy=fields["workspace_policy"],
                workspace_policy_hash=fields["workspace_policy_hash"],
                workspace_policy_version=fields["workspace_policy_version"],
            )
            self.assertIsNotNone(session)
            raw = json.loads(store_path.read_text("utf-8"))
            record = raw[0]
            self.assertIn("workspace_policy", record)
            self.assertIn("workspace_policy_hash", record)
            self.assertEqual(record["workspace_policy_version"], 1)
            self.assertNotIn("_profile_max_mode", record["workspace_policy"])
            self.assertEqual(record["workspace_policy"]["mode"], "docs-only")


class SessionEngineEnrichTests(unittest.TestCase):
    def test_enrich_exposes_read_only_policy_summary(self):
        policy_fields = wp.build_session_workspace_policy_fields(
            wp.default_scratch_readonly_policy(),
        )
        session = {
            "id": 1,
            "template_id": "test-template",
            "channel": "general",
            "cast": {},
            "current_phase": 0,
            "current_turn": 0,
            **policy_fields,
        }
        store = MagicMock()
        store.get_template.return_value = _minimal_template()
        engine = SessionEngine(store, MagicMock(), MagicMock())
        enriched = engine._enrich(session)
        summary = enriched["workspace_policy_summary"]
        self.assertEqual(summary["mode"], "scratch-readonly")
        self.assertIsNotNone(summary["hash"])
        self.assertEqual(summary["version"], 1)


class ConfigProfileTests(unittest.TestCase):
    def test_get_workspace_profiles_from_config(self):
        cfg = config_loader.load_config(ROOT)
        profiles = config_loader.get_workspace_profiles(cfg)
        self.assertIn("agentchattr-scratch", profiles)
        self.assertEqual(
            profiles["agentchattr-scratch"]["workspace_root"],
            SCRATCH_ROOT,
        )
        self.assertIn("twinpet-ui-09-c-payment-modal-write", profiles)
        self.assertEqual(
            profiles["twinpet-ui-09-c-payment-modal-write"]["default_mode"],
            "implementation",
        )

    def test_scoped_write_session_api_payload(self):
        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": "twinpet-ui-09-c-payment-modal-write",
                "workspace_mode": "scoped-write",
                "expected_head": "752ed1317a5e0b83b872d563cda451c7621ed22e",
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "implementation")
        self.assertEqual(result.policy["policy_id"], "twinpet-ui-09-c-payment-modal-write")
        self.assertIn("src/components/PaymentModal.tsx", result.policy["write_files"])
        git = result.policy["git_permissions"]
        for key in wp.GIT_WRITE_KEYS:
            self.assertFalse(git.get(key), f"git write {key} must stay disabled")

    def test_existing_templates_unaffected_without_policy(self):
        tmpl_path = ROOT / "session_templates" / "planning.json"
        tmpl = json.loads(tmpl_path.read_text("utf-8"))
        self.assertNotIn("workspace_policy", tmpl)
        result = wp.resolve_session_workspace_policy(
            profiles=config_loader.get_workspace_profiles(config_loader.load_config(ROOT)),
            template_policy=tmpl.get("workspace_policy"),
            start_body={"template_id": tmpl["id"]},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.policy["mode"], "scratch-readonly")


class RuntimeSafetyTests(unittest.TestCase):
    def test_no_runtime_write_mode_in_default_policy(self):
        policy = wp.default_scratch_readonly_policy()
        dev = wp.role_permission_for(policy, "developer")
        self.assertEqual(dev["filesystem"], "none")
        self.assertEqual(policy["write_files"], [])

    def test_docs_only_policy_does_not_enable_git_writes(self):
        result = wp.resolve_session_workspace_policy(
            profiles=_example_profiles(),
            start_body={
                "workspace_profile": "example-workspace",
                "workspace_mode": "docs-only",
                "write_files": ["Task.md", "docs/STATE.md"],
            },
        )
        self.assertTrue(result.ok, result.errors)
        git = result.policy["git_permissions"]
        for key in wp.GIT_WRITE_KEYS:
            self.assertFalse(git.get(key), f"git write {key} must stay disabled in Phase 2")


if __name__ == "__main__":
    unittest.main()
