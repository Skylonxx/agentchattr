"""Build info diagnostics tests."""

from __future__ import annotations

import unittest

from build_info import read_build_info


class BuildInfoTests(unittest.TestCase):
    def test_read_build_info_has_keys(self):
        info = read_build_info()
        self.assertIn("version", info)
        self.assertIn("git_commit", info)

    def test_git_commit_not_empty_in_repo(self):
        info = read_build_info()
        self.assertTrue(info.get("git_commit"), msg="expected git commit in dev repo")


if __name__ == "__main__":
    unittest.main()
