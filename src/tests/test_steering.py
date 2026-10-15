"""Exercise the production pure helper and exact control block without CARLA."""
import ast
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "crossguard/simulation/city_world.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
HELPER = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
              and node.name == "straight_corridor_control")
NAMESPACE = {"math": math}
exec(compile(ast.Module(body=[HELPER], type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)
CONTROL = NAMESPACE["straight_corridor_control"]


class SteeringTests(unittest.TestCase):
    def control(self, position=(10, 0), yaw=0, speed=11, end=100, forward=(1, 0)):
        return CONTROL(position, yaw, speed, (0, 0), forward, end)

    def test_aligned_lane_has_zero_steering_at_every_position(self):
        for x in (0, 10, 50):
            steer, stop = self.control((x, 0))
            self.assertAlmostEqual(steer, 0)
            self.assertFalse(stop)

    def test_cross_track_and_heading_feedback_sign(self):
        self.assertLess(self.control((10, .4))[0], 0)
        self.assertGreater(self.control((10, -.4))[0], 0)
        self.assertLess(self.control(yaw=10)[0], 0)
        self.assertGreater(self.control(yaw=-10)[0], 0)

    def test_rotated_translated_corridor_and_heading_wrap(self):
        steer, stop = CONTROL((5, 10), 90, 11, (5, 0), (0, 2), 100)
        self.assertAlmostEqual(steer, 0)
        self.assertFalse(stop)
        self.assertAlmostEqual(self.control((-10, 0), yaw=-179,
                                           forward=(-1, 0))[0], -1 / 45)

    def test_bounded_feedback_and_lateral_departure(self):
        steer, stop = self.control((10, 5), yaw=90)
        self.assertEqual(steer, -.25)
        self.assertTrue(stop)
        self.assertFalse(self.control((10, .64))[1])
        self.assertTrue(self.control((10, .66))[1])
        self.assertTrue(self.control(yaw=60)[1])
        self.assertTrue(self.control((-3, 0))[1])

    def test_guard_stops_before_endpoint_with_front_and_deceleration_margin(self):
        speed = 11
        stopping = speed * .15 + speed * speed / 8 + 2.3 + .5
        self.assertFalse(self.control((100 - stopping - .1, 0), speed=speed)[1])
        self.assertTrue(self.control((100 - stopping + .1, 0), speed=speed)[1])
        self.assertTrue(self.control((98, 0), speed=0)[1])

    def test_feedback_reduces_lateral_error_in_simple_bicycle_model(self):
        # Synthetic model validates feedback polarity/stability, not CARLA physics.
        x, y, yaw, speed, dt = 0., .5, 0., 8., .02
        for _ in range(300):
            steer, stop = self.control((x, y), math.degrees(yaw), speed, end=200)
            self.assertFalse(stop)
            yaw += speed / 2.8 * math.tan(steer * .6) * dt
            x += speed * math.cos(yaw) * dt
            y += speed * math.sin(yaw) * dt
        self.assertLess(abs(y), .04)

    def test_production_block_does_not_query_map_and_latches_stop_until_reset(self):
        city = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
                    and node.name == "CityWorld")
        tick = next(node for node in city.body if isinstance(node, ast.FunctionDef)
                    and node.name == "tick")
        def assigns(node, name):
            return (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))
        start = next(i for i, node in enumerate(tick.body) if assigns(node, "lane_origin"))
        end = next(i for i, node in enumerate(tick.body) if assigns(node, "walk_speed"))
        block = compile(ast.Module(body=tick.body[start:end], type_ignores=[]), str(SOURCE), "exec")
        origin = SimpleNamespace(x=0, y=0)
        transform = SimpleNamespace(location=origin,
                                    get_forward_vector=lambda: SimpleNamespace(x=1, y=0))
        car = SimpleNamespace(get_transform=lambda: SimpleNamespace(rotation=SimpleNamespace(yaw=0)),
                              bounding_box=SimpleNamespace(extent=SimpleNamespace(x=2.3, y=.95)))
        # Deliberately no map object: any waypoint query makes this test fail.
        world = SimpleNamespace(car_wp=SimpleNamespace(transform=transform, lane_width=3.5),
                                cross=SimpleNamespace(x=40, y=0), car=car, trial_number=1,
                                corridor_end_reserve_m=45.)
        namespace = {**NAMESPACE, "self": world, "c": SimpleNamespace(x=75, y=0),
                     "speed_mps": 11, "throttle_val": .5, "brake_val": 0}
        exec(block, namespace)
        self.assertEqual((namespace["throttle_val"], namespace["brake_val"]), (0, 1))
        namespace.update(speed_mps=0, throttle_val=.5, brake_val=0)
        exec(block, namespace)
        self.assertEqual((namespace["throttle_val"], namespace["brake_val"]), (0, 1))
        world.trial_number = 2
        namespace.update(c=SimpleNamespace(x=0, y=0), throttle_val=.5, brake_val=0)
        exec(block, namespace)
        self.assertEqual((namespace["throttle_val"], namespace["brake_val"]), (.5, 0))


if __name__ == "__main__":
    unittest.main()
