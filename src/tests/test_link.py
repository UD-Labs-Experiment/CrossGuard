"""Unit tests for SimulatedNetwork V2X communication module.

Tests expiry, packet loss, range limits, jitter reordering, duplicate rejection,
source/timestamp preservation, and parameter validation.
"""

from dataclasses import replace
import math
import unittest

from crossguard.communication.link import SimulatedNetwork, SimulatedLink
from crossguard.contracts import Message, Observation


class SimulatedNetworkTests(unittest.TestCase):
    def setUp(self):
        self.net = SimulatedNetwork(latency_s=0.15, jitter_s=0.0, loss=0.0, range_m=150.0, seed=7, max_hops=1)
        self.obs = Observation(
            object_id="ped_1",
            source_id="rsu_cam_1",
            observed_at_s=10.0,
            position=(120.0, 50.0),
            velocity=(0.0, -1.2),
            position_sigma_m=0.2,
            velocity_sigma_mps=0.1,
            confidence=0.95,
            label="person",
            perception_mode="ideal_instance_visible",
        )
        self.msg = Message(
            event_id="evt_001",
            sender_id="rsu",
            receiver_id="hero_car",
            sent_at_s=10.05,
            expires_at_s=11.5,
            observation=self.obs,
            hop_count=0,
            channel="v2i",
        )
        self.sender_pos = (120.0, 55.0)
        self.receiver_pos = (120.0, 10.0)  # distance = 45m < 150m

    def test_normal_delivery_and_source_preservation(self):
        """Message is delivered after latency_s, preserving all observation attributes."""
        res = self.net.send(self.msg, 10.05, self.sender_pos, self.receiver_pos)
        self.assertEqual(res["event"], "sent")
        self.assertEqual(res["sender_id"], "rsu")
        self.assertEqual(res["receiver_id"], "hero_car")
        self.assertEqual(res["source_id"], "rsu_cam_1")

        # Not yet arrived before latency (now_s = 10.15 < 10.05 + 0.15 = 10.20)
        delivered_early = self.net.receive("hero_car", 10.15)
        self.assertEqual(len(delivered_early), 0)

        # Arrives at latency (now_s = 10.20)
        delivered = self.net.receive("hero_car", 10.20)
        self.assertEqual(len(delivered), 1)
        msg_out = delivered[0]
        self.assertEqual(msg_out.event_id, "evt_001")
        self.assertEqual(msg_out.observation.observed_at_s, 10.0)
        self.assertEqual(msg_out.observation.source_id, "rsu_cam_1")
        self.assertEqual(msg_out.observation.object_id, "ped_1")
        self.assertEqual(msg_out.observation.position, (120.0, 50.0))
        self.assertEqual(msg_out.observation.velocity, (0.0, -1.2))
        self.assertEqual(msg_out.observation.perception_mode, "ideal_instance_visible")

        # Check recorded events
        event_types = [e["event"] for e in self.net.events]
        self.assertEqual(event_types, ["sent", "received"])

    def test_range_limit_drop(self):
        """Messages sent beyond range_m are dropped as out_of_range."""
        far_receiver = (120.0, 220.0)  # distance = 165m > 150m
        res = self.net.send(self.msg, 10.05, self.sender_pos, far_receiver)
        self.assertEqual(res["event"], "out_of_range")
        self.assertIn("exceeds_range", res.get("reason", ""))

        delivered = self.net.receive("hero_car", 10.50)
        self.assertEqual(len(delivered), 0)
        self.assertEqual(self.net.events[-1]["event"], "out_of_range")

    def test_packet_loss_deterministic(self):
        """Loss parameter deterministically drops packets based on seed."""
        lossy_net = SimulatedNetwork(latency_s=0.1, loss=0.5, seed=42)
        dropped_count = 0
        sent_count = 0
        total = 50

        for i in range(total):
            msg_i = replace(self.msg, event_id=f"loss_test_{i}")
            res = lossy_net.send(msg_i, 10.0, self.sender_pos, self.receiver_pos)
            if res["event"] == "dropped":
                dropped_count += 1
            elif res["event"] == "sent":
                sent_count += 1

        self.assertGreater(dropped_count, 10)
        self.assertGreater(sent_count, 10)
        self.assertEqual(dropped_count + sent_count, total)

        # Receiving only delivers the non-dropped packets
        received = lossy_net.receive("hero_car", 11.0)
        self.assertEqual(len(received), sent_count)

    def test_expiry_before_send_and_in_transit(self):
        """Expired messages are not delivered and recorded as expired."""
        # Expired before send
        expired_msg = replace(self.msg, sent_at_s=10.0, expires_at_s=10.1)
        res = self.net.send(expired_msg, 10.2, self.sender_pos, self.receiver_pos)
        self.assertEqual(res["event"], "expired")

        # Expired in transit
        transit_expired = replace(self.msg, event_id="transit_exp", sent_at_s=10.0, expires_at_s=10.12)
        res2 = self.net.send(transit_expired, 10.0, self.sender_pos, self.receiver_pos)
        self.assertEqual(res2["event"], "sent")  # queued with delivery at 10.15

        # At delivery time 10.15, expires_at_s (10.12) has passed
        delivered = self.net.receive("hero_car", 10.15)
        self.assertEqual(len(delivered), 0)

        # Event log should show expired
        expired_events = [e for e in self.net.events if e["event"] == "expired"]
        self.assertEqual(len(expired_events), 2)

    def test_jitter_and_sorted_queue_reordering(self):
        """Jitter can deliver later-sent packets earlier via priority queue."""
        # Latency 0.20 with jitter 0.08
        jitter_net = SimulatedNetwork(latency_s=0.20, jitter_s=0.08, seed=123)

        msg1 = replace(self.msg, event_id="msg_1")
        msg2 = replace(self.msg, event_id="msg_2")

        # Send msg1 at t=0.0
        jitter_net.send(msg1, 0.0, self.sender_pos, self.receiver_pos)
        # Send msg2 at t=0.02
        jitter_net.send(msg2, 0.02, self.sender_pos, self.receiver_pos)

        # Both will be delivered by t=0.5
        delivered = jitter_net.receive("hero_car", 0.5)
        self.assertEqual(len(delivered), 2)
        # Verify both messages are received
        event_ids = [m.event_id for m in delivered]
        self.assertIn("msg_1", event_ids)
        self.assertIn("msg_2", event_ids)

    def test_duplicate_delivery_rejection(self):
        """Duplicate event delivery to the same receiver is rejected."""
        # Send message once
        self.net.send(self.msg, 10.0, self.sender_pos, self.receiver_pos)
        # Send same message again (same event_id)
        self.net.send(self.msg, 10.01, self.sender_pos, self.receiver_pos)

        # First receive delivers first message
        d1 = self.net.receive("hero_car", 10.30)
        self.assertEqual(len(d1), 1)
        self.assertEqual(d1[0].event_id, "evt_001")

        # Duplicate event should be logged as 'duplicate'
        dup_events = [e for e in self.net.events if e["event"] == "duplicate"]
        self.assertEqual(len(dup_events), 1)
        self.assertEqual(dup_events[0]["event_id"], "evt_001")

        # Subsequent receive yields nothing new
        d2 = self.net.receive("hero_car", 10.35)
        self.assertEqual(len(d2), 0)

    def test_hop_limit_enforcement(self):
        """Hop count exceeding max_hops is dropped."""
        relayed_msg = replace(self.msg, hop_count=2)
        res = self.net.send(relayed_msg, 10.0, self.sender_pos, self.receiver_pos)
        self.assertEqual(res["event"], "dropped")
        self.assertIn("hop_limit", res.get("reason", ""))

        delivered = self.net.receive("hero_car", 10.5)
        self.assertEqual(len(delivered), 0)

    def test_finite_values_and_receiver_id_validation(self):
        """Non-finite positions, timestamps, or missing receiver IDs raise ValueError."""
        with self.assertRaises(ValueError):
            self.net.send(self.msg, float("nan"), self.sender_pos, self.receiver_pos)

        with self.assertRaises(ValueError):
            self.net.send(self.msg, 10.0, (float("inf"), 0.0), self.receiver_pos)

        with self.assertRaises(ValueError):
            self.net.send(self.msg, 10.0, self.sender_pos, (0.0, float("nan")))

        bad_msg = replace(self.msg, receiver_id="")
        with self.assertRaises(ValueError):
            self.net.send(bad_msg, 10.0, self.sender_pos, self.receiver_pos)

        with self.assertRaises(ValueError):
            self.net.receive("", 10.0)

        with self.assertRaises(ValueError):
            self.net.receive("hero_car", float("inf"))

    def test_network_parameter_validation(self):
        """Invalid construction parameters raise ValueError."""
        with self.assertRaises(ValueError):
            SimulatedNetwork(latency_s=-0.1)
        with self.assertRaises(ValueError):
            SimulatedNetwork(loss=1.5)
        with self.assertRaises(ValueError):
            SimulatedNetwork(range_m=-5.0)
        with self.assertRaises(ValueError):
            SimulatedNetwork(max_hops=-1)

    def test_reset_clears_queues_and_history(self):
        """Calling reset clears events and queues."""
        self.net.send(self.msg, 10.0, self.sender_pos, self.receiver_pos)
        self.assertGreater(len(self.net.events), 0)
        self.net.reset(seed=99)
        self.assertEqual(len(self.net.events), 0)
        delivered = self.net.receive("hero_car", 10.5)
        self.assertEqual(len(delivered), 0)


class BaselinePolicyTests(unittest.TestCase):
    def setUp(self):
        from crossguard.simulation.baseline_policy import BaselinePolicy
        self.policy = BaselinePolicy()

    def test_baseline_detects_conflict(self):
        # 3 observations of crossing pedestrian walking across vehicle lane at ~1.4 m/s
        self.assertIsNone(self.policy.observe(0.0, (10.0, 1.0), (0.0, 0.0), (10.0, 0.0)))
        self.assertIsNone(self.policy.observe(0.1, (10.0, 0.86), (1.0, 0.0), (10.0, 0.0)))
        warning = self.policy.observe(0.2, (10.0, 0.72), (2.0, 0.0), (10.0, 0.0))
        self.assertIsNotNone(warning)
        self.assertGreater(warning.predicted_conflict_in_s, 0.0)
        self.assertEqual(warning.source, "rsu")

        # Convert to contract message
        msg = warning.to_contract_message("hero_car", "v2i")
        self.assertEqual(msg.receiver_id, "hero_car")
        self.assertEqual(msg.channel, "v2i")
        self.assertEqual(msg.observation.label, "person")

    def test_baseline_filters_stationary(self):
        self.policy.observe(0.0, (10.0, 5.0), (0.0, 0.0), (10.0, 0.0))
        self.policy.observe(0.1, (10.0, 5.0), (1.0, 0.0), (10.0, 0.0))
        warning = self.policy.observe(0.2, (10.0, 5.0), (2.0, 0.0), (10.0, 0.0))
        self.assertIsNone(warning)


class CityWorldHelperTests(unittest.TestCase):
    def test_instance_counts_extraction(self):
        import numpy as np
        from crossguard.simulation.city_world import instance_counts

        # Synthetic CARLA BGRA instance buffer
        # Red channel == 12 is pedestrian class
        raw = np.zeros((10, 10, 4), dtype=np.uint8)
        # Actor ID 708: low byte = 196 (green), high byte = 2 (blue) -> 196 + (2 << 8) = 708
        raw[2:6, 2:6, 0] = 2    # Blue
        raw[2:6, 2:6, 1] = 196  # Green
        raw[2:6, 2:6, 2] = 12   # Red (class pedestrian)
        raw[2:6, 2:6, 3] = 255  # Alpha

        counts = instance_counts(raw)
        self.assertIn(708, counts)
        self.assertEqual(counts[708], 16)  # 4x4 = 16 pixels >= 8

    def test_closed_crosswalk_long_edges(self):
        import carla
        from crossguard.simulation.city_world import extract_crosswalk_polygons
        def ring(x):
            return [carla.Location(x=x+a, y=b) for a,b in [(0,0),(2,0),(2,20),(0,20),(0,0)]]
        polygons = extract_crosswalk_polygons(ring(0) + ring(50))
        self.assertEqual([len(p) for p in polygons], [4,4])
        self.assertEqual(polygons[0][2].y, 20)

    def test_extract_crosswalk_polygons(self):
        import carla
        from crossguard.simulation.city_world import extract_crosswalk_polygons

        # Two distinct polygons separated in space
        poly1 = [carla.Location(x=0.0, y=float(i), z=0.0) for i in range(4)]
        poly2 = [carla.Location(x=50.0, y=float(i), z=0.0) for i in range(4)]
        combined = poly1 + poly2

        polygons = extract_crosswalk_polygons(combined)
        self.assertEqual(len(polygons), 2)
        self.assertEqual(len(polygons[0]), 4)
        self.assertEqual(len(polygons[1]), 4)


if __name__ == "__main__":
    unittest.main()

