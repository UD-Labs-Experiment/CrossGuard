"""Measured efficiency remains unknown without the evidence its claim needs."""
import json
from pathlib import Path
import tempfile
import unittest
from tools import town10_efficiency_report as report


def vehicle(speed=10.,location=(0.,0.,0.),brake=0.,sources=None,age=None,guard=False,gap=None):
    return {'speed_mps':speed,'along_speed_mps':speed,'location':location,
            'yaw_deg':0.,'half_length_m':2.,'brake':brake,'guard':guard,'gap_m':gap,
            'decision':{'sources':sources or [],'observation_age_s':age}}


def reference(contact=True):
    rows=[{'frame':1,'time_s':1.,'vehicles':{'hero_car':vehicle(),'follower_b':vehicle(8.,gap=6.)}},
          {'frame':2,'time_s':11.,'vehicles':{'hero_car':vehicle(location=(100.,0.,0.))}}]
    return {'result':{'pedestrian_contacts':[{'frame':2,'vehicle_id':'hero_car'}] if contact else [],'secondary':{}},
            'rows':rows,'encounter':{'trigger':{'at_s':1.,'conflict_center_xy_m':[100.,0.]}},
            'messages':[],'folder':Path('missing'), 'complete':True}


def warning_run(guard=False,source='bus',age=.2,capture=.8):
    observation={'object_id':'ped','source_id':'bus','observed_at_s':.8,'frame_id':1,'position':[100.,0.]}
    rows=[{'frame':1,'time_s':.8,'pedestrian_location':[100.,0.,0.],
           'rgb_observations':{'bus':[observation]},'vehicles':{'hero_car':vehicle()}},
          {'frame':2,'time_s':1.,'pedestrian_location':[100.,0.,0.], 'rgb_observations':{},
           'vehicles':{'hero_car':vehicle(brake=1.,sources=[source],age=age,guard=guard)}}]
    receipt={'event':'received','event_id':'ped-warning','receiver_id':'hero_car','source_id':'bus',
             'channel':'cellular','at_s':.9,'observed_at_s':capture,'expires_at_s':3.3}
    return {'folder':Path('missing'),'rows':rows,'messages':[receipt],
            'result':{'assistance':True,'pedestrian_contacts':[],'requested_config':{}},'complete':True}


class EfficiencyReportTests(unittest.TestCase):
    def test_composite_contact_requires_all_three_known_for_avoidance(self):
        self.assertTrue(report.any_contact({'pedestrian':None,'A_B':True,'B_C':None}))
        self.assertIsNone(report.any_contact({'pedestrian':False,'A_B':False,'B_C':None}))
        self.assertFalse(report.any_contact({e:False for e in report.ENDPOINTS}))

    def test_early_route_exit_is_not_full_observation_coverage(self):
        result={'terminal_reason':'observation_window_complete'}
        self.assertFalse(report.window_coverage(result,[{'time_s':0.},{'time_s':14.45}],{})['passed'])
        self.assertTrue(report.window_coverage(result,[{'time_s':0.},{'time_s':19.99}],{})['passed'])

    def test_archived_failure_preserves_reason_and_remains_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);attempt=root/'Failed/Run_Attempts/Test_006/Automatic/OFF/Attempt_001'
            numeric=attempt/'numeric';numeric.mkdir(parents=True)
            (attempt/'attempt_preservation.json').write_text(json.dumps({'reason':'incomplete attempt','configuration':{'seed':3006}}))
            (numeric/'technical_failure.json').write_text(json.dumps({'error':'invalid physical initial gap'}))
            records=report.archived_attempts(root)
            self.assertEqual(records['count'],1)
            archived=records['records'][0]
            self.assertEqual(archived['seed'],3006)
            self.assertEqual(archived['test'],'Test_006')
            self.assertEqual(archived['technical_failure']['error'],'invalid physical initial gap')
            self.assertTrue(archived['excluded_from_48_completed_runs'])
            self.assertIsNone(archived['recorded_outcomes']['pedestrian'])

    def test_contact_free_differences_use_same_complete_known_pairs(self):
        good={s:{'complete_numeric_run':True,'outcomes':{e:s=='OFF' for e in report.ENDPOINTS}} for s in report.STATES}
        missing={s:{'complete_numeric_run':s=='OFF','outcomes':{e:False for e in report.ENDPOINTS}} for s in report.STATES}
        tests=[{'modes':{'Automatic':{'qualification':{'qualified':True},'runs':r}}} for r in (good,missing)]
        rates=report.contact_rates(tests)
        delta=rates['absolute_OFF_to_ON_differences']['Automatic']['contact_free']
        self.assertEqual(delta['OFF_denominator'],2)
        self.assertEqual(delta['ON_denominator'],1)
        self.assertEqual(delta['matched_pair_denominator'],1)
        self.assertEqual(delta['paired_ON_minus_OFF_percentage_points'],100.)
        self.assertEqual(delta['ON_minus_OFF_rate_percentage_points'],50.)

    def nuisance_run(self,positions,speeds,directions=None):
        directions=directions or [[0.,1.]]*len(positions)
        rows=[]
        for i,(ped,speed,direction) in enumerate(zip(positions,speeds,directions)):
            hero=vehicle(location=(-4.+4*i,0.,0.));hero['half_width_m']=1.
            rows.append({'frame':i,'time_s':float(i),'pedestrian_location':[0.,ped,0.],
                         'pedestrian_submitted_control':{'speed_mps':speed,'direction':direction},
                         'vehicles':{'hero_car':hero}})
        return {'rows':rows,'result':{'encounter_timing':{'failure':{'prediction':{'conflict_center_xy_m':[0.,0.]}}},'pedestrian_contacts':[]}}

    def test_waiting_requires_actual_stationary_curb_and_approach(self):
        measured=report.nuisance_enactment(self.nuisance_run([-3.,-3.,-3.],[0.,0.,0.]),'wait')
        self.assertTrue(measured['enacted'])
        self.assertTrue(measured['equivalent_marker_available'])
        self.assertFalse(measured['trigger_recorded'])
        intrusion=report.nuisance_enactment(self.nuisance_run([0.,0.,0.],[0.,0.,0.]),'wait')
        self.assertFalse(intrusion['enacted'])
        self.assertTrue(intrusion['unintended_physical_conflict_occupancy'])

    def test_retreat_requires_submitted_reversal_and_physical_displacement(self):
        never_walked=self.nuisance_run([-3.,-3.,-3.],[0.,0.,0.])
        self.assertFalse(report.nuisance_enactment(never_walked,'retreat')['enacted'])
        enacted=self.nuisance_run([-3.,-2.,-3.],[1.,1.,1.],[[0.,1.],[0.,1.],[0.,-1.]])
        measured=report.nuisance_enactment(enacted,'retreat')
        self.assertTrue(measured['enacted'])
        self.assertEqual(measured['physical_retreat_distance_m'],1.)
        enacted['result']['pedestrian_contacts']=[{'frame':2}]
        self.assertFalse(report.nuisance_enactment(enacted,'retreat')['enacted'])

    def test_generated_catalogue_uses_actual_seed_and_local_folder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan=root/'study_48_plan.json'
            relative='Theme_01_Crossing/Test_001_Morning_Bus'
            plan.write_text(json.dumps({'tests':[{'test':'Test_003','seed':2003,'relative_path':relative}]}))
            output=report.build_report(plan,root)
            self.assertEqual(output['tests'][0]['local_test_number'],1)
            self.assertEqual(output['tests'][0]['stable_global_id'],'global003')
            catalogue=(root/'test_catalogue.csv').read_text()
            self.assertIn('2003',catalogue)
            self.assertIn(relative,catalogue)
            self.assertIn('0/4 complete',catalogue)

    def audit(self,extra):
        return {'native_crosswalk':{'polygon':[[0.,0.,0.],[2.,0.,0.],[2.,4.,0.],[0.,4.,0.]]},
                'occluder_extent_m':[5.,2.,2.],'occluder_blueprint':'vehicle.bus',
                'actors':{'occluder':{'actor_alive':True,'blueprint':'vehicle.bus'}},
                'camera_to_pedestrian_distance_m':50.,
                'driver_to_pedestrian_ray':[{'label':'Car','distance_from_camera_m':0.},
                                           {'label':'Pedestrians','distance_from_camera_m':49.8}]+extra}

    def test_self_mesh_and_pedestrian_hits_do_not_establish_occlusion(self):
        audit=report.scene_qualification(self.audit([]))
        self.assertFalse(audit['verified'])
        self.assertFalse(audit['initial_vehicle_ray_blocker_recorded'])
        self.assertEqual(audit['vehicle_ray_blockers'],[])

    def test_physical_vehicle_hit_between_self_and_target_qualifies_ray(self):
        audit=report.scene_qualification(self.audit([{'label':'Bus','distance_from_camera_m':35.}]))
        self.assertTrue(audit['verified'])
        self.assertEqual(len(audit['vehicle_ray_blockers']),1)
        self.assertTrue(audit['native_crosswalk_verified'])

    def test_nonvehicle_near_self_or_beyond_target_hits_never_qualify(self):
        extra=[{'label':'Building','distance_from_camera_m':20.},
               {'label':'Car','distance_from_camera_m':3.},
               {'label':'Truck','distance_from_camera_m':49.},
               {'label':'Car','distance_from_camera_m':55.}]
        self.assertFalse(report.scene_qualification(self.audit(extra))['verified'])

    def test_missing_or_partial_placement_audit_remains_unknown(self):
        self.assertIsNone(report.scene_qualification(None)['verified'])
        self.assertIsNone(report.scene_qualification({'native_crosswalk':{}})['verified'])

    def test_scene_counter_excludes_timing_only_qualified_pair(self):
        runs={state:{'complete_numeric_run':True,'outcomes':{e:False for e in report.ENDPOINTS}} for state in report.STATES}
        pair={'qualification':{'qualified':True,'occluded_effectiveness_qualified':False},'runs':runs,
              'matched_outcomes':{e:'both no contact' for e in report.ENDPOINTS}}
        tests=[{'modes':{'Automatic':pair}}]
        self.assertEqual(report.aggregates(tests,True)['contact_runs']['Automatic']['OFF']['pedestrian']['known_complete_runs'],1)
        self.assertEqual(report.aggregates(tests,True,'occluded_effectiveness_qualified')['contact_runs']['Automatic']['OFF']['pedestrian']['known_complete_runs'],0)

    def test_late_secondary_contact_preserves_upstream_guard_context(self):
        run=reference(False)
        run['rows'][0]['vehicles']['hero_car']['guard']=True
        run['result']['secondary']={'B_C':{'contact':True,'events':[{'frame':2,'precontact':{'missing':False}}]}}
        context=report.contact_context(run)
        self.assertEqual(context[0]['endpoint'],'B_C')
        self.assertTrue(context[0]['potentially_guard_related'])
        self.assertEqual(context[0]['prior_upstream_or_own_guard_interventions'][0]['vehicle'],'hero_car')
        self.assertTrue(run['result']['secondary']['B_C']['contact'])

    def test_missing_contact_fields_are_unknown(self):
        self.assertEqual(report.outcome({}),{'pedestrian':None,'A_B':None,'B_C':None})
        self.assertIsNone(report.outcome({'pedestrian_contacts':[]})['A_B'])
        self.assertFalse(report.outcome({'pedestrian_contacts':[]})['pedestrian'])

    def test_matched_categories_allow_both_avoid_or_ON_contact(self):
        out=report.comparison({'pedestrian':False,'A_B':True,'B_C':None},
                              {'pedestrian':True,'A_B':False,'B_C':False})
        self.assertEqual(out['pedestrian'],'OFF no contact / ON contact')
        self.assertEqual(out['A_B'],'OFF contact / ON no contact')
        self.assertEqual(out['B_C'],'unknown')
        self.assertEqual(report.comparison({k:False for k in report.ENDPOINTS},{k:False for k in report.ENDPOINTS})['A_B'],'both no contact')

    def test_no_recorded_reference_contact_leaves_deadline_unknown(self):
        self.assertIsNone(report.stopping_deadline(reference(False),'hero_car')['deadline_s'])

    def test_primary_deadline_uses_measured_distance_speed_and_response(self):
        deadline=report.stopping_deadline(reference(),'hero_car',.2)
        expected=1.+(100.-2.-.4-1.-100./12.)/10.-.2
        self.assertAlmostEqual(deadline['deadline_s'],expected)
        self.assertEqual(deadline['endpoints'][0]['actual_contact_s'],11.)
        self.assertNotEqual(deadline['deadline_s'],11.)
        driver=report.stopping_deadline(reference(),'hero_car',1.6)
        self.assertAlmostEqual(deadline['deadline_s']-driver['deadline_s'],1.4)

    def test_secondary_deadline_keeps_gap_and_predecessor_measurements(self):
        run=reference(False)
        run['result']['secondary']={'A_B':{'contact':True,'events':[{'frame':2}]}}
        deadline=report.stopping_deadline(run,'follower_b',.5)
        self.assertAlmostEqual(deadline['deadline_s'],1.5)
        self.assertEqual(deadline['limiting_endpoint'],'A_B')
        self.assertEqual(deadline['endpoints'][0]['bumper_gap_m'],6.)

    def test_receipt_alone_local_brake_or_guard_never_counts_as_used_warning(self):
        for run in (warning_run(guard=True),warning_run(source='driver')):
            self.assertEqual(report.used_warnings(run,'hero_car'),[])
        run=warning_run();run['rows'][1]['vehicles']['hero_car']['brake']=0.
        self.assertEqual(report.used_warnings(run,'hero_car'),[])

    def test_expired_or_stale_used_sources_cannot_make_redundancy_success(self):
        run=warning_run();run['messages'][0]['expires_at_s']=.95
        self.assertEqual(report.used_warnings(run,'hero_car'),[])
        self.assertEqual(report.used_warnings(warning_run(age=1.2,capture=-.2),'hero_car'),[])

    def test_fresh_actual_receipt_associated_with_target_brake_counts(self):
        used=report.used_warnings(warning_run(),'hero_car')
        self.assertEqual(len(used),1)
        self.assertEqual(used[0]['channel'],'cellular')
        self.assertAlmostEqual(used[0]['source_age_at_use_s'],.2)
        self.assertAlmostEqual(used[0]['receipt_to_used_brake_s'],.1)

    def test_fault_endpoints_are_recorded_not_filled_from_scheduled_times(self):
        faults=report.fault_intervals([{'event':'fault_started','fault':'channel_0','at_s':2.0167,
                                      'specification':{'channel':'radio','start_s':2.,'end_s':3.}}])
        self.assertEqual(faults[0]['actual_start_s'],2.0167)
        self.assertIsNone(faults[0]['actual_end_s'])

    def test_alternate_used_channel_requires_established_deadline(self):
        on=warning_run();on['result']['requested_config']={'channel_outages':[{'channel':'radio','start_s':.5,'end_s':1.1}]}
        known=report.communication_report(on,reference(),{},'Automatic')['outage_windows'][0]
        unknown=report.communication_report(on,reference(False),{},'Automatic')['outage_windows'][0]
        self.assertTrue(known['recipients']['hero_car']['alternate_usable_before_deadline'])
        self.assertIsNone(unknown['recipients']['hero_car']['alternate_usable_before_deadline'])

    def test_qualified_denominator_does_not_include_failed_timing_pair(self):
        runs={state:{'complete_numeric_run':True,'outcomes':{e:state=='OFF' for e in report.ENDPOINTS}} for state in report.STATES}
        pair={'qualification':{'qualified':False},'runs':runs,'matched_outcomes':report.comparison(runs['OFF']['outcomes'],runs['ON']['outcomes'])}
        tests=[{'modes':{'Automatic':pair}}]
        self.assertEqual(report.aggregates(tests)['contact_runs']['Automatic']['OFF']['pedestrian']['known_complete_runs'],1)
        self.assertEqual(report.aggregates(tests,True)['contact_runs']['Automatic']['OFF']['pedestrian']['known_complete_runs'],0)

    def test_missing_future_runs_are_reported_without_fabricating_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan=root/'study_48_plan.json'
            plan.write_text(json.dumps({'tests':[{'test':'Test_005','theme':'T02','relative_path':'Theme_02/Test_005'}]}))
            output=report.build_report(plan,root)
            self.assertEqual(output['requested_run_count'],4)
            self.assertTrue((root/'efficiency_report.json').exists())
            pair=output['tests'][0]['modes']['Automatic']
            self.assertFalse(pair['qualification']['qualified'])
            self.assertIsNone(pair['runs']['OFF']['outcomes']['pedestrian'])
            self.assertEqual(output['raw_complete_outcome_summary']['contact_runs']['Automatic']['OFF']['pedestrian']['known_complete_runs'],0)


if __name__=='__main__':unittest.main()
