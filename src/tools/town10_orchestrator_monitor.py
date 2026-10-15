"""CrossGuard Town10HD Standalone Orchestrator Monitor.

Durable, read-only external monitor that tracks simulation batch progress,
bridge preservation, dashboard liveness, and failure diagnostics without
requiring Codex or AI quota per poll.

Key invariants:
- NO CARLA imports, calls, or dependencies.
- Read-only inspection of progress files and process identity.
- No simulation controls, restarts, or bridge queue writes.
- Live monitor heartbeat NEVER implies a running batch.
- SO_EXCLUSIVEADDRUSE on Windows for loopback status endpoint (port 8768).
"""

from __future__ import annotations

import argparse
import datetime
import errno
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

LOGGER = logging.getLogger("town10_orchestrator_monitor")


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def read_json_safe(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(content)
    except Exception:
        return None


def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if psutil is not None:
        try:
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


def get_process_create_time(pid: Optional[int]) -> Optional[float]:
    if not pid or pid <= 0:
        return None
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            return p.create_time()
        except Exception:
            return None
    return None


def get_listening_port_owner(port: int, host: str = "127.0.0.1") -> Optional[int]:
    """Find the owning PID listening on a specified port."""
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
                    if host in ("*", "0.0.0.0", "") or conn.laddr.ip in (host, "0.0.0.0", "127.0.0.1", "::", "::1"):
                        return conn.pid
        except Exception:
            pass
    return None


class MonitorLock:
    """PID + creation timestamp file lock to prevent duplicate orchestrator monitor instances."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.acquired = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_file():
            data = read_json_safe(self.lock_path)
            if isinstance(data, dict):
                existing_pid = data.get("pid")
                existing_create_time = data.get("create_time")
                if existing_pid and is_pid_alive(existing_pid):
                    cur_create = get_process_create_time(existing_pid)
                    if cur_create is not None and existing_create_time is not None:
                        if abs(cur_create - existing_create_time) < 1.0:
                            raise RuntimeError(
                                f"Duplicate monitor detected: PID {existing_pid} (created {existing_create_time}) "
                                f"is already actively holding lock {self.lock_path}"
                            )
                    elif existing_pid != os.getpid():
                        raise RuntimeError(
                            f"Duplicate monitor detected: PID {existing_pid} is alive and holds lock {self.lock_path}"
                        )

        # Write current lock atomically
        my_pid = os.getpid()
        my_create = get_process_create_time(my_pid) or time.time()
        payload = {
            "pid": my_pid,
            "create_time": my_create,
            "started_utc": utc_iso(),
        }
        tmp_path = self.lock_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.lock_path)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.lock_path.is_file():
                data = read_json_safe(self.lock_path)
                if isinstance(data, dict) and data.get("pid") == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
        except Exception as exc:
            LOGGER.warning("Could not remove lock file on release: %s", exc)
        self.acquired = False


class ExclusiveHTTPServer(HTTPServer):
    """HTTP server with SO_EXCLUSIVEADDRUSE and allow_reuse_address=False on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception as exc:
                LOGGER.warning("Failed to set SO_EXCLUSIVEADDRUSE: %s", exc)
        super().server_bind()


class MonitorRequestHandler(BaseHTTPRequestHandler):
    """Restricted read-only HTTP handler for orchestrator status."""

    monitor: "Town10OrchestratorMonitor"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default noisy stderr logging

    def send_json(self, status_code: int, data: Any) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, status_code: int, html_text: str) -> None:
        payload = html_text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        clean_path = self.path.split("?")[0]
        if clean_path == "/api/status":
            status_data = self.monitor.get_current_status_data()
            self.send_json(200, status_data)
            return

        if clean_path in ("/", "/index.html"):
            html_page = self.monitor.render_status_html()
            self.send_html(200, html_page)
            return

        self.send_error(404, "Endpoint not found")

    def do_POST(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self) -> None:
        self.send_error(405, "Method Not Allowed")


class Town10OrchestratorMonitor:
    """Durable read-only orchestrator monitor for Town10HD robustness batches."""

    def __init__(
        self,
        study_root: Path,
        orchestrator_dir: Optional[Path] = None,
        host: str = "127.0.0.1",
        port: int = 8768,
        dashboard_port: int = 8766,
        expected_bridge_pid: int = 2860,
        expected_bridge_created: float = 1791618082.6745353,
        expected_bridge_port: int = 8007,
        poll_interval_s: float = 5.0,
    ):
        self.study_root = study_root.resolve()
        self.orchestrator_dir = (
            orchestrator_dir.resolve()
            if orchestrator_dir
            else self.study_root / "Reports" / "Robustness_Unattended" / "Orchestrator"
        )
        self.host = host
        self.port = port
        self.dashboard_port = dashboard_port
        self.expected_bridge_pid = expected_bridge_pid
        self.expected_bridge_created = expected_bridge_created
        self.expected_bridge_port = expected_bridge_port
        self.poll_interval_s = poll_interval_s

        self.reports_dir = self.study_root / "Reports" / "Robustness_Unattended"
        self.progress_file = self.reports_dir / "progress.json"
        self.checkpoint_file = self.reports_dir / "checkpoint.json"
        self.supervisor_log_file = self.reports_dir / "supervisor.log"
        self.diagnosis_file = self.reports_dir / "failure_independent_diagnosis.json"

        self.heartbeat_file = self.orchestrator_dir / "heartbeat.json"
        self.events_file = self.orchestrator_dir / "events.jsonl"
        self.lock_file = self.orchestrator_dir / "monitor.lock"

        self.monitor_pid = os.getpid()
        self.monitor_created = get_process_create_time(self.monitor_pid) or time.time()
        self.lock = MonitorLock(self.lock_file)

        self._server: Optional[ExclusiveHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_state_signature: Optional[Tuple[Any, ...]] = None
        self._cached_status: Dict[str, Any] = {}
        self._lock_state = threading.Lock()

    def collect_state(self) -> Dict[str, Any]:
        """Read existing evidence files and process states to build status payload."""
        now_utc = utc_iso()

        # 1. Process identity: Bridge
        bridge_alive = is_pid_alive(self.expected_bridge_pid)
        bridge_create_time = get_process_create_time(self.expected_bridge_pid)
        bridge_owner_port = get_listening_port_owner(self.expected_bridge_port, host="127.0.0.1")
        bridge_port_ok = (bridge_owner_port == self.expected_bridge_pid)

        # Bridge preservation evaluation
        bridge_preserved = False
        if bridge_alive and bridge_port_ok:
            if bridge_create_time is not None:
                bridge_preserved = abs(bridge_create_time - self.expected_bridge_created) < 2.0
            else:
                bridge_preserved = True

        # 2. Process identity: Dashboard
        dash_owner_pid = get_listening_port_owner(self.dashboard_port, host="127.0.0.1")
        dash_alive = is_pid_alive(dash_owner_pid) if dash_owner_pid else False

        # 3. Read progress & checkpoint
        prog_exists = self.progress_file.is_file()
        prog_data = read_json_safe(self.progress_file) if prog_exists else None
        prog_malformed = prog_exists and (prog_data is None or not isinstance(prog_data, dict))

        check_exists = self.checkpoint_file.is_file()
        check_data = read_json_safe(self.checkpoint_file) if check_exists else None

        supervisor_pid: Optional[int] = None
        if isinstance(prog_data, dict) and prog_data.get("supervisor_pid"):
            try:
                supervisor_pid = int(prog_data["supervisor_pid"])
            except (ValueError, TypeError):
                pass
        elif isinstance(check_data, dict) and check_data.get("supervisor_pid"):
            try:
                supervisor_pid = int(check_data["supervisor_pid"])
            except (ValueError, TypeError):
                pass

        supervisor_alive = is_pid_alive(supervisor_pid) if supervisor_pid else False

        # 4. Determine batch status
        batch_last_update: Optional[str] = None
        raw_batch_status: Optional[str] = None
        latest_failure: Optional[str] = None
        catalogue_sha = "6f53d7f6eb8330d77131ebca660f5c467ce71939c0791f5fcb02d3073c56357a"

        if not prog_exists:
            batch_status = "MISSING_PROGRESS"
        elif prog_malformed:
            batch_status = "MALFORMED_PROGRESS"
        else:
            assert isinstance(prog_data, dict)
            raw_batch_status = prog_data.get("status", "UNKNOWN")
            batch_last_update = prog_data.get("updated_utc")
            latest_failure = prog_data.get("error")
            catalogue_sha = prog_data.get("catalogue_sha256") or catalogue_sha

            active_statuses = ("RUNNING", "INITIALIZING", "RUNNING_WINDOW", "ASSESSING_CONFIGURATION", "WINDOW_COMPLETED", "READY")
            if raw_batch_status in active_statuses:
                if not supervisor_alive:
                    batch_status = "INTERRUPTED"
                else:
                    batch_status = raw_batch_status
            else:
                batch_status = raw_batch_status

        # If checkpoint has error or status when progress doesn't
        if not latest_failure and isinstance(check_data, dict):
            latest_failure = check_data.get("error")
            if not batch_last_update:
                batch_last_update = check_data.get("updated_utc")

        # Diagnosis file check
        if not latest_failure and self.diagnosis_file.is_file():
            diag = read_json_safe(self.diagnosis_file)
            if isinstance(diag, dict) and diag.get("diagnosis"):
                latest_failure = diag["diagnosis"]

        # Repair status check
        repair_file = self.reports_dir / "Repair" / "status.json"
        repair_info = read_json_safe(repair_file) if repair_file.is_file() else None

        # Parse counts
        planned = 240
        recorded = 0
        deferred = 0
        qualified = 0
        independently_reviewed = 0
        failed = 0

        if isinstance(prog_data, dict):
            planned = prog_data.get("planned_runs", 240)
            recorded = prog_data.get("completed_runs", 0)
            deferred = prog_data.get("deferred_runs", 0)
            if recorded == 0 and "runs" in prog_data and isinstance(prog_data["runs"], list):
                recorded = len(prog_data["runs"])
            qualified = prog_data.get("qualified_pairs", 0)
            independently_reviewed = prog_data.get("independently_accepted_pairs", 0)
            failed = prog_data.get("failed_attempts", 0)
        elif isinstance(check_data, dict):
            recorded = check_data.get("completed_runs", 0)
            deferred = check_data.get("deferred_runs", 0)
            failed = check_data.get("failed_attempts", 0)

        # Action required determination
        if batch_status == "BLOCKED_SHARED_WEATHER_FAULT":
            action_required = (
                "BLOCKED_SHARED_WEATHER: Weather mismatch confirmed on multiple configurations. "
                "Simulator execution blocked while repair agent diagnoses and stages repair."
            )
        elif batch_status == "FAILED_SAFE_STOP":
            action_required = (
                "REPAIR_REQUIRED: Weather mismatch detected (expected cloud10/sun70/az180 vs actual 20/45/220). "
                "Bridge stopped. Human authorization of concrete repair/revalidation plan required before any batch resume."
            )
        elif batch_status == "INTERRUPTED":
            action_required = (
                "INVESTIGATION_REQUIRED: Batch supervisor process terminated unexpectedly without a safe stop."
            )
        elif batch_status == "MISSING_PROGRESS":
            action_required = "INSPECTION_REQUIRED: progress.json file is missing."
        elif batch_status == "MALFORMED_PROGRESS":
            action_required = "INSPECTION_REQUIRED: progress.json file is malformed."
        elif batch_status == "COMPLETED":
            action_required = "AWAITING_REVIEW: All 240 planned runs recorded. Ready for independent review."
        elif batch_status in ("RUNNING", "RUNNING_WINDOW", "ASSESSING_CONFIGURATION"):
            action_required = "NONE: Batch is actively executing under supervisor."
        else:
            action_required = "NONE: Standing monitor active; awaiting review or instruction."

        payload: Dict[str, Any] = {
            "monitor_pid": self.monitor_pid,
            "monitor_created": self.monitor_created,
            "updated_utc": now_utc,
            "monitor_status": "ACTIVE",
            "batch_status": batch_status,
            "batch_last_update": batch_last_update,
            "batch_pid": supervisor_pid,
            "liveness": {
                "monitor_alive": True,
                "batch_alive": supervisor_alive and (batch_status in ("RUNNING", "RUNNING_WINDOW", "ASSESSING_CONFIGURATION")),
                "supervisor_alive": supervisor_alive,
                "bridge_alive": bridge_alive,
                "dashboard_alive": dash_alive,
            },
            "bridge_pid": self.expected_bridge_pid,
            "preserved": bridge_preserved,
            "bridge_details": {
                "expected_pid": self.expected_bridge_pid,
                "expected_created": self.expected_bridge_created,
                "expected_port": self.expected_bridge_port,
                "is_alive": bridge_alive,
                "actual_port_owner": bridge_owner_port,
                "preserved": bridge_preserved,
            },
            "dashboard": {
                "port": self.dashboard_port,
                "pid": dash_owner_pid,
                "listening": dash_alive,
            },
            "counts": {
                "planned": planned,
                "recorded": recorded,
                "deferred": deferred,
                "qualified": qualified,
                "independently_reviewed": independently_reviewed,
                "failed": failed,
            },
            "repair": repair_info,
            "action_required": action_required,
            "latest_failure": latest_failure,
            "catalogue_sha256": catalogue_sha,
            "study_root": str(self.study_root),
        }

        with self._lock_state:
            self._cached_status = payload

        return payload

    def write_atomic_heartbeat(self, status: Dict[str, Any]) -> None:
        """Write atomic heartbeat.json every poll cycle."""
        self.orchestrator_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = self.heartbeat_file.with_suffix(".json.tmp")
        tmp_file.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp_file, self.heartbeat_file)

    def record_transition_if_needed(self, status: Dict[str, Any]) -> None:
        """Append state transition event to events.jsonl only when signature changes."""
        current_sig = (
            status.get("batch_status"),
            status.get("action_required"),
            status.get("preserved"),
            status.get("liveness", {}).get("supervisor_alive"),
            status.get("dashboard", {}).get("listening"),
        )

        if self._last_state_signature is None:
            # First poll event
            event = {
                "timestamp_utc": status["updated_utc"],
                "event": "MONITOR_STARTED",
                "monitor_pid": self.monitor_pid,
                "batch_status": status.get("batch_status"),
                "bridge_preserved": status.get("preserved"),
                "action_required": status.get("action_required"),
            }
            self._append_event(event)
            self._last_state_signature = current_sig
        elif current_sig != self._last_state_signature:
            event = {
                "timestamp_utc": status["updated_utc"],
                "event": "STATE_TRANSITION",
                "previous_signature": [str(x) for x in self._last_state_signature],
                "new_batch_status": status.get("batch_status"),
                "new_action_required": status.get("action_required"),
                "bridge_preserved": status.get("preserved"),
            }
            self._append_event(event)
            self._last_state_signature = current_sig

    def _append_event(self, event_data: Dict[str, Any]) -> None:
        self.orchestrator_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event_data) + "\n"
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(line)

    def get_current_status_data(self) -> Dict[str, Any]:
        with self._lock_state:
            if self._cached_status:
                return dict(self._cached_status)
        return self.collect_state()

    def render_status_html(self) -> str:
        data = self.get_current_status_data()

        monitor_status = html.escape(str(data.get("monitor_status", "UNKNOWN")))
        batch_status = html.escape(str(data.get("batch_status", "UNKNOWN")))
        monitor_pid = html.escape(str(data.get("monitor_pid", "")))
        batch_pid = html.escape(str(data.get("batch_pid", "None")))
        updated_utc = html.escape(str(data.get("updated_utc", "")))
        batch_last_update = html.escape(str(data.get("batch_last_update", "None")))
        bridge_preserved = "YES" if data.get("preserved") else "NO (MISMATCH)"
        bridge_pid = html.escape(str(data.get("bridge_pid", "")))
        dash_port = html.escape(str(data.get("dashboard", {}).get("port", 8766)))
        dash_alive = "ACTIVE" if data.get("dashboard", {}).get("listening") else "OFFLINE"
        action_required = html.escape(str(data.get("action_required", "NONE")))
        latest_failure = html.escape(str(data.get("latest_failure", "None")))

        counts = data.get("counts", {})
        planned = html.escape(str(counts.get("planned", 240)))
        recorded = html.escape(str(counts.get("recorded", 0)))
        deferred = html.escape(str(counts.get("deferred", 0)))
        qualified = html.escape(str(counts.get("qualified", 0)))
        reviewed = html.escape(str(counts.get("independently_reviewed", 0)))
        failed = html.escape(str(counts.get("failed", 0)))

        is_failed = batch_status in ("FAILED_SAFE_STOP", "BLOCKED_SHARED_WEATHER_FAULT")
        badge_color = "#dc2626" if is_failed else ("#16a34a" if batch_status == "COMPLETED" else "#eab308")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CrossGuard Orchestrator Monitor</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: #0f172a;
    color: #e2e8f0;
    margin: 0;
    padding: 2rem;
    line-height: 1.5;
  }}
  .container {{
    max-width: 900px;
    margin: 0 auto;
    background: #1e293b;
    border-radius: 8px;
    border: 1px solid #334155;
    padding: 2rem;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
  }}
  h1 {{
    margin-top: 0;
    font-size: 1.5rem;
    color: #f8fafc;
    border-bottom: 1px solid #334155;
    padding-bottom: 0.75rem;
  }}
  .badge-bar {{
    display: flex;
    gap: 1rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }}
  .badge {{
    display: inline-block;
    padding: 0.35rem 0.75rem;
    border-radius: 4px;
    font-weight: 600;
    font-size: 0.85rem;
    letter-spacing: 0.05em;
  }}
  .badge-live {{
    background: #065f46;
    color: #6ee7b7;
  }}
  .badge-status {{
    background: {badge_color};
    color: #ffffff;
  }}
  .section {{
    margin-bottom: 1.5rem;
  }}
  .section h2 {{
    font-size: 1.1rem;
    color: #94a3b8;
    margin-bottom: 0.5rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1rem;
  }}
  .card {{
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 0.75rem 1rem;
  }}
  .card-label {{
    font-size: 0.75rem;
    color: #64748b;
    text-transform: uppercase;
    font-weight: 600;
  }}
  .card-value {{
    font-size: 1.1rem;
    color: #f1f5f9;
    font-weight: bold;
    margin-top: 0.2rem;
  }}
  .banner-alert {{
    background: rgba(220, 38, 38, 0.15);
    border: 1px solid #dc2626;
    color: #fca5a5;
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 1.5rem;
  }}
  .banner-alert h3 {{
    margin: 0 0 0.5rem 0;
    color: #ef4444;
    font-size: 1rem;
  }}
  .code-block {{
    background: #020617;
    border: 1px solid #1e293b;
    border-radius: 4px;
    padding: 0.75rem;
    font-family: monospace;
    font-size: 0.85rem;
    color: #cbd5e1;
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .footer-links {{
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid #334155;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  a.dash-link {{
    color: #38bdf8;
    text-decoration: none;
    font-weight: 600;
  }}
  a.dash-link:hover {{
    text-decoration: underline;
  }}
</style>
<script>
  setTimeout(() => location.reload(), 5000);
</script>
</head>
<body>
<div class="container">
  <h1>CrossGuard Town10HD Orchestrator Monitor</h1>
  
  <div class="badge-bar">
    <span class="badge badge-live">MONITOR: {monitor_status}</span>
    <span class="badge badge-status">BATCH: {batch_status}</span>
    <span class="badge badge-live">BRIDGE PRESERVED: {bridge_preserved}</span>
  </div>

  <div class="banner-alert">
    <h3>Action Required:</h3>
    <div style="font-weight: 500;">{action_required}</div>
  </div>

  <div class="section">
    <h2>Execution & Liveness Summary</h2>
    <div class="grid">
      <div class="card">
        <div class="card-label">Monitor Process</div>
        <div class="card-value">PID {monitor_pid} (Live)</div>
      </div>
      <div class="card">
        <div class="card-label">Batch Supervisor</div>
        <div class="card-value">PID {batch_pid} (Stopped)</div>
      </div>
      <div class="card">
        <div class="card-label">Bridge Process</div>
        <div class="card-value">PID {bridge_pid} (:8007)</div>
      </div>
      <div class="card">
        <div class="card-label">Dashboard Listener</div>
        <div class="card-value">Port {dash_port} ({dash_alive})</div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Robustness Batch Progress (240 Planned)</h2>
    <div class="grid">
      <div class="card">
        <div class="card-label">Recorded Runs</div>
        <div class="card-value">{recorded} / {planned}</div>
      </div>
      <div class="card">
        <div class="card-label">Deferred Runs</div>
        <div class="card-value">{deferred}</div>
      </div>
      <div class="card">
        <div class="card-label">Qualified Pairs</div>
        <div class="card-value">{qualified}</div>
      </div>
      <div class="card">
        <div class="card-label">Independently Reviewed</div>
        <div class="card-value">{reviewed}</div>
      </div>
      <div class="card">
        <div class="card-label">Failed Attempts</div>
        <div class="card-value">{failed}</div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Latest Recorded Failure</h2>
    <div class="code-block">{latest_failure}</div>
  </div>

  <div class="section">
    <h2>Timestamps</h2>
    <div style="font-size: 0.85rem; color: #94a3b8;">
      <div><strong>Monitor Heartbeat (UTC):</strong> {updated_utc}</div>
      <div><strong>Batch Last Update (UTC):</strong> {batch_last_update}</div>
    </div>
  </div>

  <div class="footer-links">
    <a class="dash-link" href="http://127.0.0.1:{dash_port}/" target="_blank">&rarr; View Detailed Simulation Dashboard (Port {dash_port})</a>
    <a class="dash-link" href="/api/status" target="_blank">&rarr; JSON Status API</a>
  </div>
</div>
</body>
</html>"""

    def start_http_server(self) -> None:
        """Start HTTP server on exclusive socket."""
        class Handler(MonitorRequestHandler):
            pass

        Handler.monitor = self

        try:
            self._server = ExclusiveHTTPServer((self.host, self.port), Handler)
        except OSError as err:
            if err.errno in (errno.EADDRINUSE, 10048) or "address already in use" in str(err).lower():
                print(
                    f"[Orchestrator Monitor] PORT CONFLICT: Port {self.port} on {self.host} is already in use.",
                    file=sys.stderr,
                )
                print(
                    f"[Orchestrator Monitor] Failing cleanly without killing conflicting processes.",
                    file=sys.stderr,
                )
                raise
            raise

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="Town10OrchestratorHTTPServer",
            daemon=True,
        )
        self._server_thread.start()
        print(f"[Orchestrator Monitor] Status server listening on http://{self.host}:{self.port}/")

    def stop_http_server(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                LOGGER.warning("Error shutting down HTTP server: %s", exc)
            self._server = None

    def run_poll_cycle(self) -> Dict[str, Any]:
        """Execute one complete read-only poll cycle."""
        state = self.collect_state()
        self.write_atomic_heartbeat(state)
        self.record_transition_if_needed(state)
        return state

    def run(self, with_server: bool = True, max_cycles: Optional[int] = None) -> None:
        """Main service loop."""
        self.lock.acquire()
        print(f"[Orchestrator Monitor] Acquired lock {self.lock_file} with PID {self.monitor_pid}")

        if with_server:
            self.start_http_server()

        self._running = True
        cycles = 0

        try:
            while self._running:
                state = self.run_poll_cycle()
                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    break
                time.sleep(self.poll_interval_s)
        except KeyboardInterrupt:
            print("\n[Orchestrator Monitor] Shutting down on user interrupt.")
        finally:
            self._running = False
            if with_server:
                self.stop_http_server()
            self.lock.release()
            print("[Orchestrator Monitor] Stopped cleanly and released lock.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CrossGuard Town10HD Standalone Orchestrator Monitor (HTTP 8768 read-only)"
    )
    workspace_default = (
        Path(__file__).resolve().parents[2] / "Evidence" / "Study_02_Town10HD_Continuation" / "Revision_03"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=workspace_default,
        help="Path to the evidence root directory (default: %(default)s)",
    )
    parser.add_argument(
        "--orchestrator-dir",
        type=Path,
        default=None,
        help="Path to Orchestrator state directory (default: <root>/Reports/Robustness_Unattended/Orchestrator)",
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
        default=8768,
        help="Loopback port to bind to (default: 8768)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8766,
        help="Loopback port of running dashboard (default: 8766)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Heartbeat poll interval seconds (default: 5.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle without starting HTTP server, then exit",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Run poll loop without starting HTTP server",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    monitor = Town10OrchestratorMonitor(
        study_root=args.root,
        orchestrator_dir=args.orchestrator_dir,
        host=args.host,
        port=args.port,
        dashboard_port=args.dashboard_port,
        poll_interval_s=args.interval,
    )
    if args.once:
        monitor.lock.acquire()
        try:
            state = monitor.run_poll_cycle()
            print(f"[Orchestrator Monitor] Single poll completed: batch_status={state['batch_status']}")
        finally:
            monitor.lock.release()
    else:
        monitor.run(with_server=not args.no_server)


if __name__ == "__main__":
    main()
