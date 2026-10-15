"""Readable, offline delivery of already independently accepted validation evidence."""
import argparse
import hashlib
import json
from pathlib import Path

VEHICLES=('hero_car','follower_b','follower_c')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def number(value):
    return f'{value:.3f}' if isinstance(value,(int,float)) and not isinstance(value,bool) else 'unknown'


def delta(on,off):
    return on-off if isinstance(on,(int,float)) and isinstance(off,(int,float)) else None


def deliver(root):
    root=root.resolve();reports=root/'Reports'
    gate=read(reports/'validation_gate.json');seal=read(reports/'validation_package_gate.json')
    catalogue=read(root/'frozen_catalogue.json');data=read(reports/'configuration_comparisons.json')
    digest=hashlib.sha256((root/'frozen_catalogue.json').read_bytes()).hexdigest()
    if not (gate['passed'] and seal['passed'] and gate['catalogue_sha256']==seal['catalogue_sha256']==digest):
        raise RuntimeError('Both current independent gates must pass before delivery')
    if (gate['completed_runs'],gate['accepted_mode_pairs'],data['completed_runs'],data['accepted_pairs'])!=(28,14,28,14):
        raise RuntimeError('Validation delivery requires 28 complete windows and 14 accepted pairs')
    # Revalidate the seal against disk, not just its stored success boolean.
    for relative,expected in seal['evidence_sha256'].items():
        if hashlib.sha256((root/relative).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Sealed evidence changed: '+relative)
    video_count=sum(relative.endswith('.mp4') for relative in seal['evidence_sha256'])
    if video_count!=42:raise RuntimeError('Expected 28 run videos and 14 comparison videos')
    weather=read(reports/'accepted_rain_weather_verification.json')
    if not (weather['passed'] and weather['catalogue_sha256']==digest):
        raise RuntimeError('Accepted rain weather verification must match current catalogue')
    robust=[c for c in catalogue['tests'] if c['test'].startswith('Robustness')]
    if len(robust)!=60:
        raise RuntimeError('Expected 60 frozen robustness configurations')
    started=[str(p.relative_to(root)) for c in robust for name in
        ('results.json','requested_config.json','technical_failure.json')
        for p in (root/c['relative_path']).rglob(name)]
    if started:raise RuntimeError('This pre-robustness delivery requires no robustness recordings')
    lines=['# Validation efficiency and limitations','',
        'All 28 requested observation windows and 14 matched mode pairs passed independent validation. '
        'These selected validation cases establish execution and evidence readiness; they do not establish population safety effectiveness.', '',
        'Contact counts below are runs with the endpoint, not the number of contact sensor messages. '
        'OFF and ON use the same cases. Crossing results and nuisance cases are separated.', '',
        '| Behavior | Mode | Endpoint | OFF runs | ON runs | Paired ON minus OFF | Illustrative ON 95% Wilson interval |',
        '|---|---|---|---:|---:|---:|---|']
    for behavior,by_mode in data['endpoint_counts_by_behavior'].items():
        for mode,states in by_mode.items():
            for endpoint in ('pedestrian','A_B','B_C'):
                off,on=(states[s][endpoint] for s in ('OFF','ON'))
                n=on['known_complete_runs'];interval=on['illustrative_95_percent_wilson_interval']
                ci='unknown' if interval is None else f'{100*interval[0]:.1f}–{100*interval[1]:.1f}%'
                lines.append(f'| {behavior} | {mode} | {endpoint} | {off["events"]}/{off["known_complete_runs"]} | '
                    f'{on["events"]}/{n} | {on["events"]-off["events"]:+d}/{n} | {ci} |')
    lines+=['','Wilson intervals are illustrative binomial intervals. Purpose-selected cases, shared routes and small denominators '
        'do not support a representative road-user probability or a universal effectiveness percentage.','',
        '## Measured braking burden and passage delay','',
        'Values are ON minus OFF seconds, ordered A / B / C. Unknown passage means the vehicle did not have a measured conflict-plane passage '
        'in both 20-second windows; it does not mean zero delay. Non-guard brake command duration includes startup regulation and is not automatically unnecessary braking.', '',
        '| Configuration | Mode | Behavior | Added non-guard brake seconds A/B/C | Added observed passage seconds A/B/C |',
        '|---|---|---|---|---|']
    for test in data['tests']:
        cfg=test['configuration']
        for mode,pair in test['modes'].items():
            off,on=(pair['runs'][s] for s in ('OFF','ON'))
            burden=[delta(on['brake_metrics'][v]['non_guard_brake_duration_s'],off['brake_metrics'][v]['non_guard_brake_duration_s']) for v in VEHICLES]
            passage=[delta(pair['passage_times']['ON'][v],pair['passage_times']['OFF'][v]) for v in VEHICLES]
            lines.append(f'| {cfg["test"]} | {mode} | {cfg["pedestrian_behavior"]} | '+
                ' / '.join(map(number,burden))+' | '+' / '.join(map(number,passage))+' |')
    lines+=['','## Secondary event chronology and communication','',
        'The A marker and subsequent B/C command markers retain recorded reasons. These are observed commands, not proof of causation. '
        'First packet receipt and first brake commands may precede the hazard because of startup control; source-associated warning use is reported separately.']
    for test in data['tests']:
        cfg=test['configuration']
        for mode,pair in test['modes'].items():
            lines+=['',f'### {cfg["test"]} — {mode}','',
                f'[OFF/ON six-view video](../{cfg["relative_path"]}/{mode}/Comparison/{mode}_Assistance_OFF_vs_ON.mp4)','',
                '| State | A command s | B post-A command s | C post-A command s | Minimum A/B gap m | Minimum B/C gap m |',
                '|---|---:|---:|---:|---:|---:|']
            for state,run in pair['runs'].items():
                markers=run['post_A_brake_command_markers'];followers=markers['followers']
                lines.append(f'| {state} | {number(markers["A_command_marker_s"])} | '
                    f'{number(followers["follower_b"]["time_s"])} | {number(followers["follower_c"]["time_s"])} | '
                    f'{number(run["minimum_physical_gaps_m"]["A_B"])} | {number(run["minimum_physical_gaps_m"]["B_C"])} |')
                lines+=['',f'{state} B/C command reasons: '+json.dumps({v:followers[v].get('reason') for v in ('follower_b','follower_c')})+'.',
                    '',f'{state} endpoint and guard chronology: '+json.dumps(run['contact_context'])+'.']
            comm=pair['communication']
            if cfg['theme']=='T04' or comm['requested_channel_outages']:
                lines+=['','This validation case uses sequential outage windows; it is exploratory and does not isolate each channel over the same warning opportunity. '
                    'The frozen robustness cases use separate full-window failures. A usable warning requires fresh, relevant receipt associated with non-guard cooperative brake use. '
                    'The analytical deadline uses 6 m/s² braking, a 1 m buffer and a 1 s freshness ceiling; it is not a measured crash deadline.']
                for window in comm['outage_windows']:
                    lines+=['',f'Declared outage {window["declared_start_s"]}–{window["declared_end_s"]} s: {window["failed_channels"]}.']
                    for vehicle,recipient in window['recipients'].items():
                        value=recipient['alternate_usable_before_deadline']
                        lines.append(f'- {vehicle}: alternate usable before modeled deadline: {"unknown" if value is None else value}; '
                            f'deadline {number(recipient["modeled_stopping_deadline"]["deadline_s"])} s.')
    lines+=['','## Limits and retained evidence','',
        'Automatic OFF can avoid contact through RGB local control; absence of contacts does not demonstrate cooperative coordination. '
        'Retrofit crossing pedestrian and secondary outcomes remain separate. Waiting and retreating cases measure nuisance burden; '
        'later corridor-guard contacts remain in the full window and are flagged as potentially confounded, without claiming causation.','',
        'Rain changes rendered visibility and wet appearance; tire friction was not adjusted. Requested weather alone is insufficient: '
        'the first mismatched rain attempt is preserved under Failed/T02_Weather_Render_Attempt_01. The same frozen seed and parameters were repeated. '
        'Weather readbacks and actual RGB inspection corroborate the accepted repeat.','',
        '24 unchanged recordings were retained from Revision_02 with SHA256 provenance, and four rain windows were repeated in Revision_03. '
        'Retained copies are not additional physical runs. Rejected attempts and original worker transport failures remain separate.','',
        '[Complete structured comparisons and physical/contact measurements](configuration_comparisons.json) · '
        '[Validation gate](validation_gate.json) · [Media seal](validation_package_gate.json) · [Full study report](Study_Report.md)','']
    (reports/'Efficiency_Report.md').write_text('\n'.join(lines),encoding='utf-8')
    readiness={'status':'PRE_ROBUSTNESS_COMPLETE','validation_windows':28,'accepted_mode_pairs':14,
        'catalogue_sha256':digest,'fully_decoded_videos':video_count,'robustness_frozen_configurations':60,
        'robustness_planned_windows':240,'robustness_started':False,'confirmatory_started':False,
        'delivery_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'accepted_rain_weather_verification_sha256':hashlib.sha256((reports/'accepted_rain_weather_verification.json').read_bytes()).hexdigest(),
        'next_action':'Present validation package. Latest user request ends before robustness; do not launch in this turn.'}
    (reports/'pre_robustness_readiness.json').write_text(json.dumps(readiness,indent=2),encoding='utf-8')
    return readiness


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    print(json.dumps(deliver(p.parse_args().root),indent=2))
