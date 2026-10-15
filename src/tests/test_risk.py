import unittest
from dataclasses import replace

from crossguard.contracts import Observation, VehicleState
from crossguard.policy.risk import RiskPolicy


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.policy = RiskPolicy()
        self.vehicle = VehicleState("car", (0, 0), (10, 0))
        self.person = Observation("ped", "rsu", 10, (15, 3), (0, -2),
                                  position_sigma_m=0.1, velocity_sigma_mps=0.05)

    def decide(self, observations=(), distance=15, occluded=False, **kwargs):
        return self.policy.evaluate(self.vehicle, list(observations), 10,
                                    distance, occluded, **kwargs)

    def test_crossing_predicts_body_contact(self):
        decision = self.decide([self.person])
        self.assertEqual(decision.state, "decelerating")
        self.assertGreater(decision.conflict_in_s, 0)
        self.assertLess(decision.conflict_in_s, 1.5)
        self.assertEqual(decision.sources, ("rsu",))

    def test_waiting_outside_lane_and_diverging_are_not_conflicts(self):
        for velocity in ((0, 0), (0, 2)):
            with self.subTest(velocity=velocity):
                self.assertEqual(self.decide([replace(self.person, velocity=velocity)]).state,
                                 "normal")

    def test_stationary_person_in_lane_is_hazard(self):
        decision = self.decide([replace(self.person, position=(10, 0), velocity=(0, 0))])
        self.assertEqual(decision.state, "braking")

    def test_age_propagates_position(self):
        old = replace(self.person, observed_at_s=9.5, position=(15, 4))
        result = self.decide([old])
        current = self.decide([replace(self.person, position_sigma_m=0.125)])
        self.assertAlmostEqual(result.conflict_in_s, current.conflict_in_s)
        self.assertEqual(result.observation_age_s, 0.5)

    def test_uncertainty_increases_hazard_envelope(self):
        near = replace(self.person, position=(15, 3), velocity=(0, 0))
        self.assertEqual(self.decide([near]).state, "normal")
        uncertain = replace(near, position_sigma_m=1)
        self.assertIsNotNone(self.decide([uncertain]).conflict_in_s)

    def test_stale_future_nonperson_and_low_confidence_ignored(self):
        invalid = [replace(self.person, observed_at_s=8.9),
                   replace(self.person, observed_at_s=10.01),
                   replace(self.person, label="vehicle"),
                   replace(self.person, confidence=0.1)]
        self.assertEqual(self.decide(invalid).state, "normal")
        self.assertEqual(self.decide(invalid, distance=40, occluded=True).state, "degraded")

    def test_duplicate_forwarding_does_not_corroborate(self):
        single = self.decide([self.person])
        duplicate = self.decide([self.person, self.person, self.person])
        self.assertEqual(single, duplicate)
        independent = self.decide([self.person, replace(self.person, source_id="peer")])
        self.assertEqual(independent.sources, ("peer", "rsu"))
        self.assertEqual(single.brake, independent.brake)

    def test_latest_source_sample_wins(self):
        old = replace(self.person, observed_at_s=9.5)
        new = replace(self.person, position=(15, 8), velocity=(0, 2))
        self.assertEqual(self.decide([old, new]).state, "normal")

    def test_fallback_slow_is_stopping_capable(self):
        self.vehicle = replace(self.vehicle, velocity=(2, 0))
        decision = self.decide(distance=7, occluded=True)
        self.assertEqual(decision.state, "degraded")
        target = decision.target_speed_mps
        demand = target * self.vehicle.response_time_s + target**2 / (2 * self.vehicle.deceleration_mps2)
        self.assertLessEqual(demand, 7 - self.vehicle.length_m / 2 - self.policy.stop_buffer_m + 1e-9)
        self.assertLessEqual(target, self.policy.cautious_speed_mps)

    def test_fallback_brakes_and_stale_warning_does_not_release_occlusion(self):
        self.assertEqual(self.decide(distance=5, occluded=True).state, "braking")
        stale = replace(self.person, observed_at_s=8)
        result = self.decide([stale], distance=5, occluded=True)
        self.assertEqual(result.target_speed_mps, 0)
        self.assertEqual(result.brake, 1)
        self.assertEqual(self.decide([stale], distance=-5, occluded=False).state, "normal")

    def test_negative_distance_stops_while_occluded(self):
        result = self.decide(distance=-1, occluded=True)
        self.assertEqual(result.state, "braking")
        self.assertEqual(result.target_speed_mps, 0)
        self.vehicle = replace(self.vehicle, velocity=(0, 0))
        self.assertEqual(self.decide(distance=-1, occluded=True).target_speed_mps, 0)

    def test_baseline_uses_local_observations_and_same_caution(self):
        self.assertEqual(self.decide([self.person], enabled=False).state, "normal")
        driver = replace(self.person, source_id="driver")
        self.assertEqual(self.decide([driver], enabled=False).state, "decelerating")
        for enabled in (True, False):
            self.assertEqual(self.decide(distance=5, occluded=True, enabled=enabled).state,
                             "braking")

    def test_longer_response_and_weaker_braking_reduce_margin(self):
        base = self.decide(distance=40, occluded=True).stopping_margin_m
        self.vehicle = replace(self.vehicle, response_time_s=1)
        slow_response = self.decide(distance=40, occluded=True).stopping_margin_m
        self.assertLess(slow_response, base)
        self.vehicle = replace(self.vehicle, deceleration_mps2=2)
        self.assertLess(self.decide(distance=40, occluded=True).stopping_margin_m, slow_response)

    def test_advisory_and_decelerating_states(self):
        self.vehicle = replace(self.vehicle, velocity=(2, 0))
        distant = replace(self.person, position=(12, 0), velocity=(0, 0))
        self.assertEqual(self.decide([distant]).state, "advisory")
        closer = replace(distant, position=(7, 0))
        self.assertEqual(self.decide([closer]).state, "decelerating")

    def test_commands_bounded(self):
        for distance in (-10, 0, 5, 40):
            for observations in ([], [self.person]):
                decision = self.decide(observations, distance, True)
                self.assertGreaterEqual(decision.brake, 0)
                self.assertLessEqual(decision.brake, 1)
                self.assertGreaterEqual(decision.target_speed_mps, 0)
                self.assertLessEqual(decision.target_speed_mps, self.policy.max_target_speed_mps)

    def test_invalid_vehicle_rejected(self):
        self.vehicle = replace(self.vehicle, deceleration_mps2=0)
        with self.assertRaises(ValueError):
            self.decide()


if __name__ == "__main__":
    unittest.main()
