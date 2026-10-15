"""Offline evidence assembly for the frozen Town10HD twelve-run expansion.

Reads saved physics, RGB pictures and communications only. Never connects to CARLA.
Use pictures first, then videos, then verify (or report).
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import sys

from PIL import Image
from crossguard.paths import STUDY_ROOT
from crossguard.simulation.study_assessment import assess_run, check_pair

# Existing exporters also support direct invocation from src/tools.
TOOLS=Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0,str(TOOLS))
import assemble_town10_package as original
import synchronized_package as synchronized

MODES=('Automatic','Retrofit')
STATES=('OFF','ON')
VIEWS=original.VIEWS
DEFAULT_PLAN=STUDY_ROOT/'batch_12_plan.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def display(value):
    if value is None: return 'missing'
    if isinstance(value,float): return f'{value:.3f}' if math.isfinite(value) else 'invalid'
    if isinstance(value,(list,dict)): return json.dumps(value,ensure_ascii=False).replace('|','\\|')
    return str(value).replace('|','\\|').replace('\n',' ')


def run_evidence_links(root, mode, state):
    labels=(('Numeric results','results.json'),('Requested inputs','requested_config.json'),
            ('Braking and warning assessment','assessment.json'),('Causal timeline','causal_chronology.json'),
            ('Actual occupancy','conflict_occupancy.json'),('Measurement failures','measurement_failures.json'))
    links=[]
    for label,name in labels:
        relative=f'{mode}/{state}/{name}'
        links.append(f'[{label}]({relative})' if (root/relative).exists()
                     else f'{label}: missing (see numeric results and encounter qualification)')
    return ' · '.join(links)


def test_roots(plan_path=DEFAULT_PLAN):
    plan=read(plan_path)
    entries=plan.get('tests')
    if not isinstance(entries,list) or not entries:
        raise ValueError('Frozen catalogue requires a nonempty tests array')
    seen=set();root=STUDY_ROOT.resolve();tests=[]
    for entry in entries:
        relative=Path(entry['relative_path'])
        target=(root/relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(root) or target==root:
            raise ValueError('Test path must remain within Study_01_Town10HD')
        if target in seen: raise ValueError('Duplicate test path in frozen catalogue')
        seen.add(target);tests.append((entry,target))
    return plan,tests


@contextmanager
def package_context(root):
    previous=synchronized.ROOT
    env=os.environ.get('CROSSGUARD_PACKAGE_ROOT')
    synchronized.ROOT=Path(root)
    os.environ['CROSSGUARD_PACKAGE_ROOT']=str(root)
    try: yield
    finally:
        synchronized.ROOT=previous
        if env is None: os.environ.pop('CROSSGUARD_PACKAGE_ROOT',None)
        else: os.environ['CROSSGUARD_PACKAGE_ROOT']=env


def run_files(folder):
    required=('results.json','requested_config.json','physical_timeline.json','messages.json','camera_manifest.json')
    return [name for name in required if not (folder/name).is_file()]


def summarize_run(folder):
    """Missing runs return unknown outcomes, never invented zero contacts."""
    folder=Path(folder)
    missing=run_files(folder)
    result=read(folder/'results.json') if (folder/'results.json').exists() else {}
    rows=read(folder/'physical_timeline.json') if (folder/'physical_timeline.json').exists() else []
    messages=read(folder/'messages.json') if (folder/'messages.json').exists() else []
    assessment=read(folder/'assessment.json') if (folder/'assessment.json').exists() else {}
    failures=read(folder/'measurement_failures.json') if (folder/'measurement_failures.json').exists() else []
    encounter=read(folder/'encounter_assessment.json') if (folder/'encounter_assessment.json').exists() else None
    secondary=result.get('secondary',{})
    receipts={name:[e for e in messages if e.get('event')=='received' and e.get('receiver_id')==name]
              for name in original.NAMES}
    phone_events=[e for e in messages if e.get('event') in ('delivered','refreshed','reacted','expired_before_reaction','older_evidence')
                  and e.get('receiver_id') in original.NAMES]
    first_contact_frames=[e.get('frame') for e in result.get('pedestrian_contacts',[]) if e.get('frame') is not None]
    first_contact_frames += [e['frame'] for p in secondary.values() for e in p.get('events',[]) if 'frame' in e]
    cutoff=min(first_contact_frames) if first_contact_frames else None
    path_states={name:[] for name in original.NAMES}
    for name in path_states:
        after=False
        for row in rows:
            v=row.get('vehicles',{}).get(name,{})
            guard=bool(v.get('guard'))
            if guard and not after:
                path_states[name].append({'frame':row.get('frame'),'time_s':row.get('time_s'),
                                         'reason':v.get('decision',{}).get('guard_reason')})
            after=guard
    explicit_missing=list(result.get('missing_measurements',[]))
    explicit_missing += [{'file':name,'reason':'missing saved run evidence'} for name in missing]
    for name,w in assessment.get('warning_associations',{}).items():
        if w.get('missing_warning_association'):
            explicit_missing.append({'vehicle':name,'reason':'assisted brake lacks verified warning association'})
    if rows and any('pedestrian_submitted_control' not in row for row in rows):
        explicit_missing.append({'reason':'submitted walker commands missing from one or more physical rows; readbacks are separate'})
    if result and not encounter:
        explicit_missing.append({'reason':'encounter assessment unavailable'})
    passage={name:None for name in original.NAMES}
    point=(encounter or {}).get('trigger',{}).get('conflict_center_xy_m') if (encounter or {}).get('trigger') else None
    if rows and point:
        yaw=math.radians(rows[0]['vehicles']['hero_car']['yaw_deg'])
        forward=(math.cos(yaw),math.sin(yaw))
        for name in passage:
            distances=[sum((point[i]-row['vehicles'][name]['location'][i])*forward[i] for i in range(2)) for row in rows]
            passage[name]=synchronized.crossing_time(rows,distances)
    a_brake=result.get('timeline',{}).get('hero_car',{}).get('first_brake_command_s')
    at_brake=next((r for r in rows if a_brake is not None and r['time_s']>=a_brake),None)
    # Detection source classification is evaluator-only and never supplied to policy.
    return {'available':bool(result),'complete_required_numeric_files':not missing,
            'technical_failure':read(folder/'technical_failure.json') if (folder/'technical_failure.json').exists() else result.get('technical_failure'),
            'terminal_reason':result.get('terminal_reason'),'physics_rows':len(rows),
            'requested_configuration':read(folder/'requested_config.json') if (folder/'requested_config.json').exists() else None,
            'effective_configuration':result.get('effective_config'),
            'actual_crosswalk':read(folder/'crosswalk.json') if (folder/'crosswalk.json').exists() else None,
            'physical_placement_audit':read(folder/'placement_audit.json') if (folder/'placement_audit.json').exists() else None,
            'initial_measured_state':result.get('initial_measured_state'),
            'at_A_first_brake':at_brake,
            'conflict_center_passage_s':passage,
            'pedestrian_contact_vehicles':sorted(set(e.get('vehicle_id') for e in result.get('pedestrian_contacts',[]) if e.get('vehicle_id'))) if result else None,
            'raw_pedestrian_contact_callbacks':len(result.get('pedestrian_contacts',[])) if result else None,
            'secondary':{p:{'contact':v.get('contact'),'minimum_physical_bumper_gap_m':v.get('minimum_physical_bumper_gap_m'),
                            'raw_events':v.get('events',[]),
                            'raw_contact_callbacks':len(v.get('events',[]))} for p,v in secondary.items()},
            'contact_preimpact_records':assessment.get('first_contacts_by_actor_pair',[]),
            'timeline':result.get('timeline',{}),'brake_command_segments':assessment.get('brake_command_segments',{}),
            'physical_response_measurements':assessment.get('physical_response_measurements',{}),
            'warning_associations':assessment.get('warning_associations',{}),
            'target_matched_RGB_detections':assessment.get('target_matched_rgb_detections',{}),
            'packet_receipts':{n:{'count':len(es),'first_any_packet_s':es[0].get('at_s') if es else None,
                                 'channels':dict(Counter(e.get('channel','unknown') for e in es))} for n,es in receipts.items()},
            'phone_events_by_kind':dict(Counter(e['event'] for e in phone_events)),
            'phone_response_events':[e for e in phone_events if e['event']=='reacted'],
            'network_failures':[e for e in messages if e.get('event') in ('lost','dropped','expired','blocked','outage')],
            'actual_outages':result.get('actual_outages'),
            'guard_interventions':path_states,'first_any_contact_frame':cutoff,
            'encounter':encounter,'invalid_measurements':failures,
            'explicit_missing_measurements':explicit_missing,
            'unnecessary_braking':result.get('unnecessary_braking'),
            'limitations':result.get('limitations',[])+assessment.get('limitations',[])}


def compare_controls(offrows,onrows,offresult,onresult):
    """Compare actual submitted commands; keep raw readback reliability distinct."""
    contacts=[e for r in (offresult,onresult) for e in r.get('pedestrian_contacts',[])]
    times=[]
    for result,rows in ((offresult,offrows),(onresult,onrows)):
        byframe={r['frame']:r['time_s'] for r in rows}
        times += [byframe[e['frame']] for e in result.get('pedestrian_contacts',[]) if e.get('frame') in byframe]
    cutoff=min(times) if times else None
    pre_off=[r for r in offrows if cutoff is None or r['time_s']<cutoff]
    pre_on=[r for r in onrows if cutoff is None or r['time_s']<cutoff]
    aligned=len(pre_off)==len(pre_on) and bool(pre_off) and all(abs(a['time_s']-b['time_s'])<=1e-5 for a,b in zip(pre_off,pre_on))
    submitted_present=all(r.get('pedestrian_submitted_control') is not None for r in offrows+onrows) and bool(offrows) and bool(onrows)
    return {'first_pedestrian_contact_cutoff_s':cutoff,
            'missing_contact_time_records':len(contacts)-len(times),
            'precontact_rows_aligned':aligned,
            'submitted_control_present_all_rows':submitted_present,
            'submitted_controls_equal_before_first_contact':aligned and all(a.get('pedestrian_submitted_control')==b.get('pedestrian_submitted_control') for a,b in zip(pre_off,pre_on)) if submitted_present else None,
            'submitted_controls_equal_full_window':len(offrows)==len(onrows) and all(a['pedestrian_submitted_control']==b['pedestrian_submitted_control'] for a,b in zip(offrows,onrows)) if submitted_present else None,
            'readbacks_equal_before_first_contact':aligned and all(a.get('pedestrian_control')==b.get('pedestrian_control') for a,b in zip(pre_off,pre_on)),
            'note':'Submitted normal-walking commands, recorded readbacks and impact-displaced physical trajectories are distinct evidence.'}


def pair_summary(root,mode):
    off,on=root/mode/'OFF',root/mode/'ON'
    required=('results.json','requested_config.json','physical_timeline.json','encounter_assessment.json')
    missing=[f'{s}/{n}' for s in STATES for n in required if not (root/mode/s/n).exists()]
    if missing:return {'qualified':False,'missing':missing}
    check=check_pair(off,on)
    a,b=read(off/'encounter_assessment.json'),read(on/'encounter_assessment.json')
    e,f=a.get('trigger'),b.get('trigger')
    difference=abs(e['at_s']-f['at_s']) if e and f else None
    cfg=read(root/'test_config.json')
    declared=cfg.get('encounter_timing',{})
    tolerance=declared.get('tolerance_s',e.get('timing_tolerance_s',.25) if e else .25)
    pair_tolerance=declared.get('pair_trigger_tolerance_s',1/60+.00001)
    counterfactual=a.get('measured_freeflow_A_arrival_s')
    undisturbed=b.get('undisturbed_pedestrian_arrival_s')
    arrival_delta=counterfactual-undisturbed if number(counterfactual) and number(undisturbed) else None
    controls=compare_controls(read(off/'physical_timeline.json'),read(on/'physical_timeline.json'),read(off/'results.json'),read(on/'results.json'))
    check.update(trigger_difference_s=difference,trigger_match=difference is not None and difference<=pair_tolerance,
                 timing_tolerance_s=tolerance,OFF_undisturbed_reference_A_arrival_s=counterfactual,
                 ON_undisturbed_pedestrian_arrival_s=undisturbed,
                 paired_counterfactual_arrival_difference_s=arrival_delta,
                 paired_counterfactual_match=arrival_delta is not None and abs(arrival_delta)<=tolerance,
                 OFF_synchronization_valid=a.get('valid',False),ON_own_estimator_valid=b.get('valid',False),
                 actual_control_comparison=controls,
                 note='OFF measured free-flow reference assesses ON counterfactual conflict; ON estimator failure remains recorded separately.')
    check['qualified']=all((check['requested_conditions_equal_except_assistance'],check['physical_initial_match'],
                            check['trigger_match'],check['paired_counterfactual_match'],check['OFF_synchronization_valid'],
                            controls['submitted_controls_equal_before_first_contact'] is True,
                            controls['missing_contact_time_records']==0))
    check['added_distance_at_terminal_m']={name:read(on/'results.json').get('timeline',{}).get(name,{}).get('distance_travelled_m',0)-read(off/'results.json').get('timeline',{}).get(name,{}).get('distance_travelled_m',0)
                                          if number(read(on/'results.json').get('timeline',{}).get(name,{}).get('distance_travelled_m')) and number(read(off/'results.json').get('timeline',{}).get(name,{}).get('distance_travelled_m')) else None for name in original.NAMES}
    check['delay_limitation']='Terminal traveled distance difference is not a measured completed-trip delay. Missing route passage leaves delay unknown.'
    off_summary,on_summary=summarize_run(off),summarize_run(on)
    check['added_conflict_center_passage_delay_s']={name:on_summary['conflict_center_passage_s'][name]-off_summary['conflict_center_passage_s'][name]
                                                 if number(on_summary['conflict_center_passage_s'][name]) and number(off_summary['conflict_center_passage_s'][name]) else None
                                                 for name in original.NAMES}
    check['passage_delay_note']='Measured passage of the shared conflict center plane; only computed when both passages occur within the observation window, not completed-trip delay.'
    return check


def verify_pictures(folder):
    manifest=read(folder/'camera_manifest.json');rows=read(folder/'physical_timeline.json')
    frames={r['frame'] for r in rows};media=original.assets(folder)
    complete=0;images=0;failures=[]
    for entry in manifest:
        if set(entry.get('views',{}))!=set(VIEWS) or entry.get('missing') or entry.get('frame') not in frames:
            failures.append({'frame':entry.get('frame'),'reason':'incomplete six-view set or missing physical frame'});continue
        good=True
        for view in VIEWS:
            try:
                with Image.open(media/'pictures'/entry['views'][view]) as im:
                    im.load()
                    if im.size!=(960,540):raise ValueError(f'unexpected size {im.size}')
                images+=1
            except Exception as exc:
                good=False;failures.append({'frame':entry.get('frame'),'view':view,'reason':str(exc)})
        complete+=int(good)
    return {'manifest_entries':len(manifest),'complete_synchronized_six_view_sets':complete,
            'fully_decoded_pictures':images,'physics_rows':len(rows),'failures':failures,
            'passed':bool(manifest) and complete==len(manifest) and not failures}


def verify_video(path):
    import cv2
    path=Path(path)
    if not path.exists():return {'passed':False,'missing':str(path)}
    reader=cv2.VideoCapture(str(path));count=0;geometry=True
    fps=reader.get(cv2.CAP_PROP_FPS)
    while True:
        ok,img=reader.read()
        if not ok:break
        count+=1;geometry=geometry and img.shape[:2]==(540,1920)
    reader.release()
    return {'passed':count==201 and geometry and abs(fps-10)<.01,
            'decoded_frames':count,'fps':fps,'size':[1920,540] if geometry and count else None,
            'source':'actual labeled six-view RGB; nearest 0.1 s trial alignment; no audio'}


def pictures(plan_path):
    _,tests=test_roots(plan_path)
    for entry,root in tests:
        failures=[]
        with package_context(root):
            for mode in MODES:
                for state in STATES:
                    folder=root/mode/state
                    missing=run_files(folder)
                    if missing:
                        failures.append({'run':f'{mode}/{state}','missing':missing});continue
                    try:
                        synchronized.evaluate(folder)
                        assess_run(folder)
                        original.event_sets(folder)
                        result=read(folder/'results.json')
                        source=Path(result['source_trial_path']).parent/'runtime_source'
                        if source.is_dir():shutil.copytree(source,folder/'provenance'/'runtime_source',dirs_exist_ok=True)
                        save(folder/'picture_verification.json',verify_pictures(folder))
                        print('PICTURES '+str(folder),flush=True)
                    except Exception as exc:
                        failures.append({'run':f'{mode}/{state}','error':str(exc)})
        save(root/'offline_assembly_failures.json',failures)
    report(plan_path,verify=False)


def videos(plan_path):
    _,tests=test_roots(plan_path)
    for _,root in tests:
        errors=[]
        # Existing video assembly expects two complete mode pairs, so preserve
        # incomplete tests as explicit failures instead of copying substitute media.
        for mode in MODES:
            for state in STATES:
                verification=root/mode/state/'picture_verification.json'
                if not verification.exists() or not read(verification).get('passed'):
                    errors.append({'run':f'{mode}/{state}','reason':'pictures not completely verified'})
        if not errors:
            try:
                with package_context(root):original.videos()
            except Exception as exc:errors.append({'reason':str(exc)})
        save(root/'offline_video_failures.json',errors)


def report(plan_path,verify=True):
    plan,tests=test_roots(plan_path)
    batch={'frozen_plan':str(Path(plan_path).resolve()),'requested_run_count':len(tests)*4,
           'expected_comparison_videos':len(tests)*12,'tests':[]}
    index=[f'# Town10HD {len(tests)*4}-run evidence package','',
           'Each test contains Automatic and Retrofit OFF/ON runs. Outcomes are observations of this small, varied sample; no universal effectiveness estimate is inferred. Diagnostics and archived attempts are excluded from the completed-study total. Missing and failed evidence remains visible.','',
           f'[Frozen catalogue]({Path(plan_path).name}) · [Protocol](remaining_runs_protocol.md) · [Interpretation notes](batch_12_interpretation_notes.md)','',
           '| Test | Seed | Location | Daylight / weather | Complete numeric runs | Qualified pairs |','|---|---:|---|---|---:|---:|']
    for entry,root in tests:
        record={'catalogue':entry,'runs':{},'pairs':{},'videos':{}}
        lines=[f"# {entry.get('test',root.name)} — Town10HD",'',
               f"Seed **{entry.get('seed')}**; location: {display(entry.get('location'))}; daylight: {display(entry.get('daylight'))}; weather: {display(entry.get('weather_label',entry.get('weather')))}.",'',
               '[Frozen configuration](test_config.json) · [Measured comparison](batch_comparison.json) · [Synchronization checks](synchronization_checks.json)','',
               'Only assistance availability changes within each matched OFF/ON pair. Pedestrian motion is triggered once using measured approach timing; subsequent assistance braking does not retime walking. Actual occupancy, recorded submitted controls and impact-displaced trajectories are assessed separately.','',
               '| Run | Pedestrian-contact vehicles | A/B | B/C | Minimum A/B / B/C gap m | Terminal reason | Physics rows | Complete six-view sets |',
               '|---|---|---|---|---|---|---:|---:|']
        for mode in MODES:
            for state in STATES:
                folder=root/mode/state;key=f'{mode} {state}'
                try:summary=summarize_run(folder)
                except Exception as exc:summary={'available':False,'offline_error':str(exc),'explicit_missing_measurements':[str(exc)]}
                if verify and not run_files(folder):
                    try:save(folder/'picture_verification.json',verify_pictures(folder))
                    except Exception as exc:summary['picture_verification_error']=str(exc)
                if (folder/'picture_verification.json').exists():summary['picture_verification']=read(folder/'picture_verification.json')
                record['runs'][key]=summary
                pairs=summary.get('secondary',{})
                gap=' / '.join(display(pairs.get(p,{}).get('minimum_physical_bumper_gap_m')) for p in ('A_B','B_C'))
                contacts=summary.get('pedestrian_contact_vehicles')
                lines.append(f"| {key} | {', '.join(contacts) if contacts else ('none observed' if contacts==[] else 'missing')} | {display(pairs.get('A_B',{}).get('contact'))} | {display(pairs.get('B_C',{}).get('contact'))} | {gap} | {display(summary.get('terminal_reason'))} | {summary.get('physics_rows','missing')} | {summary.get('picture_verification',{}).get('complete_synchronized_six_view_sets','unverified')} |")
        for mode in MODES:
            try:record['pairs'][mode]=pair_summary(root,mode)
            except Exception as exc:record['pairs'][mode]={'qualified':False,'offline_error':str(exc)}
        lines+=['','Braking chronology distinguishes startup regulation, hazard response and guard interventions. Positive measured bumper gaps and stopping trajectories explain secondary avoidance; zero secondary contacts alone do not prove coordination. Contact callbacks can repeat during sustained contact and reciprocal sensor reporting.','']
        for mode in MODES:
            lines += [f'## {mode} matched pair','',f"Synchronization qualification: **{record['pairs'][mode]['qualified']}**. The OFF undisturbed/free-flow reference assesses ON intended coincidence; failed ON extrapolation checks remain recorded. Full verification is linked above.",'',
                      f"Added measured conflict-plane passage delay ON minus OFF (s): {display(record['pairs'][mode].get('added_conflict_center_passage_delay_s'))}. Missing passage is unknown; this is not a completed-trip delay.",'']
            for state in STATES:
                key=f'{mode} {state}';s=record['runs'][key];folder=root/mode/state
                lines += [f'### {state}','',run_evidence_links(root,mode,state),'',
                          f"Braking times A/B/C (s): {display({n:v.get('first_brake_command_s') for n,v in s.get('timeline',{}).items()})}. Guard intervention times: {display({n:v.get('guard_first_s') for n,v in s.get('timeline',{}).items()})}.",'',
                          f"Phone events: {display(s.get('phone_events_by_kind',{}))}. Associated usable warning timing, channel, response delay and pre-impact measurements are retained in the measured comparison and assessment. First packet receipt is not automatically a usable warning.",'',
                          f"Missing measurements: {display(s.get('explicit_missing_measurements',[]))}. Invalid measurement records: {len(s.get('invalid_measurements',[]))}. Technical failure: {display(s.get('technical_failure'))}.",'']
                if s.get('available'):
                    media=original.assets(folder)
                    for name in ('before_emergence','during_braking','intended_encounter','outcome','terminal'):
                        asset=media/'event_sets'/(name+'.jpg')
                        if asset.exists():lines += [f'![{name.replace("_"," ")}]({asset.relative_to(root).as_posix()})','']
                    plot=media/'braking_timeline.png'
                    if plot.exists():lines += [f'![Measured speed, acceleration, brake and physical gaps]({plot.relative_to(root).as_posix()})','']
            video_links=[]
            for view in VIEWS:
                target=root/mode/'assets'/(view+'_OFF_ON.mp4')
                verification=verify_video(target) if verify else {'passed':False,'status':'not decoded in pictures phase'}
                record['videos'][f'{mode}/{view}']=verification
                if target.exists():
                    save(target.with_suffix('.verification.json'),verification) if verify else None
                    video_links.append(f'[{view}]({target.relative_to(root).as_posix()})')
            lines += ['Comparison videos (full decode status in package verification): '+(' · '.join(video_links) if video_links else 'not generated yet')+'.','']
        lines += ['Completed-trip delay is unknown unless both vehicles finish a comparable route. Terminal traveled-distance differences are recorded separately. Crossing-case braking is not automatically classified as unnecessary braking. Async RGB completion and usable selected capture frames can vary and are recorded.','']
        save(root/'batch_comparison.json',record)
        save(root/'synchronization_checks.json',{'runs':{k:v.get('encounter') for k,v in record['runs'].items()},'pairs':record['pairs']})
        save(root/'package_verification.json',{'runs':{k:v.get('picture_verification') for k,v in record['runs'].items()},
                                              'videos':record['videos'],'pairs':record['pairs'],
                                              'all_pairs_qualified':all(p.get('qualified') for p in record['pairs'].values())})
        (root/'report.md').write_text('\n'.join(lines),encoding='utf-8')
        count=sum(v.get('complete_required_numeric_files',False) for v in record['runs'].values())
        qualified=sum(v.get('qualified',False) for v in record['pairs'].values())
        index.append(f"| [{entry.get('test',root.name)}]({Path(entry['relative_path']).as_posix()}/report.md) | {entry.get('seed')} | {display(entry.get('location'))} | {display(entry.get('daylight'))} / {display(entry.get('weather_label',entry.get('weather')))} | {count} | {qualified} / 2 |")
        batch['tests'].append(record)
    batch['completed_numeric_runs']=sum(v.get('complete_required_numeric_files',False) for test in batch['tests'] for v in test['runs'].values())
    batch['fully_verified_videos']=sum(v.get('passed',False) for test in batch['tests'] for v in test['videos'].values())
    batch['qualified_pairs']=sum(v.get('qualified',False) for test in batch['tests'] for v in test['pairs'].values())
    prefix='study_48' if Path(plan_path).stem=='study_48_plan' else 'batch_12'
    save(STUDY_ROOT/(prefix+'_comparison.json'),batch)
    index += ['',f"Saved complete numeric runs: **{batch['completed_numeric_runs']} / {batch['requested_run_count']}**. Fully decoded comparison videos: **{batch['fully_verified_videos']} / {batch['expected_comparison_videos']}**. Qualified matched synchronization pairs: **{batch['qualified_pairs']} / {len(tests)*2}**.",'',
              f'[Detailed measured batch comparison]({prefix}_comparison.json)','',
              'Native rain/light variations affect RGB visibility; no tire-friction changes were introduced. See [CARLA WeatherParameters](https://carla.readthedocs.io/en/latest/python_api/#carlaweatherparameters).', '',
              'This batch covers the conditions listed in its frozen catalogue and does not establish universal success across Town10HD. Failed synchronization remains reported. Nuisance-case qualification and the separate physical-occlusion qualification are evaluated in efficiency_report.md.','']
    (STUDY_ROOT/(prefix+'_report.md')).write_text('\n'.join(index),encoding='utf-8')
    print(json.dumps({k:batch[k] for k in ('completed_numeric_runs','fully_verified_videos','qualified_pairs')},indent=2))
    return batch


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('pictures','videos','verify','report'))
    parser.add_argument('--plan',type=Path,default=DEFAULT_PLAN)
    args=parser.parse_args()
    if args.action=='pictures':pictures(args.plan)
    elif args.action=='videos':videos(args.plan)
    else:report(args.plan,verify=True)
