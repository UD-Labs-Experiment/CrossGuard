"""Shared, simulator-independent contracts. SI units and simulation seconds.

World coordinates use CARLA's X/Y road plane. Original observation timestamps
and source IDs survive forwarding. Confidence is detector confidence, not trust.
"""
from dataclasses import dataclass, asdict
from typing import Optional
import math


@dataclass(frozen=True)
class Detection:
    bbox: tuple[float, float, float, float]
    confidence: float
    label: str


@dataclass(frozen=True)
class Observation:
    object_id: str
    source_id: str
    observed_at_s: float
    position: tuple[float, float]
    velocity: tuple[float, float] = (0.0, 0.0)
    position_sigma_m: float = 0.5
    velocity_sigma_mps: float = 0.5
    confidence: float = 1.0
    label: str = "person"
    frame_id: int = 0
    perception_mode: str = "rgb"

    def __post_init__(self):
        numbers = (self.observed_at_s, *self.position, *self.velocity,
                   self.position_sigma_m, self.velocity_sigma_mps, self.confidence)
        if not all(math.isfinite(x) for x in numbers):
            raise ValueError("Observation values must be finite")
        if min(self.position_sigma_m, self.velocity_sigma_mps) < 0 or not 0 <= self.confidence <= 1:
            raise ValueError("Invalid observation uncertainty/confidence")


@dataclass(frozen=True)
class VehicleState:
    vehicle_id: str
    position: tuple[float, float]
    velocity: tuple[float, float]
    response_time_s: float = 0.2
    deceleration_mps2: float = 6.0
    length_m: float = 4.6
    width_m: float = 1.9


@dataclass(frozen=True)
class Message:
    event_id: str
    sender_id: str
    receiver_id: str
    sent_at_s: float
    expires_at_s: float
    observation: Observation
    hop_count: int = 0
    channel: str = "v2i"

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    state: str
    target_speed_mps: float
    brake: float
    reason: str
    stopping_margin_m: Optional[float] = None
    conflict_in_s: Optional[float] = None
    observation_age_s: Optional[float] = None
    sources: tuple[str, ...] = ()


VIEW_NAMES = ("top", "driver", "person", "opposite", "driver_b", "driver_c")

# CityWorld interface for viewer: tick()->dict[view,RGB array], metrics()->dict,
# reset(enabled), close(); attributes enabled,time,finished,output,rows,messages.
# Message events use: event, at_s, sender_id, receiver_id, event_id, channel,
# observed_at_s, expires_at_s, source_id; observations remain immutable.
