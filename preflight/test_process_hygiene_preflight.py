"""Unit tests for fail-closed process hygiene preflight (isolated, no live runtime)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from preflight.checks import (  # noqa: E402
    CheckResult,
    GitSnapshot,
    PortListener,
    PreflightContext,
    ProcessInfo,
    STATUS_BLOCKED,
    VERDICT_BLOCKED,
    VERDICT_PASS,
)
from preflight.redaction import scrub_preflight_output  # noqa: E402
from preflight.runner import (  # noqa: E402
    EXIT_INTERNAL,
    PreflightReport,
    format_human,
    format_json,
    run_preflight,
)
from preflight_manifest import MANIFEST_DIR, load_manifest, validate_manifest_dict  # noqa: E402


def _clean_git() -> GitSnapshot:
    return GitSnapshot(
        branch="main",
        head="3ed690e",
        remote_head="3ed690e",
        porcelain="",
        staged_names=[],
        ahead_lines=[],
        behind_lines=[],
        config_local_ignored_line="!! config.local.toml",
    )


def _e4c_wrappers_running(*, include_server: bool = True) -> list[ProcessInfo]:
    procs = [
        ProcessInfo(100, r"C:\tools\agentchattr\repo\.venv\python.exe wrapper.py codex"),
        ProcessInfo(101, r"C:\tools\agentchattr\repo\.venv\python.exe wrapper.py codex_reviewer"),
        ProcessInfo(102, r"C:\tools\agentchattr\repo\.venv\python.exe wrapper.py codexsafe"),
    ]
    if include_server:
        procs.insert(0, ProcessInfo(99, r"C:\tools\agentchattr\repo\.venv\python.exe run.py"))
    return procs


def _e6c_wrappers_running(*, include_server: bool = True) -> list[ProcessInfo]:
    procs = [
        ProcessInfo(100, r"C:\tools\agentchattr\repo\.venv\python.exe wrapper.py agy"),
    ]
    if include_server:
        procs.insert(0, ProcessInfo(99, r"C:\tools\agentchattr\repo\.venv\python.exe run.py"))
    return procs


def _e6c_channels() -> list[str]:
    return ["general", "relay-dryrun", "sdlc-dryrun", "agy-live-validation"]


def _write_settings(data_dir: Path, channels: list[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"channels": channels}),
        encoding="utf-8",
    )


def _write_sessions(data_dir: Path, sessions: list[dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "session_runs.json").write_text(
        json.dumps(sessions),
        encoding="utf-8",
    )


def _base_ctx(
    data_dir: Path,
    *,
    git_snapshot: GitSnapshot | None = None,
    processes: list[ProcessInfo] | None = None,
    port_listeners: list[PortListener] | None = None,
) -> PreflightContext:
    return PreflightContext(
        repo_root=ROOT,
        data_dir=data_dir,
        git_snapshot=git_snapshot or _clean_git(),
        processes=processes if processes is not None else _e4c_wrappers_running(),
        port_listeners=port_listeners if port_listeners is not None else [PortListener(8300, 99)],
        config_toml_path=ROOT / "config.toml",
        config_local_path=data_dir / "missing_config.local.toml",
    )


def _base_e6c_ctx(
    data_dir: Path,
    *,
    git_snapshot: GitSnapshot | None = None,
    processes: list[ProcessInfo] | None = None,
    port_listeners: list[PortListener] | None = None,
) -> PreflightContext:
    return PreflightContext(
        repo_root=ROOT,
        data_dir=data_dir,
        git_snapshot=git_snapshot or _clean_git(),
        processes=processes if processes is not None else _e6c_wrappers_running(),
        port_listeners=port_listeners if port_listeners is not None else [PortListener(8300, 99)],
        config_toml_path=ROOT / "config.toml",
        config_local_path=data_dir / "missing_config.local.toml",
    )


class ManifestTests(unittest.TestCase):
    def test_e4c_manifest_loads(self):
        manifest, err = load_manifest("E4C_SDLC_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest.phase_id, "E4C_SDLC_LIVE")
        self.assertIn("codex_reviewer", manifest.wrappers["allowed"])

    def test_unknown_phase_blocked(self):
        report = run_preflight(
            "UNKNOWN_PHASE_XYZ",
            ctx=PreflightContext(repo_root=ROOT, git_snapshot=_clean_git(), processes=[], port_listeners=[]),
        )
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "manifest.known_phase" for c in report.checks))

    def test_missing_required_manifest_field_blocked(self):
        manifest, err = validate_manifest_dict({"phase_id": "INCOMPLETE"})
        self.assertIsNone(manifest)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("missing required manifest keys", err)


class GitPreflightTests(unittest.TestCase):
    def _run(self, snap: GitSnapshot) -> object:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            return run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data, git_snapshot=snap))

    def test_git_clean_passes(self):
        report = self._run(_clean_git())
        self.assertEqual(report.verdict, VERDICT_PASS)
        ids = {c.id for c in report.checks}
        self.assertIn("git.clean_tree", ids)
        self.assertEqual(
            next(c for c in report.checks if c.id == "git.clean_tree").status,
            "PASS",
        )

    def test_git_dirty_blocked(self):
        snap = _clean_git()
        snap.porcelain = " M app.py"
        report = self._run(snap)
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.clean_tree" and c.status == "BLOCKED" for c in report.checks))

    def test_git_untracked_non_ignored_blocked(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/new_file.py"
        report = self._run(snap)
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        blocked = next(c for c in report.checks if c.id == "git.clean_tree")
        self.assertEqual(blocked.status, STATUS_BLOCKED)
        self.assertIn("untracked", blocked.detail.lower())

    def test_git_ignored_only_still_clean(self):
        snap = _clean_git()
        snap.porcelain = "!! config.local.toml"
        report = self._run(snap)
        clean = next(c for c in report.checks if c.id == "git.clean_tree")
        self.assertEqual(clean.status, "PASS")

    def test_git_ahead_blocked(self):
        snap = _clean_git()
        snap.ahead_lines = ["abc1234 ahead commit"]
        report = self._run(snap)
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.sync" and c.status == "BLOCKED" for c in report.checks))

    def test_git_behind_blocked(self):
        snap = _clean_git()
        snap.behind_lines = ["def5678 behind commit"]
        report = self._run(snap)
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.sync" and c.status == "BLOCKED" for c in report.checks))


class WrapperPreflightTests(unittest.TestCase):
    def _ctx(self, processes: list[ProcessInfo], data_dir: Path) -> PreflightContext:
        _write_settings(data_dir, ["general", "sdlc-dryrun"])
        return _base_ctx(data_dir, processes=processes)

    def test_forbidden_wrapper_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            procs = _e4c_wrappers_running()
            procs.append(ProcessInfo(200, r"python wrapper.py agy"))
            report = run_preflight("E4C_SDLC_LIVE", ctx=self._ctx(procs, Path(tmp)))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.forbidden" for c in report.checks))

    def test_required_wrapper_missing_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            procs = [
                ProcessInfo(99, r"python run.py"),
                ProcessInfo(100, r"python wrapper.py codex"),
            ]
            report = run_preflight("E4C_SDLC_LIVE", ctx=self._ctx(procs, Path(tmp)))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.required" for c in report.checks))


class SessionPreflightTests(unittest.TestCase):
    def test_active_sessions_over_threshold_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            _write_sessions(data, [
                {"id": 1, "state": "active", "channel": "sdlc-dryrun"},
            ])
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "sessions.active_count" for c in report.checks))


class ChannelPreflightTests(unittest.TestCase):
    def test_required_channel_missing_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general"])
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "channels.required" for c in report.checks))


class SandboxPreflightTests(unittest.TestCase):
    def test_sandbox_audit_activity_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            data.mkdir(parents=True, exist_ok=True)
            (data / "sandbox_flow_audit.jsonl").write_text(
                '{"result":"reject"}\n',
                encoding="utf-8",
            )
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "sandbox.audit" for c in report.checks))


class PortPreflightTests(unittest.TestCase):
    def test_multiple_listeners_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            ctx = _base_ctx(
                data,
                port_listeners=[
                    PortListener(8300, 10),
                    PortListener(8300, 11),
                ],
            )
            report = run_preflight("E4C_SDLC_LIVE", ctx=ctx)
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "port.listeners" for c in report.checks))


class RedactionTests(unittest.TestCase):
    def test_redactor_masks_64_hex_session_token(self):
        token = "a" * 64
        out = scrub_preflight_output(token)
        self.assertNotIn(token, out)
        self.assertIn("[REDACTED_TOKEN]", out)

    def test_redactor_masks_github_pat_and_bearer(self):
        pat = "ghp_" + "x" * 36
        out = scrub_preflight_output(f"pat={pat} Bearer secretvalue12345678")
        self.assertNotIn(pat, out)
        self.assertIn("[REDACTED]", out)

    def test_redactor_masks_api_key_style(self):
        out = scrub_preflight_output("api_key=supersecretvalue")
        self.assertNotIn("supersecretvalue", out)

    def test_json_output_redacts_token_in_check_detail(self):
        token = "b" * 64
        report = PreflightReport(
            phase="TEST",
            verdict=VERDICT_BLOCKED,
            checks=[CheckResult("test.secret", STATUS_BLOCKED, f"leaked {token}")],
            blocked_reasons=[f"test.secret: leaked {token}"],
            exit_code=1,
        )
        payload = json.loads(format_json(report))
        self.assertNotIn(token, json.dumps(payload))
        self.assertIn("[REDACTED_TOKEN]", payload["checks"][0]["detail"])

    def test_human_output_redacts_token_in_check_detail(self):
        token = "c" * 64
        report = PreflightReport(
            phase="TEST",
            verdict=VERDICT_BLOCKED,
            checks=[CheckResult("test.secret", STATUS_BLOCKED, f"leaked {token}")],
            blocked_reasons=[f"test.secret: leaked {token}"],
            exit_code=1,
        )
        human = format_human(report)
        self.assertNotIn(token, human)
        self.assertIn("[REDACTED_TOKEN]", human)

    def test_human_output_has_no_raw_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            snap = _clean_git()
            snap.head = "a" * 64
            report = run_preflight(
                "E4C_SDLC_LIVE",
                ctx=_base_ctx(data, git_snapshot=snap),
            )
        human = format_human(report)
        secret = "ghp_" + "z" * 36
        self.assertNotIn(secret, human)
        injected = scrub_preflight_output(secret)
        self.assertIn("[REDACTED]", injected)


class InternalErrorTests(unittest.TestCase):
    def test_internal_error_exit_code_two(self):
        with patch("preflight.runner.run_all_checks", side_effect=RuntimeError("boom")):
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                _write_settings(data, ["general", "sdlc-dryrun"])
                report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertEqual(report.exit_code, EXIT_INTERNAL)
        self.assertTrue(any(c.id == "preflight.internal" for c in report.checks))


class OutputSchemaTests(unittest.TestCase):
    def test_json_output_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "sdlc-dryrun"])
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        payload = json.loads(format_json(report))
        self.assertEqual(payload["phase"], "E4C_SDLC_LIVE")
        self.assertIn(payload["verdict"], (VERDICT_PASS, VERDICT_BLOCKED))
        self.assertIsInstance(payload["checks"], list)
        self.assertIsInstance(payload["blocked_reasons"], list)
        for check in payload["checks"]:
            self.assertIn("id", check)
            self.assertIn("status", check)
            self.assertIn("detail", check)

    def test_full_e4c_fixture_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "relay-dryrun", "sdlc-dryrun"])
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data))
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertEqual(report.exit_code, 0)


def _git_only_ctx(snap: GitSnapshot | None = None, *, tmp: Path | None = None) -> PreflightContext:
    local_path = (tmp / "missing_config.local.toml") if tmp else (ROOT / "missing_config.local.toml")
    return PreflightContext(
        repo_root=ROOT,
        git_snapshot=snap or _clean_git(),
        processes=[],
        port_listeners=[],
        config_toml_path=ROOT / "config.toml",
        config_local_path=local_path,
    )


def _write_manifest_file(manifest_dir: Path, manifest: dict) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{manifest['phase_id']}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _git_only_manifest_skeleton(phase_id: str, *, git_extra: dict | None = None) -> dict:
    git = {
        "expected_branch": "main",
        "require_clean_tree": True,
        "require_no_staged": True,
        "require_synced_with_remote": True,
        "expected_remote_ref": "origin/main",
        "require_config_local_ignored": True,
    }
    if git_extra:
        git.update(git_extra)
    return {
        "phase_id": phase_id,
        "description": "test manifest",
        "require_runtime_checks": False,
        "git": git,
        "wrappers": {"allowed": [], "forbidden": [], "require_all_allowed_running": False},
        "sessions": {"max_active_count": 0, "active_states": []},
        "channels": {"required": [], "protected_expectation": [], "forbid_general_session_leak_count": False},
        "sandbox": {"forbid_flow_enabled": False, "forbid_audit_activity": False},
        "network": {"expected_port": 8300, "max_listeners": 99, "require_server_when_wrappers_required": False},
        "redaction": {"require_self_test": True},
        "general_fallback_forbidden": True,
    }


class ReadOnlyAuditManifestTests(unittest.TestCase):
    def test_read_only_audit_passes(self):
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx())
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertTrue(any(c.id == "runtime.skipped" for c in report.checks))
        self.assertFalse(any(c.id.startswith("wrappers.") for c in report.checks))

    def test_read_only_audit_blocks_wrong_branch(self):
        snap = _clean_git()
        snap.branch = "feature/x"
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.branch" for c in report.checks))

    def test_read_only_audit_blocks_sync_drift_ahead(self):
        snap = _clean_git()
        snap.ahead_lines = ["abc commit"]
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.sync" for c in report.checks))

    def test_read_only_audit_blocks_sync_drift_behind(self):
        snap = _clean_git()
        snap.behind_lines = ["def commit"]
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)

    def test_read_only_audit_blocks_tracked_dirty(self):
        snap = _clean_git()
        snap.porcelain = " M app.py"
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.clean_tree" for c in report.checks))

    def test_read_only_audit_blocks_untracked(self):
        snap = _clean_git()
        snap.porcelain = "?? scratch.txt"
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)

    def test_read_only_audit_blocks_staged(self):
        snap = _clean_git()
        snap.staged_names = ["app.py"]
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.staged" for c in report.checks))

    def test_read_only_audit_no_runtime_requirements(self):
        report = run_preflight("READ_ONLY_AUDIT", ctx=_git_only_ctx())
        ids = {c.id for c in report.checks}
        self.assertNotIn("port.listeners", ids)
        self.assertNotIn("wrappers.required", ids)
        self.assertNotIn("channels.required", ids)
        self.assertNotIn("sessions.active_count", ids)


class CodexDirtyTreeManifestTests(unittest.TestCase):
    def _run_codex(self, snap: GitSnapshot, allowlist: list[str]) -> PreflightReport:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _git_only_manifest_skeleton(
                "CODEX_REVIEW_DIRTY_TREE",
                git_extra={
                    "require_clean_tree": False,
                    "approved_dirty_paths": allowlist,
                },
            )
            manifest["sandbox"]["forbid_flow_enabled"] = True
            mdir = Path(tmp)
            _write_manifest_file(mdir, manifest)
            return run_preflight(
                "CODEX_REVIEW_DIRTY_TREE",
                ctx=_git_only_ctx(snap, tmp=mdir),
                manifest_dir=mdir,
            )

    def test_codex_allows_scoped_dirty_files(self):
        allowlist = ["preflight/checks.py", "preflight/runner.py"]
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py\n M preflight/runner.py"
        report = self._run_codex(snap, allowlist)
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertTrue(any(c.id == "git.dirty_allowlist" for c in report.checks))

    def test_codex_blocks_unauthorized_dirty_files(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py\n?? app.py"
        report = self._run_codex(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.dirty_allowlist" for c in report.checks))

    def test_codex_blocks_staged_when_forbidden(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py"
        snap.staged_names = ["preflight/checks.py"]
        report = self._run_codex(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.staged" for c in report.checks))

    def test_codex_empty_allowlist_blocks(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py"
        report = self._run_codex(snap, [])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)


class CommitExactFilesManifestTests(unittest.TestCase):
    def _run_commit(self, snap: GitSnapshot, allowlist: list[str]) -> PreflightReport:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _git_only_manifest_skeleton(
                "COMMIT_EXACT_FILES",
                git_extra={
                    "require_clean_tree": False,
                    "require_exact_dirty_set": True,
                    "approved_file_allowlist": allowlist,
                },
            )
            manifest["sandbox"]["forbid_flow_enabled"] = True
            mdir = Path(tmp)
            _write_manifest_file(mdir, manifest)
            return run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap, tmp=mdir),
                manifest_dir=mdir,
            )

    def test_commit_passes_exact_allowlist(self):
        allowlist = ["preflight/checks.py", "preflight_manifest.py"]
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py\n?? preflight_manifest.py"
        report = self._run_commit(snap, allowlist)
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertTrue(any(c.id == "git.exact_dirty_set" for c in report.checks))

    def test_commit_blocks_extra_files(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py\n?? extra.py"
        report = self._run_commit(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)

    def test_commit_blocks_staged_before_staging(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py"
        snap.staged_names = ["preflight/checks.py"]
        report = self._run_commit(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.staged" for c in report.checks))

    def test_commit_blocks_protected_config_path(self):
        snap = _clean_git()
        snap.porcelain = " M config.toml"
        report = self._run_commit(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.protected_path" for c in report.checks))

    def test_commit_blocks_data_path_without_allowlist(self):
        snap = _clean_git()
        snap.porcelain = "?? data/settings.json"
        report = self._run_commit(snap, ["preflight/checks.py"])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(
            c.id in ("git.exact_dirty_set", "git.protected_path")
            for c in report.checks
        ))

    def test_commit_empty_allowlist_blocks(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py"
        report = self._run_commit(snap, [])
        self.assertEqual(report.verdict, VERDICT_BLOCKED)


class PushOnlyManifestTests(unittest.TestCase):
    def test_push_only_passes_one_commit_ahead(self):
        snap = _clean_git()
        snap.head = "4000008"
        snap.remote_head = "3ed690e"
        snap.ahead_lines = ["4000008 feat(tooling): add fail-closed preflight checker"]
        report = run_preflight("PUSH_ONLY", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertTrue(any(c.id == "git.ahead" for c in report.checks))

    def test_push_only_blocks_behind_remote(self):
        snap = _clean_git()
        snap.behind_lines = ["older commit"]
        report = run_preflight("PUSH_ONLY", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.sync" for c in report.checks))

    def test_push_only_blocks_unexpected_ahead_count(self):
        snap = _clean_git()
        snap.ahead_lines = ["a", "b"]
        report = run_preflight("PUSH_ONLY", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "git.ahead" for c in report.checks))

    def test_push_only_blocks_dirty_tree(self):
        snap = _clean_git()
        snap.ahead_lines = ["4000008 commit"]
        snap.porcelain = "?? leftover.txt"
        report = run_preflight("PUSH_ONLY", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)

    def test_push_only_blocks_staged_files(self):
        snap = _clean_git()
        snap.ahead_lines = ["4000008 commit"]
        snap.staged_names = ["app.py"]
        report = run_preflight("PUSH_ONLY", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)


class AllowlistSidecarTests(unittest.TestCase):
    def _write_sidecar(self, path: Path, payload: dict) -> Path:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _codex_sidecar(self, paths: list[str], **extra: object) -> dict:
        data = {
            "manifest_id": "CODEX_REVIEW_DIRTY_TREE",
            "authorization_phase_id": "TOOLING-TEST-CODEX",
            "approved_dirty_paths": paths,
        }
        data.update(extra)
        return data

    def _commit_sidecar(self, paths: list[str], **extra: object) -> dict:
        data = {
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TOOLING-TEST-COMMIT",
            "approved_file_allowlist": paths,
        }
        data.update(extra)
        return data

    def test_sidecar_pass_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "codex.json",
                self._codex_sidecar(["preflight/checks.py", "preflight/runner.py"]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py\n M preflight/runner.py"
            report = run_preflight(
                "CODEX_REVIEW_DIRTY_TREE",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertIsNotNone(report.allowlist)
        self.assertEqual(report.allowlist["path_count"], 2)

    def test_sidecar_pass_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "commit.json",
                self._commit_sidecar(["preflight/checks.py", "preflight_manifest.py"]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py\n?? preflight_manifest.py"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertTrue(any(c.id == "git.exact_dirty_set" for c in report.checks))

    def test_sidecar_manifest_id_mismatch_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "bad.json",
                self._commit_sidecar(["preflight/checks.py"]),
            )
            report = run_preflight(
                "CODEX_REVIEW_DIRTY_TREE",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.manifest_mismatch" for c in report.checks))

    def test_sidecar_wrong_field_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "bad.json",
                {
                    "manifest_id": "COMMIT_EXACT_FILES",
                    "authorization_phase_id": "TOOLING-TEST-COMMIT",
                    "approved_dirty_paths": ["preflight/checks.py"],
                },
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.field_forbidden" for c in report.checks))

    def test_sidecar_missing_file_blocked(self):
        report = run_preflight(
            "COMMIT_EXACT_FILES",
            ctx=_git_only_ctx(),
            allowlist_file=ROOT / "does-not-exist-allowlist.json",
        )
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.file_missing" for c in report.checks))

    def test_sidecar_invalid_json_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "bad.json"
            sidecar.write_text("{not json", encoding="utf-8")
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.invalid_json" for c in report.checks))

    def test_sidecar_empty_list_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "empty.json",
                self._commit_sidecar([]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_wildcard_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "wild.json",
                self._commit_sidecar(["preflight/*.py"]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "abs.json",
                self._commit_sidecar(["/etc/passwd"]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "trav.json",
                self._commit_sidecar(["../outside.py"]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_unc_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "unc.json",
                self._commit_sidecar(["\\\\server\\share\\file.py"]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_windows_drive_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "drive.json",
                self._commit_sidecar(["C:\\Windows\\System32\\cmd.exe"]),
            )
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_backslash_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "win.json",
                self._commit_sidecar(["preflight\\checks.py"]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_PASS)

    def test_sidecar_unknown_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._commit_sidecar(["preflight/checks.py"])
            payload["force_add_paths"] = ["docs/x.md"]
            sidecar = self._write_sidecar(Path(tmp) / "unknown.json", payload)
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_unsupported_manifest_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "ro.json",
                self._commit_sidecar(["preflight/checks.py"]),
            )
            report = run_preflight(
                "READ_ONLY_AUDIT",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.unsupported_manifest" for c in report.checks))

    def test_backward_compat_without_sidecar_codex_empty_blocks(self):
        snap = _clean_git()
        snap.porcelain = "?? preflight/checks.py"
        report = run_preflight("CODEX_REVIEW_DIRTY_TREE", ctx=_git_only_ctx(snap))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertIsNone(report.allowlist)
        self.assertTrue(any(c.id == "git.dirty_allowlist" for c in report.checks))

    def test_json_output_includes_allowlist_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "meta.json",
                self._commit_sidecar(["preflight/checks.py"]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
            payload = json.loads(format_json(report))
        self.assertIn("allowlist", payload)
        self.assertEqual(payload["allowlist"]["source_basename"], "meta.json")
        self.assertEqual(payload["allowlist"]["fields_applied"], ["approved_file_allowlist"])

    def test_human_output_includes_allowlist_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "human.json",
                self._commit_sidecar(["preflight/checks.py"]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
            text = format_human(report)
        self.assertIn("Allowlist: human.json", text)
        self.assertIn("authorization_phase_id=TOOLING-TEST-COMMIT", text)

    def test_force_add_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "manifest_id": "COMMIT_EXACT_FILES",
                "authorization_phase_id": "TEST",
                "approved_file_allowlist": ["preflight/checks.py"],
                "force_add_paths": ["docs/x.md"],
            }
            sidecar = self._write_sidecar(Path(tmp) / "force.json", payload)
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def _run_sidecar_raw(self, payload: dict) -> PreflightReport:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "raw.json"
            sidecar.write_text(json.dumps(payload), encoding="utf-8")
            return run_preflight(
                payload.get("manifest_id", "COMMIT_EXACT_FILES"),
                ctx=_git_only_ctx(),
                allowlist_file=sidecar,
            )

    def test_sidecar_rejects_integer_path_entry(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": [123],
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_rejects_boolean_path_entry(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": [True],
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_rejects_null_path_entry(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": [None],
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_rejects_dot_path(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": ["."],
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_rejects_bare_dotdot_path(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": [".."],
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.path_invalid" for c in report.checks))

    def test_sidecar_normalizes_leading_dot_slash_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "dot-slash.json",
                self._commit_sidecar(["./docs/preflight-workflow-gates.md"]),
            )
            snap = _clean_git()
            snap.porcelain = " M docs/preflight-workflow-gates.md"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_PASS)

    def test_sidecar_both_arrays_present_blocked(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_dirty_paths": ["preflight/checks.py"],
            "approved_file_allowlist": ["preflight/runner.py"],
        })
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_neither_array_present_blocked(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
        })
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_non_list_allowlist_blocked(self):
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": "preflight/checks.py",
        })
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))

    def test_sidecar_duplicate_paths_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self._write_sidecar(
                Path(tmp) / "dup.json",
                self._commit_sidecar([
                    "preflight/checks.py",
                    "preflight/checks.py",
                ]),
            )
            snap = _clean_git()
            snap.porcelain = "?? preflight/checks.py"
            report = run_preflight(
                "COMMIT_EXACT_FILES",
                ctx=_git_only_ctx(snap),
                allowlist_file=sidecar,
            )
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertEqual(report.allowlist["path_count"], 1)

    def test_sidecar_max_path_count_blocked(self):
        paths = [f"preflight/file_{i}.py" for i in range(65)]
        report = self._run_sidecar_raw({
            "manifest_id": "COMMIT_EXACT_FILES",
            "authorization_phase_id": "TEST",
            "approved_file_allowlist": paths,
        })
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "allowlist.schema" for c in report.checks))


class StaleCandidatesPreflightTests(unittest.TestCase):
    """Windows venv child processes may omit repo path in command lines."""

    def _run(self, processes: list[ProcessInfo]) -> PreflightReport:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "relay-dryrun", "sdlc-dryrun"])
            return run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data, processes=processes))

    def _stale(self, report: PreflightReport) -> CheckResult:
        return next(c for c in report.checks if c.id == "process.stale_candidates")

    def _assert_stale_blocked(self, cmd: str) -> None:
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(900, cmd))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, STATUS_BLOCKED)

    def test_windows_venv_run_py_child_not_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(200, r'"C:\Users\Narachat\AppData\Local\Programs\Python\Python312\python.exe" run.py'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")

    def test_windows_venv_wrapper_codex_child_not_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(201, r'"python.exe" wrapper.py codex'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")

    def test_windows_venv_wrapper_codex_reviewer_child_not_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(202, r'"python.exe" wrapper.py codex_reviewer'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")

    def test_windows_venv_wrapper_codexsafe_child_not_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(203, r'"python.exe" wrapper.py codexsafe'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")

    def test_non_agent_python_not_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(300, r'"python.exe" -m pytest tests/test_foo.py'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")

    def test_pathless_run_py_with_unknown_flag_still_stale(self):
        self._assert_stale_blocked(r'"python.exe" run.py --unknown')

    def test_pathless_run_py_with_extra_arg_still_stale(self):
        self._assert_stale_blocked(r'"python.exe" run.py extra')

    def test_pathless_run_py_with_dev_flag_still_stale(self):
        self._assert_stale_blocked(r'"python.exe" run.py --dev')

    def test_pathless_run_py_something_still_stale(self):
        self._assert_stale_blocked(r'"python.exe" run.py something')

    def test_bare_run_py_with_unknown_flag_still_stale(self):
        self._assert_stale_blocked(r'run.py --unknown')

    def test_bare_run_py_extra_still_stale(self):
        self._assert_stale_blocked(r'run.py extra')

    def test_malformed_pathless_run_py_like_still_stale(self):
        self._assert_stale_blocked(r'"python.exe" not-run.py run.py')

    def test_ambiguous_agentchattr_like_run_py_still_stale(self):
        self._assert_stale_blocked(r'"someother.exe" run.py')

    def test_pathless_forbidden_wrapper_still_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(400, r'"python.exe" wrapper.py agy'))
        report = self._run(procs)
        stale = self._stale(report)
        self.assertEqual(stale.status, STATUS_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.forbidden" for c in report.checks))

    def test_pathless_forbidden_wrapper_claude_still_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(401, r'"python.exe" wrapper.py claude'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, STATUS_BLOCKED)

    def test_pathless_forbidden_wrapper_gemini_still_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(402, r'"python.exe" wrapper.py gemini'))
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, STATUS_BLOCKED)

    def test_pathless_unknown_wrapper_still_stale(self):
        procs = _e4c_wrappers_running()
        procs.append(ProcessInfo(403, r'"python.exe" wrapper.py unknown_agent'))
        report = self._run(procs)
        stale = self._stale(report)
        self.assertEqual(stale.status, STATUS_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.unexpected" for c in report.checks))

    def test_pathless_wrapper_only_runtime_still_requires_wrappers(self):
        procs = [
            ProcessInfo(99, r'"python.exe" run.py'),
            ProcessInfo(100, r'"python.exe" wrapper.py codex'),
            ProcessInfo(101, r'"python.exe" wrapper.py codex_reviewer'),
            ProcessInfo(102, r'"python.exe" wrapper.py codexsafe'),
        ]
        report = self._run(procs)
        self.assertEqual(self._stale(report).status, "PASS")
        required = next(c for c in report.checks if c.id == "wrappers.required")
        self.assertEqual(required.status, "PASS")

    def test_windows_venv_mixed_launcher_and_child_passes_e4c(self):
        procs = [
            ProcessInfo(99, r'"C:\tools\agentchattr\repo\.venv\Scripts\python.exe" run.py'),
            ProcessInfo(54448, r'"C:\Users\Narachat\AppData\Local\Programs\Python\Python312\python.exe" run.py'),
            ProcessInfo(100, r'"C:\tools\agentchattr\repo\.venv\Scripts\python.exe" wrapper.py codex'),
            ProcessInfo(19676, r'"C:\Users\Narachat\AppData\Local\Programs\Python\Python312\python.exe" wrapper.py codex'),
            ProcessInfo(101, r'"C:\tools\agentchattr\repo\.venv\Scripts\python.exe" wrapper.py codex_reviewer'),
            ProcessInfo(50376, r'"C:\Users\Narachat\AppData\Local\Programs\Python\Python312\python.exe" wrapper.py codex_reviewer'),
            ProcessInfo(102, r'"C:\tools\agentchattr\repo\.venv\Scripts\python.exe" wrapper.py codexsafe'),
            ProcessInfo(8968, r'"C:\Users\Narachat\AppData\Local\Programs\Python\Python312\python.exe" wrapper.py codexsafe'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "relay-dryrun", "sdlc-dryrun"])
            report = run_preflight("E4C_SDLC_LIVE", ctx=_base_ctx(data, processes=procs))
        self.assertEqual(self._stale(report).status, "PASS")
        self.assertEqual(report.verdict, VERDICT_PASS)


class E6CAgyLiveManifestTests(unittest.TestCase):
    """E6C AGY live-validation preflight manifest governance shape."""

    def test_e6c_manifest_loads(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest.phase_id, "E6C_AGY_LIVE")
        self.assertNotEqual(manifest.phase_id, "E4C_SDLC_LIVE")

    def test_e6c_requires_agy_live_validation_channel(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertIn("agy-live-validation", manifest.channels["required"])

    def test_e6c_allows_agy_wrapper_only(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertEqual(manifest.wrappers["allowed"], ["agy"])
        self.assertNotIn("codex", manifest.wrappers["allowed"])
        self.assertNotIn("codex_reviewer", manifest.wrappers["allowed"])
        self.assertNotIn("codexsafe", manifest.wrappers["allowed"])

    def test_e6c_forbids_sdlc_and_broad_wrappers(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        forbidden = set(manifest.wrappers["forbidden"])
        self.assertIn("codex", forbidden)
        self.assertIn("codex_reviewer", forbidden)
        self.assertIn("codexsafe", forbidden)
        self.assertIn("claude", forbidden)
        self.assertIn("gemini", forbidden)
        self.assertIn("agy", manifest.wrappers["allowed"])
        self.assertNotIn("agy", forbidden)

    def test_e6c_general_fallback_forbidden(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertTrue(manifest.general_fallback_forbidden)
        self.assertTrue(manifest.channels["forbid_general_session_leak_count"])

    def test_e6c_sandbox_flow_disabled_expectation(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertTrue(manifest.sandbox["forbid_flow_enabled"])
        self.assertTrue(manifest.sandbox["forbid_audit_activity"])

    def test_e6c_redaction_self_test_required(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertTrue(manifest.redaction["require_self_test"])

    def test_e6c_git_hygiene_required(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        git = manifest.git
        self.assertTrue(git["require_clean_tree"])
        self.assertTrue(git["require_no_staged"])
        self.assertTrue(git["require_synced_with_remote"])
        self.assertTrue(git["require_config_local_ignored"])

    def test_e6c_zero_active_sessions_required(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertEqual(manifest.sessions["max_active_count"], 0)

    def test_e6c_network_expects_server_port(self):
        manifest, err = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err)
        assert manifest is not None
        self.assertEqual(manifest.network["expected_port"], 8300)
        self.assertTrue(manifest.network["require_server_when_wrappers_required"])

    def test_full_e6c_fixture_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, _e6c_channels())
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data))
        self.assertEqual(report.verdict, VERDICT_PASS)
        self.assertEqual(report.exit_code, 0)
        ids = {c.id for c in report.checks}
        self.assertIn("policy.general_fallback", ids)
        self.assertIn("redaction.self_test", ids)

    def test_e6c_forbidden_codex_wrapper_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, _e6c_channels())
            procs = _e6c_wrappers_running()
            procs.append(ProcessInfo(200, r"python wrapper.py codex"))
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data, processes=procs))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.forbidden" for c in report.checks))

    def test_e6c_required_agy_missing_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, _e6c_channels())
            procs = [ProcessInfo(99, r"python run.py")]
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data, processes=procs))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "wrappers.required" for c in report.checks))

    def test_e6c_missing_agy_channel_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, ["general", "relay-dryrun", "sdlc-dryrun"])
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "channels.required" for c in report.checks))

    def test_e6c_active_sessions_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, _e6c_channels())
            _write_sessions(data, [{"id": 1, "state": "active", "channel": "agy-live-validation"}])
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "sessions.active_count" for c in report.checks))

    def test_e6c_sandbox_audit_activity_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _write_settings(data, _e6c_channels())
            (data / "sandbox_flow_audit.jsonl").write_text('{"result":"reject"}\n', encoding="utf-8")
            report = run_preflight("E6C_AGY_LIVE", ctx=_base_e6c_ctx(data))
        self.assertEqual(report.verdict, VERDICT_BLOCKED)
        self.assertTrue(any(c.id == "sandbox.audit" for c in report.checks))

    def test_e6c_not_reusing_e4c_manifest(self):
        e4c, err4 = load_manifest("E4C_SDLC_LIVE", manifest_dir=MANIFEST_DIR)
        e6c, err6 = load_manifest("E6C_AGY_LIVE", manifest_dir=MANIFEST_DIR)
        self.assertIsNone(err4)
        self.assertIsNone(err6)
        assert e4c is not None and e6c is not None
        self.assertIn("agy", e4c.wrappers["forbidden"])
        self.assertNotIn("agy", e6c.wrappers["forbidden"])
        self.assertIn("sdlc-dryrun", e4c.channels["required"])
        self.assertIn("agy-live-validation", e6c.channels["required"])
        self.assertNotIn("agy-live-validation", e4c.channels["required"])


class E5DManifestValidationTests(unittest.TestCase):
    def test_shipped_manifests_load(self):
        for phase_id in (
            "READ_ONLY_AUDIT",
            "CODEX_REVIEW_DIRTY_TREE",
            "COMMIT_EXACT_FILES",
            "PUSH_ONLY",
            "E4C_SDLC_LIVE",
            "E6C_AGY_LIVE",
        ):
            manifest, err = load_manifest(phase_id, manifest_dir=MANIFEST_DIR)
            self.assertIsNone(err, msg=f"{phase_id}: {err}")
            self.assertIsNotNone(manifest)

    def test_missing_required_field_still_blocks(self):
        manifest, err = validate_manifest_dict({"phase_id": "INCOMPLETE"})
        self.assertIsNone(manifest)
        self.assertIsNotNone(err)

    def test_require_clean_tree_false_without_allowlist_mechanism_fails_validation(self):
        data = _git_only_manifest_skeleton("BAD", git_extra={"require_clean_tree": False})
        data["git"].pop("approved_dirty_paths", None)
        manifest, err = validate_manifest_dict(data)
        self.assertIsNone(manifest)
        self.assertIn("require_clean_tree=false", err or "")


if __name__ == "__main__":
    unittest.main()
