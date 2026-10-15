import unittest
from tools.validate_communication_features import score_trial


class ExploratoryScoreTests(unittest.TestCase):
    def test_scores_contacts_minimum_gaps_first_brakes_and_guards(self):
        rows=[{'time_s':1.,'vehicles':{'follower_b':{'gap_m':10.,'brake':0.,'speed_kmh':20.}}},
              {'time_s':2.,'vehicles':{'follower_b':{'gap_m':3.,'brake':.2,'speed_kmh':15.,'corridor_guard':True}}}]
        metrics={'finished':True,'end_reason':'watchdog_timeout','pedestrian_collision':True,
                 'secondary_collision':True,'group_collisions':[{'other_id':42}],
                 'corridor_guard':True,'braking_started_s':1.2}
        score=score_trial(metrics,rows,[])
        self.assertEqual(score['followers']['follower_b']['minimum_gap_m'],3.)
        self.assertEqual(score['followers']['follower_b']['first_brake_s'],2.)
        self.assertTrue(score['followers']['follower_b']['corridor_guard'])
        self.assertTrue(score['pedestrian_collision'])
        self.assertTrue(score['secondary_collision'])
        self.assertTrue(score['timed_out'])
        self.assertEqual(score['follower_contacts'],[{'other_id':42}])
        self.assertIsNone(score['followers']['follower_c']['minimum_gap_m'])

    def test_missing_measurements_not_invented_as_zero(self):
        score=score_trial({},[],[])
        self.assertIsNone(score['hero_first_brake_s'])
        self.assertIsNone(score['followers']['follower_b']['first_brake_s'])
        self.assertFalse(score['finished'])


if __name__=='__main__':unittest.main()
