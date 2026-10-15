"""Read-only native geometry inventory. Never ticks or changes CARLA."""
import json,math
from pathlib import Path
import carla
from crossguard.simulation.city_world import extract_crosswalk_polygons,validate_and_find_crosswalk
from crossguard.paths import STUDY_ROOT

def angle(a,b):return abs((a-b+180)%360-180)
def data(w):return {'road_id':w.road_id,'lane_id':w.lane_id,'s':w.s,'location':[w.transform.location.x,w.transform.location.y,w.transform.location.z],'yaw':w.transform.rotation.yaw,'lane_width':w.lane_width}
def discover(m):
    if 'Town10HD' not in m.name:raise ValueError('Town10HD required')
    out=[];wps=m.generate_waypoints(2.)
    for idx,poly in enumerate(extract_crosswalk_polygons(m.get_crosswalks())):
        center=carla.Location(sum(p.x for p in poly)/len(poly),sum(p.y for p in poly)/len(poly),0);best=[]
        for bus in wps:
            left=bus.get_left_lane()
            if bus.is_junction or not left or left.lane_type!=carla.LaneType.Driving or angle(left.transform.rotation.yaw,bus.transform.rotation.yaw)>2:continue
            f=bus.transform.get_forward_vector();right=bus.transform.get_right_vector();d=(center-bus.transform.location).dot(f);lat=(center-bus.transform.location).dot(right)
            if not 8<=d<=12 or not -8<=lat<=1:continue
            try:cw=validate_and_find_crosswalk(m,bus)
            except RuntimeError:continue
            if math.dist(cw['center'][:2],[center.x,center.y])>.5:continue
            for setback in (50.,45.,40.,35.,30.):
                starts=left.previous(setback)
                if len(starts)!=1:continue
                start=starts[0];o=start.transform.location;axis=start.transform.get_forward_vector();length=(center-o).dot(axis);errors=[];bad=False
                for distance in range(-27,int(length+13),3):
                    options=[start] if distance==0 else start.previous(-distance) if distance<0 else start.next(distance)
                    if not options:bad=True;break
                    w=min(options,key=lambda w:angle(w.transform.rotation.yaw,start.transform.rotation.yaw));loc=w.transform.location
                    offset=abs((loc.x-o.x)*(-axis.y)+(loc.y-o.y)*axis.x);yaw=angle(w.transform.rotation.yaw,start.transform.rotation.yaw);errors.append((offset,yaw))
                    if offset>.6 or yaw>12:bad=True;break
                if bad:continue
                ped=center+right*(max((p-center).dot(right) for p in poly)-.5)
                best.append({'candidate_id':f'Town10HD_crosswalk_{idx:02}','crosswalk':cw,'car_waypoint':data(start),'bus_waypoint':data(bus),'approach_length_m':length,'route_max_offset_m':max(e[0] for e in errors),'route_max_yaw_error_deg':max(e[1] for e in errors),'route_range_relative_to_A_spawn_m':[-27,length+12],'pedestrian_start':[ped.x,ped.y,ped.z],'bus_to_crosswalk_m':d,'setback_m':setback})
                break
        if best:out.append(min(best,key=lambda c:(-c['setback_m'],abs(c['bus_to_crosswalk_m']-9))))
    return {'map':m.name,'read_only_no_ticks':True,'constraints':{'upstream_m':27,'route_offset_m':.6,'route_tangent_yaw_deg':12,'beyond_crosswalk_m':12},'candidates':out}
if __name__=='__main__':
    c=carla.Client('127.0.0.1',2000);c.set_timeout(30)
    result=discover(c.get_world().get_map())
    (STUDY_ROOT/'batch_site_inventory.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))
