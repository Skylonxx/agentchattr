"""Role file documentation tests — verify docs/ai-roles/*.md exist and align."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES_DIR = ROOT / "docs" / "ai-roles"

EXPECTED_FILES = [
    "tech-lead.md",
    "system-architect.md",
    "workflow-coordinator.md",
    "developer.md",
    "environment-auditor.md",
    "reviewer.md",
    "ux-lead.md",
    "ui-implementer.md",
    "safety-reviewer.md",
]

EXPECTED_HEADINGS = [
    "## Purpose",
    "## Typical Assignee",
    "## Allowed Scope",
    "## Forbidden Scope",
    "## Required Boundaries",
    "## Handoff Expectations",
]

TYPICAL_ASSIGNEES = {
    "tech-lead.md": "Gemini",
    "system-architect.md": "ChatGPT",
    "workflow-coordinator.md": "ChatGPT",
    "developer.md": "Claude",
    "environment-auditor.md": "Claude",
    "reviewer.md": "Codex",
    "ux-lead.md": "AGY",
    "ui-implementer.md": "Claude",
    "safety-reviewer.md": None,
}


class RoleFileExistenceTests(unittest.TestCase):

    def test_all_role_files_exist(self):
        for name in EXPECTED_FILES:
            with self.subTest(file=name):
                self.assertTrue(
                    (ROLES_DIR / name).exists(),
                    f"docs/ai-roles/{name} must exist",
                )

    def test_role_files_have_required_headings(self):
        for name in EXPECTED_FILES:
            path = ROLES_DIR / name
            if not path.exists():
                continue
            content = path.read_text("utf-8")
            with self.subTest(file=name):
                for heading in EXPECTED_HEADINGS:
                    self.assertIn(heading, content,
                                  f"{name} missing heading: {heading}")

    def test_role_files_contain_expected_assignee(self):
        for name, assignee in TYPICAL_ASSIGNEES.items():
            if assignee is None:
                continue
            path = ROLES_DIR / name
            if not path.exists():
                continue
            content = path.read_text("utf-8")
            with self.subTest(file=name, assignee=assignee):
                self.assertIn(assignee, content,
                              f"{name} must mention typical assignee '{assignee}'")


class RoleFileBoundaryTests(unittest.TestCase):

    def test_safety_reviewer_is_not_codexsafe(self):
        path = ROLES_DIR / "safety-reviewer.md"
        content = path.read_text("utf-8")
        self.assertIn("NOT CodexSafe", content,
                       "safety-reviewer.md must clarify it is NOT CodexSafe")

    def test_safety_reviewer_has_authorized_assignee(self):
        path = ROLES_DIR / "safety-reviewer.md"
        content = path.read_text("utf-8")
        self.assertIn("authorized", content.lower(),
                       "safety-reviewer.md must specify an explicitly authorized assignee")
        self.assertIn("reviewer", content.lower(),
                       "safety-reviewer.md assignee must reference reviewer concept")

    def test_reviewer_no_codex_coordinator_external_role(self):
        path = ROLES_DIR / "reviewer.md"
        content = path.read_text("utf-8")
        self.assertIn("Codex Coordinator", content,
                       "reviewer.md must mention Codex Coordinator restriction")
        self.assertIn("Forbidden", content)

    def test_workflow_coordinator_forbids_codexsafe_persona(self):
        path = ROLES_DIR / "workflow-coordinator.md"
        content = path.read_text("utf-8")
        self.assertIn("CodexSafe", content,
                       "workflow-coordinator.md must address CodexSafe persona drift")
        self.assertIn("Forbidden", content)


if __name__ == "__main__":
    unittest.main()
