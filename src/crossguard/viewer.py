"""CrossGuard Pygame Single-Active-Camera Viewer.

Features:
- Single active camera view (1920x1080 native canvas, resizable display window).
- Switch between 4 views: 'top', 'driver', 'person', 'opposite' via buttons or number keys (1-4).
- Translucent compact left HUD over the scene displaying actual telemetry/metrics only.
  Missing or None metrics are displayed as unavailable/n-a, never fabricated.
- Unobtrusive control panels (top banner and bottom action bar).
- Toggle WITH/WITHOUT system ('on' / 'off'), Replay ('R'), Pause ('Space'),
  Telemetry HUD toggle ('T'), Communications log toggle ('C'), Quit ('Esc').
- Window remains interactive and persistent after trial finishes.
- Optional natural display saturation/contrast adjustment (neutral default),
  without ever mutating raw RGB arrays or perception input.
- Communication panel displaying actual network/message events (sent, received, dropped, expired)
  with explanatory source -> receiver text/arrows.
- Lazy CityWorld loading with deterministic testable MockWorld for headless testing / --mock-demo.
- Headless verification via --verify-ui exercising event handling, camera switching, toggles,
  screenshot capture, and writing verification JSON to src/results/ui_verification.
"""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import numpy as np
import pygame

from crossguard.contracts import VIEW_NAMES

# Native layout resolution
CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1080

VIEW_LABELS = {
    "top": "1: TOP VIEW",
    "driver": "2: DRIVER VIEW",
    "person": "3: PEDESTRIAN VIEW",
    "opposite": "4: OPPOSITE APPROACH",
}


class MockWorld:
    """Deterministic, testable mock world for testing and UI demonstration.

    Clearly labeled MOCK UI ONLY. Never to be confused with evaluation evidence.
    """

    def __init__(
        self,
        town: str = "Town10HD",
        vehicles: int = 35,
        walkers: int = 75,
        seed: int = 7,
        progress: Optional[Callable[[str], None]] = None,
        resolution: Tuple[int, int] = (CANVAS_WIDTH, CANVAS_HEIGHT),
        **kwargs: Any,
    ):
        if progress:
            progress("Initializing Mock World (MOCK UI ONLY)...")
        self.town = town
        self.vehicles = vehicles
        self.walkers = walkers
        self.seed = seed
        self.resolution = resolution
        self.enabled = kwargs.get("enabled", True)
        self.time = 0.0
        self.finished = False
        self.trial_number = 1
        self.tick_count = 0

        # Create output directory under src/results
        src_root = Path(__file__).resolve().parents[1]
        self.output = src_root / "results" / "mock_runs"
        self.output.mkdir(parents=True, exist_ok=True)

        self.rows: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []

        # Synthetic generator state
        self._rng = np.random.RandomState(seed)
        self._base_speed = 35.0  # km/h

    def tick(self) -> Dict[str, np.ndarray]:
        self.tick_count += 1
        dt = 0.05
        self.time += dt

        # Vehicle dynamics mock progression
        if self.enabled:
            # Slow down if system enabled (avoid collision)
            speed = max(0.0, self._base_speed - self.time * 7.0)
            brake = 0.8 if self.time > 1.5 else 0.0
            throttle = 0.0 if brake > 0 else 0.4
            stopping_margin = max(1.2, 5.0 - self.time * 0.5)
            conflict_in = max(0.0, 3.5 - self.time) if self.time < 3.5 else None
            state = "DECELERATING" if brake > 0 else "ADVISORY"
            reason = "Pedestrian detected on marked crosswalk"
            sources = ("rsu_camera", "bus_v2v")
        else:
            # Without system: high speed, late emergency brake or no warning
            speed = self._base_speed
            brake = 1.0 if self.time > 4.0 else 0.0
            throttle = 0.6 if brake == 0 else 0.0
            stopping_margin = None
            conflict_in = max(0.0, 1.8 - self.time * 0.4) if self.time > 2.0 else None
            state = "NOMINAL" if self.time < 4.0 else "EMERGENCY"
            reason = "Blind crosswalk approach without roadside assistance"
            sources = ("driver_vision",)

        if self.time >= 6.0:
            self.finished = True

        # Log simulated communication events
        if self.tick_count % 15 == 0:
            event_types = ["received", "sent", "dropped", "expired"]
            evt_type = event_types[self.tick_count % len(event_types)]
            event = {
                "event": evt_type,
                "at_s": round(self.time, 2),
                "sender_id": "rsu_crosswalk_01",
                "receiver_id": "hero_vehicle",
                "event_id": f"msg_{self.tick_count}",
                "channel": "v2i",
                "observed_at_s": round(max(0.0, self.time - 0.12), 2),
                "expires_at_s": round(self.time + 1.5, 2),
                "source_id": "rsu_cam_main",
            }
            self.messages.append(event)
            if len(self.messages) > 50:
                self.messages.pop(0)

        # Generate deterministic labeled RGB frames for all 4 views
        w, h = self.resolution
        frames: Dict[str, np.ndarray] = {}
        for idx, view in enumerate(VIEW_NAMES):
            arr = np.zeros((h, w, 3), dtype=np.uint8)
            # Distinct subtle gradient for background view
            if view == "top":
                arr[:, :, 0] = 30
                arr[:, :, 1] = 40
                arr[:, :, 2] = 55
            elif view == "driver":
                arr[:, :, 0] = 45
                arr[:, :, 1] = 35
                arr[:, :, 2] = 40
            elif view == "person":
                arr[:, :, 0] = 35
                arr[:, :, 1] = 50
                arr[:, :, 2] = 45
            else:  # opposite
                arr[:, :, 0] = 50
                arr[:, :, 1] = 45
                arr[:, :, 2] = 35

            # Grid pattern to visualize scene motion deterministically
            grid_shift = int((self.time * 60) % 80)
            arr[grid_shift::80, :, :] = np.clip(arr[grid_shift::80, :, :] + 25, 0, 255)
            arr[:, ::120, :] = np.clip(arr[:, ::120, :] + 20, 0, 255)

            frames[view] = arr

        self._current_metrics = {
            "speed_kmh": round(speed, 1),
            "throttle": round(throttle, 2),
            "brake": round(brake, 2),
            "steer": 0.0,
            "sim_time_s": round(self.time, 2),
            "sim_fps": 20.0,
            "selected_mode": "WITH_SYSTEM" if self.enabled else "WITHOUT_SYSTEM",
            "active_sources": list(sources),
            "observation_age_s": 0.08 if self.enabled else None,
            "conflict_in_s": round(conflict_in, 2) if conflict_in is not None else None,
            "stopping_margin_m": round(stopping_margin, 2) if stopping_margin is not None else None,
            "decision_state": state,
            "reason": reason,
            "warnings_received": len([m for m in self.messages if m["event"] == "received"]),
            "warnings_sent": len([m for m in self.messages if m["event"] == "sent"]),
            "messages_dropped": len([m for m in self.messages if m["event"] == "dropped"]),
            "messages_expired": len([m for m in self.messages if m["event"] == "expired"]),
            "perception_mode": "mock_generator",
            "crosswalk_valid": True,
            "pedestrian_collision": False if self.enabled else (self.time >= 5.0),
        }
        self.rows.append(self._current_metrics)
        return frames

    def metrics(self) -> Dict[str, Any]:
        return getattr(self, "_current_metrics", {})

    def reset(self, enabled: bool):
        self.enabled = enabled
        self.time = 0.0
        self.finished = False
        self.trial_number += 1
        self.tick_count = 0
        self.rows = []
        self.messages = []

    def close(self):
        pass


def apply_display_adjustments(surface: pygame.Surface, saturation: float = 1.0, contrast: float = 1.0) -> pygame.Surface:
    """Optional presentation adjustment on display Surface. Never mutates input RGB arrays."""
    if saturation == 1.0 and contrast == 1.0:
        return surface

    # Safe presentation transformation on surface pixels
    w, h = surface.get_size()
    rgb = pygame.surfarray.pixels3d(surface).astype(np.float32)

    if contrast != 1.0:
        rgb = (rgb - 128.0) * contrast + 128.0
        np.clip(rgb, 0, 255, out=rgb)

    if saturation != 1.0:
        # Luminance per Rec. 709
        gray = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
        for c in range(3):
            rgb[:, :, c] = gray + saturation * (rgb[:, :, c] - gray)
        np.clip(rgb, 0, 255, out=rgb)

    out = pygame.surfarray.make_surface(rgb.astype(np.uint8))
    del rgb
    return out


class Viewer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        pygame.init()

        desktop_w, desktop_h = pygame.display.get_desktop_sizes()[0]
        fit = min(1.0, (desktop_w - 80) / args.width, (desktop_h - 100) / args.height)
        self.width = max(640, int(args.width * fit))
        self.height = max(360, int(args.height * fit))

        self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        pygame.display.set_caption("CrossGuard | Autonomous Cooperative Safety Viewer")

        self.clock = pygame.time.Clock()
        self.canvas = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT))

        # Fonts
        self.font_title = pygame.font.SysFont("Segoe UI", 24, bold=True)
        self.font_main = pygame.font.SysFont("Segoe UI", 18)
        self.font_bold = pygame.font.SysFont("Segoe UI", 18, bold=True)
        self.font_small = pygame.font.SysFont("Segoe UI", 15)
        self.font_tiny = pygame.font.SysFont("Consolas", 13)

        self.running = True
        self.paused = False
        self.selected_view = "top"
        self.show_hud = True
        self.show_comms = True
        self.frames: Dict[str, np.ndarray] = {}
        self.error: Optional[str] = None
        self.city: Any = None
        self.fps_smoothed = 30.0
        self.last_frame_time = time.monotonic()

        # Visual adjustment parameters (default neutral)
        self.saturation = 1.0
        self.contrast = 1.0

        # UI Control Rectangles (Native canvas coordinates)
        self._init_buttons()

        if getattr(args, "perception", "ideal") == "rgb" and not (args.mock_demo or args.verify_ui):
            pygame.quit()
            raise RuntimeError("Trained RGB integration is pending; use explicitly labeled ideal mode.")

        # Connect to CityWorld or MockWorld
        self._setup_world()

    def _init_buttons(self):
        # Top banner buttons for Camera switching
        # Top bar is 54px high
        cam_w = 210
        start_x = 420
        self.cam_buttons = {
            "top": pygame.Rect(start_x, 8, cam_w, 38),
            "driver": pygame.Rect(start_x + cam_w + 10, 8, cam_w, 38),
            "person": pygame.Rect(start_x + (cam_w + 10) * 2, 8, cam_w, 38),
            "opposite": pygame.Rect(start_x + (cam_w + 10) * 3, 8, cam_w, 38),
        }

        # Bottom bar buttons
        b_y = CANVAS_HEIGHT - 54
        b_h = 42
        self.control_buttons = {
            "toggle_system": pygame.Rect(24, b_y, 230, b_h),
            "replay": pygame.Rect(268, b_y, 160, b_h),
            "pause": pygame.Rect(442, b_y, 150, b_h),
            "hud": pygame.Rect(606, b_y, 150, b_h),
            "comms": pygame.Rect(770, b_y, 180, b_h),
            "receiver": pygame.Rect(970, b_y, 340, b_h),
            "quit": pygame.Rect(CANVAS_WIDTH - 150, b_y, 126, b_h),
        }

    def _setup_world(self):
        self.loading("Initializing World environment...")
        if self.args.mock_demo or self.args.verify_ui:
            self.city = MockWorld(
                vehicles=self.args.cars,
                walkers=self.args.people,
                seed=self.args.seed,
                progress=self.loading,
                resolution=(CANVAS_WIDTH, CANVAS_HEIGHT),
            )
        else:
            # Lazy import of real CityWorld
            try:
                from crossguard.simulation.city_world import CityWorld
                self.city = CityWorld(
                    town="Town10HD",
                    vehicles=self.args.cars,
                    walkers=self.args.people,
                    seed=self.args.seed,
                    progress=self.loading,
                    resolution=(1280, 720),
                    receiver_mode=getattr(self.args, "receiver", "autonomous"),
                )
            except Exception as exc:
                print(f"[Viewer] CARLA CityWorld import/init failed: {exc}", file=sys.stderr)
                pygame.quit()
                raise RuntimeError("Live CARLA initialization failed; mock mode requires --mock-demo") from exc

    def loading(self, message: str):
        self.canvas.fill((27, 27, 27))
        # Header
        t_surf = self.font_title.render("CrossGuard  |  V2X Pedestrian Safety", True, (242, 242, 242))
        self.canvas.blit(t_surf, (40, 40))
        m_surf = self.font_main.render(message, True, (245, 245, 245))
        self.canvas.blit(m_surf, (40, 110))
        sub_surf = self.font_small.render("Preparing simulation world, sensors, and roadside monitors...", True, (180, 180, 180))
        self.canvas.blit(sub_surf, (40, 150))
        self.flip()

        for event in pygame.event.get():
            if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                self.running = False
                return
        pygame.event.pump()

    def flip(self):
        screen_size = self.screen.get_size()
        if screen_size == (CANVAS_WIDTH, CANVAS_HEIGHT):
            self.screen.blit(self.canvas, (0, 0))
        else:
            self.screen.blit(pygame.transform.smoothscale(self.canvas, screen_size), (0, 0))
        pygame.display.flip()

    def dispatch(self, action: str):
        if action == "toggle_system" and self.city:
            new_state = not self.city.enabled
            self.loading(f"Switching trial to {'WITH' if new_state else 'WITHOUT'} CrossGuard...")
            try:
                self.city.reset(new_state)
            except Exception as exc:
                self.error = f"Reset error: {exc}"
            self.frames = {}
        elif action == "replay" and self.city:
            curr_state = self.city.enabled
            self.loading(f"Replaying scenario ({'WITH' if curr_state else 'WITHOUT'} system)...")
            try:
                self.city.reset(curr_state)
            except Exception as exc:
                self.error = f"Replay error: {exc}"
            self.frames = {}
        elif action == "receiver" and self.city:
            current = getattr(self.city, "receiver_mode", "autonomous")
            self.city.receiver_mode = "retrofit" if current == "autonomous" else "autonomous"
            self.loading(f"Preparing {self.city.receiver_mode} receiver case...")
            try:
                self.city.reset(self.city.enabled)
                self.error = None
                self._live_saved = False
            except Exception as exc:
                self.error = f"Receiver reset error: {exc}"
            self.frames = {}
        elif action == "pause":
            self.paused = not self.paused
        elif action == "hud":
            self.show_hud = not self.show_hud
        elif action == "comms":
            self.show_comms = not self.show_comms
        elif action == "quit":
            self.running = False

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_SPACE:
                    self.dispatch("pause")
                elif event.key == pygame.K_u:
                    self.dispatch("receiver")
                elif event.key == pygame.K_r:
                    self.dispatch("replay")
                elif event.key == pygame.K_t:
                    self.dispatch("hud")
                elif event.key == pygame.K_c:
                    self.dispatch("comms")
                elif event.key == pygame.K_1:
                    self.selected_view = "top"
                elif event.key == pygame.K_2:
                    self.selected_view = "driver"
                elif event.key == pygame.K_3:
                    self.selected_view = "person"
                elif event.key == pygame.K_4:
                    self.selected_view = "opposite"
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                screen_w, screen_h = self.screen.get_size()
                pt = (event.pos[0] * CANVAS_WIDTH / screen_w, event.pos[1] * CANVAS_HEIGHT / screen_h)

                # Camera switch buttons
                for cam_key, rect in self.cam_buttons.items():
                    if rect.collidepoint(pt):
                        self.selected_view = cam_key
                        break

                # Bottom control buttons
                for ctrl_key, rect in self.control_buttons.items():
                    if rect.collidepoint(pt):
                        self.dispatch(ctrl_key)
                        break

    def _draw_scene(self):
        """Draw single active full-view camera scene (NOT 3 tiles)."""
        raw_pixels = self.frames.get(self.selected_view)
        if raw_pixels is not None:
            # Carla/numpy frame format: (H, W, 3) RGB
            # Pygame make_surface expects (W, H, 3)
            # Never mutate raw_pixels; swapaxes produces a view/copy for presentation
            img_surf = pygame.surfarray.make_surface(raw_pixels.swapaxes(0, 1))
            if img_surf.get_size() != (CANVAS_WIDTH, CANVAS_HEIGHT):
                img_surf = pygame.transform.smoothscale(img_surf, (CANVAS_WIDTH, CANVAS_HEIGHT))

            # Apply display adjustments if any
            img_surf = apply_display_adjustments(img_surf, self.saturation, self.contrast)
            self.canvas.blit(img_surf, (0, 0))
        else:
            # Fallback neutral backdrop if frame not ready
            self.canvas.fill((32, 32, 32))
            wait_text = f"Awaiting frame feed for [{self.selected_view.upper()}]..."
            w_surf = self.font_main.render(wait_text, True, (180, 180, 180))
            self.canvas.blit(w_surf, (CANVAS_WIDTH // 2 - w_surf.get_width() // 2, CANVAS_HEIGHT // 2))

    def _draw_top_bar(self):
        """Unobtrusive top navigation bar."""
        bar_surf = pygame.Surface((CANVAS_WIDTH, 54), pygame.SRCALPHA)
        bar_surf.fill((19, 19, 19, 210))
        self.canvas.blit(bar_surf, (0, 0))

        # Brand
        title = self.font_title.render("CrossGuard", True, (245, 245, 245))
        sub = self.font_small.render("V2X DEFENSE", True, (170, 170, 170))
        self.canvas.blit(title, (24, 10))
        self.canvas.blit(sub, (162, 18))

        # Camera selection buttons (1-4)
        for key, rect in self.cam_buttons.items():
            is_active = (key == self.selected_view)
            bg_color = (87, 87, 87, 230) if is_active else (44, 44, 44, 180)
            btn_surf = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
            pygame.draw.rect(btn_surf, bg_color, (0, 0, rect.width, rect.height), border_radius=6)
            if is_active:
                pygame.draw.rect(btn_surf, (245, 245, 245), (0, 0, rect.width, rect.height), width=2, border_radius=6)
            self.canvas.blit(btn_surf, rect.topleft)

            lbl_surf = self.font_small.render(VIEW_LABELS[key], True, (247, 247, 247) if is_active else (200, 200, 200))
            self.canvas.blit(lbl_surf, (rect.x + (rect.width - lbl_surf.get_width()) // 2, rect.y + 9))

    def _draw_hud(self):
        """Translucent compact left HUD over scene displaying actual metrics only.

        None / missing values are rendered as unavailable/n-a, never fabricated.
        """
        if not self.show_hud:
            return

        metrics = self.city.metrics() if self.city else {}

        # Translucent background container
        hud_w, hud_h = 440, 780
        hud_x, hud_y = 24, 70
        hud_surf = pygame.Surface((hud_w, hud_h), pygame.SRCALPHA)
        hud_surf.fill((21, 21, 21, 215))
        pygame.draw.rect(hud_surf, (72, 72, 72, 180), (0, 0, hud_w, hud_h), width=1, border_radius=10)
        self.canvas.blit(hud_surf, (hud_x, hud_y))

        # Title
        ht_surf = self.font_bold.render("TELEMETRY & RISK MONITOR", True, (245, 245, 245))
        self.canvas.blit(ht_surf, (hud_x + 18, hud_y + 16))

        # Section separator
        pygame.draw.line(self.canvas, (72, 72, 72), (hud_x + 18, hud_y + 44), (hud_x + hud_w - 18, hud_y + 44), 1)

        def fmt_val(val: Any, unit: str = "", default: str = "Unavailable") -> Tuple[str, Tuple[int, int, int]]:
            if val is None:
                return default, (162, 162, 162)
            if isinstance(val, float):
                return f"{val:.2f}{unit}", (245, 245, 245)
            return f"{val}{unit}", (245, 245, 245)

        # Pull actual metrics
        speed = metrics.get("speed_kmh")
        throttle = metrics.get("throttle")
        brake = metrics.get("brake")
        steer = metrics.get("steer")
        sim_time = metrics.get("sim_time_s", getattr(self.city, "time", None))
        fps = self.fps_smoothed
        mode = "WITH CrossGuard" if (self.city and getattr(self.city, "enabled", False)) else "WITHOUT SYSTEM"
        sources = metrics.get("active_sources")
        obs_age = metrics.get("observation_age_s")
        conflict = metrics.get("conflict_in_s")
        margin = metrics.get("stopping_margin_m")
        state = metrics.get("decision_state")
        reason = metrics.get("reason")

        fields = [
            ("Vehicle Speed", fmt_val(speed, " km/h")),
            ("Throttle / Brake", (f"{throttle:.2f} / {brake:.2f}" if (throttle is not None and brake is not None) else "Unavailable", (245, 245, 245) if throttle is not None else (162, 162, 162))),
            ("Steering", fmt_val(steer)),
            ("Time / measured FPS", (f"{sim_time:.1f}s / {fps:.1f} fps" if sim_time is not None else f"n/a / {fps:.1f} fps", (245, 245, 245))),
            ("Mode", (mode, (245, 245, 245) if (self.city and self.city.enabled) else (176, 176, 176))),
            ("Perception Sources", (", ".join(sources) if sources else "None", (245, 245, 245) if sources else (162, 162, 162))),
            ("Observation Age", fmt_val(obs_age, " s")),
            ("Conflict in", fmt_val(conflict, " s", default="None")),
            ("Stopping Margin", fmt_val(margin, " m", default="Unavailable")),
            ("Decision State", fmt_val(state, default="Unavailable")),
            ("Active camera FPS", fmt_val(getattr(self, "active_sim_fps", None))),
            ("Simulation step rate", fmt_val(metrics.get("sim_fps"), " Hz")),
            ("Receiver case", fmt_val(getattr(self.city, "receiver_mode", "autonomous"))),
            ("Advisory", fmt_val(metrics.get("advisory_state"))),
            ("Driver delay (modeled)", fmt_val(metrics.get("driver_response_delay_s"), " s")),
            ("Messages sent / received", fmt_val(f"{metrics.get('warnings_sent', 0)} / {metrics.get('warnings_received', 0)}")),
            ("Dropped / expired", fmt_val(f"{metrics.get('messages_dropped', 0)} / {metrics.get('messages_expired', 0)}")),
            ("Closest pedestrian", fmt_val(metrics.get("minimum_distance_m"), " m")),
            ("Crosswalk valid", fmt_val(metrics.get("crosswalk_valid"))),
            ("Perception", fmt_val(metrics.get("perception_mode"))),
            ("Collisions", fmt_val(len(getattr(self.city, "collisions", [])))),
            ("Corridor stop guard", fmt_val(metrics.get("corridor_guard"))),
        ]

        curr_y = hud_y + 54
        for label, (v_str, v_col) in fields:
            l_surf = self.font_small.render(label, True, (182, 182, 182))
            val_surf = self.font_small.render(v_str, True, v_col)
            self.canvas.blit(l_surf, (hud_x + 18, curr_y))
            self.canvas.blit(val_surf, (hud_x + hud_w - 18 - val_surf.get_width(), curr_y))
            curr_y += 25

        # Decision / Reason block
        pygame.draw.line(self.canvas, (72, 72, 72), (hud_x + 18, curr_y + 6), (hud_x + hud_w - 18, curr_y + 6), 1)
        curr_y += 14
        r_title = self.font_small.render("Policy Reason:", True, (182, 182, 182))
        self.canvas.blit(r_title, (hud_x + 18, curr_y))
        curr_y += 22

        r_text = reason if reason else "No active risk advisory"
        # Wrap reason text if long
        r_words = r_text.split(" ")
        line = ""
        for word in r_words:
            test_line = f"{line} {word}".strip()
            if self.font_tiny.size(test_line)[0] > (hud_w - 36):
                w_surf = self.font_tiny.render(line, True, (225, 225, 225))
                self.canvas.blit(w_surf, (hud_x + 18, curr_y))
                curr_y += 18
                line = word
            else:
                line = test_line
        if line:
            w_surf = self.font_tiny.render(line, True, (225, 225, 225))
            self.canvas.blit(w_surf, (hud_x + 18, curr_y))

    def _draw_comms_panel(self):
        """Translucent communications event panel on right showing actual city.messages."""
        if not self.show_comms:
            return

        comms_w, comms_h = 540, 445
        comms_x, comms_y = CANVAS_WIDTH - comms_w - 24, 70
        surf = pygame.Surface((comms_w, comms_h), pygame.SRCALPHA)
        surf.fill((21, 21, 21, 215))
        pygame.draw.rect(surf, (72, 72, 72, 180), (0, 0, comms_w, comms_h), width=1, border_radius=10)
        self.canvas.blit(surf, (comms_x, comms_y))

        # Header
        head = self.font_bold.render("COMMUNICATIONS / V2X LINK", True, (245, 245, 245))
        self.canvas.blit(head, (comms_x + 18, comms_y + 16))
        sub = self.font_tiny.render("Explanatory link events (not physical radio)", True, (162, 162, 162))
        self.canvas.blit(sub, (comms_x + 18, comms_y + 40))

        pygame.draw.line(self.canvas, (72, 72, 72), (comms_x + 18, comms_y + 58), (comms_x + comms_w - 18, comms_y + 58), 1)

        # Retrieve actual events from city
        events = getattr(self.city, "messages", []) if self.city else []
        recent_events = events[-7:] if events else []
        receiver = getattr(self.city, "receiver_mode", "autonomous")
        flow = ["1  Visible pedestrian -> timestamped observation",
                "2  RSU (V2I) / bus (V2V) -> queued message",
                "3  Range / loss / latency / expiry checks",
                "4  Receiver checks age + conflict + stopping margin",
                "5  " + ("Vehicle controller -> brake/throttle" if receiver == "autonomous"
                          else "Audio advisory -> modeled driver delay -> brake")]
        for index, text in enumerate(flow):
            self.canvas.blit(self.font_tiny.render(text, True, (210, 210, 210)),
                             (comms_x + 18, comms_y + 70 + index * 22))

        if not recent_events:
            no_evt = self.font_small.render("No transmission events recorded", True, (162, 162, 162))
            self.canvas.blit(no_evt, (comms_x + 18, comms_y + 194))
            return

        curr_y = comms_y + 194
        for evt in reversed(recent_events):
            # Event format: event, at_s, sender_id, receiver_id, event_id, channel, observed_at_s, expires_at_s
            e_type = evt.get("event", "event").upper()
            t_s = evt.get("at_s", 0.0)
            s_id = evt.get("sender_id", "?")
            r_id = evt.get("receiver_id", "?")

            # Color tag based on event type
            tag_col = {
                "RECEIVED": (245, 245, 245),
                "SENT": (178, 178, 178),
                "DROPPED": (147, 147, 147),
                "EXPIRED": (167, 167, 167),
            }.get(e_type, (200, 200, 200))

            line1 = f"[{t_s:.1f}s] {e_type}: {s_id} -> {r_id}"
            l_surf = self.font_tiny.render(line1, True, tag_col)
            self.canvas.blit(l_surf, (comms_x + 18, curr_y))
            curr_y += 24

    def _draw_bottom_bar(self):
        """Unobtrusive bottom control bar and status line."""
        bar_h = 64
        bar_y = CANVAS_HEIGHT - bar_h
        bar_surf = pygame.Surface((CANVAS_WIDTH, bar_h), pygame.SRCALPHA)
        bar_surf.fill((19, 19, 19, 230))
        self.canvas.blit(bar_surf, (0, bar_y))

        # Status line
        if self.city:
            is_with = getattr(self.city, "enabled", False)
            is_finished = getattr(self.city, "finished", False)
            if self.paused:
                status_text = "PAUSED — Press Space to resume"
                stat_col = (176, 176, 176)
            elif is_finished:
                status_text = "TRIAL COMPLETED — Viewer persistent. Press 'R' to replay."
                stat_col = (245, 245, 245) if is_with else (176, 176, 176)
            else:
                status_text = "ACTIVE SIMULATION RUNNING"
                stat_col = (245, 245, 245)
            mode_text = "WITH CROSSGUARD SYSTEM" if is_with else "WITHOUT SYSTEM (BASELINE)"
        else:
            status_text = self.error if self.error else "INITIALIZING..."
            stat_col = (147, 147, 147) if self.error else (200, 200, 200)
            mode_text = "DISCONNECTED"

        st_surf = self.font_small.render(f"STATUS: {status_text}  |  MODE: {mode_text}", True, stat_col)
        self.canvas.blit(st_surf, (24, bar_y - 28))

        # Bottom buttons
        btn_labels = {
            "toggle_system": "SYSTEM: " + ("ON (WITH)" if (self.city and self.city.enabled) else "OFF (WITHOUT)"),
            "replay": "REPLAY (R)",
            "pause": "RESUME" if self.paused else "PAUSE (Space)",
            "hud": "HUD: ON (T)" if self.show_hud else "HUD: OFF (T)",
            "comms": "COMMS: ON (C)" if self.show_comms else "COMMS: OFF (C)",
            "receiver": "CASE: " + getattr(self.city, "receiver_mode", "autonomous").upper() + " (U)",
            "quit": "QUIT (Esc)",
        }

        for key, rect in self.control_buttons.items():
            btn_surf = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
            is_active = (key == "toggle_system" and self.city and self.city.enabled)
            bg = (87, 87, 87, 230) if is_active else (44, 44, 44, 200)
            pygame.draw.rect(btn_surf, bg, (0, 0, rect.width, rect.height), border_radius=6)
            pygame.draw.rect(btn_surf, (83, 83, 83), (0, 0, rect.width, rect.height), width=1, border_radius=6)
            self.canvas.blit(btn_surf, rect.topleft)

            lbl = self.font_small.render(btn_labels[key], True, (247, 247, 247) if is_active else (210, 210, 210))
            self.canvas.blit(lbl, (rect.x + (rect.width - lbl.get_width()) // 2, rect.y + 11))

    def _draw_driver_advisory(self):
        if getattr(self.city, "receiver_mode", "autonomous") != "retrofit":
            return
        receiver = getattr(self.city, "receiver", None)
        notification = getattr(receiver, "notification", None)
        if not notification:
            return
        event_id = notification.get("event_id")
        if event_id != getattr(self, "_sounded_advisory", None):
            self._sounded_advisory = event_id
            # Local demo beep represents a phone audio cue, not real phone delivery.
            if pygame.mixer.get_init():
                rate, _, channels = pygame.mixer.get_init()
                samples = (np.sin(2 * np.pi * 880 * np.arange(int(rate * 0.22)) / rate) * 10000).astype(np.int16)
                if channels == 2:
                    samples = np.repeat(samples[:, None], 2, axis=1)
                self._alert_sound = pygame.sndarray.make_sound(np.ascontiguousarray(samples))
                self._alert_sound.play()
        panel = pygame.Surface((880, 90), pygame.SRCALPHA)
        panel.fill((15, 15, 15, 235))
        self.canvas.blit(panel, (500, 850))
        self.canvas.blit(self.font_title.render("DRIVER ALERT: PEDESTRIAN AHEAD", True, (255, 255, 255)), (520, 864))
        state = getattr(receiver, "advisory_state", "unknown")
        self.canvas.blit(self.font_main.render(f"Simulated phone audio cue | {state} | driver response modeled", True, (205, 205, 205)), (520, 902))

    def render(self):
        self._draw_scene()
        self._draw_top_bar()
        self._draw_hud()
        self._draw_comms_panel()
        self._draw_bottom_bar()
        self._draw_driver_advisory()
        self.flip()
        now = time.monotonic()
        if self.frames and not isinstance(self.city, MockWorld) and now - getattr(self, "_last_perf_save", 0.0) >= 1.0:
            self._last_perf_save = now
            output = Path(__file__).resolve().parents[1] / "results" / "viewer_performance.json"
            output.write_text(json.dumps({"measured_loop_fps": round(self.fps_smoothed, 2),
                                         "last_active_camera_fps": round(getattr(self, "active_sim_fps", 0.0), 2),
                                         "target_display_fps": 60,
                                         "simulation_step_hz": self.city.metrics().get("sim_fps"),
                                         "simulation_time_s": getattr(self.city, "time", None),
                                         "receiver_mode": getattr(self.city, "receiver_mode", "autonomous"),
                                         "window_size": self.screen.get_size()}, indent=2))
        if self.frames and getattr(self.city, "time", 0) >= 1.0 and not isinstance(self.city, MockWorld) and not getattr(self, "_live_saved", False):
            capture = Path(__file__).resolve().parents[1] / "results" / "live_preview.png"
            capture.parent.mkdir(parents=True, exist_ok=True)
            pygame.image.save(self.screen, str(capture))
            self._live_saved = True
            print(f"[Viewer] Live preview saved: {capture}", flush=True)

    def run(self):
        while self.running:
            now = time.monotonic()
            dt = max(1e-4, now - self.last_frame_time)
            self.last_frame_time = now
            self.fps_smoothed = 0.9 * self.fps_smoothed + 0.1 * (1.0 / dt)

            self.handle_events()
            if not self.running:
                break

            if self.city and not self.paused and not self.error and not self.city.finished:
                try:
                    if hasattr(self.city, "set_view"):
                        self.city.set_view(self.selected_view)
                    self.frames = self.city.tick()
                    self.active_sim_fps = self.fps_smoothed
                except Exception as exc:
                    self.error = f"Tick error: {exc}"
                    traceback.print_exc()

            self.render()

            if self.paused or not self.city:
                time.sleep(0.03)
            else:
                self.clock.tick(60)

        if self.city:
            try:
                self.city.close()
            except Exception:
                pass
        pygame.quit()


def run_verify_ui(args: argparse.Namespace) -> int:
    """Headless automated UI verification using SDL dummy driver.

    Exercises:
    - 4 camera view selections (top, driver, person, opposite)
    - WITH / WITHOUT technology toggling
    - Pause / Resume dispatch
    - Replay dispatch
    - HUD and Comms panel visibility toggles
    - Quit action
    - Screenshot generation and verification JSON write under src/results/ui_verification.
    """
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    viewer = Viewer(args)

    from crossguard.paths import RESULTS_ROOT
    results_dir = RESULTS_ROOT / "ui_verification"
    results_dir.mkdir(parents=True, exist_ok=True)

    verification_log = {
        "verified_cameras": [],
        "toggles_tested": [],
        "screenshots": [],
        "passed": False,
        "metrics_samples": {},
    }

    try:
        # 1. Exercise Camera view switching
        for cam in ("driver", "person", "opposite", "top"):
            viewer.selected_view = cam
            if viewer.city:
                viewer.frames = viewer.city.tick()
            viewer.render()
            verification_log["verified_cameras"].append(cam)

        # 2. Exercise System Toggle (WITH -> WITHOUT -> WITH)
        assert viewer.city is not None
        initial_mode = viewer.city.enabled
        viewer.dispatch("toggle_system")
        assert viewer.city.enabled != initial_mode, "System toggle failed"
        verification_log["toggles_tested"].append("toggle_system_off")

        viewer.dispatch("toggle_system")
        assert viewer.city.enabled == initial_mode, "System toggle back failed"
        verification_log["toggles_tested"].append("toggle_system_on")

        # 3. Exercise Pause / Resume
        viewer.dispatch("pause")
        assert viewer.paused is True, "Pause toggle failed"
        viewer.dispatch("pause")
        assert viewer.paused is False, "Resume toggle failed"
        verification_log["toggles_tested"].append("pause_resume")

        # 4. Exercise HUD & Comms Toggles
        viewer.dispatch("hud")
        assert viewer.show_hud is False
        viewer.dispatch("hud")
        assert viewer.show_hud is True
        verification_log["toggles_tested"].append("hud_toggle")

        viewer.dispatch("comms")
        assert viewer.show_comms is False
        viewer.dispatch("comms")
        assert viewer.show_comms is True
        verification_log["toggles_tested"].append("comms_toggle")

        # 5. Exercise Replay
        viewer.dispatch("replay")
        verification_log["toggles_tested"].append("replay")

        # 6. Capture screenshot
        if viewer.city:
            viewer.frames = viewer.city.tick()
        viewer.render()

        screenshot_path = results_dir / "ui_verification_screenshot.png"
        pygame.image.save(viewer.canvas, str(screenshot_path))
        verification_log["screenshots"].append(str(screenshot_path))

        # Log metrics sample
        verification_log["metrics_samples"] = viewer.city.metrics() if viewer.city else {}
        verification_log["passed"] = True

        # Write verification report
        json_path = results_dir / "verification.json"
        json_path.write_text(json.dumps(verification_log, indent=2), encoding="utf-8")
        print(f"[Verification] Success! Report saved to {json_path}")

        # 7. Exercise Quit
        viewer.dispatch("quit")
        assert viewer.running is False, "Quit dispatch failed"
        return 0

    except Exception as exc:
        verification_log["error"] = str(exc)
        verification_log["traceback"] = traceback.format_exc()
        json_path = results_dir / "verification.json"
        json_path.write_text(json.dumps(verification_log, indent=2), encoding="utf-8")
        print(f"[Verification] Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if viewer.city:
            viewer.city.close()
        pygame.quit()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CrossGuard Pygame Single-Active-Camera Viewer")
    parser.add_argument("--cars", type=int, default=35, help="Number of background vehicles")
    parser.add_argument("--people", type=int, default=75, help="Number of background pedestrians")
    parser.add_argument("--seed", type=int, default=7, help="Simulation random seed")
    parser.add_argument("--width", type=int, default=1280, help="Window display width")
    parser.add_argument("--height", type=int, default=720, help="Window display height")
    parser.add_argument("--mock-demo", action="store_true", help="Run with deterministic testable MockWorld (no CARLA)")
    parser.add_argument("--verify-ui", action="store_true", help="Run headless SDL dummy event & UI verification test")
    parser.add_argument("--receiver", choices=["autonomous", "retrofit"], default="autonomous", help="Simulated receiver case")
    parser.add_argument("--perception", type=str, default="ideal", choices=["ideal", "rgb"], help="Perception mode")
    parser.add_argument("--weights", type=str, default=None, help="Weights path if running RGB perception")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verify_ui:
        sys.exit(run_verify_ui(args))
    else:
        viewer = Viewer(args)
        viewer.run()


if __name__ == "__main__":
    main()
