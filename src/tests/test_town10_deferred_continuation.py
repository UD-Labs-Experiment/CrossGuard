"""Isolated controller tests; never write into retained study evidence."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tools.town10_deferred_continuation as dc


class TestTown10DeferredContinuation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.study_root = Path(self.temp.name) / "study"
        self.study_root.mkdir()
        self.workspace = Path(__file__).resolve().parents[2]
        self.runtime_source = "src/crossguard/simulation/city_world.py"
        self.evaluation_source = "src/tools/town10_continuation_results.py"
        self.catalogue = {
            "source_hashes": {self.runtime_source: dc.digest(self.workspace / self.runtime_source)},
            "evaluation_source_hashes": {self.evaluation_source: dc.digest(self.workspace / self.evaluation_source)},
            "tests": [
                {"test": f"Robustness_T01_{number:02d}", "theme": "T01",
                 "relative_path": f"Robustness/Test_{number:02d}", "seed": 41100 + number,
                 "weather": {"cloudiness": 10.0}}
                for number in range(1, 61)
            ],
        }
        self.write_catalogue()
        sentinel = self.study_root / "Validation/sealed_fixture.txt"
        sentinel.parent.mkdir()
        sentinel.write_text("Synthetic gate-bound evidence, not a recorded simulation.")
        sealed_files = {"Validation/sealed_fixture.txt": dc.digest(sentinel)}
        # The readiness checker requires 42 sealed media entries. These bytes
        # exercise its digest/count contract; they are not playable recordings.
        for number in range(42):
            relative = f"Validation/synthetic_media_{number:02d}.mp4"
            path = self.study_root / relative
            path.write_bytes(f"Synthetic fixture {number}; not a recorded video".encode())
            sealed_files[relative] = dc.digest(path)
        gate = {"passed": True, "catalogue_sha256": self.catalogue_sha256,
                "completed_runs": 28, "accepted_mode_pairs": 14}
        self.write_json("Reports/validation_gate.json", gate)
        self.write_json("Reports/validation_package_gate.json",
                        {**gate, "evidence_sha256": sealed_files})
        self.write_json("Reports/Robustness_Unattended/Repair/status.json", {"state": "DIAGNOSING"})
        for cfg in self.catalogue["tests"][:2]:
            for mode in dc.MODES:
                for state in dc.STATES:
                    if cfg["test"] == "Robustness_T01_01" or (mode, state) == ("Automatic", "OFF"):
                        self.write_json(f"{cfg['relative_path']}/{mode}/Assistance_{state}/fixture_completion.json",
                                        {"complete": True})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Avoid constructor signal changes and forbid any actual bridge/process operation.
        self.stack.enter_context(patch.object(dc.DeferredContinuationController, "_setup_signal_handlers"))
        self.post = self.stack.enter_context(patch.object(dc.requests, "post"))
        self.popen = self.stack.enter_context(patch.object(dc.subprocess, "Popen"))
        self.stack.enter_context(patch.object(dc, "verify_completed_evidence", side_effect=self.fixture_completion))

    def write_json(self, relative, value):
        path = self.study_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def write_catalogue(self):
        self.write_json("frozen_catalogue.json", self.catalogue)
        self.catalogue_sha256 = dc.digest(self.study_root / "frozen_catalogue.json")
        self.write_json("frozen_catalogue.sha256.json", {"sha256": self.catalogue_sha256})

    def fixture_completion(self, run_folder, cfg, mode, state, weather_log):
        # Deep physical-window validation has its own tests. This supplies that
        # boundary contract while testing the controller's real ledger separation.
        self.assertTrue(run_folder.resolve().is_relative_to(self.study_root.resolve()))
        path = run_folder / "fixture_completion.json"
        return (True, None) if path.exists() and dc.read(path)["complete"] else (False, "Not started")

    def controller(self, dry_run=True):
        controller = dc.DeferredContinuationController(self.study_root,
                        catalogue_sha256=self.catalogue_sha256, dry_run=dry_run)
        self.stack.enter_context(patch.object(controller, "get_bridge_status", return_value={"state": "stopped"}))
        self.stack.enter_context(patch.object(controller, "check_bridge_alive"))
        self.stack.enter_context(patch.object(controller, "sample_carla_weather_read_only",
                                              return_value={"frame": 100, "weather": {}}))
        return controller

    def test_preflight_blocks_when_runtime_source_modified(self):
        self.catalogue["source_hashes"][self.runtime_source] = "0" * 64
        self.write_catalogue()
        controller = self.controller()
        report = controller.execute_preflight_checks()
        self.assertFalse(report["all_preflight_checks_passed"])
        self.assertIn("city_world.py", report["runtime_source_error"])
        self.assertTrue(controller.preflight_report_path.is_file())
        self.post.assert_not_called()

    def test_preflight_blocks_changed_evaluation_fingerprint(self):
        self.catalogue["evaluation_source_hashes"][self.evaluation_source] = "0" * 64
        self.write_catalogue()
        report = self.controller().execute_preflight_checks()
        self.assertFalse(report["all_preflight_checks_passed"])
        self.assertIn(self.evaluation_source, report["evaluation_source_error"])
        self.post.assert_not_called()

    def test_preflight_blocks_changed_sealed_artifact(self):
        (self.study_root / "Validation/sealed_fixture.txt").write_text("changed")
        report = self.controller().execute_preflight_checks()
        self.assertFalse(report["all_preflight_checks_passed"])
        self.assertIn("validation_gates_error", report)
        self.post.assert_not_called()

    def test_dry_run_executes_no_bridge_commands(self):
        controller = self.controller()
        with patch.object(controller, "execute_window") as execute:
            controller.run()
        progress = dc.read(controller.progress_path)
        self.assertEqual(progress["status"], "DRY_RUN_VERIFIED")
        self.assertEqual(progress["planned_runs"], 240)
        self.assertEqual(progress["completed_runs"], 4)
        self.assertEqual(progress["deferred_runs"], 4)
        self.assertEqual(progress["independently_accepted_pairs"], 0)
        execute.assert_not_called()
        self.post.assert_not_called()
        self.popen.assert_not_called()

    def test_deferral_accounting(self):
        controller = self.controller()
        controller.run()
        deferred = [r for r in controller.deferred_runs_ledger if r["test"] == "Robustness_T01_02"]
        self.assertEqual(len(deferred), 4)
        off = next(r for r in deferred if (r["mode"], r["assistance"]) == ("Automatic", "OFF"))
        self.assertEqual(off["sub_status"], "COMPLETED_RETAINED")
        self.assertEqual(sum(r["sub_status"] == "DEFERRED_UNRESOLVED" for r in deferred), 3)
        self.assertFalse(any(r["test"] == "Robustness_T01_02" for r in controller.completed_runs_ledger))
        self.assertEqual(len(controller.completed_runs_ledger), 4)
        self.assertTrue(all(r["test"] == "Robustness_T01_01" for r in controller.completed_runs_ledger))

    def test_shared_weather_fault_safe_stop(self):
        controller = self.controller(dry_run=False)
        with patch.object(controller, "execute_window", side_effect=RuntimeError("SHARED_WEATHER_FAULT: Weather mismatch detected")) as execute:
            with patch.object(controller, "safe_stop") as stop:
                controller.run()
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0]["test"], "Robustness_T01_03")
        stop.assert_called_once()
        progress = dc.read(controller.progress_path)
        self.assertEqual(progress["status"], "BLOCKED_SHARED_WEATHER_FAULT")
        self.assertIn("SHARED_WEATHER_FAULT", progress["error"])
        self.assertEqual(progress["completed_runs"], 4)
        self.assertEqual(progress["deferred_runs"], 4)
        self.post.assert_not_called()
        self.popen.assert_not_called()

    def test_lock_prevents_competing_execution_controllers(self):
        first, second = self.controller(), self.controller()
        first.acquire_lock()
        try:
            with self.assertRaisesRegex(RuntimeError, "duplicate refused"):
                second.acquire_lock()
        finally:
            first.release_lock()

    def test_repair_agent_status_reading(self):
        report = self.controller().execute_preflight_checks()
        self.assertTrue(report["all_preflight_checks_passed"])
        self.assertEqual(report["repair_agent_status"]["state"], "DIAGNOSING")


if __name__ == "__main__":
    unittest.main()
