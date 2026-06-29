"""Session Relay Bridge — server-side relay for text-in/text-out session turns.

Builds sealed prompts for relay-mode session turns so child agents (Codex,
CodexSafe) participate as pure text processors without direct MCP access.
The server owns all chat I/O; child agents receive context in prompt text
and return plain text only.
"""

import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Explicit TO: routing headers (repository routing contract)
# ---------------------------------------------------------------------------

ROLE_ROUTING_TO_TARGETS: dict[str, str] = {
    "coordinator": "Codex Coordinator",
    "developer": "Claude Developer",
    "ui_lead": "AGY UI Lead",
    "reviewer": "Codex Reviewer",
    "safety_gate": "CodexSafe Safety Gate",
}

AGENT_BASE_ROUTING_TO_TARGETS: dict[str, str] = {
    "codex_coordinator": "Codex Coordinator",
    "codex_reviewer": "Codex Reviewer",
    "codex": "Codex",
    "codexsafe": "CodexSafe Safety Gate",
    "claude": "Claude Developer",
    "agy": "AGY UI Lead",
}


def has_explicit_to_header(text: str) -> bool:
    """True when the first substantive line is an explicit TO: routing header."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.upper().startswith("TO:")
    return False


def resolve_routing_to_target(*, role: str = "", agent_base: str = "") -> str:
    """Resolve the TO: target label for a session role or agent identity."""
    base = (agent_base or "").strip().lower()
    if base in AGENT_BASE_ROUTING_TO_TARGETS:
        return AGENT_BASE_ROUTING_TO_TARGETS[base]
    role_key = (role or "").strip().lower()
    return ROLE_ROUTING_TO_TARGETS.get(role_key, role_key.title() or "Agent")


def build_routing_header_block(
    *,
    to_target: str,
    role: str,
    project: str = "agentchattr",
    phase: str = "coordinator-loop",
    subject: str = "session-handoff",
    from_source: str = "agentchattr-session-engine",
    mode: str = "session-handoff",
) -> str:
    """Build mandatory multi-agent routing header block."""
    return "\n".join([
        f"TO: {to_target}",
        f"FROM: {from_source}",
        f"ROLE: {role}",
        f"MODE: {mode}",
        f"PROJECT: {project}",
        f"PHASE: {phase}",
        f"SUBJECT: {subject}",
    ])


def ensure_explicit_routing_headers(
    text: str,
    *,
    role: str,
    agent_base: str = "",
    project: str = "agentchattr",
    phase: str = "coordinator-loop",
    subject: str = "session-handoff",
    mode: str = "session-handoff",
    from_source: str = "agentchattr-session-engine",
) -> str:
    """Prepend routing headers when the prompt lacks an explicit TO: line."""
    body = (text or "").strip()
    if has_explicit_to_header(body):
        return body
    to_target = resolve_routing_to_target(role=role, agent_base=agent_base)
    headers = build_routing_header_block(
        to_target=to_target,
        role=role or agent_base or "agent",
        project=project,
        phase=phase,
        subject=subject,
        from_source=from_source,
        mode=mode,
    )
    return f"{headers}\n\n{body}" if body else headers


_ROUTING_HANDOFF_INSTRUCTION = (
    "HANDOFF ROUTING (required for every NEXT: dispatch): "
    "After your first-line NEXT: <role> token, the prompt body you emit for that worker "
    "MUST begin with explicit routing headers including TO:, FROM:, ROLE:, MODE:, PROJECT:, "
    "PHASE:, and SUBJECT: addressed to the target role/agent. "
    "Never emit bare task instructions without a TO: header."
)

# Agents authorized for relay-mode session execution.
#
# codex_coordinator and codex_reviewer are the split Codex workflow identities
# (Workflow Coordinator / Independent Reviewer). They are relay-eligible because
# this same package ships the session-engine anti-self-review guard
# (validate_no_self_review) that refuses to cast one identity as both coordinator
# and reviewer — eligibility for the pair must never be enabled without that guard.
#
# Production "claude" is authorized for claude_relay (V2-D activation gate).
# "agy" remains ABSENT (AGY relay is not enabled). Branch-only dry-run identities
# such as "claude_dryrun" must never appear here on main.
RELAY_ELIGIBLE_AGENTS = frozenset({
    "claude",
    "codex",
    "codexsafe",
    "codex_coordinator",
    "codex_reviewer",
})


# ---------------------------------------------------------------------------
# Relay prompt builder
# ---------------------------------------------------------------------------

def build_relay_prompt(
    *,
    session_name: str,
    goal: str,
    phase_name: str,
    phase_index: int,
    total_phases: int,
    role: str,
    instruction: str,
    context_messages: list[dict] | None = None,
    agent_base: str = "",
    prompt_body: str = "",
) -> str:
    """Build a sealed session-turn prompt for a relay-mode agent.

    The prompt contains all context the agent needs. It must NOT reference
    chat_read, chat_send, or any MCP tool usage. The agent returns plain
    text only.
    """
    lines = [
        f"SESSION: {session_name}",
    ]
    if prompt_body:
        lines.extend(["", "FULL TASK MEMO (authoritative):", prompt_body])
    elif goal:
        lines.append(f"GOAL: {goal}")
    lines.append(f"PHASE: {phase_name} ({phase_index + 1}/{total_phases})")
    lines.append(f"YOUR ROLE: {role}")
    lines.append(f"INSTRUCTION: {instruction}")

    if context_messages:
        lines.append("")
        lines.append("CONTEXT (recent messages):")
        for msg in context_messages[-10:]:
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            lines.append(f"  [{sender}]: {text}")

    lines.append("")
    lines.append(
        "OUTPUT CONTRACT: Respond with plain text only. "
        "Do not use MCP tools. Do not call chat_read or chat_send. "
        "Do not run shell commands. Do not edit files. "
        "Your response will be relayed by the server."
    )

    body = "\n\n".join(lines)
    return ensure_explicit_routing_headers(
        body,
        role=role,
        agent_base=agent_base,
        project=session_name,
        phase=phase_name or f"phase-{phase_index + 1}",
        subject=goal[:120] if goal else "relay-worker-turn",
    )


def session_workspace_policy(session: dict | None) -> dict | None:
    """Return persisted workspace policy from a session dict, if present."""
    if not isinstance(session, dict):
        return None
    policy = session.get("workspace_policy")
    return dict(policy) if isinstance(policy, dict) else None


def role_uses_scoped_workspace(session: dict | None, role: str) -> bool:
    """True when role participates in an external workspace session (impl/docs-only)."""
    policy = session_workspace_policy(session)
    if not policy or policy.get("mode") not in ("implementation", "docs-only"):
        return False
    import workspace_policy as wp
    perms = wp.role_permission_for(policy, role)
    if not perms:
        return False
    root = (policy.get("workspace") or {}).get("root")
    if not root:
        return False
    return perms.get("filesystem") in ("write_allowlist", "read")


def role_uses_headless_scoped_workspace(session: dict | None, role: str) -> bool:
    """Developer/ui_lead must use headless exec (not relay) for scoped Twinpet work."""
    if not role_uses_scoped_workspace(session, role):
        return False
    return role in ("developer", "ui_lead")


def build_scoped_write_worker_prompt(
    *,
    session_name: str,
    goal: str,
    role: str,
    policy: dict,
    instruction: str = "",
    phase_name: str = "",
    phase_index: int = 0,
    total_phases: int = 1,
    context_messages: list[dict] | None = None,
    prompt_body: str = "",
) -> str:
    """Build a coordinator-loop worker prompt for workspace-bound sessions."""
    import workspace_policy as wp

    workspace = policy.get("workspace") or {}
    root = workspace.get("root") or ""
    write_files = list(policy.get("write_files") or [])
    read_paths = list(policy.get("read_paths") or [])
    report_paths = list(policy.get("report_paths") or [])
    forbidden = list(policy.get("forbidden_paths") or [])
    expected_head = workspace.get("expected_head") or ""
    mode = policy.get("mode") or ""
    perms = wp.role_permission_for(policy, role) or {}
    fs = perms.get("filesystem", "none")

    lines = [
        f"SESSION: {session_name}",
    ]
    if prompt_body:
        lines.extend(["", "FULL TASK MEMO (authoritative):", prompt_body])
    elif goal:
        lines.append(f"GOAL SUMMARY: {goal}")
    if phase_name:
        lines.append(f"PHASE: {phase_name} ({phase_index + 1}/{total_phases})")
    lines.append(f"YOUR ROLE: {role}")
    if instruction:
        lines.append(f"INSTRUCTION: {instruction}")

    lines.extend([
        "",
        f"WORKSPACE CONTRACT ({mode or 'scoped'} — authoritative over template read-only text):",
        f"WORKING DIRECTORY: {root}",
        "You MUST treat this path as your workspace root for this turn.",
        "Do not claim you are in agentchattr-scratch or another repo.",
    ])

    if expected_head:
        lines.append(f"EXPECTED GIT HEAD: {expected_head}")

    if read_paths and fs in ("read", "write_allowlist"):
        lines.extend(["", "ALLOWED READ PATHS:"])
        for rp in read_paths:
            lines.append(f"  - {rp}")

    if report_paths and fs == "write_allowlist":
        lines.extend([
            "",
            "REPORT OUTPUT PATHS (write final report here when instructed):",
        ])
        for rp in report_paths:
            lines.append(f"  - {rp}")

    if fs == "write_allowlist":
        if mode == "implementation":
            lines.extend([
                "",
                "PREFLIGHT (run before editing):",
                "  pwd",
                "  git status --short",
                "  git rev-parse HEAD",
                "",
            ])
        else:
            lines.append("")
        lines.append("ALLOWED FILE WRITES (exact allowlist only):")
        for wf in write_files:
            lines.append(f"  - {wf}")
        if mode == "implementation":
            lines.extend([
                "",
                "FORBIDDEN WRITES (non-exhaustive red zones):",
                "  POSPage.tsx, useCheckout.ts, asyncCheckout.ts, cartUtils.ts,",
                "  payment finalization, cart math, keyboard contracts, Firebase/rules, git writes.",
            ])
        elif mode == "docs-only":
            lines.extend([
                "",
                "DOCS-ONLY: No src/** or tests/** writes. Report/docs paths only.",
                "SNAPSHOT MODE: agentchattr injects AUTOMATED PRECHECK RESULTS and",
                "READ-ONLY FILE SNAPSHOT before this turn. You have no tools.",
                "Do not emit <tool_call> markup. Analyze from injected snapshots only.",
            ])
        if forbidden:
            lines.append("Configured forbidden path patterns also apply.")
        lines.extend([
            "",
            "ALLOWED READ-ONLY GIT: git status, git diff, git log, git show",
            "FORBIDDEN GIT: git add, commit, push, reset, checkout, clean, stash",
            "Do not use MCP tools. Do not call chat_read or chat_send.",
        ])
    elif fs == "read":
        lines.extend([
            "",
            "READ-ONLY WORKSPACE INSPECTION:",
            f"Inspect files under {root} only. Do not edit any files.",
            "ALLOWED READ-ONLY GIT: git status, git diff, git log, git show",
            "Do not use MCP tools. Do not call chat_read or chat_send.",
        ])
        if mode == "read-only":
            lines.extend([
                "",
                "REPORT-ONLY ANALYSIS: No Twinpet repo writes (no Task.md/Context.md/docs edits).",
                "SNAPSHOT MODE: agentchattr injects AUTOMATED PRECHECK RESULTS and",
                "READ-ONLY FILE SNAPSHOT before this turn. You have no tools.",
                "Analyze from injected snapshots only. Final report via REPORT_BEGIN/REPORT_END.",
                "External report is saved outside the repo by agentchattr when possible.",
            ])
    else:
        lines.extend([
            "",
            "READ-ONLY: Do not edit files. Do not run shell commands beyond read-only git.",
        ])

    if context_messages:
        lines.append("")
        lines.append("CONTEXT (recent messages):")
        for msg in context_messages[-10:]:
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            lines.append(f"  [{sender}]: {text}")

    lines.append("")
    lines.append(
        "Respond with plain text only. Your response will be relayed by the server."
    )
    lines.extend([
        "",
        "ROLE FLOW (Project Read-Only Coordinator Loop):",
        "  developer / Claude: technical file inspection, data-flow mapping, precheck/report drafting",
        "  ui_lead / AGY: UI/UX critique, cashier ergonomics, visual hierarchy, blueprint review",
        "  reviewer / Codex: consistency/safety review",
        "  safety_gate / CodexSafe: boundary enforcement",
        "",
        "TOOL USAGE:",
        "  Claude --print runs with tools DISABLED. Do NOT emit <tool_call> XML markup.",
        "  AUTOMATED PRECHECK RESULTS (when present) are authoritative for git HEAD/status.",
        "  Inspect files using plain-text PROGRESS updates, not tool-call syntax.",
    ])
    contract = coordinator_loop_worker_output_contract(role, workspace_bound=bool(root))
    if contract:
        lines.append("")
        lines.append(contract)
    body = "\n\n".join(lines)
    return ensure_explicit_routing_headers(
        body,
        role=role,
        project=session_name,
        phase=phase_name or f"phase-{phase_index + 1}",
        subject=goal[:120] if goal else "scoped-worker-turn",
    )


def build_safety_gate_prompt(
    *,
    session_name: str,
    goal: str,
    phase_name: str,
    content_to_review: str,
    agent_base: str = "",
    prompt_body: str = "",
) -> str:
    """Build a safety gate prompt for CodexSafe relay turns.

    The agent must respond with exactly PASS or BLOCK: <reason> on the
    first non-empty line. Any other format is treated as BLOCK.
    """
    lines = [
        "OUTPUT CONTRACT (strict — your FIRST non-empty line decides the session):",
        "Your response must begin with exactly one of these two verdicts:",
        "  PASS",
        "  BLOCK: <reason>",
        "Any other first line — including greetings, confirmations, markdown,",
        "or readiness statements — is treated as a malformed verdict and",
        "automatically becomes BLOCK.",
        "",
        "Do not greet. Do not confirm readiness. Do not acknowledge the session.",
        "Return ONLY the strict verdict on the first line.",
        "Do not write anything before the verdict line.",
        "Do not use Markdown, bullets, or code fences.",
        "Do not write PASS WITH NOTES or any variant.",
        "Do not use MCP tools. Do not call chat_read or chat_send.",
        "Do not run shell commands. Do not edit files.",
        "",
        "EXAMPLES:",
        "  Request asks agent to use chat_send, open files, run git, or request",
        "  broad MCP access → BLOCK: unsafe request asks for prohibited tool, file, git, or shell access",
        "  Request asks to modify source files, modify tests/, or tests/... paths → BLOCK",
        "  Request asks to weaken tests, weaken safety tests, or weaken channel prune tests → BLOCK",
        "  Request asks to commit changes, git push, git commit, or commit --amend → BLOCK",
        "  Request asks to manually delete channels or mutate data/settings.json → BLOCK",
        "  Request asks to print session token or paste authorization URL → BLOCK",
        "  tests/... paths are repo mutation and are not safer than source modification",
        "  Ordinary harmless summarisation or analysis request → PASS",
        "",
        f"SESSION: {session_name}",
    ]
    if prompt_body:
        lines.extend([f"GOAL SUMMARY: {goal}" if goal else "", "", "FULL TASK MEMO (authoritative):", prompt_body])
    else:
        lines.append(f"GOAL: {goal}")
    lines.extend([
        f"PHASE: {phase_name}",
        "YOUR ROLE: safety_gate",
        "",
        "CONTENT TO REVIEW:",
        "---",
        content_to_review,
        "---",
        "",
        "Now return your verdict. First non-empty line must be PASS or BLOCK: <reason>.",
    ])

    body = "\n\n".join(lines)
    return ensure_explicit_routing_headers(
        body,
        role="safety_gate",
        agent_base=agent_base,
        project=session_name,
        phase=phase_name,
        subject=goal[:120] if goal else "safety-gate-review",
    )


def build_coordinator_loop_prompt(
    *,
    session_name: str,
    goal: str,
    task_description: str,
    last_role: str,
    last_output_summary: str,
    awaiting_role: str,
    developer_round: int,
    ui_round: int,
    review_round: int,
    safety_round: int,
    allowed_tokens: list[str],
    instruction: str = "",
    agent_base: str = "codex_coordinator",
    project: str = "",
    phase: str = "coordinator-routing",
    subject: str = "coordinator-dispatch",
) -> str:
    """Build a coordinator routing prompt with strict first-line token contract."""
    lines = [
        "OUTPUT CONTRACT (strict — your REPLY's first non-empty line is routing metadata):",
        "Emit exactly ONE routing token on the first non-empty line of your reply, then your prompt body.",
        "Allowed tokens for this turn:",
    ]
    for token in allowed_tokens:
        lines.append(f"  {token}")
    lines.extend([
        "",
        "Do not emit multiple routing tokens.",
        "Do not route worker-to-worker; you alone dispatch the next role.",
        _ROUTING_HANDOFF_INSTRUCTION,
        "",
        f"SESSION: {session_name}",
    ])
    if goal:
        lines.append(f"GOAL: {goal}")
    if task_description:
        lines.append(f"TASK: {task_description}")
    lines.extend([
        f"AWAITING: {awaiting_role}",
        f"ROUNDS: developer={developer_round} ui={ui_round} review={review_round} safety={safety_round}",
    ])
    if last_role:
        lines.append(f"LAST ROLE: {last_role}")
    if last_output_summary:
        lines.append(f"LAST OUTPUT (summary): {last_output_summary[:500]}")
    if instruction:
        lines.append(f"INSTRUCTION: {instruction}")
    lines.extend([
        "",
        "REQUEST CHANGES from reviewer is a normal verdict, not a tooling failure. "
        "Route the next role with explicit TO: headers and revised read-only deliverables "
        "when the session is analysis-only.",
        "",
        "Respond with plain text only. Do not use MCP tools.",
    ])
    body = "\n\n".join(lines)
    return ensure_explicit_routing_headers(
        body,
        role="coordinator",
        agent_base=agent_base,
        project=project or session_name,
        phase=phase,
        subject=subject or (goal[:120] if goal else "coordinator-routing"),
        mode="coordinator-routing",
        from_source="agentchattr-coordinator-loop",
    )


def build_coordinator_loop_ui_lead_prompt(
    *,
    session_name: str,
    channel: str,
    goal: str,
    phase_name: str,
    phase_index: int,
    total_phases: int,
    instruction: str,
    context_messages: list[dict] | None = None,
) -> str:
    """Headless UI lead prompt for coordinator_loop (strict UX_APPROVED contract)."""
    lines = [
        f"SESSION: {session_name}",
        f"CHANNEL: #{channel}",
    ]
    if goal:
        lines.append(f"GOAL: {goal}")
    lines.append(f"PHASE: {phase_name} ({phase_index + 1}/{total_phases})")
    lines.append("YOUR ROLE: ui_lead (UI/UX reviewer)")
    lines.append(f"INSTRUCTION: {instruction}")
    if context_messages:
        lines.append("")
        lines.append("CONTEXT (recent channel messages):")
        for msg in context_messages[-10:]:
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            lines.append(f"  [{sender}]: {text}")
    lines.extend([
        "",
        "OUTPUT CONTRACT (strict — headless store_exec; plain text only):",
        "First line MUST be exactly one of:",
        "UX_APPROVED",
        "REQUEST UX CHANGES",
        "BLOCKED",
        "PASS WITH NOTES is NOT valid in coordinator_loop.",
        "Do not use tools, shell, git, MCP, or file edits.",
    ])
    body = "\n\n".join(lines)
    return ensure_explicit_routing_headers(
        body,
        role="ui_lead",
        agent_base="agy",
        project=session_name,
        phase=phase_name,
        subject=goal[:120] if goal else "ui-lead-review",
    )


def coordinator_loop_worker_output_contract(role: str, *, workspace_bound: bool = False) -> str:
    """Return the strict first-line output contract for a coordinator_loop worker."""
    if role == "developer":
        lines = [
            "OUTPUT CONTRACT (first non-empty line is authoritative):",
            "For progress updates, first line must be exactly:",
            "  PROGRESS",
            "For blockers, first line must be:",
            "  BLOCKER:",
            "For handoff, first line must be:",
            "  READY_FOR_COORDINATOR",
            "For final completion, first line must be one of:",
            "  PASS",
            "  PASS_WITH_NOTES",
            "  REQUEST_CHANGES",
            "  FAIL",
            "Infrastructure timeout only (do not use for normal work):",
            "  WORKER_TIMEOUT",
        ]
        if workspace_bound:
            lines.append(
                "Legacy plain progress phrases are tolerated, but prefer PROGRESS on line 1."
            )
        return "\n".join(lines)
    if role == "ui_lead":
        lines = [
            "OUTPUT CONTRACT (first non-empty line is authoritative):",
            "  PROGRESS — inspection in progress",
            "  UX_APPROVED / REQUEST UX CHANGES / BLOCKED",
        ]
        if workspace_bound:
            lines.append("Use PROGRESS while still reviewing; final line uses UX_* tokens.")
        return "\n".join(lines)
    if role == "reviewer":
        return (
            "OUTPUT CONTRACT: First line MUST be one of: "
            "PASS, PASS WITH NOTES, REQUEST CHANGES, BLOCKED."
        )
    return ""


# ---------------------------------------------------------------------------
# Relay queue metadata
# ---------------------------------------------------------------------------

@dataclass
class RelayTurnMeta:
    """Structured metadata for a session relay turn queue entry."""
    kind: str = "session_turn"
    session_id: int = 0
    phase: int = 0
    turn: int = 0
    role: str = ""
    # The session's channel. Carried in the metadata so the wrapper can relay the
    # agent's reply back to the SAME channel the session runs in (not a hardcoded
    # default). Without this the reply path defaults to "general".
    channel: str = "general"
    relay_mode: bool = True
    disable_mcp: bool = True

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "phase": self.phase,
            "turn": self.turn,
            "role": self.role,
            "channel": self.channel,
            "relay_mode": self.relay_mode,
            "disable_mcp": self.disable_mcp,
        }


def make_relay_queue_entry(
    *,
    prompt: str,
    session_id: int,
    phase: int,
    turn: int,
    role: str,
    channel: str = "general",
    workspace_policy_context: dict | None = None,
) -> dict:
    """Build a queue entry dict for a relay session turn.

    Includes relay_mode and disable_mcp flags so the wrapper knows to
    skip MCP injection and treat the agent as text-in/text-out.
    """
    import time

    meta = RelayTurnMeta(
        session_id=session_id,
        phase=phase,
        turn=turn,
        role=role,
        channel=channel,
    )

    entry = {
        "sender": "session-engine",
        "text": f"[session relay turn: session={session_id} phase={phase} turn={turn}]",
        "time": time.strftime("%H:%M:%S"),
        "channel": channel,
        "prompt": prompt,
        "relay_meta": meta.to_dict(),
    }
    if workspace_policy_context:
        entry["workspace_policy_context"] = dict(workspace_policy_context)
    return entry


# ---------------------------------------------------------------------------
# CodexSafe verdict parser
# ---------------------------------------------------------------------------

@dataclass
class SafetyVerdict:
    """Parsed result of a CodexSafe safety gate response."""
    passed: bool
    reason: str
    raw_output: str


_PASS_PATTERN = re.compile(r"^PASS$")
_BLOCK_PATTERN = re.compile(r"^BLOCK:\s*(.+)$")
# A "verdict-like control line" begins with the word PASS or BLOCK. This catches
# PASS, PASS:, PASS WITH NOTES, BLOCK, BLOCK: <reason>, etc. (case-sensitive, to
# match the strict verdict contract). Used to reject conflicting/mixed verdicts
# where a leading PASS is followed by a second verdict line.
_VERDICT_LIKE_PATTERN = re.compile(r"^(PASS|BLOCK)\b")


def _is_verdict_like_line(line: str) -> bool:
    """True if the line looks like a verdict control line (PASS/BLOCK family)."""
    return bool(_VERDICT_LIKE_PATTERN.match(line))


def parse_safety_verdict(output: str | None) -> SafetyVerdict:
    """Parse a CodexSafe safety gate response into a SafetyVerdict.

    Rules:
    - Accept exactly PASS on the first non-empty line.
    - Accept exactly BLOCK: <reason> on the first non-empty line.
    - Anything else is BLOCK.
    - Empty output is BLOCK.
    - PASS WITH NOTES is not accepted.
    - Markdown preambles, bullets, code fences, or malformed output is BLOCK.
    - MIXED/CONFLICTING verdicts are BLOCK: if the first line is PASS but any
      later non-empty line is a verdict-like control line (PASS, PASS:, PASS
      WITH NOTES, BLOCK, BLOCK: ...), the verdict is rejected as ambiguous.
    - raw_output is always preserved for safety evidence.
    """
    if not output or not output.strip():
        return SafetyVerdict(
            passed=False,
            reason="empty output from safety gate",
            raw_output=output or "",
        )

    non_empty = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not non_empty:
        return SafetyVerdict(
            passed=False,
            reason="no non-empty line in safety gate output",
            raw_output=output,
        )

    first_line = non_empty[0]
    rest = non_empty[1:]

    if _PASS_PATTERN.match(first_line):
        # Reject conflicting/mixed verdicts — a leading PASS must not be
        # followed by any further verdict-like control line.
        conflicting = next((ln for ln in rest if _is_verdict_like_line(ln)), None)
        if conflicting is not None:
            return SafetyVerdict(
                passed=False,
                reason=f"mixed/conflicting verdict: PASS followed by '{conflicting[:80]}'",
                raw_output=output,
            )
        return SafetyVerdict(passed=True, reason="", raw_output=output)

    block_match = _BLOCK_PATTERN.match(first_line)
    if block_match:
        # A leading BLOCK is authoritative regardless of later lines — never
        # let a trailing PASS override an initial BLOCK.
        return SafetyVerdict(
            passed=False,
            reason=block_match.group(1).strip(),
            raw_output=output,
        )

    return SafetyVerdict(
        passed=False,
        reason=f"malformed safety verdict: {first_line[:100]}",
        raw_output=output,
    )


# ---------------------------------------------------------------------------
# Relay eligibility
# ---------------------------------------------------------------------------

def is_relay_eligible(agent_base: str) -> bool:
    """Check if an agent base is eligible for relay-mode session execution."""
    return agent_base.lower() in RELAY_ELIGIBLE_AGENTS


def is_relay_queue_entry(entry: dict) -> bool:
    """Check if a queue entry is a relay session turn."""
    meta = entry.get("relay_meta", {})
    return meta.get("relay_mode", False) and meta.get("disable_mcp", False)


# ---------------------------------------------------------------------------
# Workflow verdict parser (sandbox orchestration flow)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowVerdict:
    """Parsed result of a workflow participant's verdict output."""
    token: str
    passed: bool
    needs_rework: bool
    raw_output: str
    notes: str = ""

AGY_TOKENS = {
    "PASS": {"passed": True, "needs_rework": False},
    "PASS WITH NOTES": {"passed": True, "needs_rework": False},
    "REQUEST UX CHANGES": {"passed": False, "needs_rework": True},
    "BLOCKED": {"passed": False, "needs_rework": False},
}

CODEX_REVIEWER_TOKENS = {
    "PASS": {"passed": True, "needs_rework": False},
    "PASS WITH NOTES": {"passed": True, "needs_rework": False},
    "REQUEST CHANGES": {"passed": False, "needs_rework": True},
    "BLOCKED": {"passed": False, "needs_rework": False},
}

DEVELOPER_TOKENS = {
    "READY_FOR_AGY_REVIEW": {"passed": True, "needs_rework": False},
    "READY_FOR_CODEX_REVIEW": {"passed": True, "needs_rework": False},
    "READY_FOR_REVIEW_PACKAGE": {"passed": True, "needs_rework": False},
    "BLOCKED": {"passed": False, "needs_rework": False},
}

_ALL_WORKFLOW_TOKENS = set(AGY_TOKENS) | set(CODEX_REVIEWER_TOKENS) | set(DEVELOPER_TOKENS)


def parse_workflow_verdict(output: str | None, accepted_tokens: dict) -> WorkflowVerdict:
    """Parse a workflow participant's output into a WorkflowVerdict.

    First non-empty line is matched case-insensitively against accepted_tokens.
    Ambiguous, empty, or unrecognised output fails closed (token="AMBIGUOUS",
    passed=False, needs_rework=False). Does NOT alter CodexSafe safety-gate
    verdict behaviour — that path uses parse_safety_verdict exclusively.
    """
    if not output or not output.strip():
        return WorkflowVerdict(
            token="AMBIGUOUS", passed=False, needs_rework=False,
            raw_output=output or "",
            notes="empty output from workflow participant",
        )

    non_empty = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not non_empty:
        return WorkflowVerdict(
            token="AMBIGUOUS", passed=False, needs_rework=False,
            raw_output=output,
            notes="no non-empty line in workflow output",
        )

    first_line = non_empty[0]
    first_upper = first_line.upper()

    # Build case-insensitive lookup
    upper_map = {k.upper(): k for k in accepted_tokens}

    if first_upper not in upper_map:
        return WorkflowVerdict(
            token="AMBIGUOUS", passed=False, needs_rework=False,
            raw_output=output,
            notes=f"unrecognised verdict: {first_line[:100]}",
        )

    canonical = upper_map[first_upper]
    spec = accepted_tokens[canonical]
    rest = non_empty[1:]

    # Check for conflicting verdict-like lines using ALL known workflow tokens
    # (not just the current role's accepted_tokens) so a cross-role verdict
    # token on a later line is also caught as ambiguous.
    all_upper = {k.upper() for k in _ALL_WORKFLOW_TOKENS}
    conflicting = next(
        (ln for ln in rest if ln.strip().upper() in all_upper), None
    )
    if conflicting is not None:
        return WorkflowVerdict(
            token="AMBIGUOUS", passed=False, needs_rework=False,
            raw_output=output,
            notes=f"mixed/conflicting verdict: {canonical} followed by '{conflicting[:80]}'",
        )

    notes_text = "\n".join(rest) if rest else ""

    return WorkflowVerdict(
        token=canonical,
        passed=spec["passed"],
        needs_rework=spec["needs_rework"],
        raw_output=output,
        notes=notes_text,
    )
