"""Regression tests for wrapper queue dispatch injection signatures."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper  # noqa: E402


class QueueInjectSignatureTests(unittest.TestCase):
    def test_helper_passes_channel_to_channel_aware_injector(self):
        received = []

        def inject_fn(text, channel=None):
            received.append((text, channel))

        wrapper._inject_with_supported_kwargs(
            inject_fn, "hello", channel="sandbox-flow-test")

        self.assertEqual(received, [("hello", "sandbox-flow-test")])

    def test_helper_omits_channel_for_legacy_prompt_only_injector(self):
        received = []

        def inject_fn(text):
            received.append(text)

        wrapper._inject_with_supported_kwargs(
            inject_fn, "hello", channel="sandbox-flow-test")

        self.assertEqual(received, ["hello"])

    def _run_watcher_once(self, inject_fn, was_called):
        with tempfile.TemporaryDirectory() as tmp:
            queue_file = Path(tmp) / "claude_queue.jsonl"
            queue_file.write_text(
                json.dumps({
                    "channel": "sandbox-flow-test",
                    "prompt": "Line one\nLine two",
                }) + "\n",
                encoding="utf-8",
            )

            def identity():
                return "claude", queue_file

            with patch.object(wrapper, "_fetch_role", return_value=""), \
                    patch.object(wrapper, "_fetch_active_rules", return_value=None), \
                    patch.object(wrapper, "_report_rule_sync", return_value=None):
                t = threading.Thread(
                    target=wrapper._queue_watcher,
                    args=(identity, inject_fn),
                    kwargs={
                        "agent_name": "claude",
                        "server_port": 8300,
                        "refresh_interval": 10,
                        "suppress_identity_hint": False,
                    },
                    daemon=True,
                )
                t.start()

                deadline = time.time() + 4
                while time.time() < deadline and not was_called():
                    time.sleep(0.05)

    def test_queue_watcher_channel_aware_injector_receives_channel(self):
        called = []

        def inject_fn(text, channel=None):
            called.append({"text": text, "channel": channel})

        self._run_watcher_once(inject_fn, lambda: bool(called))

        self.assertTrue(called, "queue watcher did not dispatch prompt")
        self.assertEqual(called[0]["channel"], "sandbox-flow-test")
        self.assertEqual(called[0]["text"], "Line one Line two")

    def test_queue_watcher_legacy_prompt_only_injector_does_not_type_error(self):
        called = []

        def inject_fn(text):
            called.append(text)

        self._run_watcher_once(inject_fn, lambda: bool(called))

        self.assertEqual(called, ["Line one Line two"])


if __name__ == "__main__":
    unittest.main()
