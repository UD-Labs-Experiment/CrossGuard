"""CrossGuard Town10HD Robustness Live Progress Dashboard.

Local loopback read-only HTTP dashboard for monitoring the authorized 240-run
Town10HD robustness batch and baseline validation status.
Standard library HTTP server with restricted local evidence endpoint.
Strict disk completion verification and cryptographic hash bound acceptance.
"""
from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple, Set

THEME_NAMES = {
    "T01": "Crossing Location & Occlusion",
    "T02": "Daylight & Weather",
    "T03": "Following & Secondary Collisions",
    "T04": "Communication Failures & Redundancy",
    "T05": "Observation Loss & Occlusion",
    "T06": "Waiting & Retreating Pedestrians",
}

MODES = ("Automatic", "Retrofit")
STATES = ("OFF", "ON")
REQUIRED_VIEWS = ("top", "driver", "person", "opposite", "driver_b", "driver_c")
REQUIRED_TIMELINE_ROWS = 1200
REQUIRED_PICTURE_SETS = 201
REQUIRED_PICTURE_FILES = 1206

SAFE_EXTENSIONS = {
    ".json", ".log", ".txt", ".md", ".png", ".jpg", ".jpeg", ".mp4",
}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def file_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def read_json_safe(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(content)
    except Exception:
        return None


def verify_run_disk_evidence(
    run_dir: Path,
    expected_cfg: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
    state: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Derive durable actual completion only from rigorous disk numeric evidence.
    
    Requires:
      - terminal_status.json: terminal_metrics with end_reason == 'observation_window_complete'
        (never merely terminal_metrics truthy).
      - requested_config.json: matches expected frozen configuration seed if provided.
      - results.json: terminal_reason == 'observation_window_complete', technical_failure is False,
        no technical_failure.json on disk, source_trial_path matching terminal_status trial_artifacts.
      - placement_audit.json: valid audit record exists.
      - physical_timeline.json: exactly 1200 timeline rows reaching 20 seconds.
      - camera_manifest.json: exactly 201 camera frames with all six named views.
      - assets/pictures: all 1206 image files existing on disk.
    """
    evidence_status = {
        "terminal_status": False,
        "requested_config": False,
        "placement_audit": False,
        "physical_timeline": False,
        "camera_manifest": False,
        "images_coverage": False,
        "results": False,
        "source_identity_consistent": False,
        "technical_failure": False,
        "contacts": {"pedestrian": None, "A_B": None, "B_C": None},
        "complete": False,
        "reasons": [],
    }

    if not run_dir.is_dir():
        evidence_status["reasons"].append(f"Directory missing: {run_dir}")
        return False, evidence_status

    if (run_dir / "technical_failure.json").is_file():
        evidence_status["technical_failure"] = True
        evidence_status["reasons"].append("technical_failure.json present on disk")

    # 1. Terminal Status & end_reason check
    term_path = run_dir / "terminal_status.json"
    term_data = read_json_safe(term_path)
    if not isinstance(term_data, dict):
        evidence_status["reasons"].append("terminal_status.json missing or invalid JSON")
    else:
        metrics = term_data.get("terminal_metrics")
        if not isinstance(metrics, dict):
            evidence_status["reasons"].append("terminal_status missing terminal_metrics dict")
        else:
            end_reason = metrics.get("end_reason")
            if end_reason != "observation_window_complete":
                evidence_status["reasons"].append(f"terminal_metrics end_reason is '{end_reason}', expected 'observation_window_complete'")
            else:
                evidence_status["terminal_status"] = True

    # 2. Requested Config
    req_path = run_dir / "requested_config.json"
    req_data = read_json_safe(req_path)
    if not isinstance(req_data, dict):
        evidence_status["reasons"].append("requested_config.json missing or invalid JSON")
    else:
        if expected_cfg and "seed" in expected_cfg:
            if req_data.get("seed") != expected_cfg["seed"]:
                evidence_status["reasons"].append(f"requested_config seed mismatch: {req_data.get('seed')} vs {expected_cfg['seed']}")
            else:
                evidence_status["requested_config"] = True
        else:
            evidence_status["requested_config"] = True

    # 3. Placement Audit
    place_path = run_dir / "placement_audit.json"
    place_data = read_json_safe(place_path)
    if not isinstance(place_data, dict):
        evidence_status["reasons"].append("placement_audit.json missing or invalid JSON")
    else:
        evidence_status["placement_audit"] = True

    # 4. Results & Source Identity
    res_path = run_dir / "results.json"
    res_data = read_json_safe(res_path)
    if not isinstance(res_data, dict):
        evidence_status["reasons"].append("results.json missing or invalid JSON")
    else:
        if res_data.get("technical_failure") is True:
            evidence_status["technical_failure"] = True
            evidence_status["reasons"].append("results.json reports technical_failure True")

        if res_data.get("terminal_reason") != "observation_window_complete":
            evidence_status["reasons"].append(f"results.json terminal_reason is '{res_data.get('terminal_reason')}', expected 'observation_window_complete'")
        elif not evidence_status["technical_failure"]:
            evidence_status["results"] = True

        # Check source identity consistency
        source_trial = res_data.get("source_trial_path")
        if not source_trial or not isinstance(source_trial, str):
            evidence_status["reasons"].append("results.json missing or empty source_trial_path")
        else:
            term_trial = term_data.get("trial_artifacts") if isinstance(term_data, dict) else None
            if term_trial and source_trial != term_trial:
                evidence_status["reasons"].append(f"source_trial_path {source_trial} does not match terminal_status trial_artifacts {term_trial}")
            else:
                # Check seed/mode/assistance if expected_cfg supplied
                match_id = True
                if expected_cfg and "seed" in expected_cfg:
                    if res_data.get("seed") != expected_cfg["seed"]:
                        match_id = False
                        evidence_status["reasons"].append(f"results.json seed {res_data.get('seed')} != expected {expected_cfg['seed']}")
                if mode:
                    expected_mode = {"automatic": "autonomous", "retrofit": "retrofit"}.get(mode.lower(), mode.lower())
                    if str(res_data.get("mode", "")).lower() != expected_mode:
                        match_id = False
                        evidence_status["reasons"].append(f"results.json mode {res_data.get('mode')} != expected {expected_mode} ({mode})")
                if state:
                    exp_asst = (state == "ON")
                    if res_data.get("assistance") != exp_asst:
                        match_id = False
                        evidence_status["reasons"].append(f"results.json assistance {res_data.get('assistance')} != expected {exp_asst}")
                if match_id:
                    evidence_status["source_identity_consistent"] = True

        # Extract contacts according to real results schema
        ped_contacts = res_data.get("pedestrian_contacts")
        if isinstance(ped_contacts, list):
            evidence_status["contacts"]["pedestrian"] = len(ped_contacts) > 0

        secondary = res_data.get("secondary")
        if isinstance(secondary, dict):
            ab = secondary.get("A_B")
            if isinstance(ab, dict) and "contact" in ab:
                evidence_status["contacts"]["A_B"] = bool(ab["contact"])
            bc = secondary.get("B_C")
            if isinstance(bc, dict) and "contact" in bc:
                evidence_status["contacts"]["B_C"] = bool(bc["contact"])

    # 5. Physical Timeline: exactly 1200 rows reaching 20 seconds
    phys_path = run_dir / "physical_timeline.json"
    phys_data = read_json_safe(phys_path)
    if not isinstance(phys_data, list):
        evidence_status["reasons"].append("physical_timeline.json missing or invalid JSON list")
    elif len(phys_data) != REQUIRED_TIMELINE_ROWS:
        evidence_status["reasons"].append(f"physical_timeline row count is {len(phys_data)}, expected exactly {REQUIRED_TIMELINE_ROWS}")
    else:
        start_t = phys_data[0].get("time_s", 0.0)
        end_t = phys_data[-1].get("time_s", 0.0)
        if not (isinstance(start_t, (int, float)) and isinstance(end_t, (int, float))):
            evidence_status["reasons"].append("physical_timeline timestamps not numeric")
        elif end_t < 19.98 or (end_t - start_t) < 19.95:
            evidence_status["reasons"].append(f"physical_timeline duration span {end_t - start_t:.3f}s does not cover 20.0s")
        else:
            evidence_status["physical_timeline"] = True

    # 6. Camera Manifest: exactly 201 frames with six named views
    cam_path = run_dir / "camera_manifest.json"
    cam_data = read_json_safe(cam_path)
    required_views_set = set(REQUIRED_VIEWS)
    if not isinstance(cam_data, list):
        evidence_status["reasons"].append("camera_manifest.json missing or invalid JSON list")
    elif len(cam_data) != REQUIRED_PICTURE_SETS:
        evidence_status["reasons"].append(f"camera_manifest frame count is {len(cam_data)}, expected exactly {REQUIRED_PICTURE_SETS}")
    else:
        manifest_views_valid = True
        for idx, frame in enumerate(cam_data):
            if not isinstance(frame, dict):
                manifest_views_valid = False
                evidence_status["reasons"].append(f"camera_manifest frame {idx} is not a dict")
                break
            views = frame.get("views")
            if not isinstance(views, dict) or set(views.keys()) != required_views_set:
                manifest_views_valid = False
                evidence_status["reasons"].append(f"camera_manifest frame {idx} views do not match six named views")
                break
        if manifest_views_valid:
            evidence_status["camera_manifest"] = True

    # 7. Images Coverage: all 1206 image files existing on disk
    pic_folder = run_dir / "assets" / "pictures"
    if not pic_folder.is_dir():
        evidence_status["reasons"].append("assets/pictures directory missing")
    else:
        jpg_files = list(pic_folder.glob("*.jpg"))
        if len(jpg_files) != REQUIRED_PICTURE_FILES:
            evidence_status["reasons"].append(f"assets/pictures jpg file count is {len(jpg_files)}, expected exactly {REQUIRED_PICTURE_FILES}")
        else:
            # Confirm images referenced in camera manifest exist
            all_referenced_exist = True
            if evidence_status["camera_manifest"] and isinstance(cam_data, list):
                # Sample check frames or verify all
                for frame in cam_data:
                    for v_file in frame.get("views", {}).values():
                        if not (pic_folder / v_file).is_file():
                            all_referenced_exist = False
                            evidence_status["reasons"].append(f"Referenced picture missing: {v_file}")
                            break
                    if not all_referenced_exist:
                        break
            if all_referenced_exist:
                evidence_status["images_coverage"] = True

    is_complete = (
        evidence_status["terminal_status"] and
        evidence_status["requested_config"] and
        evidence_status["placement_audit"] and
        evidence_status["physical_timeline"] and
        evidence_status["camera_manifest"] and
        evidence_status["images_coverage"] and
        evidence_status["results"] and
        evidence_status["source_identity_consistent"] and
        not evidence_status["technical_failure"]
    )
    evidence_status["complete"] = is_complete
    return is_complete, evidence_status


def verify_pair_acceptance(
    cfg_dir: Path,
    mode: str,
    off_complete: bool,
    on_complete: bool,
) -> Tuple[bool, str]:
    """Verify independent acceptance for a matched pair.
    
    Never call independently accepted just from scene_review booleans:
    requires:
      1. Both physical runs complete on disk.
      2. Protocol qualified in protocol_assessment.json or validation_assessment.json.
      3. scene_review.json review booleans all True.
      4. review result_sha256 matching results.json for both OFF and ON.
      5. review evidence_sha256 hash bindings matching disk files for both OFF and ON.
    """
    if not (off_complete and on_complete):
        return False, "Both runs must be durably complete on disk"

    proto_path = cfg_dir / "protocol_assessment.json"
    if not proto_path.is_file():
        proto_path = cfg_dir / "validation_assessment.json"
    proto_data = read_json_safe(proto_path)
    if not isinstance(proto_data, dict):
        return False, "Protocol assessment file missing or unreadable"

    mode_info = proto_data.get(mode, {})
    if not isinstance(mode_info, dict):
        return False, f"Protocol assessment for {mode} missing"

    is_qualified = mode_info.get("qualified") is True or mode_info.get("qualification", {}).get("qualified") is True
    if not is_qualified:
        return False, f"Protocol for {mode} is not qualified"

    review_path = cfg_dir / mode / "scene_review.json"
    review = read_json_safe(review_path)
    if not isinstance(review, dict):
        return False, f"scene_review.json for {mode} missing"

    required_booleans = (
        "actual_rgb_inspected", "native_markings_visible",
        "pedestrian_access_verified", "route_verified",
        "useful_occlusion_verified", "physical_gaps_verified"
    )
    if not all(review.get(k) is True for k in required_booleans):
        return False, "One or more scene review booleans is not True"

    # Verify result_sha256 bindings
    result_hashes = review.get("result_sha256")
    if not isinstance(result_hashes, dict):
        return False, "Missing result_sha256 dict in scene_review"

    for state in STATES:
        exp_res_sha = result_hashes.get(state)
        if not exp_res_sha or not isinstance(exp_res_sha, str):
            return False, f"Missing result_sha256 for {state}"
        actual_res_sha = file_sha256(cfg_dir / mode / f"Assistance_{state}" / "results.json")
        if actual_res_sha != exp_res_sha:
            return False, f"result_sha256 mismatch for {state}: {actual_res_sha} != {exp_res_sha}"

    # Verify evidence_sha256 bindings
    evidence_hashes = review.get("evidence_sha256")
    if not isinstance(evidence_hashes, dict):
        return False, "Missing evidence_sha256 dict in scene_review"

    for state in STATES:
        state_ev = evidence_hashes.get(state)
        if not isinstance(state_ev, dict) or not state_ev:
            return False, f"Missing or empty evidence_sha256 for {state}"
        for rel_file, exp_hash in state_ev.items():
            ev_file = cfg_dir / mode / f"Assistance_{state}" / rel_file
            if not ev_file.is_file():
                return False, f"Reviewed evidence file missing: {state}/{rel_file}"
            actual_ev_sha = file_sha256(ev_file)
            if actual_ev_sha != exp_hash:
                return False, f"Evidence hash mismatch for {state}/{rel_file}: {actual_ev_sha} != {exp_hash}"

    return True, "Independently accepted with verified hash bindings"


def compute_run_fingerprint(run_dir: Path) -> Tuple[Any, ...]:
    """Compute stat fingerprint of all relevant evidence artifacts and pictures directory."""
    tracked_files = (
        "results.json",
        "requested_config.json",
        "terminal_status.json",
        "placement_audit.json",
        "physical_timeline.json",
        "camera_manifest.json",
        "technical_failure.json",
    )
    fp: List[Any] = []
    for fname in tracked_files:
        fpath = run_dir / fname
        try:
            st = fpath.stat()
            fp.append((fname, st.st_mtime_ns, st.st_size))
        except OSError:
            fp.append((fname, None, None))

    pic_dir = run_dir / "assets" / "pictures"
    try:
        st_pic = pic_dir.stat()
        fp.append(("assets/pictures", st_pic.st_mtime_ns))
    except OSError:
        fp.append(("assets/pictures", None))

    return tuple(fp)


class DashboardDataCollector:
    """Reads and reconciles atomic progress.json, catalog metadata, and disk evidence."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.progress_file = self.root / "Reports" / "Robustness_Unattended" / "progress.json"
        self.catalogue_file = self.root / "frozen_catalogue.json"
        self.catalogue_sha_file = self.root / "frozen_catalogue.sha256.json"
        self.validation_gate_file = self.root / "Reports" / "validation_gate.json"
        # Bounded cache of run verification: cache_key -> (fingerprint, (complete, evidence_status))
        self._run_cache: Dict[str, Tuple[Tuple[Any, ...], Tuple[bool, Dict[str, Any]]]] = {}

    def verify_run_cached(
        self,
        run_dir: Path,
        expected_cfg: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        cache_key = str(run_dir.resolve())
        fp = compute_run_fingerprint(run_dir)

        if cache_key in self._run_cache:
            cached_fp, (cached_complete, cached_ev) = self._run_cache[cache_key]
            if cached_fp == fp:
                # Reuse cached verification: return clean copy
                return cached_complete, json.loads(json.dumps(cached_ev))

        # Fingerprint changed or not in cache: perform full rigorous verification
        complete, ev = verify_run_disk_evidence(
            run_dir, expected_cfg=expected_cfg, mode=mode, state=state
        )

        # Enforce bounded cache of at most 240 runs
        if len(self._run_cache) >= 240 and cache_key not in self._run_cache:
            oldest_key = next(iter(self._run_cache))
            del self._run_cache[oldest_key]

        self._run_cache[cache_key] = (fp, (complete, json.loads(json.dumps(ev))))
        return complete, ev

    def load_catalogue(self) -> Dict[str, Any]:
        data = read_json_safe(self.catalogue_file) or {}
        sha_data = read_json_safe(self.catalogue_sha_file) or {}
        catalogue_sha256 = sha_data.get("sha256")
        if not catalogue_sha256 and self.catalogue_file.is_file():
            try:
                raw = self.catalogue_file.read_bytes()
                catalogue_sha256 = hashlib.sha256(raw).hexdigest()
            except Exception:
                catalogue_sha256 = None

        tests = data.get("tests", [])
        robustness_tests = [t for t in tests if not t.get("test", "").startswith("Validation")]
        validation_tests = [t for t in tests if t.get("test", "").startswith("Validation")]

        return {
            "catalogue_sha256": catalogue_sha256,
            "robustness_configs": robustness_tests,
            "validation_configs": validation_tests,
            "total_robustness_configs": len(robustness_tests),
            "total_validation_configs": len(validation_tests),
        }

    def load_validation_baseline(self) -> Dict[str, Any]:
        """Missing baseline gate must display unknown/not passed, never default true/28."""
        if not self.validation_gate_file.is_file():
            return {
                "completed_runs": None,
                "planned_runs": 28,
                "qualified_pairs": None,
                "planned_pairs": 14,
                "accepted_pairs": None,
                "passed": False,
                "media_seal_passed": False,
                "status": "MISSING_NOT_PASSED",
            }

        gate = read_json_safe(self.validation_gate_file)
        if not isinstance(gate, dict):
            return {
                "completed_runs": None,
                "planned_runs": 28,
                "qualified_pairs": None,
                "planned_pairs": 14,
                "accepted_pairs": None,
                "passed": False,
                "media_seal_passed": False,
                "status": "MALFORMED_NOT_PASSED",
            }

        passed = gate.get("passed") is True
        return {
            "completed_runs": gate.get("completed_runs"),
            "planned_runs": 28,
            "qualified_pairs": gate.get("qualified_mode_pairs"),
            "planned_pairs": 14,
            "accepted_pairs": gate.get("accepted_mode_pairs"),
            "passed": passed,
            "media_seal_passed": gate.get("independent_media_package_passed") is True,
            "status": "VALIDATED_AND_FROZEN" if passed else "FAILED_GATE",
        }

    def scan_disk_robustness(self, configs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Scan physical directory evidence across all 60 configs x 4 modes."""
        grid_runs: Dict[str, Dict[str, Any]] = {}
        completed_runs_count = 0
        contacts_ped = 0
        contacts_ab = 0
        contacts_bc = 0
        theme_summary: Dict[str, Dict[str, Any]] = {}

        for theme_code in ["T01", "T02", "T03", "T04", "T05", "T06"]:
            theme_summary[theme_code] = {
                "name": THEME_NAMES.get(theme_code, theme_code),
                "total_configs": 0,
                "total_runs": 0,
                "completed_runs": 0,
                "qualified_pairs": 0,
                "accepted_pairs": 0,
                "failed_runs": 0,
                "contacts": {"pedestrian": 0, "A_B": 0, "B_C": 0},
            }

        for cfg in configs:
            test_id = cfg.get("test", "")
            theme_code = cfg.get("theme", "")
            rel_path = cfg.get("relative_path", "")
            cfg_dir = self.root / rel_path

            if theme_code in theme_summary:
                theme_summary[theme_code]["total_configs"] += 1
                theme_summary[theme_code]["total_runs"] += 4

            grid_runs[test_id] = {
                "test": test_id,
                "theme": theme_code,
                "relative_path": rel_path,
                "modes": {},
                "qualified_pairs": 0,
                "accepted_pairs": 0,
            }

            mode_completion_map: Dict[str, Dict[str, bool]] = {}

            # First verify all runs on disk
            for mode in MODES:
                mode_completion_map[mode] = {}
                for state in STATES:
                    mode_key = f"{mode}_{state}"
                    run_dir = cfg_dir / mode / f"Assistance_{state}"
                    complete, ev = self.verify_run_cached(run_dir, expected_cfg=cfg, mode=mode, state=state)
                    mode_completion_map[mode][state] = complete

                    failed = ev.get("technical_failure", False) or (run_dir / "technical_failure.json").is_file()

                    status_str = "PENDING"
                    if complete:
                        status_str = "COMPLETED"
                        completed_runs_count += 1
                        if theme_code in theme_summary:
                            theme_summary[theme_code]["completed_runs"] += 1
                        if ev["contacts"]["pedestrian"] is True:
                            contacts_ped += 1
                            if theme_code in theme_summary:
                                theme_summary[theme_code]["contacts"]["pedestrian"] += 1
                        if ev["contacts"]["A_B"] is True:
                            contacts_ab += 1
                            if theme_code in theme_summary:
                                theme_summary[theme_code]["contacts"]["A_B"] += 1
                        if ev["contacts"]["B_C"] is True:
                            contacts_bc += 1
                            if theme_code in theme_summary:
                                theme_summary[theme_code]["contacts"]["B_C"] += 1
                    elif failed:
                        status_str = "FAILED"
                        if theme_code in theme_summary:
                            theme_summary[theme_code]["failed_runs"] += 1
                    elif (run_dir / "results.json").is_file() or (run_dir / "physical_timeline.json").is_file():
                        status_str = "INCOMPLETE"

                    grid_runs[test_id]["modes"][mode_key] = {
                        "status": status_str,
                        "complete": complete,
                        "failed": failed,
                        "contacts": ev["contacts"],
                        "reasons": ev.get("reasons", []),
                        "relative_path": str((run_dir).relative_to(self.root)).replace("\\", "/") if run_dir.exists() else "",
                    }

            # Protocol qualification & Independent Acceptance check per mode pair
            proto_file = cfg_dir / "protocol_assessment.json"
            if not proto_file.is_file():
                proto_file = cfg_dir / "validation_assessment.json"
            proto_data = read_json_safe(proto_file)

            for mode in MODES:
                if isinstance(proto_data, dict):
                    m_eval = proto_data.get(mode, {})
                    if m_eval.get("qualified") is True or m_eval.get("qualification", {}).get("qualified") is True:
                        grid_runs[test_id]["qualified_pairs"] += 1
                        if theme_code in theme_summary:
                            theme_summary[theme_code]["qualified_pairs"] += 1

                # Rigorous acceptance check: requires qualified protocol, both complete runs, and hash bindings
                is_accepted, accept_reason = verify_pair_acceptance(
                    cfg_dir=cfg_dir,
                    mode=mode,
                    off_complete=mode_completion_map[mode].get("OFF", False),
                    on_complete=mode_completion_map[mode].get("ON", False),
                )
                if is_accepted:
                    grid_runs[test_id]["accepted_pairs"] += 1
                    if theme_code in theme_summary:
                        theme_summary[theme_code]["accepted_pairs"] += 1

        total_qualified_pairs = sum(g["qualified_pairs"] for g in grid_runs.values())
        total_accepted_pairs = sum(g["accepted_pairs"] for g in grid_runs.values())

        return {
            "completed_runs_count": completed_runs_count,
            "qualified_pairs": total_qualified_pairs,
            "accepted_pairs": total_accepted_pairs,
            "contacts": {
                "pedestrian": contacts_ped,
                "A_B": contacts_ab,
                "B_C": contacts_bc,
            },
            "themes": theme_summary,
            "grid": grid_runs,
        }

    def load_active_validation(self):
        marker = read_json_safe(self.root.parent / 'autopilot_active.json')
        if not isinstance(marker, dict) or marker.get('phase') != 'Validation':
            return None
        revision = marker.get('revision')
        if not isinstance(revision, str) or Path(revision).name != revision or revision in ('.', '..'):
            return {'status': 'INVALID_ACTIVE_REVISION', 'completed_runs': None}
        progress = read_json_safe(self.root.parent / revision / 'Reports/Revalidation_Unattended/progress.json')
        if not isinstance(progress, dict):
            return {'status': 'VALIDATION_STATUS_UNAVAILABLE', 'completed_runs': None}
        return {'revision': revision, 'status': progress.get('status'),
                'completed_runs': progress.get('completed_runs'), 'planned_runs': 28,
                'current': progress.get('current'), 'updated_utc': progress.get('updated_utc'),
                'supervisor_pid': progress.get('supervisor_pid'),
                'supervisor_alive': is_pid_alive(progress.get('supervisor_pid')),
                'error': progress.get('error')}

    def load_video_export(self) -> Dict[str, Any]:
        """Read six independent export checkpoints without touching simulation evidence."""
        themes = []
        for theme, name in THEME_NAMES.items():
            path = self.root / 'Reports' / 'Video_Export' / f'progress_{theme}.json'
            raw = read_json_safe(path)
            valid = isinstance(raw, dict)
            raw = raw if valid else {}
            def count(key: str) -> int:
                value = raw.get(key, 0)
                return min(60, max(0, value)) if type(value) is int else 0
            status = raw.get('status', 'STATUS_UNAVAILABLE' if path.exists() else 'NOT_STARTED')
            age = None
            try:
                stamp = datetime.datetime.fromisoformat(str(raw['updated_utc']).replace('Z', '+00:00'))
                age = max(0, (utc_now() - stamp).total_seconds())
            except (KeyError, TypeError, ValueError):
                pass
            pid = raw.get('worker_pid')
            alive = is_pid_alive(pid) if type(pid) is int else None
            stale = status in ('RUNNING', 'INITIALIZING', 'EXPORTING', 'VERIFYING') and (
                alive is False or (age is not None and age > 120))
            themes.append({**raw, 'theme': theme, 'name': name, 'status': status,
                           'planned_videos': 60, 'completed_videos': count('completed_videos'),
                           'failed_videos': count('failed_videos'), 'refresh_age_s': age,
                           'worker_alive': alive, 'is_stale': stale})
        completed = sum(t['completed_videos'] for t in themes)
        failures = sum(t['failed_videos'] for t in themes)
        statuses = {t['status'] for t in themes}
        if completed == 360 and not failures:
            status = 'COMPLETED'
        elif failures or any(t.get('error') for t in themes):
            status = 'NEEDS_ATTENTION'
        elif any(t['is_stale'] for t in themes):
            status = 'INTERRUPTED'
        elif statuses <= {'NOT_STARTED'}:
            status = 'NOT_STARTED'
        else:
            status = 'IN_PROGRESS'
        elapsed = max((t.get('elapsed_s', 0) for t in themes
                       if isinstance(t.get('elapsed_s', 0), (int, float))), default=0)
        remaining = [t.get('eta_s') for t in themes if t['completed_videos'] < 60]
        eta = 0 if completed == 360 else (max(remaining) if remaining and all(
            isinstance(v, (int, float)) and v >= 0 for v in remaining) else None)
        return {'status': status, 'planned_videos': 360, 'completed_videos': completed,
                'failed_videos': failures, 'elapsed_s': elapsed, 'eta_s': eta,
                'themes': themes, 'independent_review_status': 'PENDING'}

    def collect(self) -> Dict[str, Any]:
        """Collect and harmonize progress across atomic progress file and disk evidence."""
        cat_info = self.load_catalogue()
        baseline = self.load_validation_baseline()
        configs = cat_info["robustness_configs"]

        # 1. Read atomic progress file
        prog_raw = read_json_safe(self.progress_file)
        prog_exists = self.progress_file.is_file()
        prog_malformed = prog_exists and (prog_raw is None or not isinstance(prog_raw, dict))

        progress = prog_raw if isinstance(prog_raw, dict) else {}

        # 2. Disk scan fallback & cross-reference
        disk_data = self.scan_disk_robustness(configs)

        # 3. Status determination & freshness check
        status = progress.get("status", "IDLE" if not prog_exists else "UNKNOWN")
        updated_utc = progress.get("updated_utc")

        # Refresh age & heartbeat check
        refresh_age_s: Optional[float] = None
        is_stale = False
        stale_message: Optional[str] = None

        if updated_utc:
            try:
                if isinstance(updated_utc, (int, float)):
                    dt = datetime.datetime.fromtimestamp(updated_utc, tz=datetime.timezone.utc)
                else:
                    iso_clean = str(updated_utc).replace("Z", "+00:00")
                    dt = datetime.datetime.fromisoformat(iso_clean)
                delta = (utc_now() - dt).total_seconds()
                refresh_age_s = max(0.0, delta)
            except Exception:
                refresh_age_s = None

        supervisor_pid = progress.get("supervisor_pid")
        supervisor_alive = is_pid_alive(supervisor_pid) if supervisor_pid else None

        # Determine stale condition
        if refresh_age_s is not None:
            active_statuses = ("RUNNING", "INITIALIZING", "RUNNING_WINDOW", "ASSESSING_CONFIGURATION", "WINDOW_COMPLETED", "READY")
            # Offline assessment has two bounded 600s subprocesses and no
            # five-second heartbeat; its live process may legitimately be busy.
            stale_limit = 1245.0 if status == "ASSESSING_CONFIGURATION" else 45.0
            if status in active_statuses and refresh_age_s > stale_limit:
                is_stale = True
                stale_message = (
                    f"STALE HEARTBEAT: Supervisor has not updated progress in {int(refresh_age_s)}s. "
                    "Possible checkpoint interruption or paused execution."
                )
            elif supervisor_pid and supervisor_alive is False and status in active_statuses:
                is_stale = True
                stale_message = f"CHECKPOINT INTERRUPTION: Supervisor PID {supervisor_pid} is no longer running."
                status = "INTERRUPTED"

        # Honest completed counts
        completed_runs = disk_data["completed_runs_count"]

        qualified_pairs = progress.get("qualified_pairs")
        if qualified_pairs is None or not isinstance(qualified_pairs, int):
            qualified_pairs = disk_data["qualified_pairs"]

        accepted_pairs = progress.get("independently_accepted_pairs")
        if accepted_pairs is None or not isinstance(accepted_pairs, int):
            accepted_pairs = disk_data["accepted_pairs"]

        failed_attempts = progress.get("failed_attempts", 0)

        # Measured ETA only
        eta_s = progress.get("eta_s")
        elapsed_s = progress.get("elapsed_s")
        eta_display = "Calculating (awaiting sufficient observations)"
        if eta_s is not None and isinstance(eta_s, (int, float)) and eta_s >= 0:
            eta_h = int(eta_s // 3600)
            eta_m = int((eta_s % 3600) // 60)
            eta_sec = int(eta_s % 60)
            if eta_h > 0:
                eta_display = f"{eta_h}h {eta_m}m {eta_sec}s"
            elif eta_m > 0:
                eta_display = f"{eta_m}m {eta_sec}s"
            else:
                eta_display = f"{eta_sec}s"

        elapsed_display = "0s"
        if elapsed_s is not None and isinstance(elapsed_s, (int, float)) and elapsed_s >= 0:
            el_h = int(elapsed_s // 3600)
            el_m = int((elapsed_s % 3600) // 60)
            el_sec = int(elapsed_s % 60)
            if el_h > 0:
                elapsed_display = f"{el_h}h {el_m:02d}m {el_sec:02d}s"
            else:
                elapsed_display = f"{el_m:02d}m {el_sec:02d}s"

        current_run = progress.get("current")
        if not isinstance(current_run, dict):
            current_run = None

        # Harmonize runs ledger with grid
        runs_list = progress.get("runs", [])
        if isinstance(runs_list, list) and len(runs_list) > 0:
            for r in runs_list:
                t = r.get("test")
                m = r.get("mode")
                a = r.get("assistance")
                key = f"{m}_{a}"
                if t in disk_data["grid"] and key in disk_data["grid"][t]["modes"]:
                    cell = disk_data["grid"][t]["modes"][key]
                    if r.get("status"):
                        cell["status"] = r["status"]
                    if r.get("error"):
                        cell["error"] = r["error"]

        return {
            "timestamp_utc": utc_iso(),
            "evidence_revision": self.root.name,
            "phase": "Robustness",
            "active_validation": self.load_active_validation(),
            "video_export": self.load_video_export(),
            "status": status,
            "updated_utc": updated_utc,
            "refresh_age_s": refresh_age_s,
            "is_stale": is_stale,
            "stale_message": stale_message,
            "error": progress.get("error"),
            "progress_file_exists": prog_exists,
            "progress_file_malformed": prog_malformed,
            "planned_runs": 240,
            "completed_runs": completed_runs,
            "qualified_pairs": qualified_pairs,
            "planned_pairs": 120,
            "independently_accepted_pairs": accepted_pairs,
            "failed_attempts": failed_attempts,
            "elapsed_s": elapsed_s,
            "elapsed_display": elapsed_display,
            "eta_s": eta_s,
            "eta_display": eta_display,
            "current": current_run,
            "pids": {
                "supervisor_pid": supervisor_pid,
                "supervisor_alive": supervisor_alive,
                "worker_pid": progress.get("worker_pid"),
                "bridge_pid": progress.get("bridge_pid"),
            },
            "catalogue_sha256": progress.get("catalogue_sha256") or cat_info["catalogue_sha256"],
            "baseline_validation": baseline,
            "contacts": disk_data["contacts"],
            "themes": disk_data["themes"],
            "grid": disk_data["grid"],
            "recent_runs": runs_list[-20:] if isinstance(runs_list, list) else [],
        }


def safe_resolve_evidence_path(root: Path, relative_str: str) -> Optional[Path]:
    """Strictly resolve path inside root without traversal or secret exposure."""
    clean = urllib.parse.unquote(relative_str).replace("\\", "/").strip()
    if clean.startswith("/"):
        clean = clean.lstrip("/")

    if "\0" in clean or ".." in clean.split("/"):
        return None

    target = (root / clean).resolve()
    try:
        if not target.is_relative_to(root.resolve()):
            return None
    except AttributeError:
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return None

    if not target.is_file():
        return None

    ext = target.suffix.lower()
    if ext not in SAFE_EXTENSIONS:
        return None

    forbidden_names = {".env", "id_rsa", "credentials", "secret", ".git"}
    if any(f in target.name.lower() for f in forbidden_names):
        return None

    return target


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CrossGuard Town10HD Robustness Dashboard</title>
<style>
  :root {
    color-scheme: dark;
    --bg-main: #080808; --bg-card: #080808; --bg-subcard: #141414;
    --border: #303030; --border-bright: #999999;
    --text-primary: #f5f5f5; --text-secondary: #b8b8b8; --text-muted: #a0a0a0;
    --accent: #ffffff; --success: #ffffff; --warning: #d0d0d0;
    --danger: #ffffff; --purple: #ffffff; --cyan: #ffffff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg-main); color: var(--text-primary); padding: 32px 40px 64px; line-height: 1.5; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1560px; margin: auto; }
  ::selection { background: #ffffff; color: #080808; }
  a { color: inherit; text-underline-offset: 4px; }
  :focus-visible { outline: 2px solid #ffffff; outline-offset: 4px; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; flex-wrap: wrap; padding-bottom: 24px; }
  .title-group h1 { font-size: 28px; line-height: 1.2; font-weight: 600; letter-spacing: -0.025em; text-wrap: balance; }
  .title-group p { color: var(--text-secondary); margin-top: 10px; max-width: 70ch; }
  .header-badges { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; max-width: 520px; }
  .badge { display: inline-flex; align-items: center; padding: 5px 9px; border: 1px solid var(--border); font-size: 11px; font-weight: 600; letter-spacing: .02em; line-height: 1.4; background: transparent; color: var(--text-secondary); overflow-wrap: anywhere; }
  .badge-running, .badge-review { border-color: #ffffff; color: #ffffff; }
  .badge-danger { background: #ffffff; color: #080808; border-color: #ffffff; }
  .badge-stale { border-style: dashed; color: #ffffff; }
  .study-nav { display: flex; gap: 28px; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 14px 0; margin-bottom: 28px; }
  .study-nav a { text-decoration: none; color: var(--text-secondary); font-size: 13px; }
  .study-nav a:hover { color: #ffffff; text-decoration: underline; }
  .banner-stale { display: flex; gap: 12px; align-items: center; padding: 16px; margin-bottom: 24px; border: 1px dashed #999999; color: #ffffff; }
  .card { padding: 24px 0; background: var(--bg-card); border: 0; border-top: 1px solid var(--border); display: flex; flex-direction: column; min-width: 0; }
  .baseline-card { padding: 0 0 24px; border-top: 0; margin-bottom: 0; }
  .card-label { font-size: 12px; line-height: 1.4; color: var(--text-secondary); font-weight: 500; margin-bottom: 12px; }
  .baseline-card .card-label { font-size: 14px; margin-bottom: 8px; }
  .grid-summary { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); border-block: 1px solid var(--border); margin-bottom: 32px; }
  .grid-summary .card { padding: 22px 24px; border-top: 0; border-right: 1px solid var(--border); }
  .grid-summary .card:first-child { padding-left: 0; }
  .grid-summary .card:last-child { border-right: 0; padding-right: 0; }
  .card-val { font-size: 32px; line-height: 1.15; font-weight: 500; letter-spacing: -.02em; color: #ffffff; font-variant-numeric: tabular-nums; margin-bottom: 12px; }
  .card-sub { font-size: 12px; color: var(--text-muted); }
  .progress-bar-bg { width: 100%; height: 2px; background: #303030; margin-top: 16px; overflow: hidden; }
  .progress-bar-fill { height: 100%; background: #ffffff; }
  .section-title { font-size: 18px; font-weight: 500; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; margin-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
  th, td { padding: 13px 12px; border-bottom: 1px solid var(--border); text-align: left; }
  th { color: var(--text-secondary); font-weight: 500; font-size: 12px; white-space: nowrap; }
  td { color: var(--text-primary); vertical-align: top; }
  th:first-child, td:first-child { padding-left: 0; }
  .table-wrap, .matrix-container, .export-table-wrap { overflow-x: auto; scrollbar-color: #666666 #141414; }
  .table-wrap table { min-width: 780px; }
  .matrix-grid { min-width: 760px; }
  .matrix-grid th, .matrix-grid td { text-align: center; }
  .matrix-grid th:first-child { text-align: left; }
  .cell-row-label { text-align: left !important; font-size: 12px; font-weight: 500; white-space: nowrap; }
  .matrix-cell { width: 100%; min-height: 38px; padding: 8px; border: 1px solid transparent; background: transparent; font: inherit; font-size: 12px; text-align: center; color: #ffffff; cursor: pointer; transition: background 150ms ease-out; }
  .matrix-cell:hover { background: #242424; border-color: #777777; }
  .matrix-cell:active { background: #383838; }
  .cell-pending { color: #a0a0a0; }
  .cell-running { border: 1px solid #ffffff; }
  .cell-failed { background: #ffffff; color: #080808; }
  .cell-incomplete { border: 1px dashed #999999; }
  .cell-contact { background: #202020; border-color: #555555; }
  .contact-pill { padding: 4px 8px; font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; }
  .contact-hit { border: 1px solid #777777; color: #ffffff; }
  .contact-clean { color: var(--text-secondary); }
  .export-panel { margin-bottom: 16px; }
  .export-summary { color: var(--text-primary); margin-bottom: 16px; }
  .export-table { min-width: 700px; }
  .export-current { display: block; color: var(--text-secondary); overflow-wrap: anywhere; margin-top: 6px; }
  .export-note { color: var(--text-muted); margin-top: 16px; font-size: 12px; max-width: 75ch; }
  .export-details summary { padding: 12px 0; color: var(--text-secondary); cursor: pointer; font-size: 13px; }
  .export-details summary:hover { color: #ffffff; }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.8); display: none; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
  .modal { background: #141414; border: 1px solid #777777; width: 100%; max-width: 720px; max-height: 85vh; display: flex; flex-direction: column; }
  .modal-header { padding: 20px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  .modal-body { padding: 24px; overflow-y: auto; color: var(--text-secondary); }
  .modal-close { background: transparent; color: #ffffff; border: 1px solid #777777; width: 36px; height: 36px; font-size: 22px; cursor: pointer; }
  .modal-close:hover { background: #303030; }
  .evidence-link { color: #ffffff; overflow-wrap: anywhere; }
  pre { background: #080808; border: 1px solid var(--border); padding: 16px; overflow-x: auto; font-size: 12px; color: #e0e0e0; }
  @media (max-width: 900px) { body { padding: 24px; } .two-col { grid-template-columns: 1fr; gap: 0; } .header-badges { max-width: none; } .grid-summary { grid-template-columns: repeat(2,minmax(0,1fr)); } .grid-summary .card:nth-child(2) { border-right: 0; } .grid-summary .card:nth-child(n+3) { border-top: 1px solid var(--border); } .grid-summary .card:nth-child(3) { padding-left: 0; } }
  @media (max-width: 520px) { body { padding: 20px 16px 40px; } .title-group h1 { font-size: 24px; } .study-nav { gap: 20px; flex-wrap: wrap; } .grid-summary .card { padding: 18px 12px; } .card-val { font-size: 26px; } .header-badges { gap: 6px; } .badge { font-size: 10px; } .modal-overlay { padding: 12px; } .modal-body { padding: 16px; } }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; scroll-behavior: auto !important; } }
</style>
</head>
<body>

<header>
  <div class="title-group">
    <h1>CrossGuard Town10HD Robustness Dashboard</h1>
    <p>60 configurations &times; 2 receiver modes &times; 2 assistance states &middot; Read-only study monitor</p>
  </div>
  <div class="header-badges">
    <div id="statusBadge" class="badge badge-idle">STATUS: IDLE</div>
    <div id="heartbeatBadge" class="badge badge-info">HEARTBEAT: --</div>
    <div id="pidsBadge" class="badge badge-info">PID: --</div>
  </div>
</header>
<nav class="study-nav" aria-label="Study sections"><a href="#overview">Overview</a><a href="#exports">Video exports</a><a href="#scenarios">Scenarios</a><a href="#evidence">Run evidence</a></nav>
<main>

<div id="staleBanner" class="banner-stale" style="display: none;">
  <span>Notice:</span>
  <span id="staleMessage">Interruption or stale heartbeat detected.</span>
</div>

<!-- Baseline Card -->

<div id="overview" class="card baseline-card">
  <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
    <div>
      <div id="baselineRevisionLabel" class="card-label" style="color: var(--accent);">Validated Baseline &mdash; Distinct &amp; Frozen</div>
      <div id="baselineDetailText" style="font-size: 0.95rem; font-weight: 600; color: #ffffff;">
        Loading validation baseline status...
      </div>
    </div>
    <span id="baselineBadge" class="badge" style="background: transparent; color: #ffffff; border: 1px solid #777777;">
      GATE PENDING
    </span>
  </div>
</div>

<!-- 4 Overall Progress Cards -->
<div class="grid-summary">
  <div class="card">
    <div class="card-label">Recorded runs</div>
    <div class="card-val" id="valPhysical">0 / 240</div>
    <div class="card-sub" id="subPhysical">0.0% of batch complete</div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="barPhysical" style="width: 0%; background: var(--accent);"></div>
    </div>
  </div>
  
  <div class="card">
    <div class="card-label">Qualified pairs</div>
    <div class="card-val" id="valQualified">0 / 120</div>
    <div class="card-sub" id="subQualified">Protocol-qualified pairs</div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="barQualified" style="width: 0%; background: var(--purple);"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-label">Reviewed pairs</div>
    <div class="card-val" id="valAccepted">0 / 120</div>
    <div class="card-sub">Model review; not human validation</div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="barAccepted" style="width: 0%; background: var(--success);"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-label">Runtime and remaining</div>
    <div class="card-val" style="font-size: 1.25rem; font-weight: 600;" id="valEta">ETA: Calculating</div>
    <div class="card-sub" id="valElapsed">Elapsed: 0s &bull; Failures: 0</div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="barEta" style="width: 0%; background: var(--warning);"></div>
    </div>
  </div>
</div>

<section id="exports" class="card export-panel" aria-labelledby="exportTitle">
  <h2 class="section-title" id="exportTitle">Video export <span id="exportStatus" class="badge badge-idle">NOT STARTED</span></h2>
  <p class="export-summary" id="exportSummary" aria-live="polite">Waiting for export checkpoints. Planned: 240 run videos and 120 OFF/ON comparisons.</p>
  <details class="export-details"><summary>View six scenario workers</summary><div class="export-table-wrap">
    <table class="export-table"><thead><tr><th scope="col">Scenario worker</th><th scope="col">Verified videos</th><th scope="col">Status / current export</th><th scope="col">Elapsed / remaining</th></tr></thead>
      <tbody id="exportRows"></tbody>
    </table>
  </div>
  </details>
  <p class="export-note">Export uses saved frames. Physical runs and independent acceptance remain separate; independent checking begins after all exports finish.</p>
</section>
<div class="panel" id="activeValidationPanel" style="display:none; margin-bottom:20px">
  <div class="section-title">Active Guardrail Validation — Separate from Robustness</div>
  <div id="activeValidationText"></div>
</div>
<div class="two-col">
  <!-- Active Run Card -->
  <div class="card">
    <div class="section-title">Current run</div>
    <div id="activeRunContent" style="font-size: 0.88rem; color: var(--text-secondary);">
      <div style="padding: 1.5rem 0; text-align: center; color: var(--text-muted);">
        No active simulation run currently reported.
      </div>
    </div>
  </div>

  <!-- Contacts Outcome Summary -->
  <div class="card">
    <div class="section-title">Recorded contacts</div>
    <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.25rem;">
      <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.4rem;">
        <span style="font-size: 0.85rem; color: var(--text-secondary);">Pedestrian Impact:</span>
        <span id="contactPed" class="contact-pill contact-clean">0 recorded</span>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.4rem;">
        <span style="font-size: 0.85rem; color: var(--text-secondary);">Follower A/B Bumper Collision:</span>
        <span id="contactAB" class="contact-pill contact-clean">0 recorded</span>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.4rem;">
        <span style="font-size: 0.85rem; color: var(--text-secondary);">Follower B/C Bumper Collision:</span>
        <span id="contactBC" class="contact-pill contact-clean">0 recorded</span>
      </div>
      <div style="font-size: 0.74rem; color: var(--text-muted); margin-top: 0.2rem;">
        Contacts derived from results.json schema. Unknown or uncompleted runs remain uncounted.
      </div>
    </div>
  </div>
</div>

<!-- Six Themes Breakdown -->
<section id="scenarios" class="card">
  <h2 class="section-title">Scenario progress</h2>
  <div class="table-wrap"><table>
    <thead>
      <tr>
        <th>Scenario</th>
        <th>Physical Runs</th>
        <th>Qualified Pairs</th>
        <th>Accepted Pairs</th>
        <th>Failures</th>
        <th>Contacts (Ped / AB / BC)</th>
      </tr>
    </thead>
    <tbody id="themesTableBody">
      <!-- Generated via JS -->
    </tbody>
  </table></div>
</section>

<!-- 60 Configuration x 4 Modes Matrix -->
<section id="evidence" class="card">
  <div class="section-title">
    <span>Run evidence &middot; 240 recorded windows</span>
    <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">
      Click cell to inspect evidence &bull; Auto-refresh 3.5s
    </span>
  </div>
  <div class="matrix-container">
    <table class="matrix-grid">
      <thead>
        <tr>
          <th style="width: 220px; text-align: left;">Configuration</th>
          <th>Automatic OFF</th>
          <th>Automatic ON</th>
          <th>Retrofit OFF</th>
          <th>Retrofit ON</th>
        </tr>
      </thead>
      <tbody id="matrixTableBody">
        <!-- Generated via JS -->
      </tbody>
    </table>
  </div>
</section>
</main>

<!-- Modal for Inspection -->
<div id="detailModal" class="modal-overlay">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <div class="modal-header">
      <h3 id="modalTitle" style="font-size: 1rem; color: #fff;">Run Evidence Details</h3>
      <button class="modal-close" aria-label="Close evidence details" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody">
      Loading...
    </div>
  </div>
</div>

<script>
let lastData = null;
let lastInspection = null;

async function refreshDashboard() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error('Status HTTP ' + res.status);
    const data = await res.json();
    lastData = data;
    render(data);
  } catch (err) {
    document.getElementById('heartbeatBadge').className = 'badge badge-danger';
    document.getElementById('heartbeatBadge').textContent = 'SERVER DISCONNECTED';
  }
}

function exportDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return 'Estimating';
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m ${Math.floor(seconds % 60)}s`;
}

function renderVideoExport(value) {
  const e = value || {status: 'NOT_STARTED', completed_videos: 0, planned_videos: 360, themes: []};
  const badge = document.getElementById('exportStatus');
  badge.textContent = e.status.replaceAll('_', ' ');
  badge.className = 'badge ' + (e.status === 'COMPLETED' ? 'badge-info' :
    e.status === 'IN_PROGRESS' ? 'badge-running' : e.status === 'NOT_STARTED' ? 'badge-idle' : 'badge-danger');
  document.getElementById('exportSummary').textContent =
    `${e.completed_videos} / 360 verified videos (${(100 * e.completed_videos / 360).toFixed(1)}%) · ` +
    `Elapsed ${exportDuration(e.elapsed_s)} · Remaining ${exportDuration(e.eta_s)} · Failures ${e.failed_videos || 0}`;
  const rows = document.getElementById('exportRows');
  rows.replaceChildren();
  for (const theme of e.themes || []) {
    const row = document.createElement('tr');
    const title = document.createElement('td');
    title.textContent = `${theme.theme} · ${theme.name}`;
    const count = document.createElement('td');
    count.textContent = `${theme.completed_videos} / 60`;
    const state = document.createElement('td');
    state.textContent = theme.is_stale ? 'Checkpoint stale / worker interrupted' : String(theme.status).replaceAll('_', ' ');
    const detail = document.createElement('span');
    detail.className = 'export-current';
    const current = theme.current;
    detail.textContent = theme.error || (typeof current === 'object' && current !== null
      ? Object.entries(current).map(([key, value]) => `${key}: ${value}`).join(' · ') : current || '');
    state.appendChild(detail);
    const time = document.createElement('td');
    time.textContent = `${exportDuration(theme.elapsed_s)} / ${exportDuration(theme.eta_s)}`;
    row.append(title, count, state, time);
    rows.appendChild(row);
  }
}

function render(data) {
  renderVideoExport(data.video_export);
  // 1. Header Badges
  const badge = document.getElementById('statusBadge');
  const activeValidation = data.active_validation;
  badge.textContent = 'STATUS: ' + data.status;
  if (data.status === 'RUNNING') badge.className = 'badge badge-running';
  else if (data.status === 'AWAITING_CODEX_INDEPENDENT_REVIEW') badge.className = 'badge badge-review';
  else if (data.status === 'STOPPED' || data.status === 'FAILED') badge.className = 'badge badge-danger';
  else badge.className = 'badge badge-idle';
  const validationPanel = document.getElementById('activeValidationPanel');
  validationPanel.style.display = activeValidation ? 'block' : 'none';
  if (activeValidation) {
    const v = activeValidation;
    const c = v.current || {};
    document.getElementById('activeValidationText').textContent =
      `${v.revision || ''}: ${v.status || 'UNKNOWN'} | ${v.completed_runs ?? '?'} / 28 recorded | ` +
      `${c.test || ''} ${c.mode || ''} ${c.assistance || ''} | Simulation: ${Number(c.simulation_s || 0).toFixed(2)}s | ` +
      `Controller ${v.supervisor_alive ? 'alive' : 'not running'}. The five prior robustness records remain separate. ${v.error || ''}`;
    badge.textContent = 'VALIDATION: ' + (v.status || 'UNKNOWN');
    badge.className = v.supervisor_alive ? 'badge badge-running' : 'badge badge-idle';
  }

  const hb = document.getElementById('heartbeatBadge');
  if (data.refresh_age_s !== null && data.refresh_age_s !== undefined) {
    hb.textContent = 'HEARTBEAT: ' + Math.round(data.refresh_age_s) + 's AGO';
    hb.className = data.is_stale ? 'badge badge-stale' : 'badge badge-info';
  } else {
    hb.textContent = 'HEARTBEAT: PENDING';
    hb.className = 'badge badge-idle';
  }

  const pids = document.getElementById('pidsBadge');
  const sp = activeValidation?.supervisor_pid || data.pids?.supervisor_pid || '--';
  const wp = data.pids?.worker_pid || '--';
  pids.textContent = `SUPERVISOR:${sp} | WORKER:${wp}`;
  if (activeValidation?.updated_utc) {
    const age = Math.max(0, Math.round((Date.now() - Date.parse(activeValidation.updated_utc)) / 1000));
    hb.textContent = `VALIDATION HEARTBEAT: ${age}s AGO`;
    hb.className = activeValidation.supervisor_alive ? 'badge badge-info' : 'badge badge-stale';
  }

  // Stale Banner
  const banner = document.getElementById('staleBanner');
  if (data.error || (data.is_stale && data.stale_message)) {
    banner.style.display = 'flex';
    document.getElementById('staleMessage').textContent = data.error || data.stale_message;
  } else {
    banner.style.display = 'none';
  }

  // Baseline Rendering (missing gate displays unknown/not passed, never default true/28)
  const b = data.baseline_validation;
  document.getElementById('baselineRevisionLabel').textContent =
    `Validated Baseline (${data.evidence_revision || 'unknown revision'}) — Distinct & Frozen`;
  const bText = document.getElementById('baselineDetailText');
  const bBadge = document.getElementById('baselineBadge');
  if (b && b.passed && b.completed_runs !== null) {
    bText.innerHTML = `Passed: ${b.completed_runs}/28 Physical Windows Accepted &nbsp;&bull;&nbsp; ${b.qualified_pairs}/14 Matched Pairs Qualified &nbsp;&bull;&nbsp; Gate PASSED`;
    bBadge.textContent = 'BASELINE SEALED';
    bBadge.style.background = 'transparent';
    bBadge.style.color = '#ffffff';
    bBadge.style.border = '1px solid #777777';
  } else {
    bText.innerHTML = `Notice: Validation Baseline: UNKNOWN / NOT PASSED (Gate missing or unverified)`;
    bBadge.textContent = 'GATE NOT PASSED';
    bBadge.style.background = '#ffffff';
    bBadge.style.color = '#080808';
    bBadge.style.border = '1px solid #ffffff';
  }

  // 2. Summary Cards
  const comp = data.completed_runs || 0;
  const pctPhys = ((comp / 240) * 100).toFixed(1);
  document.getElementById('valPhysical').textContent = `${comp} / 240`;
  document.getElementById('subPhysical').textContent = `${pctPhys}% of batch complete`;
  document.getElementById('barPhysical').style.width = pctPhys + '%';

  const qual = data.qualified_pairs || 0;
  const pctQual = ((qual / 120) * 100).toFixed(1);
  document.getElementById('valQualified').textContent = `${qual} / 120`;
  document.getElementById('subQualified').textContent = `${pctQual}% pairs qualified`;
  document.getElementById('barQualified').style.width = pctQual + '%';

  const acc = data.independently_accepted_pairs || 0;
  const pctAcc = ((acc / 120) * 100).toFixed(1);
  document.getElementById('valAccepted').textContent = `${acc} / 120`;
  document.getElementById('barAccepted').style.width = pctAcc + '%';

  document.getElementById('valEta').textContent = 'ETA: ' + (data.eta_display || 'Calculating');
  document.getElementById('valElapsed').textContent = `Elapsed: ${data.elapsed_display || '0s'} • Failures: ${data.failed_attempts || 0}`;

  // 3. Active Run Card
  const activeDiv = document.getElementById('activeRunContent');
  if (data.current) {
    const c = data.current;
    activeDiv.innerHTML = `
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem;">
        <div><strong style="color:#fff;">Test:</strong> ${c.test || 'N/A'}</div>
        <div><strong style="color:#fff;">Mode:</strong> ${c.mode || 'N/A'}</div>
        <div><strong style="color:#fff;">Assistance:</strong> ${c.assistance || 'N/A'}</div>
        <div><strong style="color:#fff;">Sim Time:</strong> ${(c.simulation_s !== undefined ? c.simulation_s.toFixed(2) + 's / 20.0s' : 'Starting')}</div>
      </div>
    `;
  } else {
    activeDiv.innerHTML = '<div style="padding: 1.5rem 0; text-align: center; color: var(--text-muted);">No active simulation window reported (Supervisor IDLE or between runs).</div>';
  }

  // 4. Contacts Outcomes
  const cp = data.contacts?.pedestrian || 0;
  const cab = data.contacts?.A_B || 0;
  const cbc = data.contacts?.B_C || 0;
  const setContact = (id, count) => {
    const el = document.getElementById(id);
    el.textContent = `${count} recorded`;
    el.className = count > 0 ? 'contact-pill contact-hit' : 'contact-pill contact-clean';
  };
  setContact('contactPed', cp);
  setContact('contactAB', cab);
  setContact('contactBC', cbc);

  // 5. Themes Table
  const tbody = document.getElementById('themesTableBody');
  let tHtml = '';
  for (const [code, t] of Object.entries(data.themes || {})) {
    tHtml += `
      <tr>
        <td><strong>${code}</strong> &mdash; ${t.name}</td>
        <td>${t.completed_runs} / ${t.total_runs}</td>
        <td>${t.qualified_pairs} / ${t.total_configs * 2}</td>
        <td>${t.accepted_pairs} / ${t.total_configs * 2}</td>
        <td>${t.failed_runs}</td>
        <td>${t.contacts.pedestrian} / ${t.contacts.A_B} / ${t.contacts.B_C}</td>
      </tr>
    `;
  }
  tbody.innerHTML = tHtml;

  // 6. Matrix Grid
  const mbody = document.getElementById('matrixTableBody');
  let mHtml = '';
  for (const [testId, row] of Object.entries(data.grid || {})) {
    mHtml += `<tr><td class="cell-row-label">${testId}</td>`;
    for (const key of ['Automatic_OFF', 'Automatic_ON', 'Retrofit_OFF', 'Retrofit_ON']) {
      const cell = row.modes[key] || { status: 'PENDING' };
      let cClass = 'cell-pending';
      let symbol = cell.status;
      if (cell.status === 'COMPLETED') {
        cClass = 'cell-completed';
        const hits = [];
        if (cell.contacts?.pedestrian === true) hits.push('PED');
        if (cell.contacts?.A_B === true) hits.push('AB');
        if (cell.contacts?.B_C === true) hits.push('BC');
        symbol = hits.length > 0 ? hits.join(' ') : 'No contact';
      } else if (cell.status === 'RUNNING') {
        cClass = 'cell-running';
        symbol = 'Running';
      } else if (cell.status === 'FAILED') {
        cClass = 'cell-failed';
        symbol = 'Failed';
      } else if (cell.status === 'INCOMPLETE') {
        cClass = 'cell-incomplete';
        symbol = 'Partial';
      }
      const contactClass = cell.contacts?.pedestrian || cell.contacts?.A_B || cell.contacts?.B_C ? " cell-contact" : "";
      mHtml += `<td><button type="button" data-test="${testId}" data-mode="${key}" class="matrix-cell ${cClass}${contactClass}" onclick="inspectCell('${testId}', '${key}')" aria-label="Inspect ${testId}, ${key.replaceAll('_', ' ')}: ${symbol}">${symbol}</button></td>`;
    }
    mHtml += '</tr>';
  }
  mbody.innerHTML = mHtml;
}

function inspectCell(testId, modeKey) {
  if (!lastData) return;
  lastInspection = {testId, modeKey};
  const row = lastData.grid[testId];
  const cell = row?.modes[modeKey];
  if (!cell) return;
  
  document.getElementById('modalTitle').textContent = `${testId} — ${modeKey.replace('_', ' ')}`;
  const rel = cell.relative_path || '';
  
  let content = `
    <div style="margin-bottom: 0.8rem;">
      <strong>Status:</strong> ${cell.status}<br>
      <strong>Relative Path:</strong> <code>${rel || '(not created)'}</code>
    </div>
    <div style="margin-bottom: 0.8rem;">
      <strong>Contacts:</strong>
      Pedestrian: ${cell.contacts?.pedestrian === true ? 'YES' : (cell.contacts?.pedestrian === false ? 'None' : 'Unknown')},
      A/B: ${cell.contacts?.A_B === true ? 'YES' : (cell.contacts?.A_B === false ? 'None' : 'Unknown')},
      B/C: ${cell.contacts?.B_C === true ? 'YES' : (cell.contacts?.B_C === false ? 'None' : 'Unknown')}
    </div>
  `;
  
  if (cell.reasons && cell.reasons.length > 0) {
    content += `
      <div style="margin-bottom: 0.8rem; color: #ffffff;">
        <strong>Verification Notes:</strong>
        <ul style="margin-top: 0.3rem; padding-left: 1.2rem;">
          ${cell.reasons.map(r => `<li>${r}</li>`).join('')}
        </ul>
      </div>
    `;
  }
  
  if (rel) {
    content += `
      <div style="margin-top: 1rem;">
        <strong>Restricted Evidence Links:</strong>
        <ul style="margin-top: 0.4rem; padding-left: 1.2rem; line-height: 1.6;">
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/results.json">results.json</a></li>
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/terminal_status.json">terminal_status.json</a></li>
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/camera_manifest.json">camera_manifest.json</a></li>
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/requested_config.json">requested_config.json</a></li>
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/placement_audit.json">placement_audit.json</a></li>
          <li><a class="evidence-link" target="_blank" href="/evidence/${rel}/physical_timeline.json">physical_timeline.json</a></li>
        </ul>
      </div>
    `;
  }
  
  document.getElementById('modalBody').innerHTML = content;
  document.getElementById('detailModal').style.display = 'flex';
  document.querySelector('.modal-close').focus();
}

function closeModal() {
  document.getElementById('detailModal').style.display = 'none';
  if (lastInspection) {
    document.querySelector(`button[data-test="${lastInspection.testId}"][data-mode="${lastInspection.modeKey}"]`)?.focus();
  }
}

window.addEventListener('click', (e) => {
  if (e.target.id === 'detailModal') closeModal();
});

window.addEventListener('keydown', (event) => {
  const dialog = document.getElementById('detailModal');
  if (dialog.style.display !== 'flex') return;
  if (event.key === 'Escape') closeModal();
  if (event.key === 'Tab') {
    const items = [...dialog.querySelectorAll('button, a[href]')];
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

// Auto-refresh every 3.5 seconds
refreshDashboard();
setInterval(refreshDashboard, 3500);
</script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Restricted read-only HTTP handler for dashboard and evidence inspection."""

    collector: DashboardDataCollector
    evidence_root: Path

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_json(self, status_code: int, data: Any) -> None:
        raw = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            raw = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/status":
            data = self.collector.collect()
            self.send_json(200, data)
            return

        if path.startswith("/evidence/"):
            rel_part = path[len("/evidence/"):]
            target = safe_resolve_evidence_path(self.evidence_root, rel_part)
            if target is None:
                self.send_error(403, "Access forbidden or invalid evidence path")
                return
            if not target.is_file():
                self.send_error(404, "Evidence file not found")
                return
            self.serve_file(target)
            return

        if path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            target_rel = query.get("path", [""])[0]
            target = safe_resolve_evidence_path(self.evidence_root, target_rel)
            if target is None:
                self.send_error(403, "Access forbidden or invalid file path")
                return
            if not target.is_file():
                self.send_error(404, "File not found")
                return
            self.serve_file(target)
            return

        if path == "/api/log":
            query = urllib.parse.parse_qs(parsed.query)
            log_name = query.get("name", ["supervisor.log"])[0]
            allowed_logs = {
                "supervisor.log": self.evidence_root / "Reports" / "Robustness_Unattended" / "supervisor.log",
                "progress.json": self.evidence_root / "Reports" / "Robustness_Unattended" / "progress.json",
            }
            target_path = allowed_logs.get(log_name)
            if not target_path or not target_path.is_file():
                self.send_error(404, f"Log {log_name} not found")
                return
            self.serve_file(target_path)
            return

        self.send_error(404, "Endpoint not found")

    def serve_file(self, target: Path) -> None:
        mime, _ = mimetypes.guess_type(str(target))
        if not mime:
            mime = "text/plain; charset=utf-8" if target.suffix in (".log", ".txt", ".md") else "application/octet-stream"
        
        try:
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(500, f"Error reading file: {exc}")


def run_dashboard_server(host: str, port: int, root: Path) -> None:
    root_resolved = root.resolve()
    if not root_resolved.is_dir():
        print(f"[CrossGuard Dashboard] ERROR: Evidence root directory does not exist: {root_resolved}", file=sys.stderr)
        sys.exit(1)

    collector = DashboardDataCollector(root_resolved)

    class CustomHandler(DashboardRequestHandler):
        pass

    CustomHandler.collector = collector
    CustomHandler.evidence_root = root_resolved

    try:
        server = HTTPServer((host, port), CustomHandler)
    except OSError as err:
        if err.errno in (errno.EADDRINUSE, 10048) or "address already in use" in str(err).lower():
            print(f"[CrossGuard Dashboard] PORT CONFLICT: Port {port} on {host} is already in use.", file=sys.stderr)
            print(f"[CrossGuard Dashboard] Fail cleanly as requested. Please specify another port with --port or stop the conflicting service.", file=sys.stderr)
            sys.exit(1)
        raise

    print(f"[CrossGuard Dashboard] Listening on http://{host}:{port}/ (Root: {root_resolved})")
    print(f"[CrossGuard Dashboard] Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[CrossGuard Dashboard] Shutting down cleanly.")
    finally:
        server.server_close()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CrossGuard Town10HD Robustness Live Progress Dashboard (HTTP 8766 read-only)"
    )
    workspace_default = Path(__file__).resolve().parents[2] / "Evidence" / "Study_02_Town10HD_Continuation" / "Revision_03"
    parser.add_argument(
        "--root",
        type=Path,
        default=workspace_default,
        help="Path to the evidence root directory (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Loopback host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Loopback port to bind to (default: 8766)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    run_dashboard_server(args.host, args.port, args.root)


if __name__ == "__main__":
    main()
