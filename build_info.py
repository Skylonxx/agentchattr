"""Safe build/runtime identity for agentchattr (no secrets)."""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent
_cached: dict[str, str] | None = None


def read_build_info() -> dict[str, str]:
    """Return app version and short git commit when available."""
    global _cached
    if _cached is not None:
        return dict(_cached)

    version = ""
    try:
        version = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass

    git_commit = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_commit = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    _cached = {"version": version, "git_commit": git_commit}
    return dict(_cached)
