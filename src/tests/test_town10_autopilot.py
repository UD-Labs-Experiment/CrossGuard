import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import town10_autopilot as autopilot


class AutopilotTests(unittest.TestCase):
    def test_stop_marker_blocks_without_any_preflight_or_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'Revision'
            root.mkdir()
            marker = root.parent / 'user_stop.json'
            marker.write_text('{}')
            args = SimpleNamespace(root=root, arm=False)
            with patch.object(autopilot, 'preflight') as check, self.assertRaisesRegex(RuntimeError, 'User stopped'):
                autopilot.run(args)
            check.assert_not_called()
            self.assertTrue(marker.exists())

    def test_failed_preflight_preserves_stop_marker_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'Revision'
            root.mkdir()
            marker = root.parent / 'user_stop.json'
            marker.write_text('{}')
            ledger = root / 'progress.json'
            ledger.write_text('{"completed_runs":5}')
            with patch.object(autopilot, 'preflight', side_effect=RuntimeError('source mismatch')):
                with self.assertRaisesRegex(RuntimeError, 'source mismatch'):
                    autopilot.run(SimpleNamespace(root=root, arm=True))
            self.assertTrue(marker.exists())
            self.assertEqual(ledger.read_text(), '{"completed_runs":5}')
            self.assertFalse((root.parent / 'autopilot_owner.json').exists())

    def test_live_owner_prevents_second_autopilot(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'owner.json'
            lock.write_text('{"pid":42,"created":10}')
            with patch.object(autopilot, 'same_process', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'Another autopilot'):
                    autopilot.acquire(lock)

    def test_stale_owner_is_not_automatically_erased(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'owner.json'
            lock.write_text('{"pid":42,"created":10}')
            with patch.object(autopilot, 'same_process', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'Stale global'):
                    autopilot.acquire(lock)
            self.assertTrue(lock.exists())
