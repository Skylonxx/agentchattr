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

# Agents authorized for relay-mode session execution
RELAY_ELIGIBLE_AGENTS = frozenset({"codex", "codexsafe"})


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
) -> str:
    """Build a sealed session-turn prompt for a relay-mode agent.

    The prompt contains all context the agent needs. It must NOT reference
    chat_read, chat_send, or any MCP tool usage. The agent returns plain
    text only.
    """
    lines = [
        f"SESSION: {session_name}",
    ]
    if goal:
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

    return "\n\n".join(lines)


def build_safety_gate_prompt(
    *,
    session_name: str,
    goal: str,
    phase_name: str,
    content_to_review: str,
    agent_base: str = "",
) -> str:
    """Build a safety gate prompt for CodexSafe relay turns.

    The agent must respond with exactly PASS or BLOCK: <reason> on the
    first non-empty line. Any other format is treated as BLOCK.
    """
    lines = [
        f"SESSION: {session_name}",
        f"GOAL: {goal}",
        f"PHASE: {phase_name}",
        "YOUR ROLE: safety_gate",
        "",
        "CONTENT TO REVIEW:",
        "---",
        content_to_review,
        "---",
        "",
        "VERDICT FORMAT (strict):",
        "Your FIRST non-empty line must be exactly one of:",
        "  PASS",
        "  BLOCK: <reason>",
        "",
        "Do not write anything before the verdict line.",
        "Do not use Markdown, bullets, or code fences.",
        "Do not write PASS WITH NOTES or any variant.",
        "Do not use MCP tools. Do not call chat_read or chat_send.",
        "Do not run shell commands. Do not edit files.",
    ]

    return "\n\n".join(lines)


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
    relay_mode: bool = True
    disable_mcp: bool = True

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "phase": self.phase,
            "turn": self.turn,
            "role": self.role,
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
    )

    entry = {
        "sender": "session-engine",
        "text": f"[session relay turn: session={session_id} phase={phase} turn={turn}]",
        "time": time.strftime("%H:%M:%S"),
        "channel": channel,
        "prompt": prompt,
        "relay_meta": meta.to_dict(),
    }
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
