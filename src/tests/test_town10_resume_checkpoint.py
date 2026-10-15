"""Checkpoint file contention and preservation of verified resume history."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.town10_unattended_robustness as rob


class ResumeCheckpointTests(unittest.TestCase):
    def test_transient_windows_replace_denial_retries_without_erasing_old_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            path.write_text('{"completed_runs":74}')
            original_replace = Path.replace
            calls = []
            def replace(temporary, destination):
                calls.append(1)
                if len(calls) < 3:
                    self.assertEqual(json.loads(path.read_text())['completed_runs'], 74)
                    error = PermissionError('Windows file busy')
                    error.winerror = 5
                    raise error
                return original_replace(temporary, destination)
            with patch.object(Path, 'replace', replace), patch.object(rob.time, 'sleep'):
                rob.atomic_json(path, {'completed_runs':75})
            self.assertEqual(len(calls), 3)
            self.assertEqual(json.loads(path.read_text())['completed_runs'], 75)

    def test_persistent_denial_is_bounded_and_preserves_last_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            path.write_text('{"completed_runs":74}')
            error = PermissionError('Windows file busy')
            error.winerror = 32
            with patch.object(Path, 'replace', side_effect=error) as replacement, patch.object(rob.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    rob.atomic_json(path, {'completed_runs':75})
            self.assertEqual(replacement.call_count, 8)
            self.assertEqual(json.loads(path.read_text())['completed_runs'], 74)

    def test_resume_preserves_completion_timestamps_failure_count_and_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / 'Reports/Robustness_Unattended'
            reports.mkdir(parents=True)
            record = {'test':'Robustness_T01_01','mode':'Automatic','assistance':'OFF',
                      'status':'COMPLETED','started_utc':'2026-10-10T17:18:04+00:00',
                      'completed_utc':'2026-10-10T17:20:41+00:00'}
            (reports / 'progress.json').write_text(json.dumps({'phase':'Robustness',
                'catalogue_sha256':'frozen','runs':[record], 'failed_attempts':1, 'elapsed_s':13708.86}))
            args = argparse.Namespace(root=root, bridge_url='http://127.0.0.1:8007',
                                      resume=True, catalogue_sha256='frozen')
            with patch.object(rob.time, 'monotonic', return_value=20000):
                supervisor = rob.RobustnessSupervisor(args)
            self.assertEqual(supervisor.previous_runs[('Robustness_T01_01','Automatic','OFF')], record)
            self.assertEqual(supervisor.failed_attempts, 1)
            self.assertAlmostEqual(supervisor.start_monotonic, 20000-13708.86)
            self.assertEqual(supervisor.durations, [157])
            # Saved history is not accepted without later physical evidence verification.
            self.assertEqual(supervisor.completed_runs_ledger, [])


if __name__ == '__main__':
    unittest.main()
