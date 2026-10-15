"""Inspect actual synchronized encounter evidence before authorizing a larger batch."""
import argparse,json,math,shutil
from pathlib import Path
from PIL import Image
from crossguard.paths import STUDY_ROOT
from crossguard.simulation.study_assessment import check_pair
from crossguard.simulation.encounter_timing import arrival_time
from assemble_town10_package import event_sets,assets,VIEWS
ROOT=STUDY_ROOT/'Theme_03'/'Test_001'
def read(p): return json.loads(Path(p).read_text())
def save(p,v): Path(p).write_text(json.dumps(v,indent=2))
def crossing_time(rows,values):
    for i in range(1,len(rows)):
        if values[i-1]>0 and values[i]<=0:
            fraction=values[i-1]/(values[i-1]-values[i])
            return rows[i-1]['time_s']+fraction*(rows[i]['time_s']-rows[i-1]['time_s'])
    return None
def evaluate(folder):
    result=read(folder/'results.json');rows=read(folder/'physical_timeline.json')
    timing=result['encounter_timing'];e=timing['trigger']
    if e is None: return {'valid':False,'failure':timing['failure']}
    yaw=math.radians(rows[0]['vehicles']['hero_car']['yaw_deg']);forward=(math.cos(yaw),math.sin(yaw));right=(-forward[1],forward[0])
    point=e['conflict_center_xy_m']
    lateral=[sum((r['pedestrian_location'][i]-point[i])*right[i] for i in range(2)) for r in rows]
    ped_along=[sum((r['pedestrian_location'][i]-point[i])*forward[i] for i in range(2)) for r in rows]
    distances=[sum((point[i]-r['vehicles']['hero_car']['location'][i])*forward[i] for i in range(2)) for r in rows]
    ped_time=crossing_time(rows,lateral);a_time=crossing_time(rows,distances)
    geometry={'width_half_m':rows[0]['vehicles']['hero_car']['half_width_m'],'length_half_m':rows[0]['vehicles']['hero_car']['half_length_m']}
    width=geometry['width_half_m']+.4;length=geometry['length_half_m']+.4
    occupancy=[{'frame':r['frame'],'time_s':r['time_s'],'pedestrian_lateral_m':lateral[i],
        'pedestrian_along_m':ped_along[i],'pedestrian_in_conflict_area':abs(lateral[i])<=width and abs(ped_along[i])<=length,'A_in_conflict_area':abs(distances[i])<=length,
        'A_distance_m':distances[i]} for i,r in enumerate(rows)]
    ped_occupied=[r['time_s'] for r in occupancy if r['pedestrian_in_conflict_area']]
    a_occupied=[r['time_s'] for r in occupancy if r['A_in_conflict_area']]
    overlap=[r['time_s'] for r in occupancy if r['A_in_conflict_area'] and r['pedestrian_in_conflict_area']]
    brake=result['timeline']['hero_car']['first_brake_command_s']
    contacts=[ev for ev in result['pedestrian_contacts'] if ev['vehicle_id']=='hero_car']
    stopframe=min((ev['frame'] for ev in contacts),default=rows[-1]['frame']+1)
    candidates=[(i,r) for i,r in enumerate(rows) if r['time_s']>=e['at_s'] and r['frame']<stopframe and (brake is None or r['time_s']<brake) and distances[i]>0]
    i,last=candidates[-1] if candidates else (0,rows[0])
    v=last['vehicles']['hero_car']['along_speed_mps']
    remaining=arrival_time(distances[i],v,last['vehicles']['hero_car']['acceleration_mps2'],result['effective_config']['target_speed_kmh']/3.6) if v>.1 else None
    extrapolated=last['time_s']+remaining if remaining is not None else None
    freeflow=a_time if a_time is not None and (brake is None or a_time<brake) and not contacts else extrapolated
    # Contact can displace the walker before center passage. Preserve actual passage;
    # compare intended free-flow arrival using strictly pre-contact walking measurements.
    all_contacts=result['pedestrian_contacts']
    first_ped_contact=min((x['frame'] for x in all_contacts),default=rows[-1]['frame']+1)
    pi=next((i for i in range(len(rows)-1,0,-1) if rows[i]['frame']<first_ped_contact and rows[i]['time_s']>=e['at_s'] and lateral[i]>0),None)
    undisturbed=ped_time;ped_projection=None
    if all_contacts and pi is not None:
        dt=rows[pi]['time_s']-rows[pi-1]['time_s']
        rate=(lateral[pi-1]-lateral[pi])/dt
        if rate>.1:
            undisturbed=rows[pi]['time_s']+lateral[pi]/rate
            ped_projection={'measurement_time_s':rows[pi]['time_s'],'walking_speed_mps':rate,'remaining_lateral_distance_m':lateral[pi],'arrival_s':undisturbed,'strictly_before_contact_frame':first_ped_contact}
    measured_error=freeflow-undisturbed if freeflow is not None and undisturbed is not None else None
    out={'trigger':e,'failure':timing['failure'],'actual_pedestrian_center_arrival_s':ped_time,
         'actual_A_center_passage_s':a_time,'actual_pedestrian_center_measurement_note':'lane-centerline crossing; after impact pedestrian may be displaced along road; 2D conflict occupancy is separate','undisturbed_pedestrian_arrival_s':undisturbed,'precontact_pedestrian_projection':ped_projection,'pedestrian_conflict_occupancy_s':[min(ped_occupied),max(ped_occupied)] if ped_occupied else None,
         'A_conflict_occupancy_s':[min(a_occupied),max(a_occupied)] if a_occupied else None,
         'actual_overlap_s':[min(overlap),max(overlap)] if overlap else None,
         'measured_freeflow_A_arrival_s':freeflow,'freeflow_method':'actual passage before braking/contact, else last pre-brake/pre-contact measured acceleration-to-cruise projection',
         'projection_measurement_time_s':last['time_s'],'projection_speed_mps':v,
         'measured_freeflow_minus_undisturbed_pedestrian_arrival_s':measured_error,
         'measured_freeflow_minus_actual_pedestrian_arrival_s':freeflow-ped_time if freeflow is not None and ped_time is not None else None,
         'predicted_arrival_match':abs(e['arrival_difference_s'])<=e['timing_tolerance_s'],
         'measured_arrival_match':measured_error is not None and abs(measured_error)<=e['timing_tolerance_s'],
         'physical_conflict_half_width_m':width,'physical_conflict_half_length_m':length}
    invalid=[{'frame':r['frame'],'time_s':r['time_s'],'raw_control':r.get('pedestrian_control'),'reason':'invalid walker-control readback; raw evidence preserved'} for r in rows if r.get('pedestrian_control') and (not 0<=r['pedestrian_control']['speed_mps']<=2 or math.sqrt(sum(v*v for v in r['pedestrian_control']['direction']))>1.01)]
    save(folder/'measurement_failures.json',invalid)
    out['invalid_walker_control_readbacks']=len(invalid)
    out['valid']=out['predicted_arrival_match'] and out['measured_arrival_match'] and timing['failure'] is None
    save(folder/'encounter_assessment.json',out);save(folder/'conflict_occupancy.json',occupancy)
    return out
def assemble():
    comparison={};checks={};reports={}
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            folder=ROOT/mode/state
            assessment=evaluate(folder);result=event_sets(folder);reports[mode+' '+state]=assessment
            comparison[mode+' '+state]={'pedestrian_contacts':sorted(set(e['vehicle_id'] for e in result['pedestrian_contacts'])),
                'A_B_contact':result['secondary']['A_B']['contact'],'B_C_contact':result['secondary']['B_C']['contact'],
                'minimum_gaps_m':{k:v['minimum_physical_bumper_gap_m'] for k,v in result['secondary'].items()},
                'brake_s':{k:v['first_brake_command_s'] for k,v in result['timeline'].items()},
                'usable_assisted_brake_s':{k:v['first_warning_control_s'] for k,v in result['timeline'].items()},
                'guards_s':{k:v['guard_first_s'] for k,v in result['timeline'].items()},
                'terminal_reason':result['terminal_reason']}
            runtime=Path(result['source_trial_path']).parent/'runtime_source'
            if runtime.exists():shutil.copytree(runtime,folder/'provenance'/'runtime_source',dirs_exist_ok=True)
        check=check_pair(ROOT/mode/'OFF',ROOT/mode/'ON')
        off,on=reports[mode+' OFF'],reports[mode+' ON']
        check['trigger_difference_s']=abs(off['trigger']['at_s']-on['trigger']['at_s']) if off.get('trigger') and on.get('trigger') else None
        check['trigger_match']=check['trigger_difference_s'] is not None and check['trigger_difference_s']<=1/60+.00001
        check['paired_counterfactual_A_arrival_s']=off.get('measured_freeflow_A_arrival_s')
        check['ON_undisturbed_pedestrian_arrival_s']=on.get('undisturbed_pedestrian_arrival_s')
        check['paired_counterfactual_arrival_difference_s']=off['measured_freeflow_A_arrival_s']-on['undisturbed_pedestrian_arrival_s'] if off.get('measured_freeflow_A_arrival_s') is not None and on.get('undisturbed_pedestrian_arrival_s') is not None else None
        check['paired_counterfactual_match']=check['paired_counterfactual_arrival_difference_s'] is not None and abs(check['paired_counterfactual_arrival_difference_s'])<=.25
        check['walking_rule_identical']=read(ROOT/mode/'OFF'/'requested_config.json')['study_options']['pedestrian_speed_mps']==read(ROOT/mode/'ON'/'requested_config.json')['study_options']['pedestrian_speed_mps']
        offrows=read(ROOT/mode/'OFF'/'physical_timeline.json');onrows=read(ROOT/mode/'ON'/'physical_timeline.json')
        check['actual_walking_controls_equal']=len(offrows)==len(onrows) and all(a.get('pedestrian_control')==b.get('pedestrian_control') for a,b in zip(offrows,onrows))
        cutoff=min([ev['precontact'].get('time_s',float('inf')) for state in ('OFF','ON') for ev in read(ROOT/mode/state/'results.json')['pedestrian_contacts']] or [float('inf')])
        check['walking_control_comparison_cutoff_s']=cutoff if math.isfinite(cutoff) else None
        check['actual_walking_controls_equal_before_first_contact']=all(a.get('pedestrian_control')==b.get('pedestrian_control') for a,b in zip(offrows,onrows) if a['time_s']<=cutoff)
        check['requested_walking_schedule_identical_from_runtime_rule']=check['walking_rule_identical'] and check['trigger_match']
        check['post_contact_readback_note']='Full-window readbacks differ and include impossible values; preserved as measurement failures. They do not define submitted control commands.'
        checks[mode]=check
    save(ROOT/'numeric_comparison.json',comparison);save(ROOT/'synchronization_checks.json',{'runs':reports,'pairs':checks})
    verified={}
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            folder=ROOT/mode/state;manifest=read(folder/'camera_manifest.json');rows=read(folder/'physical_timeline.json');frames={r['frame'] for r in rows}
            count=0
            for m in manifest:
                if m['frame'] not in frames or set(m['views'])!=set(VIEWS) or m['missing']:raise RuntimeError('Incomplete/unmatched camera set')
                for name in VIEWS:
                    with Image.open(assets(folder)/'pictures'/m['views'][name]) as img:
                        img.load()
                        if img.size!=(960,540):raise RuntimeError('Unexpected camera size')
                    count+=1
            verified[mode+' '+state]={'synchronized_sets':len(manifest),'decoded_pictures':count,'physics_rows':len(rows)}
    gate=all(c['requested_conditions_equal_except_assistance'] and c['physical_initial_match'] and c['trigger_match'] and c['walking_rule_identical'] and c['actual_walking_controls_equal_before_first_contact'] and c['requested_walking_schedule_identical_from_runtime_rule'] and c['paired_counterfactual_match'] for c in checks.values()) and all(reports[m+' OFF']['valid'] for m in ('Automatic','Retrofit'))
    save(ROOT/'package_verification.json',{'actual_pictures':verified,'synchronization_gate_passed':gate,'larger_batch_started':False})
    lines=['# Synchronized Town10HD small package','',f'Synchronization gate passed: **{gate}**. Four revised runs; earlier results remain separate. No larger batch launched.','',
        '[Frozen rule and tolerance](timing_protocol.md) · [Configuration](test_config.json) · [Detailed checks](synchronization_checks.json)','',
        'Walking starts once from a measured approach prediction at the lane/path intersection. No retiming follows braking. The declared tolerance is 0.25 s; matched trigger times must agree within one physics tick. Actual conflict occupancy is measured separately from intended free-flow coincidence. When contact displaces the walker, actual center passage remains recorded and a strictly pre-contact walking projection measures the intended arrival; post-impact center passage cannot represent undisturbed walking.','',
        '| Run | Trigger s | A speed m/s | A distance m | Predicted A / pedestrian arrival s | Actual pedestrian lane-center passage s | Undisturbed pedestrian arrival s | Measured free-flow A arrival s |','|---|---:|---:|---:|---|---:|---:|---:|']
    for key,a in reports.items():
        e=a.get('trigger')
        if e:lines.append(f"| {key} | {e['at_s']:.3f} | {e['A_speed_mps']:.3f} | {e['A_distance_m']:.3f} | {e['A_predicted_arrival_s']:.3f} / {e['pedestrian_predicted_arrival_s']:.3f} | {a['actual_pedestrian_center_arrival_s']} | {a['undisturbed_pedestrian_arrival_s']} | {a['measured_freeflow_A_arrival_s']} |")
    lines+=['','| Run | Pedestrian contact vehicles | A/B | B/C | Minimum A/B / B/C gap m |','|---|---|---|---|---|']
    for key,c in comparison.items():lines.append(f"| {key} | {', '.join(c['pedestrian_contacts']) or 'none'} | {c['A_B_contact']} | {c['B_C_contact']} | {c['minimum_gaps_m']['A_B']:.3f} / {c['minimum_gaps_m']['B_C']:.3f} |")
    lines+=['','Contact counts are observed outcomes, not universal effectiveness estimates. Guard interventions and communication/driver assumptions remain in each results file and causal assessment. The ON pre-braking extrapolation is less accurate while A is accelerating and is preserved, including its failed 0.25 s estimator check. Pair synchronization uses the matched OFF free-flow trajectory as the unassisted ON reference, checked against the actual ON undisturbed pedestrian arrival. Initial poses, trigger measurements and walking controls before first contact match. Full-window walker-control readbacks contain invalid post-contact values; raw values and measurement_failures.json are preserved. Identical requested walking schedules follow the verified runtime rule; reconstruction is distinguished from recorded readbacks. This distinction preserves the failed estimator check without retiming the scenario.','',
        '[Secondary-collision diagnostic and explanation](../../diagnostics/secondary_collision_diagnostic.md)','']
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            rel=f'{mode}/assets/{state}'
            lines+=[f'## {mode} {state}','',f'[Numeric result]({mode}/{state}/results.json) · [Braking chronology]({mode}/{state}/assessment.json) · [Occupancy]({mode}/{state}/conflict_occupancy.json)','',f'![Before emergence]({rel}/event_sets/before_emergence.jpg)','',f'![During braking]({rel}/event_sets/during_braking.jpg)','',f'![Intended encounter]({rel}/event_sets/intended_encounter.jpg)','',f'![Outcome]({rel}/event_sets/outcome.jpg)','',f'![Measured timelines]({rel}/braking_timeline.png)','']
        lines += [f'Comparison videos: ' + ' · '.join(f'[{v}]({mode}/assets/{v}_OFF_ON.mp4)' for v in VIEWS),'']
    (ROOT/'report.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps({'comparison':comparison,'synchronization':reports,'pairs':checks,'gate':gate},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['evaluate','pictures']);p.add_argument('--mode',default='Automatic');p.add_argument('--state',default='OFF');a=p.parse_args()
    if a.action=='evaluate':print(json.dumps(evaluate(ROOT/a.mode/a.state),indent=2))
    else:assemble()
