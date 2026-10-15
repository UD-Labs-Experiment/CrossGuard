"""Unit tests for CrossGuard Town10HD Progress Dashboard.

Comprehensive coverage for:
  - Absolute zero CARLA imports.
  - Actual catalogue verification: 60 Robustness configs (240 runs) and 7 Validation configs (28 runs / 14 pairs).
  - Strict durable disk evidence verification:
      * 1200 timeline rows reaching 20s.
      * 201 camera frames with all six named views.
      * All 1206 image files existing.
      * Source identity consistency (source_trial_path matching terminal_status).
      * terminal_reason observation_window_complete (rejecting merely truthy terminal_metrics).
  - Rejection of premature independent acceptance:
      * Refusing scene_review booleans alone without protocol qualification and both runs complete.
      * Refusing when result_sha256 or evidence_sha256 bindings mismatch disk.
      * Validating accepted pair with genuine matching hash bindings.
  - Missing baseline gate handling: displays unknown / not passed, never defaults to True/28.
  - Real results schema contacts parsing (pedestrian, A_B, B_C).
  - Missing, partial, stale, and malformed status handling.
  - Security path traversal refusal and secret protection.
  - HTTP server handlers and clean port conflict handling.
"""
import datetime
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from http.server import HTTPServer
import urllib.request
import urllib.error

import sys
SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tools import town10_progress_dashboard as dash


def create_realistic_run_fixture(
    run_dir: Path,
    timeline_rows: int = 1200,
    end_time_s: float = 20.0,
    camera_sets: int = 201,
    image_count: int = 1206,
    terminal_end_reason: str = "observation_window_complete",
    results_terminal_reason: str = "observation_window_complete",
    source_trial: str = "C:/Trial/Path",
    term_artifacts: str = "C:/Trial/Path",
    seed: int = 41101,
    technical_failure: bool = False,
    include_all_views: bool = True,
    ped_contacts: list = None,
    secondary_contacts: dict = None,
):
    """Helper creating realistic multi-file run evidence on disk."""
    run_dir.mkdir(parents=True, exist_ok=True)
    pic_dir = run_dir / "assets" / "pictures"
    pic_dir.mkdir(parents=True, exist_ok=True)

    # 1. terminal_status.json
    term_data = {
        "state": "finished" if terminal_end_reason == "observation_window_complete" else "running",
        "finished": terminal_end_reason == "observation_window_complete",
        "trial_artifacts": term_artifacts,
        "terminal_metrics": {
            "end_reason": terminal_end_reason,
            "sim_time_s": end_time_s,
        }
    }
    (run_dir / "terminal_status.json").write_text(json.dumps(term_data), encoding="utf-8")

    # 2. requested_config.json
    req_data = {
        "seed": seed,
        "observation_window_s": 20.0,
    }
    (run_dir / "requested_config.json").write_text(json.dumps(req_data), encoding="utf-8")

    # 3. placement_audit.json
    (run_dir / "placement_audit.json").write_text(json.dumps({"audit": "passed"}), encoding="utf-8")

    # 4. results.json
    res_data = {
        "seed": seed,
        "mode": "autonomous",
        "assistance": False,
        "terminal_reason": results_terminal_reason,
        "technical_failure": technical_failure,
        "source_trial_path": source_trial,
        "pedestrian_contacts": ped_contacts if ped_contacts is not None else [],
        "secondary": secondary_contacts if secondary_contacts is not None else {
            "A_B": {"contact": False},
            "B_C": {"contact": False}
        }
    }
    (run_dir / "results.json").write_text(json.dumps(res_data), encoding="utf-8")

    # 5. physical_timeline.json
    dt = end_time_s / max(timeline_rows, 1)
    timeline = [{"frame": i, "time_s": i * dt} for i in range(timeline_rows)]
    (run_dir / "physical_timeline.json").write_text(json.dumps(timeline), encoding="utf-8")

    # 6. camera_manifest.json
    cam_manifest = []
    for i in range(camera_sets):
        if include_all_views:
            views_dict = {v: f"frame_{i:04d}_{v}.jpg" for v in dash.REQUIRED_VIEWS}
        else:
            views_dict = {"top": f"frame_{i:04d}_top.jpg"}
        cam_manifest.append({"frame": i, "views": views_dict})
    (run_dir / "camera_manifest.json").write_text(json.dumps(cam_manifest), encoding="utf-8")

    # 7. Images in assets/pictures
    for i in range(image_count):
        v_name = dash.REQUIRED_VIEWS[i % 6]
        f_idx = i // 6
        img_name = f"frame_{f_idx:04d}_{v_name}.jpg" if include_all_views else f"frame_{i:04d}_top.jpg"
        (pic_dir / img_name).write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 10)


class TestTown10ProgressDashboard(unittest.TestCase):

    def test_study_mode_label_maps_to_real_result_mode_without_accepting_wrong_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            create_realistic_run_fixture(run)
            results_path = run / 'results.json'
            results = json.loads(results_path.read_text())
            for label, actual, accepted in (
                ('Automatic', 'autonomous', True),
                ('Automatic', 'retrofit', False),
                ('Automatic', 'unknown', False),
                ('Automatic', None, False),
                ('Retrofit', 'retrofit', True),
                ('Retrofit', 'autonomous', False),
            ):
                results['mode'] = actual
                results_path.write_text(json.dumps(results))
                with self.subTest(label=label, actual=actual):
                    complete, evidence = dash.verify_run_disk_evidence(run, mode=label)
                    self.assertEqual(complete, accepted, evidence['reasons'])

    def test_no_carla_imports(self):
        """Verify dashboard module and its dependencies never import CARLA."""
        # Other simulation tests legitimately import CARLA in this interpreter.
        # Inspect a fresh dashboard-only process to prove import isolation.
        import subprocess
        subprocess.run(
            [sys.executable, "-c", "import sys; from tools import town10_progress_dashboard; assert 'carla' not in sys.modules"],
            check=True,
        )
        src_text = Path(dash.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import carla", src_text)
        self.assertNotIn("from carla", src_text)

    def test_actual_catalogue_counts(self):
        """Verify real workspace catalogue contains 60 Robustness configs (240 runs) and 7 Validation (28 runs / 14 pairs)."""
        workspace_root = Path(__file__).resolve().parents[2] / "Evidence" / "Study_02_Town10HD_Continuation" / "Revision_03"
        collector = dash.DashboardDataCollector(workspace_root)
        cat = collector.load_catalogue()

        self.assertEqual(cat["total_robustness_configs"], 60)
        self.assertEqual(len(cat["robustness_configs"]), 60)
        self.assertEqual(cat["total_validation_configs"], 7)
        self.assertEqual(len(cat["validation_configs"]), 7)
        baseline = collector.load_validation_baseline()
        self.assertEqual(baseline["completed_runs"], 28)
        self.assertEqual(baseline["qualified_pairs"], 14)
        self.assertEqual(baseline["accepted_pairs"], 14)
        self.assertIn(baseline["status"], ("VALIDATED_AND_FROZEN", "FAILED_GATE"))

    def test_actual_supervisor_status_detects_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / 'Reports/Robustness_Unattended/progress.json'
            progress.parent.mkdir(parents=True)
            for age, alive in ((10, False), (70, True)):
                progress.write_text(json.dumps({
                    'status': 'RUNNING_WINDOW', 'supervisor_pid': 12345,
                    'updated_utc': (dash.utc_now() - datetime.timedelta(seconds=age)).isoformat(),
                }))
                with patch.object(dash, 'is_pid_alive', return_value=alive):
                    data = dash.DashboardDataCollector(root).collect()
                self.assertTrue(data['is_stale'])
                if not alive:
                    self.assertEqual(data['status'], 'INTERRUPTED')

    def test_failed_preflight_counter_cannot_erase_durable_records(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = dash.DashboardDataCollector(Path(directory))
            progress = collector.progress_file
            progress.parent.mkdir(parents=True)
            progress.write_text(json.dumps({'status': 'BLOCKED_PREFLIGHT_FAILURE', 'completed_runs': 0}))
            disk = collector.scan_disk_robustness([])
            disk['completed_runs_count'] = 5
            with patch.object(collector, 'scan_disk_robustness', return_value=disk):
                self.assertEqual(collector.collect()['completed_runs'], 5)
            progress.write_text(json.dumps({'status': 'RUNNING', 'completed_runs': 240}))
            with patch.object(collector, 'scan_disk_robustness', return_value=disk):
                self.assertEqual(collector.collect()['completed_runs'], 5)

    def test_active_validation_is_separate_and_cannot_escape_study_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            collector = dash.DashboardDataCollector(base / 'Revision_03')
            marker = base / 'autopilot_active.json'
            marker.write_text(json.dumps({'revision': '../outside', 'phase': 'Validation'}))
            self.assertEqual(collector.load_active_validation()['status'], 'INVALID_ACTIVE_REVISION')
            marker.write_text(json.dumps({'revision': 'Revision_05', 'phase': 'Validation'}))
            progress = base / 'Revision_05/Reports/Revalidation_Unattended/progress.json'
            progress.parent.mkdir(parents=True)
            progress.write_text(json.dumps({'status': 'RUNNING_WINDOW', 'completed_runs': 2, 'supervisor_pid': 42}))
            with patch.object(dash, 'is_pid_alive', return_value=True):
                active = collector.load_active_validation()
            self.assertEqual(active['completed_runs'], 2)
            self.assertEqual(active['planned_runs'], 28)

    def test_bounded_offline_assessment_and_failure_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / 'Reports/Robustness_Unattended/progress.json'
            progress.parent.mkdir(parents=True)
            progress.write_text(json.dumps({
                'status': 'ASSESSING_CONFIGURATION', 'supervisor_pid': 12345,
                'updated_utc': (dash.utc_now() - datetime.timedelta(seconds=90)).isoformat(),
            }))
            with patch.object(dash, 'is_pid_alive', return_value=True):
                self.assertFalse(dash.DashboardDataCollector(root).collect()['is_stale'])
            with patch.object(dash, 'is_pid_alive', return_value=False):
                self.assertEqual(dash.DashboardDataCollector(root).collect()['status'], 'INTERRUPTED')
            progress.write_text(json.dumps({'status': 'FAILED_SAFE_STOP', 'error': 'Weather mismatch'}))
            self.assertEqual(dash.DashboardDataCollector(root).collect()['error'], 'Weather mismatch')

    def test_missing_baseline_gate_displays_unknown_not_passed(self):
        """Missing baseline gate must display unknown/not passed, never default true/28."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            collector = dash.DashboardDataCollector(root)
            baseline = collector.load_validation_baseline()

            self.assertFalse(baseline["passed"])
            self.assertIsNone(baseline["completed_runs"])
            self.assertIsNone(baseline["qualified_pairs"])
            self.assertIsNone(baseline["accepted_pairs"])
            self.assertEqual(baseline["status"], "MISSING_NOT_PASSED")

    def test_durable_complete_rigorous_success(self):
        """Complete run passes when all 7 rigorous conditions are satisfied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                timeline_rows=1200,
                end_time_s=20.001,
                camera_sets=201,
                image_count=1206,
                terminal_end_reason="observation_window_complete",
                results_terminal_reason="observation_window_complete",
            )
            complete, status = dash.verify_run_disk_evidence(run_dir, expected_cfg={"seed": 41101})
            self.assertTrue(complete)
            self.assertTrue(status["terminal_status"])
            self.assertTrue(status["requested_config"])
            self.assertTrue(status["placement_audit"])
            self.assertTrue(status["physical_timeline"])
            self.assertTrue(status["camera_manifest"])
            self.assertTrue(status["images_coverage"])
            self.assertTrue(status["results"])
            self.assertTrue(status["source_identity_consistent"])
            self.assertFalse(status["technical_failure"])

    def test_durable_complete_rejects_merely_truthy_terminal_metrics(self):
        """terminal_status with non-complete end_reason is rejected even if metrics dict is truthy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                terminal_end_reason="vehicle_left_route",  # Not observation_window_complete
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["terminal_status"])

    def test_durable_complete_rejects_timeline_under_1200(self):
        """physical_timeline with 1199 rows is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                timeline_rows=1199,
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["physical_timeline"])

    def test_durable_complete_rejects_timeline_under_20_seconds(self):
        """physical_timeline not reaching 20s is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                timeline_rows=1200,
                end_time_s=15.0,  # Stopped early
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["physical_timeline"])

    def test_durable_complete_rejects_camera_sets_under_201(self):
        """camera_manifest with 200 frames is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                camera_sets=200,
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["camera_manifest"])

    def test_durable_complete_rejects_missing_named_views(self):
        """camera_manifest without all six named views is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                include_all_views=False,  # only top view
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["camera_manifest"])

    def test_durable_complete_rejects_images_under_1206(self):
        """Run with only 1205 images is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                image_count=1205,
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["images_coverage"])

    def test_durable_complete_rejects_source_trial_mismatch(self):
        """results.json source_trial_path mismatching terminal_status is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                source_trial="C:/Different/Path/trial_002",
                term_artifacts="C:/Trial/Path/trial_001",
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertFalse(complete)
            self.assertFalse(status["source_identity_consistent"])

    def test_contacts_extracted_from_actual_schema(self):
        """Contacts are accurately extracted according to results.json schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "Run"
            create_realistic_run_fixture(
                run_dir=run_dir,
                ped_contacts=[{"frame": 500, "impulse": 120.0}],
                secondary_contacts={
                    "A_B": {"contact": True, "minimum_physical_bumper_gap_m": -0.05},
                    "B_C": {"contact": False, "minimum_physical_bumper_gap_m": 1.5}
                }
            )
            complete, status = dash.verify_run_disk_evidence(run_dir)
            self.assertTrue(status["contacts"]["pedestrian"])
            self.assertTrue(status["contacts"]["A_B"])
            self.assertFalse(status["contacts"]["B_C"])

    def test_independent_acceptance_requires_protocol_and_hash_bindings(self):
        """Never call independently accepted just from scene_review booleans: require protocol qualification and hash bindings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "Test_01"
            mode = "Automatic"

            # Create OFF and ON complete runs
            off_dir = cfg_dir / mode / "Assistance_OFF"
            on_dir = cfg_dir / mode / "Assistance_ON"
            create_realistic_run_fixture(off_dir, terminal_end_reason="observation_window_complete")
            create_realistic_run_fixture(on_dir, terminal_end_reason="observation_window_complete")

            # 1. Unqualified protocol -> rejected
            (cfg_dir / "protocol_assessment.json").write_text(json.dumps({
                mode: {"qualified": False}
            }), encoding="utf-8")

            # scene_review with booleans all True
            review_path = cfg_dir / mode / "scene_review.json"
            review_data = {
                "actual_rgb_inspected": True,
                "native_markings_visible": True,
                "pedestrian_access_verified": True,
                "route_verified": True,
                "useful_occlusion_verified": True,
                "physical_gaps_verified": True,
                "result_sha256": {
                    "OFF": dash.file_sha256(off_dir / "results.json"),
                    "ON": dash.file_sha256(on_dir / "results.json"),
                },
                "evidence_sha256": {
                    "OFF": {"results.json": dash.file_sha256(off_dir / "results.json")},
                    "ON": {"results.json": dash.file_sha256(on_dir / "results.json")},
                }
            }
            review_path.write_text(json.dumps(review_data), encoding="utf-8")

            accepted, reason = dash.verify_pair_acceptance(cfg_dir, mode, off_complete=True, on_complete=True)
            self.assertFalse(accepted)
            self.assertIn("not qualified", reason)

            # 2. Qualified protocol, but hash mismatch -> rejected
            (cfg_dir / "protocol_assessment.json").write_text(json.dumps({
                mode: {"qualified": True}
            }), encoding="utf-8")

            corrupt_review = dict(review_data)
            corrupt_review["result_sha256"] = {"OFF": "wrong_hash_123", "ON": "wrong_hash_456"}
            review_path.write_text(json.dumps(corrupt_review), encoding="utf-8")

            accepted, reason = dash.verify_pair_acceptance(cfg_dir, mode, off_complete=True, on_complete=True)
            self.assertFalse(accepted)
            self.assertIn("mismatch", reason)

            # 3. Correct matching hashes + qualified protocol -> Accepted!
            review_path.write_text(json.dumps(review_data), encoding="utf-8")
            accepted, reason = dash.verify_pair_acceptance(cfg_dir, mode, off_complete=True, on_complete=True)
            self.assertTrue(accepted)
            self.assertIn("Independently accepted", reason)

    def test_path_traversal_refusal_and_security_sandbox(self):
        """Restricted endpoint refuses directory traversal, absolute paths outside root, and sensitive files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            evidence_sub = root / "Robustness" / "Test_01"
            evidence_sub.mkdir(parents=True)
            safe_file = evidence_sub / "results.json"
            safe_file.write_text("{}", encoding="utf-8")

            self.assertEqual(dash.safe_resolve_evidence_path(root, "Robustness/Test_01/results.json"), safe_file)
            self.assertIsNone(dash.safe_resolve_evidence_path(root, "../outside.json"))
            self.assertIsNone(dash.safe_resolve_evidence_path(root, "Robustness/../../outside.json"))

            # Disallowed extensions
            py_file = evidence_sub / "script.py"
            py_file.write_text("print(1)", encoding="utf-8")
            self.assertIsNone(dash.safe_resolve_evidence_path(root, "Robustness/Test_01/script.py"))

            # Sensitive filenames
            env_file = root / ".env"
            env_file.write_text("SECRET=123", encoding="utf-8")
            self.assertIsNone(dash.safe_resolve_evidence_path(root, ".env"))

    def test_http_server_endpoints(self):
        """Test GET /, GET /api/status, GET /evidence/..., and traversal rejection over HTTP."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            run_dir = root / "Robustness" / "Test_01"
            run_dir.mkdir(parents=True)
            (run_dir / "results.json").write_text('{"status": "ok"}', encoding="utf-8")

            collector = dash.DashboardDataCollector(root)

            class TestHandler(dash.DashboardRequestHandler):
                pass

            TestHandler.collector = collector
            TestHandler.evidence_root = root

            server = HTTPServer(("127.0.0.1", 0), TestHandler)
            port = server.server_address[1]
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.daemon = True
            server_thread.start()

            base_url = f"http://127.0.0.1:{port}"

            try:
                with urllib.request.urlopen(f"{base_url}/") as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("CrossGuard Town10HD Robustness Dashboard", html)

                with urllib.request.urlopen(f"{base_url}/api/status") as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data["phase"], "Robustness")
                    self.assertEqual(data["planned_runs"], 240)

                with urllib.request.urlopen(f"{base_url}/evidence/Robustness/Test_01/results.json") as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body["status"], "ok")

                req_traversal = urllib.request.Request(f"{base_url}/evidence/../../secret.json")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req_traversal)
                self.assertEqual(ctx.exception.code, 403)

            finally:
                server.shutdown()
                server.server_close()

    def test_unchanged_repeated_polls_reuse_verifier(self):
        """Repeated polls of unchanged run evidence reuse cached verification without re-verifying."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "Run_01"
            create_realistic_run_fixture(run_dir, timeline_rows=1200, end_time_s=20.001)

            collector = dash.DashboardDataCollector(root)
            cfg = {"seed": 41101}

            # First poll: cache is empty, calls verify_run_disk_evidence
            complete1, ev1 = collector.verify_run_cached(run_dir, expected_cfg=cfg, mode="Automatic", state="OFF")
            self.assertTrue(complete1)

            # Second poll: unchanged files -> must reuse cache without calling verify_run_disk_evidence
            with patch.object(dash, "verify_run_disk_evidence", wraps=dash.verify_run_disk_evidence) as mock_verifier:
                complete2, ev2 = collector.verify_run_cached(run_dir, expected_cfg=cfg, mode="Automatic", state="OFF")
                self.assertTrue(complete2)
                self.assertEqual(mock_verifier.call_count, 0)
                self.assertEqual(ev1, ev2)

    def test_changed_artifacts_invalidate_and_reject(self):
        """Modifying an evidence artifact invalidates cache and triggers full re-verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "Run_01"
            create_realistic_run_fixture(run_dir, timeline_rows=1200, end_time_s=20.001)

            collector = dash.DashboardDataCollector(root)
            cfg = {"seed": 41101}

            # Initial valid state
            c1, _ = collector.verify_run_cached(run_dir, expected_cfg=cfg, mode="Automatic", state="OFF")
            self.assertTrue(c1)

            # Invalidate by corrupting timeline rows (1199 instead of 1200)
            time.sleep(0.01)  # Ensure filesystem timestamp tick
            short_timeline = [{"frame": i, "time_s": i * 0.016} for i in range(1199)]
            (run_dir / "physical_timeline.json").write_text(json.dumps(short_timeline), encoding="utf-8")

            with patch.object(dash, "verify_run_disk_evidence", wraps=dash.verify_run_disk_evidence) as mock_verifier:
                c2, ev2 = collector.verify_run_cached(run_dir, expected_cfg=cfg, mode="Automatic", state="OFF")
                self.assertFalse(c2)
                self.assertEqual(mock_verifier.call_count, 1)
                self.assertFalse(ev2["physical_timeline"])

            # Invalidate by deleting an image in assets/pictures (changes directory mtime)
            time.sleep(0.01)
            pic_files = list((run_dir / "assets" / "pictures").glob("*.jpg"))
            self.assertTrue(len(pic_files) > 0)
            pic_files[0].unlink()

            with patch.object(dash, "verify_run_disk_evidence", wraps=dash.verify_run_disk_evidence) as mock_verifier:
                c3, ev3 = collector.verify_run_cached(run_dir, expected_cfg=cfg, mode="Automatic", state="OFF")
                self.assertFalse(c3)
                self.assertEqual(mock_verifier.call_count, 1)
                self.assertFalse(ev3["images_coverage"])

    def test_verifier_cache_bounded_to_240_runs(self):
        """Collector cache does not grow unbounded beyond 240 runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            collector = dash.DashboardDataCollector(root)

            # Populate cache with 250 dummy run paths
            for i in range(250):
                dummy_dir = root / f"Dummy_{i}"
                dummy_dir.mkdir(parents=True, exist_ok=True)
                collector.verify_run_cached(dummy_dir)

            self.assertLessEqual(len(collector._run_cache), 240)

    def test_port_conflict_clean_failure(self):
        """Port conflict fails cleanly with clear message and sys.exit(1), no process kill."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.object(dash, "HTTPServer", side_effect=OSError(errno.EADDRINUSE, "Address already in use")):
                with self.assertRaises(SystemExit) as ctx:
                    dash.run_dashboard_server("127.0.0.1", 8766, root)
                self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
