"""Check local retrofit braking through the sole-owner bridge; never ticks CARLA."""
import argparse
import asyncio
import json
from pathlib import Path
import urllib.request
import websockets


def request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


async def validate(base):
    if request(base, '/api/status')['state'] != 'stopped':
        raise RuntimeError('Stop the bridge before validation; this starts a fresh test trial.')
    for action, params in [('system', {'enabled': False}), ('receiver', {'mode': 'retrofit'}),
                           ('following_gap', {'gap_m': 3.})]:
        request(base, '/api/command', {'action': action, 'params': params})
    destination = Path(__file__).resolve().parents[1] / 'results' / 'retrofit_visual_validation.json'
    samples, seen, responded = [], None, None
    start = None
    try:
        async with websockets.connect(base.replace('http', 'ws', 1) + '/ws', max_size=12_000_000) as connection:
            start = asyncio.create_task(asyncio.to_thread(request, base, '/api/command', {'action': 'start'}))
            while True:
                packet = json.loads(await asyncio.wait_for(connection.recv(), timeout=45))
                if packet.get('type') != 'frame':
                    if packet.get('status', {}).get('state') == 'error':
                        raise RuntimeError(packet['status']['error'])
                    continue
                metrics = packet['metrics']
                for event in packet.get('events', []):
                    if event.get('event') == 'local_hazard_seen': seen = event
                    elif event.get('event') == 'local_driver_response': responded = event
                samples.append({k: metrics.get(k) for k in ('sim_time_s', 'speed_kmh', 'brake', 'reason',
                    'active_sources', 'pedestrian_collision', 'corridor_guard', 'source_detection_counts')})
                if ((responded is not None and metrics['speed_kmh'] < 1.)
                        or metrics['sim_time_s'] >= 13. or metrics.get('finished')):
                    break
            await start
    finally:
        if start is not None: await start
        await asyncio.to_thread(request, base, '/api/command', {'action': 'pause'})
        destination.write_text(json.dumps(dict(seen=seen, response=responded, samples=samples), indent=2))
    assert seen is not None, 'No fresh local crossing sighting recorded; inspect RGB perception evidence'
    assert responded is not None, 'No local driver response recorded'
    assert responded['at_s'] - seen['at_s'] >= .99, 'Driver delay was bypassed'
    assert any(row['brake'] == 1. and 'locally seen' in row['reason'] for row in samples)
    assert not any(row['corridor_guard'] for row in samples), 'Route guard must not account for the observed braking'
    print(json.dumps(dict(status='passed', seen_at_s=seen['at_s'], response_at_s=responded['at_s'],
        final_speed_kmh=samples[-1]['speed_kmh'], evidence=str(destination)), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8003')
    asyncio.run(validate(parser.parse_args().url.rstrip('/')))
