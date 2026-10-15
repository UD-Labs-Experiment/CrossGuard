"""Detached collection of 28 fresh validation windows after an authorized repair.

Reuses the production supervisor's pinned bridge, read-only weather guard,
physical evidence verification, process ownership and safe-stop implementation.
This collector never accepts scenes, seals gates, or launches robustness.
"""
import argparse
import os
from pathlib import Path

import psutil

from town10_continuation import MODES, STATES, digest, read, verified_catalogue
from town10_unattended_robustness import (
    RobustnessSupervisor, atomic_json, utc, verify_completed_evidence,
)


class RevalidationSupervisor(RobustnessSupervisor):
    def __init__(self, args):
        super().__init__(args)
        self.directory = self.root / 'Reports/Revalidation_Unattended'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.directory / 'checkpoint.json'
        self.progress_path = self.directory / 'progress.json'
        self.lock_path = self.directory / 'supervisor.lock.json'
        self.weather_log_path = self.directory / 'read_only_weather_observations.jsonl'

    def catalogue(self):
        if digest(self.root / 'frozen_catalogue.json') != self.args.catalogue_sha256:
            raise RuntimeError('Pinned repaired catalogue hash mismatch')
        catalogue = verified_catalogue(self.root)
        for source, expected in catalogue.get('evaluation_source_hashes', {}).items():
            if digest(self.workspace / source) != expected:
                raise RuntimeError('Frozen evaluation source changed: ' + source)
        tests = [c for c in catalogue['tests'] if c['test'].startswith('Validation')]
        if len(tests) != 7 or len({c['test'] for c in tests}) != 7:
            raise RuntimeError('Expected seven distinct validation configurations / 28 windows')
        return tests

    def pending(self, tests):
        queue = []
        for cfg in tests:
            for mode in MODES:
                for state in STATES:
                    folder = self.root / cfg['relative_path'] / mode / f'Assistance_{state}'
                    complete, reason = verify_completed_evidence(
                        folder, cfg, mode, state, self.weather_log_path)
                    if complete:
                        if not self.args.resume:
                            raise RuntimeError('Existing complete attempt requires explicit --resume')
                        self.completed_runs_ledger.append({
                            'test': cfg['test'], 'theme': cfg['theme'], 'mode': mode,
                            'assistance': state, 'status': 'COMPLETED',
                            'relative_path': str(folder.relative_to(self.root)),
                            'verified_resume_utc': utc(),
                        })
                    elif reason == 'Not started':
                        queue.append((cfg, mode, state))
                    else:
                        raise RuntimeError(f'Retained failed/partial attempt blocks retry: {folder}: {reason}')
        return queue

    def save_progress(self, status, error=None):
        current = None if self.active_test is None else {
            'test': self.active_test, 'mode': self.active_mode,
            'assistance': self.active_state, 'simulation_s': self.active_sim_s,
        }
        payload = {
            'phase': 'Validation', 'status': status, 'updated_utc': utc(),
            'planned_runs': 28, 'completed_runs': len(self.completed_runs_ledger),
            'failed_attempts': self.failed_attempts, 'current': current,
            'runs': self.completed_runs_ledger, 'supervisor_pid': os.getpid(),
            'supervisor_created': psutil.Process().create_time(),
            'bridge_pid': self.args.bridge_pid,
            'bridge_created': self.args.bridge_created,
            'catalogue_sha256': self.args.catalogue_sha256,
            'independently_accepted_pairs': 0, 'robustness_started': False,
            'note': 'Physical collection only; independent RGB review and media gates remain required.',
        }
        if error is not None:
            payload['error'] = str(error)
        atomic_json(self.progress_path, payload)
        atomic_json(self.checkpoint_path, payload)

    def run(self):
        self.acquire_lock()
        try:
            tests = self.catalogue()
            self.check_bridge_alive()
            if self.get_bridge_status().get('state') == 'running':
                raise RuntimeError('Existing live simulation preserved; competing execution refused')
            queue = self.pending(tests)
            self.save_progress('READY')
            if self.args.dry_run:
                self.save_progress('DRY_RUN_VERIFIED')
                return
            for cfg, mode, state in queue:
                self.catalogue()
                self.execute_window(cfg, mode, state)
            # Assess after physical collection so offline media work does not delay
            # the next live window. Numeric failure remains a failure, never approval.
            for cfg in tests:
                self.catalogue()
                self.assess_configuration(cfg)
            if len(self.completed_runs_ledger) != 28:
                raise RuntimeError('Fresh validation collection incomplete')
            self.save_progress('AWAITING_CODEX_INDEPENDENT_REVIEW')
        except Exception as exc:
            self.failed_attempts += 1
            self.log('REVALIDATION FAILED: ' + str(exc))
            self.safe_stop()
            self.terminate_owned()
            self.save_progress('FAILED_SAFE_STOP', error=exc)
            raise
        finally:
            self.release_lock()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--catalogue-sha256', required=True)
    parser.add_argument('--bridge-url', default='http://127.0.0.1:8007')
    parser.add_argument('--bridge-pid', type=int, required=True)
    parser.add_argument('--bridge-created', type=float, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    RevalidationSupervisor(parser.parse_args()).run()


if __name__ == '__main__':
    main()
