"""Offline per-configuration assessment and evidence-package readiness seal."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from town10_continuation import (ROOT, MODES, STATES, read, save, digest,
    verified_catalogue, assess_run, evaluate, load_run, qualify)


def assess(root,test):
    cfg=next(c for c in verified_catalogue(root)['tests'] if c['test']==test)
    folder=root/cfg['relative_path'];verdicts={}
    for mode in MODES:
        runs={}
        for state in STATES:
            run=folder/mode/f'Assistance_{state}'
            if (run/'results.json').exists():
                assess_run(run);save(run/'encounter_assessment.json',evaluate(run))
            runs[state]=load_run(run)
        verdicts[mode]=qualify(folder,mode,runs['OFF'],runs['ON'],cfg)
    save(folder/'protocol_assessment.json',verdicts)
    return all(v['qualified'] for v in verdicts.values())


def seal(root,phase='Validation'):
    catalogue=verified_catalogue(root);errors=[];proofs={};complete=0
    tests=[c for c in catalogue['tests'] if c['test'].startswith(phase)]
    for cfg in tests:
        folder=root/cfg['relative_path']
        for mode in MODES:
            review_path=folder/mode/'scene_review.json'
            review=read(review_path) if review_path.exists() else {}
            if not all(review.get(k) is True for k in ('actual_rgb_inspected',
                'native_markings_visible','pedestrian_access_verified','route_verified',
                'useful_occlusion_verified','physical_gaps_verified')):
                errors.append(f'{cfg["test"]}/{mode}: independent scene review missing')
            if review_path.exists():proofs[str(review_path.relative_to(root))]=digest(review_path)
            for state in STATES:
                run=folder/mode/f'Assistance_{state}'
                if not load_run(run)['complete']:
                    errors.append(f'{cfg["test"]}/{mode}/{state}: incomplete numeric evidence')
                    continue
                complete+=1
                for relative,expected in review.get('evidence_sha256',{}).get(state,{}).items():
                    path=run/relative
                    if not path.exists() or digest(path)!=expected:
                        errors.append(f'{path}: reviewed evidence changed')
                picture_check=run/'assets/picture_verification.json'
                pic=read(picture_check) if picture_check.exists() else {}
                if pic.get('complete_synchronized_sets')!=201 or pic.get('decoded_images')!=1206:
                    errors.append(f'{run}: six-view decode/coverage incomplete')
                paths=[run/'results.json',run/'physical_timeline.json',run/'camera_manifest.json',picture_check]
                video=run/f'assets/videos/{mode}_Assistance_{state}.mp4'
                paths.extend((video,video.with_suffix('.verification.json'),video.with_suffix('.source_frames.json')))
                comparison=folder/mode/f'Comparison/{mode}_Assistance_OFF_vs_ON.mp4'
                if state=='OFF':paths.extend((comparison,comparison.with_suffix('.verification.json'),comparison.with_suffix('.source_frames.json')))
                for path in paths:
                    if not path.exists():errors.append(f'{path}: missing');continue
                    if path.name.endswith('.verification.json'):
                        verification=read(path)
                        if verification.get('passed') is not True or verification.get('decoded_frames')!=201:
                            errors.append(f'{path}: video verification failed')
                    proofs[str(path.relative_to(root))]=digest(path)
    expected=28 if phase=='Validation' else 240
    gate={'phase':phase,'passed':not errors and complete==expected,
        'catalogue_sha256':digest(root/'frozen_catalogue.json'),
        'completed_runs':complete,'expected_runs':expected,'errors':errors,'evidence_sha256':proofs,
        'note':'Offline seal of independently inspected pictures and fully decoded source-aligned videos. No CARLA client or world ticking.'}
    save(root/f'Reports/{phase.lower()}_package_gate.json',gate)
    return gate


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('assess','seal'))
    p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--test')
    p.add_argument('--phase',choices=('Validation','Robustness'),default='Validation')
    args=p.parse_args()
    if args.action=='assess':
        passed=assess(args.root,args.test);print('Protocol qualification:',passed)
    else:
        gate=seal(args.root,args.phase);passed=gate['passed']
        print({'passed':passed,'completed_runs':gate['completed_runs'],
            'error_count':len(gate['errors']),'report':str(args.root/f'Reports/{args.phase.lower()}_package_gate.json')})
    raise SystemExit(0 if passed else 1)
