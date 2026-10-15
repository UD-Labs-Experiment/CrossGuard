import unittest
from crossguard.simulation.placement import solve_projected_distance

class PlacementTests(unittest.TestCase):
    def test_curved_arc_distance_does_not_define_gap(self):
        pose,arc=solve_projected_distance(lambda d:[d],lambda d:d*.97,30.,30.)
        self.assertAlmostEqual(pose*.97,30.,delta=.01)
        self.assertGreater(arc,30.)
    def test_missing_route_fails_without_fallback(self):
        with self.assertRaises(RuntimeError):solve_projected_distance(lambda d:[],lambda d:d,30.,30.)
