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


def _session_with_policy(mode="scratch-readonly", root=None, profile_id=None):
    policy = wp.default_scratch_readonly_policy()
    if mode != "scratch-readonly" or root or profile_id:
        if mode != "scratch-readonly":
            profiles = {
                "twinpet-pos": {
                    "workspace_root": root or TWINPET,
                    "allowed_modes": ["read-only"],
                    "default_mode": "read-only",
                    "max_mode": "read-only",
                },
            }
            pid = profile_id or "twinpet-pos"
            resolved = wp.resolve_workspace_policy(
                profiles=profiles,
                profile_id=pid,
                start_payload={"workspace_mode": mode} if mode != "scratch-readonly" else None,
            )
            if not resolved.ok:
                raise AssertionError(resolved.errors)
            policy = resolved.policy
        else:
            policy = dict(policy)
            if root:
                policy["workspace"] = dict(policy.get("workspace") or {})
                policy["workspace"]["root"] = root
    fields = wp.build_session_workspace_policy_fields(policy)
    return {
        "id": 42,
        "template_id": "test",
        "channel": "general",
        **fields,
    }


def _twinpet_profiles() -> dict:
    return {
        "twinpet-pos": {
            "workspace_root": TWINPET,
            "allowed_modes": ["read-only"],
            "default_mode": "read-only",
            "max_mode": "read-only",
            "require_git_repo": True,
            "default_forbidden_paths": [".git/**"],
        },
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

    def test_scratch_readonly_policy_stays_scratch_when_enforcement_on(self):
        policy = wp.default_scratch_readonly_policy()
        for role in ("coordinator", "developer", "reviewer", "ui_lead", "safety_gate"):
            cwd = wpr.resolve_role_cwd(policy, role, enforcement_enabled=True)
            self.assertEqual(cwd, SCRATCH)

    def test_read_only_profile_resolves_external_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp.replace("\\", "/")
            profiles = {
                "test-repo": {
                    "workspace_root": root,
                    "allowed_modes": ["read-only"],
                    "default_mode": "read-only",
                    "max_mode": "read-only",
                },
            }
            resolved = wp.resolve_workspace_policy(
                profiles=profiles,
                profile_id="test-repo",
                start_payload={"workspace_mode": "read-only"},
            )
            self.assertTrue(resolved.ok, resolved.errors)
            policy = resolved.policy
            for role in ("coordinator", "developer", "reviewer", "ui_lead", "safety_gate"):
                cwd = wpr.resolve_role_cwd(
                    policy, role, enforcement_enabled=True, profiles=profiles,
                )
                self.assertEqual(Path(cwd), Path(root).resolve())

    def test_twinpet_read_only_resolves_when_path_exists(self):
        if not Path(TWINPET).is_dir():
            self.skipTest("Twinpet path not present on this machine")
        resolved = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="twinpet-pos",
            start_payload={"workspace_mode": "read-only"},
        )
        self.assertTrue(resolved.ok, resolved.errors)
        policy = resolved.policy
        cwd = wpr.resolve_role_cwd(
            policy, "reviewer",
            enforcement_enabled=True,
            profiles=_twinpet_profiles(),
        )
        self.assertEqual(Path(cwd), Path(TWINPET).resolve())

    def test_read_only_rejects_mismatched_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp.replace("\\", "/")
            profiles = {"test-repo": {
                "workspace_root": root,
                "allowed_modes": ["read-only"],
                "default_mode": "read-only",
                "max_mode": "read-only",
            }}
            resolved = wp.resolve_workspace_policy(
                profiles=profiles,
                profile_id="test-repo",
                start_payload={"workspace_mode": "read-only"},
            )
            policy = dict(resolved.policy)
            policy["workspace"] = dict(policy.get("workspace") or {})
            policy["workspace"]["root"] = "C:/other/path"
            cwd = wpr.resolve_role_cwd(
                policy, "developer", enforcement_enabled=True, profiles=profiles,
            )
            self.assertEqual(cwd, SCRATCH)

    def test_docs_only_mode_does_not_route_external_cwd(self):
        profiles = {
            "example": {
                "workspace_root": "C:/tmp/example",
                "allowed_modes": ["read-only", "docs-only"],
                "default_mode": "read-only",
                "max_mode": "docs-only",
                "allowed_write_files": ["Task.md"],
                "default_write_files": ["Task.md"],
            },
        }
        resolved = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="example",
            start_payload={"workspace_mode": "docs-only", "write_files": ["Task.md"]},
        )
        self.assertTrue(resolved.ok, resolved.errors)
        cwd = wpr.resolve_role_cwd(
            resolved.policy, "developer", enforcement_enabled=True, profiles=profiles,
        )
        self.assertEqual(cwd, SCRATCH)


class TwinpetProfilePolicyTests(unittest.TestCase):
    def test_twinpet_profile_resolves_read_only(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="twinpet-pos",
            start_payload={"workspace_mode": "read-only"},
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "read-only")
        self.assertEqual(
            wpr.normalize_workspace_root(result.policy["workspace"]["root"]),
            wpr.normalize_workspace_root(TWINPET),
        )
        self.assertEqual(result.policy.get("write_files"), [])

    def test_twinpet_docs_only_rejected(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="twinpet-pos",
            start_payload={"workspace_mode": "docs-only"},
        )
        self.assertFalse(result.ok)

    def test_twinpet_write_files_rejected(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="twinpet-pos",
            start_payload={"workspace_mode": "read-only", "write_files": ["Task.md"]},
        )
        self.assertFalse(result.ok)

    def test_unknown_profile_fails_closed(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="unknown-repo",
            start_payload={"workspace_mode": "read-only"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "UNKNOWN_PROFILE")

    def test_arbitrary_workspace_root_in_payload_rejected(self):
        payload, err = wp.extract_policy_start_fields(
            {"workspace_root": "C:/Users/Evil/other"},
        )
        self.assertIsNone(payload)
        self.assertIn("workspace_root", err or "")


class ExecCwdResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.external = Path(self.tmp.name) / "external-repo"
        self.external.mkdir()
        self.root = str(self.external).replace("\\", "/")
        self.profiles = {
            "test-repo": {
                "workspace_root": self.root,
                "allowed_modes": ["read-only"],
                "default_mode": "read-only",
                "max_mode": "read-only",
            },
        }
        resolved = wp.resolve_workspace_policy(
            profiles=self.profiles,
            profile_id="test-repo",
            start_payload={"workspace_mode": "read-only"},
        )
        self.policy = resolved.policy
        fields = wp.build_session_workspace_policy_fields(self.policy)
        self.session = {"id": 7, "template_id": "test", **fields}
        (self.data_dir / "session_runs.json").write_text(
            json.dumps([self.session]), encoding="utf-8",
        )
        self.ctx = wpr.build_session_queue_workspace_context(
            self.session, "reviewer", 0, 0,
        )
        self.cfg = {
            "workspace_policy": {
                "runtime_enforcement_enabled": False,
                "read_only_external_cwd_enabled": True,
            },
            "workspace_profiles": self.profiles,
        }
        self.default_cwd = SCRATCH

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_verified_read_only_session_to_external_root(self):
        item = {
            "prompt": "sealed",
            "relay_meta": {"session_id": 7, "relay_mode": True, "disable_mcp": True},
            "workspace_policy_context": self.ctx,
        }
        cwd = wpr.resolve_exec_cwd_for_item(
            item,
            data_dir=self.data_dir,
            config=self.cfg,
            default_cwd=self.default_cwd,
            profiles=self.profiles,
        )
        self.assertEqual(Path(cwd), self.external.resolve())

    def test_flag_off_keeps_default_cwd(self):
        item = {
            "workspace_policy_context": self.ctx,
            "relay_meta": {"session_id": 7, "relay_mode": True, "disable_mcp": True},
        }
        cfg = {"workspace_policy": {"read_only_external_cwd_enabled": False}}
        cwd = wpr.resolve_exec_cwd_for_item(
            item, data_dir=self.data_dir, config=cfg,
            default_cwd=self.default_cwd, profiles=self.profiles,
        )
        self.assertEqual(cwd, self.default_cwd)

    def test_hash_mismatch_falls_back_to_default_cwd(self):
        bad_ctx = dict(self.ctx)
        bad_ctx["policy_hash"] = "0" * 64
        item = {"workspace_policy_context": bad_ctx}
        cwd = wpr.resolve_exec_cwd_for_item(
            item, data_dir=self.data_dir, config=self.cfg,
            default_cwd=self.default_cwd, profiles=self.profiles,
        )
        self.assertEqual(cwd, self.default_cwd)

    def test_wrapper_resolve_exec_cwd_helper(self):
        import wrapper

        item = {
            "workspace_policy_context": self.ctx,
            "relay_meta": {"session_id": 7, "relay_mode": True, "disable_mcp": True},
        }
        cwd = wrapper._resolve_exec_cwd(
            item, data_dir=self.data_dir, config=self.cfg, default_cwd=self.default_cwd,
        )
        self.assertEqual(Path(cwd), self.external.resolve())


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

    def test_read_only_external_cwd_default_off(self):
        self.assertFalse(wpr.is_read_only_external_cwd_enabled({}))
        self.assertFalse(wpr.is_read_only_external_cwd_enabled({"workspace_policy": {}}))

    def test_scoped_write_external_cwd_default_off(self):
        self.assertFalse(wpr.is_scoped_write_external_cwd_enabled({}))
        self.assertFalse(wpr.is_scoped_write_external_cwd_enabled({"workspace_policy": {}}))

    def test_config_loader_helper(self):
        import config_loader

        cfg = config_loader.load_config(ROOT)
        wp_cfg = config_loader.get_workspace_policy_config(cfg)
        self.assertFalse(wp_cfg["runtime_enforcement_enabled"])
        self.assertTrue(wp_cfg["read_only_external_cwd_enabled"])
        self.assertTrue(wp_cfg["scoped_write_external_cwd_enabled"])
        self.assertIn("twinpet-pos", config_loader.get_workspace_profiles(cfg))
        self.assertIn(
            "twinpet-ui-09-c-payment-modal-write",
            config_loader.get_workspace_profiles(cfg),
        )


TWINPET_WRITE_FILES = [
    "src/components/PaymentModal.tsx",
    "src/components/PaymentModal.css",
    "tests/pos-human-checkout.spec.ts",
    "Task.md",
    "Context.md",
    "docs/reports/latest-report.md",
]


def _twinpet_scoped_write_profiles() -> dict:
    return {
        "twinpet-ui-09-c-payment-modal-write": {
            "workspace_root": TWINPET,
            "allowed_modes": ["read-only", "implementation"],
            "default_mode": "implementation",
            "max_mode": "implementation",
            "require_git_repo": True,
            "allowed_write_files": list(TWINPET_WRITE_FILES),
            "default_write_files": list(TWINPET_WRITE_FILES),
            "max_write_files": 6,
            "default_forbidden_paths": [
                "src/pages/POSPage.tsx",
                "src/hooks/pos/useCheckout.ts",
                "src/lib/pos/asyncCheckout.ts",
                "src/lib/pos/cartUtils.ts",
                "functions/**",
                "firebase.json",
                "firestore.rules",
                "storage.rules",
                "android/**",
                "ios/**",
                ".claude/**",
                "config.local.toml",
                ".git/**",
            ],
        },
    }


class TwinpetScopedWriteProfileTests(unittest.TestCase):
    def test_profile_loads_from_config(self):
        import config_loader

        profiles = config_loader.get_workspace_profiles(config_loader.load_config(ROOT))
        profile = profiles["twinpet-ui-09-c-payment-modal-write"]
        self.assertEqual(profile["default_mode"], "implementation")
        self.assertEqual(profile["allowed_write_files"], TWINPET_WRITE_FILES)

    def test_scoped_write_alias_resolves_to_implementation(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="twinpet-ui-09-c-payment-modal-write",
            start_payload={
                "workspace_mode": "scoped-write",
                "expected_head": "752ed1317a5e0b83b872d563cda451c7621ed22e",
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "implementation")
        self.assertEqual(result.policy["write_files"], TWINPET_WRITE_FILES)
        self.assertEqual(
            result.policy["workspace"]["expected_head"],
            "752ed1317a5e0b83b872d563cda451c7621ed22e",
        )

    def test_read_only_profile_unchanged(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_profiles(),
            profile_id="twinpet-pos",
            start_payload={"workspace_mode": "read-only"},
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy.get("write_files"), [])

    def test_rejects_pos_page_write_files_payload(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="twinpet-ui-09-c-payment-modal-write",
            start_payload={
                "workspace_mode": "implementation",
                "write_files": ["src/pages/POSPage.tsx"],
            },
        )
        self.assertFalse(result.ok)

    def test_rejects_wildcard_write_files(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="twinpet-ui-09-c-payment-modal-write",
            start_payload={
                "workspace_mode": "implementation",
                "write_files": ["src/components/*"],
            },
        )
        self.assertFalse(result.ok)

    def test_rejects_arbitrary_workspace_root(self):
        payload, err = wp.extract_policy_start_fields(
            {"workspace_root": "C:/Users/Evil/other"},
        )
        self.assertIsNone(payload)
        self.assertIn("workspace_root", err or "")

    def test_rejects_unknown_profile(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="unknown-profile",
            start_payload={"workspace_mode": "implementation"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "UNKNOWN_PROFILE")

    def test_rejects_docs_only_on_scoped_write_profile(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="twinpet-ui-09-c-payment-modal-write",
            start_payload={"workspace_mode": "docs-only"},
        )
        self.assertFalse(result.ok)

    def test_local_override_cannot_broaden_write_files(self):
        result = wp.resolve_workspace_policy(
            profiles=_twinpet_scoped_write_profiles(),
            profile_id="twinpet-ui-09-c-payment-modal-write",
            local_override={"write_files": ["src/pages/POSPage.tsx"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "LOCAL_BROADEN_REJECTED")

    def test_implementation_resolves_external_cwd_for_developer(self):
        if not Path(TWINPET).is_dir():
            self.skipTest("Twinpet path not present on this machine")
        profiles = _twinpet_scoped_write_profiles()
        resolved = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="twinpet-ui-09-c-payment-modal-write",
            start_payload={"workspace_mode": "scoped-write"},
        )
        self.assertTrue(resolved.ok, resolved.errors)
        cwd = wpr.resolve_role_cwd(
            resolved.policy,
            "developer",
            enforcement_enabled=True,
            profiles=profiles,
        )
        self.assertEqual(Path(cwd), Path(TWINPET).resolve())

    def test_exec_cwd_requires_scoped_write_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            profiles = {
                "write-repo": {
                    "workspace_root": str(root).replace("\\", "/"),
                    "allowed_modes": ["implementation"],
                    "default_mode": "implementation",
                    "max_mode": "implementation",
                    "allowed_write_files": ["Task.md"],
                    "default_write_files": ["Task.md"],
                },
            }
            resolved = wp.resolve_workspace_policy(
                profiles=profiles,
                profile_id="write-repo",
            )
            fields = wp.build_session_workspace_policy_fields(resolved.policy)
            session = {"id": 9, **fields}
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "session_runs.json").write_text(
                json.dumps([session]), encoding="utf-8",
            )
            ctx = wpr.build_session_queue_workspace_context(
                session, "developer", 0, 0,
            )
            item = {"workspace_policy_context": ctx}
            cfg_off = {"workspace_policy": {"scoped_write_external_cwd_enabled": False}}
            self.assertEqual(
                wpr.resolve_exec_cwd_for_item(
                    item, data_dir=data_dir, config=cfg_off,
                    default_cwd=SCRATCH, profiles=profiles,
                ),
                SCRATCH,
            )
            cfg_on = {"workspace_policy": {"scoped_write_external_cwd_enabled": True}}
            self.assertEqual(
                Path(wpr.resolve_exec_cwd_for_item(
                    item, data_dir=data_dir, config=cfg_on,
                    default_cwd=SCRATCH, profiles=profiles,
                )),
                root.resolve(),
            )

    def test_dirty_set_allows_allowlisted_files(self):
        policy = {
            "mode": "implementation",
            "write_files": TWINPET_WRITE_FILES,
            "forbidden_paths": ["src/pages/POSPage.tsx"],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" M src/components/PaymentModal.tsx\n",
            policy=policy,
        )
        self.assertTrue(result.ok)

    def test_dirty_set_blocks_red_zone_files(self):
        policy = {
            "mode": "implementation",
            "write_files": TWINPET_WRITE_FILES,
            "forbidden_paths": ["src/pages/POSPage.tsx"],
        }
        result = wpr.verify_dirty_set(
            porcelain_output=" M src/pages/POSPage.tsx\n",
            policy=policy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, "BLOCKER:unauthorized_dirty_tree")

    def test_git_write_commands_remain_blocked(self):
        for cmd in ("git add .", "git commit -m x", "git push"):
            result = wpr.check_command_guard(cmd)
            self.assertFalse(result.ok, cmd)


if __name__ == "__main__":
    unittest.main()
