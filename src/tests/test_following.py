import unittest
from dataclasses import replace
from crossguard.communication.braking import BrakingEvent
from crossguard.contracts import Decision
from crossguard.policy.following import AlongLaneState, FollowingPolicy, bumper_gap, group_recipient_ids, validate_following_gap


class FollowingTests(unittest.TestCase):
    def setUp(self):
        self.policy = FollowingPolicy()
        self.b = AlongLaneState('B', 0, 10, 4.6)
        self.a = AlongLaneState('A', 30, 10, 4.6)

    def test_body_clearance_not_actor_center_distance(self):
        self.assertAlmostEqual(bumper_gap(self.b, self.a), 25.4)

    def test_selected_gap_does_not_reopen_to_default_headway(self):
        for gap in (3., 5., 8., 12., 30.):
            leader = replace(self.a, position_m=gap + 4.6)
            for speed in (3., 10.):
                decision = FollowingPolicy(target_gap_m=gap).evaluate(
                    replace(self.b, speed_mps=speed), replace(leader, speed_mps=speed))
                self.assertAlmostEqual(decision.target_speed_mps, speed)
                self.assertEqual(decision.brake, 0.)

    def test_selected_gap_keeps_local_emergency_protection(self):
        policy = FollowingPolicy(target_gap_m=3.)
        leader = replace(self.a, position_m=7.6, speed_mps=0., acceleration_mps2=-6.)
        self.assertEqual(policy.evaluate(self.b, leader).brake, 1.)

    def test_invalid_dashboard_gaps_rejected(self):
        for value in (None, True, '5', -1., 2.9, 30.1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_following_gap(value)

    def test_normal_headway_does_not_stop_equal_speed_cars(self):
        d = self.policy.evaluate(self.b, self.a)
        self.assertEqual(d.state, 'normal')
        self.assertEqual(d.brake, 0)

    def test_local_obstacle_protection_without_cooperative_evidence(self):
        a = AlongLaneState('A', 8, 0, 4.6, -6)
        d = self.policy.evaluate(self.b, a)
        self.assertEqual(d.state, 'braking')
        self.assertEqual(d.sources, ())

    def test_early_evidence_helps_before_predecessor_decelerates(self):
        hazard = Decision('decelerating', 3, 0.8, 'Predicted pedestrian conflict', 20, 2,
                          0.2, ('rsu',))
        local = self.policy.evaluate(self.b, self.a)
        cooperative = self.policy.evaluate(self.b, self.a, hazard)
        self.assertLess(cooperative.target_speed_mps, local.target_speed_mps)
        self.assertEqual(cooperative.observation_age_s, 0.2)
        self.assertEqual(cooperative.sources, ('rsu',))

    def test_nonconflicting_remote_observation_does_not_brake(self):
        normal = Decision('normal', 11.1, 0, 'No conflict')
        self.assertEqual(self.policy.evaluate(self.b, self.a, normal).brake, 0)

    def test_fresh_predecessor_braking_event_anticipates_stopping_gap(self):
        b = AlongLaneState('B', 0, 15, 4.6)
        a = AlongLaneState('A', 18.6, 10, 4.6, 0)
        event = BrakingEvent('brake:A:1', 'A', 1., 2., 6., (18.6,0.), (10.,0.))
        local = self.policy.evaluate(b,a)
        remote = self.policy.evaluate(b,a,remote_braking=event,now_s=1.2)
        self.assertEqual(local.state,'decelerating')
        self.assertEqual(remote.state,'braking')
        self.assertLess(remote.target_speed_mps,local.target_speed_mps)
        self.assertLess(remote.stopping_margin_m,0.)
        self.assertEqual(remote.sources,('A',))
        self.assertAlmostEqual(remote.observation_age_s,.2)
        self.assertEqual(a.acceleration_mps2,0.,'Remote evidence must not mutate local measured state')

    def test_stale_expired_future_or_other_vehicle_braking_does_not_change_control(self):
        event = BrakingEvent('brake:A:1','A',1.,4.,6.,(30.,0.),(10.,0.))
        for remote,now in [(event,2.1),(replace(event,expires_at_s=1.1),1.2),
                           (event,.9),(replace(event,origin_id='other'),1.2),
                           (event,None)]:
            with self.subTest(origin=remote.origin_id,now=now):
                self.assertEqual(self.policy.evaluate(self.b,self.a),
                    self.policy.evaluate(self.b,self.a,remote_braking=remote,now_s=now))

    def test_remote_braking_does_not_replace_stronger_local_deceleration(self):
        a = replace(self.a,acceleration_mps2=-8.)
        event = BrakingEvent('brake:A:1','A',1.,2.,2.,(30.,0.),(10.,0.))
        local = self.policy.evaluate(self.b,a)
        remote = self.policy.evaluate(self.b,a,remote_braking=event,now_s=1.2)
        self.assertEqual(remote.target_speed_mps,local.target_speed_mps)
        self.assertEqual(remote.stopping_margin_m,local.stopping_margin_m)

    def test_bumper_overlap_is_an_emergency_even_without_remote_data(self):
        a = AlongLaneState('A', 4, 10, 4.6)
        self.assertEqual(self.policy.evaluate(self.b, a).brake, 1)

    def test_recipient_modes_and_system_off(self):
        self.assertEqual(group_recipient_ids('lead-only', True), ('hero_car',))
        self.assertEqual(len(group_recipient_ids('cooperative', True)), 3)
        self.assertEqual(group_recipient_ids('cooperative', False), ())

    def test_invalid_kinematics_rejected(self):
        with self.assertRaises(ValueError):
            AlongLaneState('B', float('nan'), 10, 4)
        with self.assertRaises(ValueError):
            FollowingPolicy(response_s=-1)


if __name__ == '__main__':
    unittest.main()
