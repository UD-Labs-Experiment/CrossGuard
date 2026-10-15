"""Read-only runtime/map inventory. Never ticks or changes CARLA."""
import json
from pathlib import Path
import carla

root = Path(__file__).resolve().parents[1]
client = carla.Client('127.0.0.1', 2000)
client.set_timeout(20)
world = client.get_world()
roadmap = world.get_map()
points = roadmap.get_crosswalks()
records = []
for p in points:
    wp = roadmap.get_waypoint(p)
    records.append({'x':p.x,'y':p.y,'z':p.z,
                    'road_id':wp.road_id if wp else None,
                    'lane_id':wp.lane_id if wp else None,
                    's':wp.s if wp else None})
result = {'map':roadmap.name,'synchronous_mode':world.get_settings().synchronous_mode,
          'actor_count':len(world.get_actors()),'crosswalk_vertices':records,
          'client_version':client.get_client_version(),'server_version':client.get_server_version()}
path = root/'reports'/'RUNTIME_PROBE.json'
path.write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps({**{k:v for k,v in result.items() if k!='crosswalk_vertices'},
                  'crosswalk_vertex_count':len(records),'report':str(path)},indent=2))
