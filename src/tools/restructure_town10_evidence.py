"""Reorganize delivered evidence without CARLA access or changes to raw measurements."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import textwrap

from crossguard.paths import STUDY_ROOT

THEMES={1:'Crosswalk_Location',2:'Weather_and_Visibility',3:'Following_and_Secondary_Collisions',
        4:'Communication_Failures',5:'Observation_Loss_and_Occlusion',6:'Waiting_and_Retreating_Pedestrians'}
TESTS={1:(3,1,'Close_Following_Crossing'),2:(3,2,'Marginal_Stopping_Distance'),
       3:(1,1,'Morning_Bus_Crossing'),4:(1,2,'Noon_Truck_Crossing'),5:(2,1,'Afternoon_Rain'),
       6:(2,2,'Overcast_Visibility'),7:(4,1,'Individual_Channel_Outages'),8:(4,2,'All_Channel_Outage'),
       9:(5,1,'Brief_Observation_Loss'),10:(5,2,'Extra_Occlusion_and_Observation_Loss'),
       11:(6,1,'Waiting_Pedestrian'),12:(6,2,'Retreating_Pedestrian')}
VIEWS={'top':'Overhead','driver':'Driver_A','person':'Pedestrian','opposite':'Opposite',
       'driver_b':'Driver_B','driver_c':'Driver_C'}
MODES=('Automatic','Retrofit')
STATES=('OFF','ON')
PURPOSES={1:'Compare native crosswalk locations and bus/truck occluders.',
          2:'Examine daylight, weather and RGB visibility.',
          3:'Examine following distance, response delay, stopping margin and secondary contacts.',
          4:'Examine individual communication channel outages, combined outages and redundancy.',
          5:'Examine missing observations and additional physical occlusion.',
          6:'Examine waiting or retreating pedestrians and braking burden.'}

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2),encoding='utf-8')
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def bounded(root,path):
    path=Path(path).resolve()
    if not path.is_relative_to(root.resolve()) or path==root.resolve():raise ValueError(path)
    return path
def translate(path,moves):
    value=str(path)
    for source,target in moves:
        prefix=str(source)
        if value==prefix:value=str(target)
        elif value.startswith(prefix+os.sep):value=str(target)+value[len(prefix):]
    return Path(value)
def report_target(name):
    if name=='efficiency_report.md':return Path('Reports/Efficiency_Report.md')
    if name=='study_48_report.md':return Path('Reports/Evidence_Index.md')
    if name.endswith('plan.json') or name in ('study_plan.md','remaining_runs_protocol.md','batch_12_protocol.md'):
        return Path('Reports/Study_Configuration')/name
    if name=='README.md':return Path('Reports/Technical_Records/Previous_Study_Index.md')
    return Path('Reports/Technical_Records')/name

def plan_moves(root,plan):
    moves=[];rows=[];themes={}
    for entry in plan['tests']:
        n=int(entry['test'].split('_')[1]);theme,local,name=TESTS[n]
        old=bounded(root,root/entry['relative_path'])
        new=root/'Tests'/f'Theme_{theme:02}_{THEMES[theme]}'/f'Test_{local:02}_{name}'
        moves.append((old,new));rows.append((entry,new,theme,local,name))
        themes[old.parent]=new.parent
        for mode in MODES:
            for state in STATES:
                moves.append((new/mode/state,new/mode/f'Assistance_{state}'))
                moves.append((new/mode/'assets'/state,new/mode/f'Assistance_{state}'/'assets'))
            for view,label in VIEWS.items():
                for ext in ('.mp4','.verification.json'):
                    moves.append((new/mode/'assets'/(view+'_OFF_ON'+ext),
                                  new/mode/'Comparison'/(label+'_Assistance_OFF_vs_ON'+ext)))
    for old,new in themes.items():moves.append((old/'README.md',new/'README.md'))
    # Windows treats diagnostics and Diagnostics as the same directory. Stage
    # the original outside that directory before creating the new nesting.
    staging=root/'.diagnostics_restructure_staging'
    moves.append((root/'diagnostics',staging))
    moves.append((staging,root/'Diagnostics'/'Sudden_Braking'))
    for item in root.iterdir():
        if item.is_file():moves.append((item,root/report_target(item.name)))
        elif item.name.endswith('provenance'):moves.append((item,root/'Reports'/'Technical_Records'/item.name))
    return moves,rows,themes

def reorganize(root,apply=False):
    root=root.resolve();layout=root/'Reports/Technical_Records/evidence_layout.json'
    legacy=root/'evidence_layout.json'
    if legacy.exists():
        if apply:
            layout.parent.mkdir(parents=True,exist_ok=True);legacy.rename(layout)
        else:layout=legacy
    if layout.exists():
        if apply:write_overviews(root)
        print('Already reorganized; overviews refreshed when --apply is specified.');return
    plan=read(root/'study_48_plan.json');moves,rows,themes=plan_moves(root,plan)
    for a,b in moves:
        bounded(root,a);bounded(root,b)
    for _,target,_,_,_ in rows:print('Test folder: '+str(target.relative_to(root)),flush=True)
    print(f'Validated {len(moves)} directory/file moves.',flush=True)
    if not apply:return
    originals=root/'Reports'/'Technical_Records'/'Presentation_Before_Reorganization'
    manifest_path=originals/'original_file_manifest.json'
    if manifest_path.exists():
        # Resume a filesystem interruption using the original inventory, never
        # replace it with an inventory of the partially reorganized tree.
        manifests=read(manifest_path)
        mapping={item['original_path']:item['current_path'] for item in manifests}
        markdown={}
        for item in manifests:
            p=Path(item['original_path'])
            if p.suffix!='.md' or 'Failed' in p.relative_to(root).parts:continue
            backup=originals/p.relative_to(root)
            try:markdown[p]=backup.read_text(encoding='utf-8-sig')
            except UnicodeDecodeError:markdown[p]=backup.read_text(encoding='cp1252')
    else:
        files=[p for p in root.rglob('*') if p.is_file() and 'Presentation_Before_Reorganization' not in p.parts]
        mapping={str(p):str(translate(p,moves)) for p in files}
        manifests=[];markdown={}
        for p in files:
            record={'original_path':str(p),'current_path':mapping[str(p)],'bytes':p.stat().st_size}
            if p.suffix in ('.json','.md'):record['sha256']=digest(p)
            manifests.append(record)
            if p.suffix=='.md' and 'Failed' not in p.relative_to(root).parts:
                try:markdown[p]=p.read_text(encoding='utf-8-sig')
                except UnicodeDecodeError:markdown[p]=p.read_text(encoding='cp1252')
            if p.suffix=='.md' or p.name in ('test_config.json','study_48_plan.json','batch_12_plan.json'):
                target=originals/p.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
        save(manifest_path,manifests)
    for source,target in moves:
        if not source.exists():continue
        if target.exists():raise RuntimeError('Occupied target: '+str(target))
        target.parent.mkdir(parents=True,exist_ok=True);source.rename(target)
    # Rewrite presentation links exactly once, using resolved original targets.
    for old,body in markdown.items():
        new=Path(mapping[str(old)])
        def replace(match):
            href=match.group(1);bare=href.strip('<>')
            if '://' in bare or bare.startswith('#'):return match.group(0)
            original=Path(bare) if Path(bare).is_absolute() else old.parent/bare
            original=original.resolve();target=Path(mapping.get(str(original),str(translate(original,moves))))
            if not target.exists():return match.group(0)
            relative=Path(os.path.relpath(target,new.parent)).as_posix()
            return ']('+relative+')'
        new.write_text(re.sub(r'\]\(([^)]+)\)',replace,body),encoding='utf-8')
    for filename in ('study_48_plan.json','batch_12_plan.json'):
        p=root/'Reports'/'Study_Configuration'/filename;data=read(p)
        for entry in data['tests']:
            old=root/entry['relative_path'];entry['relative_path']=translate(old,moves).relative_to(root).as_posix()
        save(p,data)
    for entry,new,theme,local,name in rows:
        config=read(new/'test_config.json');config['relative_path']=new.relative_to(root).as_posix();save(new/'test_config.json',config)
        entry['relative_path']=new.relative_to(root).as_posix()
    # Remove only empty directories left by the moves, within the study boundary.
    for old in themes:
        bounded(root,old)
        if old.exists() and not any(old.iterdir()):old.rmdir()
    for _,new,_,_,_ in rows:
        for mode in MODES:
            empty=new/mode/'assets'
            if empty.exists() and not any(empty.iterdir()):empty.rmdir()
    save(layout,{'version':2,'study_plan':'Reports/Study_Configuration/study_48_plan.json',
                 'raw_capture_paths_are_historical':True,'path_mapping':mapping,
                 'original_manifest':str((originals/'original_file_manifest.json').relative_to(root))})
    write_overviews(root)

def warning(config,result,qualification):
    behavior=config.get('pedestrian_behavior','cross')
    if behavior=='wait':return 'WAITING CASE: pedestrian remains at the curb intentionally.'
    if behavior=='retreat':return 'RETREAT CASE: pedestrian walks, then reverses direction.'
    if result.get('pedestrian_movement_trigger_s') is None:return 'INVALID ENCOUNTER: pedestrian did not emerge.'
    if not qualification['qualified']:return 'UNQUALIFIED ENCOUNTER: '+('; '.join(str(x) for x in qualification['reasons']))
    if not qualification['initial_occluded_scene_verified']:return 'INITIAL OCCLUSION UNVERIFIED: see the test overview.'
    return 'Recorded encounter. Contact outcomes and guard interventions are in the overview.'

def write_overviews(root):
    plan=read(root/'Reports/Study_Configuration/study_48_plan.json')
    efficiency=read(root/'Reports/Technical_Records/efficiency_report.json')
    by_id={t['catalogue']['test']:t for t in efficiency['tests']};register=[];index=[]
    theme_index={}
    for config in sorted(plan['tests'],key=lambda c:TESTS[int(c['test'].split('_')[1])][:2]):
        folder=root/config['relative_path'];n=int(config['test'].split('_')[1]);theme,local,name=TESTS[n];title=name.replace('_',' ')
        record=by_id[config['test']]
        body=[f'# {title}','',f'Theme {theme}: {THEMES[theme].replace("_"," ")}. Test {local}. Stable capture ID: {config["test"]}; seed: {config["seed"]}.','',
              PURPOSES[theme],'',
              f'Location: {config.get("location")}; target speed: {config["speed_kmh"]} km/h; daylight: {config.get("daylight","not separately labeled in historical config")}; weather: {config.get("weather_label",config.get("weather"))}.','',
              f'Pedestrian behavior: **{config.get("pedestrian_behavior","cross")}**. Assistance OFF means cooperative warnings disabled; assistance ON means warnings available to A, B and C.','',
              '[Configuration](test_config.json) · [Detailed measured evidence](report.md)','',
              '| Mode / assistance | Encounter and scene qualification | Pedestrian contact | A/B contact | B/C contact |','|---|---|---|---|---|']
        comparison_links=[]
        for mode in MODES:
            q=record['modes'][mode]['qualification']
            for state in STATES:
                run=folder/mode/f'Assistance_{state}';result=read(run/'results.json');status=warning(config,result,q)
                physical=read(run/'physical_timeline.json')
                columns=['frame','trial_time_s','simulation_timestamp_s']
                fields=('speed_mps','acceleration_mps2','brake','gap_m','closing_speed_mps','guard')
                columns += [f'{label}_{field}' for label in ('A','B','C') for field in fields]
                with (run/'timeline.csv').open('w',newline='',encoding='utf-8-sig') as f:
                    writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
                    for row in physical:
                        out={'frame':row['frame'],'trial_time_s':row['time_s'],'simulation_timestamp_s':row.get('simulation_timestamp_s')}
                        for actor,label in (('hero_car','A'),('follower_b','B'),('follower_c','C')):
                            for field in fields:out[f'{label}_{field}']=row['vehicles'].get(actor,{}).get(field)
                        writer.writerow(out)
                contacts=bool(result.get('pedestrian_contacts'));ab=result['secondary']['A_B']['contact'];bc=result['secondary']['B_C']['contact']
                body.append(f'| [{mode} — Assistance {state}]({mode}/Assistance_{state}/README.md) | {status} Protocol: {q["qualified"]}; initial occlusion: {q["initial_occluded_scene_verified"]} | {contacts} | {ab} | {bc} |')
                runbody=[f'# {title} — {mode} — Assistance {state}','',status,'',
                  'This is recorded simulation evidence. Stationary footage is not proof of successful avoidance.','',
                  '[Numeric results](results.json) · [Requested configuration](requested_config.json) · [Readable timeline](timeline.csv) · [Full physical timeline](physical_timeline.json) · [Brake and warning chronology](causal_chronology.json)','',
                  '[Six-view run video](assets/run_video.mp4) · [Event pictures](assets/event_sets) · [All recorded camera pictures](assets/pictures)','',
                  'Camera views: overhead, driver A, pedestrian, opposite, driver B and driver C. Stable capture labels preserve frame and simulation time.','']
                (run/'README.md').write_text('\n'.join(runbody),encoding='utf-8')
                register.append({'theme':theme,'test':local,'title':title,'capture_id':config['test'],'seed':config['seed'],'mode':mode,'assistance':state,
                  'terminal_reason':result['terminal_reason'],'pedestrian_behavior':config.get('pedestrian_behavior','cross'),
                  'encounter_status':status,'protocol_qualified':q['qualified'],'initial_occlusion_verified':q['initial_occluded_scene_verified'],
                  'pedestrian_contact':contacts,'A_B_contact':ab,'B_C_contact':bc,'folder':run.relative_to(root).as_posix()})
            comparison=folder/mode/'Comparison';comparison.mkdir(exist_ok=True)
            video=mode+'_Assistance_OFF_vs_ON.mp4'
            links=[f'# {title} — {mode} comparison','',
                   'Left panel: Assistance OFF. Right panel: Assistance ON. Both panels show the same trial elapsed time.','',
                   f'[Six-view comparison]({video})','',
                   'Individual camera comparisons:','']+[f'- [{label.replace("_"," ")}]({label}_Assistance_OFF_vs_ON.mp4)' for label in VIEWS.values()]
            (comparison/'README.md').write_text('\n'.join(links)+'\n',encoding='utf-8')
            comparison_links.append(f'[{mode} OFF versus ON comparison]({mode}/Comparison/README.md)')
        body+=['',' · '.join(comparison_links),'']
        body+=['Guard interventions, missing measurements and protocol failures remain in the detailed evidence. Contacts are reported over the complete observation window; their presence alone does not identify the cause.','']
        (folder/'TEST_OVERVIEW.md').write_text('\n'.join(body),encoding='utf-8')
        index.append(f'- [Theme {theme}, Test {local}: {title}]({config["relative_path"]}/TEST_OVERVIEW.md)')
        theme_index.setdefault(folder.parent,[]).append(f'- [Test {local}: {title}]({folder.name}/TEST_OVERVIEW.md)')
    for folder,links in theme_index.items():
        theme=int(folder.name.split('_')[1])
        (folder/'README.md').write_text(f'# Theme {theme}: {THEMES[theme].replace("_"," ")}\n\n{PURPOSES[theme]}\n\n'+'\n'.join(links)+'\n',encoding='utf-8')
    with (root/'Reports/Run_Register.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(register[0]));writer.writeheader();writer.writerows(register)
    start=['# Town10HD evidence — start here','',
      'The study contains 12 tests, each with Automatic and Retrofit assistance OFF/ON runs: 48 completed observation windows. Completion and encounter validity are different; see each test overview.','',
      '[Study report](Reports/Study_Report.md) · [Efficiency report](Reports/Efficiency_Report.md) · [48-run register](Reports/Run_Register.csv) · [Detailed evidence index](Reports/Evidence_Index.md)','',
      'Open a test overview below, then choose Automatic or Retrofit and Assistance OFF or ON. Each run keeps its numeric evidence and assets together. Comparison folders contain clearly labeled OFF versus ON videos.','',
      '## Tests','']+index+['','## Separate evidence','',
      '- [Sudden-braking diagnostics](Diagnostics/Sudden_Braking/secondary_collision_diagnostic.md)',
      '- [Failed and interrupted evidence](Failed/FAILURE_REGISTER.md)',
      '- [Technical records and preserved original presentation](Reports/Technical_Records)','',
      'Stable IDs and raw capture-time paths are historical provenance. The layout mapping records their current locations. No simulation results were changed or rerun during organization.','']
    (root/'START_HERE.md').write_text('\n'.join(start),encoding='utf-8')
    (root/'Reports/Study_Report.md').write_text('# Town10HD study report\n\n48 completed runs across 12 tests. Eighteen of 24 mode-specific pairs pass timing or nuisance-enactment protocol checks; fourteen also pass initial scene checks.\n\n[Measured efficiency and limitations](Efficiency_Report.md) · [Pictures, timelines and individual camera videos](Evidence_Index.md) · [Run register](Run_Register.csv) · [Browse tests](../START_HERE.md)\n\nThe Overcast Visibility crossing did not trigger pedestrian emergence. The waiting case intentionally stays at the curb. Neither stationary footage nor zero contacts alone establishes coordination success.\n',encoding='utf-8')
    failure=['# Failed and interrupted evidence','',
      'These preserved attempts and earlier deliveries are separate from the 48 completed study runs. Their original files remain intact.','']
    for p in sorted((root/'Failed').iterdir()):
        if p.is_dir():failure.append(f'- [{p.name.replace("_"," ")}]({p.name})')
    for attempt in efficiency['archived_failed_or_incomplete_attempts']['records']:
        p=Path(attempt['archive_path']);meta=attempt.get('preservation_metadata',{})
        capture=attempt.get('test');number=int(capture.split('_')[1]) if capture else None
        title=TESTS[number][2].replace('_',' ') if number in TESTS else str(capture)
        technical=attempt.get('technical_failure') or {}
        reason=technical.get('error') if isinstance(technical,dict) else str(technical)
        reason=reason or attempt.get('terminal_reason') or meta.get('reason','Reason recorded in attempt metadata')
        failure+=['',f'[{title} - {attempt.get("mode")} - Assistance {attempt.get("state")} - {p.name}]({Path(os.path.relpath(p,root/"Failed")).as_posix()}) - {reason}.']
    failure+=['','## Completed runs with invalid or incomplete encounter evidence','',
      'The Close Following Crossing lacks complete historical command/scene verification. Morning Bus Crossing fails measured synchronization. Overcast Visibility misses emergence. These runs remain in Tests with explicit qualifications.','']
    (root/'Failed/FAILURE_REGISTER.md').write_text('\n'.join(failure),encoding='utf-8')

def media(root):
    import cv2
    import numpy as np
    plan=read(root/'Reports/Study_Configuration/study_48_plan.json');eff=read(root/'Reports/Technical_Records/efficiency_report.json')
    by_id={t['catalogue']['test']:t for t in eff['tests']}
    for cfg in plan['tests']:
        folder=root/cfg['relative_path'];title=TESTS[int(cfg['test'].split('_')[1])][2].replace('_',' ')
        for mode in MODES:
            q=by_id[cfg['test']]['modes'][mode]['qualification'];sources={};outputs=[]
            for state in STATES:
                run=folder/mode/f'Assistance_{state}';manifest=read(run/'camera_manifest.json')
                sources[state]=(run,[x for x in manifest if all(v in x['views'] for v in VIEWS)],warning(cfg,read(run/'results.json'),q))
                outputs.append((run/'assets/run_video.mp4',(state,)))
            outputs.append((folder/mode/'Comparison'/(mode+'_Assistance_OFF_vs_ON.mp4'),STATES))
            active=[]
            for target,states in outputs:
                check=target.with_suffix('.verification.json')
                if target.exists() and check.exists() and read(check).get('passed'):continue
                width=960*len(states);height=980
                writer=cv2.VideoWriter(str(target),cv2.VideoWriter_fourcc(*'mp4v'),10,(width,height))
                if not writer.isOpened():raise RuntimeError(target)
                active.append((target,states,writer,[]))
            if not active:continue
            needed={state for _,states,_,_ in active for state in states}
            for i in range(201):
                t=i*.1;panels={};provenance={}
                for state in needed:
                    frame=np.zeros((980,960,3),dtype=np.uint8)
                    run,manifest,status=sources[state];m=min(manifest,key=lambda x:abs(x['time_s']-t))
                    cv2.putText(frame,title,(15,28),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
                    cv2.putText(frame,f'{mode} | Assistance {state} | Seed {cfg["seed"]}',(15,59),cv2.FONT_HERSHEY_SIMPLEX,.65,(255,255,255),2)
                    for k,(view,label) in enumerate(VIEWS.items()):
                        image=cv2.imread(str(run/'assets/pictures'/m['views'][view]))
                        if image is None:raise RuntimeError('Missing actual frame')
                        image=cv2.resize(image,(480,270));x=(k%2)*480;y=80+(k//2)*270
                        frame[y:y+270,x:x+480]=image
                        cv2.rectangle(frame,(x,y),(x+480,y+24),(0,0,0),-1)
                        cv2.putText(frame,label.replace('_',' '),(x+8,y+18),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
                    cv2.putText(frame,f'{cfg["test"]} | frame {m["frame"]} | trial {m["time_s"]:.3f}s | sim {m.get("simulation_timestamp_s",0):.3f}s',
                                (15,913),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1)
                    for line,text in enumerate(textwrap.wrap(status,105)[:2]):
                        cv2.putText(frame,text,(15,940+line*22),cv2.FONT_HERSHEY_SIMPLEX,.45,(100,210,255),1)
                    panels[state]=frame
                    provenance[state]={'frame':m['frame'],'trial_s':m['time_s'],'simulation_timestamp_s':m.get('simulation_timestamp_s'),'pictures':m['views']}
                for target,states,writer,selected in active:
                    writer.write(np.concatenate([panels[s] for s in states],axis=1))
                    selected.append({'video_t_s':t,'sources':{s:provenance[s] for s in states}})
            for target,states,writer,selected in active:
                writer.release();capture=cv2.VideoCapture(str(target));count=0;geometry=True
                while True:
                    ok,image=capture.read()
                    if not ok:break
                    count+=1;geometry=geometry and image.shape[:2]==(980,960*len(states))
                capture.release();passed=count==201 and geometry
                save(target.with_suffix('.verification.json'),{'passed':passed,'decoded_frames':count,'size':[960*len(states),980],'fps':10,'source':'actual recorded RGB frames',
                            'alignment':'nearest recorded trial time, 0.1-second output interval','qualification_banner':{s:sources[s][2] for s in states}})
                save(target.with_suffix('.source_frames.json'),selected)
                if not passed:raise RuntimeError('Decode failed: '+str(target))
                print('VERIFIED '+str(target.relative_to(root)),flush=True)

def verify(root,require_media=True):
    layout=read(root/'Reports/Technical_Records/evidence_layout.json');originals=read(root/layout['original_manifest']);failures=[];checked=0
    mutable={'test_config.json','study_48_plan.json','batch_12_plan.json'}
    for item in originals:
        p=Path(item['current_path'])
        if not p.exists():failures.append({'missing':str(p)});continue
        if p.suffix=='.md' or p.name in mutable:
            backup=root/'Reports/Technical_Records/Presentation_Before_Reorganization'/Path(item['original_path']).relative_to(root)
            if not backup.exists() or digest(backup)!=item['sha256']:failures.append({'changed_original_presentation':str(backup)})
            continue
        if p.stat().st_size!=item['bytes']:failures.append({'changed_bytes':str(p)})
        if item.get('sha256') and digest(p)!=item['sha256']:failures.append({'changed_measurement':str(p)})
        checked+=1
    broken=[];links=0
    for f in root.rglob('*.md'):
        if 'Failed' in f.relative_to(root).parts or 'Presentation_Before_Reorganization' in f.parts:continue
        for href in re.findall(r'\]\(([^)]+)\)',f.read_text(encoding='utf-8')):
            href=href.strip('<>')
            if '://' in href or href.startswith('#'):continue
            p=Path(href) if Path(href).is_absolute() else f.parent/href;links+=1
            if not p.exists():broken.append({'file':str(f),'target':href})
    video_checks=[]
    if require_media:
        plan=read(root/'Reports/Study_Configuration/study_48_plan.json')
        for entry in plan['tests']:
            folder=root/entry['relative_path']
            for mode in MODES:
                targets=[folder/mode/f'Assistance_{state}'/'assets/run_video.mp4' for state in STATES]
                targets.append(folder/mode/'Comparison'/(mode+'_Assistance_OFF_vs_ON.mp4'))
                for target in targets:
                    check=target.with_suffix('.verification.json')
                    valid=target.exists() and check.exists() and read(check).get('passed') and read(check).get('decoded_frames')==201
                    video_checks.append({'video':str(target.relative_to(root)),'passed':bool(valid)})
    passed=not failures and not broken and all(v['passed'] for v in video_checks)
    save(root/'Reports/Technical_Records/reorganization_verification.json',
         {'passed':passed,'unchanged_original_evidence_files_checked':checked,'preservation_failures':failures,'local_links_checked':links,'broken_links':broken,'new_six_view_video_checks':video_checks})
    print(json.dumps({'passed':passed,'preservation_failures':failures,'broken_links':broken},indent=2))
    if not passed:raise RuntimeError('Evidence layout verification failed')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--media',action='store_true');parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.media:media(STUDY_ROOT)
    elif args.verify:verify(STUDY_ROOT)
    else:reorganize(STUDY_ROOT,args.apply)
