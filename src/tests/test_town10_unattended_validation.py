"""Unattended continuation never replaces evidence or touches unrelated owners."""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from tools import town10_unattended_validation as unattended


class UnattendedTests(unittest.TestCase):
    def test_completed_windows_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg={'relative_path':'Validation/Test_01'}
            run=root/cfg['relative_path']/'Automatic/Assistance_OFF';run.mkdir(parents=True)
            (run/'results.json').write_text('{}')
            self.assertEqual(unattended.pending_runs(root,cfg),[('Automatic','ON'),('Retrofit','OFF'),('Retrofit','ON')])

    def test_partial_attempt_blocks_unattended_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg={'relative_path':'Validation/Test_01'}
            run=root/cfg['relative_path']/'Automatic/Assistance_OFF';run.mkdir(parents=True)
            (run/'requested_config.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'partial attempt'):
                unattended.pending_runs(root,cfg)

    def test_readiness_failure_cannot_stop_existing_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            args=argparse.Namespace(root=Path(directory),test='Validation_T02_01',bridge_pid=123,
                bridge_created=0.,bridge_url='http://127.0.0.1:8007')
            supervisor=unattended.Supervisor(args)
            with patch.object(unattended.requests,'post') as command:
                supervisor.safe_stop();command.assert_not_called()

    def test_owned_failure_stops_through_existing_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            args=argparse.Namespace(root=Path(directory),test='Validation_T02_01',bridge_pid=123,
                bridge_created=0.,bridge_url='http://127.0.0.1:8007')
            supervisor=unattended.Supervisor(args);supervisor.worker=object()
            response=Mock();response.json.return_value={'status':'ok'}
            with patch.object(unattended,'same_process',return_value=True),patch.object(unattended.requests,'post',return_value=response) as command,patch.object(supervisor,'status',return_value={'state':'stopped'}):
                supervisor.safe_stop()
            command.assert_called_once_with('http://127.0.0.1:8007/api/command',json={'action':'stop','params':{}},timeout=30)
            self.assertTrue(supervisor.state['safe_stop_confirmed'])


if __name__=='__main__':unittest.main()
