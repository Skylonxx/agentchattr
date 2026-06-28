"""Focused tests for Claude print_exec runtime mode."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import safety_invariants  # noqa: E402
import wrapper  # noqa: E402


class ClaudePrintCommandTests(unittest.TestCase):
    def test_build_claude_print_command_uses_stdin_by_default(self):
        cmd, stdin_payload = wrapper._build_claude_print_command(
            "claude.cmd", "Reply exactly: OK", use_stdin=True)

        self.assertEqual(cmd, ["claude.cmd", "--print", "--tools", ""])
        self.assertEqual(stdin_payload, b"Reply exactly: OK")
        self.assertNotIn("Reply exactly: OK", " ".join(cmd))

    def test_build_claude_print_command_can_fall_back_to_argv(self):
        cmd, stdin_payload = wrapper._build_claude_print_command(
            "claude.cmd", "Reply exactly: OK", use_stdin=False)

        self.assertEqual(cmd, ["claude.cmd", "--print", "--tools", "", "Reply exactly: OK"])
        self.assertIsNone(stdin_payload)


class ClaudePrintExecTests(unittest.TestCase):
    def _run_once(self, proc, *, get_token=True):
        captured = []

        def start_watcher(inject_fn):
            inject_fn("Prompt body", channel="sandbox-flow-test")

        token_fn = (lambda: "token") if get_token else None
        with patch.object(wrapper, "_relay_to_chat",
                          side_effect=lambda port, token, text, channel="general": captured.append(
                              {"port": port, "token": token, "text": text, "channel": channel}
                          )):
            with patch("subprocess.run", return_value=proc) as mock_run:
                wrapper.run_agent_claude_print_exec(
                    command="claude.cmd",
                    cwd="C:/tools/agentchattr-scratch",
                    env={"A": "B"},
                    agent="claude",
                    start_watcher=start_watcher,
                    running_flag=[False],
                    no_restart=True,
                    server_port=8300,
                    get_token_fn=token_fn,
                )
        return captured, mock_run

    def test_print_exec_relays_stdout_to_source_channel(self):
        proc = type("Proc", (), {
            "returncode": 0,
            "stdout": b"Claude says hi",
            "stderr": b"",
        })()

        captured, mock_run = self._run_once(proc)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["channel"], "sandbox-flow-test")
        self.assertEqual(captured[0]["text"], "Claude says hi")
        self.assertEqual(mock_run.call_args.kwargs["input"], b"Prompt body")
        self.assertEqual(mock_run.call_args.args[0], ["claude.cmd", "--print", "--tools", ""])

    def test_print_exec_handles_non_zero_exit_without_crashing(self):
        proc = type("Proc", (), {
            "returncode": 7,
            "stdout": b"",
            "stderr": b"boom",
        })()

        captured, _ = self._run_once(proc)

        self.assertEqual(captured[0]["channel"], "sandbox-flow-test")
        self.assertEqual(captured[0]["text"], "[claude --print failed (exit 7)]")

    def test_print_exec_handles_empty_success_without_crashing(self):
        proc = type("Proc", (), {
            "returncode": 0,
            "stdout": b"",
            "stderr": b"",
        })()

        captured, _ = self._run_once(proc)

        self.assertEqual(captured[0]["text"], "[claude produced no reply]")


class ClaudePrintExecInvariantTests(unittest.TestCase):
    def test_print_exec_is_known_run_mode(self):
        self.assertTrue(safety_invariants.check_run_mode_known("print_exec").ok)


if __name__ == "__main__":
    unittest.main()
