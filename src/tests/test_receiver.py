import unittest

from crossguard.contracts import Decision, Message, Observation
from crossguard.simulation.receiver import ReceiverController


NORMAL = Decision("normal", 11.1, 0, "clear")
BRAKE = Decision("braking", 0, 1, "conflict", -2, 0.5, 0.1, ("rsu",))
CAUTION = Decision("degraded", 3, 0.3, "blind crossing")
MSG = Message("packet1", "rsu", "hero_car", 0, 3,
              Observation("ped", "rsu", 0, (10, 0)))


class ReceiverTests(unittest.TestCase):
    def test_waiting_driver_keeps_configured_cruise_for_visual_and_remote_risk(self):
        receiver=ReceiverController('retrofit',1.,cruise_mps=40/3.6)
        sighting=Observation('ped','driver',0.,(10,0))
        visual,_=receiver.local_visual_command(0.,BRAKE,sighting)
        self.assertAlmostEqual(visual.target_speed_mps,40/3.6)
        receiver=ReceiverController('retrofit',1.,cruise_mps=20/3.6)
        remote,_=receiver.command(0.,BRAKE,NORMAL,[(MSG,BRAKE)],True)
        self.assertAlmostEqual(remote.target_speed_mps,20/3.6)
        for speed in (True,0,float('nan'),float('inf')):
            with self.subTest(speed=speed),self.assertRaises(ValueError):
                ReceiverController(cruise_mps=speed)

    def test_visual_sighting_survives_missing_frames_until_delayed_braking(self):
        receiver = ReceiverController("retrofit", 1.)
        sighting = Observation("ped", "driver", 0.1, (10., 0.))
        decision, events = receiver.local_visual_command(.2, NORMAL, sighting)
        self.assertEqual(decision.brake, 0.)
        self.assertEqual(events[0]['event'], 'local_hazard_seen')
        self.assertEqual(events[0]['observed_at_s'], .1)
        decision, _ = receiver.local_visual_command(1.19, NORMAL)
        self.assertEqual(decision.brake, 0.)
        decision, events = receiver.local_visual_command(1.21, NORMAL)
        self.assertEqual(decision.brake, 1.)
        self.assertEqual(decision.state, 'braking')
        self.assertEqual(decision.sources, ('driver',))
        self.assertAlmostEqual(decision.observation_age_s, 1.11)
        self.assertEqual(events[0]['event'], 'local_driver_response')
        self.assertGreaterEqual(events[0]['driver_response_delay_s'], 1.)

    def test_late_local_conflict_clearing_does_not_erase_intent_to_stop(self):
        receiver = ReceiverController("retrofit", 1.)
        sighting = Observation("ped", "driver", 0., (2., 0.))
        receiver.local_visual_command(0., BRAKE, sighting)
        receiver.local_visual_command(.5, NORMAL)
        decision, events = receiver.local_visual_command(1., NORMAL)
        self.assertEqual(decision.brake, 1.)
        self.assertEqual(events[0]['event'], 'local_driver_response')
        decision, events = receiver.local_visual_command(2., NORMAL)
        self.assertEqual(decision.brake, 1.)
        self.assertEqual(events, [])

    def test_repeated_sightings_do_not_restart_visual_response_delay(self):
        receiver = ReceiverController("retrofit", 1.)
        for now in (0., .3, .6, .9, 1.):
            decision, _ = receiver.local_visual_command(now, NORMAL,
                Observation("ped", "driver", now, (10., 0.)))
        self.assertEqual(decision.brake, 1.)
        self.assertEqual(receiver.visual_seen_at, 0.)

    def test_remote_stale_future_and_nonperson_inputs_do_not_trigger_local_brake(self):
        for source, timestamp, label, confidence in (
                ('rsu', 0., 'person', 1.), ('driver', -2., 'person', 1.),
                ('driver', 1., 'person', 1.), ('driver', 0., 'vehicle', 1.),
                ('driver', 0., 'person', .1)):
            receiver = ReceiverController('retrofit', 0.)
            decision, events = receiver.local_visual_command(0., NORMAL,
                Observation('ped', source, timestamp, (10., 0.), label=label, confidence=confidence))
            self.assertEqual(decision.brake, 0.)
            self.assertEqual(events, [])

    def test_new_trial_receiver_clears_remembered_visual_stop(self):
        old = ReceiverController('retrofit', 0.)
        old.local_visual_command(0., NORMAL, Observation('ped', 'driver', 0., (10., 0.)))
        fresh = ReceiverController('retrofit', 0.)
        self.assertEqual(fresh.local_visual_command(1., NORMAL)[0], NORMAL)

    def test_autonomous_visual_path_preserves_policy(self):
        receiver = ReceiverController('autonomous')
        self.assertEqual(receiver.local_visual_command(0., NORMAL,
            Observation('ped', 'driver', 0., (10., 0.)))[0], NORMAL)

    def test_autonomous_immediate_and_disabled_local_only(self):
        receiver = ReceiverController()
        self.assertEqual(receiver.command(0, BRAKE, NORMAL)[0], BRAKE)
        self.assertEqual(receiver.command(0, BRAKE, NORMAL, [(MSG, BRAKE)], False)[0], NORMAL)

    def test_retrofit_actual_receipt_audio_then_delayed_driver(self):
        receiver = ReceiverController("retrofit", 1)
        decision, events = receiver.command(0.2, BRAKE, NORMAL, [(MSG, BRAKE)])
        self.assertEqual(decision.brake, 0)
        self.assertEqual([event["event"] for event in events],
                         ["notification_created", "notification_delivered"])
        self.assertEqual(events[0]["event_id"], MSG.event_id)
        decision, events = receiver.command(1.19, BRAKE, NORMAL)
        self.assertEqual(decision.brake, 0)
        decision, events = receiver.command(1.21, BRAKE, NORMAL)
        self.assertEqual(decision.brake, 1)
        self.assertEqual(events[0]["event"], "driver_response")
        self.assertGreaterEqual(events[0]["driver_response_delay_s"], 1)

    def test_no_delivery_or_nonconflict_produces_no_audio(self):
        receiver = ReceiverController("retrofit")
        _, events = receiver.command(0, CAUTION, CAUTION, [(MSG, NORMAL)])
        self.assertEqual(events, [])
        _, events = receiver.command(1.1, CAUTION, CAUTION)
        self.assertEqual(events[0]["event"], "local_driver_response")

    def test_duplicate_message_does_not_repeat_audio(self):
        receiver = ReceiverController("retrofit")
        receiver.command(0, BRAKE, NORMAL, [(MSG, BRAKE)])
        _, events = receiver.command(0.1, BRAKE, NORMAL, [(MSG, BRAKE)])
        self.assertEqual(events, [])

    def test_existing_local_response_does_not_bypass_audio_delay(self):
        receiver = ReceiverController("retrofit")
        receiver.command(0, CAUTION, CAUTION)
        receiver.command(1.1, CAUTION, CAUTION)
        decision, _ = receiver.command(2, BRAKE, CAUTION, [(MSG, BRAKE)])
        self.assertEqual(decision.brake, CAUTION.brake)
        decision, events = receiver.command(3.01, BRAKE, CAUTION)
        self.assertEqual(decision.brake, BRAKE.brake)
        self.assertEqual(events[0]["event"], "driver_response")

    def test_warning_expires_blind_caution_persists_and_clear_releases(self):
        receiver = ReceiverController("retrofit")
        receiver.command(0, BRAKE, CAUTION, [(MSG, BRAKE)])
        decision, _ = receiver.command(1.1, CAUTION, CAUTION)
        self.assertEqual(decision.state, "degraded")
        decision, events = receiver.command(2, NORMAL, NORMAL)
        self.assertEqual(decision.state, "normal")
        self.assertEqual(receiver.advisory_state, "clear")
        self.assertEqual(events[0]["event"], "notification_cleared")

    def test_baseline_has_no_cooperative_notification(self):
        receiver = ReceiverController("retrofit")
        decision, events = receiver.command(0, BRAKE, NORMAL, [(MSG, BRAKE)], False)
        self.assertEqual(decision, NORMAL)
        self.assertEqual(events, [])

    def test_invalid_mode_and_delay(self):
        for mode, delay in (("phone", 1), ("retrofit", -1)):
            with self.assertRaises(ValueError):
                ReceiverController(mode, delay)
