"""Prompt memo parsing and safety validation for workspace session launcher."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import workspace_policy as wp

MAX_PROMPT_BODY = 50_000
MAX_GOAL = 500

_HEADER_RE = re.compile(
    r"^(PROMPT ID|TO|FROM|ROLE|MODEL|REASONING|MODE|PROJECT|PHASE|SUBJECT|STATUS|DECISION)\s*:\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,19}$")
_GIT_WRITE_RE = re.compile(
    r"\bgit\s+(add|commit|push|reset|checkout|clean|stash)\b",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(r"\bREAD[\s\-]?ONLY\b", re.IGNORECASE)
_SCOPED_WRITE_RE = re.compile(r"\bscoped[\s\-]?write\b", re.IGNORECASE)
_IMPLEMENTATION_RE = re.compile(
    r"\b(implementation|implement)\b",
    re.IGNORECASE,
)
_UI_09_C_RE = re.compile(r"UI[\s\-]?09[\s\-]?C|PaymentModal", re.IGNORECASE)


@dataclass(frozen=True)
class MemoParseResult:
    headers: dict[str, str] = field(default_factory=dict)
    prompt_id: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoSuggestions:
    prompt_id: str = ""
    channel: str = ""
    goal: str = ""
    template_id: str = "project-readonly-coordinator-loop"
    workspace_profile: str = ""
    workspace_mode: str = ""
    expected_head: str = ""
    cast: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoSafetyResult:
    ok: bool
    code: str = ""
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def normalize_prompt_body(text: str | None) -> str:
    if not text or not isinstance(text, str):
        return ""
    return text.strip()[:MAX_PROMPT_BODY]


def normalize_goal(text: str | None, *, prompt_body: str = "") -> str:
    if text and isinstance(text, str) and text.strip():
        return text.strip()[:MAX_GOAL]
    if prompt_body:
        first = prompt_body.strip().splitlines()[0][:MAX_GOAL]
        return first
    return ""


def parse_memo_headers(text: str) -> MemoParseResult:
    """Extract known memo headers; non-destructive best-effort parsing."""
    headers: dict[str, str] = {}
    if not text or not isinstance(text, str):
        return MemoParseResult()

    for match in _HEADER_RE.finditer(text):
        key = match.group(1).upper().replace(" ", "_")
        val = match.group(2).strip()
        if val:
            headers[key] = val

    prompt_id = headers.get("PROMPT_ID", "")
    warnings: list[str] = []
    if text.strip() and not prompt_id:
        warnings.append("No PROMPT ID header found; channel/title may need manual entry.")

    return MemoParseResult(headers=headers, prompt_id=prompt_id, warnings=tuple(warnings))


def slugify_channel_from_prompt_id(prompt_id: str) -> str:
    """Convert PROMPT ID to a valid channel slug."""
    if not prompt_id:
        return ""
    slug = prompt_id.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if len(slug) > 20:
        slug = slug[:20].rstrip("-")
    if not slug or not _CHANNEL_RE.match(slug):
        return ""
    return slug


def _memo_implies_read_only(text: str) -> bool:
    return bool(_READ_ONLY_RE.search(text))


def _memo_implies_scoped_write(text: str) -> bool:
    return bool(_SCOPED_WRITE_RE.search(text))


def _memo_implies_implementation(text: str) -> bool:
    return bool(_IMPLEMENTATION_RE.search(text)) and not _memo_implies_read_only(text)


def suggest_from_memo(
    memo_text: str,
    *,
    profiles: dict[str, dict],
    presets: list[dict],
    hints: dict[str, Any] | None = None,
) -> MemoSuggestions:
    """Suggest UI field values from memo headers (non-authoritative)."""
    hints = hints or {}
    parsed = parse_memo_headers(memo_text)
    headers = parsed.headers
    warnings = list(parsed.warnings)

    prompt_id = hints.get("prompt_id") or parsed.prompt_id
    channel = hints.get("channel") or slugify_channel_from_prompt_id(prompt_id)
    if prompt_id and not channel:
        warnings.append("Could not derive channel from PROMPT ID; enter channel manually.")

    subject = headers.get("SUBJECT", "")
    phase = headers.get("PHASE", "")
    mode_hdr = headers.get("MODE", "")

    goal = normalize_goal(hints.get("goal"), prompt_body=memo_text)
    if subject and len(goal) < 80:
        goal = subject[:MAX_GOAL]

    workspace_profile = str(hints.get("workspace_profile") or "")
    workspace_mode = str(hints.get("workspace_mode") or "")
    expected_head = str(hints.get("expected_head") or "")
    template_id = str(hints.get("template_id") or "project-readonly-coordinator-loop")

    cast: dict[str, str] = {}
    if isinstance(hints.get("cast"), dict):
        cast = {str(k): str(v) for k, v in hints["cast"].items()}

    combined = f"{memo_text}\n{mode_hdr}\n{subject}\n{phase}"

    if _UI_09_C_RE.search(combined):
        for preset in presets:
            pid = preset.get("id", "")
            if "analysis" in pid and _memo_implies_read_only(combined):
                workspace_profile = workspace_profile or pid
                workspace_mode = workspace_mode or preset.get("workspace_mode", "read-only-analysis")
                expected_head = expected_head or preset.get("expected_head", "")
                if not cast and isinstance(preset.get("cast"), dict):
                    cast = dict(preset["cast"])
                break
            if "write" in pid and (
                _memo_implies_scoped_write(combined) or _memo_implies_implementation(combined)
            ):
                workspace_profile = workspace_profile or pid
                workspace_mode = workspace_mode or preset.get("workspace_mode", "scoped-write")
                expected_head = expected_head or preset.get("expected_head", "")
                if not cast and isinstance(preset.get("cast"), dict):
                    cast = dict(preset["cast"])
                break

    mode_text = mode_hdr or combined
    if not workspace_mode:
        if _memo_implies_read_only(mode_text):
            workspace_mode = "read-only-analysis" if _UI_09_C_RE.search(combined) else "read-only"
        elif _memo_implies_scoped_write(mode_text):
            workspace_mode = "scoped-write"
        elif _memo_implies_implementation(mode_text):
            workspace_mode = "implementation"

    to_field = headers.get("TO", "")
    from_field = headers.get("FROM", "")
    agent_hints = f"{to_field} {from_field}".lower()
    if "claude" in agent_hints or "cursor" in agent_hints:
        cast.setdefault("developer", "claude")
    if "agy" in agent_hints:
        cast.setdefault("ui_lead", "agy")
    if "codex reviewer" in agent_hints or "codex_reviewer" in agent_hints:
        cast.setdefault("reviewer", "codex_reviewer")
    if "codexsafe" in agent_hints or "codex safe" in agent_hints:
        cast.setdefault("safety_gate", "codexsafe")
    if "coordinator" in agent_hints or "codex" in agent_hints:
        cast.setdefault("coordinator", "codex_coordinator")

    if workspace_profile and workspace_profile in profiles:
        prof = profiles[workspace_profile]
        if not expected_head:
            expected_head = str(prof.get("expected_head") or "")

    return MemoSuggestions(
        prompt_id=prompt_id,
        channel=channel,
        goal=goal,
        template_id=template_id,
        workspace_profile=workspace_profile,
        workspace_mode=workspace_mode,
        expected_head=expected_head,
        cast=cast,
        warnings=tuple(warnings),
    )


def analyze_memo(
    memo_text: str,
    *,
    profiles: dict[str, dict],
    presets: list[dict],
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full analyze response for UI prefilling."""
    suggestions = suggest_from_memo(memo_text, profiles=profiles, presets=presets, hints=hints)
    safety = validate_memo_start(
        memo_text,
        start_body={
            "workspace_profile": suggestions.workspace_profile,
            "workspace_mode": suggestions.workspace_mode,
            "expected_head": suggestions.expected_head,
            **(hints or {}),
        },
        profiles=profiles,
        policy=None,
        require_profile_for_repo=True,
    )
    return {
        "suggestions": {
            "prompt_id": suggestions.prompt_id,
            "channel": suggestions.channel,
            "goal": suggestions.goal,
            "template_id": suggestions.template_id,
            "workspace_profile": suggestions.workspace_profile,
            "workspace_mode": suggestions.workspace_mode,
            "expected_head": suggestions.expected_head,
            "cast": suggestions.cast,
        },
        "warnings": list(suggestions.warnings) + list(safety.warnings),
        "safety": {
            "ok": safety.ok,
            "code": safety.code,
            "errors": list(safety.errors),
        },
    }


def _memo_requires_repo(memo_text: str) -> bool:
    if not memo_text:
        return False
    markers = (
        "twinpet", "workspace root", "workspace_profile", "expected_head",
        "git rev-parse", "PaymentModal", "UI-09-C",
    )
    lower = memo_text.lower()
    return any(m in lower for m in markers)


def validate_memo_start(
    memo_text: str,
    *,
    start_body: dict[str, Any],
    profiles: dict[str, dict],
    policy: dict[str, Any] | None,
    require_profile_for_repo: bool = True,
) -> MemoSafetyResult:
    """Fail-closed memo/profile/mode safety checks before session start."""
    errors: list[str] = []
    warnings: list[str] = []

    profile_id = str(start_body.get("workspace_profile") or "").strip()
    ui_mode = str(start_body.get("workspace_mode") or "").strip()
    canonical_mode = wp.normalize_workspace_mode(ui_mode) or ui_mode
    expected_head = str(start_body.get("expected_head") or "").strip()

    memo_readonly = _memo_implies_read_only(memo_text)
    memo_scoped = _memo_implies_scoped_write(memo_text)
    memo_impl = _memo_implies_implementation(memo_text)

    if memo_text and _GIT_WRITE_RE.search(memo_text):
        if canonical_mode in ("read-only", "docs-only", "implementation") or ui_mode in (
            "read-only", "read-only-analysis", "scoped-write",
        ):
            errors.append(
                "Prompt requests git write commands but workspace mode forbids git writes."
            )

    if memo_readonly and ui_mode in ("scoped-write", "implementation"):
        errors.append(
            "READ-ONLY prompt with scoped-write/implementation mode selected: BLOCKED."
        )

    if memo_scoped and ui_mode in ("read-only", "read-only-analysis"):
        errors.append(
            "Scoped-write prompt with read-only mode selected: BLOCKED."
        )

    if memo_impl and canonical_mode in ("read-only", "scratch-readonly"):
        if not start_body.get("planning_only_confirmed"):
            errors.append(
                "Implementation prompt with read-only mode: confirm planning-only or select implementation/scoped-write."
            )

    if require_profile_for_repo and _memo_requires_repo(memo_text) and not profile_id:
        errors.append("BLOCKER: required workspace profile missing or not selected.")

    if profile_id and profile_id not in profiles:
        errors.append(f"Unknown workspace profile: {profile_id!r}")

    if ui_mode in ("scoped-write", "implementation") and not profile_id:
        errors.append("Scoped-write/implementation requires workspace_profile.")

    if profile_id:
        prof = profiles.get(profile_id) or {}
        prof_head = str(prof.get("default_expected_head") or "")
        if prof_head and expected_head and prof_head.lower() != expected_head.lower():
            errors.append("expected_head mismatches profile default.")

        allowed = prof.get("allowed_modes") or []
        if canonical_mode and canonical_mode not in allowed:
            if ui_mode == "read-only-analysis" and (
                "docs-only" in allowed or "read-only" in allowed
            ):
                pass
            else:
                errors.append(
                    f"Mode {ui_mode!r} not allowed for profile {profile_id!r}."
                )

        if canonical_mode == "implementation" and not prof.get("allowed_write_files"):
            errors.append("Implementation mode requires profile with write allowlist.")

        if canonical_mode in ("read-only", "scratch-readonly") and prof.get("max_mode") == "implementation":
            if memo_readonly and ui_mode in ("scoped-write", "implementation"):
                pass  # caught above
            elif ui_mode in ("scoped-write", "implementation"):
                errors.append("Read-only memo cannot use implementation write profile.")

    if policy:
        mode = policy.get("mode")
        if memo_readonly and mode == "implementation":
            errors.append("Policy resolved to implementation but memo is READ-ONLY.")
        write_files = policy.get("write_files") or []
        if memo_readonly and write_files and any(
            str(f).startswith("src/") for f in write_files
        ):
            errors.append("READ-ONLY memo cannot use profile with src/ write paths.")

    if errors:
        return MemoSafetyResult(False, code="MEMO_SAFETY_BLOCK", errors=tuple(errors), warnings=tuple(warnings))

    if not profile_id and _memo_requires_repo(memo_text):
        return MemoSafetyResult(
            False,
            code="MEMO_SAFETY_BLOCK",
            errors=("BLOCKER: required workspace profile missing or not selected.",),
        )

    return MemoSafetyResult(True, warnings=tuple(warnings))


def session_task_instruction(session: dict[str, Any] | None, fallback: str = "") -> str:
    """Return authoritative task text: prompt_body > goal > fallback."""
    if not isinstance(session, dict):
        return fallback
    body = session.get("prompt_body") or ""
    if isinstance(body, str) and body.strip():
        return body.strip()
    goal = session.get("goal") or ""
    if isinstance(goal, str) and goal.strip():
        return goal.strip()
    return fallback
