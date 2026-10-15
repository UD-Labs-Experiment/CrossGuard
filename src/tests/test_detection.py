"""Dependency-free unit tests for YoloDetector RGB perception adapter.

Verifies:
- Explicit local weights path validation and auto-download prevention.
- Lazy import and dependency error behavior.
- RGB-to-BGR channel reversal matching Ultralytics array expectations.
- Class mapping to canonical contract labels ('person' and 'vehicle').
- Confidence and class filtering.
- Bounding box boundary, degeneracy, and non-finite coordinate validation.
- Non-finite confidence and invalid model output error reporting.
- Non-swallowing of model exceptions (no fake empty inference).
- Latency tracking via time.perf_counter.
- Mode and perception mode contract compliance.
"""
from __future__ import annotations

import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from crossguard.contracts import Detection
from crossguard.perception.detection import YoloDetector, DEFAULT_LABEL_MAPPING


# Mock / Fake Ultralytics structures for dependency-free testing
class FakeBoxes:
    """Mock Boxes object mimicking Ultralytics Results.boxes."""

    def __init__(self, xyxy=None, conf=None, cls=None):
        self.xyxy = np.array(xyxy if xyxy is not None else [], dtype=float)
        self.conf = np.array(conf if conf is not None else [], dtype=float)
        self.cls = np.array(cls if cls is not None else [], dtype=float)

    def __len__(self):
        return len(self.xyxy)


class FakeResult:
    """Mock Result object mimicking an Ultralytics Results item."""

    def __init__(self, boxes: FakeBoxes | None = None):
        self.boxes = boxes


class FakeModel:
    """Mock YOLO model capturing predict arguments and returning configured results."""

    def __init__(self, names=None, results=None, delay_s: float = 0.0):
        if names is None:
            # Standard COCO subset with person, vehicles, and unrelated classes
            names = {
                0: "person",
                1: "bicycle",
                2: "car",
                3: "motorcycle",
                5: "bus",
                7: "truck",
                16: "dog",
                56: "chair",
            }
        self.names = names
        self.results = results if results is not None else [FakeResult(FakeBoxes())]
        self.delay_s = delay_s
        self.last_source = None
        self.last_kwargs = None

    def predict(self, source, **kwargs):
        self.last_source = source
        self.last_kwargs = kwargs
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        return self.results


class DetectionUnitTests(unittest.TestCase):
    """Test suite for YoloDetector."""

    def setUp(self):
        self.image_shape = (600, 800, 3)
        self.rgb_image = np.zeros(self.image_shape, dtype=np.uint8)

    # 1. Weights and auto-download checks
    def test_nonexistent_weights_raises_file_not_found(self):
        nonexistent = Path("nonexistent_yolo_weights_xyz123.pt")
        with self.assertRaises(FileNotFoundError) as ctx:
            YoloDetector(nonexistent)
        self.assertIn("Auto-downloading weights is disabled", str(ctx.exception))

    def test_weights_none_without_injection_raises_value_error(self):
        with self.assertRaises(ValueError):
            YoloDetector(None)

    # 2. Parameter validation
    def test_invalid_confidence_values(self):
        for invalid in (-0.1, 0.0, 1.1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    YoloDetector("dummy.pt", confidence=invalid, model=FakeModel())

    def test_invalid_device(self):
        for invalid in ("", "   ", None, 123):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    YoloDetector("dummy.pt", device=invalid, model=FakeModel())

    # 3. Model class exposure and mapping configuration
    def test_model_without_names_raises_value_error(self):
        class NoNamesModel:
            pass

        with self.assertRaises(ValueError):
            YoloDetector("dummy.pt", model=NoNamesModel())

    def test_model_without_person_or_vehicle_raises_value_error(self):
        fake = FakeModel(names={0: "dog", 1: "cat", 2: "chair"})
        with self.assertRaises(ValueError) as ctx:
            YoloDetector("dummy.pt", model=fake)
        self.assertIn("does not expose any person or vehicle classes", str(ctx.exception))

    def test_model_with_list_names_format(self):
        fake = FakeModel(names=["person", "car", "dog"])
        detector = YoloDetector("dummy.pt", model=fake)
        self.assertEqual(detector._class_to_label[0], "person")
        self.assertEqual(detector._class_to_label[1], "vehicle")
        self.assertNotIn(2, detector._class_to_label)

    # 4. Lazy import and missing dependency error
    def test_lazy_import_error_when_ultralytics_missing(self):
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with patch.dict(sys.modules, {"ultralytics": None}):
                with self.assertRaises(ImportError) as ctx:
                    YoloDetector(tmp_path)
                self.assertIn("ultralytics", str(ctx.exception).lower())
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    # 5. Model factory injection
    def test_model_factory_injection(self):
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            created_with = []

            def factory(p):
                created_with.append(p)
                return FakeModel()

            detector = YoloDetector(tmp_path, model_factory=factory)
            self.assertEqual(created_with, [str(tmp_path)])
            self.assertEqual(detector.mode, "rgb")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    # 6. Mode, perception_mode, and latency properties
    def test_mode_and_latency_properties(self):
        fake = FakeModel()
        detector = YoloDetector("dummy.pt", model=fake)
        self.assertEqual(detector.mode, "rgb")
        self.assertEqual(detector.perception_mode, "rgb")
        self.assertEqual(detector.model_mode, "rgb")
        self.assertIsNone(detector.last_latency_s)

        detector.detect(self.rgb_image)
        self.assertIsNotNone(detector.last_latency_s)
        self.assertGreater(detector.last_latency_s, 0.0)

    def test_latency_tracking_with_delay(self):
        fake = FakeModel(delay_s=0.015)
        detector = YoloDetector("dummy.pt", model=fake)
        detector.detect(self.rgb_image)
        self.assertGreaterEqual(detector.last_latency_s, 0.014)

    # 7. Input array validation
    def test_input_not_numpy_array(self):
        fake = FakeModel()
        detector = YoloDetector("dummy.pt", model=fake)
        for invalid in (None, [[1, 2, 3]], "image.png", 42):
            with self.subTest(invalid=type(invalid)):
                with self.assertRaises(TypeError):
                    detector.detect(invalid)

    def test_input_wrong_dtype(self):
        fake = FakeModel()
        detector = YoloDetector("dummy.pt", model=fake)
        for dtype in (np.float32, np.float64, np.int32, np.int64, np.uint16):
            with self.subTest(dtype=dtype):
                img = np.zeros((100, 100, 3), dtype=dtype)
                with self.assertRaises(ValueError):
                    detector.detect(img)

    def test_input_wrong_dimensions_or_channels(self):
        fake = FakeModel()
        detector = YoloDetector("dummy.pt", model=fake)
        # 2D grayscale
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((100, 100), dtype=np.uint8))
        # 4D tensor
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((1, 100, 100, 3), dtype=np.uint8))
        # 4-channel RGBA
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((100, 100, 4), dtype=np.uint8))
        # 1-channel
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((100, 100, 1), dtype=np.uint8))
        # Zero height or width
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((0, 100, 3), dtype=np.uint8))
        with self.assertRaises(ValueError):
            detector.detect(np.zeros((100, 0, 3), dtype=np.uint8))

    # 8. RGB to BGR conversion check
    def test_rgb_to_bgr_explicit_conversion(self):
        fake = FakeModel()
        detector = YoloDetector("dummy.pt", model=fake)
        img = np.zeros((10, 10, 3), dtype=np.uint8)
        # Set distinctive colour in RGB: Red=255, Green=120, Blue=10
        img[0, 0] = [255, 120, 10]
        detector.detect(img)

        self.assertIsNotNone(fake.last_source)
        # Ultralytics must receive BGR: Blue=10, Green=120, Red=255
        np.testing.assert_array_equal(fake.last_source[0, 0], [10, 120, 255])
        self.assertTrue(fake.last_source.flags["C_CONTIGUOUS"])
        self.assertEqual(fake.last_source.dtype, np.uint8)

    # 9. Class mapping and Detection object returns
    def test_class_mapping_person_and_vehicles(self):
        # 0: person, 1: bicycle, 2: car, 3: motorcycle, 5: bus, 7: truck
        boxes = FakeBoxes(
            xyxy=[
                [10, 20, 50, 100],
                [60, 30, 90, 80],
                [100, 50, 250, 150],
                [260, 40, 300, 120],
                [310, 10, 500, 200],
                [510, 20, 700, 250],
            ],
            conf=[0.9, 0.85, 0.95, 0.8, 0.88, 0.92],
            cls=[0, 1, 2, 3, 5, 7],
        )
        fake = FakeModel(results=[FakeResult(boxes)])
        detector = YoloDetector("dummy.pt", confidence=0.3, model=fake)
        detections = detector.detect(self.rgb_image)

        self.assertEqual(len(detections), 6)
        expected_labels = ["person", "vehicle", "vehicle", "vehicle", "vehicle", "vehicle"]
        for det, expected_label in zip(detections, expected_labels):
            self.assertIsInstance(det, Detection)
            self.assertEqual(det.label, expected_label)
            self.assertTrue(all(isinstance(c, float) for c in det.bbox))
            self.assertIsInstance(det.confidence, float)

    # 10. Confidence threshold and non-target class filtering
    def test_confidence_and_class_filtering(self):
        boxes = FakeBoxes(
            xyxy=[
                [10, 20, 50, 100],   # person, conf 0.8 -> keep
                [60, 30, 90, 80],    # car, conf 0.25 -> drop (conf < 0.3)
                [100, 50, 200, 150], # dog (cls 16), conf 0.9 -> drop (not person/vehicle)
                [210, 40, 350, 160], # chair (cls 56), conf 0.95 -> drop (not person/vehicle)
                [400, 50, 550, 200], # truck, conf 0.3 -> keep (conf == 0.3)
            ],
            conf=[0.8, 0.25, 0.9, 0.95, 0.3],
            cls=[0, 2, 16, 56, 7],
        )
        fake = FakeModel(results=[FakeResult(boxes)])
        detector = YoloDetector("dummy.pt", confidence=0.3, model=fake)
        detections = detector.detect(self.rgb_image)

        self.assertEqual(len(detections), 2)
        self.assertEqual(detections[0].label, "person")
        self.assertAlmostEqual(detections[0].confidence, 0.8)
        self.assertEqual(detections[1].label, "vehicle")
        self.assertAlmostEqual(detections[1].confidence, 0.3)

    # 11. Bounding box bounds and coordinate validation
    def test_invalid_box_coordinates_raise_value_error(self):
        # Image is 800 wide, 600 high
        invalid_boxes = [
            [-5.0, 10.0, 50.0, 50.0],       # x1 < 0
            [10.0, -2.0, 50.0, 50.0],       # y1 < 0
            [10.0, 10.0, 850.0, 50.0],      # x2 > width
            [10.0, 10.0, 50.0, 650.0],      # y2 > height
            [50.0, 10.0, 20.0, 50.0],       # degenerate: x1 >= x2
            [10.0, 50.0, 50.0, 20.0],       # degenerate: y1 >= y2
            [float("nan"), 10.0, 50.0, 50.0], # non-finite NaN
            [10.0, float("inf"), 50.0, 50.0], # non-finite Inf
            [-100.0, 10.0, -20.0, 50.0],    # entirely outside left
        ]

        for box in invalid_boxes:
            with self.subTest(box=box):
                fake = FakeModel(results=[FakeResult(FakeBoxes(xyxy=[box], conf=[0.9], cls=[0]))])
                detector = YoloDetector("dummy.pt", model=fake)
                with self.assertRaises(ValueError):
                    detector.detect(self.rgb_image)

    def test_minor_boundary_overrun_clamped(self):
        # Image is 800 wide, 600 high; 0.005 overrun within 1e-2 epsilon
        boxes = FakeBoxes(
            xyxy=[[-0.005, 10.0, 800.005, 599.995]],
            conf=[0.9],
            cls=[0],
        )
        fake = FakeModel(results=[FakeResult(boxes)])
        detector = YoloDetector("dummy.pt", model=fake)
        detections = detector.detect(self.rgb_image)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].bbox, (0.0, 10.0, 800.0, 599.995))

    # 12. Non-finite or out-of-range confidence validation
    def test_invalid_detection_confidence_raises_value_error(self):
        invalid_confs = [float("nan"), float("inf"), -float("inf"), -0.1, 1.2]
        for conf in invalid_confs:
            with self.subTest(conf=conf):
                boxes = FakeBoxes(xyxy=[[10, 10, 50, 50]], conf=[conf], cls=[0])
                fake = FakeModel(results=[FakeResult(boxes)])
                detector = YoloDetector("dummy.pt", model=fake)
                with self.assertRaises(ValueError):
                    detector.detect(self.rgb_image)

    # 13. Invalid model output handling (no silent empty inference)
    def test_model_returning_none_raises_runtime_error(self):
        class NoneResultModel(FakeModel):
            def predict(self, source, **kwargs):
                return None

        detector = YoloDetector("dummy.pt", model=NoneResultModel())
        with self.assertRaises(RuntimeError) as ctx:
            detector.detect(self.rgb_image)
        self.assertIn("None", str(ctx.exception))

    def test_model_result_missing_boxes_raises_value_error(self):
        class MalformedResultModel(FakeModel):
            def predict(self, source, **kwargs):
                return [object()]

        detector = YoloDetector("dummy.pt", model=MalformedResultModel())
        with self.assertRaises(ValueError):
            detector.detect(self.rgb_image)

    def test_model_exception_propagates_unswallowed(self):
        class FailingModel(FakeModel):
            def predict(self, source, **kwargs):
                raise RuntimeError("Hardware CUDA failure")

        detector = YoloDetector("dummy.pt", model=FailingModel())
        with self.assertRaises(RuntimeError) as ctx:
            detector.detect(self.rgb_image)
        self.assertIn("Hardware CUDA failure", str(ctx.exception))
        # Latency should still have been recorded
        self.assertIsNotNone(detector.last_latency_s)

    # 14. Empty frame handling
    def test_clean_empty_detections(self):
        fake = FakeModel(results=[FakeResult(FakeBoxes())])
        detector = YoloDetector("dummy.pt", model=fake)
        detections = detector.detect(self.rgb_image)
        self.assertEqual(detections, [])

    def test_result_boxes_none(self):
        fake = FakeModel(results=[FakeResult(boxes=None)])
        detector = YoloDetector("dummy.pt", model=fake)
        detections = detector.detect(self.rgb_image)
        self.assertEqual(detections, [])


if __name__ == "__main__":
    unittest.main()
