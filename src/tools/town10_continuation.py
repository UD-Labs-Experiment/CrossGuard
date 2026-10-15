"""Frozen continuation catalogue and fail-closed validation gate; one bridge only."""
import argparse
import copy
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crossguard.paths import PROJECT_ROOT, STUDY_ROOT
from crossguard.simulation.study_assessment import assess_run
from synchronized_package import evaluate
from town10_batch12 import run_one
from town10_efficiency_report import load_run, qualify, outcome, nuisance_enactment

ROOT = PROJECT_ROOT / 'Evidence' / 'Study_02_Town10HD_Continuation'
MODES = ('Automatic', 'Retrofit')
STATES = ('OFF', 'ON')
THEMES = ('Crosswalk_Location', 'Weather_and_Visibility',
          'Following_and_Secondary_Collisions', 'Communication_Failures',
          'Observation_Loss_and_Occlusion', 'Waiting_and_Retreating_Pedestrians')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configuration(base, theme, number, phase):
    seed = (41000 if phase == 'Validation' else 51000) + theme * 100 + number
    rng = random.Random(seed)
    cfg = copy.deepcopy(base)
    for key in tuple(cfg):
        if any(word in key for word in ('amendment', 'historical', 'original_site')):
            cfg.pop(key)
    cfg.update(test=f'{phase}_T{theme:02}_{number:02}', theme=f'T{theme:02}',
               seed=seed, readable_name=f'{phase} Theme {theme} Test {number}',
               relative_path=f'{phase}/Tests/Theme_{theme:02}_{THEMES[theme-1]}/Test_{number:02}',
               pedestrian_behavior='cross', observation_window_s=20.,
               phone_refresh_pending_by_target=True,
               sampling='Deterministic pre-outcome Random(seed); no replacement seeds')
    cfg['encounter_timing'] = {'tolerance_s': .25, 'minimum_speed_mps': 6.,
        'pedestrian_acceleration_mps2': 2.5, 'pair_trigger_tolerance_s': 1/60+.00001,
        'rule': 'one-shot measured acceleration-to-cruise A prediction versus accelerating normal walk; never retime after braking'}
    cfg['speed_kmh'] = 34.
    cfg['pedestrian_speed_mps'] = 1.75
    for followers in ('followers_automatic', 'followers_retrofit'):
        cfg[followers] = {name: {'gap_m': 7., 'update_s': .1, 'sensing_s': .1,
            'reaction_s': .4 if followers.endswith('automatic') else 1.8,
            'brake_limit': .85, 'deceleration_mps2': 5.5}
            for name in ('follower_b', 'follower_c')}
    cfg['driver_response_s'] = 1.8
    if phase == 'Robustness':
        # Individual factors first; selected combinations occupy cases 6-10.
        if theme == 1:
            cfg['pedestrian_speed_mps'] = round(rng.uniform(1.6, 1.8), 3)
        if number > 5:
            cfg['speed_kmh'] = round(rng.uniform(34., 42.), 3)
            cfg['pedestrian_speed_mps'] = round(rng.uniform(1.6, 1.8), 3)
            cfg['driver_response_s'] = round(rng.uniform(1.1, 2.5), 3)
            for followers in ('followers_automatic', 'followers_retrofit'):
                for follower in cfg[followers].values():
                    follower.update(gap_m=round(rng.uniform(6., 8.), 3),
                        update_s=round(rng.uniform(.05, .2), 3),
                        sensing_s=round(rng.uniform(.05, .2), 3),
                        reaction_s=round(rng.uniform(*((.2, .8) if followers.endswith('automatic') else (1.1, 2.5))), 3),
                        brake_limit=round(rng.uniform(.6, 1.), 3),
                        deceleration_mps2=round(rng.uniform(4., 7.), 3))
    for key in ('channels', 'channel_outages', 'observation_outages',
                'additional_occluder', 'retreat_after_s'):
        cfg.pop(key, None)
    day = ('morning', 'noon', 'afternoon')[(number-1) % 3]
    weather = ('clear', 'overcast', 'rainy')[(number-1) % 3] if theme == 2 else 'clear'
    cfg.update(daylight=day, weather_label=weather,
        weather={'cloudiness': {'clear': 10., 'overcast': 85., 'rainy': 100.}[weather],
                 'sun_altitude_angle': {'morning': 18., 'noon': 70., 'afternoon': 30.}[day],
                 'sun_azimuth_angle': {'morning': 90., 'noon': 180., 'afternoon': 250.}[day],
                 'precipitation': 65. if weather == 'rainy' else 0.,
                 'wetness': 90. if weather == 'rainy' else 0.,
                 'precipitation_deposits': 60. if weather == 'rainy' else 0.})
    if theme == 3:
        cfg['speed_kmh'] = (34., 38., 42.)[(number-1) % 3]
        if phase == 'Robustness':
            for followers in ('followers_automatic', 'followers_retrofit'):
                for follower in cfg[followers].values():
                    follower['gap_m'] = round(rng.uniform(6., 8.), 3)
                    follower['reaction_s'] = round(rng.uniform(*((.2, .8) if followers.endswith('automatic') else (1.1, 2.5))), 3)
                    follower['brake_limit'] = round(rng.uniform(.6, 1.), 3)
    if theme == 4:
        cfg['channels'] = {channel: {'latency_s': latency, 'jitter_s': .01,
            'loss': 0., 'range_m': distance} for channel, latency, distance in
            (('radio', .1, 300.), ('cellular', .5, 10000.), ('optical', .05, 120.))}
        cfg['channel_outages'] = [{'channel': channel, 'start_s': start, 'end_s': end}
            for channel, start, end in (('radio', 0., 3.), ('cellular', 3., 6.), ('optical', 6., 9.),
            ('radio', 9., 12.), ('cellular', 9., 12.), ('optical', 9., 12.))]
        if phase == 'Robustness':
            for channel in cfg['channels'].values():
                channel['latency_s'] = round(rng.uniform(.05, .8), 3)
                channel['loss'] = round(rng.uniform(0., .2), 3)
    if theme == 5:
        cfg['observation_outages'] = [{'start_s': 5., 'end_s': 5.5,
                                      'sources': ['driver', 'bus', 'rsu']}]
        if phase == 'Robustness' and number > 5:
            cfg['additional_occluder'] = True
    if theme == 6:
        cfg['pedestrian_behavior'] = 'wait' if number % 2 else 'retreat'
        if cfg['pedestrian_behavior'] == 'retreat':
            cfg['retreat_after_s'] = 1.
    return cfg


def prepare(root=ROOT):
    root = Path(root)
    if (root/'frozen_catalogue.json').exists():
        raise RuntimeError('Catalogue already frozen; never resample or overwrite')
    old = read(STUDY_ROOT/'Reports/Study_Configuration/study_48_plan.json')
    bases = {c['location']: c for c in old['tests'] if 'site' in c}
    # Last recorded native spawn geometry is retained; no outcome selected seed.
    configurations = []
    for phase in ('Validation', 'Robustness'):
        for theme in range(1, 7):
            count = (2 if theme == 6 else 1) if phase == 'Validation' else 10
            for number in range(1, count+1):
                site = 'Town10HD_crosswalk_04' if theme == 1 and number % 2 else 'Town10HD_crosswalk_03'
                cfg = configuration(bases[site], theme, number, phase)
                cfg['occluder'] = 'vehicle.carlamotors.carlacola' if site.endswith('04') else 'vehicle.mitsubishi.fusorosa'
                configurations.append(cfg)
                folder = root/cfg['relative_path']
                save(folder/'test_config.json', cfg)
                (folder/'TEST_OVERVIEW.md').write_text(
                    f'# {cfg["readable_name"]}\n\nIntended behavior: {cfg["pedestrian_behavior"]}. '
                    'Actual behavior: not yet measured. Qualification: pending; no completed run claimed.\n', encoding='utf-8')
                for mode in MODES:
                    (folder/mode/'Comparison').mkdir(parents=True, exist_ok=True)
                    for state in STATES:
                        for asset in ('pictures', 'videos'):
                            (folder/mode/f'Assistance_{state}'/'assets'/asset).mkdir(parents=True, exist_ok=True)
    sources = [p for p in (PROJECT_ROOT/'src/crossguard').rglob('*.py')]
    sources += [Path(__file__), PROJECT_ROOT/'src/tools/town10_batch12.py']
    catalogue = {'validation_runs_requested': 28, 'robustness_runs_requested': 240,
        'worker_model': 'gemini-3.8-flash-medium', 'tests': configurations,
        'ranges': {'speed_kmh': [34, 42], 'physical_gap_m': [6, 8],
                   'pedestrian_speed_mps': [1.6, 1.8], 'sensing_s': [.05, .2],
                   'update_s': [.05, .2], 'deceleration_mps2': [4., 7.],
                   'automatic_reaction_s': [.2, .8], 'retrofit_reaction_s': [1.1, 2.5],
                   'brake_limit': [.6, 1.], 'channel_latency_s': [.05, .8], 'channel_loss': [0, .2]},
        'source_hashes': {str(p.relative_to(PROJECT_ROOT)): digest(p) for p in sources},
        'rain_affects_visibility_only': True, 'validation_gate_required': True,
        'design_limitations': ['T04 outage windows share an encounter; individual causal effects are not identified.',
            'Useful occlusion and both native locations require physical evidence and independent visual review.',
            'Robustness cases 1-5 vary individual theme factors; 6-10 include selected combinations.',
            'No confirmatory evaluation is authorized yet.']}
    save(root/'frozen_catalogue.json', catalogue)
    save(root/'frozen_catalogue.sha256.json', {'sha256': digest(root/'frozen_catalogue.json')})
    for directory in ('Reports', 'Diagnostics', 'Failed'):
        (root/directory).mkdir(exist_ok=True)
    print('Frozen 7 validation configurations / 28 runs and 60 robustness configurations / 240 runs')


def verified_catalogue(root):
    if digest(root/'frozen_catalogue.json') != read(root/'frozen_catalogue.sha256.json')['sha256']:
        raise RuntimeError('Frozen catalogue changed')
    catalogue = read(root/'frozen_catalogue.json')
    for source, expected in catalogue['source_hashes'].items():
        if digest(PROJECT_ROOT/source) != expected:
            raise RuntimeError('Runtime source changed after freeze: '+source)
    return catalogue


def run(root, test, mode=None, state=None):
    catalogue = verified_catalogue(root)
    cfg = next(c for c in catalogue['tests'] if c['test'] == test)
    if test.startswith('Robustness'):
        gate = read(root/'Reports/validation_gate.json')
        if gate.get('passed') is not True or gate.get('catalogue_sha256') != digest(root/'frozen_catalogue.json'):
            raise RuntimeError('Robustness prohibited: current validation gate has not passed')
        package_path=root/'Reports/validation_package_gate.json'
        package=read(package_path) if package_path.exists() else {}
        if package.get('passed') is not True or package.get('catalogue_sha256')!=digest(root/'frozen_catalogue.json'):
            raise RuntimeError('Robustness prohibited: independently reviewed media package has not passed')
    for m in (mode,) if mode else MODES:
        for s in (state,) if state else STATES:
            output = root/cfg['relative_path']/m/f'Assistance_{s}'
            if (output/'results.json').exists() or (output/'technical_failure.json').exists():
                raise RuntimeError('Existing attempt retained; explicit same-seed repair required')
            run_one(cfg, m, s, study_root=root, organized=True)
            if not (output/'results.json').exists():
                raise RuntimeError('Worker run failed; preserve attempt and diagnose before retry')


def check(root):
    catalogue = verified_catalogue(root)
    pairs=[];complete=0;failed=0
    for cfg in (c for c in catalogue['tests'] if c['test'].startswith('Validation')):
        folder=root/cfg['relative_path'];records={}
        for mode in MODES:
            runs={}
            for state in STATES:
                output=folder/mode/f'Assistance_{state}'
                if (output/'results.json').exists():
                    assess_run(output)
                    evaluation=evaluate(output)
                    save(output/'encounter_assessment.json',evaluation)
                run_record=load_run(output);runs[state]=run_record
                complete+=int(run_record['complete'])
                failed+=int(bool(run_record['technical_failure_file']))
            qualification=qualify(folder,mode,runs['OFF'],runs['ON'],cfg)
            scene_review_path=folder/mode/'scene_review.json'
            review=read(scene_review_path) if scene_review_path.exists() else {}
            # A single ray cannot establish useful hiding or pedestrian access.
            physical_scene=all(review.get(k) is True for k in
                ('actual_rgb_inspected','native_markings_visible','pedestrian_access_verified',
                 'route_verified','useful_occlusion_verified','physical_gaps_verified'))
            accepted=qualification['qualified'] and physical_scene
            records[mode]={'qualification':qualification,'independent_scene_review':review,
                'accepted':accepted,'outcomes':{s:outcome(r['result']) for s,r in runs.items()}}
            pairs.append({'test':cfg['test'],'mode':mode,**records[mode]})
        save(folder/'validation_assessment.json',records)
        (folder/'TEST_OVERVIEW.md').write_text(f'# {cfg["readable_name"]}\n\n'
            f'Intended behavior: {cfg["pedestrian_behavior"]}.\n\n'
            'Actual behavior, qualification and failure reasons are recorded for each mode in '
            '[validation_assessment.json](validation_assessment.json). '
            'Stationary failed emergence is never a successful crossing.\n',encoding='utf-8')
    package_path=root/'Reports/validation_package_gate.json'
    package=read(package_path) if package_path.exists() else {}
    media_ready=package.get('passed') is True and package.get('catalogue_sha256')==digest(root/'frozen_catalogue.json')
    gate={'passed':complete==28 and all(p['accepted'] for p in pairs) and media_ready,
        'independent_media_package_passed':media_ready,
        'catalogue_sha256':digest(root/'frozen_catalogue.json'),
        'completed_runs':complete,'technical_failures':failed,
        'qualified_mode_pairs':sum(p['qualification']['qualified'] for p in pairs),
        'accepted_mode_pairs':sum(p['accepted'] for p in pairs),'pairs':pairs,
        'robustness_started':False}
    save(root/'Reports/validation_gate.json',gate)
    print(json.dumps({k:v for k,v in gate.items() if k!='pairs'},indent=2))
    return gate


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','run','check'))
    parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--test')
    parser.add_argument('--mode',choices=MODES);parser.add_argument('--state',choices=STATES)
    args=parser.parse_args()
    if args.action=='prepare':prepare(args.root)
    elif args.action=='check':check(args.root)
    else:run(args.root,args.test,args.mode,args.state)
