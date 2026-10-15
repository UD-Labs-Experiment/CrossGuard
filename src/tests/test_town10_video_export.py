"""Offline export admission, hash-bound resumption, and immutable input binding."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from tools import town10_video_export as export
from tools import town10_continuation_media as media


class VideoExportTests(unittest.TestCase):
    def test_default_export_still_requires_real_scene_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cfg={'test':'Robustness_T01_01','relative_path':'run'}
            with patch.object(media,'verified_catalogue',return_value={'tests':[cfg]}):
                with self.assertRaisesRegex(RuntimeError,'Inspect actual pictures'):
                    media.videos(root,cfg['test'],'Automatic')

    def test_explicit_pending_export_reaches_source_checks_without_faking_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); cfg={'test':'Robustness_T01_01','relative_path':'run'}
            with patch.object(media,'verified_catalogue',return_value={'tests':[cfg]}),\
                 patch.object(media,'complete_sets',side_effect=RuntimeError('Source check reached')):
                with self.assertRaisesRegex(RuntimeError,'Source check reached'):
                    media.videos(root,cfg['test'],'Automatic',allow_unreviewed_export=True)
            self.assertFalse((root/'run/Automatic/scene_review.json').exists())

    def test_resume_rejects_changed_video_before_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            paths=export.output_paths(folder,'Automatic')
            for path in paths:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'changed')
            saved={'videos':[{'sha256':'old'} for _ in range(3)]}
            self.assertFalse(export.validate_outputs(folder,'Automatic',saved))

    def test_source_identity_binds_all_eight_input_files_without_writing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            for state in export.STATES:
                run=folder/'Automatic'/f'Assistance_{state}';run.mkdir(parents=True)
                for name in ('results.json','camera_manifest.json','physical_timeline.json','placement_audit.json'):
                    (run/name).write_text(json.dumps({'state':state}))
            before=export.source_identity(folder,'Automatic')
            self.assertEqual(len(before),8)
            self.assertEqual(before,export.source_identity(folder,'Automatic'))
            (folder/'Automatic/Assistance_ON/results.json').write_text('{}')
            self.assertNotEqual(before,export.source_identity(folder,'Automatic'))

    def test_resume_decodes_all_frames_and_rejects_truncated_video(self):
        import cv2
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);paths=export.output_paths(folder,'Automatic');saved={'videos':[]}
            for path in paths:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'video')
                path.with_suffix('.verification.json').write_text('{"passed":true}')
                path.with_suffix('.source_frames.json').write_text(json.dumps([{}]*201))
                saved['videos'].append({'sha256':export.digest(path)})
            class Decoder:
                def __init__(self,path,count=201):
                    self.count=count;self.width=2560 if 'OFF_vs_ON' in path else 1280
                def read(self):
                    if self.count==0:return False,None
                    self.count-=1;return True,SimpleNamespace(shape=(1210,self.width,3))
                def get(self,key):return 10
                def release(self):pass
            with patch.object(cv2,'VideoCapture',side_effect=Decoder):
                self.assertTrue(export.validate_outputs(folder,'Automatic',saved))
            with patch.object(cv2,'VideoCapture',side_effect=lambda path:Decoder(path,200)):
                self.assertFalse(export.validate_outputs(folder,'Automatic',saved))
            paths[0].with_suffix('.verification.json').unlink()
            self.assertFalse(export.validate_outputs(folder,'Automatic',saved))


if __name__=='__main__':unittest.main()
