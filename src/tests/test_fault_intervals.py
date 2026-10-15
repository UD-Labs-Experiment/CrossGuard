import unittest
from crossguard.simulation.study_measurements import fault_intervals

class FaultIntervalTests(unittest.TestCase):
    def test_observed_endpoints_not_requested_times(self):
        events=[{'event':'fault_started','fault':'channel_0','at_s':.0167,'specification':{'start_s':0,'end_s':3}},
                {'event':'fault_started','fault':'observation_0','at_s':2.1},
                {'event':'fault_ended','fault':'channel_0','at_s':3.0167}]
        intervals=fault_intervals(events)
        self.assertEqual(intervals[0]['actual_start_s'],.0167)
        self.assertEqual(intervals[0]['actual_end_s'],3.0167)
        self.assertIsNone(intervals[1]['actual_end_s'])
