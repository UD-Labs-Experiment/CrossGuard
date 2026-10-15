"""Filesystem migration tests; no simulator and no claimed physical outcomes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import restructure_town10_evidence as layout


class EvidenceRestructureTests(unittest.TestCase):
    def test_migration_preserves_records_and_relocates_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            relative='Theme_01_Old/Test_003_Old'
            cfg={'test':'Test_003','relative_path':relative,'seed':2003,'speed_kmh':35,'pedestrian_behavior':'cross'}
            layout.save(root/'study_48_plan.json',{'tests':[cfg]})
            layout.save(root/'batch_12_plan.json',{'tests':[cfg]})
            layout.save(root/relative/'test_config.json',cfg)
            (root/relative/'report.md').write_text('# Detailed fixture evidence',encoding='utf-8')
            q={'qualified':False,'initial_occluded_scene_verified':False,'reasons':['fixture has no trigger']}
            layout.save(root/'efficiency_report.json',{'tests':[{'catalogue':cfg,'modes':{m:{'qualification':q} for m in layout.MODES}}],
                                                      'archived_failed_or_incomplete_attempts':{'records':[]}})
            (root/'efficiency_report.md').write_text(f'[Run]({relative}/Automatic/OFF/results.json)',encoding='utf-8')
            (root/'study_48_report.md').write_text('# Evidence',encoding='utf-8')
            (root/'README.md').write_text('# Previous presentation',encoding='utf-8')
            (root/'Failed').mkdir()
            (root/'diagnostics').mkdir()
            (root/'diagnostics/secondary_collision_diagnostic.md').write_text('# Separate diagnostic',encoding='utf-8')
            (root/'Theme_01_Old/README.md').write_text('[Test](Test_003_Old/test_config.json)',encoding='utf-8')
            expected={}
            for mode in layout.MODES:
                for state in layout.STATES:
                    run=root/relative/mode/state
                    raw={'terminal_reason':'observation_window_complete','pedestrian_movement_trigger_s':None,
                         'pedestrian_contacts':[],'secondary':{'A_B':{'contact':False},'B_C':{'contact':False}},
                         'historical_capture_path':str(run)}
                    layout.save(run/'results.json',raw);expected[(mode,state)]=(run/'results.json').read_bytes()
                    layout.save(run/'physical_timeline.json',[{'frame':1,'time_s':0,'vehicles':{}}])
                    layout.save(run/'requested_config.json',{})
                    layout.save(run/'causal_chronology.json',{})
                    media=root/relative/mode/'assets'/state
                    (media/'pictures').mkdir(parents=True);(media/'event_sets').mkdir()
                    (media/'pictures/frame.jpg').write_bytes(b'unchanged synthetic filesystem fixture')
                for view in layout.VIEWS:
                    (root/relative/mode/'assets'/f'{view}_OFF_ON.mp4').write_bytes(b'unchanged video fixture')
            layout.reorganize(root,True)
            new=root/'Tests/Theme_01_Crosswalk_Location/Test_01_Morning_Bus_Crossing'
            for mode in layout.MODES:
                for state in layout.STATES:
                    run=new/mode/f'Assistance_{state}'
                    self.assertEqual((run/'results.json').read_bytes(),expected[(mode,state)])
                    self.assertEqual((run/'assets/pictures/frame.jpg').read_bytes(),b'unchanged synthetic filesystem fixture')
                    self.assertIn('INVALID ENCOUNTER', (run/'README.md').read_text(encoding='utf-8'))
                    # Fixtures stand in for new presentation media, not simulation evidence.
                    (run/'assets/run_video.mp4').write_bytes(b'presentation link fixture')
                (new/mode/'Comparison'/f'{mode}_Assistance_OFF_vs_ON.mp4').write_bytes(b'presentation link fixture')
            self.assertEqual((new/'Automatic/Comparison/Driver_A_Assistance_OFF_vs_ON.mp4').read_bytes(),b'unchanged video fixture')
            self.assertIn('../Tests/Theme_01_Crosswalk_Location', (root/'Reports/Efficiency_Report.md').read_text(encoding='utf-8'))
            self.assertTrue((root/'Diagnostics/Sudden_Braking/secondary_collision_diagnostic.md').exists())
            layout.verify(root,require_media=False)
            self.assertTrue(layout.read(root/'Reports/Technical_Records/reorganization_verification.json')['passed'])

    def test_waiting_and_missing_emergence_have_distinct_explanations(self):
        q={'qualified':False,'reasons':['missing evidence'],'initial_occluded_scene_verified':False}
        self.assertIn('intentionally',layout.warning({'pedestrian_behavior':'wait'},{},q))
        self.assertIn('INVALID ENCOUNTER',layout.warning({'pedestrian_behavior':'cross'},{},q))
        self.assertIn('RETREAT CASE',layout.warning({'pedestrian_behavior':'retreat'},{},q))

if __name__=='__main__':unittest.main()
