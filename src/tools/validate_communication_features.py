"""Sole-owner live CARLA communication/camera validation and exploratory trials.

Run only when the web bridge and other CARLA clients are stopped. This script
owns the synchronous tick and reloads Town10HD between trial conditions.
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import time

import numpy as np


def score_trial(metrics, group_rows, rows):
    """Describe one trial without interpreting one sample as a safety benefit."""
    groups = {}
    for name in ('follower_b', 'follower_c'):
        samples = [(r['time_s'], r['vehicles'][name]) for r in group_rows
                   if name in r.get('vehicles', {})]
        groups[name] = {
            'minimum_gap_m': min((s['gap_m'] for _,s in samples),default=None),
            'first_brake_s': next((t for t,s in samples if s.get('brake',0)>.05),None),
            'corridor_guard': any(s.get('corridor_guard',False) for _,s in samples),
            'maximum_speed_kmh': max((s.get('speed_kmh',0) for _,s in samples),default=None),
        }
    return {
        'finished':metrics.get('finished',False), 'end_reason':metrics.get('end_reason'),
        'sim_time_s':metrics.get('sim_time_s'),
        'timed_out':metrics.get('end_reason')=='watchdog_timeout',
        'pedestrian_collision':metrics.get('pedestrian_collision',False),
        'secondary_collision':metrics.get('secondary_collision',False),
        'hero_other_contacts':metrics.get('other_collisions',[]),
        'follower_contacts':metrics.get('group_collisions',[]),
        'hero_corridor_guard':metrics.get('corridor_guard',False),
        'hero_first_brake_s':metrics.get('braking_started_s'),
        'hero_minimum_pedestrian_distance_m':metrics.get('minimum_distance_m'),
        'followers':groups,
    }


def ensure_server():
    import carla
    client=carla.Client('127.0.0.1',2000)
    client.set_timeout(3.)
    deadline=time.monotonic()+90
    while True:
        try:
            client.get_server_version()
            return
        except RuntimeError:
            if time.monotonic()>deadline:
                raise
            time.sleep(1)


def create_world(args, **kwargs):
    from crossguard.simulation.city_world import CityWorld
    return CityWorld(vehicles=0,walkers=0,seed=args.seed,
        perception_mode='rgb',device='gpu',resolution=(args.width,args.height),
        communication_profile='hybrid',
        progress=lambda message:print(message,flush=True),**kwargs)


def capture(world, frames, destination, name):
    from PIL import Image
    frame=frames[name]
    assert frame.ndim==3 and frame.shape[2]==3, frame.shape
    assert float(np.std(frame))>3., f'{name}: blank/constant display frame'
    Image.fromarray(frame).save(destination/f'{name}.jpg')
    stream=world.views[name]
    camera=np.asarray(stream.last_transform)
    actor=world.car if name=='top' else world.followers['follower_b' if name=='driver_b' else 'follower_c']
    actor_transform=np.asarray(actor.get_transform().get_matrix())
    relative=np.linalg.inv(actor_transform)@camera
    return dict(view=name,frame_id=stream.last_frame,sim_time_s=world.time,
                camera_world_matrix=camera.tolist(),actor_world_matrix=actor_transform.tolist(),
                relative_matrix=relative.tolist(),image=str(destination/f'{name}.jpg'))


def smoke(args, destination):
    world=None
    try:
        world=create_world(args,group_mode='cooperative')
        begin=time.monotonic()
        captures=[]
        top_initial=None
        switched_b=switched_c=switched_top=False
        while world.time<8. and not world.finished:
            frames=world.tick()
            if world.time>=1. and top_initial is None:
                top_initial=capture(world,frames,destination,'top')
                # Distinct path for the first frame, retained for movement evidence.
                (destination/'top_initial.jpg').write_bytes((destination/'top.jpg').read_bytes())
                top_initial['image']=str(destination/'top_initial.jpg')
                captures.append(top_initial)
            if world.time>=2. and not switched_b:
                world.set_view('driver_b'); switched_b=True
            elif world.time>=3.8 and switched_b and not switched_c:
                captures.append(capture(world,frames,destination,'driver_b'))
                world.set_view('driver_c'); switched_c=True
            elif world.time>=5.8 and switched_c and not switched_top:
                captures.append(capture(world,frames,destination,'driver_c'))
                world.set_view('top'); switched_top=True
            if time.monotonic()-begin>args.wall_limit:
                raise RuntimeError('Smoke exceeded wall-clock limit')
        captures.append(capture(world,frames,destination,'top'))
        top_final=captures[-1]
        movement=float(np.linalg.norm(np.array(top_final['camera_world_matrix'])[:3,3]-
                                      np.array(top_initial['camera_world_matrix'])[:3,3]))
        relative_error=float(np.max(np.abs(np.array(top_final['relative_matrix'])-
                                           np.array(top_initial['relative_matrix']))))
        assert movement>2., f'Top camera did not move sufficiently: {movement}'
        assert relative_error<.1, f'Top hero-relative transform drift: {relative_error}'
        metrics=world.metrics()
        receipts={name:sum(e['event']=='received' and e['receiver_id']==name
                          for e in world.network.events)
                  for name in ('hero_car','follower_b','follower_c')}
        assert all(receipts.values()), receipts
        assert {c['id'] for c in metrics['communication']['channels']}=={'radio','cellular','optical'}
        assert metrics['communication']['braking_events'], 'No actual brake events recorded'
        checkpoint_metrics=metrics
        world.save('communication_feature_smoke_checkpoint')
        checkpoint_artifacts=str(world.trial_path)
        world.configure_communication(optical_enabled=False)
        before=len(world.network.events)
        deadline=world.time+8.
        while world.time<deadline and not world.finished:
            frames=world.tick()
            if time.monotonic()-begin>args.wall_limit:
                raise RuntimeError('Optical-off reset smoke exceeded wall-clock limit')
        disabled=[e for e in world.network.events[before:] if e['channel']=='optical']
        assert any(e.get('reason')=='optical_disabled' for e in disabled), disabled[-5:]
        world.save('optical_disabled_checkpoint')
        from PIL import Image
        Image.fromarray(frames['top']).save(destination/'optical_disabled_top.jpg')
        return dict(status='passed',kind='RGB/GPU live integration smoke; not safety validation',
            receipts=receipts,camera_captures=captures,top_movement_m=movement,
            top_relative_transform_max_error=relative_error,
            optical_disabled_events=len(disabled),metrics=checkpoint_metrics,
            optical_disabled_metrics=world.metrics(),
            trial_artifacts=checkpoint_artifacts,optical_disabled_trial_artifacts=str(world.trial_path),
            wall_seconds=time.monotonic()-begin)
    finally:
        if world is not None:world.close()


def phone_smoke(args,destination):
    world=None
    try:
        world=create_world(args,group_mode='cooperative',receiver_mode='retrofit',phone_available=False)
        begin=time.monotonic()
        while world.time<8. and not world.finished:
            world.tick()
            if time.monotonic()-begin>args.wall_limit:
                raise RuntimeError('Phone unavailable smoke exceeded wall-clock limit')
        metrics=world.metrics()
        phone=metrics['communication']['phone_advisories']
        events=phone.get('events',[]) if isinstance(phone,dict) else phone
        assert any(e.get('event')=='unavailable' for e in events), events
        assert not any(e.get('event')=='reacted' for e in world.phone_model.events),world.phone_model.events[-20:]
        world.save('phone_unavailable_checkpoint')
        return dict(status='passed',kind='Unavailable active-phone advisory live smoke',
                    phone_advisories=phone,metrics=metrics,trial_artifacts=str(world.trial_path))
    finally:
        if world is not None:world.close()


def comparison(args,destination):
    outcomes=[]
    for label,enabled,group in [('off',False,'cooperative'),('lead-only',True,'lead-only'),
                                ('cooperative',True,'cooperative')]:
        world=None
        try:
            world=create_world(args,enabled=enabled,group_mode=group)
            begin=time.monotonic()
            while not world.finished:
                frames=world.tick()
                if time.monotonic()-begin>args.wall_limit:
                    raise RuntimeError(f'{label} exceeded wall-clock limit')
            metrics=world.metrics()
            world.save('exploratory_matched_'+label)
            from PIL import Image
            Image.fromarray(frames['top']).save(destination/(label+'_top.jpg'))
            outcomes.append(dict(condition=label,seed=args.seed,enabled=enabled,
                group_mode=group,score=score_trial(metrics,world.group_rows,world.rows),
                metrics=metrics,trial_artifacts=str(world.trial_path),
                wall_seconds=time.monotonic()-begin))
            (destination/'comparison_partial.json').write_text(json.dumps(outcomes,indent=2))
        finally:
            if world is not None:world.close()
    return dict(kind='Exploratory matched-seed single trial per condition; not statistical safety evidence',
        controls=['fresh Town10HD world per condition','same seed','same RGB detector and resolution',
                  'same 60Hz physics','same zero ambient actors','same modeled channel configuration'],
        caveat='Wall-time asynchronous RGB completion can vary across trials; fixed seed does not guarantee identical perception scheduling.',
        trials=outcomes)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=('smoke','comparison'),default='smoke')
    parser.add_argument('--seed',type=int,default=7)
    parser.add_argument('--width',type=int,default=640)
    parser.add_argument('--height',type=int,default=360)
    parser.add_argument('--wall-limit',type=float,default=900.)
    args=parser.parse_args()
    if args.width<160 or args.height<90 or args.wall_limit<=0:
        parser.error('Invalid capture size or wall limit')
    destination=Path(__file__).resolve().parents[1]/'results'/'communication_validation'/datetime.now().strftime('%Y%m%d_%H%M%S')
    destination.mkdir(parents=True,exist_ok=True)
    envelope=dict(suite=args.suite,display_resolution=[args.width,args.height],
        limitations=['simulation only; no physical protocol validation','zero ambient actors',
                     'ideal local follower ranging','single-seed exploratory evidence',
                     '640x360 default display capture; observation cameras remain their configured resolution'])
    try:
        ensure_server()
        if args.suite=='smoke':
            envelope['communication_camera_smoke']=smoke(args,destination)
            (destination/'summary.json').write_text(json.dumps(envelope,indent=2))
            envelope['phone_unavailable_smoke']=phone_smoke(args,destination)
        else:
            envelope['comparison']=comparison(args,destination)
        envelope['status']='passed'
    except Exception as exc:
        envelope.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (destination/'summary.json').write_text(json.dumps(envelope,indent=2))
        print(json.dumps({'summary':str(destination/'summary.json'),'status':envelope.get('status')}),flush=True)


if __name__=='__main__':main()
