"""Continuation gates must protect original runs and forbid premature scaling."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import town10_continuation as study


class ContinuationTests(unittest.TestCase):
    def test_frozen_catalogue_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'frozen_catalogue.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'already frozen'):
                study.prepare(root)

    def test_robustness_requires_current_passing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            study.save(root/'Reports/validation_gate.json', {'passed':False})
            catalogue={'tests':[{'test':'Robustness_T01_01'}]}
            with patch.object(study,'verified_catalogue',return_value=catalogue), patch.object(study,'run_one') as worker:
                with self.assertRaisesRegex(RuntimeError,'prohibited'):
                    study.run(root,'Robustness_T01_01')
                worker.assert_not_called()

    def test_preexisting_attempt_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cfg={'test':'Validation_T01_01','relative_path':'Validation/Tests/Theme_01/Test_01'}
            study.save(root/cfg['relative_path']/'Automatic/Assistance_OFF/technical_failure.json',{'error':'retained'})
            with patch.object(study,'verified_catalogue',return_value={'tests':[cfg]}), patch.object(study,'run_one') as worker:
                with self.assertRaisesRegex(RuntimeError,'retained'):
                    study.run(root,cfg['test'],'Automatic','OFF')
                worker.assert_not_called()

    def test_numeric_gate_without_media_cannot_launch_robustness(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);study.save(root/'frozen_catalogue.json',{})
            study.save(root/'Reports/validation_gate.json',{'passed':True,'catalogue_sha256':study.digest(root/'frozen_catalogue.json')})
            catalogue={'tests':[{'test':'Robustness_T01_01'}]}
            with patch.object(study,'verified_catalogue',return_value=catalogue),patch.object(study,'run_one') as worker:
                with self.assertRaisesRegex(RuntimeError,'media package'):
                    study.run(root,'Robustness_T01_01')
                worker.assert_not_called()

    def test_missing_runs_cannot_pass_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cfg={'test':'Validation_T06_01','relative_path':'Validation/Tests/Theme_06/Test_01',
                 'readable_name':'Waiting pedestrian','pedestrian_behavior':'wait'}
            study.save(root/'frozen_catalogue.json',{'tests':[cfg]})
            with patch.object(study,'verified_catalogue',return_value={'tests':[cfg]}):
                result=study.check(root)
            self.assertFalse(result['passed'])
            self.assertEqual(result['completed_runs'],0)
            self.assertEqual(result['accepted_mode_pairs'],0)


if __name__=='__main__':unittest.main()
