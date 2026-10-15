"""Evaluator-only physical measurements and complete run evidence."""
from dataclasses import asdict
import json

def fault_intervals(messages):
    intervals=[];active={}
    for event in messages:
        key=event.get('fault')
        if event.get('event')=='fault_started':
            interval={'fault':key,'specification':event.get('specification'),
                      'actual_start_s':event['at_s'],'actual_end_s':None}
            active[key]=interval;intervals.append(interval)
        elif event.get('event')=='fault_ended' and key in active:
            active.pop(key)['actual_end_s']=event['at_s']
    return intervals
import math
from pathlib import Path

def measure_frame(scene,frame,brake_a,throttle_a):
    vehicles={}
    actors={'hero_car':scene.car,**scene.followers}
    axis=scene.forward
    predecessor=None
    for name,actor in actors.items():
        loc=actor.get_location()
        v=actor.get_velocity()
        a=actor.get_acceleration()
        vertices=actor.bounding_box.get_world_vertices(actor.get_transform())
        projections=[p.dot(axis) for p in vertices]
        rear,front=min(projections),max(projections)
        control=actor.get_control()
        data={'actor_id':actor.id,'position_m':loc.dot(axis),
              'location':[loc.x,loc.y,loc.z],'yaw_deg':actor.get_transform().rotation.yaw,
              'speed_mps':v.length(),'along_speed_mps':v.dot(axis),
              'acceleration_mps2':a.dot(axis),'brake':brake_a if name=='hero_car' else scene.group_metrics[name]['brake'],
              'throttle':throttle_a if name=='hero_car' else scene.group_metrics[name]['throttle'],
              'rear_m':rear,'front_m':front,'half_width_m':actor.bounding_box.extent.y,'half_length_m':actor.bounding_box.extent.x,
              'guard':getattr(scene,'_corridor_stop_trial',None)==scene.trial_number if name=='hero_car' else name in scene.follower_guards,
              'decision':asdict(scene.current_decision) if name=='hero_car' else scene.group_metrics[name],
              'gap_m':None if predecessor is None else predecessor['rear_m']-front,
              'closing_speed_mps':None if predecessor is None else v.dot(axis)-predecessor['along_speed_mps']}
        vehicles[name]=data
        predecessor=data
    return {'frame':frame,'time_s':scene.time,'simulation_timestamp_s':scene.start_time+scene.time,
            'vehicles':vehicles,'pedestrian_location':[scene.person.get_location().x,scene.person.get_location().y,scene.person.get_location().z],
            'pedestrian_control':{'speed_mps':scene.person.get_control().speed,'direction':[scene.person.get_control().direction.x,scene.person.get_control().direction.y,scene.person.get_control().direction.z]},
            'pedestrian_submitted_control':getattr(scene,'pedestrian_submitted_control',None),
            'crossing_at_s':scene.crossing_at,'diagnostic_brake_at_s':scene.diagnostic_brake_at,
            'rgb':{k:v for k,v in scene.pipeline_status.items() if k not in ('preview_images','observations')},
            'rgb_observations':{k:[asdict(o) for o in obs] for k,obs in scene.pipeline_status.get('observations',{}).items()}}

def first(rows,predicate):
    return next((row['time_s'] for row in rows if predicate(row)),None)

def save_study_run(scene,reason):
    output=Path(scene.study_options['output'])
    output.mkdir(parents=True,exist_ok=True)
    rows=scene.study_rows
    contacts=scene.collisions+scene.group_collisions
    ids={'hero_car':scene.car.id,**{k:a.id for k,a in scene.followers.items()}}
    pairs={}
    for name,a,b in [('A_B','hero_car','follower_b'),('B_C','follower_b','follower_c')]:
        target={ids[a],ids[b]}
        selected=[e for e in contacts if {e['actor_id'],e['other_id']}==target]
        pairs[name]={'contact':bool(selected),'events':selected,
                     'minimum_physical_bumper_gap_m':min((r['vehicles'][b]['gap_m'] for r in rows),default=None)}
    timeline={}
    for name in ids:
        braking=first(rows,lambda r:r['vehicles'][name]['brake']>.05)
        received=next((e.get('at_s') for e in scene.messages if e.get('event')=='received' and e.get('receiver_id')==name),None)
        usable=first(rows,lambda r:bool(r['vehicles'][name]['decision'].get('sources')) and r['vehicles'][name]['brake']>.05)
        timeline[name]={'first_warning_receipt_s':received,'first_warning_control_s':usable,
                        'first_brake_command_s':braking,
                        'response_after_receipt_s':braking-received if braking is not None and received is not None else None,
                        'brake_command_duration_s':sum(1/60 for r in rows if r['vehicles'][name]['brake']>.05),
                        'minimum_speed_mps':min((r['vehicles'][name]['speed_mps'] for r in rows),default=None),
                        'guard_first_s':first(rows,lambda r:r['vehicles'][name]['guard']),
                        'distance_travelled_m':rows[-1]['vehicles'][name]['position_m']-rows[0]['vehicles'][name]['position_m'] if rows else None}
    pedestrian_contacts=[e for e in contacts if e['other_id']==scene.person.id]
    result={'requested_config':scene.study_options,'effective_config':json.loads((scene.trial_path/'config.json').read_text()),
            'seed':scene.seed,'assistance':scene.enabled,'mode':scene.receiver_mode,
            'terminal_reason':reason,'technical_failure':reason not in ('observation_window_complete','route_exit','finished'),
            'pedestrian_contacts':pedestrian_contacts,'secondary':pairs,'timeline':timeline,
            'encounter_timing':{'trigger':scene.encounter_trigger.event,'failure':scene.encounter_trigger.failure} if scene.encounter_trigger else None,
            'diagnostic_A_brake_s':scene.diagnostic_brake_at,'pedestrian_movement_trigger_s':scene.crossing_at,
            'first_detection_s':{source:first(rows,lambda r:bool(r['rgb_observations'].get(source))) for source in ('driver','bus','rsu')},
            'initial_measured_state':rows[0] if rows else None,'last_measured_state':rows[-1] if rows else None,
            'actual_outages':fault_intervals(scene.messages),
            'missing_measurements':[e for e in contacts if e.get('precontact',{}).get('missing',True)],
            'sample_count':len(rows),'source_trial_path':str(scene.trial_path),
            'limitations':['Local follower ranging is an explicit modeled sensor, not follower RGB perception.',
                'Brake command caps are declared; achieved deceleration is measured, not assumed.',
                'No contact is not evidence of universal effectiveness.'],
            'unnecessary_braking':scene.pedestrian_behavior in ('wait','retreat')}
    (output/'results.json').write_text(json.dumps(result,indent=2))
    (output/'physical_timeline.json').write_text(json.dumps(rows,indent=2))
    (output/'messages.json').write_text(json.dumps(scene.messages,indent=2))
    (output/'braking_events.json').write_text(json.dumps(scene.braking_events,indent=2))
    (output/'crosswalk.json').write_text(json.dumps(scene.crosswalk_info,indent=2))
    return result
