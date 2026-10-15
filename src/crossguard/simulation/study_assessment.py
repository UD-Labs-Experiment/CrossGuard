"""Offline evaluator of raw evidence; never provides inputs to vehicle controls."""
import json
import math
from pathlib import Path

def read(path):return json.loads(Path(path).read_text())

def canonical_requested(config):
    config=json.loads(json.dumps(config))
    config.pop('enabled',None)
    options=config.get('study_options',{})
    options.pop('output',None)
    options.pop('assets_output',None)
    options.get('labels',{}).pop('assistance',None)
    return config

def check_pair(off,on):
    off,on=Path(off),Path(on)
    configs_equal=canonical_requested(read(off/'requested_config.json'))==canonical_requested(read(on/'requested_config.json'))
    initial={}
    a,b=read(off/'results.json')['initial_measured_state'],read(on/'results.json')['initial_measured_state']
    for name in a['vehicles']:
        v,w=a['vehicles'][name],b['vehicles'][name]
        initial[name]={'position_difference_m':math.dist(v['location'],w['location']),
                      'speed_difference_mps':abs(v['speed_mps']-w['speed_mps']),
                      'yaw_difference_deg':abs(v['yaw_deg']-w['yaw_deg'])}
    return {'requested_conditions_equal_except_assistance':configs_equal,'initial_physical_differences':initial,
            'physical_initial_match':all(v['position_difference_m']<.05 and v['speed_difference_mps']<.02
                                         for v in initial.values()),
            'note':'Actor IDs and absolute CARLA frame/timestamps differ after reload; blueprints and poses are matched.'}

def is_cooperative_brake(vehicle, assistance):
    return bool(assistance and vehicle['brake']>.05 and any(source!='driver' for source in vehicle['decision'].get('sources',[])))

def assess_run(output):
    output=Path(output)
    result=read(output/'results.json')
    rows=read(output/'physical_timeline.json')
    byframe={r['frame']:r for r in rows}
    events=result['pedestrian_contacts']+[e for p in result['secondary'].values() for e in p['events']]
    first_contacts={}
    for event in sorted(events,key=lambda e:e['frame']):
        key=tuple(sorted((event['actor_id'],event['other_id'])))
        if key in first_contacts:continue
        frame=byframe.get(event['frame'])
        first_contacts[key]={'vehicle':event['vehicle_id'],'other_id':event['other_id'],'frame':event['frame'],
            'time_s':frame['time_s'] if frame else None,'impulse':event['impulse'],'precontact':event['precontact']}
    segments={}
    for name in rows[0]['vehicles']:
        commands=[];was=False
        for row in rows:
            v=row['vehicles'][name];braking=v['brake']>.05
            if braking and not was:
                commands.append({'time_s':row['time_s'],'frame':row['frame'],'brake':v['brake'],
                    'speed_mps':v['speed_mps'],'gap_m':v['gap_m'],'closing_speed_mps':v['closing_speed_mps'],
                    'reason':v['decision'].get('reason'),'sources':v['decision'].get('sources',[]),
                    'guard':v['guard'],'local_pathway':v['decision'].get('local_pathway')})
            was=braking
        segments[name]=commands
    messages=read(output/'messages.json')
    receipts={name:[e for e in messages if e.get('event')=='received' and e.get('receiver_id')==name]
              for name in segments}
    warnings={}
    for name in segments:
        first_assisted=next((r for r in rows if is_cooperative_brake(r['vehicles'][name],result.get('assistance',False))),None)
        eligible=[]
        if first_assisted:
            v=first_assisted['vehicles'][name]
            for e in receipts[name]:
                age=first_assisted['time_s']-e.get('observed_at_s',-100)
                used_age=v['decision'].get('observation_age_s')
                if (e['at_s']<=first_assisted['time_s']+1e-6 and 0<=age<=1.
                    and e.get('source_id',e.get('sender_id')) in v['decision'].get('sources',[])
                    and (used_age is None or abs(age-used_age)<1e-5)):
                    eligible.append(e)
        warning=min(eligible,key=lambda e:e['at_s']) if eligible else None
        used_observation=None
        if warning:
            for row in rows:
                for obs in row['rgb_observations'].get(warning.get('source_id'),[]):
                    if abs(obs['observed_at_s']-warning['observed_at_s'])<1e-5:
                        used_observation=obs
                        break
                if used_observation:break
        capture=byframe.get(used_observation['frame_id']) if used_observation else None
        target_error=math.dist(used_observation['position'],capture['pedestrian_location'][:2]) if capture else None
        first_contact=next((c for c in first_contacts.values() if c['vehicle']==name),None)
        warnings[name]={'first_any_packet_s':receipts[name][0]['at_s'] if receipts[name] else None,
            'first_assisted_brake_s':first_assisted['time_s'] if first_assisted else None,
            'associated_warning_receipt_s':warning['at_s'] if warning else None,
            'channel':warning.get('channel') if warning else None,
            'associated_observation_target_error_m':target_error,
            'associated_observation_matches_target':target_error<=3. if target_error is not None else None,
            'receipt_to_assisted_brake_s':first_assisted['time_s']-warning['at_s'] if warning else None,
            'warning_lead_to_actual_contact_s':first_contact['time_s']-warning['at_s'] if warning and first_contact and first_contact['time_s'] is not None else None,
            'missing_warning_association':first_assisted is not None and warning is None}
    target_detections={}
    # Assessment matching tolerance affects reporting only, never perception/control.
    # Match projected RGB track to evaluated pedestrian position at capture frame.
    for source in ('driver','bus','rsu'):
        matched=[];seen=set()
        for row in rows:
            for obs in row['rgb_observations'].get(source,[]):
                identity=(obs['object_id'],obs['frame_id'])
                if identity in seen:continue
                seen.add(identity)
                captured=byframe.get(obs['frame_id'])
                if captured is None:continue
                error=math.dist(obs['position'],captured['pedestrian_location'][:2])
                if error<=3.:
                    matched.append({'received_by_policy_s':row['time_s'],'capture_s':obs['observed_at_s'],
                        'frame':obs['frame_id'],'projection_error_m':error,'object_id':obs['object_id']})
        target_detections[source]={'first_matched':matched[0] if matched else None,'matched_samples':len(matched),
                                   'matching_tolerance_m':3.,'ground_truth_role':'evaluator only'}
    physical_response={}
    for name in segments:
        command=warnings[name]['first_assisted_brake_s'] or result['timeline']['hero_car']['first_brake_command_s']
        candidates=[r for r in rows if command is not None and r['time_s']>=command]
        sustained=next((candidates[i]['time_s'] for i in range(max(0,len(candidates)-2))
            if all(candidates[j]['vehicles'][name]['acceleration_mps2']<=-.5 for j in range(i,i+3))),None)
        physical_response[name]={'marker_brake_command_s':command,'sustained_deceleration_s':sustained,
            'delay_s':sustained-command if command is not None and sustained is not None else None,
            'criterion':'longitudinal acceleration <= -0.5 m/s² for three consecutive frames after marker; not a causal identification'}
    assessment={'pedestrian_contact_vehicles':sorted(set(e['vehicle_id'] for e in result['pedestrian_contacts'])),
        'raw_pedestrian_sensor_callbacks':len(result['pedestrian_contacts']),
        'first_contacts_by_actor_pair':list(first_contacts.values()),'brake_command_segments':segments,
        'warning_associations':warnings,
        'warning_classification_note':'Only enabled assistance with a non-driver source qualifies as cooperative braking. Raw first_warning_control_s may include local driver-camera braking.','target_matched_rgb_detections':target_detections,
        'effective_follower_assumptions':result['requested_config'].get('followers',{}),
        'legacy_metadata_note':'When followers are specified, study_options overrides the legacy follower_delay_s wrapper; local_pathway records the applied timings.',
        'nuisance_status':'Crossing case: startup regulation and route guard braking are separately identified; no blanket unnecessary-braking label.',
        'reproducibility_note':'Sampled scene/control parameters are frozen. Async inference completion and resulting selected RGB capture frames are measured, not bitwise deterministic.',
        'phone_response_assumptions':{'driver_reaction_s':result.get('effective_config',{}).get('phone_reaction_delay_s'),
            'observation_age_ceiling_s':2.,'brake_intent_lifetime_s':1.2,
            'pedestrian_message_lifetime_s':2.5,'pedestrian_control_observation_age_s':1.,
            'source':'runtime source defaults and effective configuration; not independently calibrated'},
        'physical_response_measurements':physical_response,
        'phone_event_export':{'present_in_messages':any(e.get('event')=='expired_before_reaction' for e in messages),'expired_before_reaction_count':sum(e.get('event')=='expired_before_reaction' for e in messages)},
        'missing_exports':[] if any(e.get('event')=='expired_before_reaction' for e in messages) else ['Full phone event history not established by this evidence; terminal_status retains the final 20 events.'],
        'limitations':['B/C have modeled local predecessor ranging; local pedestrian perception is implemented for A only.',
                        'First any-person detection and first any packet are not confirmed target detection and usable warning.',
                        'Contact callbacks may repeat across sustained contact and reciprocal vehicle sensors.']}
    (output/'assessment.json').write_text(json.dumps(assessment,indent=2))
    return assessment
