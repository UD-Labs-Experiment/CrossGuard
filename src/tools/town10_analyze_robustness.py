"""Read-only paired analysis of sealed Town10HD evidence; no CARLA client."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path

SCENARIOS = {
    'T01': 'Crosswalk_Location', 'T02': 'Weather_and_Visibility',
    'T03': 'Following_and_Secondary_Collisions', 'T04': 'Communication_Failures',
    'T05': 'Observation_Loss_and_Occlusion', 'T06': 'Waiting_and_Retreating_Pedestrians',
}
OUTCOMES = ('pedestrian_contact', 'A_B_contact', 'B_C_contact', 'any_contact')
IMPLICATIONS = {
    'T01': 'The two native crossings and their truck/bus occluders expose geometry sensitivity. Location and occluder vary together, so their individual causal effects cannot be separated.',
    'T02': 'Dry, overcast and wet scenes were recorded with verified weather. Weather and daylight are coupled in the catalogue (clear/morning, overcast/noon, rainy/afternoon); this is not an independent factorial weather experiment. Rain is a visibility condition, not a validated wet-road friction model.',
    'T03': 'Lead-vehicle pedestrian protection must be evaluated alongside A/B and B/C contact outcomes. Short gaps, delayed following and brake-command caps challenge the full vehicle train; measured bumper contacts are not replaced by gap-based predictions.',
    'T04': 'Latency, loss and channel outages are simulated transport conditions. Compare single-channel degradation with declared all-channel outages and surviving paths; these results do not validate physical radio, cellular or optical equipment.',
    'T05': 'The declared observation blackout and added occluder challenge evidence freshness. A completed run or successful warning receipt does not establish that stale or missing observations were handled safely in every case.',
    'T06': 'Waiting and retreating are distinct nuisance/intent cases. Waiting should not be treated as a successful crossing. Retreat can initially justify caution; brake activity alone does not prove a false alarm. Pedestrian-contact avoidance must be considered together with follower contacts and unnecessary stopping exposure.',
}

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def metrics(result):
    if result.get('technical_failure') is not False:
        raise ValueError('Technical failure or missing failure status')
    secondary = result['secondary']
    flags = {'pedestrian_contact': bool(result['pedestrian_contacts']),
             'A_B_contact': bool(secondary['A_B']['contact']),
             'B_C_contact': bool(secondary['B_C']['contact'])}
    flags['any_contact'] = any(flags.values())
    hero = result['timeline']['hero_car']
    guard = hero.get('guard_first_s')
    events = {'pedestrian_contact': result['pedestrian_contacts'],
              'A_B_contact': secondary['A_B']['events'], 'B_C_contact': secondary['B_C']['events']}
    before_guard = {}; after_guard = {}
    for key, contacts in events.items():
        times = [e['precontact']['time_s'] for e in contacts]
        before_guard[key] = any(guard is None or time < guard for time in times)
        after_guard[key] = any(guard is not None and time >= guard for time in times)
    before_guard['any_contact'] = any(before_guard.values())
    after_guard['any_contact'] = any(after_guard.values())
    return {**flags, 'hero_first_brake_s': hero['first_brake_command_s'],
        'hero_brake_duration_s': hero['brake_command_duration_s'],
        'A_B_minimum_gap_m': secondary['A_B']['minimum_physical_bumper_gap_m'],
        'B_C_minimum_gap_m': secondary['B_C']['minimum_physical_bumper_gap_m'],
        'nuisance_configuration_label': result.get('unnecessary_braking'),
        'actual_outages': result.get('actual_outages', []), 'hero_guard_first_s': guard,
        'contacts_before_hero_guard': before_guard, 'contacts_after_hero_guard': after_guard}

def paired_outcome(pairs, outcome):
    transitions = {'avoided': 0, 'introduced': 0, 'persisted': 0, 'absent_both': 0}
    for pair in pairs:
        off, on = pair['OFF'][outcome], pair['ON'][outcome]
        transitions['persisted' if off and on else 'avoided' if off else
                    'introduced' if on else 'absent_both'] += 1
    return {'OFF': sum(p['OFF'][outcome] for p in pairs),
            'ON': sum(p['ON'][outcome] for p in pairs), **transitions}

def summarize(pairs):
    advances = [p['OFF']['hero_first_brake_s'] - p['ON']['hero_first_brake_s']
                for p in pairs if p['OFF']['hero_first_brake_s'] is not None
                and p['ON']['hero_first_brake_s'] is not None]
    return {'pairs': len(pairs), 'outcomes': {k: paired_outcome(pairs, k) for k in OUTCOMES},
            'before_hero_guard_counts': {s: {k: sum(p[s]['contacts_before_hero_guard'][k] for p in pairs)
                                             for k in OUTCOMES} for s in ('OFF', 'ON')},
            'after_hero_guard_counts': {s: {k: sum(p[s]['contacts_after_hero_guard'][k] for p in pairs)
                                            for k in OUTCOMES} for s in ('OFF', 'ON')},
            'hero_brake_advance_s_median': statistics.median(advances) if advances else None,
            'hero_brake_advance_comparable_pairs': len(advances),
            'hero_brake_duration_s_median': {s: statistics.median(
                p[s]['hero_brake_duration_s'] for p in pairs) for s in ('OFF', 'ON')}}

def table(groups):
    text = '| Mode | Pairs | Pedestrian OFF → ON | A/B OFF → ON | B/C OFF → ON | Any contact OFF → ON |\n|---|---:|---:|---:|---:|---:|\n'
    for mode, group in groups.items():
        counts = [f"{group['outcomes'][k]['OFF']} → {group['outcomes'][k]['ON']}" for k in OUTCOMES]
        text += f"| {mode} | {group['pairs']} | " + ' | '.join(counts) + ' |\n'
    return text

def interpretation(groups):
    automatic, retrofit = groups['Automatic']['outcomes'], groups['Retrofit']['outcomes']
    return (f"Automatic any-contact runs changed from {automatic['any_contact']['OFF']} to {automatic['any_contact']['ON']}; "
        f"pedestrian contact was {automatic['pedestrian_contact']['OFF']} OFF and {automatic['pedestrian_contact']['ON']} ON, "
        'so this mode does not establish a pedestrian-contact benefit over its baseline. '
        f"Retrofit pedestrian-contact runs changed from {retrofit['pedestrian_contact']['OFF']} to {retrofit['pedestrian_contact']['ON']}, "
        f"but {retrofit['any_contact']['ON']} assisted runs still had some contact. "
        f"B/C contact was newly present in {retrofit['B_C_contact']['introduced']} matched Retrofit pairs. "
        'These are descriptive counts within the tested configurations, not population crash-reduction estimates.')

def guard_table(groups):
    text = '| Mode/state | Any contact before lead corridor guard | Any contact after lead corridor guard |\n|---|---:|---:|\n'
    for mode, group in groups.items():
        for state in ('OFF', 'ON'):
            text += f"| {mode}/{state} | {group['before_hero_guard_counts'][state]['any_contact']} | {group['after_hero_guard_counts'][state]['any_contact']} |\n"
    return text

def analyze(root, destination, plots=True):
    root = root.resolve(); destination = destination.resolve()
    if destination.is_relative_to(root):
        raise ValueError('Analysis output must be outside sealed evidence')
    catalogue_path = root/'frozen_catalogue.json'
    sha = digest(catalogue_path)
    if sha != read(root/'frozen_catalogue.sha256.json')['sha256']:
        raise ValueError('Frozen catalogue checksum mismatch')
    catalogue = read(catalogue_path)
    gate = read(root/'Reports/robustness_package_gate.json')
    if not gate['passed'] or gate['completed_runs'] != 240 or gate['catalogue_sha256'] != sha:
        raise ValueError('Complete sealed robustness package is required')
    configs = [c for c in catalogue['tests'] if c['test'].startswith('Robustness_')]
    if len(configs) != 60:
        raise ValueError('Expected 60 configurations')
    pairs = []; identities = {}
    for cfg in configs:
        folder = root/cfg['relative_path']
        protocol = read(folder/'protocol_assessment.json')
        for mode, expected_mode in (('Automatic', 'autonomous'), ('Retrofit', 'retrofit')):
            if protocol[mode]['qualified'] is not True:
                raise ValueError('Unqualified pair')
            pair = {'test': cfg['test'], 'scenario': cfg['theme'], 'mode': mode,
                    'seed': cfg['seed'], 'behavior': cfg['pedestrian_behavior'],
                    'weather': cfg['weather_label'], 'daylight': cfg['daylight'],
                    'location': cfg['location'], 'config': cfg}
            review_path = folder/mode/'scene_review.json'
            if digest(review_path) != gate['evidence_sha256'][str(review_path.relative_to(root))]:
                raise ValueError('Sealed visual review changed')
            for state in ('OFF', 'ON'):
                path = folder/mode/f'Assistance_{state}'/'results.json'
                relative = str(path.relative_to(root))
                actual_sha = digest(path)
                if actual_sha != gate['evidence_sha256'][relative]:
                    raise ValueError('Sealed results changed')
                result = read(path)
                if result['mode'] != expected_mode or result['seed'] != cfg['seed']:
                    raise ValueError('Mode/seed mismatch')
                if result['assistance'] is not (state == 'ON'):
                    raise ValueError('Assistance state mismatch')
                if result.get('missing_measurements'):
                    raise ValueError('Missing measurements')
                pair[state] = metrics(result)
                identities[relative] = actual_sha
            pairs.append(pair)
    groups = {mode: summarize([p for p in pairs if p['mode'] == mode])
              for mode in ('Automatic', 'Retrofit')}
    scenario_data = {theme: {mode: summarize([p for p in pairs if p['scenario'] == theme and p['mode'] == mode])
                    for mode in groups} for theme in SCENARIOS}
    result = {'catalogue_sha256': sha, 'runs': 240, 'pairs': 120, 'groups': groups,
              'scenarios': scenario_data, 'paired_runs': pairs, 'results_sha256': identities}
    destination.mkdir(parents=True, exist_ok=True)
    (destination/'paired_results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    boundaries = ('Counts mean runs with at least one measured contact, not sensor callbacks or injured persons. '
        'Each mode has 60 OFF/ON pairs; each scenario/mode has 10 heterogeneous configurations. '
        'Positive brake advance means ON commanded braking earlier when both runs have a command. '
        'These command times are not communication latency or achieved deceleration. '
        'There is one seed per configuration, a 20-second window, and no population-level efficacy estimate. '
        'The modes use different response assumptions and must not be pooled as interchangeable drivers. '
        'The recorded unnecessary_braking flag merely labels wait/retreat configurations; it is not a measured false-alarm rate. '
        'Visual acceptance was by Codex models and is not independent human validation. '
        'Full-window counts include the lead corridor-stop guard, which can initiate braking beyond the crossing. '
        'Before/after-guard classifications use the recorded immediately preceding contact sample; categories can overlap within a run and do not prove causation.')
    report = '# CrossGuard Town10HD robustness analysis\n\n'+table(groups)+'\n'+interpretation(groups)+'\n\n'+guard_table(groups)+'\n'+boundaries+'\n\n'
    report += '## Cases requiring follow-up\n\n'
    for pair in pairs:
        if pair['ON']['pedestrian_contact'] or (pair['ON']['B_C_contact'] and not pair['OFF']['B_C_contact']):
            report += f"- {pair['test']} / {pair['mode']}: assisted pedestrian contact={pair['ON']['pedestrian_contact']}; newly present B/C contact={pair['ON']['B_C_contact'] and not pair['OFF']['B_C_contact']}.\n"
    report += '\n'
    for theme, name in SCENARIOS.items():
        scenario_folder = destination/f'Scenario_{theme[1:]}_{name}'
        scenario_folder.mkdir(exist_ok=True)
        selected = [p for p in pairs if p['scenario'] == theme]
        text = f'# Scenario {theme[1:]} — {name.replace("_", " ")}\n\n'+table(scenario_data[theme])+'\n'+interpretation(scenario_data[theme])+'\n\n'+guard_table(scenario_data[theme])+'\n'+IMPLICATIONS[theme]+'\n\n'
        text += '| Configuration | Mode | Behavior | Weather/daylight | Pedestrian OFF → ON | A/B OFF → ON | B/C OFF → ON |\n|---|---|---|---|---|---|---|\n'
        for pair in selected:
            counts = [f"{int(pair['OFF'][k])} → {int(pair['ON'][k])}" for k in OUTCOMES[:3]]
            text += f"| {pair['test']} | {pair['mode']} | {pair['behavior']} | {pair['weather']}/{pair['daylight']} | " + ' | '.join(counts)+' |\n'
        text += '\n'+boundaries+'\n\nOriginal frozen evidence folders retain Theme names to preserve seals; this analysis uses Scenario folders.\n'
        subset_details = ''
        if theme == 'T06':
            for behavior in ('wait', 'retreat'):
                subset = {mode: summarize([p for p in selected if p['mode']==mode and p['behavior']==behavior]) for mode in groups}
                detail = f'\n## {behavior.title()} subset\n\n'+table(subset)+'\n'+guard_table(subset)
                text += detail
                subset_details += detail+'\n'
        (scenario_folder/'ANALYSIS.md').write_text(text, encoding='utf-8')
        (scenario_folder/'evidence_index.json').write_text(json.dumps([
            {'test':c['test'],'original_folder':str(root/c['relative_path'])} for c in configs if c['theme']==theme], indent=2), encoding='utf-8')
        report += f'## Scenario {theme[1:]} — {name.replace("_", " ")}\n\n'+table(scenario_data[theme])+'\n'+interpretation(scenario_data[theme])+'\n\n'+guard_table(scenario_data[theme])+'\n'+IMPLICATIONS[theme]+'\n\n'+subset_details
    report += '## Next-stage priorities\n\nAnalyze simultaneous pedestrian/follower tradeoffs; validate intent-aware warning behavior; independently vary weather and illumination; replicate configurations with new prespecified seeds; inspect transport/observation telemetry; calibrate driver and ranging assumptions; then design a permission-dependent university pilot. No new runs or hardware tests are launched by this analysis.\n'
    (destination/'RESULTS_ANALYSIS.md').write_text(report, encoding='utf-8')
    if plots:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        for theme, name in SCENARIOS.items():
            fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
            for ax, mode in zip(axes, groups):
                data = scenario_data[theme][mode]['outcomes']; xs = range(3)
                ax.bar([x-.18 for x in xs],[data[k]['OFF'] for k in OUTCOMES[:3]],width=.36,label='OFF')
                ax.bar([x+.18 for x in xs],[data[k]['ON'] for k in OUTCOMES[:3]],width=.36,label='ON')
                ax.set_xticks(list(xs),['Pedestrian','A/B','B/C']); ax.set_ylim(0,10.8)
                ax.set_title(mode+' (10 paired configurations)'); ax.legend();ax.set_ylabel('Runs with measured contact')
            fig.suptitle(f'Scenario {theme[1:]}: {name.replace("_", " ")}');fig.tight_layout()
            fig.savefig(destination/f'Scenario_{theme[1:]}_{name}'/'contact_outcomes.png',dpi=160);plt.close(fig)
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--no-plots',action='store_true')
    args=parser.parse_args();result=analyze(args.root,args.output,not args.no_plots)
    print(json.dumps({'runs':result['runs'],'pairs':result['pairs'],'groups':result['groups']},indent=2))
