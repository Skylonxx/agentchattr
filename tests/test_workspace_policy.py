"""Unit tests for workspace_policy schema validation (Phase 1)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import workspace_policy as wp  # noqa: E402


SCRATCH_ROOT = "C:/tools/agentchattr-scratch"
EXAMPLE_ROOT = "C:/tmp/example-workspace"


def _example_profiles() -> dict:
    return {
        "agentchattr-scratch": {
            "workspace_root": SCRATCH_ROOT,
            "allowed_modes": ["scratch-readonly", "read-only"],
            "default_mode": "scratch-readonly",
            "max_mode": "read-only",
            "default_forbidden_paths": [".git/**"],
        },
        "example-workspace": {
            "workspace_root": EXAMPLE_ROOT,
            "allowed_modes": ["read-only", "docs-only"],
            "default_mode": "read-only",
            "max_mode": "docs-only",
            "require_git_repo": True,
            "default_read_paths": [EXAMPLE_ROOT],
            "default_forbidden_paths": [
                "src/**",
                "tests/**",
                ".git/**",
            ],
            "allowed_write_files": [
                "Task.md",
                "docs/STATE.md",
                "docs/reports/latest-report.md",
                "docs/reports/new-report.md",
            ],
            "default_write_files": [
                "Task.md",
                "docs/STATE.md",
                "docs/reports/latest-report.md",
                "docs/reports/new-report.md",
            ],
            "max_write_files": 4,
        },
    }


class MissingPolicyDefaultsTests(unittest.TestCase):
    def test_missing_policy_defaults_to_scratch_readonly(self):
        result = wp.resolve_workspace_policy()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "scratch-readonly")
        self.assertIsNone(result.policy["workspace"]["root"])
        self.assertEqual(result.policy["write_files"], [])


class ValidPolicyTests(unittest.TestCase):
    def test_valid_scratch_readonly_policy(self):
        policy = wp.default_scratch_readonly_policy()
        result = wp.validate_resolved_policy(policy)
        self.assertTrue(result.ok, result.errors)

    def test_valid_read_only_policy(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={"workspace_mode": "read-only"},
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "read-only")
        self.assertEqual(result.policy["workspace"]["root"], EXAMPLE_ROOT)
        self.assertEqual(result.policy["write_files"], [])

    def test_read_only_analysis_requires_external_report_write_roots(self):
        policy = {
            "schema_version": 1,
            "mode": "read-only",
            "analysis_report_only": True,
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": [],
            "forbidden_paths": [],
            "forbidden_commands": [],
            "git_permissions": wp._default_git_permissions(),
            "report_paths": ["C:/Users/Narachat/OneDrive/Ai-Report/claude/x.md"],
            "external_report_write_roots": [],
            "role_permissions": wp._default_role_permissions("read-only"),
            "enforcement": {"fail_closed": True},
        }
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("external_report_write_roots" in e for e in result.errors))

    def test_valid_docs_only_policy_with_exact_docs_files(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": [
                    "Task.md",
                    "docs/STATE.md",
                ],
                "expected_head": "752ed13",
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["mode"], "docs-only")
        self.assertEqual(len(result.policy["write_files"]), 2)
        dev = wp.role_permission_for(result.policy, "developer")
        self.assertEqual(dev["filesystem"], "write_allowlist")


class WriteFileRejectionTests(unittest.TestCase):
    def _assert_write_rejected(self, path: str):
        errors, norm = wp._validate_write_file(path, field="write_files[0]")
        self.assertTrue(errors, f"expected rejection for {path!r}")
        self.assertIsNone(norm)

    def test_reject_wildcard_write_file(self):
        self._assert_write_rejected("docs/*.md")

    def test_reject_double_wildcard_write_file(self):
        self._assert_write_rejected("docs/**/x.md")

    def test_reject_absolute_write_file(self):
        self._assert_write_rejected("/tmp/x.md")

    def test_reject_drive_qualified_write_file(self):
        self._assert_write_rejected("C:\\tmp\\x.md")

    def test_reject_unc_write_file(self):
        self._assert_write_rejected("\\\\server\\share\\file.txt")

    def test_reject_device_namespace_write_file(self):
        self._assert_write_rejected("\\\\?\\C:\\tmp\\x.md")

    def test_reject_parent_traversal(self):
        self._assert_write_rejected("docs/../src/App.tsx")

    def test_reject_mixed_separator_traversal(self):
        self._assert_write_rejected("docs\\..\\src/App.tsx")

    def test_reject_alternate_data_stream(self):
        self._assert_write_rejected("Task.md:secret")

    def test_reject_windows_reserved_device_name(self):
        self._assert_write_rejected("CON")

    def test_reject_directory_trailing_separator(self):
        self._assert_write_rejected("docs/")


class ForbiddenPathPrecedenceTests(unittest.TestCase):
    def test_forbidden_paths_override_write_files(self):
        policy = {
            "schema_version": 1,
            "mode": "docs-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": ["src/App.tsx"],
            "forbidden_paths": ["src/**", ".git/**"],
            "forbidden_commands": [],
            "git_permissions": wp._default_git_permissions(),
            "report_paths": [],
            "role_permissions": wp._default_role_permissions("docs-only"),
            "enforcement": {"fail_closed": True},
        }
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("forbidden_paths overrides write_files" in e for e in result.errors))

    def test_git_forbidden_glob(self):
        policy = {
            "schema_version": 1,
            "mode": "docs-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": [".git/config"],
            "forbidden_paths": [".git/**"],
            "forbidden_commands": [],
            "git_permissions": wp._default_git_permissions(),
            "report_paths": [],
            "role_permissions": wp._default_role_permissions("docs-only"),
            "enforcement": {"fail_closed": True},
        }
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)

    def test_case_insensitive_forbidden_match(self):
        self.assertTrue(wp._matches_forbidden("SRC/App.tsx", "src/**"))

    def test_docs_only_rejects_source_path(self):
        policy = {
            "schema_version": 1,
            "mode": "docs-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": ["src/App.tsx"],
            "forbidden_paths": [],
            "forbidden_commands": [],
            "git_permissions": wp._default_git_permissions(),
            "report_paths": [],
            "role_permissions": wp._default_role_permissions("docs-only"),
            "enforcement": {"fail_closed": True},
        }
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("docs-only rejects source path" in e for e in result.errors))


class ProfileAndPayloadTests(unittest.TestCase):
    def test_unknown_workspace_profile_rejected(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="missing-profile",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "UNKNOWN_PROFILE")

    def test_payload_narrowing_accepted(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": ["Task.md"],
            },
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.policy["write_files"], ["Task.md"])

    def test_payload_broadening_mode_rejected(self):
        profiles = _example_profiles()
        profiles["elevated-example"] = {
            **profiles["example-workspace"],
            "allowed_modes": ["read-only", "docs-only", "implementation"],
            "max_mode": "docs-only",
        }
        result = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="elevated-example",
            start_payload={"workspace_mode": "implementation"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")
        self.assertTrue(any("broaden mode" in e for e in result.errors))

    def test_payload_broadening_write_files_rejected(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": ["src/App.tsx"],
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")
        self.assertTrue(any("broaden write_files" in e for e in result.errors))

    def test_payload_cannot_override_workspace_root(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={"workspace_root": "C:/other/path"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")

    def test_empty_allowed_write_files_rejects_payload_writes(self):
        profiles = _example_profiles()
        profiles["docs-no-writes"] = {
            "workspace_root": EXAMPLE_ROOT,
            "allowed_modes": ["read-only", "docs-only"],
            "default_mode": "read-only",
            "max_mode": "docs-only",
            "default_read_paths": [EXAMPLE_ROOT],
            "default_forbidden_paths": ["src/**", ".git/**"],
        }
        result = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="docs-no-writes",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": ["Task.md"],
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")
        self.assertTrue(
            any("profile allowlist is empty" in e for e in result.errors),
            result.errors,
        )


class ModeAndGitTests(unittest.TestCase):
    def test_invalid_mode_rejected(self):
        policy = wp.default_scratch_readonly_policy()
        policy["mode"] = "super-write"
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)

    def test_git_write_permissions_denied_by_default(self):
        perms = wp._default_git_permissions()
        for key in wp.GIT_WRITE_KEYS:
            self.assertFalse(perms[key])
        for key in wp.GIT_READ_KEYS:
            self.assertTrue(perms[key])

    def test_git_write_rejected_in_docs_only_policy(self):
        policy = wp.default_scratch_readonly_policy()
        policy.update({
            "mode": "docs-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": ["Task.md"],
            "git_permissions": {**wp._default_git_permissions(), "commit": True},
            "role_permissions": wp._default_role_permissions("docs-only"),
        })
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("git write permission" in e for e in result.errors))


class RolePermissionTests(unittest.TestCase):
    def test_role_permissions_evaluated_by_canonical_role(self):
        policy = wp.default_scratch_readonly_policy()
        policy["role_permissions"] = wp._default_role_permissions("docs-only")
        policy["mode"] = "docs-only"
        policy["workspace"] = {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False}
        policy["read_paths"] = [EXAMPLE_ROOT]
        policy["write_files"] = ["Task.md"]
        result = wp.validate_resolved_policy(policy)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(wp.role_permission_for(result.policy, "coordinator")["filesystem"], "none")
        self.assertTrue(wp.role_permission_for(result.policy, "reviewer")["verify_diff"])
        self.assertIsNone(wp.role_permission_for(result.policy, "not-a-role"))

    def test_role_broadening_rejected(self):
        policy = wp.default_scratch_readonly_policy()
        roles = wp._default_role_permissions("read-only")
        roles["coordinator"] = {"filesystem": "write_allowlist", "git": "read", "verify_diff": False}
        policy["role_permissions"] = roles
        policy["mode"] = "read-only"
        policy["workspace"] = {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False}
        policy["read_paths"] = [EXAMPLE_ROOT]
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)

    def test_developer_git_write_rejected(self):
        policy = wp.default_scratch_readonly_policy()
        roles = wp._default_role_permissions("docs-only")
        roles["developer"] = {
            "filesystem": "write_allowlist",
            "git": "write",
            "verify_diff": False,
        }
        policy.update({
            "mode": "docs-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "write_files": ["Task.md"],
            "role_permissions": roles,
        })
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "POLICY_INVALID")
        self.assertTrue(any("developer" in e and "git invalid" in e for e in result.errors))

    def test_arbitrary_role_git_value_rejected(self):
        policy = wp.default_scratch_readonly_policy()
        roles = wp._default_role_permissions("read-only")
        roles["reviewer"] = {"filesystem": "read", "git": "admin", "verify_diff": True}
        policy.update({
            "mode": "read-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "role_permissions": roles,
        })
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("git invalid" in e for e in result.errors))

    def test_default_read_git_value_accepted(self):
        policy = wp.default_scratch_readonly_policy()
        policy.update({
            "mode": "read-only",
            "workspace": {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False},
            "read_paths": [EXAMPLE_ROOT],
            "role_permissions": wp._default_role_permissions("read-only"),
        })
        result = wp.validate_resolved_policy(policy)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(wp.role_permission_for(result.policy, "developer")["git"], "read")


class TemplateCeilingTests(unittest.TestCase):
    def test_template_max_write_files_cannot_add_beyond_profile(self):
        profiles = _example_profiles()
        result = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="example-workspace",
            template_policy={"max_write_files": ["Task.md", "docs/extra-not-allowed.md"]},
            start_payload={"workspace_mode": "docs-only", "write_files": ["Task.md"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "TEMPLATE_CEILING")
        self.assertTrue(any("cannot broaden" in e for e in result.errors))

    def test_template_cannot_add_writes_when_profile_allowlist_empty(self):
        profiles = _example_profiles()
        profiles["no-writes"] = {
            "workspace_root": EXAMPLE_ROOT,
            "allowed_modes": ["read-only", "docs-only"],
            "default_mode": "read-only",
            "max_mode": "docs-only",
        }
        result = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="no-writes",
            template_policy={"max_write_files": ["Task.md"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "TEMPLATE_CEILING")

    def test_template_max_mode_constrains_payload_mode(self):
        profiles = _example_profiles()
        result = wp.resolve_workspace_policy(
            profiles=profiles,
            profile_id="example-workspace",
            template_policy={"max_mode": "read-only"},
            start_payload={"workspace_mode": "docs-only", "write_files": ["Task.md"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "PAYLOAD_REJECTED")
        self.assertTrue(
            any(
                phrase in e
                for e in result.errors
                for phrase in ("broaden mode", "not allowed by profile")
            ),
            result.errors,
        )


class InvalidProfileModeTests(unittest.TestCase):
    def test_invalid_profile_default_mode_rejected(self):
        profiles = {
            "bad-default": {
                "workspace_root": EXAMPLE_ROOT,
                "allowed_modes": ["read-only", "docs-only"],
                "default_mode": "git-push",
            }
        }
        result = wp.resolve_workspace_policy(profiles=profiles, profile_id="bad-default")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "INVALID_PROFILE")
        self.assertTrue(
            any("default_mode" in e for e in result.errors),
            result.errors,
        )

    def test_invalid_profile_max_mode_rejected(self):
        profiles = {
            "bad-max": {
                "workspace_root": EXAMPLE_ROOT,
                "allowed_modes": ["read-only", "docs-only"],
                "max_mode": "not-a-mode",
            }
        }
        result = wp.resolve_workspace_policy(profiles=profiles, profile_id="bad-max")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "INVALID_PROFILE")
        self.assertTrue(any("invalid max_mode" in e for e in result.errors))

    def test_max_mode_below_default_mode_rejected(self):
        profiles = {
            "bad-range": {
                "workspace_root": EXAMPLE_ROOT,
                "allowed_modes": ["read-only", "docs-only"],
                "default_mode": "docs-only",
                "max_mode": "read-only",
            }
        }
        result = wp.resolve_workspace_policy(profiles=profiles, profile_id="bad-range")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "INVALID_PROFILE")


class AbsolutePathValidationTests(unittest.TestCase):
    def test_workspace_root_drive_relative_rejected(self):
        errors = wp._validate_absolute_path("C:relative", field="workspace.root", require_value=True)
        self.assertTrue(errors)
        self.assertTrue(any("fully qualified" in e for e in errors))

    def test_workspace_root_root_relative_rejected(self):
        errors = wp._validate_absolute_path("\\foo", field="workspace.root", require_value=True)
        self.assertTrue(errors)
        self.assertTrue(any("root-relative" in e for e in errors))

    def test_workspace_root_unc_rejected(self):
        errors = wp._validate_absolute_path(
            "\\\\server\\share\\repo", field="workspace.root", require_value=True
        )
        self.assertTrue(errors)
        self.assertTrue(any("UNC" in e for e in errors))

    def test_workspace_root_device_namespace_rejected(self):
        errors = wp._validate_absolute_path(
            "\\\\?\\C:\\repo", field="workspace.root", require_value=True
        )
        self.assertTrue(errors)
        self.assertTrue(any("device namespace" in e for e in errors))

    def test_read_paths_drive_relative_rejected(self):
        errors = wp._validate_absolute_path("C:relative", field="read_paths[0]")
        self.assertTrue(errors)

    def test_report_paths_unc_rejected(self):
        errors = wp._validate_absolute_path("\\\\server\\share\\reports", field="report_paths[0]")
        self.assertTrue(errors)

    def test_valid_windows_absolute_path_accepted(self):
        errors = wp._validate_absolute_path("C:\\repo\\root", field="workspace.root", require_value=True)
        self.assertEqual(errors, [])


class FailClosedTests(unittest.TestCase):
    def test_corrupt_policy_fails_closed(self):
        result = wp.validate_resolved_policy("not-a-mapping")
        self.assertFalse(result.ok)

    def test_local_override_broadening_rejected(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            local_override={"write_files": ["Task.md"]},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "LOCAL_BROADEN_REJECTED")

    def test_policy_size_cap_enforced(self):
        policy = wp.default_scratch_readonly_policy()
        policy["mode"] = "read-only"
        policy["workspace"] = {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False}
        policy["read_paths"] = [EXAMPLE_ROOT]
        policy["padding"] = "x" * 9000
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)
        self.assertTrue(any("max size" in e for e in result.errors))

    def test_write_files_count_cap_enforced(self):
        policy = wp.default_scratch_readonly_policy()
        policy["mode"] = "docs-only"
        policy["workspace"] = {"root": EXAMPLE_ROOT, "expected_head": None, "require_git_repo": False}
        policy["read_paths"] = [EXAMPLE_ROOT]
        policy["write_files"] = [f"docs/file{i}.md" for i in range(wp.MAX_WRITE_FILES + 1)]
        policy["role_permissions"] = wp._default_role_permissions("docs-only")
        result = wp.validate_resolved_policy(policy)
        self.assertFalse(result.ok)


class ExpectedHeadTests(unittest.TestCase):
    def test_expected_head_full_sha(self):
        self.assertEqual(wp._validate_expected_head("a" * 40), [])

    def test_expected_head_short_sha(self):
        self.assertEqual(wp._validate_expected_head("752ed13"), [])

    def test_expected_head_invalid(self):
        self.assertTrue(wp._validate_expected_head("not-a-sha"))


class ReadAndReportPathTests(unittest.TestCase):
    def test_read_paths_require_absolute_shape(self):
        errors = wp._validate_absolute_path("relative/path", field="read_paths[0]", require_value=True)
        self.assertTrue(errors)

    def test_report_paths_do_not_imply_write_access(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "read-only",
            },
        )
        self.assertTrue(result.ok, result.errors)
        policy = result.policy
        policy["report_paths"] = ["C:/tmp/external-report-dir"]
        validated = wp.validate_resolved_policy(policy)
        self.assertTrue(validated.ok, validated.errors)
        self.assertEqual(validated.policy["write_files"], [])


class UntrackedDocsShapeTests(unittest.TestCase):
    def test_unlisted_docs_file_allowed_only_when_exactly_listed(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": ["docs/reports/new-report.md"],
            },
        )
        self.assertTrue(result.ok, result.errors)

        result2 = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={
                "workspace_mode": "docs-only",
                "write_files": ["docs/reports/not-in-profile.md"],
            },
        )
        self.assertFalse(result2.ok)


class ScopedWriteModeAliasTests(unittest.TestCase):
    def test_scoped_write_normalizes_to_implementation(self):
        self.assertEqual(wp.normalize_workspace_mode("scoped-write"), "implementation")
        self.assertEqual(wp.normalize_workspace_mode("read-only"), "read-only")
        self.assertEqual(wp.normalize_workspace_mode("read-only-analysis"), "read-only")

    def test_scoped_write_payload_rejected_without_profile_allowlist(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
            start_payload={"workspace_mode": "scoped-write"},
        )
        self.assertFalse(result.ok)


class StripInternalKeysTests(unittest.TestCase):
    def test_strip_internal_policy_keys(self):
        result = wp.resolve_workspace_policy(
            profiles=_example_profiles(),
            profile_id="example-workspace",
        )
        self.assertTrue(result.ok, result.errors)
        cleaned = wp.strip_internal_policy_keys(result.policy)
        self.assertNotIn("_profile_allowed_modes", cleaned)
        json.dumps(cleaned)


if __name__ == "__main__":
    unittest.main()
