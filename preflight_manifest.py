"""Phase manifest loading and validation for process hygiene preflight.

Manifests are explicit allowlists. Missing required fields fail closed.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_DIR = Path(__file__).resolve().parent / "preflight_manifests"

_REQUIRED_TOP_KEYS = frozenset({
    "phase_id",
    "git",
    "wrappers",
    "sessions",
    "channels",
    "sandbox",
    "network",
    "redaction",
})

_REQUIRED_GIT_KEYS = frozenset({
    "expected_branch",
    "require_clean_tree",
    "require_no_staged",
    "require_synced_with_remote",
    "expected_remote_ref",
    "require_config_local_ignored",
})

_REQUIRED_WRAPPER_KEYS = frozenset({
    "allowed",
    "forbidden",
    "require_all_allowed_running",
})

_REQUIRED_SESSION_KEYS = frozenset({
    "max_active_count",
    "active_states",
})

_REQUIRED_CHANNEL_KEYS = frozenset({
    "required",
    "protected_expectation",
    "forbid_general_session_leak_count",
})

_REQUIRED_SANDBOX_KEYS = frozenset({
    "forbid_flow_enabled",
    "forbid_audit_activity",
})

_REQUIRED_NETWORK_KEYS = frozenset({
    "expected_port",
    "max_listeners",
    "require_server_when_wrappers_required",
})

_REQUIRED_REDACTION_KEYS = frozenset({
    "require_self_test",
})


@dataclass(frozen=True)
class PhaseManifest:
    phase_id: str
    description: str
    raw: dict[str, Any]

    @property
    def git(self) -> dict[str, Any]:
        return self.raw["git"]

    @property
    def wrappers(self) -> dict[str, Any]:
        return self.raw["wrappers"]

    @property
    def sessions(self) -> dict[str, Any]:
        return self.raw["sessions"]

    @property
    def channels(self) -> dict[str, Any]:
        return self.raw["channels"]

    @property
    def sandbox(self) -> dict[str, Any]:
        return self.raw["sandbox"]

    @property
    def network(self) -> dict[str, Any]:
        return self.raw["network"]

    @property
    def redaction(self) -> dict[str, Any]:
        return self.raw["redaction"]

    @property
    def general_fallback_forbidden(self) -> bool:
        return bool(self.raw.get("general_fallback_forbidden", True))

    @property
    def require_runtime_checks(self) -> bool:
        return bool(self.raw.get("require_runtime_checks", True))


def _validate_string_list(value: object, field_name: str) -> str | None:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        return f"{field_name} must be a list of strings"
    return None


def _validate_optional_git_extensions(git: dict[str, Any]) -> str | None:
    for key in ("approved_dirty_paths", "approved_file_allowlist"):
        if key in git:
            err = _validate_string_list(git[key], f"git.{key}")
            if err:
                return err
    if "expected_ahead_count" in git:
        count = git["expected_ahead_count"]
        if not isinstance(count, int) or count < 0:
            return "git.expected_ahead_count must be a non-negative integer"
    for key in ("require_ahead_of_remote", "require_head_not_behind_origin", "require_exact_dirty_set"):
        if key in git and not isinstance(git[key], bool):
            return f"git.{key} must be a boolean"
    if git.get("require_clean_tree") is False:
        has_dirty_paths_key = "approved_dirty_paths" in git
        has_exact_set = bool(git.get("require_exact_dirty_set"))
        if not has_dirty_paths_key and not has_exact_set:
            return (
                "git.require_clean_tree=false requires approved_dirty_paths "
                "or require_exact_dirty_set"
            )
    return None


def _missing_keys(section: dict[str, Any], required: frozenset[str]) -> list[str]:
    return sorted(k for k in required if k not in section)


def validate_manifest_dict(data: dict[str, Any]) -> tuple[PhaseManifest | None, str | None]:
    """Validate manifest structure. Returns (manifest, error_reason)."""
    if not isinstance(data, dict):
        return None, "manifest root must be a JSON object"

    missing_top = _missing_keys(data, _REQUIRED_TOP_KEYS)
    if missing_top:
        return None, f"missing required manifest keys: {', '.join(missing_top)}"

    for section_name, required in (
        ("git", _REQUIRED_GIT_KEYS),
        ("wrappers", _REQUIRED_WRAPPER_KEYS),
        ("sessions", _REQUIRED_SESSION_KEYS),
        ("channels", _REQUIRED_CHANNEL_KEYS),
        ("sandbox", _REQUIRED_SANDBOX_KEYS),
        ("network", _REQUIRED_NETWORK_KEYS),
        ("redaction", _REQUIRED_REDACTION_KEYS),
    ):
        section = data.get(section_name)
        if not isinstance(section, dict):
            return None, f"manifest section '{section_name}' must be an object"
        missing = _missing_keys(section, required)
        if missing:
            return None, f"manifest.{section_name} missing keys: {', '.join(missing)}"

    phase_id = data.get("phase_id")
    if not isinstance(phase_id, str) or not phase_id.strip():
        return None, "phase_id must be a non-empty string"

    git_err = _validate_optional_git_extensions(data["git"])
    if git_err:
        return None, git_err

    if "require_runtime_checks" in data and not isinstance(data["require_runtime_checks"], bool):
        return None, "require_runtime_checks must be a boolean"

    runtime_required = bool(data.get("require_runtime_checks", True))
    allowed = data["wrappers"]["allowed"]
    forbidden = data["wrappers"]["forbidden"]
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        return None, "wrappers.allowed must be a list of strings"
    if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
        return None, "wrappers.forbidden must be a list of strings"
    if runtime_required and not allowed:
        return None, "wrappers.allowed must not be empty when require_runtime_checks is true"

    overlap = set(allowed) & set(forbidden)
    if overlap:
        return None, f"wrappers allowed/forbidden overlap: {sorted(overlap)}"

    return PhaseManifest(
        phase_id=phase_id.strip(),
        description=str(data.get("description", "")),
        raw=data,
    ), None


def load_manifest(phase_id: str, *, manifest_dir: Path | None = None) -> tuple[PhaseManifest | None, str | None]:
    """Load a phase manifest by ID. Unknown phase => (None, reason)."""
    if not phase_id or not str(phase_id).strip():
        return None, "phase id is required"

    base = manifest_dir or MANIFEST_DIR
    path = base / f"{phase_id.strip()}.json"
    if not path.is_file():
        return None, f"unknown phase manifest: {phase_id}"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"failed to read manifest: {exc}"

    manifest, err = validate_manifest_dict(data)
    if manifest is None:
        return None, err
    if manifest.phase_id != phase_id.strip():
        return None, (
            f"manifest phase_id mismatch: file declares {manifest.phase_id!r}, "
            f"requested {phase_id!r}"
        )
    return manifest, None


def list_known_phases(*, manifest_dir: Path | None = None) -> list[str]:
    base = manifest_dir or MANIFEST_DIR
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.json"))


# --- Phase-scoped allowlist sidecar (E5J) ---

ALLOWLIST_SUPPORTED_MANIFESTS = frozenset({
    "CODEX_REVIEW_DIRTY_TREE",
    "COMMIT_EXACT_FILES",
})

ALLOWLIST_SIDECAR_KEYS = frozenset({
    "manifest_id",
    "authorization_phase_id",
    "approved_dirty_paths",
    "approved_file_allowlist",
    "notes",
})

ALLOWLIST_MAX_PATH_COUNT = 64

_WILDCARD_RE = re.compile(r"[*?]")


@dataclass(frozen=True)
class AllowlistSidecar:
    manifest_id: str
    authorization_phase_id: str
    approved_dirty_paths: tuple[str, ...] | None = None
    approved_file_allowlist: tuple[str, ...] | None = None
    source_basename: str = ""


def _normalize_allowlist_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def validate_repo_relative_path(path: str, repo_root: Path) -> tuple[str | None, str | None]:
    """Validate and normalize a repo-relative allowlist path. Returns (normalized, error)."""
    if not isinstance(path, str) or not path.strip():
        return None, "path must be a non-empty string"

    raw = path.strip()
    if _WILDCARD_RE.search(raw):
        return None, f"wildcards not allowed in path: {raw!r}"

    norm = _normalize_allowlist_path(raw)
    if norm.startswith("/"):
        return None, f"absolute path not allowed: {raw!r}"
    if re.match(r"^[A-Za-z]:", norm):
        return None, f"drive path not allowed: {raw!r}"
    if norm.startswith("//") or norm.startswith("\\\\"):
        return None, f"UNC path not allowed: {raw!r}"

    while norm.startswith("./"):
        norm = norm[2:]

    if not norm:
        return None, "path is empty after normalization"

    parts = norm.split("/")
    if any(p == ".." for p in parts):
        return None, f"path traversal not allowed: {raw!r}"
    if any(p == "." for p in parts):
        return None, f"invalid path segment in: {raw!r}"
    if any(p == "" for p in parts):
        return None, f"invalid path segment in: {raw!r}"

    try:
        resolved = (repo_root / norm).resolve()
        resolved.relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return None, f"path resolves outside repo root: {raw!r}"

    return norm, None


def _validate_path_list(
    paths: object,
    *,
    repo_root: Path,
) -> tuple[tuple[str, ...] | None, str | None]:
    if not isinstance(paths, list):
        return None, "allowlist must be a list of strings"
    if not paths:
        return None, "allowlist must not be empty"
    if len(paths) > ALLOWLIST_MAX_PATH_COUNT:
        return None, f"allowlist exceeds maximum of {ALLOWLIST_MAX_PATH_COUNT} entries"

    normalized: list[str] = []
    seen: set[str] = set()
    for entry in paths:
        if not isinstance(entry, str):
            return None, "allowlist path entries must be strings"
        norm, err = validate_repo_relative_path(entry, repo_root)
        if err:
            return None, err
        assert norm is not None
        key = norm.lower() if sys.platform == "win32" else norm
        if key in seen:
            continue
        seen.add(key)
        normalized.append(norm)

    if not normalized:
        return None, "allowlist must not be empty after normalization"
    return tuple(normalized), None


def load_allowlist_sidecar(
    path: Path,
    phase_id: str,
    repo_root: Path,
) -> tuple[AllowlistSidecar | None, dict[str, Any] | None, str | None, str | None]:
    """Load and validate sidecar JSON. Returns (sidecar, metadata, check_id, detail)."""
    if not path.is_file():
        return None, None, "allowlist.file_missing", f"allowlist file not found: {path.name}"

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, None, "allowlist.file_missing", f"allowlist file unreadable: {exc}"

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, None, "allowlist.invalid_json", f"invalid allowlist JSON: {exc}"

    if not isinstance(data, dict):
        return None, None, "allowlist.invalid_json", "allowlist root must be a JSON object"

    unknown = sorted(k for k in data if k not in ALLOWLIST_SIDECAR_KEYS)
    if unknown:
        return None, None, "allowlist.schema", f"unknown sidecar keys: {', '.join(unknown)}"

    manifest_id = data.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        return None, None, "allowlist.schema", "manifest_id must be a non-empty string"
    if manifest_id.strip() != phase_id.strip():
        return None, None, "allowlist.manifest_mismatch", (
            f"sidecar manifest_id {manifest_id!r} != --phase {phase_id!r}"
        )

    auth_phase = data.get("authorization_phase_id")
    if not isinstance(auth_phase, str) or not auth_phase.strip():
        return None, None, "allowlist.schema", "authorization_phase_id must be a non-empty string"

    has_dirty = "approved_dirty_paths" in data
    has_exact = "approved_file_allowlist" in data
    if has_dirty and has_exact:
        return None, None, "allowlist.schema", "sidecar must not include both allowlist arrays"
    if not has_dirty and not has_exact:
        return None, None, "allowlist.schema", "sidecar must include exactly one allowlist array"

    phase = phase_id.strip()
    dirty_paths: tuple[str, ...] | None = None
    file_allowlist: tuple[str, ...] | None = None
    fields_applied: list[str] = []

    if phase == "CODEX_REVIEW_DIRTY_TREE":
        if has_exact:
            return None, None, "allowlist.field_forbidden", (
                "approved_file_allowlist is not allowed for CODEX_REVIEW_DIRTY_TREE"
            )
        dirty_paths, err = _validate_path_list(data["approved_dirty_paths"], repo_root=repo_root)
        if err:
            if "must be strings" in err:
                check_id = "allowlist.schema"
            elif any(token in err for token in ("path", "wildcard", "traversal", "segment", "absolute", "drive", "UNC")):
                check_id = "allowlist.path_invalid"
            else:
                check_id = "allowlist.schema"
            return None, None, check_id, err
        fields_applied = ["approved_dirty_paths"]
    elif phase == "COMMIT_EXACT_FILES":
        if has_dirty:
            return None, None, "allowlist.field_forbidden", (
                "approved_dirty_paths is not allowed for COMMIT_EXACT_FILES"
            )
        file_allowlist, err = _validate_path_list(data["approved_file_allowlist"], repo_root=repo_root)
        if err:
            if "must be strings" in err:
                check_id = "allowlist.schema"
            elif any(token in err for token in ("path", "wildcard", "traversal", "segment", "absolute", "drive", "UNC")):
                check_id = "allowlist.path_invalid"
            else:
                check_id = "allowlist.schema"
            return None, None, check_id, err
        fields_applied = ["approved_file_allowlist"]
    else:
        return None, None, "allowlist.unsupported_manifest", (
            f"manifest {phase!r} does not support --allowlist-file"
        )

    path_count = len(dirty_paths or file_allowlist or ())
    metadata = {
        "source_basename": path.name,
        "authorization_phase_id": auth_phase.strip(),
        "fields_applied": fields_applied,
        "path_count": path_count,
    }
    sidecar = AllowlistSidecar(
        manifest_id=phase,
        authorization_phase_id=auth_phase.strip(),
        approved_dirty_paths=dirty_paths,
        approved_file_allowlist=file_allowlist,
        source_basename=path.name,
    )
    return sidecar, metadata, None, None


def merge_allowlist_into_manifest(
    manifest: PhaseManifest,
    sidecar: AllowlistSidecar,
) -> tuple[PhaseManifest | None, str | None]:
    """Deep-copy official manifest and inject validated allowlist paths only."""
    import copy

    raw = copy.deepcopy(manifest.raw)
    git = raw.setdefault("git", {})
    if sidecar.approved_dirty_paths is not None:
        git["approved_dirty_paths"] = list(sidecar.approved_dirty_paths)
    if sidecar.approved_file_allowlist is not None:
        git["approved_file_allowlist"] = list(sidecar.approved_file_allowlist)

    merged, err = validate_manifest_dict(raw)
    if merged is None:
        return None, err or "failed to merge allowlist into manifest"
    return merged, None
