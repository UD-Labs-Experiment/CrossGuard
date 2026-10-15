"""Fresh repair validation must retain failures and stop before acceptance."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tools.town10_revalidation as rv


class RevalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        args = argparse.Namespace(root=Path(self.tmp.name), catalogue_sha256='pinned',
                                  bridge_url='http://127.0.0.1:8007', bridge_pid=99,
                                  bridge_created=123., resume=False, dry_run=False)
        with patch.object(rv.RevalidationSupervisor, '_setup_signals'):
            self.supervisor = rv.RevalidationSupervisor(args)
        self.tests = [{'test': f'Validation_T{i:02}_01', 'theme': f'T{i:02}',
                       'relative_path': f'Validation/Test{i}'} for i in range(1, 8)]

    def test_collection_order_all_28_and_stops_for_independent_review(self):
        supervisor = self.supervisor
        supervisor.catalogue = Mock(return_value=self.tests)
        supervisor.check_bridge_alive = Mock()
        supervisor.get_bridge_status = Mock(return_value={'state': 'stopped'})
        executed = []

        def execute(cfg, mode, state):
            executed.append((cfg['test'], mode, state))
            supervisor.completed_runs_ledger.append({'test': cfg['test']})

        supervisor.execute_window = Mock(side_effect=execute)
        supervisor.assess_configuration = Mock()
        with patch.object(rv, 'verify_completed_evidence', return_value=(False, 'Not started')):
            supervisor.run()
        self.assertEqual(len(executed), 28)
        self.assertEqual(executed[:4], [(self.tests[0]['test'], m, s)
                                      for m in rv.MODES for s in rv.STATES])
        self.assertEqual(supervisor.assess_configuration.call_count, 7)
        status = json.loads(supervisor.progress_path.read_text())
        self.assertEqual(status['status'], 'AWAITING_CODEX_INDEPENDENT_REVIEW')
        self.assertEqual(status['completed_runs'], 28)
        self.assertEqual(status['independently_accepted_pairs'], 0)
        self.assertFalse(status['robustness_started'])
        self.assertFalse(supervisor.lock_path.exists())

    def test_partial_attempt_blocks_and_safe_stops_without_retry(self):
        supervisor = self.supervisor
        supervisor.catalogue = Mock(return_value=self.tests)
        supervisor.check_bridge_alive = Mock()
        supervisor.get_bridge_status = Mock(return_value={'state': 'stopped'})
        supervisor.execute_window = Mock()
        supervisor.safe_stop = Mock()
        supervisor.terminate_owned = Mock()
        with patch.object(rv, 'verify_completed_evidence', return_value=(False, 'Partial weather failure')):
            with self.assertRaisesRegex(RuntimeError, 'blocks retry'):
                supervisor.run()
        supervisor.execute_window.assert_not_called()
        supervisor.safe_stop.assert_called_once()
        self.assertEqual(json.loads(supervisor.progress_path.read_text())['status'], 'FAILED_SAFE_STOP')

    def test_complete_evidence_requires_explicit_resume(self):
        with patch.object(rv, 'verify_completed_evidence', return_value=(True, None)):
            with self.assertRaisesRegex(RuntimeError, 'explicit --resume'):
                self.supervisor.pending(self.tests)
            self.supervisor.args.resume = True
            self.assertEqual(self.supervisor.pending(self.tests), [])
        self.assertEqual(len(self.supervisor.completed_runs_ledger), 28)

    def test_dry_run_does_not_execute(self):
        supervisor = self.supervisor
        supervisor.args.dry_run = True
        supervisor.catalogue = Mock(return_value=self.tests)
        supervisor.check_bridge_alive = Mock()
        supervisor.get_bridge_status = Mock(return_value={'state': 'stopped'})
        supervisor.execute_window = Mock()
        with patch.object(rv, 'verify_completed_evidence', return_value=(False, 'Not started')):
            supervisor.run()
        supervisor.execute_window.assert_not_called()
        self.assertEqual(json.loads(supervisor.progress_path.read_text())['status'], 'DRY_RUN_VERIFIED')


if __name__ == '__main__':
    unittest.main()
