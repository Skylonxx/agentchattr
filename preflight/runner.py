"""Preflight orchestration and reporting."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preflight.checks import (
    CheckResult,
    GitSnapshot,
    PortListener,
    PreflightContext,
    ProcessInfo,
    VERDICT_BLOCKED,
    VERDICT_PASS,
    aggregate_verdict,
    blocked_reasons,
    capture_git_snapshot,
    check_manifest_known,
    run_all_checks,
)
from preflight.redaction import scrub_preflight_output
from preflight_manifest import PhaseManifest, load_manifest

EXIT_PASS = 0
EXIT_BLOCKED = 1
EXIT_INTERNAL = 2

ROOT = Path(__file__).resolve().parents[1]


class SubprocessGitRunner:
    """Read-only git subprocess runner."""

    _ALLOWED = frozenset({
        "rev-parse", "status", "diff", "log",
    })

    def capture(self, *args: str) -> tuple[int, str, str]:
        if len(args) < 3 or args[0] != "-C":
            raise ValueError("expected git -C <path> ...")
        git_args = list(args[2:])
        if not git_args or git_args[0] not in self._ALLOWED:
            raise ValueError(f"git subcommand not allowed: {git_args[:1]}")
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr


def _default_processes() -> list[ProcessInfo]:
    """Best-effort read-only process table (Windows). Returns empty on failure."""
    try:
        import sys
        if sys.platform != "win32":
            return []
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'run\\.py|wrapper\\.py' } | "
            "Select-Object ProcessId, CommandLine | "
            "ConvertTo-Json -Compress"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        raw = json.loads(proc.stdout)
        if isinstance(raw, dict):
            raw = [raw]
        out: list[ProcessInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            pid = int(item.get("ProcessId", 0))
            cmd = str(item.get("CommandLine", ""))
            if pid and cmd:
                out.append(ProcessInfo(pid=pid, command_line=cmd))
        return out
    except Exception:
        return []


def _default_port_listeners(port: int) -> list[PortListener]:
    try:
        import sys
        if sys.platform != "win32":
            return []
        ps_cmd = (
            f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
            "Select-Object LocalPort, OwningProcess, State | ConvertTo-Json -Compress"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        raw = json.loads(proc.stdout)
        if isinstance(raw, dict):
            raw = [raw]
        out: list[PortListener] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append(PortListener(
                port=int(item.get("LocalPort", port)),
                pid=int(item.get("OwningProcess", 0)),
                state=str(item.get("State", "Listen")),
            ))
        return out
    except Exception:
        return []


@dataclass
class PreflightReport:
    phase: str
    verdict: str
    checks: list[CheckResult]
    blocked_reasons: list[str]
    exit_code: int
    baseline: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": scrub_preflight_output(self.phase),
            "verdict": self.verdict,
            "baseline": scrub_preflight_output(self.baseline),
            "checks": [c.to_dict() for c in self.checks],
            "blocked_reasons": [scrub_preflight_output(r) for r in self.blocked_reasons],
        }


def run_preflight(
    phase_id: str,
    *,
    repo_root: Path | None = None,
    ctx: PreflightContext | None = None,
    git_runner: SubprocessGitRunner | None = None,
    manifest_dir: Path | None = None,
) -> PreflightReport:
    """Execute all preflight checks for a phase. Fail-closed on any error."""
    phase = phase_id.strip() if phase_id else ""
    root = repo_root or ROOT

    try:
        manifest, manifest_err = load_manifest(phase, manifest_dir=manifest_dir)
        checks: list[CheckResult] = [check_manifest_known(manifest, manifest_err)]

        if manifest is None:
            verdict = VERDICT_BLOCKED
            reasons = blocked_reasons(checks)
            return PreflightReport(
                phase=phase,
                verdict=verdict,
                checks=checks,
                blocked_reasons=reasons,
                exit_code=EXIT_BLOCKED,
            )

        context = ctx or PreflightContext(repo_root=root)
        runner = git_runner or SubprocessGitRunner()
        git_snap = context.git_snapshot or capture_git_snapshot(root, runner)

        if context.processes is None:
            context.processes = _default_processes()
        if context.port_listeners is None:
            port = int(manifest.network["expected_port"])
            context.port_listeners = _default_port_listeners(port)

        checks.extend(run_all_checks(manifest, context, git_snap))
        verdict = aggregate_verdict(checks)
        reasons = blocked_reasons(checks)
        exit_code = EXIT_PASS if verdict == VERDICT_PASS else EXIT_BLOCKED
        baseline = git_snap.head if git_snap.head else ""

        return PreflightReport(
            phase=manifest.phase_id,
            verdict=verdict,
            checks=checks,
            blocked_reasons=reasons,
            exit_code=exit_code,
            baseline=baseline,
        )
    except Exception as exc:
        detail = scrub_preflight_output(f"internal preflight error: {exc}")
        checks = [CheckResult("preflight.internal", VERDICT_BLOCKED, detail)]
        return PreflightReport(
            phase=phase,
            verdict=VERDICT_BLOCKED,
            checks=checks,
            blocked_reasons=[detail],
            exit_code=EXIT_INTERNAL,
        )


def format_human(report: PreflightReport) -> str:
    lines = [
        f"Phase: {scrub_preflight_output(report.phase)}",
        f"Verdict: {report.verdict}",
    ]
    if report.baseline:
        lines.append(f"Baseline: {scrub_preflight_output(report.baseline)}")
    lines.append("")
    lines.append("Checks:")
    for check in report.checks:
        mark = "PASS" if check.status == "PASS" else "BLOCKED"
        lines.append(f"  [{mark}] {check.id}: {scrub_preflight_output(check.detail)}")
    if report.blocked_reasons:
        lines.append("")
        lines.append("Blocked reasons:")
        for reason in report.blocked_reasons:
            lines.append(f"  - {scrub_preflight_output(reason)}")
        lines.append("")
        lines.append("Remediation (manual operator actions only):")
        for reason in report.blocked_reasons:
            if reason.startswith("wrappers."):
                lines.append("  - Operator: stop forbidden wrappers or start required wrappers manually.")
            elif reason.startswith("git."):
                lines.append("  - Operator: resolve git state manually (no auto clean/reset/commit).")
            elif reason.startswith("sessions."):
                lines.append("  - Operator: wait for or manually complete/interrupt active sessions.")
            elif reason.startswith("port."):
                lines.append("  - Operator: inspect stale listener PID manually; do not auto-kill from preflight.")
            else:
                lines.append(f"  - Operator action required: inspect {reason.split(':')[0]}.")
    return "\n".join(lines)


def format_json(report: PreflightReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fail-closed process hygiene preflight (read-only)")
    parser.add_argument("--phase", required=True, help="Phase manifest ID (e.g. E4C_SDLC_LIVE)")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repository root path")
    args = parser.parse_args(argv)

    report = run_preflight(args.phase, repo_root=Path(args.repo_root))
    if args.format == "json":
        print(format_json(report))
    else:
        print(format_human(report))
    return report.exit_code
