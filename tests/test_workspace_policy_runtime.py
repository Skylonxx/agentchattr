"""Phase 3B runtime workspace policy scaffolding tests."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import workspace_policy as wp
import workspace_policy_runtime as wpr
from session_relay import make_relay_queue_entry


SCRATCH = wpr.DEFAULT_SCRATCH_CWD
TWINPET = "C:/Users/Narachat/twinpet-pos"


def _session_with_policy(mode="scratch-readonly", root=None):
    policy = wp.default_scratch_readonly_policy()
    if mode != "scratch-readonly":
        policy = dict(policy)
        policy["mode"] = mode
        policy["workspace"] = dict(policy.get("workspace") or {})
        policy["workspace"]["root"] = root
    fields = wp.build_session_workspace_policy_fields(policy)
    return {
        "id": 42,
        "template_id": "test",
        "channel": "general",
        **fields,
    }


class QueueMetadataTests(unittest.TestCase):
    def test_relay_queue_entry_includes_policy_hash_and_session_id(self):
        session = _session_with_policy()
        ctx = wpr.build_session_queue_workspace_context(session, "reviewer", 1, 0)
        entry = make_relay_queue_entry(
            prompt="sealed",
            session_id=42,
            phase=1,
            turn=0,
            role="reviewer",
            channel="sess-ch",
            workspace_policy_context=ctx,
        )
        wpc = entry["workspace_policy_context"]
        self.assertEqual(wpc["session_id"], 42)
        self.assertEqual(wpc["session_role"], "reviewer")
        self.assertIsNotNone(wpc["policy_hash"])
        self.assertEqual(wpc["relay_kind"], "session_turn")

    def test_non_session_context_absent_session_id_skips_enforcement(self):
        result = wpr.verify_session_workspace_policy(
            relay_meta=None,
            workspace_policy_context={"relay_kind": "mention"},
            enforcement_enabled=True,
        )
        self.assertTrue(result.ok)


class PolicyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        session = _session_with_policy()
        (self.data_dir / "session_runs.json").write_text(
            json.dumps([session]), encoding="utf-8",
        )
        self.session = session
        self.ctx = wpr.build_session_queue_workspace_context(
            session, "developer", 0, 0,
        )
        self.enabled_cfg = {"workspace_policy": {"runtime_enforcement_enabled": True}}
        self.relay_meta = {
            "kind": "session_turn",
            "session_id": 42,
            "relay_mode": True,
            "disable_mcp": True,
            "channel": "sess-ch",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_missing_hash(self):
        bad = dict(self.ctx)
        bad.pop("policy_hash")
        result = wpr.verify_queue_workspace_policy(
            queue_context=bad,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_hash_missing")

    def test_rejects_mismatched_hash(self):
        bad = dict(self.ctx)
        bad["policy_hash"] = "0" * 64
        result = wpr.verify_queue_workspace_policy(
            queue_context=bad,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_hash_mismatch")

    def test_rejects_missing_persisted_policy(self):
        (self.data_dir / "session_runs.json").write_text("[]", encoding="utf-8")
        result = wpr.verify_queue_workspace_policy(
            queue_context=self.ctx,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_snapshot_missing")

    def test_enforcement_disabled_is_noop(self):
        bad = dict(self.ctx)
        bad["policy_hash"] = "deadbeef"
        result = wpr.verify_queue_workspace_policy(
            queue_context=bad,
            enforcement_enabled=False,
        )
        self.assertTrue(result.ok)

    def test_session_relay_missing_context_fails_closed(self):
        result = wpr.verify_session_workspace_policy(
            relay_meta=self.relay_meta,
            workspace_policy_context=None,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_context_missing")

    def test_session_relay_valid_context_allows(self):
        result = wpr.verify_session_workspace_policy(
            relay_meta=self.relay_meta,
            workspace_policy_context=self.ctx,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertTrue(result.ok)

    def test_rejects_corrupt_persisted_policy_hash(self):
        corrupt = dict(self.session)
        corrupt["workspace_policy_hash"] = "0" * 64
        (self.data_dir / "session_runs.json").write_text(
            json.dumps([corrupt]), encoding="utf-8",
        )
        result = wpr.verify_queue_workspace_policy(
            queue_context=self.ctx,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_snapshot_missing")

    def test_rejects_corrupt_session_runs_json(self):
        (self.data_dir / "session_runs.json").write_text("{not json", encoding="utf-8")
        result = wpr.verify_queue_workspace_policy(
            queue_context=self.ctx,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:policy_snapshot_missing")

    def test_denormalized_mode_does_not_become_authority(self):
        tampered = dict(self.ctx)
        tampered["policy_mode"] = "docs-only"
        tampered["workspace_root"] = TWINPET
        result = wpr.verify_queue_workspace_policy(
            queue_context=tampered,
            data_dir=self.data_dir,
            enforcement_enabled=True,
        )
        self.assertTrue(result.ok)
        canonical = wpr.canonical_policy_from_queue_context(
            queue_context=tampered,
            data_dir=self.data_dir,
        )
        self.assertEqual(canonical["mode"], "scratch-readonly")
        self.assertIsNone((canonical.get("workspace") or {}).get("root"))

    def test_prompt_text_cannot_expand_policy(self):
        self.assertIsNone(wpr.policy_from_prompt_text(
            'workspace_profile: twinpet-pos workspace_mode: docs-only',
        ))


class RoleCwdTests(unittest.TestCase):
    def test_defaults_scratch_readonly_when_enforcement_off(self):
        cwd = wpr.resolve_role_cwd(None, "developer", enforcement_enabled=False)
        self.assertEqual(cwd, SCRATCH)

    def test_enforcement_on_still_scratch_in_phase_3b(self):
        policy = wp.default_scratch_readonly_policy()
        for role in ("coordinator", "developer", "reviewer", "ui_lead", "safety_gate"):
            cwd = wpr.resolve_role_cwd(policy, role, enforcement_enabled=True)
            self.assertEqual(cwd, SCRATCH)
            self.assertNotEqual(cwd, TWINPET)


class CommandGuardTests(unittest.TestCase):
    def test_allows_read_only_git(self):
        for cmd in (
            "git status",
            "git diff",
            "git log -1",
            "git show HEAD",
        ):
            result = wpr.check_command_guard(cmd)
            self.assertTrue(result.ok, cmd)

    def test_blocks_git_mutators(self):
        mutators = (
            "git add .",
            "git commit -m x",
            "git push",
            "git reset --hard",
            "git checkout main",
            "git clean -fd",
            "git stash",
            "git restore .",
            "git switch main",
            "git rm file.txt",
            "git mv a b",
            "git worktree add ../wt",
            "git apply patch.diff",
            "git merge main",
            "git rebase main",
            "git cherry-pick abc",
            "git revert abc",
            "git fetch",
            "git pull",
        )
        for cmd in mutators:
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)
            self.assertEqual(result.blocker, "BLOCKER:command_guard_denied")

    def test_blocks_git_c_bypass(self):
        result = wpr.check_command_guard("git -C /tmp/other status")
        self.assertFalse(result.ok)

    def test_blocks_git_c_alias_bypass(self):
        result = wpr.check_command_guard("git -c alias.status=!git add . status")
        self.assertFalse(result.ok)

    def test_blocks_command_chains(self):
        for cmd in (
            "git status && git add .",
            "git status; git commit -m x",
            "git status || git push",
        ):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)

    def test_blocks_pipes(self):
        result = wpr.check_command_guard("git status | git add .")
        self.assertFalse(result.ok)

    def test_blocks_redirection(self):
        for cmd in ("echo x > out.txt", "git status >> log.txt", "git status 2> err.txt"):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)

    def test_blocks_git_env_escapes(self):
        for cmd in ("GIT_DIR=/tmp/git git status", "GIT_WORK_TREE=/tmp git status"):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)

    def test_blocks_powershell_mutators(self):
        for cmd in (
            "Set-Content x.txt hi",
            "Remove-Item x.txt",
            "Out-File x.txt",
            "Move-Item a b",
            "Copy-Item a b",
            "New-Item x.txt",
        ):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)

    def test_blocks_shell_and_interpreter_escapes(self):
        for cmd in (
            "Start-Process notepad",
            "cmd /c del x",
            "powershell -Command Remove-Item x",
            "python -c \"open('x','w').write('')\"",
            "node -e \"require('fs').writeFileSync('x','')\"",
            "bash -lc \"touch x\"",
        ):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)


class DirtySetTests(unittest.TestCase):
    def test_read_only_mode_rejects_any_dirty(self):
        policy = wp.default_scratch_readonly_policy()
        result = wpr.verify_dirty_set(
            porcelain_output=" M Task.md\n",
            policy=policy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:unauthorized_dirty_tree")

    def test_docs_only_flags_path_outside_allowlist(self):
        policy = {
            "mode": "docs-only",
            "write_files": ["Task.md", "docs/STATE.md"],
            "forbidden_paths": ["src/**"],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" M src/App.tsx\n",
            policy=policy,
        )
        self.assertFalse(result.ok)

    def test_docs_only_allows_allowlisted_path(self):
        policy = {
            "mode": "docs-only",
            "write_files": ["Task.md", "docs/STATE.md"],
            "forbidden_paths": ["src/**"],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" M Task.md\n?? docs/STATE.md\n",
            policy=policy,
        )
        self.assertTrue(result.ok)

    def test_deleted_file_outside_allowlist(self):
        policy = {
            "mode": "docs-only",
            "write_files": ["Task.md"],
            "forbidden_paths": [],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" D src/removed.txt\n",
            policy=policy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:unauthorized_dirty_tree")

    def test_renamed_file_outside_allowlist(self):
        policy = {
            "mode": "docs-only",
            "write_files": ["Task.md"],
            "forbidden_paths": [],
        }
        result = wpr.verify_dirty_set(
            porcelain_output="R  old.txt -> src/new.txt\n",
            policy=policy,
        )
        self.assertFalse(result.ok)

    def test_mode_only_change_outside_allowlist(self):
        policy = {
            "mode": "docs-only",
            "write_files": ["Task.md"],
            "forbidden_paths": [],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" M scripts/deploy.sh\n",
            policy=policy,
        )
        self.assertFalse(result.ok)


class EnabledWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.session = _session_with_policy()
        (self.data_dir / "session_runs.json").write_text(
            json.dumps([self.session]), encoding="utf-8",
        )
        self.ctx = wpr.build_session_queue_workspace_context(
            self.session, "reviewer", 0, 0,
        )
        self.cfg = {"workspace_policy": {"runtime_enforcement_enabled": True}}
        self.relay_meta = {
            "kind": "session_turn",
            "session_id": 42,
            "relay_mode": True,
            "disable_mcp": True,
            "channel": "sess-ch",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_enabled_valid_context_allows_exec_item(self):
        import wrapper

        item = {
            "prompt": "sealed",
            "relay_meta": self.relay_meta,
            "workspace_policy_context": self.ctx,
            "channel": "sess-ch",
        }
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertIsNone(blocker)

    def test_enabled_missing_hash_blocks(self):
        import wrapper

        bad_ctx = dict(self.ctx)
        bad_ctx.pop("policy_hash")
        item = {
            "prompt": "sealed",
            "relay_meta": self.relay_meta,
            "workspace_policy_context": bad_ctx,
        }
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertEqual(blocker, "BLOCKER:policy_hash_missing")

    def test_enabled_hash_mismatch_blocks(self):
        import wrapper

        bad_ctx = dict(self.ctx)
        bad_ctx["policy_hash"] = "0" * 64
        item = {"workspace_policy_context": bad_ctx, "relay_meta": self.relay_meta}
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertEqual(blocker, "BLOCKER:policy_hash_mismatch")

    def test_enabled_missing_snapshot_blocks(self):
        import wrapper

        (self.data_dir / "session_runs.json").write_text("[]", encoding="utf-8")
        item = {"workspace_policy_context": self.ctx, "relay_meta": self.relay_meta}
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertEqual(blocker, "BLOCKER:policy_snapshot_missing")

    def test_enabled_session_relay_missing_context_blocks(self):
        import wrapper

        item = {"prompt": "sealed", "relay_meta": self.relay_meta}
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertEqual(blocker, "BLOCKER:policy_context_missing")

    def test_non_session_mention_remains_noop(self):
        import wrapper

        item = {"prompt": "@codex hello", "channel": "general"}
        blocker = wrapper._policy_blocker_for_exec_item(
            item, data_dir=self.data_dir, config=self.cfg,
        )
        self.assertIsNone(blocker)

    def test_build_queue_work_item_preserves_non_relay_context(self):
        import wrapper

        item = wrapper._build_queue_work_item(
            "prompt",
            channel="sess-ch",
            workspace_policy_context=self.ctx,
        )
        self.assertEqual(item["workspace_policy_context"], self.ctx)
        self.assertNotIn("relay_meta", item)


class WrapperIntegrationTests(unittest.TestCase):
    def test_wrapper_verify_respects_feature_flag(self):
        import wrapper

        relay_meta = {"relay_mode": True, "disable_mcp": True, "session_id": 1}
        blocker = wrapper._verify_workspace_policy_context(
            None,
            data_dir="/tmp",
            config={"workspace_policy": {"runtime_enforcement_enabled": False}},
            relay_meta=relay_meta,
        )
        self.assertIsNone(blocker)


class ConfigFlagTests(unittest.TestCase):
    def test_runtime_enforcement_default_off(self):
        self.assertFalse(wpr.is_runtime_enforcement_enabled({}))
        self.assertFalse(wpr.is_runtime_enforcement_enabled({"workspace_policy": {}}))

    def test_config_loader_helper(self):
        import config_loader

        cfg = config_loader.load_config(ROOT)
        wp_cfg = config_loader.get_workspace_policy_config(cfg)
        self.assertFalse(wp_cfg["runtime_enforcement_enabled"])


if __name__ == "__main__":
    unittest.main()
