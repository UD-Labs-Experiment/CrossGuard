"""Frozen three-test / twelve-run expansion. Single bridge command queue only."""
import argparse,json,random,time
from pathlib import Path
import requests
from crossguard.paths import STUDY_ROOT
from town10_study import BASE,command,write_json

def prepare():
    target=STUDY_ROOT/'batch_12_plan.json'
    if target.exists():raise RuntimeError('Batch already frozen; no resampling')
    inventory=json.loads((STUDY_ROOT/'batch_site_inventory.json').read_text())
    sites={s['candidate_id']:s for s in inventory['candidates']}
    descriptors=[('Test_003','Theme_01','Town10HD_crosswalk_03','morning','clear',(32,36),'vehicle.mitsubishi.fusorosa',{'cloudiness':10.,'sun_altitude_angle':18.,'sun_azimuth_angle':90.}),
                 ('Test_004','Theme_01','Town10HD_crosswalk_04','noon','overcast',(30,34),'vehicle.carlamotors.carlacola',{'cloudiness':85.,'sun_altitude_angle':70.,'sun_azimuth_angle':180.}),
                 ('Test_005','Theme_02','Town10HD_crosswalk_03','afternoon','rainy',(38,42),'vehicle.mitsubishi.fusorosa',{'cloudiness':100.,'precipitation':65.,'precipitation_deposits':60.,'wetness':90.,'wind_intensity':30.,'fog_density':10.,'fog_distance':25.,'sun_altitude_angle':30.,'sun_azimuth_angle':250.})]
    tests=[]
    for name,theme,site,day,weather,speeds,occluder,preset in descriptors:
        seed=2000+int(name[-3:]);rng=random.Random(seed);auto={}
        speed=round(rng.uniform(*speeds),3)
        for n in ('follower_b','follower_c'):
            auto[n]={'gap_m':round(rng.uniform(6,8),3),'update_s':round(rng.uniform(.05,.2),3),'sensing_s':round(rng.uniform(.05,.2),3),'reaction_s':round(rng.uniform(.2,.8),3),'brake_limit':round(rng.uniform(.6,1),3),'deceleration_mps2':round(rng.uniform(4,7),3)}
        retro={n:{**v,'reaction_s':round(rng.uniform(1.1,2.5),3)} for n,v in auto.items()}
        cfg={'test':name,'seed':seed,'theme':theme,'relative_path':theme+'/'+name,'location':site,'site':sites[site],
             'daylight':day,'weather_label':weather,'weather':preset,'occluder':occluder,'speed_kmh':speed,
             'followers_automatic':auto,'followers_retrofit':retro,'driver_response_s':round(rng.uniform(1.1,2.5),3),
             'pedestrian_speed_mps':round(rng.uniform(1.6,1.8),3),'observation_window_s':20.,
             'encounter_timing':{'tolerance_s':.25,'minimum_speed_mps':6.,'rule':'measured one-shot acceleration-to-cruise arrival at actual lane/walking-path intersection; no retiming'},
             'phone_refresh_pending_by_target':True,'sampling':'Python random.Random(seed), range decisions frozen before outcomes; no seed selection'}
        write_json(STUDY_ROOT/cfg['relative_path']/'test_config.json',cfg);tests.append(cfg)
    plan={'physical_runs_requested':12,'matched_tests':3,'run_order':'each test: Automatic OFF, ON, Retrofit OFF, ON','tests':tests,
          'ranges':{'physical_gap_m':[6,8],'automatic_reaction_s':[.2,.8],'retrofit_reaction_s':[1.1,2.5],'driver_response_s':[1.1,2.5],'pedestrian_speed_mps':[1.6,1.8]},
          'selection_rationale':'Two native sites pass route geometry for upstream 27m plus crossing+12m. Gap range bounded by physical route length. Fast walking range and site-specific speed bands make synchronized start feasible from normal rest launch; outcomes not used for choice.',
          'design_limit':'Exploratory varied configurations; location/weather/parameters co-vary, so no isolated factor-effect claim. Only two distinct native sites; three daylight/weather presets. Other themes remain planned.',
          'rgb_only':True,'evaluator_only_ground_truth':True,'no_night':True,'no_teleport_or_scripted_contact':True,
          'phone_change':'Fresh same-target notification evidence may refresh pending packet without restarting original reaction deadline. TTL/max-age checks unchanged. Default historical model unchanged outside opt-in batch.',
          'synchronization_qualification':'Pair trigger agreement <=one tick; same submitted walking controls; OFF measured undisturbed arrival vs actual/precontact pedestrian <=0.25s, ON uses matched OFF unassisted reference. Failures preserved and explicitly counted.'}
    write_json(target,plan)
    (STUDY_ROOT/'batch_12_protocol.md').write_text("""# Twelve-run expansion protocol
Frozen before runs. Three varied Town10HD configurations, four matched runs each. Native site inventory and batch_12_plan.json define all sampled settings. No seed tuning from outcomes. Morning clear/bus, noon overcast/truck at a second crossing, afternoon rain/bus. Bounds are geometry-conditioned within the earlier documented behavior ranges. Solar presets represent daylight conditions, not host wall-clock time.
Local safeguards, vehicles, followers and all sampled assumptions are identical within each OFF/ON pair. ON enables cooperative warnings to A/B/C. Fresh driver notification evidence may update during the original reaction interval but never eliminates that delay or resurrects stale information. Previous historical package remains separate.
Six actual RGB views at 10 Hz and full 60 Hz physics are saved in each mode's assets and numeric folders. Explicit submitted walker controls and control-command failures are now logged; impossible actor readbacks remain raw missing-measurement evidence. Initialization, native crosswalk, ray occlusion and physical placement are audited. Failed synchronization/contact/safeguard/technical measurements remain results, without replacement seeds or percentages.
These configurations combine several requested variations; they are exploratory robustness cases rather than causal single-factor estimates. The broader 12-test/48-run plan and channel-specific outages remain unexecuted in this 12-run request.
""",encoding='utf8')
    print(json.dumps({'tests':[{'test':c['test'],'location':c['location'],'daylight':c['daylight'],'weather':c['weather_label'],'speed_kmh':c['speed_kmh'],'pedestrian_speed_mps':c['pedestrian_speed_mps']} for c in tests]},indent=2))

def run_one(cfg,mode,state,study_root=None,organized=False):
    root=Path(study_root) if study_root is not None else STUDY_ROOT
    output=root/cfg['relative_path']/mode/(f'Assistance_{state}' if organized else state)
    if (output/'results.json').exists() or (output/'technical_failure.json').exists():raise RuntimeError('Preserved existing run; cannot overwrite '+str(output))
    options={k:cfg[k] for k in ('site','weather','occluder','driver_response_s','pedestrian_speed_mps','encounter_timing','phone_refresh_pending_by_target')}
    options.update(output=str(output),assets_output=str(output/'assets' if organized else output.parent/'assets'/state),labels={'test':cfg.get('readable_name',cfg['test']),'mode':mode,'assistance':f'Assistance {state}' if organized else state,'seed':cfg['seed']},followers=cfg['followers_automatic' if mode=='Automatic' else 'followers_retrofit'])
    options.update({k:cfg[k] for k in ('channels','channel_outages','observation_outages','additional_occluder') if k in cfg})
    params={'town':'Town10HD','vehicles':0,'walkers':0,'seed':cfg['seed'],'perception_mode':'rgb','device':'gpu','receiver_mode':'autonomous' if mode=='Automatic' else 'retrofit','enabled':state=='ON','group_mode':'cooperative','communication_profile':'hybrid','baseline_behavior':'local_vision','following_gap_m':8.,'target_speed_kmh':cfg['speed_kmh'],'crossing_trigger_distance_m':20.,'pedestrian_behavior':'cross','record_six_views':True,'observation_window_s':20.,'follower_model':'delayed','study_options':options}
    write_json(output/'requested_config.json',params)
    params['pedestrian_behavior']=cfg.get('pedestrian_behavior','cross')
    if 'retreat_after_s' in cfg:params['retreat_after_s']=cfg['retreat_after_s']
    write_json(output/'requested_config.json',params)
    print('START '+cfg['test']+' '+mode+' '+state,flush=True)
    try:
        command('stop');command('experiment',params);command('start');begun=time.monotonic();last=-1
        while time.monotonic()-begun<1200:
            status=requests.get(BASE+'/api/status',timeout=30).json();now=status.get('sim_time_s',0)
            if int(now)//4!=last:
                print(json.dumps({'test':cfg['test'],'mode':mode,'state':state,'simulation_s':now,'bridge':status['state']}),flush=True);last=int(now)//4
            if status['state']=='error':raise RuntimeError(status)
            if status['state']=='finished':
                write_json(output/'terminal_status.json',status);print('FINISHED '+str(output),flush=True);return
            time.sleep(1)
        raise TimeoutError('1200 wall-second watchdog')
    except Exception as exc:
        write_json(output/'technical_failure.json',{'error':str(exc),'requested_config':params});print('FAILED '+str(exc),flush=True)

def run(test=None):
    plan=json.loads((STUDY_ROOT/'batch_12_plan.json').read_text())
    for cfg in plan['tests']:
        if test and cfg['test']!=test:continue
        for mode in ('Automatic','Retrofit'):
            for state in ('OFF','ON'):run_one(cfg,mode,state)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--test');a=p.parse_args()
    prepare() if a.action=='prepare' else run(a.test)
