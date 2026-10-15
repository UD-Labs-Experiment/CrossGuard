import unittest
from types import SimpleNamespace
from crossguard.simulation.weather_guard import ensure_weather, WeatherReadinessError


class World:
    def __init__(self, readbacks=None):
        self.frame = 0
        self.writes = []
        self.readbacks = iter(readbacks) if readbacks is not None else None

    def set_weather(self, weather):
        self.writes.append(weather)

    def tick(self, timeout):
        self.frame += 1
        return self.frame

    def get_weather(self):
        value = next(self.readbacks) if self.readbacks is not None else self.writes[-1]
        return SimpleNamespace(**value)


class WeatherGuardTests(unittest.TestCase):
    def run_guard(self, world, expected=None):
        return ensure_weather(world, lambda **kw: kw,
                              expected or {'precipitation': 65., 'cloudiness': 100.})

    def test_rain_is_confirmed_on_two_fresh_frames_before_ready(self):
        world = World()
        proof = self.run_guard(world)
        self.assertEqual(proof['status'], 'VERIFIED')
        self.assertEqual(world.frame, 2)
        self.assertEqual(len(world.writes), 1)
        self.assertFalse(proof['recording_started'])

    def test_transient_mismatch_reapplies_exact_same_parameters(self):
        rain = {'precipitation': 65., 'cloudiness': 100.}
        world = World([{'precipitation': 0., 'cloudiness': 20.}] * 2 + [rain] * 2)
        proof = self.run_guard(world)
        self.assertEqual(proof['status'], 'VERIFIED')
        self.assertEqual(world.writes, [rain, rain])
        self.assertEqual(proof['verified_frame'], 4)

    def test_persistent_mismatch_is_bounded_and_never_ready(self):
        world = World([{'precipitation': 0., 'cloudiness': 20.}] * 6)
        with self.assertRaises(WeatherReadinessError) as caught:
            self.run_guard(world)
        self.assertEqual(len(world.writes), 3)
        self.assertEqual(world.frame, 6)
        self.assertEqual(caught.exception.report['status'], 'FAILED')
        self.assertFalse(caught.exception.report['recording_started'])

    def test_one_matching_frame_is_insufficient(self):
        world = World([{'cloudiness': 100.}, {'cloudiness': 20.}] * 3)
        with self.assertRaises(WeatherReadinessError):
            self.run_guard(world, {'cloudiness': 100.})

    def test_missing_or_nan_readback_cannot_pass(self):
        for actual in ({}, {'cloudiness': float('nan')}):
            with self.subTest(actual=actual), self.assertRaises(WeatherReadinessError):
                self.run_guard(World([actual] * 6), {'cloudiness': 100.})

    def test_nonadvancing_frames_cannot_pass(self):
        world = World()
        world.tick = lambda timeout: 1
        with self.assertRaises(WeatherReadinessError):
            self.run_guard(world)

    def test_rpc_failure_is_bounded(self):
        world = World()
        world.tick = lambda timeout: (_ for _ in ()).throw(TimeoutError('CARLA unavailable'))
        with self.assertRaises(WeatherReadinessError):
            self.run_guard(world)
        self.assertEqual(len(world.writes), 3)

    def test_invalid_request_is_rejected_without_server_write(self):
        for expected in ({}, None, {'cloudiness': float('nan')}, {'cloudiness': True}):
            world = World()
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                ensure_weather(world, lambda **kw: kw, expected)
            self.assertEqual(world.writes, [])
