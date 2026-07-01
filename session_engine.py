"""Session engine — orchestrates structured multi-agent sessions."""

import logging
import threading
import time

from session_relay import (
    build_relay_prompt,
    build_readonly_context_reviewer_prompt,
    build_readonly_reviewer_context_packet,
    build_safety_gate_prompt,
    build_coordinator_loop_prompt,
    build_coordinator_loop_ui_lead_prompt,
    build_scoped_write_worker_prompt,
    coordinator_loop_worker_output_contract,
    is_readonly_no_tool_reviewer_policy,
    is_relay_eligible,
    make_relay_queue_entry,
    parse_safety_verdict,
    parse_workflow_verdict,
    role_uses_headless_scoped_workspace,
    SafetyVerdict,
    AGY_TOKENS,
    CODEX_REVIEWER_TOKENS,
    DEVELOPER_TOKENS,
)
from report_orchestration import (
    DEFAULT_ALLOWED_REPORT_ROOTS,
    DEFAULT_MAX_REPORT_PROMPT_CHARS,
    build_report_orchestrated_dispatch_prompt,
    format_handoff_repair_limit_blocker,
    format_handoff_validation_diagnostics,
    is_report_orchestrated_policy,
    verify_report_write_permission,
)

log = logging.getLogger(__name__)

# Dissent mandate injected for review/critique roles
_DISSENT_LINE = "Provide your own independent analysis. Do not repeat or defer to other participants."

# Roles that get the dissent mandate
_DISSENT_ROLES = {"reviewer", "red_team", "critic", "challenger", "against"}


def build_store_exec_session_prompt(
    *,
    session_name: str,
    channel: str,
    goal: str,
    phase_name: str,
    phase_index: int,
    total_phases: int,
    role: str,
    instruction: str,
    context_messages: list[dict] | None = None,
) -> str:
    """Build a plain-text session prompt for headless store_exec agents (e.g. AGY).

    Inlines channel context so the agent does not need MCP/TUI tools. The output
    contract requires a first-line UX verdict token and forbids tool usage.
    """
    lines = [
        f"SESSION: {session_name}",
        f"CHANNEL: #{channel}",
    ]
    if goal:
        lines.append(f"GOAL: {goal}")
    lines.append(f"PHASE: {phase_name} ({phase_index + 1}/{total_phases})")
    lines.append(f"YOUR ROLE: {role} (UI/UX reviewer)")
    lines.append(f"INSTRUCTION: {instruction}")

    if role.lower() in _DISSENT_ROLES:
        lines.append(_DISSENT_LINE)

    if context_messages:
        lines.append("")
        lines.append("CONTEXT (recent channel messages — use this instead of tools):")
        for msg in context_messages[-10:]:
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            lines.append(f"  [{sender}]: {text}")

    lines.append("")
    lines.append(
        "OUTPUT CONTRACT (strict — headless store_exec; plain text only):\n"
        "Output ONLY plain text.\n"
        "First line MUST be exactly one of:\n"
        "PASS\n"
        "PASS WITH NOTES\n"
        "REQUEST UX CHANGES\n"
        "BLOCKED\n"
        "\n"
        "Do not use tools.\n"
        "Do not list directories.\n"
        "Do not inspect files.\n"
        "Do not use shell.\n"
        "Do not use git.\n"
        "Do not use MCP.\n"
        "Do not create or edit files."
    )

    return "\n\n".join(lines)

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

# Anti-self-review: the workflow coordinator and the independent reviewer must be
# SEPARATE runtime identities. These role-name families are matched (case-
# insensitive) when guarding a cast so a single identity can never be assigned to
# both a coordinator role and a reviewer role in the same session — which would
# collapse the two-Codex separation into single-Codex self-review.
_COORDINATOR_ROLES = {"coordinator", "codex_coordinator", "workflow_coordinator"}
_REVIEWER_ROLES = {"reviewer", "codex_reviewer", "independent_reviewer"}


class SelfReviewGuardResult:
    """Result of :func:`validate_no_self_review` (pure, fail-closed)."""

    __slots__ = ("ok", "reason", "identity")

    def __init__(self, ok, reason="", identity=None):
        self.ok = ok
        self.reason = reason
        self.identity = identity


def validate_no_self_review(role_to_identity, *,
                            coordinator_roles=_COORDINATOR_ROLES,
                            reviewer_roles=_REVIEWER_ROLES):
    """Pure, fail-closed anti-self-review guard.

    Rejects any cast where a coordinator role and a reviewer role resolve to the
    SAME runtime identity/instance. ``role_to_identity`` maps a role name to a
    stable identity token (instance name, identity_id, or resolved base). Role
    matching is case-insensitive and exact against the role-name families.
    Returns a :class:`SelfReviewGuardResult`; ``ok`` is False on the first
    coordinator/reviewer identity collision.

    Legacy ``codex`` used for both roles is rejected the same way (both roles
    resolve to the one ``codex`` identity), so the split coordinator/reviewer
    identities cannot be quietly bypassed by re-using the base key.
    """
    if not isinstance(role_to_identity, dict):
        return SelfReviewGuardResult(False, "cast must be a role->identity mapping")
    coord_ids: dict = {}
    rev_ids: dict = {}
    for role, identity in role_to_identity.items():
        if not isinstance(role, str):
            return SelfReviewGuardResult(False, f"invalid role key: {role!r}")
        rl = role.lower()
        if rl in coordinator_roles:
            coord_ids[identity] = role
        elif rl in reviewer_roles:
            rev_ids[identity] = role
    for identity, crole in coord_ids.items():
        if identity in rev_ids:
            return SelfReviewGuardResult(
                False,
                f"anti-self-review: identity '{identity}' cast as both "
                f"coordinator role '{crole}' and reviewer role "
                f"'{rev_ids[identity]}' (coordinator and reviewer must be "
                f"separate identities)",
                identity,
            )
    return SelfReviewGuardResult(True, "")


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

    def __init__(self, session_store, message_store, agent_trigger, registry=None,
                 sandbox_config: dict | None = None, data_dir: str | None = None):
        self._store = session_store
        self._messages = message_store
        self._trigger = agent_trigger
        self._registry = registry
        self._sandbox_config = sandbox_config or {}
        self._data_dir = data_dir
        self._lock = threading.Lock()

        # Hook into message stream
        self._messages.on_message(self._on_message)

    # --- Public API ---

    def start_session(self, template_id: str, channel: str, cast: dict,
                      started_by: str, goal: str = "",
                      prompt_body: str = "",
                      prompt_id: str = "",
                      workspace_policy: dict | None = None,
                      workspace_policy_hash: str | None = None,
                      workspace_policy_version: int | None = None) -> dict | None:
        """Start a new session. Returns the session dict or None on failure."""
        from session_memo import normalize_goal, normalize_prompt_body

        prompt_body = normalize_prompt_body(prompt_body)
        goal = normalize_goal(goal, prompt_body=prompt_body)
        prompt_id = (prompt_id or "").strip()[:200]
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

        # RBAC cast guard (fail-closed). Delegates to the centralized validators in
        # safety_invariants (lazy import avoids the module import cycle). Enforces:
        #   * codexsafe stays boundary-only — it may never be cast into a non-safety
        #     workflow role (INV-003); and
        #   * no agent may be both an authoring role and the reviewer role —
        #     self-review collapse (INV-007). Authoring roles are centralized in
        #     safety_invariants.AUTHORING_ROLES (coordinator family, developer,
        #     and shipped session-template roles such as builder and implementer),
        #     covering the legacy single-``codex`` reuse.
        # Bases are resolved for the codexsafe check and stable identities for the
        # self-review check, so renamed instances are still caught. This is
        # vocabulary-agnostic: it does NOT map session roles to the external roster,
        # so the internal codex_coordinator/codex_reviewer split is preserved.
        role_to_base = {role: self._get_agent_base(agent)
                        for role, agent in (cast or {}).items()}
        role_to_identity = {role: self._self_review_identity(agent)
                            for role, agent in (cast or {}).items()}
        from safety_invariants import check_session_cast
        cast_guard = check_session_cast(role_to_base, role_to_identity=role_to_identity)
        if not cast_guard.ok:
            log.warning("Session start refused (RBAC cast guard %s): %s",
                        cast_guard.code, cast_guard.reason)
            return None

        create_kwargs = {
            "template_id": template_id,
            "channel": channel,
            "cast": cast,
            "started_by": started_by,
            "goal": goal,
            "prompt_body": prompt_body,
            "prompt_id": prompt_id,
        }
        if workspace_policy is not None:
            create_kwargs["workspace_policy"] = workspace_policy
            create_kwargs["workspace_policy_hash"] = workspace_policy_hash
            create_kwargs["workspace_policy_version"] = workspace_policy_version
        session = self._store.create(**create_kwargs)
        if not session:
            return None

        log.info("Session %d started: %s in #%s", session["id"],
                 session["template_name"], channel)

        # Coordinator-loop sessions: initialize loop state and trigger coordinator
        tmpl = self._store.get_template(template_id)
        if tmpl and tmpl.get("coordinator_loop"):
            self._init_coordinator_loop(session, goal)
            return session

        # Flow-coordinator sessions: initialize FlowState and run intake
        if tmpl and tmpl.get("flow_coordinator"):
            self._init_flow_coordinator(session, goal)
            return session

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

    def _worker_context_from_session(self, session: dict) -> dict:
        policy = session.get("workspace_policy") if isinstance(session.get("workspace_policy"), dict) else {}
        channel = str(session.get("channel") or "general")
        report_paths = list(policy.get("report_paths") or [])
        report_roots = list(policy.get("external_report_write_roots") or [])
        ai_base = next(
            (root for root in report_roots if str(root).replace("\\", "/").lower().endswith("/ai-report")),
            r"C:\Users\Narachat\OneDrive\Ai-Report",
        )
        agy_root = next(
            (root for root in report_roots if str(root).replace("\\", "/").lower().endswith("/ai-report/agy")),
            f"{ai_base}\\agy",
        )
        codex_root = next(
            (root for root in report_roots if str(root).replace("\\", "/").lower().endswith("/ai-report/codex")),
            f"{ai_base}\\codex",
        )
        return {
            "workspace_policy": policy,
            "policy_id": policy.get("policy_id"),
            "policy_mode": policy.get("mode"),
            "prompt_id": session.get("prompt_id"),
            "has_prompt_body": bool(str(session.get("prompt_body") or "").strip()),
            "report_paths": report_paths,
            "allowed_report_roots": report_roots,
            "max_report_prompt_chars": DEFAULT_MAX_REPORT_PROMPT_CHARS,
            "report_paths_by_role": {
                "developer": report_paths[0] if report_paths else "",
                "ui_lead": f"{agy_root}\\{channel}-ux-review.md",
                "reviewer": f"{codex_root}\\{channel}-codex-review.md",
            },
        }

    def _session_workspace_policy_context(
        self, session: dict, role: str, phase_idx: int, turn_idx: int,
    ) -> dict:
        from workspace_policy_runtime import build_session_queue_workspace_context
        return build_session_queue_workspace_context(
            session, role, phase_idx, turn_idx,
        )

    def _make_relay_queue_entry(self, *, prompt: str, session: dict, phase_idx: int,
                                turn_idx: int, role: str, channel: str,
                                handoff_repair: bool = False,
                                trusted_cli_report_bridge_repair: bool = False) -> dict:
        wpc = self._session_workspace_policy_context(
            session, role, phase_idx, turn_idx,
        )
        wpc = dict(wpc)
        cls = self._load_coordinator_loop_state(session)
        if cls is not None:
            wpc["trusted_cli_report_bridge_repair_rounds"] = (
                cls.trusted_cli_report_bridge_repair_rounds
            )
            wpc["max_trusted_cli_report_bridge_repair_rounds"] = (
                cls.max_trusted_cli_report_bridge_repair_rounds
            )
        if handoff_repair or trusted_cli_report_bridge_repair:
            wpc["skip_snapshot_injection"] = True
        if handoff_repair:
            wpc["handoff_repair"] = True
        if trusted_cli_report_bridge_repair:
            wpc["trusted_cli_report_bridge_repair"] = True
        return make_relay_queue_entry(
            prompt=prompt,
            session_id=session["id"],
            phase=phase_idx,
            turn=turn_idx,
            role=role,
            channel=channel,
            workspace_policy_context=wpc,
            handoff_repair=handoff_repair,
            trusted_cli_report_bridge_repair=trusted_cli_report_bridge_repair,
        )

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

        Coordinator-loop sessions with missing/corrupt persisted state fail
        closed (interrupt, no trigger). Terminal coordinator-loop phases are
        not re-triggered. Waiting-session restart for coordinator_loop is
        intentionally not re-queued (same active-only semantics as linear
        sessions).
        """
        from coordinator_loop import CoordinatorPhase

        for session in self._store.list_all():
            if session.get("state") == "active":
                log.info("Resuming session %d (%s) from phase %d, turn %d",
                         session["id"], session.get("template_name", "?"),
                         session["current_phase"], session["current_turn"])
                tmpl = self._store.get_template(session.get("template_id", ""))
                if tmpl and tmpl.get("coordinator_loop"):
                    cls = self._load_coordinator_loop_state(session)
                    if cls is None:
                        self._interrupt_coordinator_loop_state_failure(session)
                        continue
                    if cls.phase in (
                        CoordinatorPhase.FINAL,
                        CoordinatorPhase.BLOCKER,
                        CoordinatorPhase.HALTED,
                    ):
                        continue
                    self._trigger_coordinator_loop(session)
                else:
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
        model_passed = verdict.passed

        if verdict.passed:
            from safety_invariants import check_safety_gate_request

            reviewed = self._get_last_turn_content(
                session, session.get("channel", "general"))
            policy = check_safety_gate_request(
                session.get("goal", ""), reviewed)
            if not policy.ok:
                log.warning(
                    "Session %d: safety gate policy override (model PASS) "
                    "from %s: %s",
                    session["id"], expected_agent, policy.reason,
                )
                verdict = SafetyVerdict(
                    passed=False,
                    reason=f"policy override: {policy.reason}",
                    raw_output=verdict.raw_output,
                )

        if verdict.passed:
            log.info("Session %d: safety gate PASS from %s", session["id"], expected_agent)
            return False

        log.warning(
            "Session %d: safety gate BLOCK from %s (model_passed=%s): %s",
            session["id"], expected_agent, model_passed, verdict.reason,
        )

        channel = session.get("channel", "general")
        block_meta = {
            "session_id": session["id"],
            "blocked_by": expected_agent,
            "reason": verdict.reason,
            "raw_output": verdict.raw_output[:500],
            "model_verdict": "PASS" if model_passed else "BLOCK",
            "effective_verdict": "BLOCK",
        }
        if model_passed and not verdict.passed:
            block_meta["policy_override"] = True
        self._messages.add(
            sender="system",
            text=f"Safety gate BLOCK: {verdict.reason}",
            msg_type="session_safety_block",
            channel=channel,
            metadata=block_meta,
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

        # Coordinator-loop sessions use hub routing instead of linear advance.
        if tmpl.get("coordinator_loop"):
            self._advance_coordinator_loop(session, message_id)
            return

        # Flow-coordinator sessions use verdict-driven routing instead of
        # linear forward advance. Delegate entirely to avoid mixing paths.
        # Fail closed: if a flow-coordinator template is missing its flow_state
        # (persistence failure or malformed session), halt rather than falling
        # through to linear advance.
        if tmpl.get("flow_coordinator"):
            if session.get("flow_state"):
                self._advance_flow_coordinator(session, message_id)
            else:
                log.warning("Session %d: flow_coordinator template but no "
                            "flow_state — halting (fail-closed)", session["id"])
                self._store.interrupt(
                    session["id"],
                    "flow coordinator: missing flow_state (fail-closed)")
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

    def _agent_uses_store_exec(self, agent_name: str) -> bool:
        """True only when registry confirms run_mode == store_exec (fail-closed)."""
        if not self._registry:
            return False
        base = self._get_agent_base(agent_name)
        cfg = self._registry.get_base_config(base)
        if not cfg:
            return False
        return cfg.get("run_mode") == "store_exec"

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

    def _self_review_identity(self, agent_name: str) -> str:
        """Resolve an agent to a stable identity token for the anti-self-review guard.

        Prefers the registry instance ``identity_id`` so two cast names that point
        at the SAME live instance collide (and a renamed instance is still caught);
        falls back to the instance name, then the raw cast name when no registry or
        no live instance is available. This makes 'same instance cast as both
        coordinator and reviewer' detectable, and treats two distinct identities
        (e.g. codex_coordinator vs codex_reviewer) as separate.
        """
        if self._registry:
            inst = self._registry.get_instance(agent_name)
            if inst:
                return inst.get("identity_id") or inst.get("name") or agent_name
        return agent_name

    def _trigger_current(self, session: dict):
        """Trigger the agent whose turn it is."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        if tmpl.get("coordinator_loop"):
            self._trigger_coordinator_loop(session)
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

        if role_uses_headless_scoped_workspace(session, role):
            self._trigger_scoped_workspace_worker(
                session, tmpl, phase, phase_idx, turn_idx, role, agent, channel,
                instruction=phase.get("prompt", ""),
            )
            return

        if is_relay_eligible(agent_base):
            self._trigger_relay(session, tmpl, phase, phase_idx, turn_idx,
                                role, agent, agent_base, channel)
        else:
            prompt = self._assemble_prompt(session, tmpl, phase, role, agent=agent)
            log.info("Session %d: triggering %s (%s) for phase '%s'",
                     session["id"], agent, role, phase["name"])
            try:
                self._trigger.trigger_sync(
                    agent, channel=channel, prompt=prompt,
                    workspace_policy_context=self._session_workspace_policy_context(
                        session, role, phase_idx, turn_idx,
                    ),
                )
            except Exception as exc:
                log.error("Session %d: failed to trigger %s: %s",
                          session["id"], agent, exc)

    def _session_prompt_body(self, session: dict) -> str:
        return str(session.get("prompt_body") or "")

    def _trigger_scoped_workspace_worker(
        self,
        session: dict,
        tmpl: dict,
        phase: dict,
        phase_idx: int,
        turn_idx: int,
        role: str,
        agent: str,
        channel: str,
        *,
        instruction: str = "",
    ):
        """Trigger developer/ui_lead with scoped-write workspace contract (headless exec)."""
        policy = session.get("workspace_policy") or {}
        cls = self._load_coordinator_loop_state(session)
        report_orchestrated = bool(cls and cls.report_orchestrated)
        context_messages = None if report_orchestrated else self._get_recent_context(channel)
        prompt = build_scoped_write_worker_prompt(
            session_name=tmpl.get("name", "?"),
            goal=session.get("goal", ""),
            role=role,
            policy=policy,
            instruction=instruction or phase.get("prompt", ""),
            phase_name=phase.get("name", ""),
            phase_index=phase_idx,
            total_phases=len(tmpl.get("phases", [])),
            context_messages=context_messages,
            prompt_body=self._session_prompt_body(session),
            report_orchestrated=report_orchestrated,
        )
        log.info(
            "Session %d: scoped-workspace trigger %s (%s) for phase '%s'",
            session["id"], agent, role, phase.get("name", "?"),
        )
        try:
            relay_entry = self._make_relay_queue_entry(
                prompt=prompt,
                session=session,
                phase_idx=phase_idx,
                turn_idx=turn_idx,
                role=role,
                channel=channel,
            )
            self._trigger.trigger_sync(agent, channel=channel, relay_entry=relay_entry)
        except Exception as exc:
            log.error(
                "Session %d: failed scoped-workspace trigger %s: %s",
                session["id"], agent, exc,
            )

    def _trigger_relay(self, session, tmpl, phase, phase_idx, turn_idx,
                       role, agent, agent_base, channel):
        """Trigger a relay-mode agent (Codex/CodexSafe) as text-in/text-out."""
        if role_uses_headless_scoped_workspace(session, role):
            self._trigger_scoped_workspace_worker(
                session, tmpl, phase, phase_idx, turn_idx, role, agent, channel,
                instruction=phase.get("prompt", ""),
            )
            return

        is_safety = role.lower() in _SAFETY_GATE_ROLES

        if is_safety:
            content = self._get_last_turn_content(session, channel)
            prompt = build_safety_gate_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                content_to_review=content,
                agent_base=agent_base,
                prompt_body=self._session_prompt_body(session),
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
                prompt_body=self._session_prompt_body(session),
            )

        relay_entry = self._make_relay_queue_entry(
            prompt=prompt,
            session=session,
            phase_idx=phase_idx,
            turn_idx=turn_idx,
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
                         role: str, agent: str | None = None) -> str:
        """Build the session-aware prompt for an agent."""
        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        total_phases = len(phases)

        channel = session.get("channel", "general")

        if agent and self._agent_uses_store_exec(agent):
            return build_store_exec_session_prompt(
                session_name=tmpl.get("name", "?"),
                channel=channel,
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                phase_index=phase_idx,
                total_phases=total_phases,
                role=role,
                instruction=phase.get("prompt", ""),
                context_messages=self._get_recent_context(channel),
            )

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

    # --- Flow coordinator wiring (sandbox-only, opt-in) ---

    _ROLE_TO_VERDICT_TOKENS = {
        "developer": DEVELOPER_TOKENS,
        "ui_lead": AGY_TOKENS,
        "codex_reviewer": CODEX_REVIEWER_TOKENS,
    }

    def _init_flow_coordinator(self, session: dict, goal: str):
        """Initialize flow state on a flow-coordinator session and run intake."""
        from flow_coordinator import FlowState, intake

        fs = FlowState()
        action = intake(fs, goal or session.get("goal", ""))
        self._store.update_flow_state(session["id"], fs.to_dict())

        if action.is_terminal:
            self._handle_flow_terminal(session, fs, None, action)
            return

        self._route_flow_action(session, action)

    def _advance_flow_coordinator(self, session: dict, message_id: int):
        """Advance a flow-coordinator session based on the agent's verdict."""
        from flow_coordinator import (
            FlowState, _halt, on_developer_verdict, on_agy_verdict, on_codex_verdict,
        )
        from flow_transcript import parse_report_path, validate_approved_report_path

        fs_dict = session.get("flow_state", {})
        fs = FlowState.from_dict(fs_dict)

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            self._store.interrupt(session["id"], "template not found")
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        if phase_idx >= len(phases):
            self._store.complete(session["id"], message_id)
            return

        role = phases[phase_idx]["participants"][0]
        output = session.get("_last_msg", {}).get("text", "")

        tokens = self._ROLE_TO_VERDICT_TOKENS.get(role)
        if not tokens:
            log.warning("Session %d: no verdict tokens for role '%s'",
                        session["id"], role)
            self._store.interrupt(session["id"],
                                  f"flow coordinator: unknown role '{role}'")
            return

        verdict = parse_workflow_verdict(output, tokens)
        channel = session.get("channel", "general")

        report_path = parse_report_path(output) or ""
        if role == "developer" and report_path:
            ok, reason = validate_approved_report_path(report_path)
            if not ok:
                import time as _time
                block_reason = f"blocked: invalid report_path: {reason}"
                fs.total_steps += 1
                fs.verdicts.append({
                    "role": "developer",
                    "token": "BLOCKED",
                    "notes": f"invalid report_path: {reason}",
                    "time": _time.time(),
                })
                action = _halt(fs, block_reason)
                self._store.update_flow_state(session["id"], fs.to_dict())
                self._messages.add(
                    sender="system",
                    text=f"[{role}] verdict: BLOCKED (invalid report_path)",
                    msg_type="session_flow_verdict",
                    channel=channel,
                    metadata={"session_id": session["id"], "role": role,
                              "token": "BLOCKED", "phase": fs.phase.value},
                )
                self._handle_flow_terminal(session, fs, message_id, action, output)
                return

        _ROLE_HANDLER = {
            "developer": lambda: on_developer_verdict(
                fs, verdict.token, report_path=report_path, notes=verdict.notes),
            "ui_lead": lambda: on_agy_verdict(
                fs, verdict.token, notes=verdict.notes),
            "codex_reviewer": lambda: on_codex_verdict(
                fs, verdict.token, notes=verdict.notes,
                fix_description=verdict.notes),
        }
        handler = _ROLE_HANDLER.get(role)
        if not handler:
            log.warning("Session %d: flow coordinator has no handler for role '%s'",
                        session["id"], role)
            self._store.interrupt(session["id"],
                                  f"flow coordinator: unhandled role '{role}'")
            return
        action = handler()

        self._store.update_flow_state(session["id"], fs.to_dict())

        self._messages.add(
            sender="system",
            text=f"[{role}] verdict: {verdict.token}",
            msg_type="session_flow_verdict",
            channel=channel,
            metadata={"session_id": session["id"], "role": role,
                      "token": verdict.token, "phase": fs.phase.value},
        )

        if action.is_terminal:
            self._handle_flow_terminal(session, fs, message_id, action, output)
            return

        self._route_flow_action(session, action)

    def _resolve_sandbox_output_root(self) -> str:
        from safety_invariants import SANDBOX_CONFIG_DEFAULTS, normalize_sandbox_config

        sb = normalize_sandbox_config({"sandbox": self._sandbox_config})
        return str(sb.get("flow_start_output_root")
                     or SANDBOX_CONFIG_DEFAULTS["flow_start_output_root"])

    def _export_flow_artifacts(self, session: dict, flow_state: dict,
                               last_output: str = ""):
        """Write transcript + closure markdown; return FlowExportResult."""
        from flow_transcript import FlowExportResult, export_sandbox_flow_artifacts

        channel = session.get("channel", "general")
        messages = self._messages.get_recent(count=500, channel=channel)
        tmpl = self._store.get_template(session.get("template_id", ""))
        result = export_sandbox_flow_artifacts(
            session=session,
            template=tmpl,
            flow_state=flow_state,
            messages=messages,
            output_root=self._resolve_sandbox_output_root(),
            data_dir=self._data_dir,
            last_output_snippet=last_output,
        )
        if not result.ok:
            log.warning("Session %s: flow export failed: %s",
                        session.get("id"), result.error)
        return result

    def _handle_flow_terminal(self, session: dict, fs, message_id: int,
                              action, last_output: str = ""):
        """Complete or interrupt a flow-coordinator session with V2-C exports."""
        from flow_coordinator import FlowState
        from flow_transcript import (
            format_flow_export_error_message,
            format_flow_export_system_message,
        )

        if not isinstance(fs, FlowState):
            fs = FlowState.from_dict(fs if isinstance(fs, dict) else {})

        channel = session.get("channel", "general")
        session_id = session["id"]
        fs_dict = fs.to_dict()
        original_halt_reason = fs.halt_reason or ""

        export_result = self._export_flow_artifacts(
            session, fs_dict, last_output=last_output)

        if not export_result.ok:
            export_error = export_result.error or "unknown export error"
            self._messages.add(
                sender="system",
                text=format_flow_export_error_message(
                    session=session,
                    error=export_error,
                ),
                msg_type="session_flow_export_error",
                channel=channel,
                metadata={
                    "session_id": session_id,
                    "export_error": export_error,
                    "final_status": fs_dict.get("phase"),
                },
            )
            if original_halt_reason:
                interrupt_reason = (
                    f"{original_halt_reason}; flow export failed: {export_error}")
            else:
                interrupt_reason = f"flow export failed: {export_error}"
            self._store.interrupt(session_id, interrupt_reason)
            return

        transcript_path = export_result.transcript_path
        closure_path = export_result.closure_path
        if transcript_path or closure_path:
            fs_dict["transcript_path"] = transcript_path
            fs_dict["closure_path"] = closure_path
            self._store.update_flow_state(session_id, fs_dict)

        self._messages.add(
            sender="system",
            text=format_flow_export_system_message(
                session=session,
                flow_state=fs_dict,
                transcript_path=transcript_path,
                closure_path=closure_path,
            ),
            msg_type="session_flow_export",
            channel=channel,
            metadata={
                "session_id": session_id,
                "transcript_path": transcript_path,
                "closure_path": closure_path,
                "final_status": fs_dict.get("phase"),
            },
        )

        if fs.phase.value == "closure":
            self._messages.add(
                sender="system",
                text=f"Flow coordinator closure: {action.prompt_context}",
                msg_type="session_flow_closure",
                channel=channel,
                metadata={"session_id": session_id,
                            "closure_summary": fs.closure_summary},
            )
            self._store.complete(session_id, message_id)
        else:
            self._messages.add(
                sender="system",
                text=f"Flow coordinator halted: {fs.halt_reason}",
                msg_type="session_flow_halted",
                channel=channel,
                metadata={"session_id": session_id,
                          "halt_reason": fs.halt_reason},
            )
            self._store.interrupt(session_id,
                                  f"flow coordinator: {fs.halt_reason}")

    def _route_flow_action(self, session: dict, action):
        """Set the session to the target role's phase and trigger the agent."""
        session_id = session["id"]

        # Reload from store so we never dispatch from a stale in-memory copy.
        session = self._store.get(session_id)
        if not session or session.get("state") in ("complete", "interrupted"):
            return

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        target = action.target_role
        phases = tmpl.get("phases", [])

        target_phase = None
        for i, phase in enumerate(phases):
            participants = phase.get("participants", [])
            role_map = {
                "developer": "developer",
                "agy": "ui_lead",
                "codex": "codex_reviewer",
            }
            mapped_role = role_map.get(target, target)
            if mapped_role in participants:
                target_phase = i
                break

        if target_phase is None:
            log.warning("Session %d: no phase found for target role '%s'",
                        session_id, target)
            self._store.interrupt(session_id,
                                  f"flow coordinator: no phase for '{target}'")
            return

        session = self._store.set_phase_and_turn(session_id, target_phase, 0)
        if not session:
            return

        channel = session.get("channel", "general")
        phase_obj = phases[target_phase]
        self._messages.add(
            sender="system",
            text=f"Phase: {phase_obj['name']}",
            msg_type="session_phase",
            channel=channel,
            metadata={"session_id": session_id,
                      "phase": target_phase,
                      "phase_name": phase_obj["name"]},
        )
        self._trigger_current(session)

    # --- Coordinator loop wiring (opt-in via coordinator_loop: true) ---

    def _load_coordinator_loop_state(self, session: dict):
        """Load coordinator loop state from session. Returns None if missing/corrupt."""
        from coordinator_loop import CoordinatorLoopState

        raw = session.get("coordinator_loop_state")
        if not raw:
            return None
        try:
            return CoordinatorLoopState.from_dict(raw)
        except (ValueError, TypeError, KeyError):
            return None

    def _interrupt_coordinator_loop_state_failure(self, session: dict,
                                                  detail: str = "") -> None:
        """Fail closed when coordinator_loop_state is missing or corrupt."""
        session_id = session["id"]
        reason = (
            "coordinator loop: coordinator_loop_state missing or corrupt "
            "(fail-closed)"
        )
        if detail:
            reason = f"{reason}: {detail}"
        channel = session.get("channel", "general")
        self._messages.add(
            sender="system",
            text=reason,
            msg_type="session_coord_state_error",
            channel=channel,
            metadata={
                "session_id": session_id,
                "error": "coordinator_loop_state missing or corrupt",
            },
        )
        log.warning("Session %d: %s", session_id, reason)
        self._store.interrupt(session_id, reason)

    def _init_coordinator_loop(self, session: dict, goal: str):
        """Initialize coordinator loop state and trigger coordinator intake."""
        from coordinator_loop import on_session_start, resolve_loop_budget_from_session

        policy = session.get("workspace_policy") or {}
        budget = resolve_loop_budget_from_session(session)
        report_orchestrated = is_report_orchestrated_policy(policy)
        if report_orchestrated:
            worker_ctx = self._worker_context_from_session(session)
            by_role = worker_ctx.get("report_paths_by_role") or {}
            expected_paths = list(policy.get("report_paths") or [])
            for role in ("developer", "ui_lead", "reviewer"):
                path = str(by_role.get(role) or "").strip() if isinstance(by_role, dict) else ""
                if path:
                    expected_paths.append(path)
            ok, blocker = verify_report_write_permission(
                policy,
                expected_report_paths=expected_paths,
            )
            if not ok:
                self._store.interrupt(session["id"], blocker)
                return
        cls, action = on_session_start(
            goal or session.get("goal", ""),
            loop_budget=budget,
            session_meta={
                "prompt_id": session.get("prompt_id", ""),
                "workspace_profile": policy.get("policy_id", ""),
                "workspace_mode": policy.get("mode", ""),
                "report_orchestrated": report_orchestrated,
            },
        )
        self._store.update_coordinator_loop_state(session["id"], cls.to_dict())
        self._route_coordinator_loop_action(session, action)

    def _advance_coordinator_loop(self, session: dict, message_id: int):
        """Advance a coordinator-loop session based on role output."""
        from coordinator_loop import (
            COORDINATOR_ROLE,
            CoordinatorAction,
            CoordinatorPhase,
            on_coordinator_output,
            on_worker_output,
        )

        cls = self._load_coordinator_loop_state(session)
        if cls is None:
            log.warning("Session %d: corrupt/missing coordinator_loop_state",
                        session["id"])
            self._interrupt_coordinator_loop_state_failure(session)
            return

        if cls.phase in (
            CoordinatorPhase.FINAL,
            CoordinatorPhase.BLOCKER,
            CoordinatorPhase.HALTED,
        ):
            return

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            self._store.interrupt(session["id"], "template not found")
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        if phase_idx >= len(phases):
            return

        role = phases[phase_idx]["participants"][0]
        output = session.get("_last_msg", {}).get("text", "")
        channel = session.get("channel", "general")

        if role == COORDINATOR_ROLE and cls.awaiting_role != COORDINATOR_ROLE:
            self._route_coordinator_loop_action(
                session,
                CoordinatorAction(
                    target_role=cls.awaiting_role,
                    prompt_context=session.get("coordinator_loop_worker_prompt", ""),
                    routing_body=session.get("coordinator_loop_worker_prompt", ""),
                ),
            )
            return

        if role != COORDINATOR_ROLE and cls.awaiting_role != role:
            log.warning(
                "Session %d: out-of-turn worker output ignored (expected %r, got %r)",
                session["id"], cls.awaiting_role, role,
            )
            return

        if role == COORDINATOR_ROLE:
            action = on_coordinator_output(cls, output)
        else:
            action = on_worker_output(
                cls, role, output,
                worker_context=self._worker_context_from_session(session),
            )

        worker_prompt = None
        safety_artifact = None
        if not action.is_terminal and action.target_role != COORDINATOR_ROLE:
            worker_prompt = action.routing_body or action.prompt_context
            if action.target_role == "safety_gate":
                safety_artifact = worker_prompt or session.get("goal", "")

        self._store.update_coordinator_loop_state(
            session["id"],
            cls.to_dict(),
            worker_prompt=worker_prompt,
            safety_artifact=safety_artifact,
        )

        msg_type = (
            "session_coord_routing" if role == COORDINATOR_ROLE
            else "session_coord_verdict"
        )
        self._messages.add(
            sender="system",
            text=f"[coordinator_loop][{role}] -> {action.target_role or action.terminal_kind}",
            msg_type=msg_type,
            channel=channel,
            metadata={
                "session_id": session["id"],
                "role": role,
                "target_role": action.target_role,
                "phase": cls.phase.value,
                "terminal": action.is_terminal,
            },
        )

        if action.is_terminal:
            self._handle_coordinator_loop_terminal(session, cls, message_id, action)
            return

        self._route_coordinator_loop_action(session, action)

    def _handle_coordinator_loop_terminal(self, session: dict, cls, message_id: int,
                                          action):
        """Complete or interrupt a coordinator-loop session at FINAL/BLOCKER."""
        channel = session.get("channel", "general")
        session_id = session["id"]

        if action.terminal_kind == "final":
            self._messages.add(
                sender="system",
                text=f"Coordinator loop complete: {action.prompt_context[:500]}",
                msg_type="session_coord_final",
                channel=channel,
                metadata={
                    "session_id": session_id,
                    "phase": cls.phase.value,
                    "terminal_kind": "final",
                },
            )
            self._store.complete(session_id, message_id)
            return

        reason = action.prompt_context or cls.blocker_reason or "blocked"
        self._messages.add(
            sender="system",
            text=f"Coordinator loop BLOCKER: {reason[:500]}",
            msg_type="session_coord_blocker",
            channel=channel,
            metadata={
                "session_id": session_id,
                "phase": cls.phase.value,
                "terminal_kind": "blocker",
                "blocker_reason": reason,
            },
        )
        self._store.interrupt(session_id, f"coordinator loop: {reason}")

    def _route_coordinator_loop_action(self, session: dict, action):
        """Set session to target role phase and trigger the agent."""
        session_id = session["id"]
        session = self._store.get(session_id)
        if not session or session.get("state") in ("complete", "interrupted"):
            return

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        if action.is_terminal:
            cls = self._load_coordinator_loop_state(session)
            if cls is None:
                self._interrupt_coordinator_loop_state_failure(session)
                return
            self._handle_coordinator_loop_terminal(session, cls, None, action)
            return

        target = action.target_role
        phases = tmpl.get("phases", [])

        target_phase = None
        for i, phase in enumerate(phases):
            if target in phase.get("participants", []):
                target_phase = i
                break

        if target_phase is None:
            log.warning("Session %d: no phase found for coordinator target '%s'",
                        session_id, target)
            self._store.interrupt(
                session_id,
                f"coordinator loop: no phase for '{target}'")
            return

        worker_prompt = action.routing_body or action.prompt_context
        safety_artifact = None
        if target == "safety_gate":
            safety_artifact = worker_prompt or session.get("goal", "")

        if worker_prompt is not None or safety_artifact is not None:
            self._store.update_coordinator_loop_state(
                session_id,
                session.get("coordinator_loop_state", {}),
                worker_prompt=worker_prompt,
                safety_artifact=safety_artifact,
            )
            session = self._store.get(session_id) or session

        session = self._store.set_phase_and_turn(session_id, target_phase, 0)
        if not session:
            return

        channel = session.get("channel", "general")
        phase_obj = phases[target_phase]
        self._messages.add(
            sender="system",
            text=f"Phase: {phase_obj['name']}",
            msg_type="session_phase",
            channel=channel,
            metadata={
                "session_id": session_id,
                "phase": target_phase,
                "phase_name": phase_obj["name"],
            },
        )
        self._trigger_coordinator_loop(session)

    def _phase_index_for_role(self, tmpl: dict, role: str) -> int | None:
        for i, phase in enumerate(tmpl.get("phases", [])):
            if role in phase.get("participants", []):
                return i
        return None

    def _format_worker_dispatch_blocker(
        self,
        *,
        session: dict,
        cls,
        role: str,
        assigned_agent: str,
        reason: str,
        prompt_built: bool = False,
        prompt_chars: int = 0,
        dispatch_method: str = "",
        store_exec_available: bool = False,
        relay_eligible: bool = False,
        trigger_result: str = "",
        last_exception: str = "",
    ) -> str:
        policy = session.get("workspace_policy") or {}
        tmpl = self._store.get_template(session.get("template_id", "")) or {}
        lines = [
            "BLOCKER: worker dispatch not started",
            "",
            "Diagnostics:",
            f"- role: {role}",
            f"- assigned_agent: {assigned_agent}",
            f"- phase: {session.get('current_phase')}",
            f"- session_id: {session.get('id')}",
            f"- channel: {session.get('channel', 'general')}",
            f"- template: {tmpl.get('id', '(unknown)')}",
            f"- report_orchestrated: {bool(getattr(cls, 'report_orchestrated', False))}",
            f"- prompt_built: {'true' if prompt_built else 'false'}",
            f"- prompt_chars: {prompt_chars}",
            f"- dispatch_method: {dispatch_method or '(none)'}",
            f"- store_exec_available: {'true' if store_exec_available else 'false'}",
            f"- relay_eligible: {'true' if relay_eligible else 'false'}",
            f"- trigger_result: {trigger_result or '(none)'}",
            f"- last_exception: {last_exception or '(none)'}",
            f"- workspace_profile: {getattr(cls, 'session_workspace_profile', '') or policy.get('policy_id') or '(none)'}",
            f"- workspace_mode: {getattr(cls, 'session_workspace_mode', '') or policy.get('mode') or '(none)'}",
            f"- reason: {reason}",
        ]
        return "\n".join(lines)

    def _fail_worker_dispatch_not_started(
        self,
        session: dict,
        cls,
        *,
        role: str,
        assigned_agent: str,
        reason: str,
        **diag,
    ) -> None:
        from coordinator_loop import CoordinatorPhase

        blocker = self._format_worker_dispatch_blocker(
            session=session,
            cls=cls,
            role=role,
            assigned_agent=assigned_agent,
            reason=reason,
            **diag,
        )
        cls.phase = CoordinatorPhase.BLOCKER
        cls.blocker_reason = blocker
        cls.awaiting_role = ""
        self._store.update_coordinator_loop_state(session["id"], cls.to_dict())
        channel = session.get("channel", "general")
        self._messages.add(
            sender="system",
            text=blocker[:500],
            msg_type="session_coord_blocker",
            channel=channel,
            metadata={
                "session_id": session["id"],
                "phase": cls.phase.value,
                "terminal_kind": "blocker",
                "blocker_reason": blocker,
            },
        )
        self._store.interrupt(session["id"], blocker)

    def _realign_session_worker_turn(
        self,
        session: dict,
        cls,
        tmpl: dict,
        effective_role: str,
    ) -> dict | None:
        """Move session phase/awaiting_role to the effective dispatch target."""
        phase_idx = self._phase_index_for_role(tmpl, effective_role)
        if phase_idx is None:
            return None
        cls.awaiting_role = effective_role
        self._store.update_coordinator_loop_state(session["id"], cls.to_dict())
        return self._store.set_phase_and_turn(session["id"], phase_idx, 0)

    def _fire_report_orchestrated_worker(
        self,
        session: dict,
        cls,
        tmpl: dict,
        phase: dict,
        phase_idx: int,
        turn_idx: int,
        role: str,
        agent: str,
        agent_base: str,
        instruction: str,
    ) -> bool:
        """Build report-orchestrated prompt and queue the assigned worker."""
        channel = session.get("channel", "general")
        policy = session.get("workspace_policy") or {}
        cast = session.get("cast", {})
        worker_ctx = self._worker_context_from_session(session)
        trusted_bridge_repair = bool(
            getattr(cls, "trusted_cli_report_bridge_repair_active", False)
            and role == "developer"
            and instruction.strip()
        )
        ui_lead_bridge_repair = bool(
            getattr(cls, "ui_lead_report_bridge_repair_active", False)
            and role == "ui_lead"
            and instruction.strip()
        )
        if trusted_bridge_repair:
            prompt = instruction.strip()
            cls.trusted_cli_report_bridge_repair_active = False
            effective_role = role
            effective_agent = cast.get(effective_role) or agent
            effective_agent_base = self._get_agent_base(effective_agent)
            dispatch_role = role
            handoff_repair = False
            result = None
        elif ui_lead_bridge_repair:
            prompt = instruction.strip()
            cls.ui_lead_report_bridge_repair_active = False
            effective_role = role
            effective_agent = cast.get(effective_role) or agent
            effective_agent_base = self._get_agent_base(effective_agent)
            dispatch_role = role
            handoff_repair = False
            result = None
        else:
            result = build_report_orchestrated_dispatch_prompt(
                role=role,
                report_records=cls.report_records,
                project=channel,
                phase=phase["name"],
                subject=session.get("goal", "")[:120] or "report-review",
                instruction=instruction,
                awaiting_developer_correction=cls.awaiting_developer_correction,
                developer_correction_source=cls.developer_correction_source,
                requires_agy=cls.requires_agy,
                expected_output_path=str((worker_ctx.get("report_paths_by_role") or {}).get(role) or ""),
                external_report_write_roots=list(worker_ctx.get("allowed_report_roots") or []),
                max_chars=int(worker_ctx.get("max_report_prompt_chars") or DEFAULT_MAX_REPORT_PROMPT_CHARS),
                prompt_memo_body=self._session_prompt_body(session),
                policy=policy,
            )
            if not result.ok:
                log.warning(
                    "Session %d: report-orchestrated dispatch blocked: %s",
                    session["id"],
                    result.blocker,
                )
                self._fail_worker_dispatch_not_started(
                    session,
                    cls,
                    role=role,
                    assigned_agent=agent,
                    reason=result.blocker,
                    prompt_built=False,
                )
                return False

            if result.refreshed_report_records is not None:
                cls.report_records = list(result.refreshed_report_records)
                self._store.update_coordinator_loop_state(session["id"], cls.to_dict())

            prompt = result.prompt
            dispatch_role = result.dispatch_role or role
            effective_role = dispatch_role
            effective_agent = cast.get(effective_role) or agent
            effective_agent_base = self._get_agent_base(effective_agent)
            handoff_repair = bool(getattr(result, "handoff_repair", False))

        workspace_bound = bool((policy.get("workspace") or {}).get("root"))
        contract = coordinator_loop_worker_output_contract(
            effective_role,
            workspace_bound=workspace_bound,
            report_orchestrated=cls.report_orchestrated,
        )
        if contract and not trusted_bridge_repair and not ui_lead_bridge_repair:
            prompt = f"{prompt}\n\n{contract}"

        repair_owner = (
            (getattr(result, "handoff_repair_owner_role", "") if result is not None else "")
            or dispatch_role
            or role
        )
        if handoff_repair and result is not None:
            rounds = dict(getattr(cls, "handoff_repair_rounds", {}) or {})
            max_repairs = int(
                getattr(cls, "max_handoff_repair_rounds_per_role", 2) or 2,
            )
            current_rounds = int(rounds.get(repair_owner, 0))
            if current_rounds >= max_repairs:
                tmpl_id = tmpl.get("id", "(unknown)")
                policy = session.get("workspace_policy") or {}
                blocker = format_handoff_repair_limit_blocker(
                    role=repair_owner,
                    owner_agent=cast.get(repair_owner) or agent,
                    missing_blocks=list(
                        getattr(result, "handoff_repair_missing_blocks", []) or [],
                    ),
                    invalid_blocks=list(
                        getattr(result, "handoff_repair_invalid_blocks", []) or [],
                    ),
                    repair_rounds=current_rounds,
                    report_path=str(
                        (worker_ctx.get("report_paths_by_role") or {}).get(repair_owner)
                        or "",
                    ),
                    report_chars=len(prompt),
                    last_report_status="",
                    prompt_chars=len(prompt),
                    snapshots_injected=False,
                    workspace_profile=str(
                        getattr(cls, "session_workspace_profile", "")
                        or policy.get("policy_id")
                        or "",
                    ),
                    workspace_mode=str(
                        getattr(cls, "session_workspace_mode", "")
                        or policy.get("mode")
                        or "",
                    ),
                    session_id=session["id"],
                    channel=channel,
                    template=tmpl_id,
                )
                self._fail_worker_dispatch_not_started(
                    session,
                    cls,
                    role=role,
                    assigned_agent=agent,
                    reason=blocker,
                    prompt_built=True,
                    prompt_chars=len(prompt),
                )
                return False
            rounds[repair_owner] = current_rounds + 1
            cls.handoff_repair_rounds = rounds
            diag = format_handoff_validation_diagnostics(
                intended_next_role=getattr(result, "intended_next_role", "") or role,
                dispatch_role=repair_owner,
                owner_role=getattr(result, "owner_role", "") or repair_owner,
                owner_agent=cast.get(repair_owner) or agent,
                report_path=str(
                    getattr(result, "report_path", "")
                    or (worker_ctx.get("report_paths_by_role") or {}).get(repair_owner)
                    or "",
                ),
                report_hash_before=getattr(result, "report_hash_before", ""),
                report_hash_after=getattr(result, "handoff_repair_report_hash", ""),
                report_chars=int(getattr(result, "handoff_repair_report_chars", 0) or 0),
                missing_blocks=list(
                    getattr(result, "handoff_repair_missing_blocks", []) or [],
                ),
                invalid_blocks=list(
                    getattr(result, "handoff_repair_invalid_blocks", []) or [],
                ),
                found_marker_names=list(
                    getattr(result, "found_marker_names", []) or [],
                ),
                parser_expected_marker_names=list(
                    getattr(result, "parser_expected_marker_names", []) or [],
                ),
                repair_round=rounds[repair_owner],
                max_repair_rounds=max_repairs,
                using_cached_report=bool(getattr(result, "using_cached_report", False)),
                report_reread_after_repair=bool(
                    getattr(result, "report_reread_after_repair", False),
                ),
                reason=getattr(result, "handoff_validation_reason", ""),
            )
            log.info(
                "Session %d: handoff repair dispatch role=%s round=%d\n%s",
                session["id"],
                repair_owner,
                rounds[repair_owner],
                diag,
            )
            log.info(
                "Session %d: handoff repair dispatch role=%s round=%d "
                "handoff_repair_prompt_chars=%d report_context_chars=%d "
                "report_context_injected=%s snapshots_injected=false "
                "report_path=%s missing_blocks=%s repair_round=%d",
                session["id"],
                repair_owner,
                rounds[repair_owner],
                len(prompt),
                int(getattr(result, "handoff_repair_report_context_chars", 0) or 0),
                "true" if getattr(result, "handoff_repair_report_context_injected", False) else "false",
                str((worker_ctx.get("report_paths_by_role") or {}).get(repair_owner) or ""),
                getattr(result, "handoff_repair_missing_blocks", []),
                rounds[repair_owner],
            )

        if effective_role != role:
            realigned = self._realign_session_worker_turn(session, cls, tmpl, effective_role)
            if not realigned:
                self._fail_worker_dispatch_not_started(
                    session,
                    cls,
                    role=role,
                    assigned_agent=agent,
                    reason=f"no phase for redirected role {effective_role}",
                    prompt_built=True,
                    prompt_chars=len(prompt),
                )
                return False
            session = realigned
            phase_idx = session["current_phase"]
            phase = tmpl.get("phases", [])[phase_idx]
            role = effective_role
            agent = effective_agent
            agent_base = effective_agent_base

        store_exec = self._agent_uses_store_exec(effective_agent)
        relay_ok = is_relay_eligible(effective_agent_base)
        dispatch_method = "unknown"
        wpc = self._session_workspace_policy_context(
            session, effective_role, phase_idx, turn_idx,
        )
        wpc = dict(wpc)
        wpc["trusted_cli_report_bridge_repair_rounds"] = cls.trusted_cli_report_bridge_repair_rounds
        wpc["max_trusted_cli_report_bridge_repair_rounds"] = (
            cls.max_trusted_cli_report_bridge_repair_rounds
        )
        wpc["ui_lead_report_bridge_repair_rounds"] = cls.ui_lead_report_bridge_repair_rounds
        wpc["max_ui_lead_report_bridge_repair_rounds"] = (
            cls.max_ui_lead_report_bridge_repair_rounds
        )
        if handoff_repair:
            wpc["handoff_repair"] = True
            wpc["skip_snapshot_injection"] = True
        if trusted_bridge_repair:
            wpc["trusted_cli_report_bridge_repair"] = True
            wpc["skip_snapshot_injection"] = True
        if ui_lead_bridge_repair:
            wpc["ui_lead_report_bridge_repair"] = True
            wpc["skip_snapshot_injection"] = True
        try:
            if relay_ok and effective_role in ("developer", "reviewer", "ui_lead"):
                dispatch_method = "relay_entry"
                relay_entry = self._make_relay_queue_entry(
                    prompt=prompt,
                    session=session,
                    phase_idx=phase_idx,
                    turn_idx=turn_idx,
                    role=effective_role,
                    channel=channel,
                    handoff_repair=handoff_repair,
                    trusted_cli_report_bridge_repair=trusted_bridge_repair,
                )
                self._trigger.trigger_sync(
                    effective_agent, channel=channel, relay_entry=relay_entry,
                )
            elif store_exec:
                dispatch_method = "store_exec_prompt"
                self._trigger.trigger_sync(
                    effective_agent,
                    channel=channel,
                    prompt=prompt,
                    workspace_policy_context=wpc,
                )
            else:
                dispatch_method = "prompt_sync"
                self._trigger.trigger_sync(
                    effective_agent,
                    channel=channel,
                    prompt=prompt,
                    workspace_policy_context=wpc,
                )
        except Exception as exc:
            log.exception(
                "Session %d: report-orchestrated worker dispatch failed for %s",
                session["id"],
                effective_agent,
            )
            self._fail_worker_dispatch_not_started(
                session,
                cls,
                role=effective_role,
                assigned_agent=effective_agent,
                reason="worker trigger raised exception",
                prompt_built=True,
                prompt_chars=len(prompt),
                dispatch_method=dispatch_method,
                store_exec_available=store_exec,
                relay_eligible=relay_ok,
                last_exception=str(exc),
            )
            return False

        cls.awaiting_role = effective_role
        self._store.update_coordinator_loop_state(session["id"], cls.to_dict())
        self._store.set_waiting(session["id"], effective_agent)
        log.info(
            "Session %d: report-orchestrated dispatch %s via %s (%d chars)",
            session["id"],
            effective_agent,
            dispatch_method,
            len(prompt),
        )
        return True

    def _trigger_coordinator_loop(self, session: dict):
        """Trigger the current coordinator-loop participant with role-aware prompts."""
        from coordinator_loop import CoordinatorPhase

        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        cls = self._load_coordinator_loop_state(session)
        if cls is None:
            self._interrupt_coordinator_loop_state_failure(session)
            return

        if cls.phase in (
            CoordinatorPhase.FINAL,
            CoordinatorPhase.BLOCKER,
            CoordinatorPhase.HALTED,
        ):
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
            self._store.interrupt(session["id"], f"no agent for role '{role}'")
            return

        if not self._is_agent(agent):
            self._store.set_waiting(session["id"], agent)
            return

        agent_base = self._get_agent_base(agent)
        channel = session.get("channel", "general")
        worker_prompt = session.get("coordinator_loop_worker_prompt", "")

        if role == "coordinator":
            from coordinator_loop import coordinator_allowed_tokens

            policy = session.get("workspace_policy") or {}
            allowed = coordinator_allowed_tokens(cls)
            prompt = build_coordinator_loop_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                task_description=cls.task_description,
                last_role=cls.last_role,
                last_output_summary=cls.last_output_summary,
                awaiting_role=cls.awaiting_role,
                developer_round=cls.developer_round,
                ui_round=cls.ui_round,
                review_round=cls.review_round,
                safety_round=cls.safety_round,
                allowed_tokens=allowed,
                instruction=worker_prompt or phase.get("prompt", ""),
                agent_base=agent_base,
                project=channel,
                phase=phase.get("name", f"phase-{phase_idx}"),
                subject=session.get("goal", "")[:120] or "coordinator-routing",
                readonly_analysis=is_readonly_no_tool_reviewer_policy(policy),
            )
            if is_relay_eligible(agent_base):
                relay_entry = self._make_relay_queue_entry(
                    prompt=prompt,
                    session=session,
                    phase_idx=phase_idx,
                    turn_idx=turn_idx,
                    role=role,
                    channel=channel,
                )
                self._trigger.trigger_sync(agent, channel=channel, relay_entry=relay_entry)
            else:
                wpc = self._session_workspace_policy_context(
                    session, role, phase_idx, turn_idx,
                )
                self._trigger.trigger_sync(
                    agent, channel=channel, prompt=prompt,
                    workspace_policy_context=wpc,
                )
            self._store.set_waiting(session["id"], agent)
            return

        if role.lower() in _SAFETY_GATE_ROLES:
            content = session.get("coordinator_loop_safety_artifact")
            if not content:
                content = self._get_last_turn_content(session, channel)
            prompt = build_safety_gate_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                content_to_review=content,
                agent_base=agent_base,
                prompt_body=self._session_prompt_body(session),
            )
            relay_entry = self._make_relay_queue_entry(
                prompt=prompt,
                session=session,
                phase_idx=phase_idx,
                turn_idx=turn_idx,
                role=role,
                channel=channel,
            )
            self._trigger.trigger_sync(agent, channel=channel, relay_entry=relay_entry)
            self._store.set_waiting(session["id"], agent)
            return

        instruction = worker_prompt or phase.get("prompt", "")
        policy = session.get("workspace_policy") or {}

        if cls.report_orchestrated and role in ("developer", "ui_lead", "reviewer"):
            if self._fire_report_orchestrated_worker(
                session, cls, tmpl, phase, phase_idx, turn_idx,
                role, agent, agent_base, instruction,
            ):
                return
            return

        workspace_bound = bool((policy.get("workspace") or {}).get("root"))
        report_contract = coordinator_loop_worker_output_contract(
            role,
            workspace_bound=workspace_bound,
            report_orchestrated=cls.report_orchestrated,
        )

        if role_uses_headless_scoped_workspace(session, role):
            self._trigger_scoped_workspace_worker(
                session, tmpl, phase, phase_idx, turn_idx, role, agent, channel,
                instruction=instruction,
            )
            self._store.set_waiting(session["id"], agent)
            return

        if self._agent_uses_store_exec(agent):
            prompt = build_coordinator_loop_ui_lead_prompt(
                    session_name=tmpl.get("name", "?"),
                    channel=channel,
                    goal=session.get("goal", ""),
                    phase_name=phase["name"],
                    phase_index=phase_idx,
                    total_phases=len(phases),
                    instruction=instruction,
                    context_messages=self._get_recent_context(channel),
            )
            if report_contract:
                prompt = f"{prompt}\n\n{report_contract}"
            self._trigger.trigger_sync(
                agent, channel=channel, prompt=prompt,
                workspace_policy_context=self._session_workspace_policy_context(
                    session, role, phase_idx, turn_idx,
                ),
            )
            self._store.set_waiting(session["id"], agent)
            return

        context_messages = self._get_recent_context(channel)
        if (
            role == "reviewer"
            and is_readonly_no_tool_reviewer_policy(policy)
            and not cls.report_orchestrated
        ):
            packet = build_readonly_reviewer_context_packet(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                phase_index=phase_idx,
                total_phases=len(phases),
                policy=policy,
                context_messages=context_messages,
                cast=session.get("cast"),
                coordinator_instruction=instruction,
                agent_base=agent_base,
                project=channel,
                subject=session.get("goal", "")[:120] or "read-only-analysis-review",
                verdict_log=cls.verdict_log,
                stored_developer_analysis=cls.last_developer_analysis,
                stored_ui_lead_notes=cls.last_ui_lead_notes,
                prompt_id=cls.session_prompt_id,
                workspace_profile=cls.session_workspace_profile,
                workspace_mode=cls.session_workspace_mode,
            )
            if not packet.ok:
                log.warning(
                    "Session %d: read-only reviewer context packet incomplete: %s",
                    session["id"],
                    packet.diagnostics,
                )
                cls.phase = CoordinatorPhase.BLOCKER
                cls.blocker_reason = packet.blocker
                cls.awaiting_role = ""
                self._store.update_coordinator_loop_state(session["id"], cls.to_dict())
                self._store.interrupt(session["id"], packet.blocker)
                return
            prompt = packet.prompt
        else:
            prompt = build_relay_prompt(
                session_name=tmpl.get("name", "?"),
                goal=session.get("goal", ""),
                phase_name=phase["name"],
                phase_index=phase_idx,
                total_phases=len(phases),
                role=role,
                instruction=instruction,
                context_messages=context_messages,
                agent_base=agent_base,
                prompt_body=self._session_prompt_body(session),
            )
        if report_contract:
            prompt = f"{prompt}\n\n{report_contract}"
        relay_entry = self._make_relay_queue_entry(
            prompt=prompt,
            session=session,
            phase_idx=phase_idx,
            turn_idx=turn_idx,
            role=role,
            channel=channel,
        )
        self._trigger.trigger_sync(agent, channel=channel, relay_entry=relay_entry)
        self._store.set_waiting(session["id"], agent)

    def is_coordinator_loop_session(self, session: dict) -> bool:
        """True if the session's template opts in to coordinator-loop routing."""
        tmpl = self._store.get_template(session.get("template_id", ""))
        if not tmpl:
            return False
        return bool(tmpl.get("coordinator_loop"))

    def is_flow_coordinator_session(self, session: dict) -> bool:
        """True if the session's template opts in to flow-coordinator routing.

        Only templates with ``"flow_coordinator": true`` activate the sandbox
        orchestration loop. All other templates (including the 6 shipped linear
        templates) behave exactly as before — this is the opt-in gate.
        """
        tmpl = self._store.get_template(session.get("template_id", ""))
        if not tmpl:
            return False
        return bool(tmpl.get("flow_coordinator"))

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
            session["flow_coordinator"] = bool(tmpl.get("flow_coordinator"))
            session["coordinator_loop"] = bool(tmpl.get("coordinator_loop"))
        from workspace_policy import workspace_policy_read_summary
        session["workspace_policy_summary"] = workspace_policy_read_summary(session)
        return session
