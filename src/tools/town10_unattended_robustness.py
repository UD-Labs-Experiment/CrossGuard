"""Detached, fail-closed supervisor for the frozen 60 Robustness configurations (240 runs).
Single bridge command queue only; never starts/restarts bridge or CARLA.
Read-only weather sampling with strict tolerance comparison; safe failure stop.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from town10_continuation import MODES, STATES, digest, read, verified_catalogue

# Tolerances and constants
WEATHER_TOLERANCE = 0.01
REQUIRED_TIMELINE_ROWS = 1200
REQUIRED_PICTURE_SETS = 201
REQUIRED_PICTURE_FILES = 1206
WALL_TIME_LIMIT_S = 1500
STALL_TIMEOUT_S = 180
MAX_STATUS_ERRORS = 3
TOTAL_PLANNED_RUNS = 240
CARLA_RPC_PORT = 2000
VIEWS = ('top', 'driver', 'person', 'opposite', 'driver_b', 'driver_c')


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f'.tmp.{os.getpid()}')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    # Windows readers/scanners can briefly deny replacement of an open file.
    # Retain the old checkpoint until the atomic replacement succeeds.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 7:
                raise
            time.sleep(min(.05 * 2 ** attempt, .4))


def same_process(pid, created):
    if pid is None or created is None:
        return False
    try:
        proc = psutil.Process(pid)
        return abs(proc.create_time() - created) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def verify_completed_evidence(run_folder, cfg, mode, state, weather_log_path=None):
    """
    Durable verification of a completed window.
    Returns (True, None) if completely and cleanly recorded,
    or (False, reason) if incomplete, partial, or failed.
    """
    results_path = run_folder / 'results.json'
    failure_path = run_folder / 'technical_failure.json'

    if failure_path.exists():
        return False, f'Retained technical failure found at {failure_path}'

    if not results_path.exists():
        # Check if any partial artifacts exist
        any_artifacts = list(run_folder.glob('*.json'))
        pictures_folder = run_folder / 'assets/pictures'
        if pictures_folder.exists() and any(pictures_folder.glob('*.jpg')):
            any_artifacts.append(pictures_folder)
        if any_artifacts:
            return False, f'Partial uncompleted artifacts found at {run_folder}'
        return False, 'Not started'

    try:
        results = read(results_path)
    except Exception as exc:
        return False, f'Unreadable results.json: {exc}'

    if results.get('technical_failure') is not False:
        return False, 'results.json technical_failure is not False'

    if results.get('terminal_reason') != 'observation_window_complete':
        return False, f"terminal_reason is '{results.get('terminal_reason')}', expected 'observation_window_complete'"

    # Mode: Automatic results mode is 'autonomous', Retrofit is 'retrofit'
    expected_result_mode = 'autonomous' if mode == 'Automatic' else 'retrofit'
    if results.get('mode') != expected_result_mode:
        return False, f"Mode mismatch: expected '{expected_result_mode}', got '{results.get('mode')}'"

    expected_bool = (state == 'ON')
    if results.get('assistance') != expected_bool:
        return False, f"Assistance mismatch: expected {expected_bool}, got {results.get('assistance')}"

    if results.get('seed') != cfg['seed']:
        return False, f"Seed mismatch: expected {cfg['seed']}, got {results.get('seed')}"
    if weather_log_path is not None and not Path(weather_log_path).is_file():
        return False, 'Missing retained read-only weather proof'

    # Verify requested_config.json
    req_path = run_folder / 'requested_config.json'
    if not req_path.exists():
        return False, 'Missing requested_config.json'
    try:
        req = read(req_path)
    except Exception as exc:
        return False, f'Unreadable requested_config.json: {exc}'

    expected_receiver_mode = 'autonomous' if mode == 'Automatic' else 'retrofit'
    if req.get('receiver_mode') != expected_receiver_mode:
        return False, f"requested_config receiver_mode mismatch: expected '{expected_receiver_mode}', got '{req.get('receiver_mode')}'"
    if req.get('enabled') != expected_bool:
        return False, f"requested_config enabled mismatch: expected {expected_bool}, got {req.get('enabled')}"
    if req.get('seed') != cfg['seed']:
        return False, f"requested_config seed mismatch: expected {cfg['seed']}, got {req.get('seed')}"

    # Verify study_options and results.requested_config against cfg
    study_opt = req.get('study_options', {})
    res_req = results.get('requested_config', {})

    if study_opt.get('weather') != cfg.get('weather'):
        return False, 'requested_config weather mismatch with cfg'
    if res_req.get('weather') != cfg.get('weather'):
        return False, 'results.requested_config weather mismatch with cfg'

    expected_followers = cfg.get('followers_automatic' if mode == 'Automatic' else 'followers_retrofit')
    if study_opt.get('followers') != expected_followers:
        return False, 'requested_config followers mismatch with cfg'
    if res_req.get('followers') != expected_followers:
        return False, 'results.requested_config followers mismatch with cfg'

    for field in ('occluder', 'pedestrian_speed_mps', 'driver_response_s'):
        if field in cfg:
            if study_opt.get(field) != cfg[field]:
                return False, f"requested_config study_options {field} mismatch"
            if res_req.get(field) != cfg[field]:
                return False, f"results.requested_config {field} mismatch"

    # Verify terminal_status.json and source_trial_path
    term_path = run_folder / 'terminal_status.json'
    if not term_path.exists():
        return False, 'Missing terminal_status.json'
    try:
        term = read(term_path)
    except Exception as exc:
        return False, f'Unreadable terminal_status.json: {exc}'

    source_trial = results.get('source_trial_path')
    term_artifacts = term.get('trial_artifacts')
    if not source_trial:
        return False, 'Missing source_trial_path in results.json'
    if not term_artifacts:
        return False, 'Missing trial_artifacts in terminal_status.json'
    if source_trial != term_artifacts:
        return False, f'source_trial_path {source_trial} does not match terminal_status trial_artifacts {term_artifacts}'

    # Verify physical coverage: 20s physical coverage, 1200 rows
    pt_path = run_folder / 'physical_timeline.json'
    if not pt_path.exists():
        return False, 'Missing physical_timeline.json'
    try:
        pt = read(pt_path)
        if not isinstance(pt, list) or len(pt) != REQUIRED_TIMELINE_ROWS:
            return False, f'physical_timeline row count is {len(pt) if isinstance(pt, list) else 0}, expected {REQUIRED_TIMELINE_ROWS}'
        start_t = pt[0].get('time_s', 0.0)
        end_t = pt[-1].get('time_s', 0.0)
        if abs(end_t - start_t - 20.0) > 0.2:
            return False, f'physical_timeline duration span {end_t - start_t:.3f}s does not cover ~20.0s'
        start_frame = pt[0].get('frame')
        end_frame = pt[-1].get('frame')
    except Exception as exc:
        return False, f'Unreadable physical_timeline.json: {exc}'

    # Verify camera manifest and six-view sets: 201 sets, 1206 pictures
    cm_path = run_folder / 'camera_manifest.json'
    if not cm_path.exists():
        return False, 'Missing camera_manifest.json'
    try:
        cm = read(cm_path)
        if not isinstance(cm, list) or len(cm) != REQUIRED_PICTURE_SETS:
            return False, f'camera_manifest set count is {len(cm) if isinstance(cm, list) else 0}, expected {REQUIRED_PICTURE_SETS}'
        pic_folder = run_folder / 'assets/pictures'
        if not pic_folder.exists():
            return False, 'Missing assets/pictures directory'
        for entry in cm:
            if entry.get('missing'):
                return False, f'camera_manifest entry frame {entry.get("frame")} marked missing'
            entry_views = entry.get('views', {})
            for v in VIEWS:
                if v not in entry_views:
                    return False, f'camera_manifest entry frame {entry.get("frame")} missing named view {v}'
            for v in VIEWS:
                img_name = entry_views[v]
                if not (pic_folder / img_name).exists():
                    return False, f'camera_manifest image file missing on disk: {img_name}'
    except Exception as exc:
        return False, f'Unreadable camera_manifest.json: {exc}'

    jpg_count = len(list(pic_folder.glob('*.jpg')))
    if jpg_count != REQUIRED_PICTURE_FILES:
        return False, f'assets/pictures jpg count is {jpg_count}, expected {REQUIRED_PICTURE_FILES}'

    # Retained matching weather proof check (if weather_log_path is provided)
    if weather_log_path is not None:
        has_matching_weather = False
        try:
            with open(weather_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get('run_label') == f"{cfg['test']}_{mode}_{state}":
                        frame = entry.get('world_frame')
                        if frame is not None and start_frame is not None and end_frame is not None:
                            effective = entry.get('effective', {})
                            matching = all(k in effective and abs(effective[k] - v) <= WEATHER_TOLERANCE
                                           for k, v in cfg.get('weather', {}).items())
                            if start_frame <= frame <= end_frame and not entry.get('mismatches') and matching:
                                has_matching_weather = True
                                break
            if not has_matching_weather:
                return False, f"No verified read-only weather sample bound to frame interval [{start_frame}, {end_frame}] found in {weather_log_path}"
        except Exception as exc:
            return False, f'Error checking retained weather proof: {exc}'

    return True, None


def check_readiness_gates(root, catalogue_sha256):
    """
    Verify both passed catalogue-bound gates and all sealed file digests before launch.
    """
    reports = root / 'Reports'
    val_gate_path = reports / 'validation_gate.json'
    pkg_gate_path = reports / 'validation_package_gate.json'

    if not val_gate_path.exists():
        raise RuntimeError('Missing validation_gate.json')
    if not pkg_gate_path.exists():
        raise RuntimeError('Missing validation_package_gate.json')

    val_gate = read(val_gate_path)
    pkg_gate = read(pkg_gate_path)

    if val_gate.get('passed') is not True:
        raise RuntimeError('validation_gate.json passed is not True')
    if val_gate.get('catalogue_sha256') != catalogue_sha256:
        raise RuntimeError(f"validation_gate catalogue_sha256 mismatch: {val_gate.get('catalogue_sha256')}")
    if val_gate.get('completed_runs') != 28 or val_gate.get('accepted_mode_pairs') != 14:
        raise RuntimeError(f"validation_gate incomplete: {val_gate.get('completed_runs')}/28, {val_gate.get('accepted_mode_pairs')}/14")

    if pkg_gate.get('passed') is not True:
        raise RuntimeError('validation_package_gate.json passed is not True')
    if pkg_gate.get('catalogue_sha256') != catalogue_sha256:
        raise RuntimeError(f"validation_package_gate catalogue_sha256 mismatch: {pkg_gate.get('catalogue_sha256')}")

    sealed_files = pkg_gate.get('evidence_sha256', {})
    if not sealed_files:
        raise RuntimeError('validation_package_gate contains no sealed evidence digests')

    for rel_path_str, expected_hash in sealed_files.items():
        fp = root / rel_path_str
        if not fp.exists():
            raise RuntimeError(f'Sealed file missing from disk: {rel_path_str}')
        actual_hash = digest(fp)
        if actual_hash != expected_hash:
            raise RuntimeError(f'Sealed file digest mismatch for {rel_path_str}: expected {expected_hash}, got {actual_hash}')

    mp4_count = sum(1 for k in sealed_files if k.endswith('.mp4'))
    if mp4_count != 42:
        raise RuntimeError(f'Expected 42 videos in validation seal, found {mp4_count}')


def count_genuinely_accepted_pairs(root, catalogue_sha256, tests):
    """
    Count independent acceptance ONLY from genuine Codex scene reviews matching file hashes,
    requiring both runs complete, qualified numeric pair, and nonempty bound hashes.
    Never creates or fakes reviews.
    """
    accepted = 0
    for cfg in tests:
        folder = root / cfg['relative_path']
        proto_path = folder / 'protocol_assessment.json'
        proto = read(proto_path) if proto_path.exists() else {}

        for mode in MODES:
            # Must be a qualified pair
            if proto.get(mode, {}).get('qualified') is not True:
                continue

            # Both runs must be physically complete
            off_run = folder / mode / 'Assistance_OFF'
            on_run = folder / mode / 'Assistance_ON'
            if not ((off_run / 'results.json').exists() and (on_run / 'results.json').exists()):
                continue

            sr_path = folder / mode / 'scene_review.json'
            if not sr_path.exists():
                continue
            try:
                review = read(sr_path)
            except Exception:
                continue

            if review.get('reviewer') != 'Codex':
                continue
            if review.get('catalogue_sha256') != catalogue_sha256:
                continue
            if not all(review.get(k) is True for k in (
                'actual_rgb_inspected', 'native_markings_visible',
                'pedestrian_access_verified', 'route_verified',
                'useful_occlusion_verified', 'physical_gaps_verified'
            )):
                continue

            # Must have nonempty evidence_sha256 for both OFF and ON
            evidence_sha = review.get('evidence_sha256', {})
            off_hashes = evidence_sha.get('OFF', {})
            on_hashes = evidence_sha.get('ON', {})
            if not off_hashes or not on_hashes:
                continue

            hashes_ok = True
            for state, state_hashes in (('OFF', off_hashes), ('ON', on_hashes)):
                for rel, expected in state_hashes.items():
                    fp = folder / mode / f'Assistance_{state}' / rel
                    if not fp.exists() or digest(fp) != expected:
                        hashes_ok = False
                        break
                if not hashes_ok:
                    break

            if hashes_ok:
                accepted += 1
    return accepted


class RobustnessSupervisor:
    def __init__(self, args):
        self.args = args
        self.root = args.root.resolve()
        self.workspace = Path(__file__).resolve().parents[2]
        self.directory = self.root / 'Reports/Robustness_Unattended'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.directory / 'checkpoint.json'
        self.progress_path = self.directory / 'progress.json'
        self.lock_path = self.directory / 'supervisor.lock.json'
        self.weather_log_path = self.directory / 'read_only_weather_observations.jsonl'

        self.worker = None
        self.owned = {}
        self.env = os.environ.copy()
        self.env.update(
            PYTHONPATH=str(self.workspace / 'src'),
            CROSSGUARD_STUDY_URL=args.bridge_url
        )

        self.carla_client = None
        self.carla_world = None
        self.active_test = None
        self.active_mode = None
        self.active_state = None
        self.active_sim_s = 0.0

        self.completed_runs_ledger = []
        self.failed_attempts = 0
        self.durations = []
        self.start_monotonic = time.monotonic()
        self.previous_runs = {}
        if args.resume and self.progress_path.exists():
            previous = read(self.progress_path)
            if previous.get('catalogue_sha256') != args.catalogue_sha256 or previous.get('phase') != 'Robustness':
                raise RuntimeError('Resume progress belongs to a different catalogue or phase')
            self.previous_runs = {
                (r['test'], r['mode'], r['assistance']): r
                for r in previous.get('runs', []) if r.get('status') == 'COMPLETED'
            }
            self.failed_attempts = previous.get('failed_attempts', 0)
            self.start_monotonic -= previous.get('elapsed_s', 0)
            for record in self.previous_runs.values():
                started = datetime.fromisoformat(record['started_utc'])
                completed = datetime.fromisoformat(record['completed_utc'])
                duration = (completed - started).total_seconds()
                if duration > 0:
                    self.durations.append(duration)

        self._interrupted = False
        self._setup_signals()

    def _setup_signals(self):
        def _handler(signum, frame):
            self.log(f'Received termination signal ({signum}); initiating fail-closed safe stop')
            self._interrupted = True
            self.safe_stop()
            self.terminate_owned()
            self.save_progress('INTERRUPTED_SAFE_STOP', error=f'Termination signal {signum}')
            self.release_lock()
            sys.exit(1)

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def log(self, message):
        line = f'{utc()} {message}'
        log_file = self.directory / 'supervisor.log'
        with log_file.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
        print(line, flush=True)

    def log_weather(self, entry):
        line = json.dumps(entry)
        with self.weather_log_path.open('a', encoding='utf-8') as f:
            f.write(line + '\n')

    def acquire_lock(self):
        if self.lock_path.exists():
            try:
                old = read(self.lock_path)
                if same_process(old.get('pid'), old.get('created')):
                    raise RuntimeError(f"Existing robustness supervisor (PID {old.get('pid')}) is alive; duplicate refused")
                if not self.args.resume:
                    raise RuntimeError(f"Stale supervisor lock found (PID {old.get('pid')}); explicit --resume required")
                self.log(f"Clearing stale supervisor lock from exited PID {old.get('pid')}")
                self.lock_path.unlink()
            except Exception as exc:
                if 'duplicate refused' in str(exc) or 'explicit --resume' in str(exc):
                    raise
                self.log(f"Warning reading existing lock: {exc}")
                if not self.args.resume:
                    raise RuntimeError("Existing lock unreadable; explicit --resume required")
                self.lock_path.unlink()

        my_pid = os.getpid()
        my_created = psutil.Process().create_time()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'pid': my_pid, 'created': my_created, 'created_utc': utc()}, f)

    def release_lock(self):
        if self.lock_path.exists():
            try:
                data = read(self.lock_path)
                if data.get('pid') == os.getpid():
                    self.lock_path.unlink()
            except Exception:
                pass

    def check_bridge_alive(self):
        """
        Check bridge PID, creation timestamp, and listener ownership on port 8007.
        Refuses absent listener or inaccessible ownership.
        """
        if not same_process(self.args.bridge_pid, self.args.bridge_created):
            raise RuntimeError(f"Original sole bridge identity changed or exited (expected PID {self.args.bridge_pid}, created {self.args.bridge_created}); no replacement started")

        listener_found = False
        try:
            for conn in psutil.net_connections(kind='tcp'):
                if conn.laddr and conn.laddr.port == 8007 and conn.status == psutil.CONN_LISTEN:
                    if conn.pid is None:
                        raise RuntimeError('Port 8007 listener ownership is inaccessible')
                    if conn.pid != self.args.bridge_pid:
                        raise RuntimeError(f"Port 8007 listener owned by unexpected PID {conn.pid}, expected {self.args.bridge_pid}")
                    listener_found = True
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
            raise RuntimeError(f'Port 8007 listener check failed: {exc}')

        if not listener_found:
            raise RuntimeError('Port 8007 listener is absent; sole bridge not listening')

    def get_bridge_status(self):
        self.check_bridge_alive()
        response = requests.get(self.args.bridge_url + '/api/status', timeout=10)
        response.raise_for_status()
        return response.json()

    def get_carla_world(self):
        if self.carla_world is None:
            import carla
            self.carla_client = carla.Client('127.0.0.1', CARLA_RPC_PORT)
            self.carla_client.set_timeout(5.0)
            self.carla_world = self.carla_client.get_world()
        return self.carla_world

    def check_effective_weather(self, cfg, run_label, sim_time_s):
        """
        Read actual CARLA weather via read-only get_weather/get_snapshot.
        Compare fields explicitly declared in cfg.weather with 0.01 tolerance.
        Does not mutate, tick, or change settings.
        Returns (True, snapshot.frame, None) or (False, snapshot.frame, mismatch_details).
        """
        world = self.get_carla_world()
        weather = world.get_weather()
        snapshot = world.get_snapshot()

        expected_weather = cfg.get('weather', {})
        sample_log = {
            'run_label': run_label,
            'time_utc': utc(),
            'sim_time_s': sim_time_s,
            'world_frame': snapshot.frame,
            'world_elapsed_s': snapshot.timestamp.elapsed_seconds,
            'effective': {},
            'expected': expected_weather,
            'mismatches': []
        }

        mismatches = []
        for field, exp_val in expected_weather.items():
            if hasattr(weather, field):
                act_val = getattr(weather, field)
                sample_log['effective'][field] = act_val
                if abs(act_val - exp_val) > WEATHER_TOLERANCE:
                    diff_msg = f"Field '{field}': expected {exp_val}, effective {act_val} (diff {abs(act_val - exp_val):.4f} > {WEATHER_TOLERANCE})"
                    mismatches.append(diff_msg)
            else:
                sample_log['effective'][field] = 'UNAVAILABLE_ATTR'
                mismatches.append(f"Field '{field}' missing from CARLA WeatherParameters")

        sample_log['mismatches'] = mismatches
        self.log_weather(sample_log)

        if mismatches:
            return False, snapshot.frame, mismatches
        return True, snapshot.frame, None

    def safe_stop(self):
        """
        Safe failure stop: only if this supervisor has launched its own worker;
        queue stop to original bridge while alive, confirm stopped,
        terminate only owned runner descendants identity checked, never bridge/CARLA/dashboard/other agy.
        """
        if self.worker is None:
            self.log('No owned worker launched; existing bridge, CARLA and unrelated workers left untouched')
            return

        try:
            self.check_bridge_alive()
        except RuntimeError as exc:
            self.log(f'Original bridge unavailable during safe stop ({exc}); no replacement process touched')
            return

        try:
            self.log('Submitting stop action through bridge command queue...')
            response = requests.post(
                self.args.bridge_url + '/api/command',
                json={'action': 'stop', 'params': {}},
                timeout=30
            )
            response.raise_for_status()
            until = time.monotonic() + 30
            while time.monotonic() < until:
                status = self.get_bridge_status()
                if status.get('state') == 'stopped':
                    self.log('Bridge stop confirmed: state is stopped')
                    break
                time.sleep(1)
        except Exception as exc:
            self.log(f'Safe stop queue request failed: {exc}')

    def collect_children(self):
        if self.worker and self.worker.poll() is None:
            try:
                for p in psutil.Process(self.worker.pid).children(recursive=True):
                    self.owned[p.pid] = p.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def terminate_owned(self):
        self.collect_children()
        if self.worker and self.worker.poll() is None:
            try:
                self.worker.terminate()
            except Exception:
                pass

        for pid, created in list(self.owned.items()):
            if pid != self.args.bridge_pid and same_process(pid, created):
                try:
                    psutil.Process(pid).terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def save_progress(self, status, error=None):
        elapsed_s = round(time.monotonic() - self.start_monotonic, 2)
        completed_count = len(self.completed_runs_ledger)
        remaining = TOTAL_PLANNED_RUNS - completed_count

        eta_s = None
        if self.durations:
            avg_duration = sum(self.durations) / len(self.durations)
            eta_s = round(avg_duration * remaining, 1)

        qualified_pairs = 0
        independently_accepted_pairs = 0
        try:
            catalogue = read(self.root / 'frozen_catalogue.json')
            rob_tests = [c for c in catalogue['tests'] if c['test'].startswith('Robustness')]
            independently_accepted_pairs = count_genuinely_accepted_pairs(
                self.root, self.args.catalogue_sha256, rob_tests
            )
            for cfg in rob_tests:
                folder = self.root / cfg['relative_path']
                proto_path = folder / 'protocol_assessment.json'
                if proto_path.exists():
                    proto = read(proto_path)
                    for mode in MODES:
                        if proto.get(mode, {}).get('qualified') is True:
                            qualified_pairs += 1
        except Exception:
            pass

        current_info = None
        if self.active_test:
            current_info = {
                'test': self.active_test,
                'mode': self.active_mode,
                'assistance': self.active_state,
                'simulation_s': self.active_sim_s
            }

        progress_payload = {
            'phase': 'Robustness',
            'status': status,
            'updated_utc': utc(),
            'planned_runs': TOTAL_PLANNED_RUNS,
            'completed_runs': completed_count,
            'qualified_pairs': qualified_pairs,
            'independently_accepted_pairs': independently_accepted_pairs,
            'failed_attempts': self.failed_attempts,
            'current': current_info,
            'elapsed_s': elapsed_s,
            'eta_s': eta_s,
            'runs': self.completed_runs_ledger,
            'supervisor_pid': os.getpid(),
            'worker_pid': self.worker.pid if (self.worker and self.worker.poll() is None) else None,
            'bridge_pid': self.args.bridge_pid,
            'catalogue_sha256': self.args.catalogue_sha256
        }
        if error:
            progress_payload['error'] = error

        atomic_json(self.progress_path, progress_payload)
        atomic_json(self.checkpoint_path, {
            'updated_utc': utc(),
            'supervisor_pid': os.getpid(),
            'supervisor_created': psutil.Process().create_time(),
            'phase': 'Robustness',
            'status': status,
            'completed_runs': completed_count,
            'failed_attempts': self.failed_attempts,
            'active': current_info,
            'catalogue_sha256': self.args.catalogue_sha256,
            'model': 'gemini-3.8-flash-medium'
        })

    def run_offline(self, *arguments):
        command = [sys.executable, *map(str, arguments)]
        self.log('Running offline assessment: ' + subprocess.list2cmdline(command))
        offline_log = self.directory / 'offline.log'
        with offline_log.open('a', encoding='utf-8') as log_f:
            result = subprocess.run(
                command,
                cwd=self.workspace,
                env=self.env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                timeout=600
            )
        if result.returncode != 0:
            raise RuntimeError(f'Offline command failed (code {result.returncode}); inspect offline.log')

    def execute_window(self, cfg, mode, state):
        status = self.get_bridge_status()
        if status.get('state') == 'running':
            raise RuntimeError('Existing live simulation running; refusing competing execution')

        name = f'{cfg["test"]}_{mode}_{state}'
        run_folder = self.root / cfg['relative_path'] / mode / f'Assistance_{state}'
        run_folder.mkdir(parents=True, exist_ok=True)

        self.active_test = cfg['test']
        self.active_mode = mode
        self.active_state = state
        self.active_sim_s = 0.0

        run_started_utc = utc()
        t0 = time.monotonic()

        command = [
            sys.executable,
            str(self.workspace / 'src/tools/town10_continuation.py'),
            'run',
            '--root', str(self.root),
            '--test', cfg['test'],
            '--mode', mode,
            '--state', state
        ]
        cmd_str = subprocess.list2cmdline(command)
        invocation_data = {
            'command': command,
            'command_line': cmd_str,
            'model': 'gemini-3.8-flash-medium',
            'created_utc': run_started_utc,
            'env': {'PYTHONPATH': 'src', 'CROSSGUARD_STUDY_URL': self.args.bridge_url}
        }
        atomic_json(self.directory / f'{name}_invocation.json', invocation_data)

        sim_log_path = self.directory / f'{name}_simulation.log'
        err_log_path = self.directory / f'{name}_stderr.log'

        self.log(f'Starting execution for {name}...')
        self.save_progress('RUNNING_WINDOW')

        verified_weather_frames = []

        with sim_log_path.open('w', encoding='utf-8') as out_f, err_log_path.open('w', encoding='utf-8') as err_f:
            self.worker = subprocess.Popen(
                command,
                cwd=self.workspace,
                env=self.env,
                stdout=out_f,
                stderr=err_f,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            begun = time.monotonic()
            last_progress = begun
            last_sim_time = None
            consecutive_errors = 0

            while self.worker.poll() is None:
                self.collect_children()

                if time.monotonic() - begun > WALL_TIME_LIMIT_S:
                    raise RuntimeError(f'Worker wall-time bound ({WALL_TIME_LIMIT_S}s) exceeded')

                try:
                    bridge_st = self.get_bridge_status()
                    consecutive_errors = 0
                    current_sim = bridge_st.get('sim_time_s', 0.0)
                    self.active_sim_s = current_sim

                    if current_sim != last_sim_time:
                        last_progress = time.monotonic()
                        last_sim_time = current_sim

                    if bridge_st.get('state') == 'running' and (time.monotonic() - last_progress > STALL_TIMEOUT_S):
                        raise RuntimeError(f'Simulation progress stalled for {STALL_TIMEOUT_S}s')

                    if bridge_st.get('state') == 'running' and current_sim > 0.0:
                        weather_ok, frame_sampled, mismatch_info = self.check_effective_weather(cfg, name, current_sim)
                        if not weather_ok:
                            raise RuntimeError(f'Weather mismatch detected: {mismatch_info}')
                        verified_weather_frames.append(frame_sampled)

                    self.save_progress('RUNNING_WINDOW')

                except requests.RequestException as req_err:
                    consecutive_errors += 1
                    self.log(f'Bridge status read error ({consecutive_errors}/{MAX_STATUS_ERRORS}): {req_err}')
                    if consecutive_errors >= MAX_STATUS_ERRORS:
                        raise RuntimeError('Bridge status unavailable for three consecutive reads')

                time.sleep(5)

        duration = time.monotonic() - t0
        self.durations.append(duration)

        if self.worker.returncode != 0:
            raise RuntimeError(f'Worker process exited with code {self.worker.returncode}')

        if not verified_weather_frames:
            raise RuntimeError(f'No weather samples verified during running phase for {name}; refusing window acceptance')

        evidence_ok, err_msg = verify_completed_evidence(run_folder, cfg, mode, state, self.weather_log_path)
        if not evidence_ok:
            raise RuntimeError(f'Evidence verification failed after run completion: {err_msg}')

        leftovers = [pid for pid, created in self.owned.items() if same_process(pid, created)]
        if leftovers:
            raise RuntimeError(f'Worker exited with orphaned child processes: {leftovers}')

        self.completed_runs_ledger.append({
            'test': cfg['test'],
            'theme': cfg['theme'],
            'mode': mode,
            'assistance': state,
            'status': 'COMPLETED',
            'relative_path': str(run_folder.relative_to(self.root)),
            'started_utc': run_started_utc,
            'completed_utc': utc(),
            'error': None
        })

        self.active_test = None
        self.active_mode = None
        self.active_state = None
        self.active_sim_s = 0.0
        self.save_progress('WINDOW_COMPLETED')
        self.log(f'Successfully completed and verified window {name} ({duration:.1f}s)')

    def assess_configuration(self, cfg):
        """
        After all 4 runs of a configuration succeed, run numeric assessment using town10_continuation_package.assess
        and offline pictures helper. If numeric qualification fails, stop progression.
        """
        self.log(f'Running offline assessment for {cfg["test"]}...')
        self.save_progress('ASSESSING_CONFIGURATION')

        folder = self.root / cfg['relative_path']

        assess_script = self.workspace / 'src/tools/town10_continuation_package.py'
        self.run_offline(assess_script, 'assess', '--root', self.root, '--test', cfg['test'])

        proto_path = folder / 'protocol_assessment.json'
        if not proto_path.exists():
            raise RuntimeError(f'Missing protocol_assessment.json for {cfg["test"]}')
        proto = read(proto_path)

        for mode in MODES:
            mode_eval = proto.get(mode, {})
            if mode_eval.get('qualified') is not True:
                reason = mode_eval.get('reason', 'unknown')
                raise RuntimeError(f'Numeric pair qualification failed for {cfg["test"]} ({mode}): {reason}; stopping progression for diagnosis')

        media_script = self.workspace / 'src/tools/town10_continuation_media.py'
        self.run_offline(media_script, 'pictures', '--root', self.root, '--test', cfg['test'])

        self.log(f'Offline assessment and pictures helper completed successfully for {cfg["test"]}')

    def run(self):
        self.acquire_lock()
        try:
            cat_file = self.root / 'frozen_catalogue.json'
            if digest(cat_file) != self.args.catalogue_sha256:
                raise RuntimeError(f'Pinned catalogue hash mismatch: expected {self.args.catalogue_sha256}, got {digest(cat_file)}')

            catalogue = verified_catalogue(self.root)

            for source, expected in catalogue.get('evaluation_source_hashes', {}).items():
                if digest(self.workspace / source) != expected:
                    raise RuntimeError(f'Frozen evaluation source changed: {source}')

            check_readiness_gates(self.root, self.args.catalogue_sha256)

            self.check_bridge_alive()
            initial_status = self.get_bridge_status()
            if initial_status.get('state') == 'running':
                raise RuntimeError('Existing live simulation running; cannot start supervisor')

            rob_configs = [c for c in catalogue['tests'] if c['test'].startswith('Robustness')]
            if len(rob_configs) != 60:
                raise RuntimeError(f'Expected 60 frozen Robustness configurations, found {len(rob_configs)}')

            plan_queue = []
            for cfg in rob_configs:
                cfg_runs_completed = 0
                for mode in MODES:
                    for state in STATES:
                        run_folder = self.root / cfg['relative_path'] / mode / f'Assistance_{state}'
                        is_complete, reason = verify_completed_evidence(
                            run_folder, cfg, mode, state, self.weather_log_path
                        )
                        if is_complete:
                            cfg_runs_completed += 1
                            self.completed_runs_ledger.append(self.previous_runs.get((cfg['test'], mode, state), {
                                'test': cfg['test'],
                                'theme': cfg['theme'],
                                'mode': mode,
                                'assistance': state,
                                'status': 'COMPLETED',
                                'relative_path': str(run_folder.relative_to(self.root)),
                                'started_utc': utc(),
                                'completed_utc': utc(),
                                'error': None
                            }))
                        elif reason == 'Not started':
                            plan_queue.append((cfg, mode, state))
                        else:
                            raise RuntimeError(f'Retained partial or failed artifact blocks unattended execution: {reason} at {run_folder}')

                # In resume mode: assess every completed four-run config even if already finished
                if cfg_runs_completed == 4:
                    folder = self.root / cfg['relative_path']
                    proto_path = folder / 'protocol_assessment.json'
                    if not proto_path.exists():
                        self.log(f'Resuming assessment for completed four-run config {cfg["test"]}')
                        self.assess_configuration(cfg)

            self.log(f'Readiness audit complete: {len(self.completed_runs_ledger)} existing verified, {len(plan_queue)} pending')
            self.save_progress('READY')

            if self.args.dry_run:
                self.save_progress('DRY_RUN_VERIFIED')
                self.log(f'Dry run verified cleanly: 0 simulation commands submitted, 60 configs ({TOTAL_PLANNED_RUNS} runs) planned')
                return

            for cfg, mode, state in plan_queue:
                # Recheck verified_catalogue runtime hashes AND evaluation hashes before EVERY run
                if digest(cat_file) != self.args.catalogue_sha256:
                    raise RuntimeError('Catalogue hash changed before run')
                verified_catalogue(self.root)
                for source, expected in catalogue.get('evaluation_source_hashes', {}).items():
                    if digest(self.workspace / source) != expected:
                        raise RuntimeError(f'Evaluation source changed before run: {source}')
                check_readiness_gates(self.root, self.args.catalogue_sha256)

                self.execute_window(cfg, mode, state)

                cfg_all_runs = [r for r in self.completed_runs_ledger if r['test'] == cfg['test']]
                if len(cfg_all_runs) == 4 and (mode == 'Retrofit' and state == 'ON'):
                    self.assess_configuration(cfg)

            # Final verification of full 240 runs
            if len(self.completed_runs_ledger) == TOTAL_PLANNED_RUNS:
                # Require all 120 qualified pairs
                total_qualified = 0
                for cfg in rob_configs:
                    proto_path = self.root / cfg['relative_path'] / 'protocol_assessment.json'
                    if proto_path.exists():
                        proto = read(proto_path)
                        for m in MODES:
                            if proto.get(m, {}).get('qualified') is True:
                                total_qualified += 1
                if total_qualified != 120:
                    raise RuntimeError(f'Final 240 runs cannot claim complete without all 120 qualified pairs; found {total_qualified}')

            self.save_progress('AWAITING_CODEX_INDEPENDENT_REVIEW')
            self.log('All 240 physical robustness runs recorded and numerically assessed. Status: AWAITING_CODEX_INDEPENDENT_REVIEW')

        except Exception as exc:
            self.failed_attempts += 1
            err_msg = str(exc)
            self.log(f'SUPERVISOR FAILED: {err_msg}')
            self.safe_stop()
            self.terminate_owned()
            self.save_progress('FAILED_SAFE_STOP', error=err_msg)
            raise
        finally:
            self.release_lock()


def main():
    p = argparse.ArgumentParser(description='Detached fail-closed Robustness supervisor (240 runs)')
    p.add_argument('--root', type=Path, required=True, help='Path to Study Revision root')
    p.add_argument('--catalogue-sha256', required=True, help='Expected SHA256 of frozen_catalogue.json')
    p.add_argument('--bridge-url', default='http://127.0.0.1:8007', help='Bridge HTTP URL')
    p.add_argument('--bridge-pid', type=int, required=True, help='Expected bridge PID')
    p.add_argument('--bridge-created', type=float, required=True, help='Expected bridge creation timestamp')
    p.add_argument('--agy', default='agy', help='Path to agy binary')
    p.add_argument('--resume', action='store_true', help='Allow resuming from previous completed runs / clearing stale lock')
    p.add_argument('--dry-run', action='store_true', help='Verify preflight readiness without executing simulations')
    args = p.parse_args()
    supervisor = RobustnessSupervisor(args)
    supervisor.run()


if __name__ == '__main__':
    main()
