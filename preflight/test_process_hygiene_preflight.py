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


if __name__ == "__main__":
    unittest.main()
