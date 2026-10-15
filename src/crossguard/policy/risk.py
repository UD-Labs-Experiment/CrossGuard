"""Observation-only conflict policy; constant velocity is a demo assumption.

Crossing distance is signed distance from vehicle center to the near conflict
boundary along its travel direction. Source IDs refer to original sensors.
"""
import math
from crossguard.provenance import register_source
register_source(__name__,__file__)

from crossguard.contracts import Decision, Observation, VehicleState


class RiskPolicy:
    def __init__(self, horizon_s=5.0, max_observation_age_s=1.0,
                 max_target_speed_mps=13.9, cautious_speed_mps=3.0,
                 stop_buffer_m=1.5, person_radius_m=0.4,
                 uncertainty_multiplier=2.0, min_confidence=0.25,
                 local_source_ids=("driver", "driver_camera")):
        values = (horizon_s, max_observation_age_s, max_target_speed_mps,
                  cautious_speed_mps, stop_buffer_m, person_radius_m,
                  uncertainty_multiplier, min_confidence)
        if not all(math.isfinite(x) and x >= 0 for x in values):
            raise ValueError("Policy parameters must be finite and nonnegative")
        if horizon_s <= 0 or max_observation_age_s <= 0 or min_confidence > 1:
            raise ValueError("Invalid horizon, age limit, or confidence")
        self.horizon_s = horizon_s
        self.max_observation_age_s = max_observation_age_s
        self.max_target_speed_mps = max_target_speed_mps
        self.cautious_speed_mps = cautious_speed_mps
        self.stop_buffer_m = stop_buffer_m
        self.person_radius_m = person_radius_m
        self.uncertainty_multiplier = uncertainty_multiplier
        self.min_confidence = min_confidence
        self.local_source_ids = frozenset(local_source_ids)

    @staticmethod
    def _safe_speed(available, response, deceleration):
        # Invert v * response + v**2 / (2 * deceleration) <= available.
        if available <= 0:
            return 0.0
        return math.sqrt((deceleration * response) ** 2 +
                         2 * deceleration * available) - deceleration * response

    def _contact_time(self, vehicle, observation, age, heading):
        position = tuple(p + v * age for p, v in
                         zip(observation.position, observation.velocity))
        relative = tuple(p - q for p, q in zip(position, vehicle.position))
        velocity = tuple(p - q for p, q in
                         zip(observation.velocity, vehicle.velocity))
        sigma = self.uncertainty_multiplier * (
            observation.position_sigma_m + observation.velocity_sigma_mps * age)
        growth = self.uncertainty_multiplier * observation.velocity_sigma_mps
        axes = (heading, (-heading[1], heading[0]))
        radii = (vehicle.length_m / 2, vehicle.width_m / 2)
        low, high = 0.0, self.horizon_s
        # Intersection of linear inequalities for an expanding rectangular body.
        for axis, radius in zip(axes, radii):
            r = sum(x * y for x, y in zip(relative, axis))
            v = sum(x * y for x, y in zip(velocity, axis))
            allowance = radius + self.person_radius_m + sigma
            for slope, limit in ((v - growth, allowance - r),
                                 (-v - growth, allowance + r)):
                if abs(slope) < 1e-12:
                    if limit < 0:
                        return None
                elif slope > 0:
                    high = min(high, limit / slope)
                else:
                    low = max(low, limit / slope)
                if low > high:
                    return None
        return low if 0 <= low <= self.horizon_s else None

    def evaluate(self, vehicle: VehicleState, observations: list[Observation],
                 now_s: float, distance_to_crossing_m: float,
                 occluded: bool, enabled: bool = True) -> Decision:
        numbers = (*vehicle.position, *vehicle.velocity, vehicle.response_time_s,
                   vehicle.deceleration_mps2, vehicle.length_m, vehicle.width_m,
                   now_s, distance_to_crossing_m)
        if (not all(math.isfinite(x) for x in numbers) or
                vehicle.response_time_s < 0 or vehicle.deceleration_mps2 <= 0 or
                min(vehicle.length_m, vehicle.width_m) <= 0):
            raise ValueError("Invalid vehicle state, clock, or crossing distance")
        speed = math.hypot(*vehicle.velocity)
        heading = (tuple(v / speed for v in vehicle.velocity)
                   if speed > 1e-6 else (1.0, 0.0))
        stopping = (speed * vehicle.response_time_s +
                    speed ** 2 / (2 * vehicle.deceleration_mps2))
        available = (distance_to_crossing_m - vehicle.length_m / 2 -
                     self.stop_buffer_m)
        margin = available - stopping
        local_sources = self.local_source_ids | {vehicle.vehicle_id}
        fresh = {}
        for observation in observations:
            age = now_s - observation.observed_at_s
            if (observation.label != "person" or age < 0 or
                    age > self.max_observation_age_s or
                    observation.confidence < self.min_confidence or
                    not observation.source_id or not observation.object_id or
                    (not enabled and observation.source_id not in local_sources)):
                continue
            # Forwarded copies retain original source identity. Latest sample per
            # source/object wins; duplicate copies never increase confidence.
            key = (observation.source_id, observation.object_id)
            previous = fresh.get(key)
            if previous is None or observation.observed_at_s > previous.observed_at_s:
                fresh[key] = observation
        conflicts = []
        for observation in fresh.values():
            age = now_s - observation.observed_at_s
            contact = self._contact_time(vehicle, observation, age, heading)
            if contact is not None:
                conflicts.append((contact, age, observation.source_id))
        if conflicts:
            contact, age, _ = min(conflicts)
            # Contact time already includes vehicle body and uncertainty.
            conflict_available = max(0.0, speed * contact - self.stop_buffer_m)
            conflict_margin = conflict_available - stopping
            sources = tuple(sorted({source for _, _, source in conflicts}))
            state = "braking" if conflict_margin <= 0 else "decelerating"
            if contact > 3 and conflict_margin > stopping:
                state = "advisory"
            target = min(self.max_target_speed_mps, self.cautious_speed_mps,
                         self._safe_speed(conflict_available,
                                          vehicle.response_time_s,
                                          vehicle.deceleration_mps2))
            if state == "braking":
                target = 0.0
            brake = (1.0 if state == "braking" else
                     min(1.0, max(0.0, (speed - target) /
                                  vehicle.deceleration_mps2)))
            return Decision(state, target, brake, "Predicted pedestrian conflict",
                            conflict_margin, contact, age, sources)
        if occluded:
            # A fresh nonconflicting detection is not proof that the blind area
            # is empty. This branch also persists after a warning expires.
            target = min(self.max_target_speed_mps, self.cautious_speed_mps,
                         self._safe_speed(available, vehicle.response_time_s,
                                          vehicle.deceleration_mps2))
            brake = min(1.0, max(0.0, (speed - target) /
                                 vehicle.deceleration_mps2))
            state = "braking" if speed > 0 and margin <= 0 else "degraded"
            if state == "braking":
                target, brake = 0.0, 1.0
            return Decision(state, target, brake,
                            "Occluded crossing: stopping-capable cautious approach",
                            margin, sources=tuple(sorted({o.source_id for o in fresh.values()})))
        return Decision("normal", self.max_target_speed_mps, 0.0,
                        "Unobstructed with no predicted conflict", margin)
