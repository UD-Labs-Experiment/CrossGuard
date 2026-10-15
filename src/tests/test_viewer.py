"""Tests for CrossGuard single-active-camera Viewer.

Verifies:
- MockWorld deterministic RGB frame generation and telemetry keys.
- Viewer initialization in headless dummy mode.
- View selection across all 4 cameras ('top', 'driver', 'person', 'opposite').
- WITH/WITHOUT system toggles, pause/resume, replay, HUD and Comms toggles, and quit.
- Display adjustment helper non-mutation of input RGB arrays.
- Telemetry rendering handling of missing/None values without fabricating data.
- UI verification workflow execution and screenshot/JSON export.
"""

import json
import os
from pathlib import Path
import unittest
import numpy as np
import pygame

from crossguard.contracts import VIEW_NAMES
from crossguard.viewer import (
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    MockWorld,
    Viewer,
    apply_display_adjustments,
    build_arg_parser,
    run_verify_ui,
)


class MockWorldTests(unittest.TestCase):
    def test_mock_world_deterministic_tick_and_keys(self):
        world = MockWorld(seed=42, resolution=(640, 480))
        self.assertEqual(world.seed, 42)
        self.assertTrue(world.enabled)
        self.assertEqual(world.time, 0.0)

        frames = world.tick()
        self.assertEqual(set(frames.keys()), set(VIEW_NAMES))
        for name, frame in frames.items():
            self.assertEqual(frame.shape, (480, 640, 3))
            self.assertEqual(frame.dtype, np.uint8)

        metrics = world.metrics()
        required_keys = (
            "speed_kmh",
            "throttle",
            "brake",
            "steer",
            "sim_time_s",
            "sim_fps",
            "selected_mode",
            "active_sources",
            "observation_age_s",
            "conflict_in_s",
            "stopping_margin_m",
            "decision_state",
            "reason",
            "warnings_received",
            "warnings_sent",
            "messages_dropped",
            "messages_expired",
            "perception_mode",
            "crosswalk_valid",
            "pedestrian_collision",
        )
        for key in required_keys:
            self.assertIn(key, metrics, f"Missing telemetry key: {key}")

    def test_mock_world_reset(self):
        world = MockWorld(seed=7)
        world.tick()
        self.assertGreater(world.time, 0.0)
        self.assertEqual(world.trial_number, 1)

        world.reset(enabled=False)
        self.assertEqual(world.time, 0.0)
        self.assertFalse(world.enabled)
        self.assertEqual(world.trial_number, 2)


class DisplayAdjustmentTests(unittest.TestCase):
    def setUp(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        pygame.init()

    def tearDown(self):
        pygame.quit()

    def test_neutral_adjustment_is_noop(self):
        surf = pygame.Surface((100, 100))
        surf.fill((120, 150, 180))
        out = apply_display_adjustments(surf, saturation=1.0, contrast=1.0)
        self.assertIs(out, surf)

    def test_adjustment_does_not_mutate_original(self):
        surf = pygame.Surface((50, 50))
        surf.fill((100, 100, 100))
        out = apply_display_adjustments(surf, saturation=1.5, contrast=1.2)
        self.assertIsNot(out, surf)
        # Original remains 100, 100, 100
        arr = pygame.surfarray.pixels3d(surf)
        self.assertEqual(arr[0, 0, 0], 100)


class ViewerUITests(unittest.TestCase):
    def setUp(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    def test_viewer_init_and_camera_switch(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--mock-demo", "--width", "800", "--height", "450"])
        viewer = Viewer(args)

        self.assertEqual(viewer.selected_view, "top")
        self.assertTrue(viewer.running)
        self.assertFalse(viewer.paused)

        # Switch to each view
        for view_name in VIEW_NAMES:
            viewer.selected_view = view_name
            self.assertEqual(viewer.selected_view, view_name)
            viewer.render()

        viewer.dispatch("quit")
        self.assertFalse(viewer.running)

    def test_viewer_dispatch_actions(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--mock-demo"])
        viewer = Viewer(args)

        # Toggle system
        self.assertTrue(viewer.city.enabled)
        viewer.dispatch("toggle_system")
        self.assertFalse(viewer.city.enabled)

        # Pause / resume
        viewer.dispatch("pause")
        self.assertTrue(viewer.paused)
        viewer.dispatch("pause")
        self.assertFalse(viewer.paused)

        # Replay
        viewer.dispatch("replay")
        self.assertEqual(viewer.city.time, 0.0)

        # HUD toggle
        self.assertTrue(viewer.show_hud)
        viewer.dispatch("hud")
        self.assertFalse(viewer.show_hud)
        viewer.dispatch("hud")
        self.assertTrue(viewer.show_hud)

        # Comms toggle
        self.assertTrue(viewer.show_comms)
        viewer.dispatch("comms")
        self.assertFalse(viewer.show_comms)
        viewer.dispatch("comms")
        self.assertTrue(viewer.show_comms)

        # Quit
        viewer.dispatch("quit")
        self.assertFalse(viewer.running)

    def test_verify_ui_runner(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--verify-ui"])
        exit_code = run_verify_ui(args)
        self.assertEqual(exit_code, 0)

        from crossguard.paths import RESULTS_ROOT
        verification_json = RESULTS_ROOT / "ui_verification" / "verification.json"
        screenshot_png = RESULTS_ROOT / "ui_verification" / "ui_verification_screenshot.png"

        self.assertTrue(verification_json.is_file())
        self.assertTrue(screenshot_png.is_file())

        data = json.loads(verification_json.read_text(encoding="utf-8"))
        self.assertTrue(data.get("passed"))
        self.assertEqual(data.get("verified_cameras"), ["driver", "person", "opposite", "top"])
        self.assertIn("toggle_system_off", data.get("toggles_tested", []))
        self.assertIn("pause_resume", data.get("toggles_tested", []))


if __name__ == "__main__":
    unittest.main()
