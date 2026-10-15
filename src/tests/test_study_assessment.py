import unittest
from crossguard.simulation.study_assessment import canonical_requested, is_cooperative_brake

class AssessmentTests(unittest.TestCase):
    def test_pair_ignores_only_assistance_and_artifact_labels(self):
        a={'enabled':False,'seed':7,'study_options':{'output':'off','labels':{'assistance':'OFF'},'followers':{'B':{'reaction_s':1.}}}}
        b={'enabled':True,'seed':7,'study_options':{'output':'on','labels':{'assistance':'ON'},'followers':{'B':{'reaction_s':1.}}}}
        self.assertEqual(canonical_requested(a),canonical_requested(b))
        b['study_options']['followers']['B']['reaction_s']=.5
        self.assertNotEqual(canonical_requested(a),canonical_requested(b))
        self.assertEqual(a['study_options']['labels']['assistance'],'OFF')

    def test_local_camera_braking_is_not_cooperative_warning(self):
        local={'brake':1.,'decision':{'sources':['driver']}}
        remote={'brake':1.,'decision':{'sources':['driver','bus']}}
        self.assertFalse(is_cooperative_brake(local,False))
        self.assertFalse(is_cooperative_brake(local,True))
        self.assertFalse(is_cooperative_brake(remote,False))
        self.assertTrue(is_cooperative_brake(remote,True))
