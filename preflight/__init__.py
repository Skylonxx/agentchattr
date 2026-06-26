"""Fail-closed process hygiene preflight checker (read-only)."""

from preflight.checks import (
    VERDICT_BLOCKED,
    VERDICT_PASS,
    CheckResult,
    GitSnapshot,
    PortListener,
    PreflightContext,
    ProcessInfo,
)
from preflight.runner import (
    EXIT_BLOCKED,
    EXIT_INTERNAL,
    EXIT_PASS,
    PreflightReport,
    format_human,
    format_json,
    main,
    run_preflight,
)

__all__ = [
    "VERDICT_BLOCKED",
    "VERDICT_PASS",
    "CheckResult",
    "GitSnapshot",
    "PortListener",
    "PreflightContext",
    "ProcessInfo",
    "PreflightReport",
    "EXIT_PASS",
    "EXIT_BLOCKED",
    "EXIT_INTERNAL",
    "format_human",
    "format_json",
    "main",
    "run_preflight",
]
