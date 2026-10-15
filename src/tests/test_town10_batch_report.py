"""Offline batch helpers preserve missing outcomes and isolate physical evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import town10_batch_report as batch


class BatchReportTests(unittest.TestCase):
    def test_missing_run_does_not_claim_zero_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            result=batch.summarize_run(Path(directory))
        self.assertFalse(result['available'])
        self.assertIsNone(result['pedestrian_contact_vehicles'])
        self.assertEqual(result['secondary'],{})
        self.assertEqual(len(result['explicit_missing_measurements']),5)

    def test_control_comparison_excludes_contact_frame(self):
        off=[{'frame':10+i,'time_s':i/60,'pedestrian_control':{'speed_mps':1.},
              'pedestrian_submitted_control':{'speed_mps':1.}} for i in range(3)]
        on=json.loads(json.dumps(off))
        for r in on:r['frame']+=100
        on[-1]['pedestrian_control']={'speed_mps':-9000.}
        result=batch.compare_controls(off,on,{'pedestrian_contacts':[{'frame':12}]},{'pedestrian_contacts':[]})
        self.assertTrue(result['submitted_controls_equal_before_first_contact'])
        self.assertTrue(result['submitted_controls_equal_full_window'])
        self.assertTrue(result['readbacks_equal_before_first_contact'])
        self.assertEqual(result['first_pedestrian_contact_cutoff_s'],2/60)

    def test_missing_submitted_commands_remain_unknown(self):
        rows=[{'frame':1,'time_s':0.,'pedestrian_control':{'speed_mps':0.}}]
        result=batch.compare_controls(rows,rows,{}, {})
        self.assertIsNone(result['submitted_controls_equal_before_first_contact'])
        self.assertFalse(result['submitted_control_present_all_rows'])

    def test_plan_rejects_escape_and_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan=root/'plan.json'
            for paths in (['../outside'],['Theme_01/Test_003','Theme_01/Test_003']):
                plan.write_text(json.dumps({'tests':[{'relative_path':p} for p in paths]}))
                with patch.object(batch,'STUDY_ROOT',root),self.assertRaises(ValueError):
                    batch.test_roots(plan)

    def test_package_context_restores_exporter_roots(self):
        before=batch.synchronized.ROOT
        with batch.package_context(Path('temporary')):
            self.assertEqual(batch.synchronized.ROOT,Path('temporary'))
        self.assertEqual(batch.synchronized.ROOT,before)


if __name__=='__main__':unittest.main()
