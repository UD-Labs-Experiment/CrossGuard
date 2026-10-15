"""One deterministic entry point for execution; no model calls or agent workers."""
import argparse
import json
import os
from pathlib import Path

import psutil
import requests

from town10_continuation import digest, read, verified_catalogue
from town10_unattended_robustness import (
    RobustnessSupervisor, check_readiness_gates, same_process,
    verify_completed_evidence, atomic_json, utc,
)
from town10_revalidation import RevalidationSupervisor


def recorded_counts(root):
    """Read durable evidence, never a failed controller's zeroed counter."""
    cat = read(root / 'frozen_catalogue.json')
    result = {'Validation': 0, 'Robustness': 0}
    for cfg in cat['tests']:
        phase = cfg['test'].split('_')[0]
        for mode in ('Automatic', 'Retrofit'):
            for state in ('OFF', 'ON'):
                folder = root / cfg['relative_path'] / mode / f'Assistance_{state}'
                # A physical-recorded count is separate from weather acceptance
                # and release gates, whose retained proofs live in other files.
                complete, _ = verify_completed_evidence(folder, cfg, mode, state)
                result[phase] += int(complete)
    return result


def preflight(args):
    root = args.root.resolve()
    if digest(root / 'frozen_catalogue.json') != args.catalogue_sha256:
        raise RuntimeError('Pinned catalogue mismatch')
    cat = verified_catalogue(root)
    workspace = Path(__file__).resolve().parents[2]
    guard = 'src/crossguard/simulation/weather_guard.py'
    if cat['source_hashes'].get(guard) != digest(workspace / guard):
        raise RuntimeError('Catalogue must explicitly freeze the stricter weather guard')
    if args.phase == 'Robustness':
        check_readiness_gates(root, args.catalogue_sha256)
    if not same_process(args.bridge_pid, args.bridge_created):
        raise RuntimeError('Pinned sole bridge is not alive')
    owners = {c.pid for c in psutil.net_connections('tcp')
              if c.status == 'LISTEN' and c.laddr.port == int(args.bridge_url.rsplit(':', 1)[1])}
    if owners != {args.bridge_pid}:
        raise RuntimeError('Sole bridge socket ownership mismatch')
    response = requests.get(args.bridge_url + '/api/status', timeout=10)
    response.raise_for_status()
    if response.json()['state'] != 'stopped':
        raise RuntimeError('Existing simulation must be stopped before autopilot starts')
    return cat


def acquire(path):
    if path.exists():
        old = read(path)
        if same_process(old['pid'], old['created']):
            raise RuntimeError('Another autopilot owns execution; duplicate refused')
        raise RuntimeError('Stale global owner lock requires explicit review; no automatic retry')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w') as output:
        json.dump({'pid': os.getpid(), 'created': psutil.Process().create_time()}, output)


def run(args):
    stop = args.root.resolve().parent / 'user_stop.json'
    if stop.exists() and not args.arm:
        raise RuntimeError('User stopped execution. Explicit --arm is required after readiness review')
    preflight(args)  # Refusal cannot erase progress, remove stop marker or create a worker.
    owner = args.root.resolve().parent / 'autopilot_owner.json'
    acquire(owner)
    try:
        if stop.exists():
            stopped = read(stop)
            atomic_json(args.root / 'Reports/Autopilot/user_stop_history.json', stopped)
            stop.unlink()
        atomic_json(args.root / 'Reports/Autopilot/owner.json', {
            'pid': os.getpid(), 'created': psutil.Process().create_time(),
            'phase': args.phase, 'updated_utc': utc(), 'model_calls': 0,
            'catalogue_sha256': args.catalogue_sha256,
        })
        supervisor = RevalidationSupervisor if args.phase == 'Validation' else RobustnessSupervisor
        supervisor(args).run()
    finally:
        if owner.exists() and read(owner).get('pid') == os.getpid():
            owner.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'preflight', 'run'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', choices=('Validation', 'Robustness'), default='Validation')
    parser.add_argument('--catalogue-sha256')
    parser.add_argument('--bridge-pid', type=int)
    parser.add_argument('--bridge-created', type=float)
    parser.add_argument('--bridge-url', default='http://127.0.0.1:8007')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--arm', action='store_true')
    args = parser.parse_args()
    args.dry_run = False
    if args.action == 'status':
        print(json.dumps({'root': str(args.root.resolve()), 'recorded': recorded_counts(args.root),
                          'user_stopped': (args.root.resolve().parent / 'user_stop.json').exists()}, indent=2))
    else:
        if not all(value is not None for value in (args.catalogue_sha256, args.bridge_pid, args.bridge_created)):
            parser.error('execution/preflight requires pinned catalogue and bridge identity')
        if args.action == 'preflight':
            preflight(args)
            print('READ_ONLY_PREFLIGHT_PASSED')
        else:
            run(args)


if __name__ == '__main__':
    main()
