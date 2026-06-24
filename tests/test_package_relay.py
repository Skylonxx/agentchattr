"""Regression tests for Codex review-package path relay."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_relay import (  # noqa: E402
    APPROVED_ROOTS,
    PackageRelayError,
    build_sealed_review_prompt,
    chunk_text,
    extract_package_title,
    format_relay_system_summary,
    is_codex_agent,
    load_package,
    make_package_relay_queue_entry,
    parse_review_package_path,
    prepare_package_review_relay,
    validate_package_path,
)


class PackageRelayPathValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.onedrive_root = self.root / "OneDrive" / "Ai-Report"
        self.desktop_root = self.root / "Desktop" / "Ai-Report"
        self.onedrive_root.mkdir(parents=True)
        self.desktop_root.mkdir(parents=True)

        import package_relay as pr

        self._orig_roots = pr.APPROVED_ROOTS
        pr.APPROVED_ROOTS = (self.onedrive_root, self.desktop_root)

    def tearDown(self):
        import package_relay as pr

        pr.APPROVED_ROOTS = self._orig_roots

    def _write(self, rel: str, content: str, *, desktop: bool = False) -> Path:
        base = self.desktop_root if desktop else self.onedrive_root
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.resolve()

    def test_onedrive_path_accepted(self):
        path = self._write("claude/pkg.md", "# Title\nbody")
        resolved = validate_package_path(str(path))
        self.assertEqual(resolved, path)

    def test_desktop_path_accepted(self):
        path = self._write("claude/pkg.txt", "hello", desktop=True)
        resolved = validate_package_path(str(path))
        self.assertEqual(resolved, path)

    def test_non_approved_path_rejected(self):
        outside = self.root / "outside" / "x.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(PackageRelayError):
            validate_package_path(str(outside.resolve()))

    def test_missing_file_rejected(self):
        missing = self.onedrive_root / "claude" / "missing.md"
        with self.assertRaises(PackageRelayError):
            validate_package_path(str(missing))

    def test_path_traversal_rejected(self):
        with self.assertRaises(PackageRelayError):
            validate_package_path(
                str(self.onedrive_root / "claude" / ".." / ".." / "secret.md")
            )

    def test_md_and_txt_accepted(self):
        md = self._write("a.md", "# A")
        txt = self._write("b.txt", "B")
        self.assertEqual(validate_package_path(str(md)).suffix, ".md")
        self.assertEqual(validate_package_path(str(txt)).suffix, ".txt")

    def test_other_extensions_rejected(self):
        bad = self._write("bad.json", "{}")
        with self.assertRaises(PackageRelayError):
            validate_package_path(str(bad))


class PackageRelayManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import package_relay as pr

        self._orig_roots = pr.APPROVED_ROOTS
        root = Path(self.tmp.name) / "Ai-Report"
        root.mkdir()
        pr.APPROVED_ROOTS = (root,)
        self.pkg = root / "claude" / "sample.md"
        self.pkg.parent.mkdir(parents=True)
        self.content = "# Sample Package\n\nline two\n"
        self.pkg.write_text(self.content, encoding="utf-8")

    def tearDown(self):
        import package_relay as pr

        pr.APPROVED_ROOTS = self._orig_roots

    def test_sha256_stable(self):
        m1, _ = load_package(str(self.pkg))
        m2, _ = load_package(str(self.pkg))
        expected = hashlib.sha256(self.pkg.read_bytes()).hexdigest()
        self.assertEqual(m1.sha256, expected)
        self.assertEqual(m1.sha256, m2.sha256)

    def test_counts_included(self):
        raw = self.pkg.read_bytes()
        manifest, _ = load_package(str(self.pkg))
        self.assertEqual(manifest.bytes, len(raw))
        self.assertEqual(manifest.chars, len(raw.decode("utf-8")))
        self.assertGreaterEqual(manifest.lines, 2)
        self.assertEqual(manifest.title, "Sample Package")

    def test_chunk_order_preserved(self):
        long_body = "A" * 100 + "\n" + "B" * 100
        self.pkg.write_text("# Long\n" + long_body, encoding="utf-8")
        manifest, prompt = load_package(str(self.pkg), max_chunk_chars=80)
        self.assertGreater(manifest.chunk_count, 1)
        self.assertIn("BEGIN PACKAGE CHUNK 1/", prompt)
        self.assertIn("END PACKAGE CHUNK 1/", prompt)
        joined = "".join(chunk_text(long_body, 80))
        self.assertEqual(joined, long_body)


class PackageRelayPromptTests(unittest.TestCase):
    def test_prompt_includes_manifest_and_ack_instruction(self):
        from package_relay import PackageManifest

        manifest = PackageManifest(
            path=r"C:\x\pkg.md",
            title="Pkg",
            sha256="abc123",
            bytes=10,
            chars=10,
            lines=2,
            chunk_count=1,
        )
        prompt = build_sealed_review_prompt(manifest, ["chunk-body"])
        self.assertIn("MODE: SEALED-TEXT REVIEW ONLY", prompt)
        self.assertIn("NO REPO ACCESS", prompt)
        self.assertIn("Package title: Pkg", prompt)
        self.assertIn("SHA-256: abc123", prompt)
        self.assertIn("Chunk count: 1", prompt)
        self.assertIn("BEGIN PACKAGE CHUNK 1/1", prompt)
        self.assertIn("END PACKAGE CHUNK 1/1", prompt)
        self.assertIn("Acknowledge package title, SHA-256, chunk count", prompt)

    def test_relay_queue_entry_disables_mcp(self):
        entry = make_package_relay_queue_entry(
            prompt="sealed", channel="general", path=r"C:\x\pkg.md",
        )
        self.assertTrue(entry["relay_meta"]["relay_mode"])
        self.assertTrue(entry["relay_meta"]["disable_mcp"])
        self.assertEqual(entry["prompt"], "sealed")


class PackageRelayParseTests(unittest.TestCase):
    def test_parse_unquoted_path(self):
        text = "@codex review-package C:\\Users\\Narachat\\OneDrive\\Ai-Report\\claude\\a.md"
        self.assertEqual(
            parse_review_package_path(text),
            r"C:\Users\Narachat\OneDrive\Ai-Report\claude\a.md",
        )

    def test_parse_quoted_path_with_spaces(self):
        text = '@codex review-package "C:\\Users\\Narachat\\OneDrive\\Ai-Report\\claude\\file with spaces.md"'
        self.assertEqual(
            parse_review_package_path(text),
            r"C:\Users\Narachat\OneDrive\Ai-Report\claude\file with spaces.md",
        )

    def test_normal_codex_mention_not_parsed_as_review_package(self):
        self.assertIsNone(parse_review_package_path("@codex please review this text"))

    def test_is_codex_agent(self):
        self.assertTrue(is_codex_agent("codex"))
        self.assertTrue(is_codex_agent("codex-1"))
        self.assertTrue(is_codex_agent("codex_reviewer"))
        self.assertFalse(is_codex_agent("agy"))


class PackageRelayIntegrationTests(unittest.TestCase):
    def test_prepare_relay_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import package_relay as pr

        orig = pr.APPROVED_ROOTS
        root = Path(tmp.name) / "Ai-Report" / "claude"
        root.mkdir(parents=True)
        pkg = root / "review.md"
        pkg.write_text("# Review\nCSS rules here\n", encoding="utf-8")
        pr.APPROVED_ROOTS = (Path(tmp.name) / "Ai-Report",)

        entry, manifest = prepare_package_review_relay(str(pkg), channel="general")
        self.assertIn("relay_meta", entry)
        self.assertIn("CSS rules here", entry["prompt"])
        self.assertIn("Review", manifest.title)
        self.assertIn("SHA-256", entry["prompt"])
        pr.APPROVED_ROOTS = orig

    def test_no_write_stage_commit_language(self):
        prompt = build_sealed_review_prompt(
            __import__("package_relay").PackageManifest(
                path="p", title="t", sha256="h", bytes=1, chars=1, lines=1, chunk_count=1,
            ),
            ["body"],
        )
        lowered = prompt.lower()
        self.assertNotIn("git commit", lowered)
        self.assertNotIn("git push", lowered)
        self.assertNotIn("stage files", lowered)


if __name__ == "__main__":
    unittest.main()
