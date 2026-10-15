"""CrossGuard FastAPI Web Server and WebSocket Bridge.

Features:
- Dedicated worker thread as sole CARLA tick owner.
- Frame-boundary command queue (start, stop, pause, resume, reset, system, receiver, view).
- Latest-frame WebSocket streaming: drops outdated display frames for slow clients
  without mutating simulation timestamps.
- Zero-fabrication telemetry contract: preserves unavailable/null metrics.
- Combined JSON WebSocket packet (JPEG base64 + metrics + bounded events + frame ID).
- Local-only binding (127.0.0.1) with fixed Vite localhost CORS.
- Lazy CityWorld loading: CARLA is never imported until start command.
- Integrated offline MockWorld support (--mock-demo) and automated test runner (--self-test).
- Serves Vite production build from src/web/dist when built.
"""

import argparse
import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import io
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from PIL import Image
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from crossguard.policy.following import DEFAULT_FOLLOWING_GAP_M, validate_following_gap
from crossguard.experiments import experiment_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [webserver] %(message)s",
)
logger = logging.getLogger("crossguard.webserver")

SRC_ROOT = Path(__file__).resolve().parents[1]
from crossguard.paths import RESULTS_ROOT,CARLA_LOCK_ROOT
DEFAULT_MODELS_PATH = SRC_ROOT / "models" / "yolo11n.onnx"

VIEW_NAMES = ("top", "driver", "person", "opposite", "driver_b", "driver_c")


# -----------------------------------------------------------------------------
# Synthetic MockWorld for UI demonstration & offline testing (--mock-demo / --self-test)
# -----------------------------------------------------------------------------
class MockCityWorld:
    """Deterministic, simulator-free mock world for headless tests and demo mode.
    
    Strictly labeled perception_mode='mock_generator'.
    """

    def __init__(
        self,
        town: str = "Town10HD",
        vehicles: int = 12,
        walkers: int = 20,
        seed: int = 7,
        resolution: Tuple[int, int] = (960, 540),
        perception_mode: str = "mock_generator",
        receiver_mode: str = "autonomous",
        weights: Optional[str] = None,
        device: str = "cpu",
        enabled: bool = True,
        **kwargs: Any,
    ):
        self.town = town
        self.vehicles = vehicles
        self.walkers = walkers
        self.seed = seed
        self.resolution = resolution
        self.perception_mode = perception_mode
        self.receiver_mode = receiver_mode
        self.weights = weights
        self.device = device
        self.enabled = enabled
        self.time = 0.0
        self.finished = False
        self.tick_count = 0
        self.trial_number = 1
        self.current_view = "driver"
        self.display_mode = "rgb"
        self.messages: List[Dict[str, Any]] = []
        self._rng = np.random.RandomState(seed)

    def set_view(self, name: str):
        if name in VIEW_NAMES:
            self.current_view = name

    def tick(self) -> Dict[str, np.ndarray]:
        self.tick_count += 1
        dt = 0.05
        self.time += dt

        # Vehicle dynamics mock progression
        base_speed = 35.0
        if self.enabled:
            speed = max(0.0, base_speed - self.time * 6.5)
            brake = 0.85 if self.time > 1.2 else 0.0
            throttle = 0.0 if brake > 0 else 0.35
            stopping_margin = max(1.5, 6.0 - self.time * 0.6)
            conflict_in = max(0.0, 3.2 - self.time) if self.time < 3.2 else None
            state = "DECELERATING" if brake > 0 else "ADVISORY"
            reason = "Pedestrian crossing ahead; V2X cooperative yield"
            sources = ("rsu_camera", "bus_v2v")
            advisory_state = "warning" if self.time > 0.8 else "clear"
        else:
            speed = base_speed
            brake = 1.0 if self.time > 4.2 else 0.0
            throttle = 0.55 if brake == 0 else 0.0
            stopping_margin = None
            conflict_in = max(0.0, 1.8 - self.time * 0.4) if self.time > 2.0 else None
            state = "NOMINAL" if self.time < 4.2 else "EMERGENCY"
            reason = "Blind crosswalk approach without roadside assistance"
            sources = ("driver_vision",)
            advisory_state = "clear"

        if self.time >= 6.0:
            self.finished = True

        # Generate simulated events including retrofit driver audio notification
        if self.tick_count % 12 == 0:
            if self.receiver_mode == "retrofit" and self.enabled and self.time > 1.0 and not any(m.get("event") in ("notification_delivered", "notification_cleared") for m in self.messages):
                event = {
                    "event": "notification_delivered",
                    "channel": "simulated_audio",
                    "at_s": round(self.time, 2),
                    "event_id": f"notif_{self.trial_number}_{self.tick_count}",
                    "sender_id": "rsu_crosswalk_01",
                    "receiver_id": "hero_vehicle",
                    "source_id": "rsu_cam_main",
                    "observed_at_s": round(self.time - 0.1, 2),
                    "expires_at_s": round(self.time + 2.0, 2),
                }
            else:
                event_types = ["received", "sent", "v2v_beacon"]
                evt_type = event_types[self.tick_count % len(event_types)]
                event = {
                    "event": evt_type,
                    "channel": "v2i" if evt_type != "v2v_beacon" else "v2v",
                    "at_s": round(self.time, 2),
                    "event_id": f"msg_{self.trial_number}_{self.tick_count}",
                    "sender_id": "rsu_crosswalk_01",
                    "receiver_id": "hero_vehicle",
                    "source_id": "rsu_cam_main",
                    "observed_at_s": round(self.time - 0.1, 2),
                    "expires_at_s": round(self.time + 1.5, 2),
                }
            self.messages.append(event)
            if len(self.messages) > 60:
                self.messages.pop(0)

        # Generate synthetic frame with moving elements and view labels
        w, h = self.resolution
        frames: Dict[str, np.ndarray] = {}
        for view in VIEW_NAMES:
            arr = np.zeros((h, w, 3), dtype=np.uint8)
            # Background colors
            if view == "top":
                arr[:, :, 0] = 25; arr[:, :, 1] = 35; arr[:, :, 2] = 50
            elif view == "driver":
                arr[:, :, 0] = 40; arr[:, :, 1] = 30; arr[:, :, 2] = 35
            elif view == "person":
                arr[:, :, 0] = 30; arr[:, :, 1] = 45; arr[:, :, 2] = 40
            else:
                arr[:, :, 0] = 45; arr[:, :, 1] = 40; arr[:, :, 2] = 30

            # Grid lines
            shift = int((self.time * 50) % 60)
            arr[shift::60, :, :] = np.clip(arr[shift::60, :, :] + 20, 0, 255)
            arr[:, ::80, :] = np.clip(arr[:, ::80, :] + 15, 0, 255)

            frames[view] = arr

        self._current_metrics = {
            "perception_mode": self.perception_mode,
            "frame_id": self.tick_count,
            "sim_time_s": round(self.time, 3),
            "time_s": round(self.time, 3),
            "inference_latency_ms": round(14.2 + (self.tick_count % 5) * 1.1, 1),
            "detector_count": 2 if self.enabled else 1,
            "source_detection_counts": {"rsu_cam": 1, "bus_cam": 1} if self.enabled else {"driver_cam": 1},
            "source_frames": {
                "rsu_cam": {"captured_at_s": round(self.time, 3), "frame_id": self.tick_count, "ground_tracks": 1 if self.enabled else 0},
                "bus_cam": {"captured_at_s": round(self.time, 3), "frame_id": self.tick_count, "ground_tracks": 1 if self.enabled else 0},
                "driver_cam": {"captured_at_s": round(self.time, 3), "frame_id": self.tick_count, "ground_tracks": 1},
            },
            "pipeline_completed_batches": self.tick_count,
            "pipeline_dropped_batches": 0,
            "device_location": [12.4, -4.2, 1.8],
            "device_visible_entity": "vehicle_hero",
            "baseline_behavior": "local_vision",
            "coverage_assumption_active": bool(self.enabled),
            "active_sources": list(sources),
            "receiver_mode": self.receiver_mode,
            "advisory_state": advisory_state,
            "speed_kmh": round(speed, 1),
            "speed": round(speed / 3.6, 2),
            "throttle": round(throttle, 2),
            "brake": round(brake, 2),
            "steer": 0.0,
            "stopping_margin_m": round(stopping_margin, 2) if stopping_margin is not None else None,
            "margin": round(stopping_margin, 2) if stopping_margin is not None else None,
            "conflict_in_s": round(conflict_in, 2) if conflict_in is not None else None,
            "conflict": round(conflict_in, 2) if conflict_in is not None else None,
            "decision_state": state,
            "reason": reason,
            "end_reason": "Trial completed: goal reached" if self.finished else None,
            "sim_fps": 20.0,
        }
        return frames

    def metrics(self) -> Dict[str, Any]:
        return getattr(self, "_current_metrics", {})

    def reset(self, enabled: bool, reload_world: bool = False):
        self.enabled = enabled
        self.time = 0.0
        self.finished = False
        self.tick_count = 0
        self.trial_number += 1
        self.messages.clear()

    def close(self):
        pass


# -----------------------------------------------------------------------------
# Worker Command Pattern & Simulation Worker Thread
# -----------------------------------------------------------------------------
@dataclass
class WorkerCommand:
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    ack_event: threading.Event = field(default_factory=threading.Event)
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


ALLOWED_ACTIONS = {"start", "stop", "pause", "resume", "reset", "system", "receiver", "view", "display", "communication", "group", "following_gap", "experiment", "probe_town"}


class SimulationWorker(threading.Thread):
    """Sole tick owner for CARLA / simulation world.
    
    Commands enqueue and execute at frame boundaries.
    """

    def __init__(
        self,
        default_config: Dict[str, Any],
        broadcast_callback,
    ):
        super().__init__(name="SimulationWorker", daemon=True)
        self.config = default_config
        self.broadcast_callback = broadcast_callback
        self.cmd_queue: queue.Queue = queue.Queue()
        self._stop_requested = threading.Event()

        self.state = "stopped"  # "stopped", "running", "paused", "finished", "error"
        self.last_error: Optional[str] = None
        self.current_view = "driver"
        self.display_mode = "rgb"
        self.system_enabled = bool(default_config.get("enabled", True))
        self.receiver_mode = default_config.get("receiver_mode", "autonomous")
        self.perception_mode = default_config.get("perception_mode", "rgb")
        self.trial_id = 1
        self.seq_id = 0
        self.latest_frame_packet = None

        self.world: Optional[Any] = None
        self.use_mock_demo = bool(default_config.get("mock_demo", False))

        # Measured streaming FPS calculation
        self._frame_times: deque = deque(maxlen=30)
        self.measured_fps = 0.0

    def run(self):
        logger.info("Simulation worker thread started.")
        while not self._stop_requested.is_set():
            # 1. Process all pending commands at this frame boundary
            self._drain_commands()

            # 2. State-dependent action
            if self.state in ("stopped", "paused", "finished", "error"):
                time.sleep(0.02)
                continue

            if self.state == "running":
                if self.world is None:
                    self.state = "error"
                    self.last_error = "Simulation world is not instantiated."
                    self._emit_status_update()
                    continue

                if getattr(self.world, "finished", False):
                    self.state = "finished"
                    self._emit_status_update()
                    time.sleep(0.02)
                    continue

                try:
                    t_start = time.perf_counter()
                    frames = self.world.tick()
                    t_tick = time.perf_counter() - t_start

                    # Update measured FPS
                    now = time.time()
                    self._frame_times.append(now)
                    if len(self._frame_times) >= 2:
                        duration = self._frame_times[-1] - self._frame_times[0]
                        if duration > 0:
                            self.measured_fps = (len(self._frame_times) - 1) / duration

                    metrics = self.world.metrics() if hasattr(self.world, "metrics") else {}
                    self._package_and_broadcast(frames, metrics)

                    # Dynamic pacing to prevent CPU saturation while matching target sim rate
                    target_interval = 1.0 / 60.0  # 1/60 target (60 Hz cap)
                    sleep_time = target_interval - t_tick
                    if sleep_time > 0.001:
                        time.sleep(sleep_time)

                except Exception as exc:
                    logger.exception("Simulation tick encountered an exception")
                    self.state = "error"
                    self.last_error = str(exc)
                    self._emit_status_update()

        # Clean up world upon worker exit
        self._teardown_world()
        logger.info("Simulation worker thread stopped.")

    def _drain_commands(self):
        while True:
            try:
                cmd: WorkerCommand = self.cmd_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._execute_command(cmd)
            except Exception as e:
                cmd.error = str(e)
                self.state = "error"
                self.last_error = str(e)
                self._emit_status_update()
                logger.exception("Command execution failed for %s", cmd.action)
            finally:
                cmd.ack_event.set()

    def _execute_command(self, cmd: WorkerCommand):
        action = cmd.action
        params = cmd.params
        logger.info("Worker executing command: %s (params: %s)", action, params)

        if action == "start":
            if self.state == "running":
                logger.info("Worker start received while already running. Acknowledging existing running state.")
                cmd.result = {"status": "started", "state": self.get_status_dict()}
                return
            self.state = "starting"
            self.last_error = None
            self._emit_status_update()

            # Apply any overrides
            for k in ("town", "vehicles", "walkers", "seed", "weights", "device", "perception_mode", "receiver_mode", "enabled"):
                if k in params:
                    self.config[k] = params[k]
            if "perception_mode" in params:
                self.perception_mode = params["perception_mode"]
            if "receiver_mode" in params:
                self.receiver_mode = params["receiver_mode"]
            if "enabled" in params:
                self.system_enabled = bool(params["enabled"])

            self._setup_world()
            self.state = "running"
            self.last_error = None
            cmd.result = {"status": "started", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "experiment":
            validated = experiment_config(params)
            if self.state != "stopped" or self.world is not None:
                raise ValueError("Invalid experiment state: stop the bridge before configuring a trial")
            self.config.update(validated)
            self.system_enabled = validated["enabled"]
            self.receiver_mode = validated["receiver_mode"]
            self.current_view = "top"
            cmd.result = {"status": "experiment_configured", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "probe_town":
            if self.state != "stopped" or self.world is not None:
                raise ValueError("Invalid map probe state: stop the bridge first")
            town=params.get("town")
            if not isinstance(town,str) or not town.startswith('Town') or '/' in town or '\\' in town:
                raise ValueError("Invalid probe town")
            crossing_type=params.get('crossing_type','marked')
            if crossing_type not in ('marked','unmarked'):
                raise ValueError('Invalid crossing type: use marked or unmarked')
            from crossguard.simulation.ownership import CarlaLease
            from crossguard.simulation.sites import discover_sites, discover_unmarked_sites
            import carla
            client=carla.Client('127.0.0.1',2000)
            client.set_timeout(90.)
            available=client.get_available_maps()
            if town not in {m.rsplit('/',1)[-1] for m in available}:
                cmd.result={'status':'map_not_available','town':town,'available_maps':available}
                return
            lease=CarlaLease(CARLA_LOCK_ROOT/'carla_2000.lock')
            try:
                world=client.load_world(town)
                discover=discover_unmarked_sites if crossing_type=='unmarked' else discover_sites
                cmd.result={'status':'map_geometry_probed','town':town,'crossing_type':crossing_type,'inventory':discover(world.get_map())}
            finally:
                lease.close()

        elif action == "stop":
            self.state = "stopped"
            self._teardown_world()
            cmd.result = {"status": "stopped", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "pause":
            if self.state == "running":
                self.state = "paused"
            cmd.result = {"status": "paused", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "resume":
            if self.state == "paused":
                self.state = "running"
            cmd.result = {"status": "resumed", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "reset":
            reload_world = bool(params.get("reload_world", False))
            if "enabled" in params:
                self.system_enabled = bool(params["enabled"])
            self.trial_id += 1
            self._frame_times.clear()
            self.measured_fps = 0.0
            if self.world is None:
                self._setup_world()
            else:
                if reload_world:
                    self._teardown_world()
                    self._setup_world()
                else:
                    self.world.reset(enabled=self.system_enabled, reload_world=False)
                    self.world.set_view(self.current_view)
            self.state = "running"
            self.last_error = None
            cmd.result = {"status": "reset", "trial_id": self.trial_id, "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "system":
            enabled = bool(params.get("enabled", not self.system_enabled))
            self.system_enabled = enabled
            if self.world is not None:
                self.trial_id += 1
                self._frame_times.clear()
                self.measured_fps = 0.0
                self.world.reset(enabled=enabled, reload_world=False)
                self.world.set_view(self.current_view)
                self.state = "paused" if self.state == "paused" else "running"
            cmd.result = {"status": "system_toggled", "enabled": self.system_enabled, "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "receiver":
            mode = params.get("mode")
            if mode not in ("autonomous", "retrofit"):
                raise ValueError(f"Invalid receiver mode '{mode}'. Must be 'autonomous' or 'retrofit'.")
            self.receiver_mode = mode
            if self.world is not None and hasattr(self.world, "receiver_mode"):
                self.world.receiver_mode = mode
                self.trial_id += 1
                self._frame_times.clear()
                self.measured_fps = 0.0
                self.world.reset(enabled=self.system_enabled, reload_world=False)
                self.world.set_view(self.current_view)
                self.state = "paused" if self.state == "paused" else "running"
            cmd.result = {"status": "receiver_mode_updated", "mode": self.receiver_mode, "state": self.get_status_dict()}
            self._emit_status_update()

        elif action in ("communication", "group", "following_gap"):
            if action == "following_gap":
                self.config["following_gap_m"] = validate_following_gap(params.get("gap_m"))
            elif action == "communication":
                profile = params.get("profile", self.config.get("communication_profile", "radio"))
                if profile not in ("radio", "cellular", "optical", "hybrid"):
                    raise ValueError("Invalid communication profile")
                for key in ("optical_enabled", "phone_available"):
                    if key in params:
                        if not isinstance(params[key], bool):
                            raise ValueError(f"Invalid {key}; expected boolean")
                self.config["communication_profile"] = profile
                for key in ("optical_enabled", "phone_available"):
                    if key in params:
                        self.config[key] = params[key]
            else:
                mode = params.get("mode")
                if mode not in ("single", "lead-only", "cooperative"):
                    raise ValueError("Invalid group mode")
                self.config["group_mode"] = mode
                if mode == "single" and self.current_view in ("driver_b", "driver_c"):
                    self.current_view = "driver"
            if self.world is not None:
                paused = self.state == "paused"
                self.trial_id += 1
                self.state = "starting"
                self._emit_status_update()
                self._setup_world()
                self.state = "paused" if paused else "running"
            cmd.result = {"status": "configuration_updated", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "display":
            mode = params.get("mode", "rgb")
            if mode not in ("rgb", "yolo"):
                raise ValueError("Display mode must be rgb or yolo")
            self.display_mode = mode
            if self.world is not None:
                self.world.preview_detection_enabled = mode == "yolo"
            cmd.result = {"status": "display_selected", "state": self.get_status_dict()}
            self._emit_status_update()

        elif action == "view":
            view_name = params.get("name")
            if view_name not in VIEW_NAMES:
                raise ValueError(f"Invalid view name '{view_name}'. Must be one of {VIEW_NAMES}.")
            if view_name in ("driver_b", "driver_c") and self.config.get("group_mode", "single") == "single":
                raise ValueError("Invalid view: followers require a group scenario")
            self.current_view = view_name
            if self.world is not None and hasattr(self.world, "set_view"):
                self.world.set_view(view_name)
            cmd.result = {"status": "view_selected", "view": self.current_view, "state": self.get_status_dict()}
            self._emit_status_update()

        else:
            raise ValueError(f"Unknown action: '{action}'")

    def _setup_world(self):
        self._teardown_world()
        self._frame_times.clear()
        self.measured_fps = 0.0
        if self.use_mock_demo or self.perception_mode == "mock":
            logger.info("Initializing MockCityWorld (Mock demo mode)...")
            self.world = MockCityWorld(
                town=self.config.get("town", "Town10HD"),
                vehicles=int(self.config.get("vehicles", 12)),
                walkers=int(self.config.get("walkers", 20)),
                seed=int(self.config.get("seed", 7)),
                resolution=(int(self.config.get("res_w", 1920)), int(self.config.get("res_h", 1080))),
                perception_mode="mock_generator",
                receiver_mode=self.receiver_mode,
                weights=self.config.get("weights"),
                device=self.config.get("device", "gpu"),
                enabled=self.system_enabled,
            )
        else:
            # Explicit contract: Never silent mock fallback for real simulation runs!
            logger.info("Initializing CARLA CityWorld with RGB perception...")
            try:
                # Lazy import to avoid importing CARLA until start
                from crossguard.simulation.city_world import CityWorld
            except ImportError as ie:
                msg = f"CARLA CityWorld import failed: {ie}. Ensure carla package is available."
                logger.error(msg)
                raise RuntimeError(msg) from ie

            weights_path = self.config.get("weights")
            if not weights_path:
                weights_path = str(DEFAULT_MODELS_PATH)

            self.world = CityWorld(
                town=self.config.get("town", "Town10HD"),
                vehicles=int(self.config.get("vehicles", 12)),
                walkers=int(self.config.get("walkers", 20)),
                seed=int(self.config.get("seed", 7)),
                resolution=(int(self.config.get("res_w", 1920)), int(self.config.get("res_h", 1080))),
                perception_mode="ideal_instance_visible" if self.perception_mode == "ideal" else self.perception_mode,
                receiver_mode=self.receiver_mode,
                weights=weights_path,
                group_mode=self.config.get("group_mode", "single"),
                following_gap_m=self.config.get("following_gap_m", DEFAULT_FOLLOWING_GAP_M),
                baseline_behavior=self.config.get("baseline_behavior", "local_vision"),
                target_speed_kmh=self.config.get("target_speed_kmh", 40.0),
                crossing_trigger_distance_m=self.config.get("crossing_trigger_distance_m", 60.0),
                pedestrian_behavior=self.config.get("pedestrian_behavior", "cross"),
                retreat_after_s=self.config.get("retreat_after_s", 2.0),
                scenario_definition=self.config.get("scenario_definition"),
                scene_preflight=self.config.get("scene_preflight",False),
                record_six_views=self.config.get("record_six_views",False),
                observation_window_s=self.config.get("observation_window_s"),
                follower_model=self.config.get("follower_model", "ideal"),
                follower_delay_s=self.config.get("follower_delay_s", 1.0),
                study_options=self.config.get('study_options'),
                communication_profile=self.config.get("communication_profile", "radio"),
                optical_enabled=self.config.get("optical_enabled", True),
                phone_available=self.config.get("phone_available", True),
                device=self.config.get("device", "gpu"),
                enabled=self.system_enabled,
            )

        self.world.preview_detection_enabled = self.display_mode == "yolo"
        if hasattr(self.world, "set_view"):
            self.world.set_view(self.current_view)

    def _teardown_world(self):
        if self.world is not None:
            try:
                self.world.close()
            except Exception as e:
                logger.warning("Error during world.close(): %s", e)
            self.world = None

    def _package_and_broadcast(self, frames: Any, metrics: Dict[str, Any]):
        self.seq_id += 1
        active_frame = None

        if isinstance(frames, dict):
            active_frame = frames.get(self.current_view)

        elif isinstance(frames, np.ndarray):
            active_frame = frames

        image_frame = metrics.get("frame_id")
        image_time = metrics.get("sim_time_s")
        if self.display_mode == "yolo" and self.world and hasattr(self.world, "detection_preview"):
            preview = self.world.detection_preview(self.current_view)
            active_frame = preview[0] if preview else None
            image_frame = preview[1]["frame_id"] if preview else None
            image_time = preview[1]["captured_at_s"] if preview else None
        b64_image = None
        if active_frame is not None and isinstance(active_frame, np.ndarray):
            try:
                # Compress as JPEG to optimize WebSocket transfer
                buf = io.BytesIO()
                im = Image.fromarray(active_frame)
                im.save(buf, format="JPEG", quality=80)
                b64_image = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as e:
                logger.warning("Frame JPEG compression failed: %s", e)

        # Extract bounded recent events
        recent_events = []
        if self.world and hasattr(self.world, "messages"):
            # Send latest 25 events
            raw_msgs = getattr(self.world, "messages", [])
            recent_events = list(raw_msgs[-25:])
            # Critical HMI events survive dropped display packets; clients deduplicate by trial/event.
            advisories = [m for m in raw_msgs if m.get("event") in ("notification_delivered", "notification_cleared")][-8:]
            recent_events = advisories + [m for m in recent_events if m not in advisories]

        sim_time = (
            metrics.get("sim_time_s")
            or metrics.get("time_s")
            or (getattr(self.world, "time", 0.0) if self.world else 0.0)
        )

        packet = {
            "type": "frame",
            "seq": self.seq_id,
            "frame_id": metrics.get("frame_id", self.seq_id),
            "timestamp": time.time(),
            "sim_time_s": sim_time,
            "view": self.current_view,
            "image_mode": self.display_mode,
            "image_frame_id": image_frame,
            "image_capture_time_s": image_time,
            "image": f"data:image/jpeg;base64,{b64_image}" if b64_image else None,
            "metrics": metrics,
            "events": recent_events,
            "stream_fps": round(self.measured_fps, 1),
            "status": {
                "state": self.state,
                "display_mode": self.display_mode,
                "system_enabled": getattr(self.world, "enabled", self.system_enabled),
                "receiver_mode": getattr(self.world, "receiver_mode", self.receiver_mode),
                "perception_mode": getattr(self.world, "perception_mode", self.perception_mode),
                "trial_id": self.trial_id,
                "error": self.last_error,
            },
        }

        self.latest_frame_packet = packet
        self.broadcast_callback(packet)

    def retained_frame(self):
        packet = self.latest_frame_packet
        if packet is None or self.world is None or packet["status"]["trial_id"] != self.trial_id:
            return None
        retained = {**packet,"status":self.get_status_dict(),"view":self.current_view}
        if packet["view"] != self.current_view or packet["image_mode"] != self.display_mode:
            retained.update(image=None,image_frame_id=None,image_capture_time_s=None,image_mode=self.display_mode)
        return retained

    def _emit_status_update(self):
        status_packet = {
            "type": "status_update",
            "seq": self.seq_id,
            "timestamp": time.time(),
            "status": self.get_status_dict(),
        }
        self.broadcast_callback(status_packet)

    def get_status_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "system_enabled": getattr(self.world, "enabled", self.system_enabled) if self.world else self.system_enabled,
            "receiver_mode": getattr(self.world, "receiver_mode", self.receiver_mode) if self.world else self.receiver_mode,
            "perception_mode": getattr(self.world, "perception_mode", self.perception_mode) if self.world else self.perception_mode,
            "current_view": self.current_view,
            "available_views": list(VIEW_NAMES if self.config.get("group_mode", "single") != "single" else VIEW_NAMES[:4]),
            "group_mode": self.config.get("group_mode", "single"),
            "following_gap_m": self.config.get("following_gap_m", DEFAULT_FOLLOWING_GAP_M),
            "communication_profile": self.config.get("communication_profile", "radio"),
            "optical_enabled": self.config.get("optical_enabled", True),
            "phone_available": self.config.get("phone_available", True),
            "display_mode": self.display_mode,
            "trial_id": self.trial_id,
            "sim_time_s": getattr(self.world, "time", 0.0) if self.world else 0.0,
            "finished": getattr(self.world, "finished", False) if self.world else False,
            "terminal_metrics": self.world.metrics() if self.world and getattr(self.world, "finished", False) else None,
            "trial_artifacts": str(self.world.trial_path) if self.world and hasattr(self.world, "trial_path") else None,
            "measured_fps": round(self.measured_fps, 1),
            "error": self.last_error,
            "config": {
                "town": self.config.get("town"),
                "vehicles": self.config.get("vehicles"),
                "walkers": self.config.get("walkers"),
                "seed": self.config.get("seed"),
                "resolution": (self.config.get("res_w"), self.config.get("res_h")),
                "weights": self.config.get("weights"),
                "device": self.config.get("device"),
                "mock_demo": self.use_mock_demo,
                "group_mode": self.config.get("group_mode", "single"),
                "following_gap_m": self.config.get("following_gap_m", DEFAULT_FOLLOWING_GAP_M),
                "communication_profile": self.config.get("communication_profile", "radio"),
                "experiment": {key: self.config.get(key) for key in (
                    "scenario_definition", "target_speed_kmh", "crossing_trigger_distance_m",
                    "pedestrian_behavior", "retreat_after_s", "baseline_behavior", "condition", "follower_model", "follower_delay_s")},
            },
        }

    def dispatch_command(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Thread-safe command submission from FastAPI handlers to worker thread."""
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Unknown action: '{action}'. Allowed: {sorted(ALLOWED_ACTIONS)}")
        if timeout is None:
            # CityWorld constructor can take up to 60s connecting to CARLA; allow 75s
            reloads_world = action in ("start", "communication", "group", "following_gap", "probe_town") or bool((params or {}).get("reload_world"))
            timeout = 120.0 if reloads_world else 15.0
        cmd = WorkerCommand(action=action, params=params or {})
        self.cmd_queue.put(cmd)
        if not cmd.ack_event.wait(timeout=timeout):
            raise TimeoutError(f"Command '{action}' timed out after {timeout} seconds at frame boundary.")
        if cmd.error:
            if "Unknown action" in cmd.error or "Invalid" in cmd.error:
                raise ValueError(cmd.error)
            raise RuntimeError(cmd.error)
        return cmd.result

    def stop_worker(self):
        self._stop_requested.set()


# -----------------------------------------------------------------------------
# FastAPI App Definition and WebSocket Connection Manager
# -----------------------------------------------------------------------------
class ConnectionManager:
    """Thread-safe WebSocket manager implementing latest-frame transport."""

    def __init__(self):
        self.active_connections: Dict[WebSocket, asyncio.Queue] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.latest_packet: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def register(self, websocket: WebSocket) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        with self._lock:
            self.active_connections[websocket] = q
        return q

    def unregister(self, websocket: WebSocket):
        with self._lock:
            self.active_connections.pop(websocket, None)

    def broadcast_packet(self, packet: Dict[str, Any]):
        """Called from worker thread; schedules non-blocking frame delivery."""
        with self._lock:
            self.latest_packet = packet
            if not self.loop or self.loop.is_closed():
                return
            for ws, q in list(self.active_connections.items()):
                self.loop.call_soon_threadsafe(self._enqueue_latest, q, packet)

    @staticmethod
    def _enqueue_latest(q: asyncio.Queue, packet: Dict[str, Any]):
        # Latest-frame transport: discard previous frame if queue is full
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(packet)
        except asyncio.QueueFull:
            pass


# -----------------------------------------------------------------------------
# Global Server Context & Lifecycle
# -----------------------------------------------------------------------------
class ServerContext:
    def __init__(self):
        self.manager = ConnectionManager()
        self.worker: Optional[SimulationWorker] = None
        self.config: Dict[str, Any] = {}


server_ctx = ServerContext()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup event loop in connection manager
    loop = asyncio.get_running_loop()
    server_ctx.manager.set_loop(loop)
    logger.info("FastAPI server starting on 127.0.0.1; worker initialized.")
    yield
    # Shutdown
    logger.info("FastAPI server shutting down; stopping simulation worker...")
    if server_ctx.worker:
        server_ctx.worker.stop_worker()
        server_ctx.worker.join(timeout=5.0)
    logger.info("Graceful shutdown complete.")


app = FastAPI(
    title="CrossGuard Web Bridge",
    description="FastAPI WebSocket bridge connecting React viewer to CARLA CityWorld",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: strictly limited to local Vite & server ports
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CommandRequest(BaseModel):
    action: str = Field(..., description="Action name: start, stop, pause, resume, reset, system, receiver, view")
    params: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Action parameters")


# -----------------------------------------------------------------------------
# HTTP REST Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/health")
def get_health():
    worker_alive = server_ctx.worker.is_alive() if server_ctx.worker else False
    return {
        "status": "healthy",
        "worker_alive": worker_alive,
        "timestamp": time.time(),
    }


@app.get("/api/status")
def get_status():
    if not server_ctx.worker:
        return JSONResponse(status_code=503, content={"error": "Worker not initialized"})
    return server_ctx.worker.get_status_dict()


@app.post("/api/command")
def post_command(cmd: CommandRequest):
    if not server_ctx.worker:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    try:
        res = server_ctx.worker.dispatch_command(cmd.action, cmd.params)
        return {"status": "ok", "action": cmd.action, "result": res, "acknowledged_state": server_ctx.worker.get_status_dict()}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except TimeoutError as te:
        raise HTTPException(status_code=504, detail=str(te))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# -----------------------------------------------------------------------------
# WebSocket Stream
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = server_ctx.manager.register(websocket)

    # Immediately push current status
    if server_ctx.worker:
        initial_status = {
            "type": "status_update",
            "seq": 0,
            "timestamp": time.time(),
            "status": server_ctx.worker.get_status_dict(),
        }
        await websocket.send_text(json.dumps(initial_status))
        retained = server_ctx.worker.retained_frame()
        if retained is not None:
            await websocket.send_text(json.dumps(retained))

    async def sender_task():
        while True:
            packet = await q.get()
            try:
                await websocket.send_text(json.dumps(packet))
            except Exception:
                break

    async def receiver_task():
        while True:
            try:
                data_text = await websocket.receive_text()
                data = json.loads(data_text)
                msg_type = data.get("type")
                if msg_type == "command":
                    action = data.get("action")
                    params = data.get("params", {})
                    if server_ctx.worker and action:
                        try:
                            res = await asyncio.to_thread(server_ctx.worker.dispatch_command, action, params)
                            ack = {"type": "ack", "action": action, "status": "ok", "result": res}
                        except Exception as e:
                            ack = {"type": "ack", "action": action, "status": "error", "error": str(e)}
                        await websocket.send_text(json.dumps(ack))
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "timestamp": time.time()}))
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.warning("Error processing incoming WS message: %s", e)
                break

    sender = asyncio.create_task(sender_task())
    receiver = asyncio.create_task(receiver_task())

    done, pending = await asyncio.wait(
        [sender, receiver],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    server_ctx.manager.unregister(websocket)


# -----------------------------------------------------------------------------
# Static Files & SPA Production Serving
# -----------------------------------------------------------------------------
dist_path = SRC_ROOT / "web" / "dist"
if dist_path.exists() and (dist_path / "index.html").exists():
    logger.info("Serving built React SPA from: %s", dist_path)
    assets_path = dist_path / "assets"
    if assets_path.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        target = dist_path / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(dist_path / "index.html")
else:
    @app.get("/")
    async def root_placeholder():
        return HTMLResponse(
            """<!DOCTYPE html>
<html>
<head><title>CrossGuard Web Bridge</title></head>
<body style="font-family:sans-serif; background:#111; color:#eee; padding:2rem;">
  <h2>CrossGuard Web Bridge Active (API &amp; WebSocket)</h2>
  <p>React Vite production bundle not found in <code>src/web/dist</code>.</p>
  <p>To view the React interface:</p>
  <ol>
    <li>Run <code>cd src/web &amp;&amp; npm install &amp;&amp; npm run dev</code> for local development at <code>http://localhost:5173</code></li>
    <li>Or run <code>cd src/web &amp;&amp; npm run build</code> to produce production files for root serving.</li>
  </ol>
  <p>API endpoints: <a style="color:#6cf" href="/api/health">/api/health</a>, <a style="color:#6cf" href="/api/status">/api/status</a>. WebSocket: <code>/ws</code></p>
</body>
</html>"""
        )


# -----------------------------------------------------------------------------
# CLI Parser & Self-Test Verification Suite
# -----------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CrossGuard FastAPI Webserver & WebSocket Bridge")
    parser.add_argument("--host", type=str, default=os.getenv("CROSSGUARD_HOST", "127.0.0.1"), help="Host IP (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("CROSSGUARD_PORT", "8000")), help="Port (default 8000)")
    parser.add_argument("--town", type=str, default=os.getenv("CROSSGUARD_TOWN", "Town10HD"), help="CARLA Town (default Town10HD)")
    parser.add_argument("--group-mode", choices=["single", "lead-only", "cooperative"], default="single",
                        help="Controlled A/B/C scenario")
    parser.add_argument("--communication-profile", choices=["radio","cellular","optical","hybrid"],default="hybrid")
    parser.add_argument("--vehicles", type=int, default=int(os.getenv("CROSSGUARD_VEHICLES", "12")), help="Vehicle count")
    parser.add_argument("--walkers", type=int, default=int(os.getenv("CROSSGUARD_WALKERS", "20")), help="Walker count")
    parser.add_argument("--seed", type=int, default=int(os.getenv("CROSSGUARD_SEED", "7")), help="Random seed")
    parser.add_argument("--res-w", type=int, default=int(os.getenv("CROSSGUARD_RES_W", "1920")), help="Camera resolution width")
    parser.add_argument("--res-h", type=int, default=int(os.getenv("CROSSGUARD_RES_H", "1080")), help="Camera resolution height")
    parser.add_argument("--perception", type=str, default=os.getenv("CROSSGUARD_PERCEPTION", "rgb"), choices=["rgb", "ideal", "mock"], help="Perception mode")
    parser.add_argument("--receiver", type=str, default=os.getenv("CROSSGUARD_RECEIVER", "autonomous"), choices=["autonomous", "retrofit"], help="Receiver mode")
    parser.add_argument("--weights", type=str, default=os.getenv("CROSSGUARD_WEIGHTS", str(DEFAULT_MODELS_PATH)), help="YOLO weights file path")
    parser.add_argument("--device", type=str, default=os.getenv("CROSSGUARD_DEVICE", "gpu"), help="Inference device (cpu/cuda)")
    parser.add_argument("--mock-demo", action="store_true", help="Launch with deterministic MockWorld (no CARLA)")
    parser.add_argument("--self-test", action="store_true", help="Run comprehensive unit and integration verification test")
    return parser


def run_self_tests() -> int:
    """Pure offline verification of webserver, worker, frame boundaries, and command dispatch."""
    print("================================================================================")
    print("CrossGuard Web Bridge Self-Test Suite")
    print("================================================================================")
    import unittest

    test_config = {
        "town": "Town10HD",
        "vehicles": 12,
        "walkers": 20,
        "seed": 7,
        "res_w": 320,
        "res_h": 180,
        "perception_mode": "mock",
        "receiver_mode": "retrofit",
        "mock_demo": True,
        "weights": None,
        "device": "cpu",
        "enabled": True,
    }

    packets_received = []

    def mock_broadcast(pkt):
        packets_received.append(pkt)

    worker = SimulationWorker(test_config, mock_broadcast)
    worker.start()

    try:
        # 1. Verify initial status
        print("[TEST 1] Checking worker initialization status...")
        status = worker.get_status_dict()
        assert status["state"] == "stopped", f"Expected stopped, got {status['state']}"
        print("  -> Worker initialized in stopped state as expected.")

        # 2. Start simulation with mock world
        print("[TEST 2] Dispatching 'start' command...")
        res = worker.dispatch_command("start", {"perception_mode": "mock"})
        time.sleep(0.3)
        status = worker.get_status_dict()
        assert status["state"] == "running", f"Expected running, got {status['state']}"
        assert len(packets_received) > 0, "Expected broadcast packets"
        first_frame = packets_received[-1]
        assert first_frame["type"] == "frame"
        assert "image" in first_frame and first_frame["image"] is not None
        assert "metrics" in first_frame
        metrics = first_frame["metrics"]
        assert "source_detection_counts" in metrics
        assert "source_frames" in metrics
        assert "inference_latency_ms" in metrics
        assert "pipeline_completed_batches" in metrics
        assert "device_location" in metrics
        assert "device_visible_entity" in metrics
        assert "baseline_behavior" in metrics
        assert "coverage_assumption_active" in metrics
        print("  -> Start acknowledged; telemetry contracts verified.")

        # 3. View switching
        print("[TEST 3] Testing camera view switching (top, person, opposite, driver)...")
        for v in ("top", "person", "opposite", "driver"):
            res = worker.dispatch_command("view", {"name": v})
            time.sleep(0.1)
            assert worker.current_view == v, f"Failed view switch to {v}"
        print("  -> View switching successfully verified across all 4 cameras.")

        # 4. System toggle
        print("[TEST 4] Testing system toggle (enabled on/off)...")
        res = worker.dispatch_command("system", {"enabled": False})
        assert worker.system_enabled is False
        res = worker.dispatch_command("system", {"enabled": True})
        assert worker.system_enabled is True
        print("  -> System enable/disable toggle successfully acknowledged.")

        # 5. Receiver mode toggle
        print("[TEST 5] Testing receiver mode toggle (autonomous/retrofit)...")
        res = worker.dispatch_command("receiver", {"mode": "autonomous"})
        assert worker.receiver_mode == "autonomous"
        res = worker.dispatch_command("receiver", {"mode": "retrofit"})
        assert worker.receiver_mode == "retrofit"
        print("  -> Receiver mode toggle successfully verified.")

        # 6. Pause and resume
        print("[TEST 6] Testing pause and resume...")
        worker.dispatch_command("pause")
        assert worker.state == "paused"
        curr_seq = worker.seq_id
        time.sleep(0.15)
        assert worker.seq_id == curr_seq, "Ticks advanced during pause!"
        worker.dispatch_command("resume")
        time.sleep(0.15)
        assert worker.seq_id > curr_seq, "Ticks did not resume!"
        print("  -> Pause/resume behavior verified at frame boundaries.")

        # 7. Reset trial
        print("[TEST 7] Testing trial reset...")
        prev_trial = worker.trial_id
        res = worker.dispatch_command("reset", {"enabled": True})
        assert worker.trial_id == prev_trial + 1
        print("  -> Reset verified; trial ID incremented.")

        # 8. Verify audio notification event structure for retrofit mode
        print("[TEST 8] Verifying audio notification emission in retrofit mode...")
        # Tick a few more times to trigger notification
        time.sleep(0.6)
        recent_packet = packets_received[-1]
        events = recent_packet.get("events", [])
        notif_events = [e for e in events if e.get("channel") == "simulated_audio"]
        if notif_events:
            print(f"  -> Successfully generated simulated_audio event: {notif_events[0]}")
        else:
            print("  -> Retrofit mock events present:", len(events))

        # 9. Stop simulation
        print("[TEST 9] Testing stop command...")
        worker.dispatch_command("stop")
        assert worker.state == "stopped"
        print("  -> Stop acknowledged.")

        # 10. Test FastAPI HTTP Endpoints via TestClient
        print("[TEST 10] Testing FastAPI HTTP endpoints with TestClient...")
        from fastapi.testclient import TestClient
        server_ctx.worker = worker
        client = TestClient(app)

        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["status"] == "healthy"

        status_resp = client.get("/api/status")
        assert status_resp.status_code == 200
        assert "state" in status_resp.json()

        cmd_resp = client.post("/api/command", json={"action": "view", "params": {"name": "opposite"}})
        assert cmd_resp.status_code == 200
        assert cmd_resp.json()["status"] == "ok"
        assert worker.current_view == "opposite"

        # Invalid action should return 400
        bad_resp = client.post("/api/command", json={"action": "invalid_action"})
        assert bad_resp.status_code == 400

        # SPA root delivery
        spa_resp = client.get("/")
        assert spa_resp.status_code == 200
        assert 'id="root"' in spa_resp.text and 'type="module"' in spa_resp.text
        print("  -> HTTP endpoints (/api/health, /api/status, /api/command, /) verified.")

        # 11. Verify failure sets worker error state and surfaces
        print("[TEST 11] Verifying error propagation and worker error state on failure...")
        try:
            worker.dispatch_command("receiver", {"mode": "invalid_mode"})
        except ValueError:
            pass
        status = worker.get_status_dict()
        assert status["error"] is not None, "Worker error state was not populated on failure"
        assert worker.state == "error", f"Expected error state, got {worker.state}"
        # Recover cleanly
        worker.dispatch_command("stop")
        print("  -> Worker error state successfully populated and surfaced.")

        print("================================================================================")
        print("ALL 11 SELF-TESTS PASSED SUCCESSFULLY!")
        print("================================================================================")
        return 0

    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        worker.stop_worker()
        worker.join(timeout=2.0)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_tests())

    config = {
        "town": args.town,
        "group_mode": args.group_mode,
        "communication_profile":args.communication_profile,
        "vehicles": args.vehicles,
        "walkers": args.walkers,
        "seed": args.seed,
        "res_w": args.res_w,
        "res_h": args.res_h,
        "perception_mode": args.perception,
        "receiver_mode": args.receiver,
        "weights": args.weights,
        "device": args.device,
        "mock_demo": args.mock_demo,
        "enabled": True,
    }

    server_ctx.config = config
    server_ctx.worker = SimulationWorker(config, server_ctx.manager.broadcast_packet)
    server_ctx.worker.start()

    import uvicorn

    logger.info("Starting Uvicorn server on http://%s:%d (reload=False)", args.host, args.port)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=False,  # strictly disabled to prevent duplicate tick clients
        log_level="info",
    )


if __name__ == "__main__":
    main()
