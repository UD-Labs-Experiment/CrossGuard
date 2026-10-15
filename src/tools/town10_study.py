"""Fresh study driver: all CARLA mutations go through the sole HTTP bridge."""
import argparse
import csv
import json
import os
import random
import time
from pathlib import Path
import requests
from crossguard.paths import STUDY_ROOT

BASE=os.environ.get('CROSSGUARD_STUDY_URL','http://127.0.0.1:8001')
THEMES=['Crosswalk location and bus/truck occlusion','Daylight, weather and visibility',
        'Encounter difficulty and secondary collisions','Communication failures and redundancy',
        'Missing observations and additional physical occlusion','Waiting/retreating pedestrians and nuisance responses']

PLAN='''# Study 01: Town10HD robustness study

Frozen before physical outcome runs, 9 October 2026. Archived work is not resumed.
Only Town10HD native marked crossings qualify. RGB YOLO11n ONNX observations
feed pedestrian policy; simulator positions are setup/evaluator data only.
Local follower ranging is explicitly modeled, not claimed to be RGB-derived.

## First delivery gate
Run separate sudden-braking diagnostics, then Test_001 Automatic OFF/ON and
Retrofit OFF/ON. Present numeric comparison, braking chronology and labeled
actual six-view pictures before any larger batch. Videos follow picture inspection.
Remaining 11 tests are planned, not results. No selection based on collision percentages.

## Assumptions frozen before results
Speeds 30–50 km/h; physical gaps 6–22 m (close-following stress, not safe-driving
advice). Sample uniform ranges with Python Random(seed), recorded before each run.
Automatic sensing updates 0.05–0.20 s, sensing latency 0.05–0.20 s, reaction
0.2–0.8 s. These are degraded modeled control assumptions, not calibrated ACC.
Retrofit sensing updates 0.10–0.20 s, sensing latency 0.05–0.20 s, modeled driver
reaction 1.1–2.5 s. B and C draw independently; both retain local emergency logic
through the same delayed pathway. Brake-command caps 0.6–1.0, stopping-model
deceleration 4–7 m/s²; achieved braking is measured in CARLA rather than equated
to that analytical assumption. Immediate/ideal ranging remains a labeled reference.
Pedestrian speed 1.0–1.8 m/s, emergence trigger 20–60 m; wait and retreat are nuisance
cases with equivalent motion/response markers. Occluders: Fuso Rosa bus or available
truck, confirmed blueprint and physical geometry before a run. No vehicle relocation,
collision-physics changes or scripted contacts. Initialization declares a rolling
velocity for all vehicles, then uses normal physics and records actual launch speed.

FHWA documents mean 1.1 s reaction with 5.4 m/s² braking and design 2.5 s reaction:
https://highways.dot.gov/safety/speed-management/speed-concepts-informational-guide/chapter-4-engineering-and-technical
NHTSA car-following experiments used headways roughly 1.0–2.9 s at 50/70 km/h:
https://www.nhtsa.gov/sites/nhtsa.gov/files/carfollowingrpt.pdf
Our narrower gaps deliberately cover physically plausible tailgating stress. These
sources contextualize assumptions, not validate the particular model distribution.

## Six themes and selected combined failures
T01: two distinct physically qualified native crossings, bus versus truck.
T02: daylight/weather single factors then rainy afternoon. Morning/noon/afternoon;
clear/overcast/rain, no night. Native route, curb access and occlusion require pictures
and evaluator geometry checks before results count.
T03: close-following test then higher speed with independent follower delays/gaps.
T04: radio-only outage, cellular-only outage, optical-only outage and all three
together within two tests, each frozen time window; usable alternate-channel receipt
must precede a recipient-specific stopping deadline. An optical path blocked by
geometry is a failure even when enabled. Actual blocked sends and receipt recorded.
T05: brief missing RGB observations then missing observations plus physical occlusion.
T06: waiting pedestrian then retreating pedestrian; quantify braking and distance/delay.
Individual factors precede selected combinations. Every test has four runs, yielding
12 tests/48 effectiveness runs only after the first package is shown.

## Matched conditions and endpoints
OFF/ON share seed, vehicle blueprints, physical poses, weather, follower model,
local guard settings and sampled assumptions. Only cooperative warning availability
changes; roadside hardware and cameras exist in both. ON recipients are A/B/C.
Twenty-second window retains contacts after A stops; every corridor intervention,
technical failure and missing measurement remains evidence. Physics 60 Hz; picture
sets 10 Hz, six cameras same CARLA frame. Mark before emergence, braking and outcome.
Contact sensors on A/B/C. A/B and B/C scored separately with exact preceding snapshot
speeds and relative velocity. Whole-frame projected bounding-box gaps are evaluator
measures, with orientation recorded. A/B/C brake/speed/acceleration and actual
warning/pathway events are retained. Neither zero contacts nor a guard stop establishes
coordination success. No expectation that all OFF collide or all ON avoid.
'''

def write_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2))

def prepare():
    STUDY_ROOT.mkdir(parents=True,exist_ok=True)
    if (STUDY_ROOT/'study_plan.md').exists():
        raise RuntimeError('Fresh study already prepared; will not overwrite frozen plan')
    (STUDY_ROOT/'study_plan.md').write_text(PLAN,encoding='utf-8')
    with (STUDY_ROOT/'test_catalogue.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['theme','test','description','seed','status'])
        for t,theme in enumerate(THEMES,1):
            for j in range(2):
                n={1:3,2:5,3:1,4:7,5:9,6:11}[t]+j
                w.writerow([f'T{t:02}',f'Test_{n:03}',theme,1000+n,'planned; physical qualification pending'])
    # First test belongs to encounter difficulty. Keep its ID stable in evidence.
    rng=random.Random(1001)
    speed=round(rng.uniform(35,45),3)
    followers={}
    for name in ('follower_b','follower_c'):
        followers[name]={'gap_m':round(rng.uniform(6,10),3),
            'update_s':round(rng.uniform(.05,.2),3),'sensing_s':round(rng.uniform(.05,.2),3),
            'reaction_s':round(rng.uniform(.2,.8),3),'brake_limit':round(rng.uniform(.6,1.),3),
            'deceleration_mps2':round(rng.uniform(4,7),3)}
    config={'test':'Test_001','theme':'T03','seed':1001,'speed_kmh':speed,
        'trigger_m':round(rng.uniform(25,45),3),'followers_automatic':followers,
        'followers_retrofit':{k:{**v,'reaction_s':round(rng.uniform(1.1,2.5),3)} for k,v in followers.items()},
        'driver_response_s':round(rng.uniform(1.1,2.5),3),'pedestrian_speed_mps':round(rng.uniform(1,1.8),3),
        'weather':{'cloudiness':20.,'sun_altitude_angle':45.,'sun_azimuth_angle':220.},
        'occluder':'vehicle.mitsubishi.fusorosa','location':'Town10HD road10 native crossing',
        'sampling':'Python random.Random(1001), frozen before runs','observation_window_s':20.}
    write_json(STUDY_ROOT/'Theme_03'/'Test_001'/'test_config.json',config)

def command(action,params=None):
    response=requests.post(BASE+'/api/command',json={'action':action,'params':params or {}},timeout=300)
    response.raise_for_status()
    data=response.json()
    if data.get('status')=='error': raise RuntimeError(data)
    return data

def run(kind,mode='Automatic',assistance='OFF'):
    if kind.startswith('diagnostic'):
        delayed=kind in ('diagnostic_delayed','diagnostic_insufficient')
        output=STUDY_ROOT/'diagnostics'/kind
        follower={'gap_m':8.,'update_s':.1,'sensing_s':.1,'reaction_s':1.2,
                  'brake_limit':.8,'deceleration_mps2':6.}
        if kind=='diagnostic_insufficient':
            follower.update(gap_m=6.,reaction_s=2.5,brake_limit=.6,deceleration_mps2=4.)
        options={'output':str(output),'labels':{'test':kind,'mode':mode,'assistance':'OFF','seed':9001},
                 'diagnostic':{'brake_distance_to_crosswalk_m':45.},
                 'followers':{k:dict(follower) for k in ('follower_b','follower_c')} if delayed else {},
                 'weather':{'cloudiness':20.,'sun_altitude_angle':45.,'sun_azimuth_angle':220.}}
        speed,seed,trigger=40.,9001,5.
        pedestrian_behavior='wait'
    else:
        root=STUDY_ROOT/'Theme_03'/'Test_001'
        cfg=json.loads((root/'test_config.json').read_text())
        output=root/mode/assistance
        options={'output':str(output),'labels':{'test':cfg['test'],'mode':mode,'assistance':assistance,'seed':cfg['seed']},
                 'followers':cfg['followers_automatic' if mode=='Automatic' else 'followers_retrofit'],
                 'weather':cfg['weather'],'occluder':cfg['occluder'],
                 'driver_response_s':cfg['driver_response_s'],'pedestrian_speed_mps':cfg['pedestrian_speed_mps']}
        if cfg.get('encounter_timing'):
            options.update(encounter_timing=cfg['encounter_timing'],assets_output=str(root/mode/'assets'/assistance))
        speed,seed,trigger=cfg['speed_kmh'],cfg['seed'],cfg['trigger_m']
        pedestrian_behavior='cross'
    if (output/'results.json').exists():
        raise RuntimeError('Refusing to overwrite completed/failed run '+str(output))
    output.mkdir(parents=True,exist_ok=True)
    params={'town':'Town10HD','vehicles':0,'walkers':0,'seed':seed,'perception_mode':'rgb',
        'device':'gpu','receiver_mode':'autonomous' if mode=='Automatic' else 'retrofit',
        'enabled':assistance=='ON','group_mode':'cooperative','communication_profile':'hybrid',
        'baseline_behavior':'local_vision','following_gap_m':8.,'target_speed_kmh':speed,
        'crossing_trigger_distance_m':trigger,'pedestrian_behavior':pedestrian_behavior,
        'record_six_views':True,'observation_window_s':20.,
        'follower_model':'delayed' if options['followers'] else 'ideal','study_options':options}
    write_json(output/'requested_config.json',params)
    try:
        command('stop')
        command('experiment',params)
        print(json.dumps(command('start')),flush=True)
        begun=time.monotonic()
        last=-1
        while time.monotonic()-begun<1200:
            status=requests.get(BASE+'/api/status',timeout=30).json()
            sim=status.get('sim_time_s',0)
            if int(sim)//2 != last:
                print(json.dumps({'state':status['state'],'sim_s':sim,'fps':status.get('measured_fps')}),flush=True)
                last=int(sim)//2
            if status['state']=='error': raise RuntimeError(status)
            if status['state']=='finished':
                write_json(output/'terminal_status.json',status)
                print('FINISHED '+str(output),flush=True)
                return
            time.sleep(1)
        raise TimeoutError('Physical run did not finish within 1200 wall seconds')
    except Exception as exc:
        write_json(output/'technical_failure.json',{'error':str(exc),'requested_config':params})
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','diagnostic_ideal','diagnostic_ideal_valid','diagnostic_delayed','diagnostic_insufficient','first','synchronized'])
    p.add_argument('--mode',choices=['Automatic','Retrofit'],default='Automatic')
    p.add_argument('--assistance',choices=['OFF','ON'],default='OFF')
    args=p.parse_args()
    prepare() if args.action=='prepare' else run(args.action,args.mode,args.assistance)
