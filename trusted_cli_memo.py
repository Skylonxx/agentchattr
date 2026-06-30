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
    "REQUIRED HANDOFF BLOCKS IN YOUR REPORT",
    "FINAL RESPONSE REQUIREMENT",
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


def _trusted_cli_handoff_block_lines() -> list[str]:
    """Handoff marker instructions aligned with report_orchestration constants."""
    from report_orchestration import (
        HANDOFF_FOR_AGY_BEGIN,
        HANDOFF_FOR_AGY_END,
        HANDOFF_FOR_CODEX_REVIEWER_BEGIN,
        HANDOFF_FOR_CODEX_REVIEWER_END,
    )

    return [
        "REQUIRED HANDOFF BLOCKS IN YOUR REPORT:",
        "  Include these marker blocks inside your Markdown report body:",
        "",
        f"  {HANDOFF_FOR_AGY_BEGIN}",
        "  <concise UI/UX handoff for AGY UI Lead>",
        f"  {HANDOFF_FOR_AGY_END}",
        "",
        f"  {HANDOFF_FOR_CODEX_REVIEWER_BEGIN}",
        "  <concise reviewer handoff for Codex Reviewer>",
        f"  {HANDOFF_FOR_CODEX_REVIEWER_END}",
        "",
        "  AGY handoff must include:",
        "  - PaymentModal files inspected",
        "  - UI/UX findings by severity",
        "  - accessibility/focus/keyboard risks",
        "  - responsive/narrow-screen risks",
        "  - print/receipt behavior notes",
        "  - recommended safe implementation scope",
        "  - exact allowed files",
        "  - red-zone confirmation",
        "",
        "  Codex Reviewer handoff must include:",
        "  - summary of analysis",
        "  - files inspected",
        "  - risk boundaries",
        "  - exact recommended implementation files",
        "  - confirmation no source/test/config modifications",
        "  - items Codex should verify before approving implementation",
        "",
    ]


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
        "  Write: only the exact REPORT PATH below if you choose file output (Ai-Report only).",
        "",
        "FORBIDDEN FILES / RED ZONES:",
        *_bullet_list(red_zones + forbidden_paths),
        "",
        "ALLOWED ACTIONS:",
        "  - Read and search files in the repo using your normal tools.",
        "  - Run read-only git inspection (git status, git diff, git log, git show).",
        "  - Return your complete analysis as Markdown in your final response.",
        "",
        "FORBIDDEN ACTIONS:",
        "  - No modifications to any Twinpet product/source/test/config files.",
        "  - No git commit / push / stash / reset / clean / checkout / history rewrite.",
        "  - No access to or modification of .env, secrets, or .git internals.",
        "  - No broad/recursive deletion.",
        "  - Do not leave the WORKDIR repository root.",
        "  - Do not write the report anywhere except the REPORT PATH above.",
        "  - Do not ask for file-write permission.",
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
        "  This is the only external report path you may write to (if you choose file output).",
        "",
        *_trusted_cli_handoff_block_lines(),
        "FINAL RESPONSE REQUIREMENT:",
        "  Return the full Markdown report in stdout OR write it only to the exact REPORT PATH above.",
        "  If you write the file, return a short completion message in stdout.",
        "  Do not write anywhere else.",
        "  Do not modify Twinpet product files.",
        "  Do not ask for file-write permission.",
        "",
        "  Include at minimum:",
        "  - A title (# heading)",
        "  - Status: PASS / PASS_WITH_NOTES / REQUEST_CHANGES / BLOCKER",
        "  - ## Summary",
        "  - ## Files inspected (or equivalent)",
        "  - ## Findings (or equivalent)",
        "  - ## Red-zone confirmation (no product modifications)",
        "  - ## Recommended next step",
        "",
        "FINAL REPLY FORMAT:",
        "  Either the full Markdown report in stdout, or a short completion message if the",
        "  report was written to the REPORT PATH above.",
        "  Do not use REPORT_FILE_WRITE_BEGIN/END in trusted CLI mode.",
        "  If you cannot proceed safely, reply with a single line starting 'BLOCKER:'.",
        "",
        "STOP CONDITIONS:",
        "  - Stop after delivering the report via stdout or the REPORT PATH above.",
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
