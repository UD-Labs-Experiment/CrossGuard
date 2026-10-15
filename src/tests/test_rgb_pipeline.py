import time
import unittest
import numpy as np
from crossguard.contracts import Detection
from crossguard.perception.pipeline import RGBPipeline
from crossguard.perception.onnx_detection import OnnxYoloDetector


class DecoderTests(unittest.TestCase):
    def test_letterbox_nms_and_class_filter(self):
        import cv2
        detector = OnnxYoloDetector.__new__(OnnxYoloDetector)
        detector.cv2, detector.confidence = cv2, 0.35
        tensor = np.zeros((1, 84, 8400), dtype=np.float32)
        # 1280x720 -> 640x360 with 140px vertical padding.
        tensor[0, :4, 0] = [320, 320, 40, 100]
        tensor[0, 4, 0] = 0.9
        tensor[0, :4, 1] = [321, 320, 40, 100]
        tensor[0, 4, 1] = 0.8
        tensor[0, :4, 2] = [320, 320, 40, 100]
        tensor[0, 6, 2] = 0.7
        boxes = detector.decode(tensor, .5, 0, 140, 1280, 720)
        self.assertEqual([b.label for b in boxes], ["person", "vehicle"])
        np.testing.assert_allclose(boxes[0].bbox, [600, 260, 680, 460])

    def test_malformed_output_fails(self):
        detector = OnnxYoloDetector.__new__(OnnxYoloDetector)
        with self.assertRaises(ValueError):
            detector.decode(np.zeros((1,6,300)), 1,0,0,640,640)


class PipelineTests(unittest.TestCase):
    @staticmethod
    def sample(timestamp=2.0, frame=120):
        transform = np.eye(4)
        transform[2,3] = 2
        return {"rgb":np.zeros((600,800,3),dtype=np.uint8), "size":(800,600),
                "fov":90, "transform":transform, "timestamp_s":timestamp,
                "frame_id":frame, "ground_z":0}

    def wait_result(self, pipeline):
        limit = time.monotonic()+3
        while time.monotonic()<limit:
            state=pipeline.snapshot()
            if state['completed_batches'] or state['error']:
                return state
            time.sleep(.01)
        self.fail("Worker failed to finish")

    def test_rgb_pose_capture_time_and_frame_preserved(self):
        class Detector:
            def detect(self, image):
                return [Detection((390,330,410,380),.9,"person")]
        pipeline=RGBPipeline(Detector())
        try:
            pipeline.submit({"rsu":self.sample()})
            state=self.wait_result(pipeline)
            observation=state['observations']['rsu'][0]
            self.assertEqual((observation.observed_at_s,observation.frame_id,
                              observation.source_id,observation.perception_mode),
                             (2,120,'rsu','rgb'))
            np.testing.assert_allclose(observation.position,[10,0])
        finally:
            pipeline.close()

    def test_inference_failure_is_visible_and_never_ideal(self):
        class Detector:
            def detect(self,image):
                raise RuntimeError("weights failure")
        pipeline=RGBPipeline(Detector())
        try:
            pipeline.submit({"driver":self.sample()})
            state=self.wait_result(pipeline)
            self.assertIn('weights failure',state['error'])
            self.assertEqual(state['observations'],{})
        finally:
            pipeline.close()
