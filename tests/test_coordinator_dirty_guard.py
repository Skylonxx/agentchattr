"""Coordinator/session pre-worker dirty guard policy sync tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_loader
import workspace_policy as wp
import workspace_policy_runtime as wpr
from worker_workspace import run_workspace_precheck_structured

ROOT = Path(__file__).resolve().parents[1]
TWINPET = "C:/Users/Narachat/twinpet-pos"
EXPECTED_HEAD = "752ed1317a5e0b83b872d563cda451c7621ed22e"
ANALYSIS_PROFILE = "twinpet-ui-09-c-payment-modal-analysis"
WRITE_PROFILE = "twinpet-ui-09-c-payment-modal-write"
DOCS_DIRTY = " M Task.md\n M Context.md\n M docs/reports/latest-report.md\n"


def _profiles():
    return config_loader.get_workspace_profiles(config_loader.load_config(ROOT))


def _analysis_session(*, stale: bool = False, session_id: int = 1) -> dict:
    profiles = _profiles()
    result = wp.resolve_session_workspace_policy(
        profiles=profiles,
        start_body={
            "workspace_profile": ANALYSIS_PROFILE,
            "workspace_mode": "read-only-analysis",
            "expected_head": EXPECTED_HEAD,
        },
    )
    fields = wp.build_session_workspace_policy_fields(result.policy)
    session = {
        "id": session_id,
        "prompt_body": "PROMPT ID: TEST\n" + ("x" * 2000),
        "prompt_id": "TWINPET-UI-09-C-READONLY-ANALYSIS-BLUEPRINT-001",
        "goal": "short",
        **fields,
    }
    if stale:
        policy = dict(session["workspace_policy"])
        policy.pop("analysis_report_only", None)
        policy["mode"] = "docs-only"
        policy["write_files"] = []
        session["workspace_policy"] = policy
    return session


def _queue_item(
    session: dict,
    *,
    role: str,
    data_dir: Path,
) -> dict:
    (data_dir / "session_runs.json").write_text(json.dumps([session]), encoding="utf-8")
    ctx = wpr.build_session_queue_workspace_context(session, role, 0, 0)
    return {
        "prompt": "relay prompt",
        "channel": ANALYSIS_PROFILE,
        "relay_meta": {
            "kind": "session_turn",
            "session_id": session["id"],
            "phase": 0,
            "turn": 0,
            "role": role,
            "channel": ANALYSIS_PROFILE,
            "relay_mode": True,
            "disable_mcp": True,
        },
        "workspace_policy_context": ctx,
    }


def _wrapper_cfg() -> dict:
    return {
        "workspace_policy": {
            "runtime_enforcement_enabled": True,
            "read_only_external_cwd_enabled": True,
            "scoped_write_external_cwd_enabled": True,
        },
    }


class CoordinatorDirtyGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _coordinator_blocker(self, session: dict, *, porcelain: str) -> str | None:
        import wrapper

        item = _queue_item(session, role="coordinator", data_dir=self.data_dir)
        cfg = _wrapper_cfg()
        cwd = wrapper._resolve_exec_cwd(
            item, data_dir=self.data_dir, config=cfg, default_cwd=TWINPET,
        )
        with mock.patch.object(wrapper, "_git_porcelain_at_cwd", return_value=porcelain):
            with mock.patch.object(wpr, "git_head_at_cwd", return_value=EXPECTED_HEAD):
                return wrapper._workspace_dirty_blocker(
                    item,
                    data_dir=self.data_dir,
                    config=cfg,
                    effective_cwd=cwd,
                    when="pre",
                )

    def test_coordinator_allows_docs_only_dirty_for_analysis_profile(self):
        session = _analysis_session()
        blocker = self._coordinator_blocker(session, porcelain=DOCS_DIRTY)
        self.assertIsNone(blocker)

    def test_stale_session_without_analysis_flag_still_allows_docs_dirty(self):
        session = _analysis_session(stale=True)
        blocker = self._coordinator_blocker(session, porcelain=DOCS_DIRTY)
        self.assertIsNone(blocker)

    def test_coordinator_blocks_src_dirty(self):
        session = _analysis_session()
        porcelain = DOCS_DIRTY + " M src/components/PaymentModal.tsx\n"
        blocker = self._coordinator_blocker(session, porcelain=porcelain)
        self.assertIsNotNone(blocker)
        self.assertIn("BLOCKER:unauthorized_dirty_tree", blocker)
        self.assertIn("blocking_dirty=src/components/PaymentModal.tsx", blocker)

    def test_coordinator_blocks_tests_dirty(self):
        session = _analysis_session()
        porcelain = " M tests/pos-human-checkout.spec.ts\n"
        blocker = self._coordinator_blocker(session, porcelain=porcelain)
        self.assertIsNotNone(blocker)
        self.assertIn("tests/pos-human-checkout.spec.ts", blocker)

    def test_worker_and_coordinator_dirty_policy_agree_docs_only(self):
        session = _analysis_session()
        policy = session["workspace_policy"]
        worker = wpr.verify_dirty_set(
            porcelain_output=DOCS_DIRTY,
            policy=policy,
            profiles=_profiles(),
        )
        coordinator = self._coordinator_blocker(session, porcelain=DOCS_DIRTY)
        self.assertTrue(worker.ok)
        self.assertIsNone(coordinator)

    def test_worker_and_coordinator_dirty_policy_agree_src_block(self):
        session = _analysis_session()
        policy = session["workspace_policy"]
        porcelain = " M src/components/PaymentModal.tsx\n"
        worker = wpr.verify_dirty_set(
            porcelain_output=porcelain,
            policy=policy,
            profiles=_profiles(),
        )
        coordinator = self._coordinator_blocker(session, porcelain=porcelain)
        self.assertFalse(worker.ok)
        self.assertIsNotNone(coordinator)

    def test_blocker_includes_diagnostics(self):
        session = _analysis_session()
        porcelain = " M src/components/PaymentModal.tsx\n"
        blocker = self._coordinator_blocker(session, porcelain=porcelain)
        self.assertIn(f"workspace_profile={ANALYSIS_PROFILE}", blocker or "")
        self.assertIn("workspace_mode=read-only", blocker or "")
        self.assertIn("canonical_mode=read-only", blocker or "")
        self.assertIn("analysis_report_only=true", blocker or "")
        self.assertIn("guard_source=wrapper.pre_turn_dirty_guard", blocker or "")
        self.assertIn(f"git_commit={EXPECTED_HEAD}", blocker or "")

    def test_only_task_context_dirty_proceeds(self):
        session = _analysis_session()
        porcelain = " M Task.md\n M Context.md\n"
        self.assertIsNone(self._coordinator_blocker(session, porcelain=porcelain))

    def test_scoped_write_profile_still_blocks_unauthorized_dirty(self):
        profiles = _profiles()
        result = wp.resolve_session_workspace_policy(
            profiles=profiles,
            start_body={
                "workspace_profile": WRITE_PROFILE,
                "workspace_mode": "scoped-write",
                "expected_head": EXPECTED_HEAD,
            },
        )
        fields = wp.build_session_workspace_policy_fields(result.policy)
        session = {"id": 2, **fields}
        item = _queue_item(session, role="coordinator", data_dir=self.data_dir)
        import wrapper

        cfg = _wrapper_cfg()
        cwd = wrapper._resolve_exec_cwd(
            item, data_dir=self.data_dir, config=cfg, default_cwd=TWINPET,
        )
        with mock.patch.object(wrapper, "_git_porcelain_at_cwd", return_value=" M src/pages/POSPage.tsx\n"):
            blocker = wrapper._workspace_dirty_blocker(
                item,
                data_dir=self.data_dir,
                config=cfg,
                effective_cwd=cwd,
                when="pre",
            )
        self.assertIsNotNone(blocker)

    def test_generic_read_only_template_without_profile_remains_conservative(self):
        policy = wp.default_scratch_readonly_policy()
        result = wpr.verify_dirty_set(
            porcelain_output=" M Task.md\n",
            policy=policy,
        )
        self.assertFalse(result.ok)

    def test_porcelain_parser_regression(self):
        porcelain = " M Context.md\n M Task.md\n M docs/reports/latest-report.md\n"
        paths = [e["path"] for e in wpr.parse_git_porcelain(porcelain)]
        self.assertEqual(paths, ["Context.md", "Task.md", "docs/reports/latest-report.md"])
        self.assertNotIn("ontext.md", paths)

    def test_developer_precheck_reaches_worker_with_docs_dirty(self):
        session = _analysis_session()
        policy = session["workspace_policy"]
        with mock.patch(
            "worker_workspace.subprocess.run",
            side_effect=[
                mock.Mock(returncode=0, stdout=EXPECTED_HEAD + "\n"),
                mock.Mock(returncode=0, stdout=DOCS_DIRTY),
            ],
        ):
            pre = run_workspace_precheck_structured(
                TWINPET,
                expected_head=EXPECTED_HEAD,
                policy=policy,
            )
        self.assertTrue(pre.ok)
        self.assertIsNone(pre.blocker)


if __name__ == "__main__":
    unittest.main()
