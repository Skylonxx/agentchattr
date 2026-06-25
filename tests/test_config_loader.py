"""Focused tests for config_loader local agent override behavior."""

import io
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config_loader  # noqa: E402
import safety_invariants  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


class ConfigLoaderLocalAgentOverrideTests(unittest.TestCase):
    def _load_from_temp(self, *, base_toml: str, local_toml: str | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "config.toml", base_toml)
            if local_toml is not None:
                _write(root / "config.local.toml", local_toml)

            buf = io.StringIO()
            with redirect_stdout(buf):
                config = config_loader.load_config(root)
        return config, buf.getvalue()

    def test_existing_agent_safe_keys_override_base(self):
        config, output = self._load_from_temp(
            base_toml="""
            [agents.claude]
            command = "claude"
            cwd = ".."
            color = "#da7756"
            label = "Claude"
            """,
            local_toml="""
            [agents.claude]
            cwd = "C:/tools/agentchattr-scratch"
            run_mode = "print_exec"
            """,
        )

        claude = config["agents"]["claude"]
        self.assertEqual(claude["cwd"], "C:/tools/agentchattr-scratch")
        self.assertEqual(claude["run_mode"], "print_exec")
        self.assertIn("Applied safe local overrides for agent 'claude'", output)

    def test_existing_agent_preserves_protected_base_keys(self):
        config, _ = self._load_from_temp(
            base_toml="""
            [agents.claude]
            command = "claude"
            cwd = ".."
            color = "#da7756"
            label = "Claude"
            """,
            local_toml="""
            [agents.claude]
            cwd = "C:/tools/agentchattr-scratch"
            run_mode = "print_exec"
            """,
        )

        claude = config["agents"]["claude"]
        self.assertEqual(claude["command"], "claude")
        self.assertEqual(claude["label"], "Claude")
        self.assertEqual(claude["color"], "#da7756")

    def test_existing_agent_ignores_protected_command_override(self):
        config, output = self._load_from_temp(
            base_toml="""
            [agents.claude]
            command = "claude"
            cwd = ".."
            color = "#da7756"
            label = "Claude"
            """,
            local_toml="""
            [agents.claude]
            command = "evil-claude"
            cwd = "C:/tools/agentchattr-scratch"
            run_mode = "print_exec"
            system_prompt = "override"
            """,
        )

        claude = config["agents"]["claude"]
        self.assertEqual(claude["command"], "claude")
        self.assertEqual(claude["cwd"], "C:/tools/agentchattr-scratch")
        self.assertEqual(claude["run_mode"], "print_exec")
        self.assertIn("Ignoring unsafe local overrides for agent 'claude'", output)
        self.assertIn("command", output)
        self.assertIn("system_prompt", output)

    def test_brand_new_local_agents_are_added(self):
        config, _ = self._load_from_temp(
            base_toml="""
            [agents.claude]
            command = "claude"
            """,
            local_toml="""
            [agents.localtest]
            command = "local-cli"
            cwd = "C:/tmp/localtest"
            run_mode = "exec"
            """,
        )

        self.assertEqual(config["agents"]["localtest"]["command"], "local-cli")
        self.assertEqual(config["agents"]["localtest"]["cwd"], "C:/tmp/localtest")
        self.assertEqual(config["agents"]["localtest"]["run_mode"], "exec")

    def test_sandbox_local_override_behavior_remains_unchanged(self):
        config, _ = self._load_from_temp(
            base_toml="""
            [sandbox]
            flow_start_enabled = false
            flow_start_max_active = 1
            flow_start_output_root = "C:/base"
            """,
            local_toml="""
            [sandbox]
            flow_start_enabled = true
            flow_start_max_active = 2
            flow_start_output_root = "C:/local"
            """,
        )

        sandbox = config["sandbox"]
        self.assertTrue(sandbox["flow_start_enabled"])
        self.assertEqual(sandbox["flow_start_max_active"], 2)
        self.assertEqual(sandbox["flow_start_output_root"], "C:/local")

    def test_unknown_local_run_mode_remains_subject_to_invariant_guard(self):
        config, _ = self._load_from_temp(
            base_toml="""
            [agents.claude]
            command = "claude"
            cwd = ".."
            """,
            local_toml="""
            [agents.claude]
            run_mode = "totally_invalid_mode"
            """,
        )

        run_mode = config["agents"]["claude"]["run_mode"]
        self.assertEqual(run_mode, "totally_invalid_mode")
        self.assertFalse(safety_invariants.check_run_mode_known(run_mode).ok)


if __name__ == "__main__":
    unittest.main()
