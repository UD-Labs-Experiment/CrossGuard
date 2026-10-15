"""Offline continuation comparisons with explicit denominators and missingness."""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from town10_continuation import ROOT, MODES, STATES, read, save, verified_catalogue
from town10_efficiency_report import (load_run, public_run, communication_report,
                                     passage_times, any_contact, comparison, qualify)


def wilson(events,n):
    if not n:return None
    z=1.959963984540054;rate=events/n;denominator=1+z*z/n
    center=(rate+z*z/(2*n))/denominator
    radius=z*math.sqrt(rate*(1-rate)/n+z*z/(4*n*n))/denominator
    return [max(0.,center-radius),min(1.,center+radius)]


def report(root,phase='Validation'):
    catalogue=verified_catalogue(root)
    gate=read(root/'Reports/validation_gate.json')
    pairs={(p['test'],p['mode']):p for p in gate['pairs']};tests=[]
    requested=28 if phase=='Validation' else 240
    def empty_counts():return {mode:{state:{endpoint:{'events':0,'known_complete_runs':0}
        for endpoint in ('pedestrian','A_B','B_C','any_contact')}
        for state in STATES} for mode in MODES}
    counts=empty_counts()
    behavior_counts={behavior:empty_counts() for behavior in ('cross','wait','retreat')}
    matched_differences=[];details=[];table=[]
    guard_contacts=[]
    lines=[f'# Town10HD continuation {phase.lower()} results','',
        f'Validation physical windows: {gate["completed_runs"]}/28. '
        f'Timing/behavior-qualified pairs: {gate["qualified_mode_pairs"]}/14. '
        f'Accepted pairs: {gate["accepted_mode_pairs"]}/14. Validation gate: {gate["passed"]}.',
        '', 'Contacts and qualification are separate. '
        'Unknown or missing observations never count as contact-free success.',
        '', '| Configuration | Mode | Physical runs | Protocol qualified | Scene accepted | Pedestrian OFF → ON | A/B OFF → ON | B/C OFF → ON |',
        '|---|---|---:|---|---|---|---|---|']
    for cfg in (c for c in catalogue['tests'] if c['test'].startswith(phase)):
        folder=root/cfg['relative_path'];record={'configuration':cfg,'modes':{}}
        for mode in MODES:
            runs={s:load_run(folder/mode/f'Assistance_{s}') for s in STATES}
            if phase=='Validation':verdict=pairs[(cfg['test'],mode)]
            else:
                q=qualify(folder,mode,runs['OFF'],runs['ON'],cfg)
                review_path=folder/mode/'scene_review.json'
                review=read(review_path) if review_path.exists() else {}
                verdict={'qualification':q,'independent_scene_review':review,
                    'accepted':q['qualified'] and all(review.get(key) is True for key in
                        ('actual_rgb_inspected','native_markings_visible','pedestrian_access_verified',
                         'route_verified','useful_occlusion_verified','physical_gaps_verified'))}
            values={s:r['outcome'] for s,r in runs.items()}
            for state,run in runs.items():
                if not run['complete']:continue
                for endpoint,value in {**run['outcome'],'any_contact':any_contact(run['outcome'])}.items():
                    if isinstance(value,bool):
                        counts[mode][state][endpoint]['known_complete_runs']+=1
                        counts[mode][state][endpoint]['events']+=int(value)
                        subgroup=behavior_counts[cfg['pedestrian_behavior']][mode][state][endpoint]
                        subgroup['known_complete_runs']+=1;subgroup['events']+=int(value)
            detail={'verdict':verdict,'runs':{s:public_run(r) for s,r in runs.items()},
                'matched_outcomes':comparison(values['OFF'],values['ON']),
                'communication':communication_report(runs['ON'],runs['OFF'],cfg,mode),
                'passage_times':{s:passage_times(r) for s,r in runs.items()}}
            for state,run in detail['runs'].items():
                guard_contacts.extend({'test':cfg['test'],'mode':mode,'assistance':state,
                    'behavior':cfg['pedestrian_behavior'],**context} for context in run['contact_context']
                    if context['potentially_guard_related'] is True)
            record['modes'][mode]=detail
            label=f'[{cfg["test"]}](../{cfg["relative_path"]}/TEST_OVERVIEW.md)'
            def difference(endpoint):return f'{values["OFF"][endpoint]} → {values["ON"][endpoint]}'
            table.append(f'| {label} | {mode} | {sum(r["complete"] for r in runs.values())}/2 | '
                f'{verdict["qualification"]["qualified"]} | {verdict["accepted"]} | '
                f'{difference("pedestrian")} | {difference("A_B")} | {difference("B_C")} |')
            if all(r['complete'] for r in runs.values()):
                matched_differences.append({'test':cfg['test'],'mode':mode,'behavior':cfg['pedestrian_behavior'],
                    'accepted':verdict['accepted'],'ON_minus_OFF_contacts':{
                        endpoint:int(values['ON'][endpoint])-int(values['OFF'][endpoint])
                        if all(isinstance(values[s][endpoint],bool) for s in STATES) else None
                        for endpoint in ('pedestrian','A_B','B_C')}})
            details+=['',f'## {cfg["readable_name"]} — {mode}',
                    '',f'Intended behavior: {cfg["pedestrian_behavior"]}. '
                    f'Qualification reasons: {verdict["qualification"]["reasons"]}.',
                    '',f'Independent scene review: {verdict["independent_scene_review"]}.']
            for state,run in runs.items():
                if not run['complete']:continue
                rel=Path('..')/cfg['relative_path']/mode/f'Assistance_{state}'
                details+=['',f'Assistance {state}: [Numeric results](<{rel.as_posix()}/results.json>) · '
                    f'[Physical timelines](<{rel.as_posix()}/physical_timeline.json>) · '
                    f'[Message evidence](<{rel.as_posix()}/messages.json>)',
                    '',f'![Initial six actual views](<{rel.as_posix()}/assets/initial_scene_six_views.jpg>)',
                    '',f'![Braking timeline](<{rel.as_posix()}/assets/braking_timeline.png>)']
        tests.append(record)
        overview=[f'# {cfg["readable_name"]}', '',
            f'Intended pedestrian behavior: **{cfg["pedestrian_behavior"]}**. Seed: {cfg["seed"]}.', '',
            'Intentional waiting is evaluated as stationary curb behavior with an approach opportunity. '
            'Crossing or retreating without the required movement is a failed encounter.', '',
            '| Mode | Complete windows | Actual behavior qualified | Scene accepted | Reasons |',
            '|---|---:|---|---|---|']
        for mode,detail in record['modes'].items():
            verdict=detail['verdict'];q=verdict['qualification']
            overview.append(f'| {mode} | {sum(r["complete_numeric_run"] for r in detail["runs"].values())}/2 | '
                f'{q["qualified"]} | {verdict["accepted"]} | {q["reasons"]} |')
            overview+=['',f'[{mode} Assistance OFF versus ON video]({mode}/Comparison/{mode}_Assistance_OFF_vs_ON.mp4)',
                f'[{mode} independent scene review]({mode}/scene_review.json)']
        overview+=['', '[Frozen test configuration](test_config.json)',
            'Numeric evidence, contact sensors and physical timelines are inside each Assistance folder. '
            'Actual pictures and videos are inside its assets folder. Missing media links mean pending verification.']
        (folder/'TEST_OVERVIEW.md').write_text('\n'.join(overview)+'\n',encoding='utf-8')
    for group in (counts,*behavior_counts.values()):
        for mode in group.values():
            for state in mode.values():
                for endpoint in state.values():
                    n=endpoint['known_complete_runs'];events=endpoint['events']
                    endpoint['observed_rate']=events/n if n else None
                    endpoint['illustrative_95_percent_wilson_interval']=wilson(events,n)
    prefix='' if phase=='Validation' else 'robustness_'
    completed=sum(r['complete_numeric_run'] for t in tests for p in t['modes'].values() for r in p['runs'].values())
    accepted=sum(p['verdict']['accepted'] for t in tests for p in t['modes'].values())
    lines.insert(2,f'{phase} completed windows: {completed}/{requested}; accepted pairs: {accepted}/{requested//2}.')
    save(root/f'Reports/{prefix}configuration_comparisons.json',{'phase':phase,
        'completed_runs':completed,'requested_runs':requested,'accepted_pairs':accepted,
        'tests':tests,'raw_endpoint_counts':counts,
        'endpoint_counts_by_behavior':behavior_counts,'matched_contact_differences':matched_differences,
        'potentially_guard_confounded_first_contacts':guard_contacts,
        'uncertainty_limitation':'Wilson intervals assume binomial sampling; this deliberately selected stress suite is not a representative population sample. Crossing and nuisance cases must be interpreted separately. No universal effectiveness estimate.',
        'robustness_runs_completed':completed if phase=='Robustness' else None})
    lines+=table+details
    lines+=['','## Limits','',
        'Failure reasons, guards, startup regulation, source-association failures and incomplete '
        'measurements are retained in configuration_comparisons.json. Contact-free outcomes '
        'alone do not establish coordination. Waiting/retreating and crossing results must be '
        'interpreted separately. Rain changes visibility without tire-friction adjustment. '
        'No completed-trip delay is claimed where passage is unobserved. '
        'The selected cases do not establish population effectiveness.']
    filename='Study_Report.md' if phase=='Validation' else 'Robustness_Report.md'
    (root/'Reports'/filename).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if phase=='Robustness':
        print(root/'Reports'/filename);return
    (root/'START_HERE.md').write_text('# Town10HD continuation\n\n'
        '[Validation results](Reports/Study_Report.md) · '
        '[Frozen catalogue](frozen_catalogue.json) · '
        '[Validation gate](Reports/validation_gate.json) · '
        '[Configuration comparisons](Reports/configuration_comparisons.json)\n\n'
        f'{gate["completed_runs"]}/28 completed validation windows; '
        f'{gate["accepted_mode_pairs"]}/14 accepted pairs. '
        'The 240-run robustness study remains gated.\n',encoding='utf-8')
    print(root/'START_HERE.md')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--phase',choices=('Validation','Robustness'),default='Validation')
    args=parser.parse_args();report(args.root,args.phase)
