"""Read-only native lane validation: never tick, move or spawn an actor."""
import copy,json,math
from pathlib import Path
import carla
from crossguard.paths import STUDY_ROOT
from crossguard.simulation.placement import solve_projected_distance

def inspect(map_,cfg,offset):
    spec=cfg['site']['car_waypoint'];origin=map_.get_waypoint_xodr(spec['road_id'],spec['lane_id'],spec['s'])
    if offset:origin=origin.next(offset)[0]
    forward=origin.transform.get_forward_vector();right=origin.transform.get_right_vector()
    anchor=origin.transform.location;arc=0.;records=[]
    for name in ('follower_b','follower_c'):
        gap=cfg['followers_automatic'][name]['gap_m'];target=4.791779518+gap;arc+=target
        native=origin.previous(arc)[0]
        project=lambda wp:(anchor-wp.transform.location).dot(forward)
        if abs(project(native)-target)>.25:native,unused=solve_projected_distance(origin.previous,project,target,arc)
        yaw=abs((native.transform.rotation.yaw-origin.transform.rotation.yaw+180)%360-180)
        lateral=abs((native.transform.location-origin.transform.location).dot(right))
        records.append({'vehicle':name,'requested_gap_m':gap,'predicted_gap_m':project(native)-4.791779518,
          'yaw_difference_deg':yaw,'lateral_offset_m':lateral,'within_corridor':yaw<=12 and lateral<=.6 and abs(project(native)-target)<=.15})
        anchor=native.transform.location
    return origin,records

if __name__=='__main__':
    path=STUDY_ROOT/'study_48_plan.json';plan=json.loads(path.read_text());client=carla.Client('127.0.0.1',2000);client.set_timeout(10)
    map_=client.get_world().get_map()
    if not map_.name.endswith('Town10HD'):raise RuntimeError('Town10HD required')
    audit=[]
    for cfg in plan['tests']:
        if 'site' not in cfg:continue
        completed=any((STUDY_ROOT/cfg['relative_path']/m/s/'results.json').exists() and
            json.loads((STUDY_ROOT/cfg['relative_path']/m/s/'results.json').read_text()).get('technical_failure') is False
            for m in ('Automatic','Retrofit') for s in ('OFF','ON'))
        origin,records=inspect(map_,cfg,0.)
        original=copy.deepcopy(cfg['site']);offset=0.
        if not all(r['within_corridor'] for r in records):
            if completed:raise RuntimeError('Cannot amend a location with completed matched runs')
            # Smallest deterministic 1m forward native-lane offset that accommodates
            # the unchanged group; based solely on geometry, never contact outcomes.
            for offset in range(1,11):
                origin,records=inspect(map_,cfg,float(offset))
                if all(r['within_corridor'] for r in records):break
            else:raise RuntimeError('No valid native group placement')
            cfg.setdefault('original_site_before_placement_amendment',original)
            wp=cfg['site']['car_waypoint'];wp.update(road_id=origin.road_id,lane_id=origin.lane_id,s=origin.s,
                location=[origin.transform.location.x,origin.transform.location.y,origin.transform.location.z],yaw=origin.transform.rotation.yaw)
            accumulated=offset+cfg.get('placement_amendment',{}).get('forward_native_spawn_offset_m',0.)
            cfg['site']['approach_length_m']-=offset
            cfg['placement_amendment']={'forward_native_spawn_offset_m':accumulated,'reason':'Unchanged sampled group must fit native lane corridor and preflight gap error <=0.15m before existing settled 0.25m gate; actor creation occurs once at amended poses','before_any_complete_run':True}
            (STUDY_ROOT/cfg['relative_path']/'test_config.json').write_text(json.dumps(cfg,indent=2))
        audit.append({'test':cfg['test'],'offset_m':offset,'followers':records})
    path.write_text(json.dumps(plan,indent=2))
    (STUDY_ROOT/'native_placement_preflight.json').write_text(json.dumps({'read_only_no_ticks':True,'nominal_mesh_half_length_m':2.395889759,'settled_gap_gate_unchanged_m':.25,'tests':audit},indent=2))
    print(json.dumps(audit,indent=2))
