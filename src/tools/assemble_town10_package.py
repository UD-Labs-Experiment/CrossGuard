"""Verify saved real RGB sets and assemble numeric/picture evidence before video."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import hashlib
from PIL import Image,ImageDraw
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from crossguard.paths import STUDY_ROOT
from crossguard.simulation.study_assessment import assess_run,check_pair

VIEWS=['top','driver','person','opposite','driver_b','driver_c']
NAMES={'hero_car':'A','follower_b':'B','follower_c':'C'}

def load(path):return json.loads(Path(path).read_text())

def package_root():
    return Path(os.environ.get('CROSSGUARD_PACKAGE_ROOT',str(STUDY_ROOT/'Theme_03'/'Test_001')))

def assets(output):
    output=Path(output)
    if (output/'assets').is_dir():return output/'assets'
    relocated=output.parent/'assets'/output.name
    if relocated.is_dir(): return relocated
    return Path(load(output/'results.json')['requested_config'].get('assets_output',str(output)))

def event_sets(output):
    assess_run(output)
    result=load(output/'results.json')
    rows=load(output/'physical_timeline.json')
    media=assets(output)
    manifest=load(output/'camera_manifest.json')
    full=[m for m in manifest if set(m['views'])==set(VIEWS) and not m['missing']]
    if not full:raise RuntimeError('No synchronized six-view sets: '+str(output))
    emergence=result['pedestrian_movement_trigger_s']
    a=result['diagnostic_A_brake_s'] or result['timeline']['hero_car']['first_brake_command_s']
    contact_frames=[e['frame'] for e in result['pedestrian_contacts']]
    contact_frames += [e['frame'] for pair in result['secondary'].values() for e in pair['events']]
    impact=next((r['time_s'] for r in rows if contact_frames and r['frame']==min(contact_frames)),None)
    markers={'before_emergence':max(0,(emergence or a or 1.)-.2),
             'during_braking':a+.2 if a is not None else (emergence or 1.)+.5,
             'outcome':impact if impact is not None else rows[-1]['time_s'],
             'terminal':rows[-1]['time_s']}
    if (output/'encounter_assessment.json').exists():
        encounter=load(output/'encounter_assessment.json')
        t=encounter.get('undisturbed_pedestrian_arrival_s')
        if t is not None:
            markers['intended_encounter']=t
            if impact is None: markers['outcome']=t
    requested=load(output/'requested_config.json') if (output/'requested_config.json').exists() else {}
    behavior=requested.get('pedestrian_behavior','cross')
    if behavior in ('wait','retreat'):
        timing=result.get('encounter_timing') or {}
        prediction=timing.get('trigger') or (timing.get('failure') or {}).get('prediction') or {}
        point=prediction.get('conflict_center_xy_m')
        passage=None
        if point:
            yaw=math.radians(rows[0]['vehicles']['hero_car']['yaw_deg']);axis=(math.cos(yaw),math.sin(yaw))
            for previous,current in zip(rows,rows[1:]):
                a_distance=sum((point[i]-previous['vehicles']['hero_car']['location'][i])*axis[i] for i in range(2))
                b_distance=sum((point[i]-current['vehicles']['hero_car']['location'][i])*axis[i] for i in range(2))
                if a_distance>0 and b_distance<=0:
                    passage=previous['time_s']+(current['time_s']-previous['time_s'])*a_distance/(a_distance-b_distance);break
        moving=[r for r in rows if r.get('pedestrian_submitted_control',{}).get('speed_mps',0)>.1]
        reverse=None
        if moving:
            direction=moving[0]['pedestrian_submitted_control']['direction']
            reverse=next((r['time_s'] for r in moving if sum(a*b for a,b in zip(direction,r['pedestrian_submitted_control']['direction']))<0),None)
        equivalent={'behavior':behavior,'waiting_reference_s':rows[0]['time_s'],
          'actual_A_conflict_plane_passage_s':passage,'walking_command_start_s':moving[0]['time_s'] if moving else None,
          'retreat_command_start_s':reverse,'source':'recorded physical trajectory and submitted commands; evaluator only'}
        (output/'equivalent_event_markers.json').write_text(json.dumps(equivalent,indent=2))
        markers['waiting_reference']=rows[0]['time_s']
        if passage is not None:markers['actual_approach_opportunity']=passage
        if reverse is not None:markers['retreat_command']=reverse
    for pair,record in result['secondary'].items():
        if record['events']:
            earliest=min(e['frame'] for e in record['events'])
            markers[pair+'_contact']=next(r['time_s'] for r in rows if r['frame']==earliest)
    entries={}
    for marker,t in markers.items():
        m=min(full,key=lambda m:abs(m['time_s']-t))
        canvas=Image.new('RGB',(1440,1245),(15,18,24))
        d=ImageDraw.Draw(canvas)
        labels=result.get('requested_config',{}).get('labels',{})
        title=' | '.join(str(labels.get(key,'unlabelled')) for key in ('test','mode','assistance','seed'))
        d.text((10,5),title+f' | {marker} | frame {m["frame"]} | trial {m["time_s"]:.3f}s',fill='white')
        for i,name in enumerate(VIEWS):
            img=Image.open(media/'pictures'/m['views'][name]);img.load()
            if img.size != (960,540):raise RuntimeError('Unexpected image size')
            canvas.paste(img.resize((720,405)),((i%2)*720,30+(i//2)*405))
        target=media/'event_sets';target.mkdir(exist_ok=True)
        canvas.save(target/(marker+'.jpg'),quality=94)
        for name in VIEWS:
            shutil.copyfile(media/'pictures'/m['views'][name],target/(marker+'_'+name+'.jpg'))
        entries[marker]=m
    (media/'event_sets'/'markers.json').write_text(json.dumps(entries,indent=2))
    fig,ax=plt.subplots(4,1,figsize=(12,11),sharex=True)
    times=[r['time_s'] for r in rows]
    for name,label in NAMES.items():
        ax[0].plot(times,[r['vehicles'][name]['speed_mps']*3.6 for r in rows],label=label)
        ax[1].plot(times,[r['vehicles'][name]['acceleration_mps2'] for r in rows],label=label)
        ax[2].plot(times,[r['vehicles'][name]['brake'] for r in rows],label=label)
    for name in ('follower_b','follower_c'):
        ax[3].plot(times,[r['vehicles'][name]['gap_m'] for r in rows],label='A/B' if name=='follower_b' else 'B/C')
    for panel,title in zip(ax,['Measured speed (km/h)','Measured longitudinal acceleration (m/s²)','Brake command (0–1)','Physical projected bumper gap (m)']):
        panel.set_ylabel(title);panel.grid(alpha=.3);panel.legend(ncol=3)
        if a is not None:panel.axvline(a,color='black',linestyle=':',label='A braking')
    ax[3].axhline(0,color='red',linewidth=.8);ax[-1].set_xlabel('Simulation time since launch (s)')
    fig.suptitle(str(output.relative_to(STUDY_ROOT)))
    fig.tight_layout();fig.savefig(media/'braking_timeline.png',dpi=150);plt.close(fig)
    # Causal chronology distinguishes normal speed regulation from hazard response.
    chronology=[]
    for name in NAMES:
        brake=next((r for r in rows if (a is None or r['time_s']>=a) and r['vehicles'][name]['brake']>.05),None)
        chronology.append({'vehicle':NAMES[name],'post_A_brake_s':brake['time_s'] if brake else None,
            'warning_receipt_s':result['timeline'][name]['first_warning_receipt_s']})
    contacts=result['pedestrian_contacts']+[e for p in result['secondary'].values() for e in p['events']]
    for pair,p in result['secondary'].items():
        for e in p['events']:
            pre=e['precontact']
            before=next((r for r in reversed(rows) if r['frame']<e['frame']),None)
            chronology.append({'contact_pair':pair,'frame':e['frame'],
                'precontact':pre,'signed_closing_speed_mps':before['vehicles']['follower_b' if pair=='A_B' else 'follower_c']['closing_speed_mps'] if before else None})
    (output/'causal_chronology.json').write_text(json.dumps(chronology,indent=2))
    return result

def pictures():
    if load(package_root()/'test_config.json').get('encounter_timing'):
        from synchronized_package import assemble
        return assemble()
    results={}
    for folder in sorted((STUDY_ROOT/'diagnostics').glob('diagnostic*')):
        if (folder/'results.json').exists(): results[str(folder.relative_to(STUDY_ROOT))]=event_sets(folder)
    root=package_root()
    for mode in ('Automatic','Retrofit'):
        for assistance in ('OFF','ON'):
            folder=root/mode/assistance
            if (folder/'results.json').exists():results[mode+' '+assistance]=event_sets(folder)
    compact={key:{'pedestrian_contact_vehicles':sorted(set(e['vehicle_id'] for e in r['pedestrian_contacts'])),
        'raw_pedestrian_contact_callbacks':len(r['pedestrian_contacts']),
        'A_B_contact':r['secondary']['A_B']['contact'],'B_C_contact':r['secondary']['B_C']['contact'],
        'min_A_B_gap_m':r['secondary']['A_B']['minimum_physical_bumper_gap_m'],
        'min_B_C_gap_m':r['secondary']['B_C']['minimum_physical_bumper_gap_m'],
        'brake_times_s':{NAMES[k]:v['first_brake_command_s'] for k,v in r['timeline'].items()},
        'terminal_reason':r['terminal_reason'],'guards':{NAMES[k]:v['guard_first_s'] for k,v in r['timeline'].items()}}
        for key,r in results.items()}
    (STUDY_ROOT/'first_package_comparison.json').write_text(json.dumps(compact,indent=2))
    checks={mode:check_pair(root/mode/'OFF',root/mode/'ON') for mode in ('Automatic','Retrofit')
            if all((root/mode/state/'results.json').exists() for state in ('OFF','ON'))}
    (STUDY_ROOT/'matched_pair_checks.json').write_text(json.dumps(checks,indent=2))
    print(json.dumps(compact,indent=2))

def videos():
    import cv2
    root=package_root()
    for mode in ('Automatic','Retrofit'):
        manifests={state:load(root/mode/state/'camera_manifest.json') for state in ('OFF','ON')}
        for view in VIEWS:
            target=root/mode/'assets'/(view+'_OFF_ON.mp4') if (root/mode/'assets').is_dir() else root/(mode+'_'+view+'_OFF_ON.mp4')
            writer=cv2.VideoWriter(str(target),cv2.VideoWriter_fourcc(*'mp4v'),10,(1920,540))
            if not writer.isOpened():raise RuntimeError('Video writer unavailable')
            count=0
            for tindex in range(201):
                t=tindex*.1
                frames=[]
                for state in ('OFF','ON'):
                    available=[m for m in manifests[state] if view in m['views']]
                    m=min(available,key=lambda m:abs(m['time_s']-t))
                    img=cv2.imread(str(assets(root/mode/state)/'pictures'/m['views'][view]))
                    if img is None:raise RuntimeError('Missing video source frame')
                    frames.append(img)
                import numpy as np
                writer.write(np.concatenate(frames,axis=1));count+=1
            writer.release()
            reader=cv2.VideoCapture(str(target));decoded=0
            while True:
                ok,img=reader.read()
                if not ok:break
                if img.shape[:2]!=(540,1920):raise RuntimeError('Invalid decoded video geometry')
                decoded+=1
            reader.release()
            if decoded!=count:raise RuntimeError('Video decode count mismatch')
            (target.with_suffix('.verification.json')).write_text(json.dumps({'decoded_frames':decoded,'fps':10,
                'size':[1920,540],'source':'actual labeled synchronized RGB cameras','alignment':'nearest 0.1s trial simulation time',
                'repeated_nearest_frames':True,'audio':False},indent=2))
            print('VERIFIED '+str(target))

def report():
    if load(package_root()/'test_config.json').get('encounter_timing'):
        from synchronized_package import assemble
        return assemble()
    root=package_root()
    cfg=load(root/'test_config.json')
    checks=load(STUDY_ROOT/'matched_pair_checks.json')
    detail={}
    crossing=load(root/'Automatic'/'OFF'/'crosswalk.json')['center']
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            output=root/mode/state
            result=load(output/'results.json');rows=load(output/'physical_timeline.json')
            yaw=math.radians(rows[0]['vehicles']['hero_car']['yaw_deg'])
            axis=(math.cos(yaw),math.sin(yaw));plane=crossing[0]*axis[0]+crossing[1]*axis[1]
            at_a=min(rows,key=lambda r:abs(r['time_s']-result['timeline']['hero_car']['first_brake_command_s']))
            detail[mode+' '+state]={'crossing_center_passage_s':{n:next((r['time_s'] for r in rows
                if r['vehicles'][n]['location'][0]*axis[0]+r['vehicles'][n]['location'][1]*axis[1]>=plane),None) for n in NAMES},
                'at_A_first_brake':{n:{q:v[q] for q in ('speed_mps','gap_m','closing_speed_mps','brake','guard')}
                                    for n,v in at_a['vehicles'].items()}}
    (root/'physical_comparison_detail.json').write_text(json.dumps(detail,indent=2))
    source_root=STUDY_ROOT.parent.parent
    shutil.copyfile(source_root/'src'/'reports'/'SECONDARY_COLLISION_DIAGNOSTIC.md',
                    STUDY_ROOT/'diagnostics'/'secondary_collision_diagnostic.md')
    provenance=STUDY_ROOT/'provenance';provenance.mkdir(exist_ok=True)
    shutil.copyfile(source_root/'src'/'models'/'MODEL_PROVENANCE.json',provenance/'MODEL_PROVENANCE.json')
    model=source_root/'src'/'models'/'yolo11n.onnx'
    (provenance/'model_hash_at_export.json').write_text(json.dumps({'sha256':hashlib.sha256(model.read_bytes()).hexdigest(),
        'bytes':model.stat().st_size,'path':str(model),'measurement':'export-time check; model file was not changed during runs'},indent=2))
    lines=['# First Town10HD package: diagnostic and close-following crosswalk',
        '', 'Completed: three separate diagnostics and four Test_001 runs. No larger effectiveness batch has started.',
        '', '## Recorded secondary-collision cause', '',
        'The old nominal-speed launch produced a measured −27.20 m/s² startup transient without an A brake command. Ideal B/C reacted in that same frame. The corrected immediate reference also developed lower follower speeds and wider actual gaps before A braked. Both mechanisms can conceal a close-following encounter; absence of contact is not proof of coordination.',
        '', 'Normal acceleration replaced injected chassis velocity. The delayed diagnostic then recorded A/B contact at **5.617 s**, closing **8.03 m/s**, and B/C contact at **7.483 s**, closing **8.13 m/s**. Reciprocal sensors recorded both impacts, with strictly preceding-frame velocities and nonzero impulses. No guard intervened. Diagnostics are excluded from the four-run comparison.',
        '', '[Diagnostic explanation](diagnostics/secondary_collision_diagnostic.md)',
        '', '## Frozen Test_001 setup', '',
        f'Seed **{cfg["seed"]}**; Town10HD native road-10 approach crosswalk; stopped Fuso Rosa bus; clear afternoon solar preset; target {cfg["speed_kmh"]:.3f} km/h; pedestrian {cfg["pedestrian_speed_mps"]:.3f} m/s; movement trigger {cfg["trigger_m"]:.3f} m before crossing. Every run starts from rest and lasts 20 simulation seconds. Actual encounter speeds and gaps differ from requested launch parameters and are recorded.',
        '', '| Mode / follower | Spawn gap m | Update s | Sensing s | Reaction s | Brake cap | Stopping-model deceleration m/s² |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for mode,key in [('Automatic','followers_automatic'),('Retrofit','followers_retrofit')]:
        for name,v in cfg[key].items():
            lines.append(f'| {mode} {NAMES[name]} | {v["gap_m"]:.3f} | {v["update_s"]:.3f} | {v["sensing_s"]:.3f} | {v["reaction_s"]:.3f} | {v["brake_limit"]:.3f} | {v["deceleration_mps2"]:.3f} |')
    lines += ['',f'Retrofit warning/phone response is {cfg["driver_response_s"]:.3f} s for A/B/C. Local predecessor following uses the B/C reaction values above. The radio path uses 150 ms, zero configured jitter/loss and 150 m range; cellular uses 300 ±100 ms, 3% loss and 10 km range; optical uses 10 ±3 ms, 1% loss and 120 m range plus geometry. These are modeled communication assumptions. The first test uses hybrid transport without injected outages.',
        '', '## Numeric comparison', '',
        '| Run | Pedestrian-contact vehicles | A/B contact | B/C contact | Minimum A/B gap m | Minimum B/C gap m |',
        '|---|---|---|---|---:|---:|']
    verification={'matched_pair_checks':checks,'runs':{},'videos':{}}
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            output=root/mode/state
            r=load(output/'results.json');a=load(output/'assessment.json')
            contacts=', '.join(NAMES[n] for n in a['pedestrian_contact_vehicles']) or 'none'
            lines.append(f'| {mode} {state} | {contacts} | {r["secondary"]["A_B"]["contact"]} | {r["secondary"]["B_C"]["contact"]} | {r["secondary"]["A_B"]["minimum_physical_bumper_gap_m"]:.3f} | {r["secondary"]["B_C"]["minimum_physical_bumper_gap_m"]:.3f} |')
            manifest=load(output/'camera_manifest.json')
            physical=load(output/'physical_timeline.json')
            frames={r['frame'] for r in physical}
            images=0
            for entry in manifest:
                if set(entry['views'])!=set(VIEWS) or entry['missing'] or entry['frame'] not in frames:
                    raise RuntimeError('Incomplete or unmatched camera set')
                for filename in entry['views'].values():
                    with Image.open(media/'pictures'/filename) as img:
                        img.load()
                        if img.size!=(960,540):raise RuntimeError('Incorrect evidence geometry')
                    images+=1
            verification['runs'][mode+' '+state]={'physics_samples':len(physical),
                'complete_six_view_sets':len(manifest),'decoded_picture_references':images,
                'terminal_vehicle_speeds_mps':{n:v['speed_mps'] for n,v in physical[-1]['vehicles'].items()},
                'terminal_reason':r['terminal_reason'],'technical_failure':r['technical_failure']}
            # Preserve the runtime source snapshot already exported by the owning bridge.
            session=Path(r['source_trial_path']).parent
            if (session/'runtime_source').is_dir():
                shutil.copytree(session/'runtime_source',output/'provenance'/'runtime_source',dirs_exist_ok=True)
    lines += ['', 'Contact columns are binary per physical actor pair. Sustained and reciprocal sensor callbacks are retained as raw events, not counted as separate accidents. Negative minima are small projected body-bound overlaps corroborated by contact sensors.',
        '', 'Both Automatic runs avoided A/B and B/C contact, but the measured minimum A/B clearance was **0.612 m OFF versus 3.851 m ON**; B/C was **6.087 m OFF versus 5.247 m ON**. Actual speeds at A braking were about 41.54 km/h for A/B in both runs, with A/B gaps about 14.53 m. The ON bus observation triggered A/B braking together at 5.000 s; OFF A first braked for its route guard at 8.700 s. These are materially different stopping episodes. A did not contact the pedestrian in any run; that alone would conceal the B/C pedestrian contacts.',
        '', 'The Retrofit cases have identical recorded outcomes. Delivered warnings expired before the 2.391 s modeled phone response: the current phone model uses a 2 s observation-age ceiling, and brake-intent packets expire after 1.2 s. Terminal phone logs explicitly record `expired_before_reaction`. This is a concrete implementation limitation, not evidence that cooperation cannot help a real driver.',
        '', '## Braking and contact chronology', '',
        '| Run | A first brake / cause | B first physical deceleration after A | C first physical deceleration after A | A/B first impact | B/C first impact |',
        '|---|---|---:|---:|---:|---:|']
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            output=root/mode/state;r=load(output/'results.json');a=load(output/'assessment.json')
            t=r['timeline']['hero_car']['first_brake_command_s']
            cause='bus RGB warning' if mode=='Automatic' and state=='ON' else 'route guard'
            post={n:next((x['time_s'] for x in load(output/'physical_timeline.json') if x['time_s']>=t and x['vehicles'][n]['acceleration_mps2']<=-.5),None) for n in ('follower_b','follower_c')}
            impacts={}
            for pair,p in r['secondary'].items():
                impacts[pair]=min((e['precontact']['time_s']+1/60 for e in p['events']),default=None)
            fmt=lambda value:'—' if value is None else f'{value:.3f}'
            lines.append(f'| {mode} {state} | {t:.3f} / {cause} | {fmt(post["follower_b"])} | {fmt(post["follower_c"])} | {fmt(impacts["A_B"])} | {fmt(impacts["B_C"])} |')
    lines += ['', 'First physical deceleration is a temporal marker, not proof of causal response; C can already be regulating speed. Full brake-command segments and reason/source transitions are in each `assessment.json` and `physical_timeline.json`. The first B/C brake commands near initialization are speed regulation, not pedestrian response.',
        '', 'Automatic ON: target-matched bus capture **4.967 s** → optical receipt **5.000 s** → A/B brake commands **5.000 s** → measured A/B deceleration **5.033 s**. The first any packet at 0.050 s was not this usable warning. C’s earliest cooperative braking concerned B’s startup braking; its later full warning/control trace is preserved. In Retrofit ON, packets/notifications arrived, but no cooperative brake response matured; A stopped under the route guard at 8.700 s, B decelerated under its guard at 11.700 s and hit A at 12.167 s, then C decelerated at 14.550 s and hit B at 14.900 s. These vehicle contacts follow a **route-guard stop**, not an A pedestrian emergency, and must not be attributed to pedestrian-induced secondary collision prevention.',
        '', '## Delay and warning lead measures', '',
        '| Mode | Added time for A to pass crossing centre, ON−OFF s | B s | C s |',
        '|---|---:|---:|---:|']
    for mode in ('Automatic','Retrofit'):
        delays=[detail[mode+' ON']['crossing_center_passage_s'][n]-detail[mode+' OFF']['crossing_center_passage_s'][n] for n in NAMES]
        lines.append('| '+mode+' | '+' | '.join(f'{d:.3f}' for d in delays)+' |')
    lines += ['', 'This is measured passage time at a common physical crossing plane, not a full-trip delay estimate. Automatic slowing was associated with avoiding recorded pedestrian contacts; it is not automatically unnecessary braking. Usable A/B receipt at 5.000 s precedes the matched OFF B pedestrian contact at 8.400 s by 3.400 s. That is a **matched OFF reference lead**, not a lead to an ON contact (none occurred). Actual contact-referenced lead is null where no contact occurred. Nuisance quantification remains for T06.',
        '', '## Actual six-view pictures', '',
        'Each event set includes overhead, driver A, pedestrian, opposite, driver B and driver C from one CARLA frame. Every original picture has test/mode/OFF-ON/seed/frame/absolute simulation timestamp. Outcome sets select the first contact when one occurred; terminal sets separately show the end of the 20 s window. Additional A/B and B/C impact sets are retained.']
    for mode in ('Automatic','Retrofit'):
        for state in ('OFF','ON'):
            lines += ['',f'### {mode} {state}', '',
                f'[Before emergence](Theme_03/Test_001/{mode}/{state}/event_sets/before_emergence.jpg) · [Braking](Theme_03/Test_001/{mode}/{state}/event_sets/during_braking.jpg) · [Outcome](Theme_03/Test_001/{mode}/{state}/event_sets/outcome.jpg) · [Terminal](Theme_03/Test_001/{mode}/{state}/event_sets/terminal.jpg)',
                '',f'![Six actual cameras: {mode} {state}](Theme_03/Test_001/{mode}/{state}/event_sets/outcome.jpg)',
                '',f'[Measured braking timeline](Theme_03/Test_001/{mode}/{state}/braking_timeline.png) · [Configuration and results](Theme_03/Test_001/{mode}/{state}/results.json) · [Evaluator assessment](Theme_03/Test_001/{mode}/{state}/assessment.json)']
    lines += ['', '## Verified comparison videos', '', 'Pictures were shown before video encoding. OFF is left, ON is right. Each video aligns nearest captured images on the same 0.1 s trial-time grid; actual source frame/time labels remain visible. No synthetic interpolation or scripted outcomes. Every video was reopened and all 201 frames decoded at 1920×540, 10 FPS.']
    for mode in ('Automatic','Retrofit'):
        lines += ['',mode+': '+ ' · '.join(f'[{v}](Theme_03/Test_001/{mode}_{v}_OFF_ON.mp4)' for v in VIEWS)]
        for view in VIEWS:
            verification['videos'][mode+' '+view]=load(root/(mode+'_'+view+'_OFF_ON.verification.json'))
    lines += ['', '## Verification and limits before expansion', '',
        'Both OFF/ON configuration checks pass after excluding assistance and artifact labels. Initial A/B/C positions, speed and yaw match exactly in the recorded first frame. Native marking, bus geometry and pedestrian access are visible in the pictures; the read-only ray audit intersects the bus. Route samples remain on the native road through the used stopping corridor; the downstream road starts curving near the reserve endpoint, reinforcing the reason for retaining guards. All four runs contain 1200 physical samples and finish the declared observation window without a technical exception.',
        '', '152 Python tests and 11 offline bridge self-tests pass. Source/runtime snapshots, raw contact events, complete physical timelines, communication messages, requested/effective configurations and camera manifests are retained. The archived cleanup was not repeated; `simulation/` was not edited. Local source commits only; no push.',
        '', 'This remains one bounded crosswalk test, with modeled local ranging and an uncalibrated response queue. B/C local pedestrian RGB response is not implemented; asynchronous RGB completion is recorded but not bitwise deterministic. Full phone event history was not exported (the terminal record contains its last 20 events); this missing measurement is explicit in the assessments. Neither a guard stop nor zero contacts in Automatic ON establishes universal effectiveness. Diagnostics also differ in timing and brake caps, so they are not an isolated causal estimate of delay.',
        '', 'The larger 12-test/48-run study remains **planned and unlaunched**, with only this first site physically inspected. Additional locations, daylight/weather cells, per-channel failures/all-channel failure, missing observations, extra physical occlusion, and nuisance pedestrians have not yet been run. Before expansion, resolve phone hazard memory/lifetimes, declare B/C local pedestrian behavior, and tighten reproducible RGB scheduling. Preserve this package as the original inspectable evidence.',
        '', '[Frozen study plan](study_plan.md) · [Protocol addendum](protocol_addendum.md) · [Catalogue](test_catalogue.csv) · [Matched checks](matched_pair_checks.json) · [Verification](package_verification.json)']
    (STUDY_ROOT/'first_package_report.md').write_text('\n'.join(lines),encoding='utf-8')
    (STUDY_ROOT/'package_verification.json').write_text(json.dumps(verification,indent=2))
    print('REPORT '+str(STUDY_ROOT/'first_package_report.md'))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['pictures','videos','report']);a=p.parse_args()
    {'pictures':pictures,'videos':videos,'report':report}[a.action]()
