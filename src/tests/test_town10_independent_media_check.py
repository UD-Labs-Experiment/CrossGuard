"""Independent checker rejects forged sources, incomplete decodes and changed evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import town10_independent_media_check as check


class IndependentMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = {'OFF': self.root / 'OFF'}
        pics = self.runs['OFF'] / 'assets/pictures'
        pics.mkdir(parents=True)
        views = {v: f'{v}.jpg' for v in check.VIEWS}
        for name in views.values():
            (pics / name).write_bytes(b'original image')
        self.manifest = [{'frame': i, 'time_s': i / 10, 'views': views, 'missing': []} for i in range(201)]
        self.sources = [{'video_s': i / 10, 'sources': {'OFF': copy.deepcopy(e)}} for i, e in enumerate(self.manifest)]

    def test_original_manifest_frames_pass(self):
        self.assertEqual(check.verify_sources(self.sources, {'OFF': self.manifest}, self.runs, ('OFF',)), 0)

    def test_forged_original_frame_metadata_rejected(self):
        self.sources[100]['sources']['OFF']['views']['driver'] = 'substitute.jpg'
        with self.assertRaisesRegex(RuntimeError, 'original camera manifest'):
            check.verify_sources(self.sources, {'OFF': self.manifest}, self.runs, ('OFF',))

    def test_real_frame_from_wrong_time_rejected(self):
        self.sources[100]['sources']['OFF'] = copy.deepcopy(self.manifest[101])
        with self.assertRaisesRegex(RuntimeError, 'alignment'):
            check.verify_sources(self.sources, {'OFF': self.manifest}, self.runs, ('OFF',))

    def test_output_timeline_and_missing_camera_rejected(self):
        self.sources[100]['video_s'] = 10.2
        with self.assertRaisesRegex(RuntimeError, 'timing'):
            check.verify_sources(self.sources, {'OFF': self.manifest}, self.runs, ('OFF',))
        self.sources[100]['video_s'] = 10
        (self.runs['OFF'] / 'assets/pictures/top.jpg').unlink()
        with self.assertRaisesRegex(RuntimeError, 'image missing'):
            check.verify_sources(self.sources, {'OFF': self.manifest}, self.runs, ('OFF',))

    def test_original_json_snapshot_rejects_changes_and_path_escape(self):
        path = self.root / 'results.json'; path.write_text('{}')
        snapshot = {'results.json': check.digest(path)}
        check.unchanged_inputs(self.root, snapshot)
        path.write_text('{"changed": true}')
        with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            check.unchanged_inputs(self.root, snapshot)
        with self.assertRaisesRegex(RuntimeError, 'leaves root'):
            check.contained(self.root, '../outside.json')

    def capture(self, frames, width=1280, fps=10):
        class Capture:
            count = 0
            released = False
            def isOpened(self): return True
            def read(self):
                self.count += 1
                return (True, SimpleNamespace(shape=(1210, width, 3))) if self.count <= frames else (False, None)
            def get(self, prop): return fps
            def release(self): self.released = True
        return Capture()

    def test_fresh_full_decode_rejects_truncation_even_with_claimed_success(self):
        capture = self.capture(200)
        with self.assertRaisesRegex(RuntimeError, '200 frames'):
            check.decode_video(self.root / 'video.mp4', 1280, lambda _: capture)
        self.assertTrue(capture.released)
        result = check.decode_video(self.root / 'video.mp4', 1280, lambda _: self.capture(201))
        self.assertEqual(result['decoded_frames'], 201)

    def test_decode_rejects_wrong_dimensions_and_fps(self):
        with self.assertRaisesRegex(RuntimeError, 'dimensions'):
            check.decode_video(self.root / 'video.mp4', 1280, lambda _: self.capture(201, width=2560))
        with self.assertRaisesRegex(RuntimeError, 'fps'):
            check.decode_video(self.root / 'video.mp4', 1280, lambda _: self.capture(201, fps=9))

    def test_incomplete_export_cannot_start_independent_decode_or_accept_scene(self):
        catalogue = self.root / 'frozen_catalogue.json'; catalogue.write_text('{}')
        export = self.root / 'Reports/Video_Export'; export.mkdir(parents=True)
        (export / 'progress.json').write_text(json.dumps({'completed_videos': 359, 'failed_videos': 0, 'status': 'RUNNING'}))
        (export / 'simulation_input_preservation.json').write_text(json.dumps({'passed': True, 'changed_files': []}))
        configs = {'tests': [{'test': f'Robustness_T01_{n}'} for n in range(60)]}
        with patch.object(check, 'verified_catalogue', return_value=configs), patch.object(check, 'decode_video') as decoder:
            report = check.check(self.root, check.digest(catalogue))
            decoder.assert_not_called()
        self.assertFalse(report['passed'])
        self.assertEqual(report['scene_review_status'], 'PENDING')
        self.assertEqual(report['checked_videos'], 0)
        self.assertEqual(list(self.root.rglob('scene_review.json')), [])


if __name__ == '__main__':
    unittest.main()
