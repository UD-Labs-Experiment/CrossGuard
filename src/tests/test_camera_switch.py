"""Exercise production camera switching with sensors/world replaced by doubles."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1]/"crossguard/simulation/city_world.py"
TREE = ast.parse(SOURCE.read_text())
CITY = next(node for node in TREE.body if isinstance(node,ast.ClassDef) and node.name=="CityWorld")
METHOD = next(node for node in CITY.body if isinstance(node,ast.FunctionDef) and node.name=="set_view")


class CameraSwitchTests(unittest.TestCase):
    def test_switching_paused_camera_never_advances_physics_and_attaches_correct_vehicle(self):
        spawned=[]
        def stream(world, actors, pose, **kwargs):
            sensor=SimpleNamespace(stop=lambda:None,destroy=lambda:None)
            actors.append(sensor)
            spawned.append(kwargs)
            return SimpleNamespace(sensor=sensor)
        namespace={"Stream":stream}
        exec(compile(ast.Module(body=[METHOD],type_ignores=[]),str(SOURCE),"exec"),namespace)
        old=SimpleNamespace(stop=lambda:None,destroy=lambda:None)
        vehicle=object()
        def forbidden_tick():
            self.fail("Changing cameras advanced a paused world")
        world=SimpleNamespace(view_poses={"driver_b":object()},active_view="top",
            record_six_views=False,
            views={"top":SimpleNamespace(sensor=old)},scenario=[old],
            view_parents={"driver_b":vehicle},world=SimpleNamespace(tick=forbidden_tick),resolution=(640,360))
        namespace["set_view"](world,"driver_b")
        self.assertIs(spawned[0]["parent"],vehicle)
        self.assertEqual(world.active_view,"driver_b")
        self.assertNotIn(old,world.scenario)

    def test_recording_view_switch_keeps_all_sensors_alive(self):
        namespace={}
        exec(compile(ast.Module(body=[METHOD],type_ignores=[]),str(SOURCE),'exec'),namespace)
        views={name:object() for name in ('top','driver','person','opposite','driver_b','driver_c')}
        world=SimpleNamespace(view_poses=views,views=views,record_six_views=True,active_view='top')
        namespace['set_view'](world,'driver_c')
        self.assertEqual(world.active_view,'driver_c')
        self.assertIs(world.views,views)
