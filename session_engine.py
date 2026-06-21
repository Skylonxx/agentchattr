"""Session engine — orchestrates structured multi-agent sessions."""

import logging
import threading
import time

from session_relay import (
    build_relay_prompt,
    build_safety_gate_prompt,
    is_relay_eligible,
    make_relay_queue_entry,
    parse_safety_verdict,
)

log = logging.getLogger(__name__)

# Dissent mandate injected for review/critique roles
_DISSENT_LINE = "Provide your own independent analysis. Do not repeat or defer to other participants."

# Roles that get the dissent mandate
_DISSENT_ROLES = {"reviewer", "red_team", "critic", "challenger", "against"}

# Roles treated as safety gates (CodexSafe)
_SAFETY_GATE_ROLES = {"safety_gate", "safety", "gate", "review_gate"}

# Agents that are NEVER permitted to occupy a safety/verdict-parsed role. The
# dry-run Claude responder (``claude_dryrun``) is a text-in/text-out RESPONDER
# only; CodexSafe remains the sole permitted safety gate. Enforced centrally
# (start_session refuses, and _check_safety_block refuses to verdict-parse) so a
# miscast can never cause Claude output to be read as a PASS/BLOCK verdict.
# Production ``claude`` is handled separately by relay eligibility (it is not in
# RELAY_ELIGIBLE_AGENTS); this set only names the dry-run responder identity.
_SAFETY_ROLE_RESTRICTED_AGENTS = frozenset({"claude_dryrun"})


class RoleGuardResult:
    """Result of :func:`validate_relay_participant_roles` (pure, fail-closed)."""

    __slots__ = ("ok", "reason", "rejected_role", "rejected_agent")

    def __init__(self, ok, reason="", rejected_role=None, rejected_agent=None):
        self.ok = ok
        self.reason = reason
        self.rejected_role = rejected_role
        self.rejected_agent = rejected_agent


def validate_relay_participant_roles(role_to_agent, *,
                                     restricted_agents=_SAFETY_ROLE_RESTRICTED_AGENTS,
                                     safety_roles=_SAFETY_GATE_ROLES):
    """Pure, fail-closed safety-role guard for relay participant casting.

    Rejects any role->agent mapping that casts a restricted agent (e.g.
    ``claude_dryrun``) into a safety/verdict-parsed role. ``role_to_agent`` maps a
    role name to an agent identity (name or resolved base). Returns a
    :class:`RoleGuardResult`; ``ok`` is False on the FIRST violation.

    Allows ``safety_gate -> codexsafe`` and ``responder -> claude_dryrun``. Role
    matching is case-insensitive and exact against ``safety_roles``, consistent
    with :meth:`SessionEngine._check_safety_block`. This is the single central
    helper that the session-start path (and, in future, template-load/
    registration validation) call so the rule lives in exactly one place.
    """
    if not isinstance(role_to_agent, dict):
        return RoleGuardResult(False, "cast must be a role->agent mapping")
    for role, agent in role_to_agent.items():
        if not isinstance(role, str):
            return RoleGuardResult(False, f"invalid role key: {role!r}", role, agent)
        if agent in restricted_agents and role.lower() in safety_roles:
            return RoleGuardResult(
                False,
                f"agent '{agent}' may not occupy safety role '{role}' "
                f"(CodexSafe is the only permitted safety gate)",
                role, agent,
            )
    return RoleGuardResult(True, "")


class SessionEngine:
    """Orchestrates session turn flow on top of existing chat infrastructure.

    Listens to message store callbacks, advances session state, and triggers
    agents via the AgentTrigger system.
    """

    def __init__(self, session_store, message_store, agent_trigger, registry=None):
        self._store = session_store
        self._messages = message_store
        self._trigger = agent_trigger
        self._registry = registry
        self._lock = threading.Lock()

        # Hook into message stream
        self._messages.on_message(self._on_message)

    # --- Public API ---

    def start_session(self, template_id: str, channel: str, cast: dict,
                      started_by: str, goal: str = "") -> dict | None:
        """Start a new session. Returns the session dict or None on failure."""
        # Central safety-role guard (fail-closed). A restricted dry-run agent
        # (claude_dryrun) must never be cast into a safety/verdict-parsed role;
        # refuse to even create the session. Bases are resolved so a renamed
        # instance is still caught. CodexSafe remains the only safety gate.
        role_to_id = {role: self._restricted_identity(agent)
                      for role, agent in (cast or {}).items()}
        guard = validate_relay_participant_roles(role_to_id)
        if not guard.ok:
            log.warning("Session start refused (safety-role guard): %s", guard.reason)
            return None

        session = self._store.create(
            template_id=template_id,
            channel=channel,
            cast=cast,
            started_by=started_by,
            goal=goal,
        )
        if not session:
            return None

        log.info("Session %d started: %s in #%s", session["id"],
                 session["template_name"], channel)

        # Trigger the first participant
        self._trigger_current(session)
        return session

    def emit_current_phase_banner(self, session: dict):
        """Post the banner for the session's current phase."""
        tmpl = self._store.get_template(session.get("template_id", ""))
        if not tmpl:
            return

        phases = tmpl.get("phases", [])
        phase_idx = session.get("current_phase", 0)
        if phase_idx >= len(phases):
            return

        phase = phases[phase_idx]
        self._messages.add(
            sender="system",
            text=f"Phase: {phase['name']}",
            msg_type="session_phase",
            channel=session.get("channel", "general"),
            metadata={
                "session_id": session["id"],
                "phase": phase_idx,
                "phase_name": phase["name"],
            },
        )

    def end_session(self, session_id: int, reason: str = "ended by user") -> dict | None:
        """End a session early."""
        session = self._store.interrupt(session_id, reason)
        if session:
            log.info("Session %d interrupted: %s", session_id, reason)
        return session

    def get_active(self, channel: str) -> dict | None:
        """Get the active session for a channel, enriched with phase info."""
        session = self._store.get_active(channel)
        if not session:
            return None
        return self._enrich(session)

    def get_allowed_agent(self, channel: str) -> str | None:
        """If a session is active on this channel, return the agent whose turn it is.
        Returns None if no session is active (meaning all agents are allowed)."""
        session = self._store.get_active(channel)
        if not session or session.get("state") not in ("active", "waiting"):
            return None
        return self._get_expected_agent(session)

    def list_active(self) -> list[dict]:
        """List all active/waiting/paused sessions, enriched for the frontend."""
        active = []
        for session in self._store.list_all():
            if session.get("state") in ("active", "waiting", "paused"):
                active.append(self._enrich(session))
        return active

    def resume_active_sessions(self):
        """On server restart, resume any sessions that were in progress.

        Only re-trigger 'active' sessions. 'waiting' sessions already had
        their trigger sent before the restart — re-triggering would
        double-queue the same participant.
        """
        for session in self._store.list_all():
            if session.get("state") == "active":
                log.info("Resuming session %d (%s) from phase %d, turn %d",
                         session["id"], session.get("template_name", "?"),
                         session["current_phase"], session["current_turn"])
                self._trigger_current(session)

    def _is_agent(self, name: str) -> bool:
        """Check if name belongs to a registered agent (not a human)."""
        if self._registry:
            return self._registry.is_registered(name)
        return False

    # --- Message callback ---

    def _on_message(self, msg: dict):
        """Called on every new chat message. Checks if it advances a session."""
        channel = msg.get("channel", "general")
        sender = msg.get("sender", "")

        # Ignore system-generated messages (banners, phase markers, etc.)
        if sender == "system" or msg.get("type", "chat") != "chat":
            return

        session = self._store.get_active(channel)
        if not session:
            return

        expected_agent = self._get_expected_agent(session)
        if not expected_agent:
            return

        cast_agents = set(session.get("cast", {}).values())
        sender_is_agent = self._is_agent(sender)

        # Agent not in this session's cast — ignore
        if sender_is_agent and sender not in cast_agents:
            return

        # Human spoke but it's not their turn — pause if an agent is expected
        if not sender_is_agent and sender != expected_agent and self._is_agent(expected_agent):
            self._store.pause(session["id"])
            log.info("Session %d paused: human interruption by %s", session["id"], sender)
            return

        if sender == expected_agent:
            if session["state"] == "paused":
                self._store.resume(session["id"])
            session["_last_msg"] = msg
            threading.Timer(0.3, self._advance, args=(session, msg["id"])).start()
            return

        # Wrong agent spoke - ignore
        return

    # --- Engine core ---

    def _check_safety_block(self, session: dict, msg: dict) -> bool:
        """Check if a safety gate agent returned a BLOCK verdict.

        Returns True if the session should be halted (BLOCK or malformed).
        Returns False if the verdict is PASS and the session should continue.

        Product contract (role-scoped): the safety gate is enforced ONLY when
        the current cast role is a safety-gate role (see _SAFETY_GATE_ROLES).
        Enforcement follows the assigned ROLE, never the agent's identity:
          - Any agent cast into a safety-gate role IS strictly verdict-parsed
            (malformed/empty/mixed output auto-BLOCKs).
          - CodexSafe cast into a NON-safety role is NOT verdict-parsed; its
            output flows through as ordinary content.
        The responding agent must still be relay-eligible for a relay verdict
        to be parsed. Role-string matching is exact (case-insensitive): a role
        name outside _SAFETY_GATE_ROLES — including near-misses such as
        'safety-gate' — is NOT treated as a gate, so name the gate role
        exactly (e.g. 'safety_gate').
        """
        expected_agent = self._get_expected_agent(session)
        if not expected_agent:
            return False

        agent_base = self._get_agent_base(expected_agent)
        if not is_relay_eligible(agent_base):
            return False

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return False

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]
        if phase_idx >= len(phases):
            return False

        phase = phases[phase_idx]
        participants = phase.get("participants", [])
        if turn_idx >= len(participants):
            return False

        role = participants[turn_idx]
        if role.lower() not in _SAFETY_GATE_ROLES:
            return False

        # Defense in depth: a restricted dry-run agent (claude_dryrun) must NEVER
        # be verdict-parsed. start_session should already have refused such a
        # cast; if one somehow reached here (e.g. a session created out-of-band),
        # fail closed by HALTING rather than reading Claude output as a verdict.
        if agent_base in _SAFETY_ROLE_RESTRICTED_AGENTS:
            log.warning("Session %d: refusing to verdict-parse restricted agent "
                        "%s in safety role '%s' — halting (CodexSafe is the only "
                        "safety gate)", session["id"], expected_agent, role)
            self._store.interrupt(
                session["id"],
                f"safety-role guard: {expected_agent} may not be a safety gate")
            return True

        output = msg.get("text", "")
        verdict = parse_safety_verdict(output)

        if verdict.passed:
            log.info("Session %d: safety gate PASS from %s", session["id"], expected_agent)
            return False

        log.warning("Session %d: safety gate BLOCK from %s: %s",
                    session["id"], expected_agent, verdict.reason)

        channel = session.get("channel", "general")
        self._messages.add(
            sender="system",
            text=f"Safety gate BLOCK: {verdict.reason}",
            msg_type="session_safety_block",
            channel=channel,
            metadata={
                "session_id": session["id"],
                "blocked_by": expected_agent,
                "reason": verdict.reason,
                "raw_output": verdict.raw_output[:500],
            },
        )

        self._store.interrupt(session["id"],
                              f"safety gate BLOCK: {verdict.reason}")
        return True

    def _advance(self, session: dict, message_id: int):
        """Advance session after the expected agent has responded."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            self._store.interrupt(session["id"], "template not found")
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            self._store.complete(session["id"], message_id)
            return

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        # Check for safety gate BLOCK before advancing
        if session.get("_last_msg"):
            if self._check_safety_block(session, session["_last_msg"]):
                return

        next_turn = turn_idx + 1
        if next_turn < len(participants):
            session = self._store.advance_turn(session["id"], message_id)
            if session:
                self._trigger_current(session)
        else:
            next_phase = phase_idx + 1
            if next_phase < len(phases):
                session = self._store.advance_phase(session["id"], message_id)
                if session:
                    next_phase_obj = phases[next_phase]
                    self._messages.add(
                        sender="system",
                        text=f"Phase: {next_phase_obj['name']}",
                        msg_type="session_phase",
                        channel=session.get("channel", "general"),
                        metadata={"session_id": session["id"],
                                  "phase": next_phase, "phase_name": next_phase_obj["name"]},
                    )
                    self._trigger_current(session)
            else:
                is_output = phase.get("is_output", False)
                self._store.complete(session["id"],
                                     message_id if is_output else None)
                log.info("Session %d complete", session["id"])

    def _get_agent_base(self, agent_name: str) -> str:
        """Get the base family name for a registered agent."""
        if self._registry:
            inst = self._registry.get_instance(agent_name)
            if inst:
                return inst.get("base", agent_name)
        return agent_name

    def _restricted_identity(self, agent_name: str) -> str:
        """Resolve an agent to the identity used for safety-role restriction.

        Prefers the resolved base so a renamed instance (base ``claude_dryrun``)
        is still caught; falls back to matching the cast name itself. Returns the
        restricted token when either matches, else the base.
        """
        base = self._get_agent_base(agent_name)
        if base in _SAFETY_ROLE_RESTRICTED_AGENTS:
            return base
        if agent_name in _SAFETY_ROLE_RESTRICTED_AGENTS:
            return agent_name
        return base

    def _trigger_current(self, session: dict):
        """Trigger the agent whose turn it is."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            return

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        if turn_idx >= len(participants):
            return

        role = participants[turn_idx]
        cast = session.get("cast", {})
        agent = cast.get(role)

        if not agent:
            log.warning("Session %d: no agent cast for role '%s'", session["id"], role)
            self._store.interrupt(session["id"], f"no agent for role '{role}'")
            return

        if not self._is_agent(agent):
            self._store.set_waiting(session["id"], agent)
            return

        self._store.set_waiting(session["id"], agent)

        agent_base = self._get_agent_base(agent)
        channel = session.get("channel", "general")

        if is_relay_eligible(agent_base):
            self._trigger_relay(session, tmpl, phase, phase_idx, turn_idx,
                                role, agent, agent_base, channel)
        else:
            prompt = self._assemble_prompt(session, tmpl, phase, role)
            log.info("Session %d: triggering %s (%s) for phase '%s'",
                     session["id"], agent, role, phase["name"])
            try:
                self._trigger.trigger_sync(agent, channel=channel, prompt=prompt)
            except Exception as exc:
                log.error("Session %d: failed to trigger %s: %s",
                          session["id"], agent, exc)

    def _trigger_relay(self, session, tmpl, phase, phase_idx, turn_idx,
                       role, agent, agent_base, channel):
        """Trigger a relay-mode agent (Codex/CodexSafe) as text-in/text-out."""
        is_safety = role.lower() in _SAFETY_GATE_ROLES

        if is_safety:
            content = self._get_last_turn_content(session, channel)
            prompt = build_safety_gate_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                content_to_review=content,
                agent_base=agent_base,
            )
        else:
            context_messages = self._get_recent_context(channel)
            prompt = build_relay_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                phase_index=phase_idx,
                total_phases=len(tmpl.get("phases", [])),
                role=role,
                instruction=phase.get("prompt", ""),
                context_messages=context_messages,
                agent_base=agent_base,
            )

        relay_entry = make_relay_queue_entry(
            prompt=prompt,
            session_id=session["id"],
            phase=phase_idx,
            turn=turn_idx,
            role=role,
            channel=channel,
        )

        log.info("Session %d: relay-triggering %s (%s) for phase '%s' [relay_mode]",
                 session["id"], agent, role, phase["name"])

        try:
            self._trigger.trigger_sync(agent, channel=channel, relay_entry=relay_entry)
        except Exception as exc:
            log.error("Session %d: failed to relay-trigger %s: %s",
                      session["id"], agent, exc)

    def _get_last_turn_content(self, session: dict, channel: str) -> str:
        """Get the content from the last turn for safety gate review."""
        try:
            recent = self._messages.get_recent(count=5, channel=channel)
        except Exception:
            # Do NOT swallow silently: an error here means the safety gate
            # reviews empty content (a previous bug passed an invalid `limit=`
            # kwarg, which was hidden by a bare except). Log it loudly.
            log.exception(
                "Session %s: failed to read recent messages in #%s for safety gate",
                session.get("id"), channel,
            )
            return "(no content available for review)"
        for msg in reversed(recent):
            if msg.get("sender") != "system" and msg.get("type", "chat") == "chat":
                return msg.get("text", "")
        return "(no content available for review)"

    def _get_recent_context(self, channel: str) -> list[dict]:
        """Get recent messages for relay prompt context."""
        try:
            return self._messages.get_recent(count=10, channel=channel)
        except Exception:
            log.exception("Failed to read recent context messages in #%s", channel)
            return []

    def _assemble_prompt(self, session: dict, tmpl: dict, phase: dict,
                         role: str) -> str:
        """Build the session-aware prompt for an agent."""
        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        total_phases = len(phases)

        channel = session.get("channel", "general")
        lines = [
            f"SESSION: {tmpl.get('name', '?')}",
        ]
        if session.get("goal"):
            lines.append(f"GOAL: {session['goal']}")
        lines.append(f"PHASE: {phase['name']} ({phase_idx + 1}/{total_phases})")
        lines.append(f"YOUR ROLE: {role}")
        lines.append(f"INSTRUCTION: {phase.get('prompt', '')}")

        # Dissent mandate for review/critique roles
        if role.lower() in _DISSENT_ROLES:
            lines.append(f"\n{_DISSENT_LINE}")

        lines.append("")
        lines.append(f"IMPORTANT: You MUST respond using the 'chat_send' tool in the #{channel} channel. "
                      "The session flow is blocked until your message appears in the chat. "
                      "Do NOT respond only in your terminal.")
        lines.append("Read recent messages in the channel for context (use 'chat_read' or 'mcp read'), "
                      "then post your response. Stay focused on the session goal.")

        # Use double newlines to ensure separation in TUIs that might collapse single newlines
        return "\n\n".join(lines)

    def _get_expected_agent(self, session: dict) -> str | None:
        """Get the agent name expected to respond next."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return None

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            return None

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        if turn_idx >= len(participants):
            return None

        role = participants[turn_idx]
        cast = session.get("cast", {})
        return cast.get(role)

    def _enrich(self, session: dict) -> dict:
        """Add computed fields to a session dict for the frontend."""
        tmpl = self._store.get_template(session["template_id"])
        if tmpl:
            phases = tmpl.get("phases", [])
            session["total_phases"] = len(phases)
            phase_idx = session["current_phase"]
            if phase_idx < len(phases):
                phase = phases[phase_idx]
                session["phase_name"] = phase["name"]
                participants = phase.get("participants", [])
                turn_idx = session["current_turn"]
                if turn_idx < len(participants):
                    role = participants[turn_idx]
                    session["current_role"] = role
                    session["current_agent"] = session.get("cast", {}).get(role)
        return session
