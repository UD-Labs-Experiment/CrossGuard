import unittest
from tools.town10_analyze_robustness import metrics, paired_outcome, summarize

class AnalysisTests(unittest.TestCase):
    def result(self):
        return {'technical_failure':False,'pedestrian_contacts':[],
            'secondary':{k:{'contact':False,'events':[],'minimum_physical_bumper_gap_m':.5} for k in ('A_B','B_C')},
            'timeline':{'hero_car':{'first_brake_command_s':0.,'brake_command_duration_s':2.,'guard_first_s':5.}},
            'unnecessary_braking':True}

    def test_repeated_callbacks_count_as_one_contact_run(self):
        value=self.result();value['pedestrian_contacts']=[{'precontact':{'time_s':3.}}]*4
        result=metrics(value)
        self.assertTrue(result['pedestrian_contact']);self.assertTrue(result['any_contact'])

    def test_guard_separation_retains_late_contacts(self):
        value=self.result();value['secondary']['B_C'].update(contact=True,events=[{'precontact':{'time_s':6.}}])
        result=metrics(value)
        self.assertTrue(result['B_C_contact']);self.assertFalse(result['contacts_before_hero_guard']['any_contact'])
        self.assertTrue(result['contacts_after_hero_guard']['any_contact'])

    def test_nuisance_label_does_not_become_contact_or_false_alarm_measure(self):
        result=metrics(self.result())
        self.assertTrue(result['nuisance_configuration_label']);self.assertFalse(result['any_contact'])
        self.assertNotIn('false_alarm',result)

    def test_paired_changes_do_not_hide_introduced_contacts(self):
        pairs=[{'OFF':{'x':off},'ON':{'x':on}} for off,on in [(True,False),(False,True),(True,True),(False,False)]]
        self.assertEqual(paired_outcome(pairs,'x'),{'OFF':2,'ON':2,'avoided':1,'introduced':1,'persisted':1,'absent_both':1})

    def test_zero_brake_time_is_comparable(self):
        off=metrics(self.result());on={**off,'hero_first_brake_s':1.}
        summary=summarize([{'OFF':off,'ON':on}])
        self.assertEqual(summary['hero_brake_advance_s_median'],-1.)
        self.assertEqual(summary['hero_brake_advance_comparable_pairs'],1)

    def test_failure_is_excluded_not_qualified_as_safe(self):
        result=self.result();result['technical_failure']=True
        with self.assertRaises(ValueError):metrics(result)

if __name__=='__main__':unittest.main()
