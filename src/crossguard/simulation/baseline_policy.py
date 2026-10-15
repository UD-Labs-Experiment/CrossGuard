"""Baseline conflict detection policy adapted from DesertEye proposal.

SI units and simulation seconds.
Preserves baseline trajectory-based TTC calculation and warning generation.
"""

from collections import deque
from dataclasses import asdict, dataclass
import math
from typing import Optional, Tuple

from crossguard.contracts import Message, Observation


@dataclass(frozen=True)
class BaselineWarning:
    sequence: int
    observed_at_s: float
    expires_at_s: float
    predicted_conflict_in_s: float
    predicted_center_distance_m: float
    pedestrian_position: Tuple[float, float]
    pedestrian_velocity: Tuple[float, float]
    receiver: str = "approaching_car"
    action: str = "brake"
    source: str = "desert_eye_rsu"

    def to_dict(self):
        return asdict(self)

    def to_contract_message(self, receiver_id: str = "hero_car", channel: str = "v2i") -> Message:
        obs = Observation(
            object_id=f"ped_{self.sequence}",
            source_id=self.source,
            observed_at_s=self.observed_at_s,
            position=self.pedestrian_position,
            velocity=self.pedestrian_velocity,
            position_sigma_m=0.4,
            velocity_sigma_mps=0.3,
            confidence=0.9,
            label="person",
            perception_mode="ideal_instance_visible",
        )
        return Message(
            event_id=f"{self.source}_warn_{self.sequence}",
            sender_id=self.source,
            receiver_id=receiver_id,
            sent_at_s=self.observed_at_s,
            expires_at_s=self.expires_at_s,
            observation=obs,
            channel=channel,
        )


class BaselinePolicy:
    """Minimal baseline policy: observed positions -> linear velocity -> predicted TTC conflict."""

    def __init__(
        self,
        horizon_s: float = 4.0,
        conflict_radius_m: float = 2.0,
        ttl_s: float = 1.0,
        min_interval_s: float = 0.1,
        source_id: str = "rsu",
    ):
        self.horizon_s = horizon_s
        self.conflict_radius_m = conflict_radius_m
        self.ttl_s = ttl_s
        self.min_interval_s = min_interval_s
        self.source_id = source_id
        self.track: deque[Tuple[float, Tuple[float, float]]] = deque(maxlen=5)
        self.last_message_s = -math.inf
        self.sequence = 0

    def reset(self):
        self.track.clear()
        self.last_message_s = -math.inf
        self.sequence = 0

    def observe(
        self,
        timestamp_s: float,
        pedestrian_position: Optional[Tuple[float, float]],
        vehicle_position: Tuple[float, float],
        vehicle_velocity: Tuple[float, float],
    ) -> Optional[BaselineWarning]:
        if pedestrian_position is None:
            # Never predict indefinitely through unobserved occlusion
            self.track.clear()
            return None
        if self.track and timestamp_s <= self.track[-1][0]:
            raise ValueError("Observation timestamps must increase")

        self.track.append((timestamp_s, pedestrian_position))
        if len(self.track) < 3:
            return None

        old_t, old_p = self.track[0]
        dt = timestamp_s - old_t
        if dt <= 1e-6:
            return None

        pv = (
            (pedestrian_position[0] - old_p[0]) / dt,
            (pedestrian_position[1] - old_p[1]) / dt,
        )

        # Stationary person outside lane is not a crossing hazard
        if math.hypot(*pv) < 0.25:
            return None

        r = (
            pedestrian_position[0] - vehicle_position[0],
            pedestrian_position[1] - vehicle_position[1],
        )
        rv = (
            pv[0] - vehicle_velocity[0],
            pv[1] - vehicle_velocity[1],
        )
        v2 = rv[0] * rv[0] + rv[1] * rv[1]
        if v2 < 1e-6:
            return None

        ttc = -(r[0] * rv[0] + r[1] * rv[1]) / v2
        if not (0.0 < ttc <= self.horizon_s):
            return None

        separation = math.hypot(r[0] + rv[0] * ttc, r[1] + rv[1] * ttc)
        if separation > self.conflict_radius_m or (timestamp_s - self.last_message_s < self.min_interval_s - 1e-6):
            return None

        self.sequence += 1
        self.last_message_s = timestamp_s
        return BaselineWarning(
            sequence=self.sequence,
            observed_at_s=timestamp_s,
            expires_at_s=timestamp_s + self.ttl_s,
            predicted_conflict_in_s=ttc,
            predicted_center_distance_m=separation,
            pedestrian_position=pedestrian_position,
            pedestrian_velocity=pv,
            source=self.source_id,
        )


# Compatibility alias
DesertEye = BaselinePolicy
Warning = BaselineWarning
