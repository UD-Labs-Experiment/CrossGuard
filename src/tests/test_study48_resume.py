import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import town10_study48 as runner

class ResumeTests(unittest.TestCase):
    def test_failed_numbers_and_assets_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            previous=runner.STUDY_ROOT;runner.STUDY_ROOT=Path(temp)
            try:
                output=Path(temp)/'Theme/Test/Automatic/OFF';output.mkdir(parents=True)
                (output/'technical_failure.json').write_text('{"error":"placement"}')
                media=output.parent/'assets/OFF';media.mkdir(parents=True);(media/'raw.jpg').write_bytes(b'original')
                runner.preserve_failed_attempt(output,{'test':'Test_006'},'Automatic','OFF')
                saved=Path(temp)/'Failed/Run_Attempts/Test_006/Automatic/OFF/Attempt_001'
                self.assertEqual((saved/'assets/raw.jpg').read_bytes(),b'original')
                self.assertIn('placement',(saved/'numeric/technical_failure.json').read_text())
                self.assertFalse(output.exists())
            finally:runner.STUDY_ROOT=previous
    def test_complete_run_cannot_be_archived(self):
        with tempfile.TemporaryDirectory() as temp:
            previous=runner.STUDY_ROOT;runner.STUDY_ROOT=Path(temp)
            try:
                output=Path(temp)/'Test/Automatic/OFF';output.mkdir(parents=True)
                (output/'results.json').write_text(json.dumps({'terminal_reason':'observation_window_complete','technical_failure':False}))
                with self.assertRaises(RuntimeError):runner.preserve_failed_attempt(output,{'test':'Test_001'},'Automatic','OFF')
                self.assertTrue(runner.completed(output))
            finally:runner.STUDY_ROOT=previous
