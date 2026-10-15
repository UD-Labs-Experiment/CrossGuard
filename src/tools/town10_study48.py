"""Twelve stable tests / 48 total runs. Preserve completed runs; single bridge queue."""
import argparse,copy,hashlib,json,random,shutil
from pathlib import Path
from crossguard.paths import STUDY_ROOT
from town10_batch12 import run_one
from town10_study import write_json

PLAN=STUDY_ROOT/'study_48_plan.json'
def completed(output):
    path=output/'results.json'
    if not path.exists():return False
    result=json.loads(path.read_text())
    return result.get('terminal_reason')=='observation_window_complete' and result.get('technical_failure') is False

def preserve_failed_attempt(output,cfg,mode,state):
    root=STUDY_ROOT.resolve();output=output.resolve()
    if not output.is_relative_to(root) or output==root:raise ValueError('Attempt path outside study')
    if completed(output):raise RuntimeError('Cannot archive a completed run')
    if not output.exists():return
    media=(output.parent/'assets'/state).resolve()
    if not media.is_relative_to(root):raise ValueError('Assets outside study')
    base=root/'Failed'/'Run_Attempts'/cfg['test']/mode/state
    index=1
    while (base/f'Attempt_{index:03}').exists():index+=1
    destination=base/f'Attempt_{index:03}'
    destination.mkdir(parents=True)
    shutil.move(str(output),str(destination/'numeric'))
    if media.exists():shutil.move(str(media),str(destination/'assets'))
    write_json(destination/'attempt_preservation.json',{'original_output':str(output),'original_assets':str(media),
       'reason':'Incomplete/technical attempt preserved before identical-seed retry','configuration':cfg})
def prepare():
    if PLAN.exists():raise RuntimeError('Already frozen; cannot resample')
    old=json.loads((STUDY_ROOT/'batch_12_plan.json').read_text())
    inventory=json.loads((STUDY_ROOT/'batch_site_inventory.json').read_text())
    sites={s['candidate_id']:s for s in inventory['candidates']}
    tests=[copy.deepcopy(c) for c in old['tests'] if c['test'] in ('Test_003','Test_004')]
    first=json.loads((STUDY_ROOT/'Theme_03/Test_001/test_config.json').read_text())
    first.update(relative_path='Theme_03/Test_001',daylight='afternoon',weather_label='clear',historical=True)
    tests.append(first)
    weather={
      'clear':{'cloudiness':10.,'sun_altitude_angle':18.,'sun_azimuth_angle':90.},
      'overcast':{'cloudiness':85.,'sun_altitude_angle':70.,'sun_azimuth_angle':180.},
      'rainy':{'cloudiness':100.,'precipitation':65.,'precipitation_deposits':60.,'wetness':90.,'wind_intensity':30.,'fog_density':10.,'fog_distance':25.,'sun_altitude_angle':30.,'sun_azimuth_angle':250.}}
    descriptors=[(2,3,'clear','morning','Town10HD_crosswalk_03',(40,46),(6,8)),
      (6,2,'overcast','noon','Town10HD_crosswalk_04',(32,34),(8,12)),
      (7,4,'clear','morning','Town10HD_crosswalk_03',(34,38),(8,10)),
      (8,4,'rainy','afternoon','Town10HD_crosswalk_03',(38,42),(6,10)),
      (9,5,'overcast','noon','Town10HD_crosswalk_03',(34,38),(8,12)),
      (10,5,'rainy','afternoon','Town10HD_crosswalk_03',(38,42),(6,10)),
      (11,6,'clear','morning','Town10HD_crosswalk_04',(32,34),(8,12)),
      (12,6,'overcast','noon','Town10HD_crosswalk_03',(34,38),(6,10))]
    for n,theme,w,day,site,speeds,gaps in descriptors:
        rng=random.Random(3000+n);auto={};speed=round(rng.uniform(*speeds),3)
        for name in ('follower_b','follower_c'):
            auto[name]={'gap_m':round(rng.uniform(*gaps),3),'update_s':round(rng.uniform(.05,.2),3),
                'sensing_s':round(rng.uniform(.05,.2),3),'reaction_s':round(rng.uniform(.2,.8),3),
                'brake_limit':round(rng.uniform(.6,1),3),'deceleration_mps2':round(rng.uniform(4,7),3)}
        retro={name:{**v,'reaction_s':round(rng.uniform(1.1,2.5),3)} for name,v in auto.items()}
        cfg={'test':f'Test_{n:03}','theme':f'Theme_{theme:02}','relative_path':f'Theme_{theme:02}/Test_{n:03}',
          'seed':3000+n,'location':site,'site':sites[site],'daylight':day,'weather_label':w,'weather':weather[w],
          'occluder':'vehicle.mitsubishi.fusorosa','speed_kmh':speed,'followers_automatic':auto,'followers_retrofit':retro,
          'driver_response_s':round(rng.uniform(1.1,2.5),3),'pedestrian_speed_mps':round(rng.uniform(1.6,1.8),3),
          'observation_window_s':20.,'phone_refresh_pending_by_target':True,
          'encounter_timing':{'tolerance_s':.25,'minimum_speed_mps':6.,'pedestrian_acceleration_mps2':2.5,
             'rule':'measured A arrival vs normally accelerating walker; one shot, no retiming'},
          'sampling':'Random(3000+test number); declared ranges before outcomes','pedestrian_behavior':'cross'}
        if n in (7,8):
            cfg['channels']={key:{'latency_s':round(rng.uniform(*bounds),3),'jitter_s':jitter,
                'loss':round(rng.uniform(0,loss),3),'range_m':distance} for key,bounds,jitter,loss,distance in
                [('radio',(.05,.25),.015,.2,300.),('cellular',(.3,.8),.1,.2,10000.),('optical',(.01,.1),.003,.1,120.)]}
            cfg['channel_outages']=([{'channel':key,'start_s':i*3.,'end_s':(i+1)*3.} for i,key in enumerate(('radio','cellular','optical'))]
               if n==7 else [{'channel':key,'start_s':0.,'end_s':8.} for key in ('radio','cellular','optical')])
        if n in (9,10):cfg['observation_outages']=[{'start_s':5.,'end_s':5.5 if n==9 else 6.,'sources':['driver','bus','rsu']}]
        if n==10:cfg['additional_occluder']=True
        if n==11:cfg['pedestrian_behavior']='wait'
        if n==12:cfg.update(pedestrian_behavior='retreat',retreat_after_s=1.)
        tests.append(cfg)
    fifth=copy.deepcopy(next(c for c in old['tests'] if c['test']=='Test_005'))
    fifth['encounter_timing']['pedestrian_acceleration_mps2']=2.5
    fifth['pre_run_amendment']='Previously unstarted; startup model amended from physical displacement evidence before first run. Original batch_12_plan remains preserved.'
    tests.append(fifth)
    tests.sort(key=lambda c:c['test'])
    for cfg in tests:
        if cfg.get('historical') or cfg['test'] in ('Test_003','Test_004'):continue
        write_json(STUDY_ROOT/cfg['relative_path']/'test_config.json',cfg)
    write_json(PLAN,{'physical_runs_requested':48,'tests':tests,'completed_at_freeze':12,
        'protocol':'remaining_runs_protocol.md','no_outcome_tuning':True,'historical_control_versions_preserved':True})
    (STUDY_ROOT/'remaining_runs_protocol.md').write_text(Path('src/reports/TOWN10HD_REMAINING_RUNS_PROTOCOL.md').read_text(),encoding='utf8')
    provenance=STUDY_ROOT/'study_48_provenance';provenance.mkdir(exist_ok=True)
    files=[Path('src/crossguard/simulation')/n for n in ('city_world.py','encounter_timing.py','study_measurements.py')]
    files += [Path('src/crossguard/communication')/n for n in ('channels.py','braking.py')]+[Path('src/crossguard/policy/following.py'),Path(__file__)]
    hashes={}
    for path in files:
        hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        (provenance/path.name).write_bytes(path.read_bytes())
    write_json(provenance/'source_hashes.json',hashes)
    print('Frozen 12 tests / 48 total runs, 36 remaining')

def run(test=None):
    plan=json.loads(PLAN.read_text())
    # Validate corrected walking synchronization in Test_002 before the other new cases.
    entries=sorted(plan['tests'],key=lambda c:(c['test']!='Test_002',c['test']))
    for cfg in entries:
        if test and cfg['test']!=test:continue
        for mode in ('Automatic','Retrofit'):
            for state in ('OFF','ON'):
                output=STUDY_ROOT/cfg['relative_path']/mode/state
                if completed(output):continue
                if output.exists():preserve_failed_attempt(output,cfg,mode,state)
                run_one(cfg,mode,state)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','run']);parser.add_argument('--test')
    args=parser.parse_args();prepare() if args.action=='prepare' else run(args.test)
