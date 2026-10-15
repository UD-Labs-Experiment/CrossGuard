"""Offline, conservative comparison of the frozen 48-run Town10HD study.

Never connects to CARLA or changes run evidence. Writes reports and generated catalogue metadata.
"""
import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import re

from crossguard.paths import STUDY_ROOT
from crossguard.simulation.study_assessment import check_pair

MODES=('Automatic','Retrofit')
STATES=('OFF','ON')
VEHICLES=('hero_car','follower_b','follower_c')
CHANNELS=('radio','cellular','optical')
ENDPOINTS=('pedestrian','A_B','B_C')
DECELERATION=6.0
BUFFER=1.0
AGE_LIMIT=1.0
THEME_NAMES={
    'T01':'Crossing Location and Physical Occlusion',
    'T02':'Daylight and Weather',
    'T03':'Encounter Difficulty and Secondary Collisions',
    'T04':'Communication Failures and Redundancy',
    'T05':'Missing Observations and Physical Occlusion',
    'T06':'Waiting and Retreating Nuisance Responses',
}


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def read(path,missing,default=None):
    path=Path(path)
    if not path.is_file():
        missing.append({'file':str(path),'reason':'missing'});return default
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        missing.append({'file':str(path),'reason':str(exc)});return default


def optional(path,default=None):
    return read(path,[],default)


def save(path,value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')


def show(value):
    if value is None:return 'unknown'
    if isinstance(value,float):return f'{value:.3f}' if math.isfinite(value) else 'invalid'
    if isinstance(value,(list,dict)):return json.dumps(value,ensure_ascii=False).replace('|','\\|')
    return str(value).replace('|','\\|').replace('\n',' ')


def file_link(label,path):
    path=Path(path)
    return f'[{label}](<{path.resolve().as_posix()}>)' if path.is_file() else f'{label} (not saved)'


def visible_test_name(entry,root):
    match=re.match(r'Test_(\d+)(?:_(.*))?$',root.name)
    local=int(match.group(1)) if match else None
    description=match.group(2).replace('_',' ') if match and match.group(2) else entry.get('test',root.name)
    stable=str(entry.get('test',root.name))
    stable_match=re.search(r'(\d+)$',stable)
    global_id=f"global{int(stable_match.group(1)):03d}" if stable_match else stable
    return {'local_test_number':local,'stable_global_id':global_id,
            'visible_name':f'Test {local}: {description} ({global_id})' if local is not None else f'{description} ({global_id})'}


def window_coverage(result,rows,requested):
    expected=(requested or {}).get('observation_window_s',result.get('effective_config',{}).get('observation_window_s',20.))
    times=[r.get('time_s') for r in rows if finite(r.get('time_s'))]
    first,last=(min(times),max(times)) if times else (None,None)
    tolerance=1/60+.00001
    reached=finite(expected) and expected>0 and finite(last) and last>=expected-tolerance
    launch=finite(first) and 0<=first<=tolerance
    return {'declared_window_s':expected,'first_recorded_time_s':first,'last_recorded_time_s':last,
            'time_tolerance_s':tolerance,'starts_at_launch':launch,'reaches_declared_endpoint':reached,
            'passed':bool(launch and reached),
            'rule':'explicit observation-window completion and recorded launch-to-end coverage within one 60 Hz tick; no early route-exit or technical-failure attempt is a complete requested run'}


def any_contact(outcomes):
    values=[outcomes.get(e) for e in ENDPOINTS]
    if any(v is True for v in values):return True
    return False if all(v is False for v in values) else None


def outcome(result):
    """An absent contact field is unknown, never automatically contact-free."""
    if not result:return {key:None for key in ENDPOINTS}
    ped=result.get('pedestrian_contacts')
    out={'pedestrian':bool(ped) if isinstance(ped,list) else None}
    for pair in ('A_B','B_C'):
        value=result.get('secondary',{}).get(pair,{}).get('contact')
        out[pair]=value if isinstance(value,bool) else None
    return out


def comparison(off,on):
    categories={}
    for endpoint in ENDPOINTS:
        a,b=off.get(endpoint),on.get(endpoint)
        if a is None or b is None:label='unknown'
        elif a and not b:label='OFF contact / ON no contact'
        elif not a and b:label='OFF no contact / ON contact'
        elif a and b:label='both contact'
        else:label='both no contact'
        categories[endpoint]=label
    return categories


def theme_id(entry,root):
    source=str(entry.get('theme',root.parent.name))
    match=re.search(r'(?:Theme_|T)(\d{1,2})',source)
    return f'T{int(match.group(1)):02d}' if match else source


def read_plan(path,study_root):
    plan=json.loads(Path(path).read_text(encoding='utf-8'))
    root=Path(study_root).resolve();seen=set();tests=[]
    for entry in plan.get('tests',[]):
        relative=Path(entry['relative_path']);target=(root/relative).resolve()
        if relative.is_absolute() or target==root or not target.is_relative_to(root):
            raise ValueError('Test paths must stay within the evidence study')
        if target in seen:raise ValueError('Duplicate test path')
        seen.add(target);tests.append((entry,target))
    if not tests:raise ValueError('Study catalogue has no tests')
    return plan,tests


def archived_attempts(study_root):
    root=Path(study_root)/'Failed'/'Run_Attempts';records=[]
    if not root.is_dir():return {'count':0,'records':[],'classification_counts':{},'scope':str(root)}
    attempts=sorted({p.parent for p in root.rglob('attempt_preservation.json')}
                    | {p for p in root.rglob('Attempt_*') if p.is_dir()})
    for folder in attempts:
        missing=[];preservation=read(folder/'attempt_preservation.json',missing,{}) or {}
        numeric=folder/'numeric'
        result=optional(numeric/'results.json',{}) or {}
        failure=optional(numeric/'technical_failure.json')
        rows=optional(numeric/'physical_timeline.json',[]) or []
        requested=optional(numeric/'requested_config.json')
        coverage=window_coverage(result,rows,requested)
        if result.get('terminal_reason')=='observation_window_complete' and result.get('technical_failure') is False and coverage['passed']:
            classification='unexpected_complete_archived_attempt'
        elif rows and not coverage['passed']:classification='incomplete_observation_window'
        elif result or rows:classification='technical_or_early_terminal_failure'
        else:classification='initialization_or_no_numeric_run'
        config=preservation.get('configuration',{})
        records.append({'archive_path':str(folder.resolve()),'preservation_metadata':preservation,
                        'test':config.get('test',folder.parents[2].name),'seed':config.get('seed'),
                        'mode':folder.parents[1].name,'state':folder.parent.name,
                        'classification':classification,'preservation_reason':preservation.get('reason'),
                        'technical_failure':failure or result.get('technical_failure'),
                        'terminal_reason':result.get('terminal_reason'),'physics_rows':len(rows),
                        'window_coverage':coverage,'recorded_outcomes':outcome(result),
                        'metadata_missing':missing,'excluded_from_48_completed_runs':True})
    return {'count':len(records),'records':records,'classification_counts':dict(Counter(r['classification'] for r in records)),
            'scope':str(root.resolve()),
            'note':'Archived technical/incomplete attempts are separate from the 48 intended complete runs and retain their reasons/outcomes. Earlier superseded packages and diagnostics outside Run_Attempts are not counted here.'}


def load_run(folder):
    missing=[];folder=Path(folder)
    result=read(folder/'results.json',missing)
    rows=read(folder/'physical_timeline.json',missing,[]) or []
    messages=read(folder/'messages.json',missing,[]) or []
    requested=read(folder/'requested_config.json',missing)
    assessment=optional(folder/'assessment.json',{}) or {}
    encounter=optional(folder/'encounter_assessment.json')
    numeric_fail=optional(folder/'technical_failure.json')
    coverage=window_coverage(result or {},rows,requested)
    complete=bool(result) and bool(rows) and not missing and result.get('technical_failure') is False and not numeric_fail and result.get('terminal_reason')=='observation_window_complete' and coverage['passed']
    return {'folder':folder,'result':result or {},'rows':rows,'messages':messages,'requested':requested,
            'assessment':assessment,'encounter':encounter,'missing':missing,'technical_failure_file':numeric_fail,
            'complete':complete,'window_coverage':coverage,'outcome':outcome(result)}


def control_match(off,on):
    contacts=[]
    for run in (off,on):
        byframe={row['frame']:row['time_s'] for row in run['rows']}
        for event in run['result'].get('pedestrian_contacts',[]):
            if event.get('frame') not in byframe:return None
            contacts.append(byframe[event['frame']])
    cutoff=min(contacts) if contacts else math.inf
    a=[r for r in off['rows'] if r['time_s']<cutoff]
    b=[r for r in on['rows'] if r['time_s']<cutoff]
    if not a or len(a)!=len(b):return False
    if any(r.get('pedestrian_submitted_control') is None for r in a+b):return None
    return all(abs(x['time_s']-y['time_s'])<1e-5 and x['pedestrian_submitted_control']==y['pedestrian_submitted_control'] for x,y in zip(a,b))


def nuisance_enactment(run,behavior):
    rows=run['rows'];reasons=[]
    if not rows:return {'enacted':False,'reasons':['physical observations unavailable'],'equivalent_marker_available':False}
    timing=run['result'].get('encounter_timing') or {}
    prediction=timing.get('trigger') or (timing.get('failure') or {}).get('prediction') or {}
    point=prediction.get('conflict_center_xy_m')
    first=rows[0];ped=first.get('pedestrian_location');hero=first.get('vehicles',{}).get('hero_car',{})
    controls=[r.get('pedestrian_submitted_control') for r in rows]
    valid=all(c and finite(c.get('speed_mps')) and isinstance(c.get('direction'),list) and len(c['direction'])>=2 for c in controls)
    yaw=hero.get('yaw_deg');origin=hero.get('location')
    axis=(math.cos(math.radians(yaw)),math.sin(math.radians(yaw))) if finite(yaw) else None
    if not point and valid and axis and ped and origin:
        direction=controls[0]['direction'];cross=lambda a,b:a[0]*b[1]-a[1]*b[0]
        denominator=cross(direction,axis)
        if abs(denominator)>1e-6:
            distance=cross((origin[0]-ped[0],origin[1]-ped[1]),axis)/denominator
            point=[ped[i]+distance*direction[i] for i in range(2)]
    distances=[sum((point[i]-r['vehicles']['hero_car']['location'][i])*axis[i] for i in range(2)) for r in rows] if point and axis else []
    closest_index=min(range(len(distances)),key=lambda i:abs(distances[i])) if distances else None
    opportunity=bool(distances and max(distances)-min(distances)>1.)
    if not valid:reasons.append('recorded submitted normal-walking commands missing or invalid')
    if not opportunity:reasons.append('measurable vehicle approach opportunity/equivalent marker unavailable')
    record={'declared_behavior':behavior,'trigger_recorded':timing.get('trigger') is not None,
            'hypothetical_trigger_failure':timing.get('failure'),'equivalent_marker_available':opportunity,
            'reference_time_s':first['time_s'],'conflict_center_xy_m':point,
            'closest_measured_A_approach_s':rows[closest_index]['time_s'] if closest_index is not None else None,
            'closest_measured_A_distance_m':distances[closest_index] if closest_index is not None else None}
    if behavior=='wait':
        stationary_commands=valid and all(c['speed_mps']<=.001 for c in controls)
        displacement=max((math.dist(r['pedestrian_location'],ped) for r in rows),default=math.inf) if ped else math.inf
        occupied=None
        if point and axis:
            right=(-axis[1],axis[0]);width=hero.get('half_width_m');length=hero.get('half_length_m')
            if finite(width) and finite(length):
                occupied=any(abs(sum((r['pedestrian_location'][i]-point[i])*right[i] for i in range(2)))<=width+.4 and
                             abs(sum((r['pedestrian_location'][i]-point[i])*axis[i] for i in range(2)))<=length+.4 for r in rows)
        record.update(zero_walking_commands=stationary_commands,maximum_pedestrian_displacement_m=displacement if finite(displacement) else None,
                      unintended_physical_conflict_occupancy=occupied)
        if not stationary_commands or displacement>.25 or occupied is not False:
            reasons.append('stationary curb waiting without conflict occupancy not physically established')
    else:
        first_contact=min((e.get('frame',math.inf) for e in run['result'].get('pedestrian_contacts',[])),default=math.inf)
        moving=[(r,c) for r,c in zip(rows,controls) if valid and r['frame']<first_contact and c['speed_mps']>.1]
        initial=moving[0][1]['direction'] if moving else None
        reversed_rows=[r for r,c in moving if initial and sum(c['direction'][i]*initial[i] for i in range(2))<-.5]
        progress=[(r,sum((r['pedestrian_location'][i]-ped[i])*initial[i] for i in range(2))) for r,c in moving] if initial and ped else []
        peak=max((p for r,p in progress),default=0.)
        after=[p for r,p in progress if reversed_rows and r['time_s']>=reversed_rows[0]['time_s']]
        retreat_distance=peak-min(after) if after else None
        record.update(first_forward_walk_s=moving[0][0]['time_s'] if moving else None,
                      first_submitted_reversal_s=reversed_rows[0]['time_s'] if reversed_rows else None,
                      maximum_forward_displacement_m=peak,physical_retreat_distance_m=retreat_distance,
                      measurement_cutoff='strictly before first pedestrian contact')
        if not moving or not reversed_rows or peak<=.1 or retreat_distance is None or retreat_distance<=.1:
            reasons.append('forward walking, reversed submitted command and physical retreat not established before contact')
    return {'enacted':not reasons,'reasons':reasons,**record}


def scene_qualification(audit):
    """Verify recorded native placement and initial vehicle ray obstruction.

    Self-mesh hits, pedestrian hits and road labels never establish occlusion.
    """
    if not isinstance(audit,dict) or not audit:
        return {'verified':None,'reason':'placement_audit.json unavailable; historical documentary evidence is not assumed to establish this per-run initial ray',
                'native_crosswalk_verified':None,'initial_vehicle_ray_blocker_recorded':None,'vehicle_ray_blockers':[]}
    native=audit.get('native_crosswalk',{})
    polygon=native.get('polygon');marked=(len(polygon)>=3) if isinstance(polygon,list) else None
    if native.get('native_marked_crosswalk') is False:marked=False
    extents=audit.get('occluder_extent_m')
    dimensions=(len(extents)==3 and all(finite(x) and x>0 for x in extents)) if isinstance(extents,list) else None
    distance=audit.get('camera_to_pedestrian_distance_m');ray=audit.get('driver_to_pedestrian_ray')
    blockers=[];ray_verified=None
    if isinstance(ray,list) and finite(distance) and distance>4:
        # CARLA semantic ray labels are descriptive classes, not actor IDs.
        for hit in ray:
            if not isinstance(hit,dict):continue
            label=str(hit.get('label','')).casefold()
            d=hit.get('distance_from_camera_m')
            if label in {'car','truck','bus','vehicle','vehicles','motorcycle'} and finite(d) and 3<d<distance-1:
                blockers.append(hit)
        ray_verified=bool(blockers)
    actor=audit.get('actors',{}).get('occluder',{})
    alive=actor.get('actor_alive')
    blueprint=audit.get('occluder_blueprint')
    blueprint_matches=(actor['blueprint']==blueprint) if blueprint and actor.get('blueprint') else None
    inputs=(marked,dimensions,ray_verified,alive if isinstance(alive,bool) else None,blueprint_matches)
    verified=False if any(v is False for v in inputs) else (True if all(v is True for v in inputs) else None)
    reasons=[]
    if not marked:reasons.append('native marked polygon not established')
    if not dimensions:reasons.append('positive physical occluder dimensions unavailable')
    if ray_verified is False:reasons.append('no initial physical-vehicle ray blocker beyond self-mesh and before pedestrian')
    elif ray_verified is None:reasons.append('usable initial driver/pedestrian ray measurement unavailable')
    if alive is not True:reasons.append('live occluder not established')
    if not blueprint_matches:reasons.append('requested/actual occluder blueprint not matched in audit')
    return {'verified':verified,'reasons':reasons,'native_crosswalk_verified':marked,
            'native_crosswalk':native,'occluder_blueprint':blueprint,'occluder_half_extents_m':extents,
            'occluder_actor':actor,'initial_vehicle_ray_blocker_recorded':ray_verified,
            'camera_to_pedestrian_distance_m':distance,'vehicle_ray_blockers':blockers,
            'driver_to_pedestrian_ray':ray,
            'rule':'physical vehicle class only; 3 m < distance from camera < pedestrian distance - 1 m',
            'scope':'initial central driver-camera-to-pedestrian ray only; semantic classes do not uniquely identify the occluder actor, prove full silhouette hiding, validate continuous approach visibility or establish pedestrian access'}


def qualify(root,mode,off,on,cfg):
    reasons=[];details={}
    if not off['complete'] or not on['complete']:reasons.append('incomplete or technically failed numeric evidence')
    if off['requested'] and on['requested'] and off['result'] and on['result']:
        try:details['matching']=check_pair(off['folder'],on['folder'])
        except Exception as exc:reasons.append('cannot verify matched physical/config conditions: '+str(exc))
        if details.get('matching') and not all(details['matching'].get(k) for k in ('requested_conditions_equal_except_assistance','physical_initial_match')):
            reasons.append('OFF/ON conditions or initial physical states do not match')
    else:reasons.append('matching inputs missing')
    behavior=(off.get('requested') or {}).get('pedestrian_behavior',cfg.get('pedestrian_behavior','cross'))
    details['pedestrian_behavior']=behavior
    saved=optional(root/'synchronization_checks.json',{}).get('pairs',{}).get(mode,{})
    controls=control_match(off,on)
    details['precontact_submitted_commands_match']=controls
    # The retained first package predates submitted-command logging. Its recorded
    # verification explicitly labels reconstructed schedules and precontact readbacks.
    legacy=(controls is None and saved.get('actual_walking_controls_equal_before_first_contact') is True
            and saved.get('requested_walking_schedule_identical_from_runtime_rule') is True)
    details['legacy_verified_schedule_reconstruction']=legacy
    if controls is not True and not legacy:reasons.append('matched submitted pedestrian commands not established')
    if behavior in ('wait','retreat'):
        details['qualification_rule']='nuisance comparison: matched physical/config conditions and walking schedule; intended center-arrival coincidence is not required'
        details['nuisance_enactment']={s:nuisance_enactment(r,behavior) for s,r in (('OFF',off),('ON',on))}
        if not all(v['enacted'] for v in details['nuisance_enactment'].values()):
            reasons.append('declared nuisance behavior or measured equivalent approach marker not enacted/established')
    else:
        a,b=off['encounter'],on['encounter']
        if not a or not b:
            reasons.append('measured synchronization assessment missing')
        else:
            e,f=a.get('trigger'),b.get('trigger')
            tolerance=cfg.get('encounter_timing',{}).get('tolerance_s',.25)
            pair_tolerance=cfg.get('encounter_timing',{}).get('pair_trigger_tolerance_s',1/60+.00001)
            trigger_delta=abs(e['at_s']-f['at_s']) if e and f else None
            ar=a.get('measured_freeflow_A_arrival_s');pr=b.get('undisturbed_pedestrian_arrival_s')
            delta=ar-pr if finite(ar) and finite(pr) else None
            details.update(OFF_timing_valid=a.get('valid'),ON_own_estimator_valid=b.get('valid'),
                           trigger_difference_s=trigger_delta,tolerance_s=tolerance,
                           OFF_reference_minus_ON_pedestrian_s=delta,
                           qualification_rule='measured OFF undisturbed/free-flow reference versus ON undisturbed pedestrian; same one-shot trigger and normal-walking commands')
            if a.get('valid') is not True:reasons.append('OFF measured center-arrival synchronization failed or missing')
            if trigger_delta is None or trigger_delta>pair_tolerance:reasons.append('matched one-shot trigger failed or missing')
            if delta is None or abs(delta)>tolerance:reasons.append('paired OFF-counterfactual / ON-pedestrian synchronization failed or missing')
            if a.get('failure') or b.get('failure'):reasons.append('runtime synchronized-trigger failure')
    scenes={s:scene_qualification(optional(r['folder']/'placement_audit.json')) for s,r in (('OFF',off),('ON',on))}
    statuses=[scene['verified'] for scene in scenes.values()]
    scene_verified=True if all(v is True for v in statuses) else (False if any(v is False for v in statuses) else None)
    return {'qualified':not reasons,'matched_timing_or_nuisance_protocol_qualified':not reasons,
            'initial_occluded_scene_verified':scene_verified,
            'occluded_effectiveness_qualified':not reasons and scene_verified is True,
            'initial_scene_audits':scenes,'reasons':reasons,
            'scene_qualification_note':'Timing/protocol matching and measured initial vehicle-ray occlusion are separate gates. Missing placement audits remain unknown, including the retained first package.',**details}


def contact_times(run,vehicle):
    frames={r['frame']:r['time_s'] for r in run['rows']}
    return [frames[e['frame']] for e in run['result'].get('pedestrian_contacts',[])
            if e.get('vehicle_id')==vehicle and e.get('frame') in frames]


def stopping_deadline(reference,vehicle,driver_response_s=.2):
    """Analytical latest warning time only for a recorded relevant OFF contact.

    This is a constant-speed/6 m/s² stopping model, not actual impact prediction.
    """
    if not reference.get('complete'):
        return {'deadline_s':None,'reason':'matched OFF numeric evidence incomplete or technically failed'}
    if not finite(driver_response_s) or driver_response_s<0:
        return {'deadline_s':None,'reason':'declared response assumption missing or invalid'}
    result=reference['result'];rows=reference['rows'];encounter=reference['encounter'] or {}
    trigger=encounter.get('trigger') or (result.get('encounter_timing') or {}).get('trigger')
    if not trigger or not rows:return {'deadline_s':None,'reason':'measured emergence/reference trajectory unavailable'}
    marker=trigger.get('at_s')
    if not finite(marker):return {'deadline_s':None,'reason':'measured emergence time unavailable'}
    candidates=[r for r in rows if r['time_s']<=marker+1e-6]
    if not candidates:return {'deadline_s':None,'reason':'no reference snapshot at or before emergence'}
    row=candidates[-1];v=row.get('vehicles',{}).get(vehicle,{})
    speed=v.get('along_speed_mps',v.get('speed_mps'))
    if not finite(speed) or speed<=.1:return {'deadline_s':None,'reason':'positive recorded receiver approach speed unavailable'}
    if v.get('guard'):return {'deadline_s':None,'reason':'reference receiver already constrained by guard'}
    endpoints=[]
    times=contact_times(reference,vehicle)
    point=trigger.get('conflict_center_xy_m')
    if times and point and finite(v.get('yaw_deg')) and len(v.get('location',[]))>=2 and finite(v.get('half_length_m')):
        yaw=math.radians(v['yaw_deg']);direction=(math.cos(yaw),math.sin(yaw))
        distance=sum((point[i]-v['location'][i])*direction[i] for i in range(2))
        available=distance-v['half_length_m']-.4-BUFFER
        latest=row['time_s']+(available-speed*speed/(2*DECELERATION))/speed-driver_response_s
        endpoints.append({'endpoint':'pedestrian','deadline_s':latest,'actual_contact_s':min(times),
                          'reference_time_s':row['time_s'],'speed_mps':speed,'center_distance_m':distance,
                          'available_stopping_distance_m':available,'response_s':driver_response_s})
    pair={'follower_b':('A_B','hero_car'),'follower_c':('B_C','follower_b')}.get(vehicle)
    if pair:
        record=result.get('secondary',{}).get(pair[0],{})
        frame_times={r['frame']:r['time_s'] for r in rows}
        contacts=[frame_times[e['frame']] for e in record.get('events',[]) if e.get('frame') in frame_times]
        gap=v.get('gap_m');leader=row.get('vehicles',{}).get(pair[1],{}).get('along_speed_mps')
        if record.get('contact') is True and contacts and finite(gap) and finite(leader) and leader>=0:
            available=gap-BUFFER+leader*leader/(2*DECELERATION)
            latest=row['time_s']+(available-speed*speed/(2*DECELERATION))/speed-driver_response_s
            endpoints.append({'endpoint':pair[0],'deadline_s':latest,'actual_contact_s':min(contacts),
                              'reference_time_s':row['time_s'],'speed_mps':speed,'bumper_gap_m':gap,
                              'predecessor_speed_mps':leader,'available_stopping_distance_m':available,
                              'response_s':driver_response_s})
    if not endpoints:return {'deadline_s':None,'reason':'no recorded relevant OFF contact with sufficient pre-event physical measurements'}
    earliest=min(endpoints,key=lambda x:x['deadline_s'])
    return {'deadline_s':earliest['deadline_s'],'limiting_endpoint':earliest['endpoint'],'endpoints':endpoints,
            'model_deceleration_mps2':DECELERATION,'model_stop_buffer_m':BUFFER,
            'method':'reference emergence snapshot; latest warning = marker + (available distance - v²/(2a))/v - declared response',
            'limitation':'constant recorded emergence speed; equal 6 m/s² predecessor braking for secondary model; achieved physical braking and continuing acceleration can differ. Analytical deadline is not actual contact time.'}


def used_warnings(run,vehicle):
    """Only fresh verified receipts associated with non-guard remote braking."""
    if run['result'].get('assistance') is not True:return []
    receipts=[m for m in run['messages'] if m.get('event')=='received' and m.get('receiver_id')==vehicle and m.get('channel') in CHANNELS]
    created=optional(run['folder']/'braking_events.json',[]) or []
    braking_ids={e.get('event_id') for e in created if e.get('event') in ('braking_created','braking_received')}
    byframe={row['frame']:row for row in run['rows']}
    observations={}
    for row in run['rows']:
        for source,obs in row.get('rgb_observations',{}).items():
            for observation in obs:
                observations[(source,round(observation.get('observed_at_s',-100),5))]=observation
    out=[];seen=set()
    for row in run['rows']:
        v=row.get('vehicles',{}).get(vehicle,{})
        if not finite(v.get('brake')) or v['brake']<=.05 or v.get('guard'):continue
        decision=v.get('decision',{});sources=set(decision.get('sources',[]))-{'driver','driver_camera',vehicle,'local_driver'}
        if not sources:continue
        age_used=decision.get('observation_age_s')
        for receipt in receipts:
            source=receipt.get('source_id',receipt.get('sender_id'))
            capture=receipt.get('observed_at_s');at=receipt.get('at_s');expiry=receipt.get('expires_at_s')
            if source not in sources or not all(finite(x) for x in (capture,at,expiry)):continue
            age=row['time_s']-capture
            if not 0<=age<=AGE_LIMIT+1e-6 or at>row['time_s']+1e-6 or expiry<row['time_s']:continue
            if not finite(age_used) or abs(age-age_used)>1e-4:continue
            identity=(receipt.get('event_id'),receipt['channel'])
            if identity in seen:continue
            matches_target=None;target_error=None
            observation=observations.get((source,round(capture,5)))
            if observation and observation.get('frame_id') in byframe:
                original=byframe[observation['frame_id']]
                target_error=math.dist(observation['position'],original['pedestrian_location'][:2])
                matches_target=target_error<=3.
            verified_braking=receipt.get('event_id') in braking_ids and source in VEHICLES
            if matches_target is not True and not verified_braking:continue
            seen.add(identity)
            out.append({'event_id':receipt.get('event_id'),'source_id':source,'channel':receipt['channel'],
                        'receipt_s':at,'observed_at_s':capture,'brake_use_s':row['time_s'],'source_age_at_use_s':age,
                        'receipt_to_used_brake_s':row['time_s']-at,'target_error_m':target_error,
                        'evidence':'evaluator-matched RGB pedestrian' if matches_target else 'recorded upstream braking event',
                        'actual_contact_s':min(contact_times(run,vehicle),default=None)})
    return sorted(out,key=lambda x:x['receipt_s'])


def fault_intervals(messages):
    pending={};intervals=[]
    for event in messages:
        if event.get('event') not in ('fault_started','fault_ended'):continue
        key=event.get('fault');spec=event.get('specification',{})
        if event['event']=='fault_started':
            pending[key]={'fault':key,'specification':spec,'actual_start_s':event.get('at_s'),'actual_end_s':None}
        elif key in pending:
            interval=pending.pop(key);interval['actual_end_s']=event.get('at_s');intervals.append(interval)
        else:intervals.append({'fault':key,'specification':spec,'actual_start_s':None,'actual_end_s':event.get('at_s'),'missing_start':True})
    return intervals+list(pending.values())


def communication_report(on,off,cfg,mode):
    response=.2 if mode=='Automatic' else on['result'].get('requested_config',{}).get('driver_response_s',cfg.get('driver_response_s'))
    deadlines={name:stopping_deadline(off,name,response) for name in VEHICLES}
    used={name:used_warnings(on,name) for name in VEHICLES}
    actual=fault_intervals(on['messages'])
    blocked=[e for e in on['messages'] if e.get('event')=='blocked']
    options=on['result'].get('requested_config',{})
    declared=options.get('channel_outages',cfg.get('channel_outages',[]))
    groups={}
    for spec in declared:
        key=(spec.get('start_s'),spec.get('end_s'))
        if not all(finite(x) for x in key):continue
        groups.setdefault(key,set()).add(spec.get('channel'))
    windows=[]
    for (start,end),channels in groups.items():
        channels=channels.intersection(CHANNELS)
        blocked_window=[e for e in blocked if e.get('channel') in channels and finite(e.get('at_s')) and start<=e['at_s']<end]
        outage_blocked=[e for e in blocked_window if e.get('reason')=='scheduled_outage']
        recipients={}
        for name in VEHICLES:
            alternate=[w for w in used[name] if w['channel'] not in channels and start<=w['receipt_s']<end]
            deadline=deadlines[name]['deadline_s']
            timely=[w for w in alternate if deadline is not None and w['receipt_s']<=deadline]
            recipients[name]={'modeled_stopping_deadline':deadlines[name],
                              'alternate_channel_used_warnings':alternate,
                              'alternate_usable_before_deadline':bool(timely) if deadline is not None and on['complete'] else None,
                              'first_timely_used_warning':timely[0] if timely else None,
                              'actual_blocked_send_count':sum(e.get('receiver_id')==name for e in blocked_window),
                              'actual_scheduled_outage_blocked_send_count':sum(e.get('receiver_id')==name for e in outage_blocked)}
        windows.append({'declared_start_s':start,'declared_end_s':end,'failed_channels':sorted(channels),
                        'all_implemented_channels_failed':channels==set(CHANNELS),
                        'actual_fault_transitions':[i for i in actual if i['specification'].get('channel') in channels and i['specification'].get('start_s')==start],
                        'actual_blocked_send_times_s':[e['at_s'] for e in blocked_window],
                        'scheduled_outage_blocked_send_times_s':[e['at_s'] for e in outage_blocked],
                        'actual_blocked_sends':blocked_window,'recipients':recipients})
    return {'response_assumption_s':response,'usable_source_age_ceiling_s':AGE_LIMIT,
            'requested_channel_outages':declared,'recorded_fault_intervals':actual,
            'actual_blocked_sends':blocked,'outage_windows':windows,'used_warning_associations':used,
            'modeled_deadlines':deadlines,
            'definition':'A usable warning here requires an actual receipt associated with subsequent non-guard cooperative brake use, source age ≤1 s at use, original packet expiry, and evaluator target match or recorded braking event. Receipt alone is not brake use.',
            'null_rule':'No recorded relevant OFF contact, incomplete physical reference, missing declared response or incomplete ON evidence leaves before-deadline success unknown. All-channel failure does not imply contact; queued earlier messages and local control can still act.',
            'fault_note':'Declared intervals, actual started/ended fault transitions, and observed blocked-send timestamps are different measurements. Sparse blocked sends do not prove continuous coverage of a requested outage.'}


def brake_metrics(run):
    rows=run['rows'];out={}
    for vehicle in VEHICLES:
        samples=[]
        for i,row in enumerate(rows[:-1]):
            v=row.get('vehicles',{}).get(vehicle,{})
            dt=rows[i+1]['time_s']-row['time_s']
            if not finite(dt) or dt<=0:continue
            if finite(v.get('brake')) and v['brake']>.05:
                samples.append((row,dt))
        out[vehicle]={'brake_command_duration_s':sum(dt for _,dt in samples) if rows else None,
                      'non_guard_brake_duration_s':sum(dt for r,dt in samples if not r['vehicles'][vehicle].get('guard')) if rows else None,
                      'guard_brake_duration_s':sum(dt for r,dt in samples if r['vehicles'][vehicle].get('guard')) if rows else None,
                      'first_brake_s':samples[0][0]['time_s'] if samples else None,
                      'first_guard_s':next((r['time_s'] for r in rows if r.get('vehicles',{}).get(vehicle,{}).get('guard')),None)}
    return out


def passage_times(run):
    trigger=(run['encounter'] or {}).get('trigger') or (run['result'].get('encounter_timing') or {}).get('trigger') or {}
    point=trigger.get('conflict_center_xy_m');rows=run['rows'];times={n:None for n in VEHICLES}
    if not point or not rows:return times
    yaw=rows[0]['vehicles']['hero_car'].get('yaw_deg')
    if not finite(yaw):return times
    direction=(math.cos(math.radians(yaw)),math.sin(math.radians(yaw)))
    for name in VEHICLES:
        for a,b in zip(rows,rows[1:]):
            if name not in a['vehicles'] or name not in b['vehicles']:continue
            d=[sum((point[i]-r['vehicles'][name]['location'][i])*direction[i] for i in range(2)) for r in (a,b)]
            if d[0]>0>=d[1]:
                times[name]=a['time_s']+d[0]/(d[0]-d[1])*(b['time_s']-a['time_s']);break
    return times


def contact_context(run):
    """Label upstream guard timing without altering raw contact outcomes."""
    rows=run['rows'];byframe={r['frame']:r for r in rows}
    firstguards={n:next((r['time_s'] for r in rows if r.get('vehicles',{}).get(n,{}).get('guard')),None) for n in VEHICLES}
    records=[]
    for vehicle in VEHICLES:
        events=[e for e in run['result'].get('pedestrian_contacts',[]) if e.get('vehicle_id')==vehicle]
        if events:records.append((f'pedestrian/{vehicle}',vehicle,min(events,key=lambda e:e.get('frame',math.inf))))
    for pair,receiver in (('A_B','follower_b'),('B_C','follower_c')):
        events=run['result'].get('secondary',{}).get(pair,{}).get('events',[])
        if events:records.append((pair,receiver,min(events,key=lambda e:e.get('frame',math.inf))))
    context=[]
    for endpoint,receiver,event in records:
        row=byframe.get(event.get('frame'));time=row['time_s'] if row else None
        upstream=VEHICLES[:VEHICLES.index(receiver)+1]
        guards=[{'vehicle':name,'guard_first_s':firstguards[name]} for name in upstream
                if finite(time) and finite(firstguards[name]) and firstguards[name]<time-1e-6]
        context.append({'endpoint':endpoint,'frame':event.get('frame'),'time_s':time,'precontact':event.get('precontact'),
                        'prior_upstream_or_own_guard_interventions':guards,
                        'potentially_guard_related':bool(guards) if time is not None else None,
                        'causal_note':'Contact follows upstream/own guard intervention and may be guard-related; cannot attribute it to crosswalk coordination failure without timeline evidence.' if guards else 'No earlier recorded upstream/own guard intervention; absence of this confound does not establish a cause.'})
    return context


def post_A_brake_markers(run):
    rows=run['rows'];result=run['result']
    trigger=(result.get('encounter_timing') or {}).get('trigger') or {}
    emergence=trigger.get('at_s',result.get('pedestrian_movement_trigger_s'))
    a=next((r['time_s'] for r in rows if (not finite(emergence) or r['time_s']>=emergence)
            and r.get('vehicles',{}).get('hero_car',{}).get('brake',0)>.05),None)
    out={'A_command_marker_s':a,'followers':{},
         'note':'First B/C brake commands can be startup speed regulation. These are commands at/after A marker, with recorded reasons, not a causal hazard-response identification.'}
    for name in ('follower_b','follower_c'):
        row=next((r for r in rows if a is not None and r['time_s']>=a and r.get('vehicles',{}).get(name,{}).get('brake',0)>.05),None)
        v=row['vehicles'][name] if row else {}
        out['followers'][name]={'time_s':row['time_s'] if row else None,'guard':v.get('guard'),
                                'reason':v.get('decision',{}).get('reason'),'sources':v.get('decision',{}).get('sources'),
                                'local_pathway':v.get('decision',{}).get('local_pathway')}
    return out


def public_run(run):
    result=run['result'];folder=run['folder']
    contacts=result.get('pedestrian_contacts')
    return {'evidence_path':str(folder),'outcomes':run['outcome'],'complete_numeric_run':run['complete'],
            'any_contact':any_contact(run['outcome']),
            'contact_free':not any_contact(run['outcome']) if any_contact(run['outcome']) is not None else None,
            'observation_window_coverage':run.get('window_coverage'),
            'pedestrian_contact_vehicles':sorted(set(e.get('vehicle_id') for e in contacts if e.get('vehicle_id'))) if isinstance(contacts,list) else None,
            'pedestrian_contacts_raw':contacts,'secondary_contacts_raw':result.get('secondary'),
            'minimum_physical_gaps_m':{p:v.get('minimum_physical_bumper_gap_m') for p,v in result.get('secondary',{}).items()},
            'physics_rows':len(run['rows']),'terminal_reason':result.get('terminal_reason'),
            'technical_failure':run['technical_failure_file'] or result.get('technical_failure'),
            'missing_files':run['missing'],'missing_measurements':result.get('missing_measurements'),
            'measurement_failures':optional(folder/'measurement_failures.json'),
            'encounter_assessment':run['encounter'],'recorded_braking_timeline':result.get('timeline'),
            'intended_encounter_triggered':(result.get('encounter_timing') or {}).get('trigger') is not None,
            'intended_encounter_failure':(result.get('encounter_timing') or {}).get('failure'),
            'contact_context':contact_context(run),'post_A_brake_command_markers':post_A_brake_markers(run),
            'brake_metrics':brake_metrics(run),'conflict_center_passage_s':passage_times(run),
            'requested_configuration':run['requested'],'effective_configuration':result.get('effective_config'),
            'actual_crosswalk':optional(folder/'crosswalk.json'),'placement_audit':optional(folder/'placement_audit.json'),
            'existing_run_assessment':run['assessment'],'asset_verification':optional(folder/'picture_verification.json'),
            'camera_manifest_entry_count':len(optional(folder/'camera_manifest.json',[]) or []),
            'nuisance_warning_counts':dict(Counter(e.get('event') for e in run['messages'] if e.get('event') in ('notification_delivered','delivered','refreshed','reacted','expired_before_reaction')))}


def aggregates(tests,qualified_only=False,qualification_key='qualified'):
    counts={mode:{state:{e:{'contacts':0,'known_complete_runs':0} for e in ENDPOINTS} for state in STATES} for mode in MODES}
    categories={mode:{e:Counter() for e in ENDPOINTS} for mode in MODES}
    for test in tests:
        for mode,pair in test['modes'].items():
            if qualified_only and not pair['qualification'].get(qualification_key):continue
            for state,run in pair['runs'].items():
                for endpoint,value in run['outcomes'].items():
                    if run['complete_numeric_run'] and isinstance(value,bool):
                        counts[mode][state][endpoint]['known_complete_runs']+=1
                        counts[mode][state][endpoint]['contacts']+=int(value)
            for endpoint,category in pair['matched_outcomes'].items():
                if all(r['complete_numeric_run'] for r in pair['runs'].values()):categories[mode][endpoint][category]+=1
                elif not qualified_only:categories[mode][endpoint]['incomplete pair']+=1
    return {'contact_runs':counts,'matched_pair_categories':{m:{e:dict(c) for e,c in by.items()} for m,by in categories.items()},
            'denominator_rule':'Only complete known binary endpoint measurements enter each run denominator. Qualified view additionally requires matched encounter qualification; all raw incomplete records remain in tests.'}


def contact_rates(tests,qualification_key=None):
    metrics=(*ENDPOINTS,'any_contact','contact_free')
    counts={m:{s:{e:{'event_runs':0,'known_complete_runs':0} for e in metrics} for s in STATES} for m in MODES}
    matched={m:{e:{'matched_pairs':0,'OFF_event_runs':0,'ON_event_runs':0} for e in metrics} for m in MODES}
    for test in tests:
        for mode,pair in test['modes'].items():
            if qualification_key and not pair['qualification'].get(qualification_key):continue
            values={}
            for state,run in pair['runs'].items():
                composite=any_contact(run['outcomes'])
                values[state]={**run['outcomes'],'any_contact':composite,'contact_free':not composite if composite is not None else None}
                for e,value in values[state].items():
                    if run['complete_numeric_run'] and isinstance(value,bool):
                        counts[mode][state][e]['known_complete_runs']+=1
                        counts[mode][state][e]['event_runs']+=int(value)
            if all(r['complete_numeric_run'] for r in pair['runs'].values()):
                for e in metrics:
                    if all(isinstance(values[s][e],bool) for s in STATES):
                        matched[mode][e]['matched_pairs']+=1
                        for state in STATES:matched[mode][e][state+'_event_runs']+=int(values[state][e])
    differences={m:{} for m in MODES}
    for mode in MODES:
        for e in metrics:
            for state in STATES:
                c=counts[mode][state][e];n=c['known_complete_runs']
                c['observed_rate']=c['event_runs']/n if n else None
            a,b=counts[mode]['OFF'][e],counts[mode]['ON'][e];p=matched[mode][e];n=p['matched_pairs']
            differences[mode][e]={'OFF_denominator':a['known_complete_runs'],'ON_denominator':b['known_complete_runs'],
                                  'ON_minus_OFF_event_runs':b['event_runs']-a['event_runs'],
                                  'ON_minus_OFF_rate_percentage_points':100*(b['observed_rate']-a['observed_rate']) if a['observed_rate'] is not None and b['observed_rate'] is not None else None,
                                  'matched_pair_denominator':n,'paired_OFF_event_runs':p['OFF_event_runs'],
                                  'paired_ON_event_runs':p['ON_event_runs'],
                                  'paired_ON_minus_OFF_event_runs':p['ON_event_runs']-p['OFF_event_runs'] if n else None,
                                  'paired_ON_minus_OFF_percentage_points':100*(p['ON_event_runs']-p['OFF_event_runs'])/n if n else None}
    return {'counts_and_observed_rates':counts,'absolute_OFF_to_ON_differences':differences,
            'note':'Signed ON−OFF absolute rate differences in percentage points are descriptive, not relative effectiveness or universal probabilities. Marginal denominators may differ; paired differences use only the same complete known OFF/ON cases.'}


def write_catalogue(study_root,tests):
    path=Path(study_root)/'test_catalogue.csv'
    with path.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.writer(handle)
        writer.writerow(['theme','theme_name','local_test_number','stable_global_id','test_name','seed','relative_path','complete_runs','timing_protocol_qualified_pairs','occluded_protocol_qualified_pairs','status'])
        for test in tests:
            complete=sum(r['complete_numeric_run'] for p in test['modes'].values() for r in p['runs'].values())
            timing=sum(p['qualification']['qualified'] for p in test['modes'].values())
            scenes=sum(p['qualification']['occluded_effectiveness_qualified'] for p in test['modes'].values())
            writer.writerow([test['theme'],test['theme_name'],test['local_test_number'],test['stable_global_id'],test['visible_name'],
                             test['catalogue'].get('seed'),test['catalogue']['relative_path'],complete,timing,scenes,
                             'complete; qualification reported separately' if complete==4 else f'{complete}/4 complete; missing/incomplete attempts retained'])
    return path


def build_report(plan_path,study_root=STUDY_ROOT):
    study_root=Path(study_root);plan,entries=read_plan(plan_path,study_root);tests=[]
    lines=['# Town10HD observed efficiency and robustness','',
           'Results are observed outcomes of a small, varied study. Raw recorded outcomes and qualified matched comparisons are reported separately. No universal effectiveness or assumed OFF-collision/ON-avoidance rate is inferred. Missing evidence is unknown, not a zero contact.','',
           f'[Frozen 48-run catalogue]({Path(plan_path).name}) · [Theme and local test names](../../src/reports/TOWN10HD_THEME_TEST_NAMES.md)','',
           '“Efficiency” here means measured contact changes, warning/use timing, braking burden and observed conflict-plane passage delay. It is not a universal safety percentage or a completed-trip efficiency measure.','',
           '| Test / theme | Mode | Timing/protocol | Initial occlusion verified | Pedestrian OFF → ON | A/B OFF → ON | B/C OFF → ON |','|---|---|---|---|---|---|---|']
    for entry,root in entries:
        cfg=optional(root/'test_config.json',{}) or {};theme=theme_id(entry,root)
        record={'catalogue':entry,'theme':theme,'theme_name':THEME_NAMES.get(theme,theme),'test_configuration':cfg,
                'recorded_configuration_amendments':{k:v for k,v in (cfg or entry).items() if 'amendment' in k},
                **visible_test_name(entry,root),'modes':{}}
        for mode in MODES:
            off,on=(load_run(root/mode/s) for s in STATES)
            q=qualify(root,mode,off,on,cfg)
            run_public={s:public_run(r) for s,r in (('OFF',off),('ON',on))}
            category=comparison(off['outcome'],on['outcome'])
            communication=communication_report(on,off,cfg,mode)
            off_pass,on_pass=passage_times(off),passage_times(on)
            behavior=q['pedestrian_behavior']
            deltas={n:on_pass[n]-off_pass[n] if finite(on_pass[n]) and finite(off_pass[n]) else None for n in VEHICLES}
            brake_delta={n:run_public['ON']['brake_metrics'][n]['non_guard_brake_duration_s']-run_public['OFF']['brake_metrics'][n]['non_guard_brake_duration_s']
                         if finite(run_public['ON']['brake_metrics'][n]['non_guard_brake_duration_s']) and finite(run_public['OFF']['brake_metrics'][n]['non_guard_brake_duration_s']) else None for n in VEHICLES}
            record['modes'][mode]={'qualification':q,'runs':run_public,'matched_outcomes':category,
                                   'communication':communication,'added_conflict_plane_delay_s':deltas,
                                   'ON_minus_OFF_non_guard_brake_duration_s':brake_delta,
                                   'nuisance_comparison':{'applicable':behavior in ('wait','retreat'),
                                       'added_braking_duration_s':brake_delta if behavior in ('wait','retreat') else None,
                                       'added_observed_passage_delay_s':deltas if behavior in ('wait','retreat') else None,
                                       'unnecessary_braking_classification':None,
                                       'note':'Added non-guard brake duration is measured burden, not automatically unnecessary braking. Waiting/retreating pedestrians can present temporary genuine hazards. Guard and startup regulation remain separately recorded.'}}
            title=record['visible_name']
            lines.append(f"| [{show(title)} / {theme}]({Path(entry['relative_path']).as_posix()}/report.md) | {mode} | {'qualified' if q['qualified'] else 'unqualified'} | {show(q['initial_occluded_scene_verified'])} | {show(off['outcome']['pedestrian'])} → {show(on['outcome']['pedestrian'])} | {show(off['outcome']['A_B'])} → {show(on['outcome']['A_B'])} | {show(off['outcome']['B_C'])} → {show(on['outcome']['B_C'])} |")
        tests.append(record)
    report={'frozen_plan':str(Path(plan_path).resolve()),'requested_test_count':len(entries),'requested_run_count':len(entries)*4,
            'complete_requested_runs':sum(r['complete_numeric_run'] for t in tests for p in t['modes'].values() for r in p['runs'].values()),
            'archived_failed_or_incomplete_attempts':archived_attempts(study_root),
            'observed_rates_all_complete':contact_rates(tests),
            'observed_rates_timing_protocol_qualified':contact_rates(tests,'qualified'),
            'observed_rates_occluded_protocol_qualified':contact_rates(tests,'occluded_effectiveness_qualified'),
            'tests':tests,'raw_complete_outcome_summary':aggregates(tests),'qualified_summary':aggregates(tests,True),
            'timing_or_nuisance_protocol_qualified_summary':aggregates(tests,True),
            'initial_occluded_scene_and_protocol_qualified_summary':aggregates(tests,True,'occluded_effectiveness_qualified'),
            'qualification_note':'Crossing cases require matched initial physics/configuration, one-shot trigger agreement, OFF measured center-arrival tolerance and ON matched undisturbed pedestrian. Nuisance cases require matched configuration/physical state and walking commands, not intended collision timing. Historical verified schedule reconstruction is explicitly labeled.',
            'communication_model_assumptions':{'deceleration_mps2':DECELERATION,'stop_buffer_m':BUFFER,'usable_source_age_ceiling_s':AGE_LIMIT,'automatic_response_s':.2,'retrofit_response_s':'declared requested driver_response_s; modeled, not calibrated human behavior'},
            'limits':['No contact proves neither universal effectiveness nor successful cooperation.',
                      'Timing failures remain raw results and are excluded from qualified crossing comparisons.',
                      'Declared guards, local delayed following and RGB perception can avoid contact with assistance OFF.',
                      'A received packet is not a used warning. Deadline metrics require recorded relevant OFF contact and valid reference geometry.',
                      'Modeled 6 m/s² stopping deadlines differ from measured achieved braking; continuing acceleration can invalidate constant-speed approximation.',
                      'Nuisance brake burden includes residual startup regulation unless source chronology supports tighter classification.',
                      'Missing crossing-plane passage is unknown delay; completed-trip delay is not measured.']}
    report['limits'] += ['This is a small purpose-selected set of stress/failure/nuisance cases, not a random road-user population; qualified subsets can differ in difficulty and source version.',
                         'Historical and remaining runs use documented different controller/timing/phone versions. Geometric initialization amendments for never-completed sites are recorded and matched within their pairs, not hidden as uniform study conditions.']
    catalogue=write_catalogue(study_root,tests)
    overview=['## Actual catalogue and sampled settings','',
              file_link('Current 12-test catalogue',catalogue),'',
              '| Theme / local test (stable ID) | Seed | Location | Daylight / weather | Occluder | Target km/h | Initial B/C gaps m |','|---|---:|---|---|---|---:|---|']
    for test,(entry,root) in zip(tests,entries):
        cfg=test['test_configuration'] or entry;followers=cfg.get('followers_automatic',{})
        gaps=[followers.get(n,{}).get('gap_m') for n in ('follower_b','follower_c')]
        overview.append(f"| {test['theme']} {file_link(test['visible_name'],root/'test_config.json')} | {show(cfg.get('seed'))} | {show(cfg.get('location'))} | {show(cfg.get('daylight'))} / {show(cfg.get('weather_label',cfg.get('weather')))} | {show(cfg.get('occluder'))} | {show(cfg.get('speed_kmh'))} | {show(gaps)} |")
    overview += ['', '| Test | Automatic B/C reaction s | Retrofit B/C local reaction s | Phone response s | Pedestrian m/s / movement | Fault settings |','|---|---|---|---:|---|---|']
    for test in tests:
        cfg=test['test_configuration'] or test['catalogue'];reaction=lambda key:[cfg.get(key,{}).get(n,{}).get('reaction_s') for n in ('follower_b','follower_c')]
        faults={k:cfg[k] for k in ('channel_outages','observation_outages','additional_occluder') if k in cfg}
        overview.append(f"| {test['theme']} {show(test['visible_name'])} | {show(reaction('followers_automatic'))} | {show(reaction('followers_retrofit'))} | {show(cfg.get('driver_response_s'))} | {show(cfg.get('pedestrian_speed_mps'))} / {show(cfg.get('pedestrian_behavior','cross'))} | {show(faults)} |")
    overview += ['',f"Complete requested runs currently available: **{report['complete_requested_runs']} / {report['requested_run_count']}**. Archived incomplete/technical attempts: **{report['archived_failed_or_incomplete_attempts']['count']}**, reported separately and excluded from the 48 complete-run denominator.",'',
                 'Location labels can reuse a native crossing; physical coordinates and placement audits remain authoritative. This is a small selected stress-study sample. Qualified subsets may differ in difficulty, scene visibility and source/control version, so their rates cannot be treated as universal effectiveness. Startup regulation, local sensing, guards, weather and recorded initialization amendments are material confounds.','']
    lines[8:8]=overview
    for heading,key in [('All complete runs','observed_rates_all_complete'),('Timing/protocol-qualified subset','observed_rates_timing_protocol_qualified'),('Initial occlusion plus protocol-qualified subset','observed_rates_occluded_protocol_qualified')]:
        rates=report[key]
        lines += ['',f'## Contact-free observations — {heading}','',
                  '| Mode | OFF contact-free / known runs | ON contact-free / known runs | OFF any-contact / known runs | ON any-contact / known runs | Paired ON−OFF contact-free difference |','|---|---|---|---|---|---|']
        for mode in MODES:
            states=rates['counts_and_observed_rates'][mode]
            text=lambda state,metric:f"{states[state][metric]['event_runs']} / {states[state][metric]['known_complete_runs']} ("+(f"{states[state][metric]['observed_rate']*100:.1f}%" if states[state][metric]['observed_rate'] is not None else 'unknown rate')+")"
            delta=rates['absolute_OFF_to_ON_differences'][mode]['contact_free']
            lines.append(f"| {mode} | {text('OFF','contact_free')} | {text('ON','contact_free')} | {text('OFF','any_contact')} | {text('ON','any_contact')} | {show(delta['paired_ON_minus_OFF_percentage_points'])} percentage points; n={delta['matched_pair_denominator']} |")
        lines+=['','Rates are measured fractions within this named subset. Paired differences use common complete-case denominators; incomplete or failed attempts do not become contact-free successes. Raw and qualified rates are not interchangeable.','']
        lines+=['| Mode | Endpoint | Complete matched pairs | OFF event runs | ON event runs | ON−OFF event runs | ON−OFF percentage points |','|---|---|---:|---:|---:|---:|---:|']
        for mode in MODES:
            for endpoint in (*ENDPOINTS,'any_contact'):
                delta=rates['absolute_OFF_to_ON_differences'][mode][endpoint]
                lines.append(f"| {mode} | {endpoint} | {delta['matched_pair_denominator']} | {delta['paired_OFF_event_runs']} | {delta['paired_ON_event_runs']} | {show(delta['paired_ON_minus_OFF_event_runs'])} | {show(delta['paired_ON_minus_OFF_percentage_points'])} |")
        lines+=['']
    lines += ['## Archived incomplete and technical attempts','',
              '| Original test | Mode / state | Attempt | Reason / classification | Recorded endpoint |','|---|---|---|---|---|']
    for attempt in report['archived_failed_or_incomplete_attempts']['records']:
        folder=Path(attempt['archive_path'])
        failure=attempt['technical_failure']
        reason=failure.get('error',failure.get('reason',failure.get('message'))) if isinstance(failure,dict) else failure
        reason=reason or attempt['preservation_reason']
        lines.append(f"| {show(attempt['test'])} | {show(attempt['mode'])} / {show(attempt['state'])} | {file_link(folder.name,folder/'attempt_preservation.json')} | {show(reason)}; {attempt['classification']} | {show(attempt['terminal_reason'])}; last t={show(attempt['window_coverage']['last_recorded_time_s'])} s |")
    lines+=['','Identical-seed retries do not erase archived failures. An archived initialization failure may have no physical encounter or contact measurements; those values remain unknown. The attempt count excludes earlier superseded study packages and diagnostics outside Run_Attempts.','']
    for heading,summary in [('All complete raw observations',report['raw_complete_outcome_summary']),
                            ('Timing/protocol-qualified matched results',report['qualified_summary']),
                            ('Initial occlusion and protocol-qualified results',report['initial_occluded_scene_and_protocol_qualified_summary'])]:
        lines += ['',f'## {heading}','', '| Mode | State | Pedestrian contact runs | A/B contact runs | B/C contact runs |','|---|---|---|---|---|']
        for mode in MODES:
            for state in STATES:
                c=summary['contact_runs'][mode][state]
                values=[f"{c[e]['contacts']} / {c[e]['known_complete_runs']}" for e in ENDPOINTS]
                lines.append(f'| {mode} | {state} | '+ ' | '.join(values)+' |')
        lines+=['','Counts are contact runs / complete known endpoint measurements. Different denominators and unqualified exclusions remain visible.','']
    lines+=['Initial scene qualification requires a recorded native marked polygon, positive physical occluder dimensions and matching live blueprint, plus a physical-vehicle center-ray hit more than 3 m from the camera and more than 1 m before the pedestrian. Self-mesh hits, pedestrian hits and road labels do not qualify. A missing per-run placement audit remains unknown, including the retained first package. A clear initial ray does not prove that later approach views remain clear; single-ray qualification does not prove full-silhouette hiding or pedestrian access.','',
            'All tables retain complete observation-window contacts, including later contacts following guard intervention. Such contacts are explicitly flagged as potentially guard-related and cannot be attributed to crosswalk coordination failure from contact presence alone.','']
    lines+=['## Timing, stopping deadlines and communication','',
            'A delivered packet is counted as used only when a later non-guard cooperative brake command identifies its source and capture age (≤1 s), the original packet is unexpired, and RGB target matching or a recorded braking event verifies relevance. Local driver-camera brakes and guard brakes are excluded from cooperative-use attribution. Receipt and used-brake timestamps are reported separately.','',
            'Deadline model: latest warning = reference marker + (available stopping distance − v²/(2a))/v − declared response; a = 6 m/s², 1 m stopping buffer, recorded emergence speed. Primary distance includes measured half-length and a 0.4 m pedestrian radius. Secondary distance additionally credits the predecessor stopping distance at the same declared deceleration. This analytical assumption is not measured achieved braking, actual impact timing or a guarantee.','',
            'A deadline is unknown when the matched OFF reference lacks a recorded relevant contact or necessary physical measurements. Actual contact times are separate. Alternate-channel before-deadline claims require verified used packets during the requested failure window; sparse blocked-send timestamps and actual fault transitions are preserved separately. All channels failing need not cause contact because local control and earlier queued messages can act.','']
    for test,(entry,root) in zip(tests,entries):
        cfg=test['test_configuration'] or entry
        relative=Path(entry['relative_path']).as_posix()
        lines += [f"## {show(test['visible_name'])} — {test['theme_name']}",'',
                  file_link('Configuration',root/'test_config.json')+' · '+file_link('Detailed test report',root/'report.md'),'',
                  f"Recorded source/geometric configuration amendments: {show(test['recorded_configuration_amendments'])}. Historical control versions and initialization amendments remain separate from each matched pair's settings.",'']
        for mode,pair in test['modes'].items():
            lines += [f'### {mode}','',f"Matched timing/protocol qualification: **{pair['qualification']['qualified']}**. Reasons: {show(pair['qualification']['reasons'])}. Initial occluded scene verified: **{show(pair['qualification']['initial_occluded_scene_verified'])}**. Combined qualification: **{pair['qualification']['occluded_effectiveness_qualified']}**.",'',
                      f"Measured ON minus OFF conflict-plane passage delay (s): {show(pair['added_conflict_plane_delay_s'])}. Added non-guard brake-command duration (s): {show(pair['ON_minus_OFF_non_guard_brake_duration_s'])}.",'']
            for state,run in pair['runs'].items():
                lines += [f"**{state}**: complete numeric evidence={run['complete_numeric_run']}; terminal={show(run['terminal_reason'])}; physics rows={run['physics_rows']}; recorded camera manifest entries={run['camera_manifest_entry_count']}. Asset verification={show(run['asset_verification'])}.",'',
                          f'[Results]({relative}/{mode}/{state}/results.json) · [Braking/observations]({relative}/{mode}/{state}/physical_timeline.json) · [Messages]({relative}/{mode}/{state}/messages.json) · [Assessment]({relative}/{mode}/{state}/assessment.json)','',
                          f"Minimum A/B and B/C gaps (m): {show(run['minimum_physical_gaps_m'])}. Guard/braking metrics: {show(run['brake_metrics'])}.",'',
                          f"Contact/guard chronology: {show(run['contact_context'])}. First B/C command markers can be startup regulation; post-A markers and their actual reasons are retained separately in the structured report.",'',
                          f"Missing files: {show(run['missing_files'])}; missing measurements: {show(run['missing_measurements'])}; technical failure: {show(run['technical_failure'])}.",'']
                if run['intended_encounter_failure']:
                    meaning=('An untriggered crossing pedestrian makes contact-free observations uninformative about the intended crossing challenge; the run is retained as a timing failure.'
                             if cfg.get('pedestrian_behavior','cross')=='cross' else
                             'This hypothetical crossing gate does not establish the nuisance behavior; qualification instead requires the recorded behavior and measured approach marker below.')
                    lines += [f"Intended encounter trigger failure: {show(run['intended_encounter_failure'])}. {meaning}",'']
                if pair['qualification'].get('nuisance_enactment'):
                    lines += [f"Recorded nuisance enactment/equivalent opportunity: {show(pair['qualification']['nuisance_enactment'][state])}.",'']
                media=root/mode/'assets'/state/'event_sets'
                for name in ('before_emergence','during_braking','outcome'):
                    asset=media/(name+'.jpg')
                    if asset.exists():lines += [f'![{mode} {state} {name.replace("_"," ")}]({asset.resolve().as_posix()})','']
            for window in pair['communication']['outage_windows']:
                lines += [f"Failure window {show(window['declared_start_s'])}–{show(window['declared_end_s'])} s; channels {show(window['failed_channels'])}; blocked-send samples={len(window['actual_blocked_send_times_s'])}.",'']
                for n,w in window['recipients'].items():
                    lines.append(f"- {n}: modeled deadline={show(w['modeled_stopping_deadline']['deadline_s'])} s; alternate channel verified-used before deadline={show(w['alternate_usable_before_deadline'])}.")
                lines+=['']
    lines += ['[Full structured evidence comparison](efficiency_report.json)','',
              'Pre-impact contact measurements, failed timing assessments, unknown deadlines and guard interventions remain in the structured report and original run files. Results do not imply every OFF run must collide or every ON run must avoid contact.','']
    save(study_root/'efficiency_report.json',report)
    (study_root/'efficiency_report.md').write_text('\n'.join(lines),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--plan',type=Path,default=STUDY_ROOT/'study_48_plan.json')
    parser.add_argument('--study-root',type=Path,default=STUDY_ROOT)
    args=parser.parse_args()
    report=build_report(args.plan,args.study_root)
    print(json.dumps({'requested_runs':report['requested_run_count'],
                      'qualified_pairs':sum(p['qualification']['qualified'] for t in report['tests'] for p in t['modes'].values())},indent=2))
