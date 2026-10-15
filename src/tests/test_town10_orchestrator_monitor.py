"""Tests for CrossGuard Town10HD Standalone Orchestrator Monitor.

Verifies:
- NO CARLA imports or dependencies.
- Honest handling of missing / malformed progress files.
- Terminal safe-stop heartbeat freshness without claiming running batch.
- Dead / stale supervisor process detection.
- Duplicate monitor lock refusal based on PID + creation time.
- Real occupied port refusal via SO_EXCLUSIVEADDRUSE without killing other processes.
- Honest separate monitor vs batch liveness without simulation controls.
- API and HTML status endpoints with escaped output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request

import tools.town10_orchestrator_monitor as mon


class TestTown10OrchestratorMonitor(unittest.TestCase):

    def test_no_carla_imports(self):
        """Verify orchestrator monitor and its dependencies never import CARLA."""
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from tools import town10_orchestrator_monitor; assert 'carla' not in sys.modules",
            ],
            check=True,
        )
        src_text = Path(mon.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import carla", src_text)
        self.assertNotIn("from carla", src_text)

    def test_missing_progress_file(self):
        """Verify monitor honestly reports MISSING_PROGRESS and writes fresh heartbeat."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            monitor = mon.Town10OrchestratorMonitor(study_root=root, poll_interval_s=0.1)
            state = monitor.run_poll_cycle()

            self.assertEqual(state["batch_status"], "MISSING_PROGRESS")
            self.assertFalse(state["liveness"]["batch_alive"])
            self.assertIn("progress.json file is missing", state["action_required"])
            self.assertTrue(monitor.heartbeat_file.is_file())

            heartbeat = mon.read_json_safe(monitor.heartbeat_file)
            self.assertIsNotNone(heartbeat)
            self.assertEqual(heartbeat["batch_status"], "MISSING_PROGRESS")
            self.assertTrue(heartbeat["liveness"]["monitor_alive"])
            self.assertFalse(heartbeat["liveness"]["batch_alive"])

    def test_malformed_progress_file(self):
        """Verify monitor honestly reports MALFORMED_PROGRESS on invalid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prog_path = root / "Reports" / "Robustness_Unattended" / "progress.json"
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            prog_path.write_text("{ incomplete_json: ", encoding="utf-8")

            monitor = mon.Town10OrchestratorMonitor(study_root=root, poll_interval_s=0.1)
            state = monitor.run_poll_cycle()

            self.assertEqual(state["batch_status"], "MALFORMED_PROGRESS")
            self.assertFalse(state["liveness"]["batch_alive"])
            self.assertIn("progress.json file is malformed", state["action_required"])

    def test_terminal_safestop_heartbeat_remains_fresh(self):
        """Verify monitor heartbeat updates timestamps across cycles while batch stays FAILED_SAFE_STOP."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prog_path = root / "Reports" / "Robustness_Unattended" / "progress.json"
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            prog_path.write_text(
                json.dumps({
                    "status": "FAILED_SAFE_STOP",
                    "updated_utc": "2026-10-10T14:24:53.262724+00:00",
                    "planned_runs": 240,
                    "completed_runs": 5,
                    "qualified_pairs": 2,
                    "independently_accepted_pairs": 2,
                    "failed_attempts": 1,
                    "supervisor_pid": 27996,
                    "error": "Weather mismatch detected: [expected cloud10/sun70/az180 vs actual 20/45/220]",
                }),
                encoding="utf-8",
            )

            monitor = mon.Town10OrchestratorMonitor(study_root=root, poll_interval_s=0.1)
            state1 = monitor.run_poll_cycle()
            time.sleep(0.05)
            state2 = monitor.run_poll_cycle()

            self.assertEqual(state1["batch_status"], "FAILED_SAFE_STOP")
            self.assertEqual(state2["batch_status"], "FAILED_SAFE_STOP")
            self.assertIn("REPAIR_REQUIRED", state1["action_required"])
            self.assertIn("REPAIR_REQUIRED", state2["action_required"])
            self.assertEqual(state1["batch_last_update"], state2["batch_last_update"])
            # Heartbeat updated_utc must advance even when batch is stopped
            self.assertNotEqual(state1["updated_utc"], state2["updated_utc"])
            self.assertEqual(state1["counts"]["planned"], 240)
            self.assertEqual(state1["counts"]["recorded"], 5)
            self.assertEqual(state1["counts"]["qualified"], 2)
            self.assertEqual(state1["counts"]["independently_reviewed"], 2)
            self.assertEqual(state1["counts"]["failed"], 1)

    def test_dead_stale_process_identity(self):
        """Verify supervisor PID dead status is recognized and does not fabricate running state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prog_path = root / "Reports" / "Robustness_Unattended" / "progress.json"
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            # Pretend status is RUNNING but supervisor PID 999999 is dead
            prog_path.write_text(
                json.dumps({
                    "status": "RUNNING",
                    "supervisor_pid": 999999,
                    "updated_utc": "2026-10-10T14:00:00Z",
                }),
                encoding="utf-8",
            )

            with patch.object(mon, "is_pid_alive", return_value=False):
                monitor = mon.Town10OrchestratorMonitor(study_root=root, poll_interval_s=0.1)
                state = monitor.run_poll_cycle()

                self.assertEqual(state["batch_status"], "INTERRUPTED")
                self.assertFalse(state["liveness"]["batch_alive"])
                self.assertFalse(state["liveness"]["supervisor_alive"])
                self.assertIn("INVESTIGATION_REQUIRED", state["action_required"])

    def test_duplicate_lock_refusal(self):
        """Verify second monitor instance refuses to run when active lock exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "monitor.lock"
            my_pid = os.getpid()
            my_create = mon.get_process_create_time(my_pid) or time.time()

            lock1 = mon.MonitorLock(lock_path)
            lock1.acquire()
            self.assertTrue(lock1.acquired)
            self.assertTrue(lock_path.is_file())

            # Attempting to acquire from another MonitorLock instance
            lock2 = mon.MonitorLock(lock_path)
            with self.assertRaises(RuntimeError) as ctx:
                lock2.acquire()
            self.assertIn("Duplicate monitor detected", str(ctx.exception))

            # Releasing lock1 allows lock2 to acquire
            lock1.release()
            self.assertFalse(lock_path.is_file())
            lock2.acquire()
            self.assertTrue(lock2.acquired)
            lock2.release()

    def test_real_occupied_port_refusal(self):
        """Verify exclusive address use raises error on occupied port without killing process."""
        # Find an available port, bind it with socket, then try starting ExclusiveHTTPServer on it
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        occupied_port = s.getsockname()[1]

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                monitor = mon.Town10OrchestratorMonitor(
                    study_root=root,
                    port=occupied_port,
                    poll_interval_s=0.1,
                )
                with self.assertRaises(OSError):
                    monitor.start_http_server()
        finally:
            s.close()

    def test_honest_separate_monitor_and_batch_state(self):
        """Verify monitor is alive while batch is clearly stopped and no simulation control happens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prog_path = root / "Reports" / "Robustness_Unattended" / "progress.json"
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            prog_path.write_text(
                json.dumps({
                    "status": "FAILED_SAFE_STOP",
                    "supervisor_pid": 27996,
                    "bridge_pid": 2860,
                }),
                encoding="utf-8",
            )

            # Bridge PID 2860 alive, supervisor 27996 dead
            def fake_is_alive(pid):
                return pid == 2860

            with patch.object(mon, "is_pid_alive", side_effect=fake_is_alive):
                monitor = mon.Town10OrchestratorMonitor(study_root=root, poll_interval_s=0.1)
                state = monitor.run_poll_cycle()

                self.assertTrue(state["liveness"]["monitor_alive"])
                self.assertFalse(state["liveness"]["batch_alive"])
                self.assertFalse(state["liveness"]["supervisor_alive"])
                self.assertTrue(state["liveness"]["bridge_alive"])
                self.assertEqual(state["batch_status"], "FAILED_SAFE_STOP")
                self.assertEqual(state["monitor_status"], "ACTIVE")

    def test_endpoint_api_status_and_html(self):
        """Verify /api/status returns valid JSON and / returns escaped HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prog_path = root / "Reports" / "Robustness_Unattended" / "progress.json"
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            prog_path.write_text(
                json.dumps({
                    "status": "FAILED_SAFE_STOP",
                    "planned_runs": 240,
                    "completed_runs": 5,
                    "error": "<script>alert('xss')</script> Weather mismatch",
                }),
                encoding="utf-8",
            )

            # Pick free port
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.bind(("127.0.0.1", 0))
            test_port = test_sock.getsockname()[1]
            test_sock.close()

            monitor = mon.Town10OrchestratorMonitor(
                study_root=root,
                port=test_port,
                dashboard_port=8766,
                poll_interval_s=0.1,
            )
            monitor.run_poll_cycle()
            monitor.start_http_server()

            try:
                # Test /api/status
                url_api = f"http://127.0.0.1:{test_port}/api/status"
                with urllib.request.urlopen(url_api, timeout=2.0) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("application/json", resp.headers.get("Content-Type", ""))
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data["batch_status"], "FAILED_SAFE_STOP")
                    self.assertEqual(data["counts"]["planned"], 240)

                # Test / (HTML page)
                url_html = f"http://127.0.0.1:{test_port}/"
                with urllib.request.urlopen(url_html, timeout=2.0) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                    body = resp.read().decode("utf-8")
                    self.assertIn("CrossGuard Town10HD Orchestrator Monitor", body)
                    self.assertIn("8766", body)
                    # XSS attack vector in error must be escaped
                    self.assertNotIn("<script>alert('xss')</script>", body)
                    self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", body)

                # Test 404
                url_404 = f"http://127.0.0.1:{test_port}/nonexistent"
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url_404, timeout=2.0)
                self.assertEqual(ctx.exception.code, 404)

            finally:
                monitor.stop_http_server()


if __name__ == "__main__":
    unittest.main()
