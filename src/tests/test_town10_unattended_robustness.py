"""Unit tests for the fail-closed detached Robustness supervisor.
Verifies duplicate locks, partial evidence refusal, changed gates/hashes,
weather mismatch safety, process ownership, exit failure, progress denominator,
and error handling without real bridge requests or simulations.
"""
import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

# Ensure imports resolve from src and src/tools
import tools.town10_unattended_robustness as rob


class RobustnessSupervisorUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.reports_dir = self.root / 'Reports'
        self.reports_dir.mkdir(parents=True)
        self.unattended_dir = self.reports_dir / 'Robustness_Unattended'
        self.unattended_dir.mkdir(parents=True)

        self.mock_args = argparse.Namespace(
            root=self.root,
            catalogue_sha256='test_catalogue_sha',
            bridge_url='http://127.0.0.1:8007',
            bridge_pid=9999,
            bridge_created=1000.0,
            agy='C:\\fake\\agy.exe',
            resume=False,
            dry_run=False
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_duplicate_lock_refusal(self):
        """Supervisor must refuse duplicate run if existing lock process is alive."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)
        lock_file = supervisor.lock_path
        # Create existing lock with current process PID and create_time
        curr_pid = os.getpid()
        curr_created = psutil.Process(curr_pid).create_time()
        lock_file.write_text(json.dumps({'pid': curr_pid, 'created': curr_created}))

        with self.assertRaisesRegex(RuntimeError, 'duplicate refused'):
            supervisor.acquire_lock()

    def test_stale_lock_requires_resume(self):
        """Stale lock from exited process requires explicit --resume switch."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)
        lock_file = supervisor.lock_path
        # Dead PID
        lock_file.write_text(json.dumps({'pid': 99999999, 'created': 123.45}))

        with self.assertRaisesRegex(RuntimeError, 'explicit --resume required'):
            supervisor.acquire_lock()

        # With resume switch, stale lock is cleared and acquired
        supervisor.args.resume = True
        supervisor.acquire_lock()
        self.assertTrue(lock_file.exists())
        acquired_data = json.loads(lock_file.read_text())
        self.assertEqual(acquired_data['pid'], os.getpid())

    def test_partial_evidence_blocks_execution(self):
        """Partial or incomplete artifacts must block execution; no silent overwrite."""
        cfg = {
            'test': 'Robustness_T01_01',
            'seed': 51101,
            'relative_path': 'Robustness/Tests/Theme_01/Test_01',
            'weather': {'cloudiness': 10.0}
        }
        run_folder = self.root / cfg['relative_path'] / 'Automatic/Assistance_OFF'
        run_folder.mkdir(parents=True)

        # 1. No files at all -> Not started
        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertEqual(reason, 'Not started')

        # 2. Only requested_config.json exists -> Partial attempt
        (run_folder / 'requested_config.json').write_text(json.dumps({'seed': 51101}))
        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertIn('Partial uncompleted artifacts', reason)

        # 3. Technical failure file exists -> Retained technical failure
        (run_folder / 'technical_failure.json').write_text(json.dumps({'error': 'crash'}))
        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertIn('Retained technical failure', reason)

    def test_missing_weather_log_blocks_resume(self):
        cfg = {'test': 'Robustness_T01_01', 'seed': 51101}
        run = self.root / 'run'
        run.mkdir()
        (run / 'results.json').write_text(json.dumps({
            'technical_failure': False, 'terminal_reason': 'observation_window_complete',
            'mode': 'autonomous', 'assistance': False, 'seed': 51101,
        }))
        ok, reason = rob.verify_completed_evidence(run, cfg, 'Automatic', 'OFF', self.root / 'missing.jsonl')
        self.assertFalse(ok)
        self.assertIn('Missing retained read-only weather proof', reason)

    def test_mode_and_terminal_reason_verification(self):
        """results.json mode must be 'autonomous' for Automatic and terminal_reason must be complete."""
        cfg = {
            'test': 'Robustness_T01_01',
            'seed': 51101,
            'relative_path': 'Robustness/Tests/Theme_01/Test_01',
            'weather': {'cloudiness': 10.0},
            'followers_automatic': {'follower_b': {'gap_m': 7.0}}
        }
        run_folder = self.root / cfg['relative_path'] / 'Automatic/Assistance_OFF'
        run_folder.mkdir(parents=True)

        # Invalid terminal reason
        bad_results = {
            'technical_failure': False,
            'terminal_reason': 'interrupted',
            'mode': 'autonomous',
            'assistance': False,
            'seed': 51101
        }
        (run_folder / 'results.json').write_text(json.dumps(bad_results))
        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertIn('terminal_reason', reason)

        # Mode string wrongly 'Automatic' instead of 'autonomous'
        bad_results['terminal_reason'] = 'observation_window_complete'
        bad_results['mode'] = 'Automatic'
        (run_folder / 'results.json').write_text(json.dumps(bad_results))
        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertIn('Mode mismatch', reason)

    def test_camera_manifest_and_six_views_coverage(self):
        """Camera manifest must contain 201 entries with all 6 views and matching files."""
        cfg = {
            'test': 'Robustness_T01_01',
            'seed': 51101,
            'relative_path': 'Robustness/Tests/Theme_01/Test_01',
            'weather': {'cloudiness': 10.0},
            'followers_automatic': {'follower_b': {'gap_m': 7.0}}
        }
        run_folder = self.root / cfg['relative_path'] / 'Automatic/Assistance_OFF'
        run_folder.mkdir(parents=True)
        pic_folder = run_folder / 'assets/pictures'
        pic_folder.mkdir(parents=True)

        # Setup valid results, requested_config, terminal_status, physical_timeline
        (run_folder / 'results.json').write_text(json.dumps({
            'technical_failure': False,
            'terminal_reason': 'observation_window_complete',
            'mode': 'autonomous',
            'assistance': False,
            'seed': 51101,
            'requested_config': {'weather': {'cloudiness': 10.0}, 'followers': {'follower_b': {'gap_m': 7.0}}},
            'source_trial_path': 'trial_123'
        }))
        (run_folder / 'requested_config.json').write_text(json.dumps({
            'receiver_mode': 'autonomous',
            'enabled': False,
            'seed': 51101,
            'study_options': {'weather': {'cloudiness': 10.0}, 'followers': {'follower_b': {'gap_m': 7.0}}}
        }))
        (run_folder / 'terminal_status.json').write_text(json.dumps({
            'trial_artifacts': 'trial_123'
        }))
        timeline_rows = [{'frame': 100 + i, 'time_s': i * (20.0 / 1199)} for i in range(1200)]
        (run_folder / 'physical_timeline.json').write_text(json.dumps(timeline_rows))

        # Camera manifest with entry missing a named view
        manifest = [{
            'frame': 100 + i * 6,
            'views': {v: f'{i}_{v}.jpg' for v in ('top', 'driver', 'person', 'opposite', 'driver_b')}  # missing driver_c
        } for i in range(201)]
        (run_folder / 'camera_manifest.json').write_text(json.dumps(manifest))

        ok, reason = rob.verify_completed_evidence(run_folder, cfg, 'Automatic', 'OFF')
        self.assertFalse(ok)
        self.assertIn('missing named view driver_c', reason)

    def test_gate_and_hash_failure(self):
        """Supervisor refuses launch if validation gates fail or file hashes differ."""
        val_gate = self.reports_dir / 'validation_gate.json'
        pkg_gate = self.reports_dir / 'validation_package_gate.json'

        # Missing gates
        with self.assertRaisesRegex(RuntimeError, 'Missing validation_gate.json'):
            rob.check_readiness_gates(self.root, 'expected_hash')

        # Gate passed is False
        val_gate.write_text(json.dumps({'passed': False, 'catalogue_sha256': 'expected_hash'}))
        pkg_gate.write_text(json.dumps({'passed': True, 'catalogue_sha256': 'expected_hash'}))
        with self.assertRaisesRegex(RuntimeError, 'validation_gate.json passed is not True'):
            rob.check_readiness_gates(self.root, 'expected_hash')

        # Sealed file digest mismatch
        val_gate.write_text(json.dumps({
            'passed': True,
            'catalogue_sha256': 'expected_hash',
            'completed_runs': 28,
            'accepted_mode_pairs': 14
        }))
        dummy_file = self.root / 'dummy.txt'
        dummy_file.write_text('actual_content')
        pkg_gate.write_text(json.dumps({
            'passed': True,
            'catalogue_sha256': 'expected_hash',
            'evidence_sha256': {'dummy.txt': 'wrong_hash'},
            'note': 'test'
        }))
        with self.assertRaisesRegex(RuntimeError, 'Sealed file digest mismatch'):
            rob.check_readiness_gates(self.root, 'expected_hash')

    def test_bridge_ownership_and_inaccessible_listener(self):
        """check_bridge_alive refuses when listener port is absent or owned by wrong process."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)

        with patch('tools.town10_unattended_robustness.same_process', return_value=True):
            # No listener connection on 8007
            with patch('psutil.net_connections', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'Port 8007 listener is absent'):
                    supervisor.check_bridge_alive()

            # Listener owned by different PID
            bad_conn = Mock()
            bad_conn.laddr = Mock(port=8007)
            bad_conn.status = psutil.CONN_LISTEN
            bad_conn.pid = 1111
            with patch('psutil.net_connections', return_value=[bad_conn]):
                with self.assertRaisesRegex(RuntimeError, 'unexpected PID 1111'):
                    supervisor.check_bridge_alive()

    def test_safe_stop_behavior(self):
        """Safe stop submits stop queue only if worker was launched and bridge is valid."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)

        # 1. No worker launched -> nothing called
        with patch('requests.post') as mock_post:
            supervisor.safe_stop()
            mock_post.assert_not_called()

        # 2. Worker launched -> posts stop to queue and confirms stopped
        supervisor.worker = Mock()
        with patch.object(supervisor, 'check_bridge_alive', return_value=None):
            with patch.object(supervisor, 'get_bridge_status', return_value={'state': 'stopped'}):
                with patch('requests.post') as mock_post:
                    mock_resp = Mock()
                    mock_resp.raise_for_status = Mock()
                    mock_post.return_value = mock_resp
                    supervisor.safe_stop()
                    mock_post.assert_called_once_with(
                        'http://127.0.0.1:8007/api/command',
                        json={'action': 'stop', 'params': {}},
                        timeout=30
                    )

    def test_weather_mismatch_detection(self):
        """Read-only weather check detects deviation greater than 0.01 tolerance."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)
        cfg = {
            'weather': {
                'cloudiness': 100.0,
                'precipitation': 65.0,
                'wetness': 90.0
            }
        }

        # Mock CARLA weather with precipitation mismatch (e.g. 0.0 instead of 65.0)
        mock_weather = Mock(cloudiness=100.0, precipitation=0.0, wetness=90.0)
        mock_snapshot = Mock(frame=500000)
        mock_snapshot.timestamp.elapsed_seconds = 10.0
        mock_world = Mock()
        mock_world.get_weather.return_value = mock_weather
        mock_world.get_snapshot.return_value = mock_snapshot

        with patch.object(supervisor, 'get_carla_world', return_value=mock_world):
            ok, frame, mismatches = supervisor.check_effective_weather(cfg, 'test_run', 5.0)
            self.assertFalse(ok)
            self.assertEqual(frame, 500000)
            self.assertTrue(any('precipitation' in m for m in mismatches))

    def test_progress_denominator_and_atomic_save(self):
        """Progress file must always specify planned_runs=240 and save atomically."""
        supervisor = rob.RobustnessSupervisor(self.mock_args)
        supervisor.save_progress('TESTING_PHASE')

        self.assertTrue(supervisor.progress_path.exists())
        data = json.loads(supervisor.progress_path.read_text())
        self.assertEqual(data['phase'], 'Robustness')
        self.assertEqual(data['planned_runs'], 240)
        self.assertEqual(data['status'], 'TESTING_PHASE')
        self.assertIn('completed_runs', data)
        self.assertIn('elapsed_s', data)


if __name__ == '__main__':
    unittest.main()
