"""Export checkpoints remain separate from physical and acceptance counters."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.town10_progress_dashboard import DashboardDataCollector


class ExportDashboardTests(unittest.TestCase):
    def test_missing_workers_are_not_falsely_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            data = DashboardDataCollector(Path(directory)).load_video_export()
            self.assertEqual(data['status'], 'NOT_STARTED')
            self.assertEqual(data['completed_videos'], 0)
            self.assertEqual(len(data['themes']), 6)
            self.assertIsNone(data['eta_s'])

    def test_per_theme_counts_override_stale_summary_and_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / 'Reports/Video_Export'
            reports.mkdir(parents=True)
            (reports / 'progress.json').write_text('{"completed_videos": 0}')
            for index in range(1, 7):
                (reports / f'progress_T{index:02}.json').write_text(json.dumps({
                    'status': 'COMPLETED', 'completed_videos': 60,
                    'elapsed_s': index * 10, 'eta_s': 0}))
            data = DashboardDataCollector(root).load_video_export()
            self.assertEqual(data['completed_videos'], 360)
            self.assertEqual(data['status'], 'COMPLETED')
            self.assertEqual(data['elapsed_s'], 60)
            self.assertEqual(data['eta_s'], 0)
            self.assertEqual(data['independent_review_status'], 'PENDING')

    def test_malformed_checkpoint_and_dead_worker_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / 'Reports/Video_Export'
            reports.mkdir(parents=True)
            (reports / 'progress_T01.json').write_text('{')
            (reports / 'progress_T02.json').write_text(json.dumps({
                'status': 'RUNNING', 'completed_videos': 3, 'worker_pid': 12345,
                'elapsed_s': 5, 'eta_s': 90}))
            with patch('tools.town10_progress_dashboard.is_pid_alive', return_value=False):
                data = DashboardDataCollector(root).load_video_export()
            self.assertEqual(data['themes'][0]['status'], 'STATUS_UNAVAILABLE')
            self.assertTrue(data['themes'][1]['is_stale'])
            self.assertEqual(data['status'], 'INTERRUPTED')
            self.assertEqual(data['completed_videos'], 3)
            self.assertIsNone(data['eta_s'])


if __name__ == '__main__':
    unittest.main()

