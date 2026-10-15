"""Sole-owner CARLA RGB/GPU smoke check; not matched safety evaluation."""
import json
import math
import time
from pathlib import Path
import carla
from PIL import Image
from crossguard.simulation.city_world import CityWorld


def main():
    client = carla.Client('127.0.0.1', 2000)
    client.set_timeout(3)
    deadline = time.monotonic() + 90
    while True:
        try:
            client.get_server_version()
            break
        except RuntimeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    world = None
    try:
        world = CityWorld(vehicles=0, walkers=0, seed=7, group_mode='cooperative',
                          perception_mode='rgb', device='gpu')
        begin = time.monotonic()
        while world.time < 8 and not world.finished:
            frames = world.tick()
            if time.monotonic() - begin > 180:
                raise RuntimeError('Group smoke exceeded wall-clock limit')
        metrics = world.metrics()
        assert set(metrics['group_vehicles']) == {'follower_b','follower_c'}
        assert metrics['group_mode'] == 'cooperative'
        for row in metrics['group_vehicles'].values():
            assert math.isfinite(row['gap_m']) and math.isfinite(row['speed_kmh'])
            assert not row['corridor_guard'], row
        for name in ('follower_b', 'follower_c'):
            assert max(r['vehicles'][name]['speed_kmh'] for r in world.group_rows) > 10, name
        receipts = {name: sum(e['event'] == 'received' and e['receiver_id'] == name
                             for e in world.network.events)
                    for name in ('hero_car','follower_b','follower_c')}
        assert all(v > 0 for v in receipts.values()), receipts
        world.save('group_smoke_checkpoint')
        Image.fromarray(frames['top']).save(world.output / 'group_top.jpg')
        summary = {'status':'passed', 'kind':'8-second RGB/GPU integration smoke, not safety evaluation',
                   'receipts':receipts, 'metrics':metrics, 'wall_seconds':time.monotonic()-begin,
                   'limitations':['zero ambient traffic', 'ideal follower ranging',
                                  'autonomous followers only', 'no optical or physical communication',
                                  'trial checkpoint does not establish route completion or safety benefit']}
        (world.output / 'group_smoke.json').write_text(json.dumps(summary,indent=2))
        print(json.dumps({'summary':str(world.output/'group_smoke.json'),
                          'receipts':receipts,'metrics':metrics['group_vehicles']}),flush=True)
    finally:
        if world is not None:
            world.close()


if __name__ == '__main__':
    main()
