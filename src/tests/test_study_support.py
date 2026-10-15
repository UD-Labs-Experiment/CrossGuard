import unittest
from crossguard.experiments import experiment_config, trial_end_reason
from crossguard.simulation.contacts import ContactHistory

class StudySupportTests(unittest.TestCase):
    def test_predecessor_braking_cannot_bypass_delays(self):
        from dataclasses import replace
        from crossguard.policy.following import AlongLaneState,FollowingPolicy,SampledFollowingPolicy
        f=AlongLaneState('B',0,10,4.8)
        a=AlongLaneState('A',12.8,10,4.8)
        p=SampledFollowingPolicy(FollowingPolicy(target_gap_m=8),.1,.2,.8,.6)
        for i in range(15):
            t=i*.1
            lead=a if t < .5 else replace(a,speed_mps=0,acceleration_mps2=-9)
            d=p.evaluate(f,lead,now_s=t)
            if t < 1.5-1e-8:
                self.assertEqual(d.brake,0, f'Leaked predecessor braking at {t}')
        self.assertGreater(p.evaluate(f,replace(a,speed_mps=0,acceleration_mps2=-9),now_s=1.5).brake,0)
    def test_contact_never_uses_impact_frame(self):
        h=ContactHistory()
        h.remember(10,.1,{1:(10,0,0),2:(5,0,0)})
        h.remember(11,.2,{1:(0,0,0),2:(0,0,0)})
        m=h.measurement(11,1,2)
        self.assertEqual(m['frame'],10)
        self.assertEqual(m['relative_speed_mps'],5)
        self.assertTrue(h.measurement(10,1,2)['missing'])
    def test_window_does_not_stop_when_lead_stops(self):
        self.assertIsNone(trial_end_reason(5,2,False,True,20))
        self.assertEqual(trial_end_reason(20,2,False,True,20),'observation_window_complete')
    def test_rgb_town_and_unknown_fields_fail_closed(self):
        for params in ({'town':'Town03'},{'perception_mode':'ideal'},{'mystery':True}):
            with self.assertRaises(ValueError): experiment_config(params)
