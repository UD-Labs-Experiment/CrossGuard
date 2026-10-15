"""Evidence seals reject changed reviewed files and failed video coverage."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import town10_continuation_package as package


class PackageTests(unittest.TestCase):
    def build(self,root):
        tests=[{'test':f'Validation_{n}', 'relative_path':f'Validation/Test_{n}'} for n in range(7)]
        package.save(root/'frozen_catalogue.json',{'tests':tests})
        for cfg in tests:
            folder=root/cfg['relative_path']
            for mode in package.MODES:
                review={k:True for k in ('actual_rgb_inspected','native_markings_visible',
                    'pedestrian_access_verified','route_verified','useful_occlusion_verified','physical_gaps_verified')}
                review['evidence_sha256']={}
                for state in package.STATES:
                    run=folder/mode/f'Assistance_{state}'
                    for name in ('results.json','physical_timeline.json','camera_manifest.json'):
                        package.save(run/name,{})
                    review['evidence_sha256'][state]={'results.json':package.digest(run/'results.json')}
                    package.save(run/'assets/picture_verification.json',{'complete_synchronized_sets':201,'decoded_images':1206})
                    paths=[run/f'assets/videos/{mode}_Assistance_{state}.mp4']
                    if state=='OFF':paths.append(folder/mode/f'Comparison/{mode}_Assistance_OFF_vs_ON.mp4')
                    for path in paths:
                        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
                        package.save(path.with_suffix('.verification.json'),{'passed':True,'decoded_frames':201})
                        package.save(path.with_suffix('.source_frames.json'),[])
                package.save(folder/mode/'scene_review.json',review)
        return {'tests':tests}

    def test_readiness_rejects_changed_reviewed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalogue=self.build(root)
            with patch.object(package,'verified_catalogue',return_value=catalogue), patch.object(package,'load_run',return_value={'complete':True}):
                self.assertTrue(package.seal(root)['passed'])
                package.save(root/'Validation/Test_0/Automatic/Assistance_OFF/results.json',{'changed':True})
                gate=package.seal(root)
                self.assertFalse(gate['passed'])
                self.assertTrue(any('reviewed evidence changed' in e for e in gate['errors']))

    def test_readiness_rejects_failed_video_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalogue=self.build(root)
            package.save(root/'Validation/Test_0/Automatic/Comparison/Automatic_Assistance_OFF_vs_ON.verification.json',{'passed':False,'decoded_frames':200})
            with patch.object(package,'verified_catalogue',return_value=catalogue), patch.object(package,'load_run',return_value={'complete':True}):
                gate=package.seal(root)
            self.assertFalse(gate['passed'])
            self.assertTrue(any('video verification failed' in e for e in gate['errors']))


if __name__=='__main__':unittest.main()
