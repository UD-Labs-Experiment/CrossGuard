"""Execute production weather/reset methods without a CARLA connection."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from crossguard.simulation.weather_guard import ensure_weather, WeatherReadinessError


SOURCE = Path(__file__).resolve().parents[1] / "crossguard/simulation/city_world.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
CITY = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
            and node.name == "CityWorld")
METHODS = [node for node in CITY.body if isinstance(node, ast.FunctionDef)
           and node.name in ("_apply_trial_weather", "reset")]
NAMESPACE = {"carla": SimpleNamespace(WeatherParameters=lambda **values: values),
             "ensure_weather": ensure_weather, "WeatherReadinessError": WeatherReadinessError}
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="ProductionMethods", bases=[],
     keywords=[], body=METHODS, decorator_list=[])], type_ignores=[])),
     str(SOURCE), "exec"), NAMESPACE)
ProductionMethods = NAMESPACE["ProductionMethods"]


class BeforeSpawn(Exception):
    pass


class WeatherTests(unittest.TestCase):
    def fixture(self, options, trial_number=1):
        city = ProductionMethods()
        calls = []
        frames = iter(range(1, 100))
        city.world = SimpleNamespace(set_weather=calls.append,
            tick=lambda timeout: next(frames),
            get_weather=lambda: SimpleNamespace(**calls[-1]))
        city._save_weather_readiness = lambda: None
        city.study_options = options
        city.trial_number = trial_number
        city.rows = []
        city.unit = []
        city.scenario = []
        city.destroy = lambda actors: (_ for _ in ()).throw(BeforeSpawn())
        return city, calls

    def test_declared_weather_has_one_write_and_no_default_write(self):
        for weather in ({"cloudiness": 10., "sun_altitude_angle": 70.,
                         "sun_azimuth_angle": 180., "precipitation": 0.},
                        {"cloudiness": 90., "precipitation": 70., "wetness": 80.}):
            with self.subTest(weather=weather):
                city, calls = self.fixture({"weather": weather})
                city._apply_trial_weather()
                self.assertEqual(calls, [weather])
                self.assertEqual(city.study_options, {"weather": weather})

    def test_missing_weather_keeps_previous_default(self):
        for options in ({}, {"driver_response_s": 1.}):
            city, calls = self.fixture(options)
            city._apply_trial_weather()
            self.assertEqual(calls, [{"cloudiness": 20., "sun_altitude_angle": 45.,
                                      "sun_azimuth_angle": 220.}])

    def test_invalid_declared_weather_does_not_silently_fall_back(self):
        city, calls = self.fixture({"weather": None})
        with self.assertRaises(ValueError):
            city._apply_trial_weather()
        self.assertEqual(calls, [])

    def test_initial_reset_does_not_duplicate_initialization_weather(self):
        city, calls = self.fixture({"weather": {"cloudiness": 10.}}, trial_number=0)
        with self.assertRaises(BeforeSpawn):
            city.reset()
        self.assertEqual(calls, [])

    def test_existing_world_reset_reapplies_weather_before_spawning(self):
        weather = {"cloudiness": 10., "sun_altitude_angle": 70.}
        city, calls = self.fixture({"weather": weather})
        destroyed = []
        city.destroy = destroyed.append
        city.pipeline = None
        city._save_weather_readiness = lambda: (_ for _ in ()).throw(BeforeSpawn())
        with self.assertRaises(BeforeSpawn):
            city.reset(enabled=False)
        self.assertEqual(calls, [weather])
        self.assertEqual(destroyed, [city.unit, city.scenario])

    def test_reload_reset_delegates_weather_to_new_world_initialization(self):
        city, calls = self.fixture({"weather": {"cloudiness": 10.}})
        city.town_name = "Town10HD"
        city.vehicle_count, city.walker_count, city.seed = 3, 0, 51101
        city.resolution, city.perception_mode = (1920, 1080), "rgb"
        city.receiver_mode = "autonomous"
        city.kwargs = {"study_options": city.study_options}
        events = []
        city.close = lambda: events.append("close")
        city.__init__ = lambda **kwargs: events.append(kwargs)
        city.reset(enabled=False, reload_world=True)
        self.assertEqual(calls, [])
        self.assertEqual(events[0], "close")
        self.assertEqual(events[1]["study_options"], city.study_options)
        self.assertFalse(events[1]["enabled"])

    def test_initialization_has_one_weather_application_before_blueprints(self):
        constructor = next(node for node in CITY.body if isinstance(node, ast.FunctionDef)
                           and node.name == "__init__")
        calls = [node for node in ast.walk(constructor) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        weather_calls = [node for node in calls
                         if node.func.attr in ("set_weather", "_apply_trial_weather")]
        self.assertEqual(len(weather_calls), 1)
        self.assertEqual(weather_calls[0].func.attr, "_apply_trial_weather")
        blueprints = next(node for node in calls
                          if node.func.attr == "get_blueprint_library")
        self.assertLess(weather_calls[0].lineno, blueprints.lineno)


if __name__ == "__main__":
    unittest.main()
