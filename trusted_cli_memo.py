"""Deterministic coordinator execution-memo compiler for trusted_direct_repo_cli mode.

Phase 1: read-only. The memo replaces snapshot/manifest injection. Claude runs in
the real repo with normal tools enabled and is bounded by the memo's explicit scope,
red zones, allowed/forbidden actions, required evidence, report path, and stop
conditions — not by mechanical mid-flow runtime blockers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REQUIRED_MEMO_SECTIONS = (
    "PROMPT ID",
    "TO",
    "FROM",
    "MODEL",
    "REASONING",
    "MODE",
    "PROJECT",
    "PHASE",
    "SUBJECT",
    "WORKDIR",
    "CURRENT BASELINE",
    "OBJECTIVE",
    "SCOPE",
    "ALLOWED FILES",
    "FORBIDDEN FILES / RED ZONES",
    "ALLOWED ACTIONS",
    "FORBIDDEN ACTIONS",
    "REQUIRED TESTS",
    "REQUIRED EVIDENCE",
    "REPORT PATH",
    "FINAL REPLY FORMAT",
    "STOP CONDITIONS",
)

DEFAULT_RED_ZONES = (
    "src/pages/POSPage.tsx (checkout page write path)",
    "src/hooks/pos/useCheckout.ts (checkout hook write path)",
    "src/lib/pos/asyncCheckout.ts (async checkout / submitAsyncOrder)",
    "src/lib/pos/cartUtils.ts (cart math)",
    "confirmSale / submitAsyncOrder / cart math / Firebase writes",
    "git commit / push / stash / reset / clean / any history rewrite",
    ".env / secrets / .git internals",
)


@dataclass
class TrustedMemoResult:
    ok: bool
    prompt: str = ""
    blocker: str = ""
    missing_sections: list[str] = field(default_factory=list)


def _bullet_list(items: list[str], *, empty: str = "(none)") -> list[str]:
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return [f"  - {empty}"]
    return [f"  - {i}" for i in items]


def build_trusted_cli_execution_memo(
    *,
    project: str,
    phase: str,
    subject: str,
    workspace_root: str,
    expected_head: str = "",
    prompt_memo_body: str,
    read_paths: list[str] | None = None,
    primary_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    red_zones: list[str] | None = None,
    expected_output_path: str = "",
    external_report_write_roots: list[str] | None = None,
    prompt_id: str = "",
    instruction: str = "",
) -> TrustedMemoResult:
    """Compile the full ChatGPT-style execution memo for a trusted read-only CLI turn."""
    read_paths = list(read_paths or [])
    primary_paths = list(primary_paths or read_paths[:2])
    forbidden_paths = list(forbidden_paths or [])
    red_zones = list(red_zones or DEFAULT_RED_ZONES)
    roots = list(external_report_write_roots or [])

    lines: list[str] = [
        f"PROMPT ID: {prompt_id or 'TRUSTED-DIRECT-REPO-CLI-READONLY'}",
        "TO: Claude Developer",
        "FROM: agentchattr Coordinator",
        "MODEL: Claude",
        "REASONING: High",
        "MODE: trusted_direct_repo_cli (read-only — tools ENABLED, no snapshots)",
        f"PROJECT: {project}",
        f"PHASE: {phase}",
        f"SUBJECT: {subject}",
        "",
        "WORKDIR:",
        f"  {workspace_root}",
        "  You are already running inside this real repository with your normal CLI tools",
        "  enabled. Inspect files directly (read/search). No source snapshots are injected.",
        "",
        "CURRENT BASELINE:",
        f"  expected HEAD: {expected_head or '(unspecified — verify with git rev-parse HEAD)'}",
        "",
        "OBJECTIVE:",
        f"  {(instruction or prompt_memo_body).strip().splitlines()[0] if (instruction or prompt_memo_body).strip() else 'See PROMPT MEMO below.'}",
        "",
        "SCOPE:",
        "  Read-only analysis. Inspect the primary files first; only open checkout/cart",
        "  files if needed to understand boundaries. Do NOT modify product files.",
        "",
        "  Primary files to inspect first:",
        *_bullet_list(primary_paths),
        "",
        "  In-scope read/inspect paths:",
        *_bullet_list(read_paths),
        "",
        "ALLOWED FILES:",
        "  Read/inspect: any in-scope path above (read-only).",
        "  Write: ONLY the external report file under the report path below.",
        "",
        "FORBIDDEN FILES / RED ZONES:",
        *_bullet_list(red_zones + forbidden_paths),
        "",
        "ALLOWED ACTIONS:",
        "  - Read and search files in the repo using your normal tools.",
        "  - Run read-only git inspection (git status, git diff, git log, git show).",
        "  - Write your analysis report to the REPORT PATH below.",
        "",
        "FORBIDDEN ACTIONS:",
        "  - No modifications to any Twinpet product/source/test/config files.",
        "  - No git commit / push / stash / reset / clean / checkout / history rewrite.",
        "  - No access to or modification of .env, secrets, or .git internals.",
        "  - No broad/recursive deletion.",
        "  - Do not leave the WORKDIR repository root.",
        "",
        "REQUIRED TESTS:",
        "  Read-only analysis: no tests are run in this phase. If you recommend tests,",
        "  list the exact commands in your report under a 'Recommended tests' heading.",
        "",
        "REQUIRED EVIDENCE (include in report):",
        "  - git rev-parse HEAD (confirm baseline)",
        "  - git status --short (must show no product modifications)",
        "  - list of files inspected",
        "  - explicit confirmation no red-zone files were modified",
        "",
        "REPORT PATH:",
        f"  {expected_output_path or '(coordinator-provided Ai-Report path)'}",
        "  Write the report ONLY under the Ai-Report write allowlist:",
        *_bullet_list(roots),
        "",
        "FINAL REPLY FORMAT:",
        "  When the report file is written, reply with:",
        "    REPORT_READY",
        "    Path: <report path>",
        "    Status: <PASS / PASS_WITH_NOTES / REQUEST_CHANGES / BLOCKER>",
        "    Summary: <one line>",
        "  If you cannot proceed safely, reply with a single line starting 'BLOCKER:'.",
        "",
        "STOP CONDITIONS:",
        "  - Stop after the report is written and REPORT_READY is emitted.",
        "  - Stop and emit BLOCKER if the task would require a product modification,",
        "    a forbidden action, or leaving the repo root.",
        "  - Do not loop; do not request snapshots (none are used in this mode).",
        "",
        "PROMPT MEMO:",
        prompt_memo_body.strip(),
    ]

    prompt = "\n".join(lines)
    prompt_lines = prompt.splitlines()
    missing = [
        s for s in REQUIRED_MEMO_SECTIONS
        if not any(ln.startswith(s) for ln in prompt_lines)
    ]
    if missing:
        return TrustedMemoResult(
            ok=False,
            blocker=f"BLOCKER: trusted CLI memo missing sections: {', '.join(missing)}",
            missing_sections=missing,
        )
    return TrustedMemoResult(ok=True, prompt=prompt)
