import unittest
from crossguard.simulation.encounter_timing import arrival_time, predict_encounter, EncounterTrigger

class TimingTests(unittest.TestCase):
    def test_normal_walking_startup_is_predicted_without_retiming(self):
        p=predict_encounter((0,0),(40,6),(0,0),(1,0),(0,1),10,0,10,1.5,0,pedestrian_acceleration_mps2=2.5)
        self.assertAlmostEqual(p['pedestrian_predicted_arrival_after_s'],4.3)
        short=predict_encounter((0,0),(40,.1),(0,0),(1,0),(0,1),10,0,10,1.5,0,pedestrian_acceleration_mps2=2.5)
        self.assertAlmostEqual(short['pedestrian_predicted_arrival_after_s'],(.2/2.5)**.5)
    def test_constant_speed_and_acceleration_cap(self):
        self.assertEqual(arrival_time(50,10,0,12),5)
        self.assertAlmostEqual(arrival_time(44,8,2,12),4)
    def test_lane_intersection_and_one_shot(self):
        p=predict_encounter((0,0),(40,6),(0,0),(1,0),(0,1),10,0,10,1.5,0)
        self.assertEqual(p['conflict_center_xy_m'],[40,0])
        t=EncounterTrigger();e=t.update(p,2,100,0)
        self.assertEqual(e['A_predicted_arrival_s'],6)
        self.assertIsNone(t.update(p,10,200,1))
        self.assertEqual(t.event,e)
    def test_missed_window_and_braking_do_not_retime(self):
        p={'arrival_difference_s':-.4,'A_speed_mps':10}
        t=EncounterTrigger();self.assertIsNone(t.update(p,1,10,0));self.assertIsNotNone(t.failure)
        p['arrival_difference_s']=0
        self.assertIsNone(t.update(p,2,20,0))
        t=EncounterTrigger();self.assertIsNone(t.update(p,1,10,1));self.assertIsNotNone(t.failure)
    def test_initial_rest_brake_does_not_invalidate(self):
        t=EncounterTrigger();self.assertIsNone(t.update({'arrival_difference_s':None,'A_speed_mps':0},0,0,1))
        self.assertIsNone(t.failure)

    def test_oblique_walker_intersects_actual_lane(self):
        p=predict_encounter((0,0),(40,6),(0,0),(1,0),(.1,.995),10,0,10,1.5,0)
        self.assertAlmostEqual(p['conflict_center_xy_m'][1],0)
        self.assertAlmostEqual(p['conflict_center_xy_m'][0],40-6*.1/.995)
