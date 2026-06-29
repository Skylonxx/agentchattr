"""Pure workspace policy schema validation (Phase 1 — no runtime enforcement).

Deterministic, side-effect-free validation for per-session workspace policies.
Missing policy resolves to safe ``scratch-readonly`` defaults.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

SCHEMA_VERSION = 1
SESSION_POLICY_VERSION = 1
MAX_WRITE_FILES = 32
MAX_FORBIDDEN_PATHS = 64
MAX_READ_PATHS = 16
MAX_REPORT_PATHS = 8
MAX_REPORT_ROOTS = 8
MAX_POLICY_BYTES = 8192
MIN_HEAD_LEN = 7
MAX_HEAD_LEN = 40

VALID_MODES = (
    "scratch-readonly",
    "read-only",
    "docs-only",
    "implementation",
    "git-stage",
    "git-commit",
    "git-push",
)

MODE_RANK = {mode: idx for idx, mode in enumerate(VALID_MODES)}

# Session API alias: scoped-write maps to implementation (narrow write_files allowlist).
SCOPED_WRITE_MODE_ALIASES = frozenset({"scoped-write"})
READ_ONLY_ANALYSIS_MODE_ALIASES = frozenset({"read-only-analysis"})
# Trusted direct repo CLI (Phase 1) is a read-only session driven by a coordinator
# execution memo with Claude tools enabled and no snapshots. The canonical mode is
# read-only; trusted behaviour is carried by the profile flag trusted_direct_repo_cli.
TRUSTED_DIRECT_REPO_CLI_MODE_ALIASES = frozenset({
    "trusted_direct_repo_cli",
    "trusted-direct-repo-cli",
})


def normalize_workspace_mode(mode: str | None) -> str | None:
    """Normalize API mode aliases to canonical VALID_MODES values."""
    if mode is None:
        return None
    if mode in SCOPED_WRITE_MODE_ALIASES:
        return "implementation"
    if mode in READ_ONLY_ANALYSIS_MODE_ALIASES:
        return "read-only"
    if mode in TRUSTED_DIRECT_REPO_CLI_MODE_ALIASES:
        return "read-only"
    return mode

CANONICAL_ROLES = frozenset({
    "coordinator",
    "developer",
    "ui_lead",
    "reviewer",
    "safety_gate",
})

GIT_READ_KEYS = frozenset({"status", "diff", "log", "show", "stash_list"})
GIT_WRITE_KEYS = frozenset({
    "stage",
    "commit",
    "push",
    "reset",
    "checkout",
    "clean",
    "stash_apply",
    "stash_pop",
    "stash_drop",
})

VALID_ROLE_GIT = frozenset({"none", "read"})

WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
})

_UNC_RE = re.compile(r"^\\\\[^\\]+\\[^\\]+")
_DEVICE_NS_RE = re.compile(r"^\\\\[?.]\\")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HEAD_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class PolicyValidationResult:
    ok: bool
    policy: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()
    code: str = ""


def default_scratch_readonly_policy() -> dict[str, Any]:
    """Safe default when no workspace policy is supplied."""
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "scratch-readonly",
        "workspace": {
            "root": None,
            "expected_head": None,
            "require_git_repo": False,
        },
        "read_paths": [],
        "write_files": [],
        "forbidden_paths": [],
        "forbidden_commands": list(_default_forbidden_commands()),
        "git_permissions": dict(_default_git_permissions()),
        "report_paths": [],
        "external_report_write_roots": [],
        "role_permissions": dict(_default_role_permissions("scratch-readonly")),
        "enforcement": {
            "fail_closed": True,
            "abort_on_unauthorized_dirty": True,
            "post_turn_diff_check": False,
            "max_write_files_per_turn": 0,
        },
    }


def _default_forbidden_commands() -> list[str]:
    return [
        "git stash apply",
        "git stash pop",
        "git stash drop",
        "git add",
        "git commit",
        "git push",
        "git reset",
        "git checkout",
        "git clean",
    ]


def _default_git_permissions() -> dict[str, bool]:
    perms = {key: True for key in GIT_READ_KEYS}
    for key in GIT_WRITE_KEYS:
        perms[key] = False
    return perms


def _default_role_permissions(mode: str) -> dict[str, dict[str, Any]]:
    base_read = {"filesystem": "read", "git": "read", "verify_diff": False}
    base_none = {"filesystem": "none", "git": "none", "verify_diff": False}
    reviewer = {"filesystem": "read", "git": "read", "verify_diff": True}
    safety = {"filesystem": "read", "git": "read", "verify_diff": True}

    if mode == "scratch-readonly":
        developer = {"filesystem": "none", "git": "none", "verify_diff": False}
    elif mode in ("read-only", "docs-only", "implementation"):
        developer_fs = "write_allowlist" if mode in ("docs-only", "implementation") else "read"
        developer = {"filesystem": developer_fs, "git": "read", "verify_diff": False}
    else:
        developer = {"filesystem": "write_allowlist", "git": "read", "verify_diff": False}

    return {
        "coordinator": dict(base_none),
        "developer": developer,
        "ui_lead": dict(base_read),
        "reviewer": dict(reviewer),
        "safety_gate": dict(safety),
    }


def resolve_workspace_policy(
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    profile_id: str | None = None,
    template_policy: dict[str, Any] | None = None,
    start_payload: dict[str, Any] | None = None,
    local_override: dict[str, Any] | None = None,
) -> PolicyValidationResult:
    """Resolve and validate an effective workspace policy.

    Profile registry is server-approved input. ``start_payload`` may only narrow
    the profile/template maximum. ``local_override`` must not broaden policy.
    """
    profiles = profiles or {}

    if (
        profile_id is None
        and not template_policy
        and not start_payload
        and not local_override
    ):
        policy = default_scratch_readonly_policy()
        return validate_resolved_policy(policy)

    if profile_id is None:
        return PolicyValidationResult(
            False,
            errors=("workspace profile is required for non-default policy",),
            code="MISSING_PROFILE",
        )

    if profile_id not in profiles:
        return PolicyValidationResult(
            False,
            errors=(f"unknown workspace profile: {profile_id!r}",),
            code="UNKNOWN_PROFILE",
        )

    profile = profiles[profile_id]
    profile_errors = _validate_profile_definition(profile_id, profile)
    if profile_errors:
        return PolicyValidationResult(False, errors=tuple(profile_errors), code="INVALID_PROFILE")

    max_policy = _profile_to_policy(profile_id, profile)
    if template_policy:
        narrow = _apply_template_ceiling(max_policy, template_policy)
        if narrow.errors:
            return PolicyValidationResult(False, errors=narrow.errors, code=narrow.code)
        max_policy = narrow.policy or max_policy

    if start_payload:
        narrowed = _apply_start_payload(max_policy, start_payload, profile)
        if narrowed.errors:
            return PolicyValidationResult(False, errors=narrowed.errors, code=narrowed.code)
        max_policy = narrowed.policy or max_policy

    if local_override:
        local_result = _apply_local_override(max_policy, local_override)
        if local_result.errors:
            return PolicyValidationResult(False, errors=local_result.errors, code=local_result.code)
        max_policy = local_result.policy or max_policy

    return validate_resolved_policy(max_policy)


def validate_resolved_policy(policy: dict[str, Any]) -> PolicyValidationResult:
    """Validate a fully resolved policy object (fail-closed)."""
    errors: list[str] = []

    if not isinstance(policy, dict):
        return PolicyValidationResult(False, errors=("policy must be a mapping",), code="INVALID_POLICY")

    try:
        encoded = json.dumps(policy, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return PolicyValidationResult(False, errors=("policy is not JSON-serializable",), code="INVALID_POLICY")

    if len(encoded.encode("utf-8")) > MAX_POLICY_BYTES:
        errors.append(f"policy exceeds max size ({MAX_POLICY_BYTES} bytes)")

    version = policy.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {version!r}")

    mode = policy.get("mode")
    if mode not in VALID_MODES:
        errors.append(f"invalid mode: {mode!r}")
    else:
        errors.extend(_validate_mode_constraints(policy, mode))

    workspace = policy.get("workspace")
    if not isinstance(workspace, dict):
        errors.append("workspace must be a mapping")
        workspace = {}

    root = workspace.get("root")
    if mode == "scratch-readonly":
        if root not in (None, ""):
            errors.append("scratch-readonly must not set workspace.root")
    else:
        root_errs = _validate_absolute_path(root, field="workspace.root", require_value=True)
        errors.extend(root_errs)

    expected_head = workspace.get("expected_head")
    if expected_head is not None and expected_head != "":
        errors.extend(_validate_expected_head(expected_head))

    read_paths = policy.get("read_paths", [])
    if not isinstance(read_paths, list):
        errors.append("read_paths must be a list")
        read_paths = []
    elif len(read_paths) > MAX_READ_PATHS:
        errors.append(f"read_paths exceeds max ({MAX_READ_PATHS})")
    else:
        for idx, path in enumerate(read_paths):
            errors.extend(_validate_absolute_path(path, field=f"read_paths[{idx}]"))

    report_paths = policy.get("report_paths", [])
    if not isinstance(report_paths, list):
        errors.append("report_paths must be a list")
        report_paths = []
    elif len(report_paths) > MAX_REPORT_PATHS:
        errors.append(f"report_paths exceeds max ({MAX_REPORT_PATHS})")
    else:
        for idx, path in enumerate(report_paths):
            errors.extend(_validate_absolute_path(path, field=f"report_paths[{idx}]"))

    report_roots = policy.get("external_report_write_roots", [])
    if not isinstance(report_roots, list):
        errors.append("external_report_write_roots must be a list")
        report_roots = []
    elif len(report_roots) > MAX_REPORT_ROOTS:
        errors.append(f"external_report_write_roots exceeds max ({MAX_REPORT_ROOTS})")
    else:
        for idx, path in enumerate(report_roots):
            errors.extend(_validate_absolute_path(path, field=f"external_report_write_roots[{idx}]"))

    write_files = policy.get("write_files", [])
    if not isinstance(write_files, list):
        errors.append("write_files must be a list")
        write_files = []
    elif len(write_files) > MAX_WRITE_FILES:
        errors.append(f"write_files exceeds max ({MAX_WRITE_FILES})")
    else:
        seen: set[str] = set()
        for idx, wf in enumerate(write_files):
            wf_errors, norm = _validate_write_file(wf, field=f"write_files[{idx}]")
            errors.extend(wf_errors)
            if norm and norm in seen:
                errors.append(f"duplicate write_files entry: {norm!r}")
            if norm:
                seen.add(norm)

    forbidden_paths = policy.get("forbidden_paths", [])
    if not isinstance(forbidden_paths, list):
        errors.append("forbidden_paths must be a list")
        forbidden_paths = []
    elif len(forbidden_paths) > MAX_FORBIDDEN_PATHS:
        errors.append(f"forbidden_paths exceeds max ({MAX_FORBIDDEN_PATHS})")
    else:
        for idx, fp in enumerate(forbidden_paths):
            if not isinstance(fp, str) or not fp.strip():
                errors.append(f"forbidden_paths[{idx}] must be a non-empty string")
            elif ".." in fp.replace("\\", "/"):
                errors.append(f"forbidden_paths[{idx}] must not contain parent traversal")

    for wf in write_files if isinstance(write_files, list) else []:
        _, norm = _validate_write_file(wf, field="write_files")
        if norm and isinstance(forbidden_paths, list):
            for pattern in forbidden_paths:
                if isinstance(pattern, str) and _matches_forbidden(norm, pattern):
                    errors.append(
                        f"forbidden_paths overrides write_files: {norm!r} matched {pattern!r}"
                    )
                    break

    git_perms = policy.get("git_permissions")
    errors.extend(_validate_git_permissions(git_perms, mode if mode in VALID_MODES else "scratch-readonly"))

    role_perms = policy.get("role_permissions")
    errors.extend(_validate_role_permissions(role_perms, mode if mode in VALID_MODES else "scratch-readonly"))

    if errors:
        return PolicyValidationResult(False, errors=tuple(errors), code="POLICY_INVALID")

    return PolicyValidationResult(True, policy=policy, code="OK")


def role_permission_for(policy: dict[str, Any], role: str) -> dict[str, Any] | None:
    """Return canonical role permissions from a validated policy."""
    if role not in CANONICAL_ROLES:
        return None
    role_perms = policy.get("role_permissions") or {}
    if not isinstance(role_perms, dict):
        return None
    entry = role_perms.get(role)
    return dict(entry) if isinstance(entry, dict) else None


def _validate_profile_definition(profile_id: str, profile: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["workspace profile must be a mapping"]

    root = profile.get("workspace_root")
    errors.extend(_validate_absolute_path(root, field=f"profiles[{profile_id}].workspace_root", require_value=True))

    allowed_modes = profile.get("allowed_modes")
    if not isinstance(allowed_modes, list) or not allowed_modes:
        errors.append(f"profiles[{profile_id}].allowed_modes must be a non-empty list")
    else:
        for mode in allowed_modes:
            if mode not in VALID_MODES:
                errors.append(f"profiles[{profile_id}] has invalid allowed_mode: {mode!r}")

    max_write = profile.get("max_write_files", MAX_WRITE_FILES)
    if not isinstance(max_write, int) or max_write < 0 or max_write > MAX_WRITE_FILES:
        errors.append(f"profiles[{profile_id}].max_write_files out of range")

    default_forbidden = profile.get("default_forbidden_paths", [])
    if default_forbidden is not None and not isinstance(default_forbidden, list):
        errors.append(f"profiles[{profile_id}].default_forbidden_paths must be a list")

    allowed_mode_set = [m for m in (allowed_modes or []) if isinstance(m, str)]

    default_mode = profile.get("default_mode")
    if default_mode is not None:
        if default_mode not in VALID_MODES:
            errors.append(f"profiles[{profile_id}] has invalid default_mode: {default_mode!r}")
        elif allowed_mode_set and default_mode not in allowed_mode_set:
            errors.append(f"profiles[{profile_id}] default_mode not in allowed_modes")

    max_mode = profile.get("max_mode")
    if max_mode is not None:
        if max_mode not in VALID_MODES:
            errors.append(f"profiles[{profile_id}] has invalid max_mode: {max_mode!r}")
        elif allowed_mode_set and max_mode not in allowed_mode_set:
            errors.append(f"profiles[{profile_id}] max_mode not in allowed_modes")
        elif (
            default_mode in VALID_MODES
            and max_mode in VALID_MODES
            and MODE_RANK[max_mode] < MODE_RANK[default_mode]
        ):
            errors.append(f"profiles[{profile_id}] max_mode is below default_mode")

    return errors


def _profile_to_policy(profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    allowed_modes = profile.get("allowed_modes") or ["scratch-readonly"]
    allowed_modes = [m for m in allowed_modes if m in VALID_MODES]
    default_mode = profile.get("default_mode") or allowed_modes[0]
    if default_mode not in allowed_modes:
        default_mode = allowed_modes[0]
    max_mode = profile.get("max_mode")
    if max_mode not in VALID_MODES or max_mode not in allowed_modes:
        max_mode = max(allowed_modes, key=lambda m: MODE_RANK[m])
    root = profile.get("workspace_root")
    write_files: list[str] = []
    if default_mode in ("docs-only", "implementation", "git-stage", "git-commit", "git-push"):
        write_files = list(profile.get("default_write_files") or [])
    report_paths = list(profile.get("default_report_paths") or [])
    report_roots = list(profile.get("default_external_report_write_roots") or [])
    if not report_roots:
        for path in report_paths:
            try:
                report_roots.append(str(PureWindowsPath(path).parent))
            except Exception:
                continue
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_id": profile_id,
        "mode": default_mode,
        "workspace": {
            "root": root,
            "expected_head": profile.get("expected_head"),
            "require_git_repo": bool(profile.get("require_git_repo", False)),
        },
        "read_paths": list(profile.get("default_read_paths") or ([root] if root else [])),
        "write_files": write_files,
        "forbidden_paths": list(profile.get("default_forbidden_paths") or []),
        "forbidden_commands": list(profile.get("forbidden_commands") or _default_forbidden_commands()),
        "git_permissions": dict(profile.get("git_permissions") or _default_git_permissions()),
        "report_paths": report_paths,
        "external_report_write_roots": report_roots,
        "analysis_report_only": bool(profile.get("analysis_report_only", False)),
        "on_demand_snapshots": bool(profile.get("on_demand_snapshots", False)),
        "trusted_direct_repo_cli": bool(profile.get("trusted_direct_repo_cli", False)),
        "suggested_initial_snapshot_paths": list(
            profile.get("suggested_initial_snapshot_paths") or []
        ),
        "trusted_cli_primary_paths": list(
            profile.get("trusted_cli_primary_paths") or []
        ),
        "role_permissions": dict(_default_role_permissions(default_mode)),
        "enforcement": {
            "fail_closed": True,
            "abort_on_unauthorized_dirty": True,
            "post_turn_diff_check": default_mode != "scratch-readonly",
            "max_write_files_per_turn": len(profile.get("default_write_files") or []),
        },
        "_profile_allowed_modes": list(allowed_modes),
        "_profile_max_mode": max_mode,
        "_profile_max_write_files": profile.get("max_write_files", MAX_WRITE_FILES),
        "_profile_allowed_write_files": list(
            profile.get("allowed_write_files") or profile.get("default_write_files") or []
        ),
    }


def _apply_template_ceiling(max_policy: dict[str, Any], template_policy: dict[str, Any]) -> PolicyValidationResult:
    errors: list[str] = []
    out = dict(max_policy)

    tmpl_mode = template_policy.get("max_mode") or template_policy.get("mode")
    if tmpl_mode:
        if tmpl_mode not in VALID_MODES:
            return PolicyValidationResult(
                False,
                errors=(f"template max_mode invalid: {tmpl_mode!r}",),
                code="INVALID_TEMPLATE",
            )
        current_max = out.get("_profile_max_mode")
        if current_max in VALID_MODES and MODE_RANK[tmpl_mode] < MODE_RANK[current_max]:
            out["_profile_max_mode"] = tmpl_mode
        profile_modes = out.get("_profile_allowed_modes") or list(VALID_MODES)
        out["_profile_allowed_modes"] = [
            m for m in profile_modes if m in VALID_MODES and MODE_RANK[m] <= MODE_RANK[tmpl_mode]
        ]
        if not out["_profile_allowed_modes"]:
            errors.append("template max_mode leaves no allowed modes")
        elif out["mode"] not in out["_profile_allowed_modes"]:
            out["mode"] = out["_profile_allowed_modes"][0]
        elif MODE_RANK[out["mode"]] > MODE_RANK[tmpl_mode]:
            out["mode"] = tmpl_mode
        out["role_permissions"] = dict(_default_role_permissions(out["mode"]))
        if out["mode"] in ("scratch-readonly", "read-only"):
            out["write_files"] = []

    allowed = template_policy.get("allowed_modes")
    if allowed:
        if not isinstance(allowed, list):
            errors.append("template allowed_modes must be a list")
        else:
            profile_modes = out.get("_profile_allowed_modes") or list(VALID_MODES)
            intersection = [m for m in profile_modes if m in allowed]
            if not intersection:
                errors.append("template allowed_modes incompatible with profile")
            else:
                out["_profile_allowed_modes"] = intersection
                effective_max = out.get("_profile_max_mode")
                if effective_max in intersection:
                    out["_profile_allowed_modes"] = [
                        m for m in intersection if MODE_RANK[m] <= MODE_RANK[effective_max]
                    ]
                if out["mode"] not in out["_profile_allowed_modes"]:
                    out["mode"] = out["_profile_allowed_modes"][0]
                    out["role_permissions"] = dict(_default_role_permissions(out["mode"]))

    tmpl_forbidden = template_policy.get("forbidden_paths")
    if tmpl_forbidden:
        merged = list(out.get("forbidden_paths") or [])
        for item in tmpl_forbidden:
            if item not in merged:
                merged.append(item)
        out["forbidden_paths"] = merged

    tmpl_writes = template_policy.get("max_write_files")
    if isinstance(tmpl_writes, list):
        profile_allowed = set(out.get("_profile_allowed_write_files") or [])
        if tmpl_writes:
            if not profile_allowed:
                errors.append("template cannot introduce write_files when profile allowlist is empty")
            elif any(w not in profile_allowed for w in tmpl_writes):
                errors.append("template max_write_files cannot broaden beyond profile allowlist")
            else:
                narrowed_allowed = [w for w in tmpl_writes if w in profile_allowed]
                out["_profile_allowed_write_files"] = narrowed_allowed
                out["write_files"] = [
                    w for w in out.get("write_files", []) if w in narrowed_allowed
                ]

    if errors:
        return PolicyValidationResult(False, errors=tuple(errors), code="TEMPLATE_CEILING")
    return PolicyValidationResult(True, policy=out, code="OK")


def _apply_start_payload(
    max_policy: dict[str, Any],
    payload: dict[str, Any],
    profile: dict[str, Any],
) -> PolicyValidationResult:
    errors: list[str] = []
    out = dict(max_policy)

    if payload.get("workspace_root") and payload.get("workspace_root") != out.get("workspace", {}).get("root"):
        errors.append("start payload cannot override workspace root")

    if payload.get("profile_id") and payload.get("profile_id") != out.get("policy_id"):
        errors.append("start payload cannot switch workspace profile")

    mode = payload.get("workspace_mode") or payload.get("mode")
    if mode:
        mode = normalize_workspace_mode(mode)
        if mode not in VALID_MODES:
            errors.append(f"invalid workspace_mode: {mode!r}")
        else:
            allowed = out.get("_profile_allowed_modes") or profile.get("allowed_modes") or []
            max_mode = out.get("_profile_max_mode") or profile.get("max_mode") or mode
            if mode not in allowed:
                errors.append(f"workspace_mode {mode!r} not allowed by profile")
            elif MODE_RANK[mode] > MODE_RANK[max_mode]:
                errors.append("start payload cannot broaden mode beyond profile maximum")
            else:
                out["mode"] = mode
                out["role_permissions"] = dict(_default_role_permissions(mode))
                if mode in ("scratch-readonly", "read-only"):
                    out["write_files"] = []
                elif mode in ("docs-only", "implementation") and not out.get("write_files"):
                    out["write_files"] = list(out.get("_profile_allowed_write_files") or [])

    payload_writes = payload.get("write_files")
    if payload_writes is not None:
        if not isinstance(payload_writes, list):
            errors.append("write_files payload must be a list")
        else:
            max_writes = set(out.get("_profile_allowed_write_files") or [])
            if payload_writes and not max_writes:
                errors.append(
                    "start payload cannot add write_files when profile allowlist is empty"
                )
            elif any(w not in max_writes for w in payload_writes):
                errors.append("start payload cannot broaden write_files beyond profile maximum")
            else:
                max_count = out.get("_profile_max_write_files", MAX_WRITE_FILES)
                if len(payload_writes) > max_count:
                    errors.append("start payload write_files exceeds profile cap")
                else:
                    out["write_files"] = list(payload_writes)

    expected_head = payload.get("expected_head")
    if expected_head is not None:
        head_errors = _validate_expected_head(expected_head)
        if head_errors:
            errors.extend(head_errors)
        else:
            ws = dict(out.get("workspace") or {})
            ws["expected_head"] = expected_head.strip()
            out["workspace"] = ws

    if errors:
        return PolicyValidationResult(False, errors=tuple(errors), code="PAYLOAD_REJECTED")
    return PolicyValidationResult(True, policy=out, code="OK")


def _apply_local_override(max_policy: dict[str, Any], local_override: dict[str, Any]) -> PolicyValidationResult:
    """Local overrides (e.g. config.local.toml) must not broaden policy."""
    errors: list[str] = []

    broaden_keys = (
        "write_files",
        "mode",
        "workspace_mode",
        "workspace_root",
        "git_permissions",
        "read_paths",
        "report_paths",
        "external_report_write_roots",
    )
    for key in broaden_keys:
        if key not in local_override:
            continue
        errors.append(f"local override cannot modify workspace policy field: {key}")

    if local_override.get("disable_fail_closed") or local_override.get("allow_wildcard_writes"):
        errors.append("local override broadening flags are not supported")

    if errors:
        return PolicyValidationResult(False, errors=tuple(errors), code="LOCAL_BROADEN_REJECTED")
    return PolicyValidationResult(True, policy=max_policy, code="OK")


def _validate_mode_constraints(policy: dict[str, Any], mode: str) -> list[str]:
    errors: list[str] = []
    write_files = policy.get("write_files") or []

    if mode == "scratch-readonly":
        if write_files:
            errors.append("scratch-readonly must not allow write_files")
        if policy.get("read_paths"):
            root = (policy.get("workspace") or {}).get("root")
            if root:
                errors.append("scratch-readonly must not declare external read_paths")
    elif mode == "read-only":
        if write_files:
            errors.append("read-only mode must not allow write_files")
        if policy.get("analysis_report_only") and not (policy.get("external_report_write_roots") or []):
            errors.append("read-only analysis mode requires external_report_write_roots")
    elif mode == "docs-only":
        if not write_files:
            errors.append("docs-only mode requires explicit write_files")
        for wf in write_files:
            norm = _normalize_rel_path(str(wf))
            if norm.startswith("src/") or "/src/" in f"/{norm}/":
                errors.append(f"docs-only rejects source path: {norm!r}")
    elif mode == "implementation":
        if not write_files:
            errors.append("implementation mode requires explicit write_files")
    elif mode in ("git-stage", "git-commit", "git-push"):
        git_perms = policy.get("git_permissions") or {}
        if mode == "git-stage" and not git_perms.get("stage"):
            errors.append(f"{mode} requires git_permissions.stage=true")
        if mode == "git-commit" and (not git_perms.get("stage") or not git_perms.get("commit")):
            errors.append(f"{mode} requires git_permissions.stage and commit")
        if mode == "git-push" and not (git_perms.get("push") and git_perms.get("commit") and git_perms.get("stage")):
            errors.append(f"{mode} requires git_permissions.stage, commit, and push")

    return errors


def _validate_git_permissions(git_perms: Any, mode: str) -> list[str]:
    errors: list[str] = []
    if git_perms is None:
        return errors
    if not isinstance(git_perms, dict):
        return ["git_permissions must be a mapping"]

    for key in git_perms:
        if key not in GIT_READ_KEYS and key not in GIT_WRITE_KEYS:
            errors.append(f"unknown git_permissions key: {key!r}")

    merged = _default_git_permissions()
    merged.update({k: bool(v) for k, v in git_perms.items()})

    for key in GIT_WRITE_KEYS:
        if merged.get(key) and not _git_write_allowed_for_mode(mode, key):
            errors.append(
                f"git write permission {key!r} is not allowed for mode {mode!r}"
            )

    return errors


def _git_write_allowed_for_mode(mode: str, key: str) -> bool:
    if mode == "git-stage" and key == "stage":
        return True
    if mode == "git-commit" and key in ("stage", "commit"):
        return True
    if mode == "git-push" and key in ("stage", "commit", "push"):
        return True
    if mode == "implementation" and key in ("stage", "commit", "push"):
        return False
    return False


def _validate_role_permissions(role_perms: Any, mode: str) -> list[str]:
    errors: list[str] = []
    if role_perms is None:
        return errors
    if not isinstance(role_perms, dict):
        return ["role_permissions must be a mapping"]

    for role, perms in role_perms.items():
        if role not in CANONICAL_ROLES:
            errors.append(f"unknown role_permissions role: {role!r}")
            continue
        if not isinstance(perms, dict):
            errors.append(f"role_permissions[{role!r}] must be a mapping")
            continue

        fs = perms.get("filesystem")
        if fs not in ("none", "read", "write_allowlist"):
            errors.append(f"role_permissions[{role!r}].filesystem invalid: {fs!r}")

        git = perms.get("git")
        if git is not None and git not in VALID_ROLE_GIT:
            errors.append(f"role_permissions[{role!r}].git invalid: {git!r}")

        if role == "coordinator" and fs not in (None, "none", "read"):
            errors.append("coordinator must not have write filesystem permission")
        if role == "developer" and mode in ("read-only", "scratch-readonly") and fs == "write_allowlist":
            errors.append("developer write_allowlist not permitted in read-only/scratch modes")
        if role in ("reviewer", "safety_gate") and not perms.get("verify_diff", False):
            errors.append(f"{role} must have verify_diff=true")
        if role in ("coordinator", "ui_lead") and perms.get("verify_diff"):
            errors.append(f"{role} must not have verify_diff=true")

    defaults = _default_role_permissions(mode)
    for role in CANONICAL_ROLES:
        if role not in role_perms:
            continue
        entry = role_perms[role]
        default = defaults[role]
        if _role_broadens(default, entry):
            errors.append(f"role_permissions[{role!r}] broadens beyond canonical defaults")

    return errors


def _role_broadens(default: dict[str, Any], actual: dict[str, Any]) -> bool:
    fs_rank = {"none": 0, "read": 1, "write_allowlist": 2}
    git_rank = {"none": 0, "read": 1}
    d_fs = fs_rank.get(default.get("filesystem", "none"), 0)
    a_fs = fs_rank.get(actual.get("filesystem", "none"), 0)
    if a_fs > d_fs:
        return True
    d_git = git_rank.get(default.get("git", "none"), 0)
    a_git = git_rank.get(actual.get("git", "none"), -1)
    if a_git < 0:
        return True
    if a_git > d_git:
        return True
    if actual.get("verify_diff") and not default.get("verify_diff"):
        return True
    return False


def _validate_expected_head(value: Any) -> list[str]:
    if not isinstance(value, str):
        return ["expected_head must be a string"]
    text = value.strip()
    if not _HEAD_RE.match(text):
        return ["expected_head must be 7-40 hex characters"]
    return []


def _validate_absolute_path(value: Any, *, field: str, require_value: bool = False) -> list[str]:
    errors: list[str] = []
    if value is None or value == "":
        if require_value:
            errors.append(f"{field} is required")
        return errors
    if not isinstance(value, str):
        errors.append(f"{field} must be a string")
        return errors

    text = value.strip()
    if not text:
        errors.append(f"{field} must not be empty")
        return errors

    if ".." in text.replace("\\", "/"):
        errors.append(f"{field} must not contain parent traversal")

    if _DEVICE_NS_RE.match(text) or text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        errors.append(f"{field} must not use device namespace paths")
        return errors

    if _UNC_RE.match(text):
        errors.append(f"{field} must not be a UNC path")
        return errors

    if _DRIVE_RE.match(text):
        rest = text[2:]
        if not rest.startswith("\\") and not rest.startswith("/"):
            errors.append(f"{field} must be a fully qualified absolute path")
        return errors

    if text.startswith("\\"):
        errors.append(f"{field} must not be a root-relative path")
        return errors

    if text.startswith("/"):
        errors.append(f"{field} must be absolute")
        return errors

    if require_value:
        errors.append(f"{field} must be absolute")

    return errors


def _validate_write_file(value: Any, *, field: str) -> tuple[list[str], str | None]:
    errors: list[str] = []
    if not isinstance(value, str):
        return ([f"{field} must be a string"], None)

    raw = value.strip()
    if not raw:
        return ([f"{field} must not be empty"], None)

    if raw.startswith(("/", "\\")) or _DRIVE_RE.match(raw) or _UNC_RE.match(raw):
        errors.append(f"{field} must be a relative path (not absolute/UNC/drive-qualified)")

    if _DEVICE_NS_RE.match(raw):
        errors.append(f"{field} must not use device namespace paths")

    if "*" in raw or "?" in raw:
        errors.append(f"{field} must not contain wildcards")

    if raw.endswith("/") or raw.endswith("\\"):
        errors.append(f"{field} must not be a directory path")

    if ":" in raw:
        errors.append(f"{field} must not contain alternate data stream separators")

    norm = _normalize_rel_path(raw)
    if ".." in norm.split("/"):
        errors.append(f"{field} must not contain parent traversal")

    if norm in (".", ""):
        errors.append(f"{field} must name a file")

    basename = norm.rsplit("/", 1)[-1]
    stem = basename.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        errors.append(f"{field} uses reserved Windows device name: {basename!r}")

    if "/" not in norm and norm.endswith("."):
        errors.append(f"{field} must not be a directory placeholder")

    if norm.count("/") >= 1 and norm.endswith("/."):
        errors.append(f"{field} must not be a directory path")

    return (errors, norm if not errors else None)


def _normalize_rel_path(path: str) -> str:
    text = path.replace("\\", "/").strip("/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            parts.append("..")
            continue
        parts.append(part)
    return "/".join(parts)


def _matches_forbidden(rel_path: str, pattern: str) -> bool:
    path = _normalize_rel_path(rel_path).lower()
    pat = pattern.replace("\\", "/").strip("/").lower()

    if pat.endswith("/**"):
        prefix = pat[:-3].strip("/")
        if not prefix:
            return True
        return path == prefix or path.startswith(prefix + "/")

    if "**" in pat:
        regex = "^" + re.escape(pat).replace("\\*\\*", ".*").replace("\\*", "[^/]*") + "$"
        return re.match(regex, path) is not None

    if "*" in pat or "?" in pat:
        return fnmatch.fnmatch(path, pat)

    return path == pat


def path_matches_forbidden(path: str, pattern: str) -> bool:
    """Return True when ``path`` matches a forbidden path pattern."""
    return _matches_forbidden(path, pattern)


def strip_internal_policy_keys(policy: dict[str, Any]) -> dict[str, Any]:
    """Remove resolver-only keys before persistence/display."""
    out = dict(policy)
    out.pop("_profile_allowed_modes", None)
    out.pop("_profile_max_write_files", None)
    out.pop("_profile_max_mode", None)
    out.pop("_profile_allowed_write_files", None)
    return out


def extract_policy_start_fields(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Extract workspace policy fields from a session start request body.

    Returns ``(start_payload, client_error)``. Policy authority is never parsed
    from ``goal`` or other free-text fields — only explicit payload keys count.
    """
    if not isinstance(body, dict):
        return None, None

    policy_keys = (
        "workspace_profile",
        "profile_id",
        "workspace_mode",
        "mode",
        "write_files",
        "expected_head",
        "workspace_root",
    )
    if not any(key in body for key in policy_keys):
        return None, None

    if body.get("workspace_root"):
        return None, "start payload cannot specify workspace_root"

    payload: dict[str, Any] = {}
    profile_id = body.get("workspace_profile") or body.get("profile_id")
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id.strip():
            return None, "workspace_profile must be a non-empty string"
        payload["profile_id"] = profile_id.strip()

    mode = body.get("workspace_mode")
    if mode is None and "mode" in body:
        mode = body.get("mode")
    if mode is not None:
        payload["workspace_mode"] = normalize_workspace_mode(mode)

    if "write_files" in body:
        payload["write_files"] = body["write_files"]

    if "expected_head" in body:
        payload["expected_head"] = body["expected_head"]

    return payload, None


def resolve_session_workspace_policy(
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    template_policy: dict[str, Any] | None = None,
    start_body: dict[str, Any] | None = None,
    local_override: dict[str, Any] | None = None,
) -> PolicyValidationResult:
    """Resolve workspace policy for session start (Phase 2 metadata wiring)."""
    start_body = start_body or {}
    start_payload, field_err = extract_policy_start_fields(start_body)
    if field_err:
        return PolicyValidationResult(False, errors=(field_err,), code="PAYLOAD_REJECTED")

    profile_id: str | None = None
    if start_payload:
        profile_id = start_payload.get("profile_id")
    if not profile_id and template_policy:
        profile_id = template_policy.get("profile_id") or template_policy.get("workspace_profile")

    return resolve_workspace_policy(
        profiles=profiles,
        profile_id=profile_id,
        template_policy=template_policy,
        start_payload=start_payload,
        local_override=local_override,
    )


def persistable_workspace_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Canonical immutable snapshot suitable for session persistence."""
    return strip_internal_policy_keys(policy)


def compute_workspace_policy_hash(policy: dict[str, Any]) -> str:
    """Deterministic SHA-256 hash of the canonical persisted policy snapshot."""
    canonical = persistable_workspace_policy(policy)
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_session_workspace_policy_fields(policy: dict[str, Any]) -> dict[str, Any]:
    """Build immutable session policy metadata fields after validation."""
    snapshot = persistable_workspace_policy(policy)
    return {
        "workspace_policy": snapshot,
        "workspace_policy_hash": compute_workspace_policy_hash(snapshot),
        "workspace_policy_version": SESSION_POLICY_VERSION,
    }


def workspace_policy_read_summary(session: dict[str, Any]) -> dict[str, Any]:
    """Read-only policy summary for API/session bar display (no authority)."""
    policy = session.get("workspace_policy")
    if not isinstance(policy, dict):
        return {
            "mode": "scratch-readonly",
            "profile_id": None,
            "workspace_root": None,
            "write_files": [],
            "write_files_count": 0,
            "external_report_write_roots": [],
            "external_report_write_roots_count": 0,
            "report_write_permission_ok": True,
            "expected_head": None,
            "hash": session.get("workspace_policy_hash"),
            "version": session.get("workspace_policy_version"),
        }

    workspace = policy.get("workspace") or {}
    write_files = list(policy.get("write_files") or [])
    report_roots = list(policy.get("external_report_write_roots") or [])
    report_orchestrated = bool(policy.get("analysis_report_only"))
    report_write_permission_ok = (not report_orchestrated) or bool(report_roots)
    return {
        "mode": policy.get("mode"),
        "profile_id": policy.get("policy_id"),
        "workspace_root": workspace.get("root"),
        "write_files": write_files,
        "write_files_count": len(write_files),
        "external_report_write_roots": report_roots,
        "external_report_write_roots_count": len(report_roots),
        "report_write_permission_ok": report_write_permission_ok,
        "report_write_permission_warning": (
            "BLOCKER: report write permission missing for report-orchestrated flow"
            if report_orchestrated and not report_roots
            else ""
        ),
        "expected_head": workspace.get("expected_head"),
        "hash": session.get("workspace_policy_hash"),
        "version": session.get("workspace_policy_version"),
        "prompt_id": session.get("prompt_id"),
        "has_prompt_body": bool((session.get("prompt_body") or "").strip()),
    }
