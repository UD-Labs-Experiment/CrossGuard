import math
import unittest
import numpy as np

from crossguard.contracts import Detection
from crossguard.perception.geometry import ground_position
from crossguard.perception.tracking import GroundTracker


SIZE = (800, 600)
CAMERA = np.eye(4)
CAMERA[2, 3] = 2.0


def box_at(x, y, label="person", camera=CAMERA, ground_z=0):
    local = camera[:3, :3].T @ (np.array([x, y, ground_z]) - camera[:3, 3])
    u = 400 + 400 * local[1] / local[0]
    v = 300 - 400 * local[2] / local[0]
    return Detection((u - 10, v - 40, u + 10, v), 0.9, label)


class GeometryTests(unittest.TestCase):
    def test_known_ground_points(self):
        for point in ((10, 0), (20, 5), (8, -3)):
            np.testing.assert_allclose(ground_position(box_at(*point), SIZE, 90, CAMERA), point)

    def test_yaw_translation_and_nonzero_plane(self):
        transform = np.eye(4)
        angle = math.radians(35)
        transform[:3, :3] = [[math.cos(angle), -math.sin(angle), 0],
                             [math.sin(angle), math.cos(angle), 0], [0, 0, 1]]
        transform[:3, 3] = [5, -2, 4]
        point = (15, 6)
        detection = box_at(*point, camera=transform, ground_z=1)
        np.testing.assert_allclose(ground_position(detection, SIZE, 90, transform, ground_z=1), point)

    def test_downward_pitch(self):
        transform = CAMERA.copy()
        angle = math.radians(20)
        transform[:3, :3] = [[math.cos(angle), 0, math.sin(angle)],
                             [0, 1, 0], [-math.sin(angle), 0, math.cos(angle)]]
        point = (10, 1)
        np.testing.assert_allclose(ground_position(box_at(*point, camera=transform), SIZE, 90, transform), point)

    def test_truncation_and_malformed_boxes(self):
        for bbox in ((0, 320, 420, 380), (380, 0, 420, 380),
                     (380, 320, 799, 380), (380, 320, 420, 599),
                     (420, 320, 380, 380), (380, 320, float("nan"), 380)):
            self.assertIsNone(ground_position(Detection(bbox, 0.9, "person"), SIZE, 90, CAMERA))

    def test_horizon_upward_and_distance(self):
        for bottom in (300, 299):
            self.assertIsNone(ground_position(Detection((380, 200, 420, bottom), .9, "person"), SIZE, 90, CAMERA))
        self.assertIsNone(ground_position(box_at(99, 0), SIZE, 90, CAMERA, max_distance=98))
        self.assertIsNotNone(ground_position(box_at(99, 0), SIZE, 90, CAMERA, max_distance=100))
        below = CAMERA.copy()
        below[2, 3] = -2
        self.assertIsNone(ground_position(box_at(10, 0), SIZE, 90, below))

    def test_invalid_calibration(self):
        bad = CAMERA.copy()
        bad[0, 0] = 2
        for transform in (np.eye(3), bad, np.full((4, 4), np.nan)):
            with self.assertRaises(ValueError):
                ground_position(box_at(10, 0), SIZE, 90, transform)
        for fov in (0, 180, float("nan")):
            with self.assertRaises(ValueError):
                ground_position(box_at(10, 0), SIZE, fov, CAMERA)


class TrackingTests(unittest.TestCase):
    def update(self, tracker, detections, timestamp):
        return tracker.update(detections, SIZE, 90, CAMERA, timestamp, frame_id=12)

    def test_motion_and_uncertainty(self):
        tracker = GroundTracker("camera-a")
        initial = self.update(tracker, [box_at(10, 0)], 0)[0]
        self.assertEqual(initial.velocity, (0, 0))
        self.assertGreaterEqual(initial.velocity_sigma_mps, 10)
        moved = self.update(tracker, [box_at(10.2, .1)], .1)[0]
        self.assertEqual(initial.object_id, moved.object_id)
        np.testing.assert_allclose(moved.velocity, (2, 1))
        self.assertGreaterEqual(moved.velocity_sigma_mps, 7)
        self.assertEqual((moved.source_id, moved.observed_at_s, moved.frame_id,
                          moved.perception_mode), ("camera-a", .1, 12, "rgb"))

    def test_association_is_independent_of_detection_order(self):
        tracker = GroundTracker("road")
        first = self.update(tracker, [box_at(10, -3), box_at(10, 3)], 0)
        second = self.update(tracker, [box_at(10.2, 3), box_at(10.2, -3)], .1)
        self.assertEqual([o.object_id for o in second], [first[1].object_id, first[0].object_id])

    def test_label_and_spatial_gates(self):
        tracker = GroundTracker("road")
        first = self.update(tracker, [box_at(10, 0)], 0)[0]
        second = self.update(tracker, [box_at(10, 0, "car"), box_at(30, 0)], .1)
        self.assertTrue(all(item.object_id != first.object_id for item in second))
        self.assertGreaterEqual(second[0].position_sigma_m, 2)

    def test_missed_frame_then_expiry(self):
        tracker = GroundTracker("road", max_age_s=.5)
        first = self.update(tracker, [box_at(10, 0)], 0)[0]
        self.assertEqual(self.update(tracker, [], .2), [])
        recovered = self.update(tracker, [box_at(10, 0)], .3)[0]
        self.assertEqual(first.object_id, recovered.object_id)
        self.update(tracker, [], .9)
        expired = self.update(tracker, [box_at(10, 0)], 1)[0]
        self.assertNotEqual(first.object_id, expired.object_id)

    def test_out_of_order_and_duplicate_leave_state_unchanged(self):
        tracker = GroundTracker("road")
        first = self.update(tracker, [box_at(10, 0)], 1)[0]
        for timestamp in (0, 1, float("nan")):
            with self.assertRaises(ValueError):
                self.update(tracker, [box_at(20, 0)], timestamp)
        valid = self.update(tracker, [box_at(10.2, 0)], 1.1)[0]
        self.assertEqual(first.object_id, valid.object_id)
        np.testing.assert_allclose(valid.velocity, [2, 0])

    def test_source_ids_are_independent(self):
        a = self.update(GroundTracker("a"), [box_at(10, 0)], 0)[0]
        b = self.update(GroundTracker("b"), [box_at(10, 0)], 0)[0]
        self.assertNotEqual(a.object_id, b.object_id)
        self.assertEqual(a.position, b.position)

    def test_robust_velocity_and_invalid_confidence(self):
        tracker = GroundTracker("a")
        for i, x in enumerate((10, 11, 13, 14)):
            observation = self.update(tracker, [box_at(x, 0)], i)[0]
        np.testing.assert_allclose(observation.velocity, [1, 0])
        self.assertGreaterEqual(observation.velocity_sigma_mps, 1)
        invalid = Detection(box_at(15, 0).bbox, float("nan"), "person")
        self.assertEqual(self.update(tracker, [invalid], 4), [])


if __name__ == "__main__":
    unittest.main()
