"""Aggressive output redaction for preflight reports."""

from __future__ import annotations

import re

from safety_invariants import redact_secrets

_REDACTED_TOKEN = "[REDACTED_TOKEN]"
_REDACTED = "[REDACTED]"

# 64-char hex session tokens (run.py secrets.token_hex(32)).
_SESSION_HEX = re.compile(r"\b[0-9a-fA-F]{64}\b")

# Wrapper/registry instance tokens are often shorter hex or uuid-like.
_INSTANCE_TOKEN = re.compile(
    r"\b(?:instance[_-]?token|session[_-]?token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)

# Bearer / Authorization headers in command lines or logs.
_AUTH_HEADER = re.compile(
    r"(?i)(authorization|x-session-token)\s*[:=]\s*\S+",
)


def scrub_preflight_output(text: object) -> str:
    """Redact secrets and token-like values before any preflight output."""
    if text is None:
        return ""
    s = str(text)
    s = redact_secrets(s)
    s = _SESSION_HEX.sub(_REDACTED_TOKEN, s)
    s = _INSTANCE_TOKEN.sub(_REDACTED_TOKEN, s)
    s = _AUTH_HEADER.sub(_REDACTED_TOKEN, s)
    return s


def redaction_self_test() -> tuple[bool, str]:
    """Synthetic coverage for scrubber. Returns (ok, detail)."""
    samples = [
        ("a" * 64, _REDACTED_TOKEN),
        ("ghp_" + "x" * 36, _REDACTED),
        ("Bearer abcdefgh12345678", _REDACTED),
        ("api_key=supersecretvalue", _REDACTED),
        ("session_token=abc123", _REDACTED_TOKEN),
    ]
    failures: list[str] = []
    for raw, needle in samples:
        out = scrub_preflight_output(raw)
        if needle not in out:
            failures.append(f"expected {needle!r} in scrubbed output")
        if raw in out:
            failures.append("raw secret still present after scrub")
    if failures:
        return False, "; ".join(failures)
    return True, "synthetic redaction samples passed"
