"""CrossGuard Town10HD Deferred-Configuration Continuation Controller.

Authorizes continuing healthy configurations while deferring broken/retained configurations
(such as Robustness_T01_02) for later coordinated review, without bypassing independent gates,
restarting preserved processes, or recording mismatched weather.

Key invariants:
- Preserves sole bridge PID 2860, creation 1791618082.6745353, listening on port 8007.
- Never imports CARLA in monitor/offline contexts; uses read-only CARLA client strictly for live weather sampling.
- Reuses verified gates and checksums from town10_unattended_robustness and town10_continuation.
- Retains 240 planned runs denominator: clearly separates completed, deferred, failed, pending, qualified, and reviewed.
- Deferrals are NEVER counted as completed or independently approved.
- Permits exactly one bounded attempt on the next untouched configuration (Robustness_T01_03).
- If weather mismatch recurs on that attempt, treats it as a shared infrastructure fault,
  executes safe-stop, preserves evidence, and blocks simulator execution while repair proceeds.
- Dry run executes all preflight checks without issuing any bridge simulation commands.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import psutil
import requests

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from town10_continuation import MODES, STATES, digest, read, verified_catalogue
from town10_unattended_robustness import (
    CARLA_RPC_PORT,
    REQUIRED_PICTURE_FILES,
    REQUIRED_PICTURE_SETS,
    REQUIRED_TIMELINE_ROWS,
    STALL_TIMEOUT_S,
    TOTAL_PLANNED_RUNS,
    VIEWS,
    WALL_TIME_LIMIT_S,
    WEATHER_TOLERANCE,
    check_readiness_gates,
    count_genuinely_accepted_pairs,
    same_process,
    verify_completed_evidence,
)

FROZEN_CATALOGUE_SHA256 = "6f53d7f6eb8330d77131ebca660f5c467ce71939c0791f5fcb02d3073c56357a"
DEFAULT_BRIDGE_PID = 2860
DEFAULT_BRIDGE_CREATED = 1791618082.6745353
DEFAULT_BRIDGE_PORT = 8007
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8007"
DEFERRED_CONFIG_IDS = ["Robustness_T01_02"]


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


class DeferredContinuationController:
    """Fail-closed continuation controller supporting deferred configurations and shared weather fault halts."""

    def __init__(
        self,
        root: Path,
        catalogue_sha256: str = FROZEN_CATALOGUE_SHA256,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        bridge_pid: int = DEFAULT_BRIDGE_PID,
        bridge_created: float = DEFAULT_BRIDGE_CREATED,
        dry_run: bool = False,
    ):
        self.root = root.resolve()
        self.workspace = Path(__file__).resolve().parents[2]
        self.catalogue_sha256 = catalogue_sha256
        self.bridge_url = bridge_url
        self.bridge_pid = bridge_pid
        self.bridge_created = bridge_created
        self.dry_run = dry_run

        self.reports_dir = self.root / "Reports" / "Robustness_Unattended"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.progress_path = self.reports_dir / "progress.json"
        self.checkpoint_path = self.reports_dir / "checkpoint.json"
        self.lock_path = self.reports_dir / "supervisor.lock.json"
        self.deferred_lock_path = self.reports_dir / "deferred_continuation.lock.json"
        self.log_path = self.reports_dir / "supervisor.log"
        self.weather_log_path = self.reports_dir / "read_only_weather_observations.jsonl"
        self.repair_status_path = self.reports_dir / "Repair" / "status.json"
        self.preflight_report_path = self.reports_dir / "preflight_validation_report.json"

        self.env = os.environ.copy()
        self.env.update(
            PYTHONPATH=f"{self.workspace / 'src'};{self.workspace / 'src' / 'tools'}",
            CROSSGUARD_STUDY_URL=self.bridge_url,
        )

        self.worker: Optional[subprocess.Popen] = None
        self.owned_children: Dict[int, float] = {}
        self.completed_runs_ledger: List[Dict[str, Any]] = []
        self.deferred_runs_ledger: List[Dict[str, Any]] = []
        self.failed_attempts = 1  # 1 historical failed attempt in T01_02
        self.start_monotonic = time.monotonic()
        self.durations: List[float] = []

        self.active_test: Optional[str] = None
        self.active_mode: Optional[str] = None
        self.active_state: Optional[str] = None
        self.active_sim_s: float = 0.0

        self.carla_client = None
        self.carla_world = None

        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        def _handler(signum: int, frame: Any) -> None:
            self.log(f"Received termination signal ({signum}); initiating fail-closed safe stop")
            self.safe_stop()
            self.terminate_owned()
            self.save_progress("INTERRUPTED_SAFE_STOP", error=f"Termination signal {signum}")
            self.release_lock()
            sys.exit(1)

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def log(self, message: str) -> None:
        line = f"{utc_now_iso()} {message}"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def log_weather(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry) + "\n"
        with self.weather_log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def acquire_lock(self) -> None:
        # Check supervisor lock
        if self.lock_path.exists():
            try:
                old = read(self.lock_path)
                if same_process(old.get("pid"), old.get("created")):
                    raise RuntimeError(
                        f"Existing robustness supervisor (PID {old.get('pid')}) is alive; duplicate refused"
                    )
                self.log(f"Clearing stale supervisor lock from exited PID {old.get('pid')}")
                self.lock_path.unlink()
            except Exception as exc:
                if "duplicate refused" in str(exc):
                    raise
                self.log(f"Warning reading supervisor lock: {exc}")
                self.lock_path.unlink()

        # Check deferred continuation lock
        if self.deferred_lock_path.exists():
            try:
                old = read(self.deferred_lock_path)
                if same_process(old.get("pid"), old.get("created")):
                    raise RuntimeError(
                        f"Existing deferred continuation controller (PID {old.get('pid')}) is alive; duplicate refused"
                    )
                self.log(f"Clearing stale deferred continuation lock from exited PID {old.get('pid')}")
                self.deferred_lock_path.unlink()
            except Exception as exc:
                if "duplicate refused" in str(exc):
                    raise
                self.log(f"Warning reading deferred continuation lock: {exc}")
                self.deferred_lock_path.unlink()

        my_pid = os.getpid()
        my_created = psutil.Process().create_time()
        lock_data = {"pid": my_pid, "created": my_created, "created_utc": utc_now_iso()}
        atomic_write_json(self.lock_path, lock_data)
        atomic_write_json(self.deferred_lock_path, lock_data)

    def release_lock(self) -> None:
        for p in (self.lock_path, self.deferred_lock_path):
            if p.exists():
                try:
                    data = read(p)
                    if data.get("pid") == os.getpid():
                        p.unlink()
                except Exception:
                    pass

    def check_bridge_alive(self) -> None:
        if not same_process(self.bridge_pid, self.bridge_created):
            raise RuntimeError(
                f"Original sole bridge identity changed or exited (expected PID {self.bridge_pid}, "
                f"created {self.bridge_created}); no replacement started"
            )

        listener_found = False
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.laddr and conn.laddr.port == DEFAULT_BRIDGE_PORT and conn.status == psutil.CONN_LISTEN:
                    if conn.pid is None:
                        raise RuntimeError(f"Port {DEFAULT_BRIDGE_PORT} listener ownership is inaccessible")
                    if conn.pid != self.bridge_pid:
                        raise RuntimeError(
                            f"Port {DEFAULT_BRIDGE_PORT} listener owned by unexpected PID {conn.pid}, expected {self.bridge_pid}"
                        )
                    listener_found = True
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
            raise RuntimeError(f"Port {DEFAULT_BRIDGE_PORT} listener check failed: {exc}")

        if not listener_found:
            raise RuntimeError(f"Port {DEFAULT_BRIDGE_PORT} listener is absent; sole bridge not listening")

    def get_bridge_status(self) -> Dict[str, Any]:
        self.check_bridge_alive()
        resp = requests.get(f"{self.bridge_url}/api/status", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_carla_world(self) -> Any:
        if self.carla_world is None:
            import carla
            self.carla_client = carla.Client("127.0.0.1", CARLA_RPC_PORT)
            self.carla_client.set_timeout(5.0)
            self.carla_world = self.carla_client.get_world()
        return self.carla_world

    def check_effective_weather(
        self, cfg: Dict[str, Any], run_label: str, sim_time_s: float
    ) -> Tuple[bool, int, Optional[List[str]]]:
        """Read actual CARLA weather via read-only get_weather/get_snapshot."""
        world = self.get_carla_world()
        weather = world.get_weather()
        snapshot = world.get_snapshot()

        expected_weather = cfg.get("weather", {})
        sample_log: Dict[str, Any] = {
            "run_label": run_label,
            "time_utc": utc_now_iso(),
            "sim_time_s": sim_time_s,
            "world_frame": snapshot.frame,
            "world_elapsed_s": snapshot.timestamp.elapsed_seconds,
            "effective": {},
            "expected": expected_weather,
            "mismatches": [],
        }

        mismatches = []
        for field, exp_val in expected_weather.items():
            if hasattr(weather, field):
                act_val = getattr(weather, field)
                sample_log["effective"][field] = act_val
                if abs(act_val - exp_val) > WEATHER_TOLERANCE:
                    diff_msg = (
                        f"Field '{field}': expected {exp_val}, effective {act_val} "
                        f"(diff {abs(act_val - exp_val):.4f} > {WEATHER_TOLERANCE})"
                    )
                    mismatches.append(diff_msg)
            else:
                sample_log["effective"][field] = "UNAVAILABLE_ATTR"
                mismatches.append(f"Field '{field}' missing from CARLA WeatherParameters")

        sample_log["mismatches"] = mismatches
        self.log_weather(sample_log)

        if mismatches:
            return False, snapshot.frame, mismatches
        return True, snapshot.frame, None

    def sample_carla_weather_read_only(self) -> Dict[str, Any]:
        """Sample current CARLA weather read-only without modifying anything."""
        world = self.get_carla_world()
        weather = world.get_weather()
        snapshot = world.get_snapshot()
        fields = [
            "cloudiness",
            "precipitation",
            "precipitation_deposits",
            "wind_intensity",
            "sun_azimuth_angle",
            "sun_altitude_angle",
            "fog_density",
            "fog_distance",
            "wetness",
        ]
        sampled = {f: getattr(weather, f, None) for f in fields}
        return {"frame": snapshot.frame, "weather": sampled}

    def safe_stop(self) -> None:
        """Submit stop action to bridge queue and await stopped state."""
        if self.worker is None:
            self.log("No owned worker launched; existing bridge and CARLA left untouched")
            return

        try:
            self.check_bridge_alive()
        except RuntimeError as exc:
            self.log(f"Original bridge unavailable during safe stop ({exc})")
            return

        try:
            self.log("Submitting stop action through bridge command queue...")
            resp = requests.post(
                f"{self.bridge_url}/api/command",
                json={"action": "stop", "params": {}},
                timeout=30,
            )
            resp.raise_for_status()
            until = time.monotonic() + 30
            while time.monotonic() < until:
                status = self.get_bridge_status()
                if status.get("state") == "stopped":
                    self.log("Bridge stop confirmed: state is stopped")
                    break
                time.sleep(1)
        except Exception as exc:
            self.log(f"Safe stop queue request failed: {exc}")

    def collect_children(self) -> None:
        if self.worker and self.worker.poll() is None:
            try:
                for p in psutil.Process(self.worker.pid).children(recursive=True):
                    self.owned_children[p.pid] = p.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def terminate_owned(self) -> None:
        self.collect_children()
        if self.worker and self.worker.poll() is None:
            try:
                self.worker.terminate()
            except Exception:
                pass

        for pid, created in list(self.owned_children.items()):
            if pid != self.bridge_pid and same_process(pid, created):
                try:
                    psutil.Process(pid).terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def save_progress(self, status: str, error: Optional[str] = None) -> None:
        elapsed_s = round(time.monotonic() - self.start_monotonic, 2)
        completed_count = len(self.completed_runs_ledger)
        deferred_count = len(self.deferred_runs_ledger)
        remaining = TOTAL_PLANNED_RUNS - completed_count - deferred_count

        eta_s = None
        if self.durations:
            avg_duration = sum(self.durations) / len(self.durations)
            eta_s = round(avg_duration * max(0, remaining), 1)

        qualified_pairs = 0
        independently_accepted_pairs = 0
        try:
            catalogue = read(self.root / "frozen_catalogue.json")
            rob_tests = [c for c in catalogue["tests"] if c["test"].startswith("Robustness")]
            independently_accepted_pairs = count_genuinely_accepted_pairs(
                self.root, self.catalogue_sha256, rob_tests
            )
            for cfg in rob_tests:
                # Do not count deferred configurations toward qualified pairs
                if cfg["test"] in DEFERRED_CONFIG_IDS:
                    continue
                folder = self.root / cfg["relative_path"]
                proto_path = folder / "protocol_assessment.json"
                if proto_path.exists():
                    proto = read(proto_path)
                    for mode in MODES:
                        if proto.get(mode, {}).get("qualified") is True:
                            qualified_pairs += 1
        except Exception:
            pass

        current_info = None
        if self.active_test:
            current_info = {
                "test": self.active_test,
                "mode": self.active_mode,
                "assistance": self.active_state,
                "simulation_s": self.active_sim_s,
            }

        progress_payload = {
            "phase": "Robustness",
            "status": status,
            "updated_utc": utc_now_iso(),
            "planned_runs": TOTAL_PLANNED_RUNS,
            "completed_runs": completed_count,
            "deferred_runs": deferred_count,
            "deferred_configs": DEFERRED_CONFIG_IDS,
            "qualified_pairs": qualified_pairs,
            "independently_accepted_pairs": independently_accepted_pairs,
            "failed_attempts": self.failed_attempts,
            "current": current_info,
            "elapsed_s": elapsed_s,
            "eta_s": eta_s,
            "runs": self.completed_runs_ledger,
            "deferred_ledger": self.deferred_runs_ledger,
            "supervisor_pid": os.getpid(),
            "worker_pid": self.worker.pid if (self.worker and self.worker.poll() is None) else None,
            "bridge_pid": self.bridge_pid,
            "catalogue_sha256": self.catalogue_sha256,
        }
        if error:
            progress_payload["error"] = error

        atomic_write_json(self.progress_path, progress_payload)
        atomic_write_json(
            self.checkpoint_path,
            {
                "updated_utc": utc_now_iso(),
                "supervisor_pid": os.getpid(),
                "supervisor_created": psutil.Process().create_time(),
                "phase": "Robustness",
                "status": status,
                "completed_runs": completed_count,
                "deferred_runs": deferred_count,
                "failed_attempts": self.failed_attempts,
                "active": current_info,
                "catalogue_sha256": self.catalogue_sha256,
                "model": "gemini-3.8-flash-medium",
            },
        )

    def execute_preflight_checks(self) -> Dict[str, Any]:
        """Perform full independent verification without sending simulation commands."""
        report: Dict[str, Any] = {
            "timestamp_utc": utc_now_iso(),
            "catalogue_verified": False,
            "evaluation_sources_verified": False,
            "validation_gate_passed": False,
            "validation_package_gate_passed": False,
            "sealed_evidence_digests_passed": False,
            "bridge_identity_preserved": False,
            "repair_agent_status": None,
            "current_carla_weather": None,
            "all_preflight_checks_passed": False,
        }

        # 1. Read Repair status
        if self.repair_status_path.is_file():
            try:
                report["repair_agent_status"] = read(self.repair_status_path)
            except Exception as exc:
                report["repair_agent_error"] = str(exc)

        # 2. Read-only CARLA weather sampling
        try:
            report["current_carla_weather"] = self.sample_carla_weather_read_only()
        except Exception as exc:
            report["carla_weather_error"] = str(exc)

        # 3. Check catalogue digest
        cat_file = self.root / "frozen_catalogue.json"
        cat_sha = digest(cat_file)
        if cat_sha != self.catalogue_sha256:
            report["catalogue_error"] = f"Catalogue hash mismatch: expected {self.catalogue_sha256}, got {cat_sha}"
            report["all_preflight_checks_passed"] = False
            atomic_write_json(self.preflight_report_path, report)
            return report

        try:
            catalogue = verified_catalogue(self.root)
            report["catalogue_verified"] = True
            report["runtime_sources_verified"] = True
        except RuntimeError as exc:
            report["runtime_source_error"] = str(exc)
            report["all_preflight_checks_passed"] = False
            atomic_write_json(self.preflight_report_path, report)
            return report

        # 2. Check evaluation sources
        for source, exp in catalogue.get("evaluation_source_hashes", {}).items():
            act = digest(self.workspace / source)
            if act != exp:
                report["evaluation_source_error"] = f"Frozen evaluation source changed: {source}"
                report["all_preflight_checks_passed"] = False
                atomic_write_json(self.preflight_report_path, report)
                return report
        report["evaluation_sources_verified"] = True

        # 3. Check validation gates & sealed digests
        try:
            check_readiness_gates(self.root, self.catalogue_sha256)
            report["validation_gate_passed"] = True
            report["validation_package_gate_passed"] = True
            report["sealed_evidence_digests_passed"] = True
        except RuntimeError as exc:
            report["validation_gates_error"] = str(exc)
            report["all_preflight_checks_passed"] = False
            atomic_write_json(self.preflight_report_path, report)
            return report

        # 4. Check bridge identity
        try:
            self.check_bridge_alive()
            bridge_st = self.get_bridge_status()
            if bridge_st.get("state") == "running":
                report["bridge_error"] = "Existing live simulation running; cannot start continuation controller"
                report["all_preflight_checks_passed"] = False
                atomic_write_json(self.preflight_report_path, report)
                return report
            report["bridge_identity_preserved"] = True
        except RuntimeError as exc:
            report["bridge_error"] = str(exc)
            report["all_preflight_checks_passed"] = False
            atomic_write_json(self.preflight_report_path, report)
            return report

        report["all_preflight_checks_passed"] = True
        atomic_write_json(self.preflight_report_path, report)
        return report

    def execute_window(self, cfg: Dict[str, Any], mode: str, state: str) -> None:
        status = self.get_bridge_status()
        if status.get("state") == "running":
            raise RuntimeError("Existing live simulation running; refusing competing execution")

        name = f"{cfg['test']}_{mode}_{state}"
        run_folder = self.root / cfg["relative_path"] / mode / f"Assistance_{state}"
        run_folder.mkdir(parents=True, exist_ok=True)

        self.active_test = cfg["test"]
        self.active_mode = mode
        self.active_state = state
        self.active_sim_s = 0.0

        run_started_utc = utc_now_iso()
        t0 = time.monotonic()

        command = [
            sys.executable,
            str(self.workspace / "src/tools/town10_continuation.py"),
            "run",
            "--root",
            str(self.root),
            "--test",
            cfg["test"],
            "--mode",
            mode,
            "--state",
            state,
        ]
        cmd_str = subprocess.list2cmdline(command)
        invocation_data = {
            "command": command,
            "command_line": cmd_str,
            "model": "gemini-3.8-flash-medium",
            "created_utc": run_started_utc,
            "env": {"PYTHONPATH": "src;src/tools", "CROSSGUARD_STUDY_URL": self.bridge_url},
        }
        atomic_write_json(self.reports_dir / f"{name}_invocation.json", invocation_data)

        sim_log_path = self.reports_dir / f"{name}_simulation.log"
        err_log_path = self.reports_dir / f"{name}_stderr.log"

        self.log(f"Starting execution for {name}...")
        self.save_progress("RUNNING_WINDOW")

        verified_weather_frames = []

        with sim_log_path.open("w", encoding="utf-8") as out_f, err_log_path.open("w", encoding="utf-8") as err_f:
            self.worker = subprocess.Popen(
                command,
                cwd=self.workspace,
                env=self.env,
                stdout=out_f,
                stderr=err_f,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            begun = time.monotonic()
            last_progress = begun
            last_sim_time = None
            consecutive_errors = 0

            while self.worker.poll() is None:
                self.collect_children()

                if time.monotonic() - begun > WALL_TIME_LIMIT_S:
                    raise RuntimeError(f"Worker wall-time bound ({WALL_TIME_LIMIT_S}s) exceeded")

                try:
                    bridge_st = self.get_bridge_status()
                    consecutive_errors = 0
                    current_sim = bridge_st.get("sim_time_s", 0.0)
                    self.active_sim_s = current_sim

                    if current_sim != last_sim_time:
                        last_progress = time.monotonic()
                        last_sim_time = current_sim

                    if bridge_st.get("state") == "running" and (time.monotonic() - last_progress > STALL_TIMEOUT_S):
                        raise RuntimeError(f"Simulation progress stalled for {STALL_TIMEOUT_S}s")

                    if bridge_st.get("state") == "running" and current_sim > 0.0:
                        weather_ok, frame_sampled, mismatch_info = self.check_effective_weather(cfg, name, current_sim)
                        if not weather_ok:
                            raise RuntimeError(
                                f"SHARED_WEATHER_FAULT: Weather mismatch detected: {mismatch_info}"
                            )
                        verified_weather_frames.append(frame_sampled)

                    self.save_progress("RUNNING_WINDOW")

                except requests.RequestException as req_err:
                    consecutive_errors += 1
                    self.log(f"Bridge status read error ({consecutive_errors}/3): {req_err}")
                    if consecutive_errors >= 3:
                        raise RuntimeError("Bridge status unavailable for three consecutive reads")

                time.sleep(5)

        duration = time.monotonic() - t0
        self.durations.append(duration)

        if self.worker.returncode != 0:
            raise RuntimeError(f"Worker process exited with code {self.worker.returncode}")

        if not verified_weather_frames:
            raise RuntimeError(
                f"No weather samples verified during running phase for {name}; refusing window acceptance"
            )

        evidence_ok, err_msg = verify_completed_evidence(run_folder, cfg, mode, state, self.weather_log_path)
        if not evidence_ok:
            raise RuntimeError(f"Evidence verification failed after run completion: {err_msg}")

        leftovers = [pid for pid, created in self.owned_children.items() if same_process(pid, created)]
        if leftovers:
            raise RuntimeError(f"Worker exited with orphaned child processes: {leftovers}")

        self.completed_runs_ledger.append({
            "test": cfg["test"],
            "theme": cfg["theme"],
            "mode": mode,
            "assistance": state,
            "status": "COMPLETED",
            "relative_path": str(run_folder.relative_to(self.root)),
            "started_utc": run_started_utc,
            "completed_utc": utc_now_iso(),
            "error": None,
        })

        self.active_test = None
        self.active_mode = None
        self.active_state = None
        self.active_sim_s = 0.0
        self.save_progress("WINDOW_COMPLETED")
        self.log(f"Successfully completed and verified window {name} ({duration:.1f}s)")

    def assess_configuration(self, cfg: Dict[str, Any]) -> None:
        self.log(f"Running offline assessment for {cfg['test']}...")
        self.save_progress("ASSESSING_CONFIGURATION")

        folder = self.root / cfg["relative_path"]
        assess_script = self.workspace / "src/tools/town10_continuation_package.py"
        command = [sys.executable, str(assess_script), "assess", "--root", str(self.root), "--test", cfg["test"]]
        self.log("Running offline assessment: " + subprocess.list2cmdline(command))
        offline_log = self.reports_dir / "offline.log"
        with offline_log.open("a", encoding="utf-8") as log_f:
            res = subprocess.run(command, cwd=self.workspace, env=self.env, stdout=log_f, stderr=subprocess.STDOUT, timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"Offline assess failed (code {res.returncode})")

        proto_path = folder / "protocol_assessment.json"
        if not proto_path.exists():
            raise RuntimeError(f"Missing protocol_assessment.json for {cfg['test']}")
        proto = read(proto_path)

        for mode in MODES:
            mode_eval = proto.get(mode, {})
            if mode_eval.get("qualified") is not True:
                reason = mode_eval.get("reason", "unknown")
                raise RuntimeError(f"Numeric pair qualification failed for {cfg['test']} ({mode}): {reason}")

        media_script = self.workspace / "src/tools/town10_continuation_media.py"
        media_cmd = [sys.executable, str(media_script), "pictures", "--root", str(self.root), "--test", cfg["test"]]
        with offline_log.open("a", encoding="utf-8") as log_f:
            res_m = subprocess.run(media_cmd, cwd=self.workspace, env=self.env, stdout=log_f, stderr=subprocess.STDOUT, timeout=600)
        if res_m.returncode != 0:
            raise RuntimeError(f"Offline pictures helper failed (code {res_m.returncode})")

        self.log(f"Offline assessment and pictures helper completed successfully for {cfg['test']}")

    def run(self) -> None:
        self.acquire_lock()
        try:
            # 1. Preflight checks
            preflight = self.execute_preflight_checks()
            if not preflight.get("all_preflight_checks_passed"):
                missing = (
                    preflight.get("runtime_source_error")
                    or preflight.get("catalogue_error")
                    or preflight.get("evaluation_source_error")
                    or preflight.get("validation_gates_error")
                    or preflight.get("bridge_error")
                    or "Preflight checks failed"
                )
                self.log(f"PREFLIGHT BLOCKED: Refusing execution safely. Missing check: {missing}")
                self.save_progress("BLOCKED_PREFLIGHT_FAILURE", error=missing)
                return

            self.log("Preflight checks verified: validation gates, sealed digests, catalogue, and bridge preserved.")

            catalogue = verified_catalogue(self.root)
            rob_configs = [c for c in catalogue["tests"] if c["test"].startswith("Robustness")]
            if len(rob_configs) != 60:
                raise RuntimeError(f"Expected 60 frozen Robustness configurations, found {len(rob_configs)}")

            # 2. Audit existing ledger and separate deferred configurations
            plan_queue: List[Tuple[Dict[str, Any], str, str]] = []
            for cfg in rob_configs:
                test_id = cfg["test"]
                if test_id in DEFERRED_CONFIG_IDS:
                    # Defer entire configuration
                    for mode in MODES:
                        for state in STATES:
                            run_folder = self.root / cfg["relative_path"] / mode / f"Assistance_{state}"
                            # If existing completed run (like T01_02 Automatic OFF)
                            is_complete, _ = verify_completed_evidence(run_folder, cfg, mode, state, self.weather_log_path)
                            self.deferred_runs_ledger.append({
                                "test": test_id,
                                "theme": cfg["theme"],
                                "mode": mode,
                                "assistance": state,
                                "status": "DEFERRED",
                                "sub_status": "COMPLETED_RETAINED" if is_complete else "DEFERRED_UNRESOLVED",
                                "relative_path": str(run_folder.relative_to(self.root)),
                                "reason": "User steering: deferred while weather mismatch is diagnosed; retains evidence without accepted pair claim",
                            })
                    self.log(f"Configuration {test_id} deferred per user steering; retained in place without acceptance claim.")
                    continue

                # For non-deferred configurations:
                cfg_completed = 0
                for mode in MODES:
                    for state in STATES:
                        run_folder = self.root / cfg["relative_path"] / mode / f"Assistance_{state}"
                        is_complete, reason = verify_completed_evidence(run_folder, cfg, mode, state, self.weather_log_path)
                        if is_complete:
                            cfg_completed += 1
                            self.completed_runs_ledger.append({
                                "test": test_id,
                                "theme": cfg["theme"],
                                "mode": mode,
                                "assistance": state,
                                "status": "COMPLETED",
                                "relative_path": str(run_folder.relative_to(self.root)),
                                "started_utc": utc_now_iso(),
                                "completed_utc": utc_now_iso(),
                                "error": None,
                            })
                        elif reason == "Not started":
                            plan_queue.append((cfg, mode, state))
                        else:
                            raise RuntimeError(
                                f"Retained partial or failed artifact blocks execution: {reason} at {run_folder}"
                            )

            self.log(
                f"Readiness audit complete: {len(self.completed_runs_ledger)} verified completed, "
                f"{len(self.deferred_runs_ledger)} deferred, {len(plan_queue)} pending in queue."
            )
            self.save_progress("READY")

            if self.dry_run:
                self.save_progress("DRY_RUN_VERIFIED")
                self.log(
                    f"Dry run verified cleanly: 0 simulation commands submitted, "
                    f"{len(self.completed_runs_ledger)} completed, {len(self.deferred_runs_ledger)} deferred, "
                    f"{len(plan_queue)} planned."
                )
                return

            # 3. Live Execution: Only one bounded attempt on next untouched config
            if not plan_queue:
                self.log("No pending configurations in plan queue.")
                self.save_progress("COMPLETED")
                return

            next_cfg, next_mode, next_state = plan_queue[0]
            self.log(
                f"Permitting bounded attempt on next untouched configuration: {next_cfg['test']} ({next_mode} {next_state})"
            )

            # Check repair agent status immediately before launch
            if self.repair_status_path.is_file():
                repair_data = read(self.repair_status_path)
                self.log(
                    f"Repair agent status: state={repair_data.get('state')}, readiness={repair_data.get('repair_readiness')}"
                )

            # Execute the window
            try:
                self.execute_window(next_cfg, next_mode, next_state)
            except RuntimeError as exc:
                err_str = str(exc)
                if "SHARED_WEATHER_FAULT" in err_str or "Weather mismatch detected" in err_str:
                    self.log(
                        f"SHARED INFRASTRUCTURE FAULT CONFIRMED on {next_cfg['test']}: {err_str}. "
                        "Halting continuation cleanly to prevent catalogue corruption."
                    )
                    self.safe_stop()
                    self.terminate_owned()
                    self.save_progress("BLOCKED_SHARED_WEATHER_FAULT", error=err_str)
                    return
                raise

            # If the first run succeeded, check if full config can be finished
            remaining_for_cfg = [
                (c, m, s) for (c, m, s) in plan_queue[1:] if c["test"] == next_cfg["test"]
            ]
            for c, m, s in remaining_for_cfg:
                self.execute_window(c, m, s)

            # Assess completed configuration
            self.assess_configuration(next_cfg)

            # If healthy, continue remaining unattended
            remaining_queue = [
                (c, m, s) for (c, m, s) in plan_queue if c["test"] != next_cfg["test"]
            ]
            for c, m, s in remaining_queue:
                # Check repair status between configurations
                if self.repair_status_path.is_file():
                    repair_data = read(self.repair_status_path)
                    if repair_data.get("blocking_scope") == "halt_simulator":
                        self.log("Repair agent requested simulator halt; stopping gracefully between configurations")
                        break

                self.execute_window(c, m, s)
                cfg_runs = [r for r in self.completed_runs_ledger if r["test"] == c["test"]]
                if len(cfg_runs) == 4 and (m == "Retrofit" and s == "ON"):
                    self.assess_configuration(c)

            self.save_progress("AWAITING_CODEX_INDEPENDENT_REVIEW")

        except Exception as exc:
            self.failed_attempts += 1
            err_msg = str(exc)
            self.log(f"CONTINUATION CONTROLLER FAILED: {err_msg}")
            self.safe_stop()
            self.terminate_owned()
            self.save_progress("FAILED_SAFE_STOP", error=err_msg)
            raise
        finally:
            self.release_lock()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CrossGuard Town10HD Deferred-Configuration Continuation Controller"
    )
    workspace_default = (
        Path(__file__).resolve().parents[2] / "Evidence" / "Study_02_Town10HD_Continuation" / "Revision_03"
    )
    parser.add_argument("--root", type=Path, default=workspace_default, help="Path to evidence root directory")
    parser.add_argument("--catalogue-sha256", default=FROZEN_CATALOGUE_SHA256, help="Expected frozen catalogue SHA256")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL, help="Bridge HTTP URL")
    parser.add_argument("--bridge-pid", type=int, default=DEFAULT_BRIDGE_PID, help="Expected bridge PID")
    parser.add_argument("--bridge-created", type=float, default=DEFAULT_BRIDGE_CREATED, help="Expected bridge creation timestamp")
    parser.add_argument("--dry-run", action="store_true", help="Execute all independent preflight checks without simulation commands")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    controller = DeferredContinuationController(
        root=args.root,
        catalogue_sha256=args.catalogue_sha256,
        bridge_url=args.bridge_url,
        bridge_pid=args.bridge_pid,
        bridge_created=args.bridge_created,
        dry_run=args.dry_run,
    )
    controller.run()


if __name__ == "__main__":
    main()
