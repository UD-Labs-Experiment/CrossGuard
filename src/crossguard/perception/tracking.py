"""Source-local RGB ground tracking with explicit empirical uncertainty.

IDs are meaningful only within a source; this does not fuse or identify tracks
across cameras. Deterministic greedy gated association uses label and predicted
ground position. Dense overlaps can swap identities: no appearance model exists.
"""
from collections import deque
from dataclasses import dataclass, field
import math
import numpy as np

from crossguard.contracts import Detection, Observation
from .geometry import ground_position


@dataclass
class _Track:
    object_id: str
    label: str
    position: np.ndarray
    timestamp: float
    sigma: float
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(2))
    velocity_sigma: float = 10.0
    differences: deque = field(default_factory=lambda: deque(maxlen=5))
    history: deque = field(default_factory=lambda: deque(maxlen=32))


class GroundTracker:
    def __init__(self, source_id, association_distance_m=3.0, max_age_s=1.0,
                 max_distance=100.0, max_speed_mps=40.0, velocity_window_s=0.0):
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a nonempty string")
        values = (association_distance_m, max_age_s, max_distance, max_speed_mps)
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Tracker distances, age and speed must be positive")
        if not math.isfinite(velocity_window_s) or velocity_window_s < 0:
            raise ValueError("Invalid velocity window")
        self.velocity_window_s = velocity_window_s
        self.source_id = source_id
        self.association_distance_m = association_distance_m
        self.max_age_s = max_age_s
        self.max_distance = max_distance
        self.max_speed_mps = max_speed_mps
        self._tracks = {}
        self._next_id = 1
        self._last_timestamp = None

    def update(self, detections, image_size, horizontal_fov, world_from_camera,
               timestamp_s, frame_id=0, ground_z=0):
        """Emit only current detections; retain missed tracks until max_age_s.

        Timestamps must strictly increase, including empty frames. Rejected
        out-of-order/duplicate frames leave state unchanged. Initial velocity is
        unknown (zero with 10 m/s sigma). Later velocities use the median of up
        to five finite differences; sigma includes pixel/position error and
        sample spread, so a single pair never implies precise motion.
        """
        if (not math.isfinite(timestamp_s) or
                (self._last_timestamp is not None and timestamp_s <= self._last_timestamp)):
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        candidates = []
        for detection in detections:
            if not math.isfinite(detection.confidence) or not 0 <= detection.confidence <= 1:
                continue
            position = ground_position(detection, image_size, horizontal_fov,
                                       world_from_camera, ground_z, self.max_distance)
            if position is None:
                continue
            position = np.array(position)
            # One-pixel bottom-point sensitivity plus an explicit modelling floor.
            sensitivities = []
            for offset in (-1.0, 1.0):
                box = (*detection.bbox[:3], detection.bbox[3] + offset)
                perturbed = ground_position(Detection(box, detection.confidence, detection.label),
                                            image_size, horizontal_fov, world_from_camera,
                                            ground_z, self.max_distance)
                if perturbed is not None:
                    sensitivities.append(float(np.linalg.norm(np.array(perturbed) - position)))
            floor = 0.5 if detection.label in ("person", "pedestrian") else 2.0
            sigma = max([floor, *sensitivities])
            candidates.append((detection, position, sigma))
        self._tracks = {key: track for key, track in self._tracks.items()
                        if timestamp_s - track.timestamp <= self.max_age_s}
        edges = []
        for key, track in self._tracks.items():
            dt = timestamp_s - track.timestamp
            predicted = track.position + track.velocity * dt
            gate = self.association_distance_m + min(5.0, track.velocity_sigma * dt)
            for index, (detection, position, _) in enumerate(candidates):
                distance = float(np.linalg.norm(position - predicted))
                displacement = float(np.linalg.norm(position - track.position))
                if (detection.label == track.label and distance <= gate
                        and displacement <= self.max_speed_mps * dt + self.association_distance_m):
                    edges.append((distance, key, index))
        assignments, used_tracks = {}, set()
        for _, key, index in sorted(edges):
            if key not in used_tracks and index not in assignments:
                assignments[index] = self._tracks[key]
                used_tracks.add(key)
        observations = []
        for index, (detection, position, sigma) in enumerate(candidates):
            track = assignments.get(index)
            if track is None:
                key = f"{self.source_id}:{self._next_id}"
                self._next_id += 1
                track = _Track(key, detection.label, position, timestamp_s, sigma)
                self._tracks[key] = track
                track.history.append((timestamp_s, position.copy(), sigma))
            else:
                dt = timestamp_s - track.timestamp
                difference = (position - track.position) / dt
                noise = math.hypot(sigma, track.sigma) / dt
                track.differences.append((difference, noise))
                samples = np.array([sample for sample, _ in track.differences])
                track.velocity = np.median(samples, axis=0)
                spread = float(np.max(np.linalg.norm(samples - track.velocity, axis=1)))
                track.velocity_sigma = max(1.0, spread,
                                           max(error for _, error in track.differences))
                if self.velocity_window_s and track.history:
                    while len(track.history) > 1 and timestamp_s - track.history[1][0] >= self.velocity_window_s:
                        track.history.popleft()
                    origin_time, origin_position, origin_sigma = track.history[0]
                    span = timestamp_s - origin_time
                    track.velocity = (position - origin_position) / span
                    track.velocity_sigma = max(1.0, math.hypot(sigma, origin_sigma) / span)
                    track.history.append((timestamp_s, position.copy(), sigma))
                track.position, track.timestamp, track.sigma = position, timestamp_s, sigma
            observations.append(Observation(
                object_id=track.object_id, source_id=self.source_id,
                observed_at_s=float(timestamp_s), position=tuple(map(float, position)),
                velocity=tuple(map(float, track.velocity)), position_sigma_m=sigma,
                velocity_sigma_mps=track.velocity_sigma,
                confidence=detection.confidence, label=detection.label,
                frame_id=frame_id, perception_mode="rgb"))
        self._last_timestamp = timestamp_s
        return observations
