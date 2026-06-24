"""Path-based sealed review package relay for Codex.

Reads approved Markdown/text packages from Owner Ai-Report folders and
builds a sealed text-relay prompt delivered to Codex via the existing
relay queue contract (relay_mode + disable_mcp → stdin exec, no MCP).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = __import__("logging").getLogger(__name__)

APPROVED_ROOTS: tuple[Path, ...] = (
    Path(r"C:\Users\Narachat\OneDrive\Ai-Report"),
    Path(r"C:\Users\Narachat\Desktop\Ai-Report"),
)

ALLOWED_EXTENSIONS = frozenset({".md", ".txt"})

DEFAULT_CHUNK_CHARS = 16_000

REVIEW_PACKAGE_RE = re.compile(
    r"@codex\b[\s\S]*?\breview-package\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s\r\n]+))",
    re.IGNORECASE,
)


class PackageRelayError(Exception):
    """Fail-closed package relay validation or load error."""


@dataclass(frozen=True)
class PackageManifest:
    path: str
    title: str
    sha256: str
    bytes: int
    chars: int
    lines: int
    chunk_count: int


def is_codex_agent(name: str) -> bool:
    """True when ``name`` is a Codex-family registered instance."""
    n = name.lower()
    return n == "codex" or n.startswith("codex-") or n.startswith("codex_")


def parse_review_package_path(text: str) -> str | None:
    """Extract package path from ``@codex review-package <path>`` mention text."""
    if not text:
        return None
    match = REVIEW_PACKAGE_RE.search(text)
    if not match:
        return None
    return (match.group(1) or match.group(2) or match.group(3) or "").strip()


def _normalize_roots() -> list[Path]:
    roots: list[Path] = []
    for root in APPROVED_ROOTS:
        try:
            roots.append(root.resolve())
        except OSError:
            roots.append(root)
    return roots


def _is_under_approved_root(resolved: Path) -> bool:
    resolved = resolved.resolve()
    target = str(resolved)
    target_lower = target.lower()
    for root in _normalize_roots():
        root_s = str(root)
        root_lower = root_s.lower()
        if target_lower == root_lower:
            return True
        prefix = root_lower + os.sep
        if target_lower.startswith(prefix):
            return True
    return False


def validate_package_path(raw_path: str) -> Path:
    """Resolve and validate an Owner package path (fail-closed)."""
    if not raw_path or not raw_path.strip():
        raise PackageRelayError("package path is empty")

    candidate = Path(raw_path.strip().strip('"').strip("'"))
    if not candidate.is_absolute():
        raise PackageRelayError(f"path must be absolute: {raw_path!r}")

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PackageRelayError(f"cannot resolve path: {raw_path!r}") from exc

    if ".." in candidate.parts:
        raise PackageRelayError(f"path traversal rejected: {raw_path!r}")

    if not _is_under_approved_root(resolved):
        raise PackageRelayError(
            f"path outside approved Ai-Report roots: {resolved}"
        )

    suffix = resolved.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise PackageRelayError(
            f"unsupported file type {suffix!r} (allowed: .md, .txt)"
        )

    if not resolved.is_file():
        raise PackageRelayError(f"package file not found: {resolved}")

    return resolved


def extract_package_title(content: str, path: Path) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Deterministically split ``text`` into ordered chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def build_sealed_review_prompt(manifest: PackageManifest, chunks: list[str]) -> str:
    """Build the sealed Codex review prompt including manifest and all chunks."""
    lines = [
        "MODE: SEALED-TEXT REVIEW ONLY",
        "NO REPO ACCESS",
        "NO SHELL",
        "NO GIT",
        "NO FILE EDITS",
        "",
        "PACKAGE MANIFEST:",
        f"Package path: {manifest.path}",
        f"Package title: {manifest.title}",
        f"SHA-256: {manifest.sha256}",
        f"Bytes: {manifest.bytes}",
        f"Characters: {manifest.chars}",
        f"Lines: {manifest.lines}",
        f"Chunk count: {manifest.chunk_count}",
        "",
    ]

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        lines.append(f"BEGIN PACKAGE CHUNK {index}/{total}")
        lines.append(chunk)
        lines.append(f"END PACKAGE CHUNK {index}/{total}")
        lines.append("")

    lines.extend([
        "INSTRUCTIONS:",
        "Acknowledge package title, SHA-256, chunk count, and whether the package "
        "appears complete.",
        "If complete, review the package.",
        "If missing, truncated, or incomplete, BLOCK clearly.",
    ])
    return "\n".join(lines)


def load_package(raw_path: str, *, max_chunk_chars: int = DEFAULT_CHUNK_CHARS) -> tuple[PackageManifest, str]:
    """Validate, read, hash, chunk, and seal a review package."""
    resolved = validate_package_path(raw_path)
    try:
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        raise PackageRelayError(f"cannot read package: {resolved}") from exc

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageRelayError(f"package is not valid UTF-8: {resolved}") from exc

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    chunks = chunk_text(content, max_chunk_chars)
    title = extract_package_title(content, resolved)
    manifest = PackageManifest(
        path=str(resolved),
        title=title,
        sha256=sha256,
        bytes=len(raw_bytes),
        chars=len(content),
        lines=len(content.splitlines()) if content else 0,
        chunk_count=len(chunks),
    )
    prompt = build_sealed_review_prompt(manifest, chunks)
    return manifest, prompt


def format_relay_system_summary(manifest: PackageManifest) -> str:
    return (
        f"Codex review-package relay loaded: **{manifest.title}** "
        f"({manifest.chunk_count} chunk(s), SHA-256 `{manifest.sha256[:16]}…`). "
        f"Delivering sealed text to Codex — no manual paste required."
    )


def make_package_relay_queue_entry(*, prompt: str, channel: str, path: str) -> dict:
    """Build a relay queue entry for a sealed package review turn."""
    import time

    return {
        "sender": "package-relay",
        "text": f"[codex review-package: {path}]",
        "time": time.strftime("%H:%M:%S"),
        "channel": channel,
        "prompt": prompt,
        "relay_meta": {
            "kind": "package_review",
            "relay_mode": True,
            "disable_mcp": True,
            "channel": channel,
        },
    }


def prepare_package_review_relay(raw_path: str, *, channel: str = "general") -> tuple[dict, PackageManifest]:
    """Load package and return (relay_queue_entry, manifest)."""
    manifest, prompt = load_package(raw_path)
    entry = make_package_relay_queue_entry(
        prompt=prompt,
        channel=channel,
        path=manifest.path,
    )
    return entry, manifest
