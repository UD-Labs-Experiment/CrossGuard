"""Standalone CrossGuard CARLA CityWorld with marked crosswalk validation,
multi-camera views, instance-visible perception, direct V2V / V2I communication,
and RiskPolicy collision avoidance.

Coordinates use SI units and CARLA simulation seconds.
All run outputs, metrics, and source snapshots are saved under src/results/.
"""

from collections import deque
from dataclasses import replace
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import random
import shutil
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import carla
import numpy as np
from PIL import Image

from crossguard.contracts import (
    VIEW_NAMES,
    Decision,
    Message,
    Observation,
    VehicleState,
)
from crossguard.communication.link import SimulatedNetwork
from crossguard.communication.channels import ChannelNetwork, ChannelConfig, OpticalGeometry
from crossguard.communication.braking import BrakingEvent, PhoneAdvisoryModel, relay_braking
from crossguard.policy.risk import RiskPolicy
from crossguard.policy.following import AlongLaneState, FollowingPolicy, bumper_gap, group_recipient_ids
from crossguard.policy.following import DEFAULT_FOLLOWING_GAP_M, validate_following_gap
from crossguard.experiments import number, pedestrian_motion, validate_follower_model, trial_end_reason
from crossguard.policy.following import DelayedFollowingPolicy, SampledFollowingPolicy
from crossguard.simulation.receiver import ReceiverController
from crossguard.simulation.ownership import CarlaLease
from crossguard.simulation.weather_guard import ensure_weather, WeatherReadinessError

DT = 1.0 / 60.0  # 60 Hz fixed simulation delta; wall FPS measured by viewer
from crossguard.provenance import register_source,save_runtime_sources
register_source(__name__,__file__)
SRC_ROOT = Path(__file__).resolve().parents[2]
from crossguard.paths import RESULTS_ROOT,CARLA_LOCK_ROOT


def straight_corridor_control(position, yaw_degrees, speed_mps, origin, forward,
                              end_distance_m, lane_width_m=3.5,
                              vehicle_half_width_m=0.95, vehicle_half_length_m=2.3):
    """Bounded straight-line feedback and a conservative corridor-stop guard.

    Inputs are planar tuples and degrees. Positive CARLA yaw/steer turns right.
    The fixed spawn-lane axis never queries junction successor waypoints. The
    caller must latch a stop until reset, since braking reduces stopping distance.
    This geometric guard does not certify the continuation as drivable road.
    """
    axis_length = math.hypot(*forward)
    if axis_length <= 1e-9:
        raise ValueError("Corridor direction must be nonzero")
    fx, fy = forward[0] / axis_length, forward[1] / axis_length
    dx, dy = position[0] - origin[0], position[1] - origin[1]
    along = dx * fx + dy * fy
    cross_track = dx * -fy + dy * fx
    speed = max(0.0, speed_mps)
    lookahead = max(8.0, 0.8 * speed)
    desired_yaw = math.atan2(fy, fx) + math.atan2(-cross_track, lookahead)
    yaw_error = (desired_yaw - math.radians(yaw_degrees) + math.pi) % (2 * math.pi) - math.pi
    steer = max(-0.25, min(0.25, yaw_error / math.radians(45)))
    lateral_limit = max(0.0, lane_width_m / 2 - vehicle_half_width_m - 0.15)
    # 4 m/sÂ² conservative deceleration, 0.15s actuation allowance, plus front.
    stopping_distance = speed * 0.15 + speed * speed / 8 + vehicle_half_length_m + 0.5
    stop = (end_distance_m - along <= stopping_distance
            or abs(cross_track) >= lateral_limit
            or abs(yaw_error) > math.radians(45)
            or along < -2.0)
    return steer, stop


def instance_counts(raw: np.ndarray) -> Dict[int, int]:
    """Extract pedestrian instance counts from CARLA instance segmentation buffer.

    In CARLA BGRA instance segmentation buffer:
    - Red channel (channel index 2) == 12 is the pedestrian semantic class.
    - Green (channel index 1) is the low byte.
    - Blue (channel index 0) is the high byte of the 16-bit actor ID.
    """
    if raw is None or raw.size == 0:
        return {}
    ped_mask = raw[:, :, 2] == 12
    pixels = raw[ped_mask]
    if len(pixels) == 0:
        return {}
    ids = pixels[:, 1].astype(np.uint16) + (pixels[:, 0].astype(np.uint16) << 8)
    values, counts = np.unique(ids, return_counts=True)
    return {int(v): int(n) for v, n in zip(values, counts) if n >= 8}


def look_at(location: carla.Location, target: carla.Location) -> carla.Transform:
    """Compute CARLA Transform at location pointing toward target."""
    dx = target.x - location.x
    dy = target.y - location.y
    dz = target.z - location.z
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return carla.Transform(location, carla.Rotation(pitch=pitch, yaw=yaw, roll=0.0))


class Stream:
    """Synchronous camera stream reader for CARLA RGB and segmentation sensors."""

    def __init__(
        self,
        world: carla.World,
        actors: list,
        transform: carla.Transform,
        kind: str = "rgb",
        parent: Optional[carla.Actor] = None,
        size: Tuple[int, int] = (1920, 1080),
        fov: float = 90.0,
    ):
        bp = world.get_blueprint_library().find("sensor.camera." + kind)
        bp.set_attribute("image_size_x", str(size[0]))
        bp.set_attribute("image_size_y", str(size[1]))
        bp.set_attribute("fov", str(fov))

        if kind == "rgb":
            # Natural moderate exposure settings (compatible with Epic and Low quality)
            bp.set_attribute("iso", "100")
            bp.set_attribute("shutter_speed", "200")
            bp.set_attribute("fstop", "2.8")

        self.sensor = (
            world.spawn_actor(bp, transform, attach_to=parent)
            if parent
            else world.spawn_actor(bp, transform)
        )
        actors.append(self.sensor)
        self.queue: queue.Queue = queue.Queue()
        self.sensor.listen(self.queue.put)
        self.last: Optional[np.ndarray] = None
        self.size = size
        self.fov = fov
        self.last_frame = None
        self.last_timestamp = None
        self.last_transform = None

    def read(self, frame: int) -> Optional[np.ndarray]:
        while True:
            image = self.queue.get(timeout=15.0)
            if image.frame > frame:
                raise RuntimeError(
                    f"Camera frame skipped (expected {frame}, got {image.frame}); "
                    "another client may be ticking the world."
                )
            if image.frame == frame:
                self.last_frame = image.frame
                self.last_timestamp = image.timestamp
                self.last_transform = np.array(image.transform.get_matrix())
                self.last = (
                    np.frombuffer(image.raw_data, np.uint8)
                    .reshape(image.height, image.width, 4)
                    .copy()
                )
                return self.last

    def rgb(self) -> Optional[np.ndarray]:
        if self.last is None:
            return None
        # Convert BGRA to RGB
        return self.last[:, :, :3][:, :, ::-1].copy()


def extract_crosswalk_polygons(
    raw_points: List[carla.Location],
) -> List[List[carla.Location]]:
    """Group flat crosswalk vertices into individual crosswalk polygons."""
    if not raw_points:
        return []
    # CARLA emits closed rings. Retain support for older unclosed test fixtures.
    has_closed_ring = any(raw_points[i].distance(raw_points[j]) < 0.1
                          for i in range(len(raw_points)) for j in range(i + 3, len(raw_points)))
    if not has_closed_ring:
        groups = []
        group = []
        for pt in raw_points:
            if group and pt.distance(group[-1]) > 15.0:
                if len(group) >= 3:
                    groups.append(group)
                group = []
            group.append(pt)
        if len(group) >= 3:
            groups.append(group)
        return groups
    polygons: List[List[carla.Location]] = []
    current: List[carla.Location] = []
    for pt in raw_points:
        if current and len(current) >= 3 and pt.distance(current[0]) < 0.1:
            polygons.append(current)
            current = []
            continue
        current.append(pt)
    if len(current) >= 3:
        polygons.append(current)
    return polygons



def validate_and_find_crosswalk(
    carla_map: carla.Map,
    bus_wp: carla.Waypoint,
) -> Dict[str, Any]:
    """Find and geometrically validate a marked crosswalk on the straight adjacent-lane approach.

    Raises RuntimeError if no geometrically valid crosswalk is found.
    """
    raw_crosswalks = carla_map.get_crosswalks()
    if not raw_crosswalks:
        raise RuntimeError(
            "Map contains no crosswalks. This scenario requires Town10HD with marked crosswalks."
        )

    polygons = extract_crosswalk_polygons(raw_crosswalks)
    forward = bus_wp.transform.get_forward_vector()
    bus_loc = bus_wp.transform.location

    best_poly = None
    best_dist = math.inf
    best_center = None
    best_cw_info: Optional[Dict[str, Any]] = None

    for poly in polygons:
        center_x = sum(p.x for p in poly) / len(poly)
        center_y = sum(p.y for p in poly) / len(poly)
        center_z = sum(p.z for p in poly) / len(poly)
        center_loc = carla.Location(x=center_x, y=center_y, z=center_z)

        # Vector from bus to crosswalk center
        dx = center_loc.x - bus_loc.x
        dy = center_loc.y - bus_loc.y
        longitudinal_dist = dx * forward.x + dy * forward.y

        # Crosswalk must be downstream of bus (within 5m to 35m)
        if 5.0 <= longitudinal_dist <= 35.0:
            # Check waypoint on driving lane at crosswalk center
            wp = carla_map.get_waypoint(
                center_loc, project_to_road=True, lane_type=carla.LaneType.Driving
            )
            # Crosswalks at junction boundaries have connector road IDs.
            # Require the approach lane to intersect the lateral polygon span.
            lateral = [((p.x - bus_loc.x) * -forward.y +
                        (p.y - bus_loc.y) * forward.x) for p in poly]
            if wp and min(lateral) <= 0.0 <= max(lateral):
                # Validate polygon width and length
                min_x = min(p.x for p in poly)
                max_x = max(p.x for p in poly)
                min_y = min(p.y for p in poly)
                max_y = max(p.y for p in poly)
                extent = math.hypot(max_x - min_x, max_y - min_y)

                if extent >= 4.0:
                    if longitudinal_dist < best_dist:
                        best_dist = longitudinal_dist
                        best_poly = poly
                        best_center = center_loc
                        best_cw_info = {
                            "center": (center_loc.x, center_loc.y, center_loc.z),
                            "polygon": [(p.x, p.y, p.z) for p in poly],
                            "longitudinal_distance_m": round(longitudinal_dist, 2),
                            "road_id": wp.road_id,
                            "lane_id": wp.lane_id,
                            "extent_m": round(extent, 2),
                        }

    if best_poly is None or best_cw_info is None:
        raise RuntimeError(
            "Crosswalk geometric validation failed: No valid marked crosswalk found "
            f"on road {bus_wp.road_id} downstream of bus. Arbitrary crossing is disallowed."
        )

    return best_cw_info


class CityWorld:
    """Standalone CARLA simulation world for CrossGuard V2X safety evaluation.

    Features:
    - Native Town10HD straight adjacent-lane corridor with validated marked crosswalk.
    - True bus occlusion of crossing pedestrian.
    - 4 synchronous 1920x1080 camera views: 'top', 'driver', 'person', 'opposite'.
    - Ideal instance-visible perception (driver_seg, rsu_seg, bus_seg).
    - V2I (RSU) and V2V (Bus) simulated communication via SimulatedNetwork.
    - RiskPolicy decision and collision avoidance integration.
    - Controlled interactive toggle vs matched full-world reload evaluation resets.
    """

    def __init__(
        self,
        town: str = "Town10HD",
        vehicles: int = 35,
        walkers: int = 75,
        seed: int = 7,
        progress: Callable[[str], None] = lambda message: None,
        resolution: Tuple[int, int] = (1920, 1080),
        perception_mode: str = "rgb",
        enabled: bool = True,
        receiver_mode: str = "autonomous",
        **kwargs: Any,
    ):
        """Initialize CARLA world, load map, and spawn background traffic.

        Documented **kwargs:
        - latency_s (float): V2X base latency in seconds (default: 0.15).
        - jitter_s (float): V2X jitter bound in seconds (default: 0.0).
        - loss (float): V2X packet loss rate [0, 1] (default: 0.0).
        - range_m (float): V2X communication range in meters (default: 150.0).
        - host (str): CARLA server IP (default: '127.0.0.1').
        - port (int): CARLA RPC port (default: 2000).
        - driver_response_delay_s (float): simulated retrofit driver delay (default: 1.0).
        """
        self.town_name = town
        self.study_options = kwargs.get('study_options') or {}
        self.study_rows = []
        from crossguard.simulation.encounter_timing import EncounterTrigger
        timing=self.study_options.get("encounter_timing")
        self.encounter_trigger=EncounterTrigger(timing["tolerance_s"],timing["minimum_speed_mps"]) if timing else None
        self.diagnostic_brake_at = None
        self.scene_preflight=kwargs.get('scene_preflight',False)
        self.record_six_views=kwargs.get('record_six_views',False)
        if not isinstance(self.record_six_views,bool):
            raise ValueError('Invalid record_six_views')
        self.observation_window_s=kwargs.get('observation_window_s')
        if self.observation_window_s is not None:
            self.observation_window_s=number(self.observation_window_s,'observation_window_s',20,30)
        self.corridor_end_reserve_m=60. if town in ('Town02','Town03') else 45.
        if not isinstance(self.scene_preflight,bool):
            raise ValueError('Invalid scene_preflight')
        if town.rsplit("/", 1)[-1] not in ("Town10HD", "Town10HD_Opt", "Town03") and not (town=='Town02' and self.scene_preflight):
            raise ValueError("Invalid town for current scene: supported geometry is Town10HD or Town03")
        if town=='Town03' and not self.scene_preflight and self.observation_window_s!=20:
            raise ValueError('Town03 quantitative trials require the declared 20-second encounter window')
        self.target_speed_mps = number(kwargs.get("target_speed_kmh", 40.0), "target_speed_kmh", 5, 60) / 3.6
        self.crossing_trigger_distance_m = number(kwargs.get("crossing_trigger_distance_m", 60.0), "crossing_trigger_distance_m", 5, 100)
        self.retreat_after_s = number(kwargs.get("retreat_after_s", 2.0), "retreat_after_s", .1, 10)
        self.pedestrian_behavior = kwargs.get("pedestrian_behavior", "cross")
        if self.pedestrian_behavior not in ("cross", "wait", "retreat"):
            raise ValueError("Invalid pedestrian_behavior")
        self.scenario_definition = kwargs.get("scenario_definition")
        self.follower_model = kwargs.get("follower_model", "ideal")
        self.follower_delay_s = kwargs.get("follower_delay_s", 1.0)
        validate_follower_model(self.follower_model, self.follower_delay_s)
        self.vehicle_count = vehicles
        self.walker_count = walkers
        self.seed = seed
        self.group_mode = kwargs.get("group_mode", "single")
        group_recipient_ids(self.group_mode, True)  # Validate before touching CARLA.
        self.following_gap_m = validate_following_gap(kwargs.get("following_gap_m", DEFAULT_FOLLOWING_GAP_M))
        self.communication_profile = kwargs.get("communication_profile", "radio")
        if self.communication_profile not in ("radio", "cellular", "optical", "hybrid"):
            raise ValueError("Invalid communication profile")
        self.optical_enabled = kwargs.get("optical_enabled", True)
        self.phone_available = kwargs.get("phone_available", True)
        self.resolution = resolution
        if perception_mode not in ("rgb", "ideal_instance_visible"):
            raise ValueError("perception_mode must be rgb or explicitly ideal_instance_visible")
        self.perception_mode = perception_mode
        self.weights = str(kwargs.get("weights", SRC_ROOT / "models" / "yolo11n.onnx"))
        self.inference_device = kwargs.get("device", "gpu")
        self.detector = None
        self.pipeline = None
        if perception_mode == "rgb":
            from crossguard.perception.onnx_detection import OnnxYoloDetector
            from crossguard.perception.detection import YoloDetector
            factory = OnnxYoloDetector if Path(self.weights).suffix == ".onnx" else YoloDetector
            self.detector = factory(self.weights, confidence=0.35, device=self.inference_device)
        self.baseline_behavior = kwargs.get("baseline_behavior", "local_vision")
        if self.baseline_behavior not in ("local_vision", "cautious"):
            raise ValueError("baseline_behavior must be local_vision or cautious")
        self.enabled = enabled
        self.receiver_mode = receiver_mode
        self.driver_response_delay_s = float(kwargs.get("driver_response_delay_s", 1.0))
        if self.study_options:
            self.driver_response_delay_s = self.study_options.get('driver_response_s', 1.)
        self.receiver = ReceiverController(receiver_mode, self.driver_response_delay_s,self.target_speed_mps)
        self.kwargs = kwargs

        # Communication network parameters
        self.latency_s = float(kwargs.get("latency_s", 0.15))
        self.jitter_s = float(kwargs.get("jitter_s", 0.0))
        self.loss = float(kwargs.get("loss", 0.0))
        self.range_m = float(kwargs.get("range_m", 150.0))

        host = kwargs.get("host", "127.0.0.1")
        port = int(kwargs.get("port", 2000))

        self.client = carla.Client(host, port)
        self.client.set_timeout(90.0)

        self.background: List[carla.Actor] = []
        self.controllers: List[carla.Actor] = []
        self.scenario: List[carla.Actor] = []
        self.unit: List[carla.Actor] = []

        self.world: Optional[carla.World] = None
        self.original: Optional[carla.WorldSettings] = None
        self.tm: Optional[carla.TrafficManager] = None

        # Create output directory under src/results
        self.output = RESULTS_ROOT / ("city_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        self.output.mkdir(parents=True, exist_ok=True)

        self.trial_number = 0
        self.random = random.Random(self.seed)

        # Public interface attributes
        self.time = 0.0
        self.finished = False
        self.rows: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []

        try:
            self._lease = CarlaLease(CARLA_LOCK_ROOT / f"carla_{port}.lock")
            progress(f"Loading CARLA {town}...")
            self.world = self.client.load_world(town)
            self.map = self.world.get_map()
            self.original = self.world.get_settings()

            settings = self.world.get_settings()
            settings.synchronous_mode = True
            settings.fixed_delta_seconds = DT
            settings.no_rendering_mode = False
            settings.substepping = True
            settings.max_substep_delta_time = 0.01
            settings.max_substeps = 10
            self.world.apply_settings(settings)

            self._apply_trial_weather()
            self.lib = self.world.get_blueprint_library()

            # Keep Traffic Manager's RPC service separate from the HTTP bridge.
            self.tm = self.client.get_trafficmanager(8010)
            self.tm.set_synchronous_mode(True)
            self.tm.set_random_device_seed(self.seed)
            self.tm.global_percentage_speed_difference(25.0)
            self.tm.set_global_distance_to_leading_vehicle(3.0)

            self.scene_candidate_id=None
            self.bus_spawn_transform=None
            self.crossing_type='unmarked' if town=='Town02' else 'marked'
            self.pedestrian_crossing_duration_s=14.
            if town=='Town03':
                from crossguard.simulation.sites import discover_sites
                inventory=discover_sites(self.map)
                candidate=next((c for c in inventory['candidates'] if c['candidate_id']=='Town03_02'),None)
                if candidate is None:
                    raise RuntimeError('Town03 preflight candidate no longer passes geometry checks')
                self.scene_candidate_id=candidate['candidate_id']
                bus,car=candidate['bus_waypoint'],candidate['car_waypoint']
                if (car['road_id'],car['lane_id'],round(car['s'],2))!=(667,2,4.09) or (bus['road_id'],bus['lane_id'],round(bus['s'],2))!=(1,3,5.):
                    raise RuntimeError('Town03 candidate differs from the frozen preflight site')
                self.bus_wp=self.map.get_waypoint_xodr(bus['road_id'],bus['lane_id'],bus['s'])
                self.car_wp=self.map.get_waypoint_xodr(car['road_id'],car['lane_id'],car['s'])
            elif town=='Town02':
                from crossguard.simulation.sites import discover_unmarked_sites
                candidate=next((c for c in discover_unmarked_sites(self.map)['candidates'] if c['candidate_id']=='Town02_unmarked_01'),None)
                if candidate is None:
                    raise RuntimeError('No Town02 unmarked candidate passes road geometry')
                self.scene_candidate_id=candidate['candidate_id']
                car,bus=candidate['car_waypoint'],candidate['proposed_bus_pose']
                if (car['road_id'],car['lane_id'],round(car['s'],2))!=(12,-1,30.):
                    raise RuntimeError('Town02 candidate differs from the nominated road site')
                self.car_wp=self.map.get_waypoint_xodr(car['road_id'],car['lane_id'],car['s'])
                self.bus_wp=self.map.get_waypoint_xodr(bus['road_id'],bus['lane_id'],bus['s'])
                self.bus_spawn_transform=carla.Transform(carla.Location(*bus['location']),carla.Rotation(pitch=bus['rotation'][0],yaw=bus['rotation'][1],roll=bus['rotation'][2]))
                start_offset=self.car_wp.lane_width/2+3.
                left=self.car_wp.get_left_lane()
                far_offset=self.car_wp.lane_width/2+.5+(left.lane_width if left is not None and left.lane_type==carla.LaneType.Driving else 0.)
                self.pedestrian_crossing_duration_s=(start_offset+far_offset)/1.4
                self.crosswalk_info={'center':candidate['crossing_center'],'polygon':[],
                    'crossing_type':'unmarked','native_marked_crosswalk':False,
                    'pedestrian_start_offset_m':start_offset,
                    'provenance':'explicitly modeled unmarked road crossing; no native marking claimed'}
            else:
                # Existing Town10HD straight adjacent-lane scene.
                site=self.study_options.get('site')
                if site:
                    self.scene_candidate_id=site['candidate_id']
                    car,bus=site['car_waypoint'],site['bus_waypoint']
                    self.car_wp=self.map.get_waypoint_xodr(car['road_id'],car['lane_id'],car['s'])
                    self.bus_wp=self.map.get_waypoint_xodr(bus['road_id'],bus['lane_id'],bus['s'])
                else:
                    self.bus_wp = self.map.get_waypoint_xodr(10, -2, 104).next(46.0)[0]
                    self.car_wp = self.map.get_waypoint_xodr(10, -1, 100)
            if self.bus_wp is None or self.car_wp is None:
                raise RuntimeError(
                    "This scenario requires Town10HD geometry with road 10 lanes -1 and -2."
                )

            self.forward = self.bus_wp.transform.get_forward_vector()
            self.right = self.bus_wp.transform.get_right_vector()
            if self.crossing_type=='unmarked' or self.study_options.get('site'):
                self.forward=self.car_wp.transform.get_forward_vector()
                self.right=self.car_wp.transform.get_right_vector()

            # Geometric marked crosswalk validation
            progress("Validating geometric crosswalk alignment...")
            if self.crossing_type=='marked':
                self.crosswalk_info = validate_and_find_crosswalk(self.map, self.bus_wp)
                if self.study_options.get('site') and math.dist(self.crosswalk_info['center'],self.study_options['site']['crosswalk']['center'])>.25:
                    raise RuntimeError('Native crosswalk differs from frozen site inventory')
            self.crosswalk_valid = self.crossing_type=='marked'
            if town in ('Town02','Town03'):
                from crossguard.simulation.sites import straight_path
                o,f=self.car_wp.transform.location,self.car_wp.transform.get_forward_vector()
                end=((self.crosswalk_info['center'][0]-o.x)*f.x+
                     (self.crosswalk_info['center'][1]-o.y)*f.y+self.corridor_end_reserve_m)
                samples=[]
                for distance in [*range(0,int(end),5),end]:
                    options=[self.car_wp] if distance==0 else self.car_wp.next(float(distance))
                    if len(options)!=1:
                        raise RuntimeError('Town03 extended controller corridor is ambiguous')
                    wp=options[0];p,v=wp.transform.location,wp.transform.get_forward_vector()
                    samples.append(((p.x,p.y),(v.x,v.y),wp.lane_width))
                if not straight_path((o.x,o.y),(f.x,f.y),samples):
                    raise RuntimeError('Town03 extended controller corridor fails geometry checks')

            progress("Populating city streets with ambient traffic...")
            self.populate(self.vehicle_count, self.walker_count, progress)

            self.session = {
                "town": self.map.name,
                "seed": self.seed,
                "background_cars": len(self.cars),
                "background_walkers": len(self.walkers),
                "client_version": self.client.get_client_version(),
                "server_version": self.client.get_server_version(),
                "resolution": list(self.resolution),
                "perception_mode": self.perception_mode,
                "group_mode": self.group_mode,
                "communication_profile": self.communication_profile,
                "crosswalk_info": self.crosswalk_info,
                "crossing_type":self.crossing_type,
                "pedestrian_crossing_duration_s":self.pedestrian_crossing_duration_s,
                "scene_preflight":self.scene_preflight,
                "scene_candidate_id":self.scene_candidate_id,
                "corridor_end_reserve_m":self.corridor_end_reserve_m,
                "observation_window_s":self.observation_window_s,
                "contact_measurement":"exact_preceding_snapshot_v1",
                "comparison": (
                    "Interactive toggle shares ambient traffic; matched evaluation reloads world "
                    "with fixed seed to guarantee paired trial consistency."
                ),
            }
            (self.output / "session.json").write_text(json.dumps(self.session, indent=2))

            # Save snapshot copies of source scripts to output
            for name in ("city_world.py", "baseline_policy.py"):
                src_file = Path(__file__).resolve().parent / name
                if src_file.exists():
                    shutil.copyfile(src_file, self.output / name)
            link_src = SRC_ROOT / "crossguard" / "communication" / "link.py"
            if link_src.exists():
                shutil.copyfile(link_src, self.output / "link.py")
            shutil.copyfile(SRC_ROOT / "crossguard/policy/following.py", self.output / "following.py")
            for name in ("channels.py", "braking.py"):
                shutil.copyfile(SRC_ROOT / "crossguard/communication" / name, self.output / name)

            progress("Preparing bus, crossing pedestrian, roadside unit, and cameras...")
            self.reset(self.enabled)

        except BaseException:
            self.close()
            raise

    def populate(self, count_cars: int, count_walkers: int, progress: Callable[[str], None]):
        """Spawn ambient traffic and pedestrians away from the hero corridor."""
        points = self.map.get_spawn_points()
        self.random.shuffle(points)
        bps = [
            bp
            for bp in self.lib.filter("vehicle.*")
            if bp.has_attribute("number_of_wheels")
            and int(bp.get_attribute("number_of_wheels")) == 4
            and "fusorosa" not in bp.id
        ]
        self.cars = []
        for tr in points:
            if len(self.cars) >= count_cars:
                break
            # Keep the initial hero corridor clear
            if tr.location.distance(self.bus_wp.transform.location) < 55.0:
                continue
            bp = self.random.choice(bps)
            if bp.has_attribute("color"):
                bp.set_attribute(
                    "color", self.random.choice(bp.get_attribute("color").recommended_values)
                )
            actor = self.world.try_spawn_actor(bp, tr)
            if actor:
                self.background.append(actor)
                self.cars.append(actor)
                actor.set_autopilot(True, 8000)
                self.tm.auto_lane_change(actor, False)
                if len(self.cars) % 10 == 0:
                    progress(f"Cars spawned: {len(self.cars)} / {count_cars}")

        self.world.set_pedestrians_seed(self.seed)
        self.world.set_pedestrians_cross_factor(0.03)
        self.walkers = []
        scene_center = carla.Location(*self.crosswalk_info["center"])
        bps_w = sorted(self.lib.filter("walker.pedestrian.*"), key=lambda b: b.id)
        for _ in range(count_walkers * 12):
            if len(self.walkers) >= count_walkers:
                break
            location = self.world.get_random_location_from_navigation()
            if location is None or location.distance(self.bus_wp.transform.location) > 110.0 or location.distance(scene_center) < 35.0:
                continue
            bp = self.random.choice(bps_w)
            if bp.has_attribute("is_invincible"):
                bp.set_attribute("is_invincible", "true")
            actor = self.world.try_spawn_actor(bp, carla.Transform(location))
            if actor:
                self.background.append(actor)
                self.walkers.append(actor)
                if len(self.walkers) % 15 == 0:
                    progress(f"Pedestrians spawned: {len(self.walkers)} / {count_walkers}")

        self.world.tick()
        for actor in self.walkers:
            controller = self.world.try_spawn_actor(
                self.lib.find("controller.ai.walker"), carla.Transform(), attach_to=actor
            )
            if controller:
                self.background.append(controller)
                self.controllers.append(controller)

        self.world.tick()
        for controller in self.controllers:
            controller.start()
            destination = None
            origin = controller.get_location() - scene_center
            for _ in range(30):
                candidate = self.world.get_random_location_from_navigation()
                if candidate is None or candidate.distance(scene_center) < 35:
                    continue
                direction = candidate - scene_center
                cosine = origin.dot(direction) / max(1e-6, origin.length()*direction.length())
                if cosine > 0.7:
                    destination = candidate
                    break
            if destination:
                controller.go_to_location(destination)
            controller.set_max_speed(self.random.uniform(0.9, 1.6))

        for _ in range(10):
            self.world.tick()

        progress(f"City ready: {len(self.cars)} cars, {len(self.walkers)} pedestrians")

    def destroy(self, actors: List[carla.Actor]):
        """Safely destroy an owned list of actors without affecting unrelated actors."""
        live_ids = {actor.id for actor in self.world.get_actors()} if self.world else set()
        ids = []
        for actor in reversed(actors):
            try:
                if actor.id in live_ids:
                    if actor.type_id.startswith("sensor."):
                        actor.stop()
                    ids.append(actor.id)
            except RuntimeError:
                pass
        if ids:
            self.client.apply_batch_sync([carla.command.DestroyActor(a_id) for a_id in ids], False)
        actors.clear()

    def _apply_trial_weather(self):
        """Prove actual weather before trial actors or recorded physics begin."""
        weather = self.study_options.get('weather', {
            'cloudiness': 20., 'sun_altitude_angle': 45., 'sun_azimuth_angle': 220.,
        })
        try:
            self.weather_guard_report = ensure_weather(self.world, carla.WeatherParameters, weather)
        except WeatherReadinessError as exc:
            self.weather_guard_report = exc.report
            self._save_weather_readiness()
            raise
        self._save_weather_readiness()

    def _save_weather_readiness(self):
        output = self.study_options.get('output')
        if output:
            folder = Path(output)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / 'weather_readiness.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.weather_guard_report, indent=2), encoding='utf-8')
            temporary.replace(target)

    def reset(self, enabled: bool = True, reload_world: bool = False):
        """Reset the evaluation trial.

        Modes:
        - Interactive Toggle (reload_world=False): Fast in-place reset. Preserves ambient
          traffic while cleanly destroying and respawning scenario actors and network state.
        - Matched Evaluation Reset (reload_world=True): Full world reload with fixed seed
          ensuring strictly identical ambient traffic for paired comparisons.
        """
        if hasattr(self, "rows") and self.rows:
            self.save("restarted" if not self.finished else "finished")

        if reload_world:
            self.close()
            self.__init__(
                town=self.town_name,
                vehicles=self.vehicle_count,
                walkers=self.walker_count,
                seed=self.seed,
                resolution=self.resolution,
                perception_mode=self.perception_mode,
                enabled=enabled,
                receiver_mode=self.receiver_mode,
                **self.kwargs,
            )
            return

        self.destroy(self.unit)
        self.destroy(self.scenario)

        if self.pipeline:
            self.pipeline.close()
            self.pipeline = None
        # No old trial actors remain during readiness warm-up. Initialization
        # already verified weather before its first reset.
        if self.trial_number > 0:
            self._apply_trial_weather()
        self.enabled = enabled
        self.trial_number += 1
        self.rows = []
        self.study_rows = []
        from crossguard.simulation.encounter_timing import EncounterTrigger
        timing=self.study_options.get("encounter_timing")
        self.encounter_trigger=EncounterTrigger(timing["tolerance_s"],timing["minimum_speed_mps"]) if timing else None
        self.diagnostic_brake_at = None
        self.messages = []
        self._network_log_cursor = 0
        self._sent_observations = set()
        self._received_observations = {}
        self._last_inference_submit_s = -1.0
        self._healthy_coverage = False
        self.pipeline_status = {}
        self.frame_id = None
        self.end_reason = None
        self.collisions = []
        self.followers = {}
        self.follower_origins = {}
        self.follower_guards = set()
        self.group_collisions = []
        from crossguard.simulation.contacts import ContactHistory
        self.contact_history=ContactHistory()
        self.group_rows = []
        self.group_metrics = {}
        self.braking_events = []
        self._last_braking_send = {}
        self._braking_received = {name: {} for name in ("follower_b", "follower_c")}
        self._phone_ready = {name: {} for name in ("hero_car", "follower_b", "follower_c")}
        self.phone_model = PhoneAdvisoryModel(self.phone_available, self.driver_response_delay_s,refresh_pending_by_target=self.study_options.get('phone_refresh_pending_by_target',False))
        self._phone_cursor = 0
        self._optical_cache = {}
        self.follower_observations = {name: {} for name in ("follower_b", "follower_c")}
        self.following_policy = FollowingPolicy(cruise_mps=self.target_speed_mps, target_gap_m=self.following_gap_m)
        if self.follower_model == "delayed":
            self.following_policy = DelayedFollowingPolicy(self.following_policy, self.follower_delay_s)
        self.study_following = {}
        for name in ('follower_b','follower_c'):
            assumptions=self.study_options.get('followers',{}).get(name)
            if assumptions:
                self.study_following[name] = SampledFollowingPolicy(
                    FollowingPolicy(cruise_mps=self.target_speed_mps,
                        target_gap_m=assumptions['gap_m'],deceleration_mps2=assumptions['deceleration_mps2']),
                    update_s=assumptions['update_s'],sensing_s=assumptions['sensing_s'],
                    reaction_s=assumptions['reaction_s'],brake_limit=assumptions['brake_limit'])
        self.finished = False

        self.crossing_at: Optional[float] = None
        self.brake_at: Optional[float] = None
        self.received_at: Optional[float] = None
        self.minimum_distance = math.inf

        # Communication network and risk policy
        radio_config = ChannelConfig(self.latency_s, self.jitter_s, self.loss, self.range_m)
        channel_configs={"radio":radio_config}
        channel_configs.update({k:ChannelConfig(**v) for k,v in self.study_options.get('channels',{}).items()})
        self._fault_states={}
        self.network = ChannelNetwork(profile=self.communication_profile, seed=self.seed,
            configs=channel_configs, geometry=self._optical_geometry,
            max_hops=1, optical_enabled=self.optical_enabled,outages=self.study_options.get('channel_outages'))
        self.policy = RiskPolicy(max_target_speed_mps=self.target_speed_mps)
        self.receiver = ReceiverController(self.receiver_mode, self.driver_response_delay_s,self.target_speed_mps)

        # 1. Spawn Stopped Bus in curb lane (lane -2)
        original_bus_pose=self.bus_spawn_transform or self.bus_wp.transform
        bus_pose = carla.Transform(original_bus_pose.location,original_bus_pose.rotation)
        bus_pose.location.z += 0.5
        self.bus = self.world.spawn_actor(
            self.lib.find(self.study_options.get('occluder','vehicle.mitsubishi.fusorosa')), bus_pose
        )
        self.scenario.append(self.bus)
        if self.study_options.get('additional_occluder'):
            extra_pose=carla.Transform(bus_pose.location-self.bus_wp.transform.get_forward_vector()*12.,bus_pose.rotation)
            extra=self.world.spawn_actor(self.lib.find('vehicle.carlamotors.carlacola'),extra_pose)
            extra.apply_control(carla.VehicleControl(hand_brake=True))
            self.scenario.append(extra)
            self.additional_occluder=extra

        # 2. Spawn Hero Vehicle upstream in adjacent lane (lane -1)
        car_pose = carla.Transform(self.car_wp.transform.location, self.car_wp.transform.rotation)
        car_pose.location.z += 0.5
        bp_car = self.lib.find("vehicle.tesla.model3")
        bp_car.set_attribute("color", "215,55,30")
        self.car = self.world.spawn_actor(bp_car, car_pose)
        self.scenario.append(self.car)

        # Load A's mesh at its single spawn pose before placing B/C. No actor
        # relocation is needed to obtain physical bumper gaps.
        self.car.apply_control(carla.VehicleControl(brake=1.0))
        self.bus.apply_control(carla.VehicleControl(brake=1.0,hand_brake=True))
        for _ in range(60):
            self.world.tick()
        if self.car.bounding_box.extent.x < .5:
            raise RuntimeError('A mesh bounds unavailable before physical follower placement')

        # Explicit controlled followers; never assign these to Traffic Manager.
        if self.group_mode != "single":
            distance=0.
            for name, distance, color in (("follower_b", 20.0, "70,100,145"),
                                          ("follower_c", 27.0 if self.town_name=='Town02' else 40.0, "135,135,135")):
                # All three use the same vehicle blueprint and loaded length.
                gap=self.study_options.get('followers',{}).get(name,{}).get('gap_m',self.following_gap_m)
                if name=='follower_b':
                    distance=2*self.car.bounding_box.extent.x+gap
                    cumulative_distance=distance
                else:
                    distance=cumulative_distance+2*self.car.bounding_box.extent.x+gap
                candidates = self.car_wp.previous(distance)
                if not candidates:
                    raise RuntimeError(f"No upstream lane pose for {name}")
                # Native route arc distance is not a measured longitudinal bumper gap.
                # Solve before actor creation only when the old pose would fail the
                # unchanged 0.25 m physical-gap gate. Preserve historical valid poses.
                predecessor_location=self.car.get_location() if name=='follower_b' else self.follower_origins['follower_b']
                target=2*self.car.bounding_box.extent.x+gap
                # Newly spawned B has no published snapshot yet. Its declared
                # native spawn pose is the placement anchor until the first tick.
                project=lambda wp:(predecessor_location-wp.transform.location).dot(self.forward)
                if abs(project(candidates[0])-target)>.25:
                    from crossguard.simulation.placement import solve_projected_distance
                    native,solved_arc=solve_projected_distance(self.car_wp.previous,project,target,distance)
                    lateral=abs((native.transform.location-self.car_wp.transform.location).dot(self.right))
                    if lateral>.6 or abs((native.transform.rotation.yaw-self.car_wp.transform.rotation.yaw+180)%360-180)>12:
                        raise RuntimeError('Solved follower spawn outside validated native lane corridor')
                    candidates=[native]
                pose = candidates[0].transform
                pose.location.z += 0.5
                bp_car.set_attribute("color", color)
                actor = self.world.spawn_actor(bp_car, pose)
                self.scenario.append(actor)
                self.followers[name] = actor
                self.follower_origins[name] = carla.Location(pose.location.x, pose.location.y, pose.location.z)
                sensor = self.world.spawn_actor(self.lib.find("sensor.other.collision"),
                                               carla.Transform(), attach_to=actor)
                self.scenario.append(sensor)
                sensor.listen(lambda e, vehicle_id=name,actor_id=actor.id:
                    self.group_collisions.append(self._contact_event(e,vehicle_id,actor_id)))
                actor.apply_control(carla.VehicleControl(brake=1.0))

        self.world.tick()  # Populate bounding boxes and snapshots

        # 3. Position crossing on validated marked crosswalk
        cw_center = carla.Location(
            x=self.crosswalk_info["center"][0],
            y=self.crosswalk_info["center"][1],
            z=self.crosswalk_info["center"][2],
        )
        self.cross = cw_center

        # Pedestrian starts on sidewalk curb at crosswalk entrance
        # Start at the actual marked polygon edge, not an arbitrary lane offset.
        curb_offset = self.crosswalk_info['pedestrian_start_offset_m'] if self.crossing_type=='unmarked' else max((x - self.cross.x) * self.right.x +
                          (y - self.cross.y) * self.right.y
                          for x, y, _ in self.crosswalk_info["polygon"]) - 0.5
        ped_start = carla.Location(
            x=self.cross.x + self.right.x * curb_offset,
            y=self.cross.y + self.right.y * curb_offset,
            z=bus_pose.location.z + 0.5,
        )
        bp_p = self.lib.find("walker.pedestrian.0001")
        bp_p.set_attribute("is_invincible", "false")
        ped_yaw = self.bus_wp.transform.rotation.yaw - 90.0
        self.person = self.world.spawn_actor(
            bp_p, carla.Transform(ped_start, carla.Rotation(yaw=ped_yaw))
        )
        self.scenario.append(self.person)

        # Collision sensor on hero car
        collision_sensor = self.world.spawn_actor(
            self.lib.find("sensor.other.collision"), carla.Transform(), attach_to=self.car
        )
        self.scenario.append(collision_sensor)
        collision_sensor.listen(
            lambda e: self.collisions.append(self._contact_event(e,'hero_car',self.car.id))
        )

        # Hold bus stationary
        self.client.apply_batch_sync(
            [
                carla.command.ApplyVehicleControl(
                    self.bus.id, carla.VehicleControl(brake=1.0, hand_brake=True)
                ),
                carla.command.ApplyVehicleControl(
                    self.car.id, carla.VehicleControl(brake=1.0)
                ),
            ],
            False,
        )
        for _ in range(30):
            self.world.tick()

        # A freshly reloaded CARLA actor can briefly report an empty mesh bound.
        # Wait before fixing vehicle geometry; zero-length bodies invalidate gaps.
        controlled = [self.car, *self.followers.values()]
        for _ in range(300):
            if all(actor.bounding_box.extent.x > 0.5 and actor.bounding_box.extent.y > 0.3
                   for actor in controlled):
                break
            self.world.tick()
        else:
            raise RuntimeError("Controlled vehicle mesh bounds unavailable after settling: " +
                repr([(a.id,a.type_id,str(a.bounding_box.extent)) for a in controlled]))
        self.vehicle_dimensions = {actor.id:(actor.bounding_box.extent.x,actor.bounding_box.extent.y)
                                   for actor in controlled}

        # Place followers using loaded body lengths, rather than actor-centre gaps.
        # Initial wide spawning above avoids overlaps while CARLA loads the meshes.
        # Validate the measured clearance at the original settled spawn poses.
        predecessor=self.car
        for name,actor in self.followers.items():
            gap=(predecessor.get_location()-actor.get_location()).dot(self.forward)-(
                predecessor.bounding_box.extent.x+actor.bounding_box.extent.x)
            requested=self.study_options.get('followers',{}).get(name,{}).get('gap_m',self.following_gap_m)
            if abs(gap-requested)>.25:
                raise RuntimeError(f'Invalid initial physical gap {name}: {gap} vs {requested}')
            predecessor=actor

        # Repositioning lifts followers above the road. Settle every controlled
        # actor with the same stopped control before imparting launch velocity;
        # otherwise grounded A and airborne B/C start with different dynamics.
        self.client.apply_batch_sync([
            carla.command.ApplyVehicleControl(actor.id, carla.VehicleControl(brake=1.0))
            for actor in controlled], False)
        for _ in range(30):
            self.world.tick()
            predecessor = actor
        if self.followers:
            self.world.tick()

        # 4. Mount 4 Display Cameras (1920x1080)
        # Top view: close overhead view of crosswalk and approach corridor
        top_loc = carla.Location(
            x=self.cross.x - self.forward.x * 22.0,
            y=self.cross.y - self.forward.y * 22.0,
            z=self.cross.z + 27.0,
        )
        top_pose = carla.Transform(
            top_loc, carla.Rotation(pitch=-52.0, yaw=self.bus_wp.transform.rotation.yaw)
        )
        # Rigid hero-relative camera: follows A while retaining upstream context.
        top_pose = carla.Transform(carla.Location(x=-25.0 if self.followers else -12.0,
            z=90.0 if self.followers else 40.0), carla.Rotation(pitch=-75.0, yaw=0.0))

        # Pedestrian approach view: near eye-level from sidewalk entrance
        ped_cam_loc = carla.Location(
            x=ped_start.x + self.forward.x * 2.0 + self.right.x * 2.0,
            y=ped_start.y + self.forward.y * 2.0 + self.right.y * 2.0,
            z=self.cross.z + 1.6,
        )
        ped_cam_pose = look_at(ped_cam_loc, self.cross - self.forward * 5.0)

        # True windshield driver view
        driver_pose = carla.Transform(
            carla.Location(x=0.4, y=-0.3, z=1.25), carla.Rotation(pitch=-2.0, yaw=0.0)
        )

        # Opposite curb view: close view looking across from opposite sidewalk
        opp_loc = carla.Location(
            x=self.cross.x - self.right.x * 8.5,
            y=self.cross.y - self.right.y * 8.5,
            z=self.cross.z + 1.6,
        )
        opp_pose = look_at(opp_loc, self.cross + self.right * 2.0)

        # Render only the selected display camera; observation cameras remain active.
        self.view_poses = {"top": top_pose, "driver": driver_pose,
                           "person": ped_cam_pose, "opposite": opp_pose}
        self.view_parents = {"top": self.car, "driver": self.car,
                             "person": None, "opposite": None}
        for name, actor in self.followers.items():
            view_name = "driver_b" if name == "follower_b" else "driver_c"
            self.view_poses[view_name] = driver_pose
            self.view_parents[view_name] = actor
        self.active_view = "top"
        self.views = {"top": Stream(self.world, self.scenario, top_pose,
                                    parent=self.car, size=self.resolution, fov=90.0)}
        if self.record_six_views:
            self.views['top'].sensor.stop()
            self.views['top'].sensor.destroy()
            self.scenario.remove(self.views['top'].sensor)
            self.views={name:Stream(self.world,self.scenario,pose,parent=self.view_parents[name],
                size=(960,540),fov=90.) for name,pose in self.view_poses.items()}

        # 5. Mount Instance Segmentation Cameras for Ideal Visibility Mode
        # Driver windshield instance camera
        self.driver_seg = Stream(
            self.world,
            self.scenario,
            driver_pose,
            kind=("rgb" if self.perception_mode == "rgb" else "instance_segmentation"),
            parent=self.car,
            size=((640, 360) if self.perception_mode == "rgb" else (480, 270)),
            fov=90.0,
        )

        # Bus V2V instance camera: mounted on front passenger corner of stopped bus
        bus_cam_pose = carla.Transform(
            carla.Location(
                x=self.bus.bounding_box.extent.x + 0.2,
                y=self.bus.bounding_box.extent.y + 0.2,
                z=2.0,
            ),
            carla.Rotation(pitch=-5.0, yaw=-30.0),
        )
        self.bus_seg = Stream(
            self.world,
            self.scenario,
            bus_cam_pose,
            kind=("rgb" if self.perception_mode == "rgb" else "instance_segmentation"),
            parent=self.bus,
            size=((640, 360) if self.perception_mode == "rgb" else (480, 270)),
            fov=95.0,
        )

        self.rsu_seg = None
        self.rsu_pose = None
        if self.enabled or self.study_options:
            self.make_rsu_unit()

        if self.perception_mode == "rgb":
            from crossguard.perception.pipeline import RGBPipeline
            self.pipeline = RGBPipeline(self.detector)
        # Sensors were spawned after the settle ticks: produce their first frame.
        self.world.tick()
        self.start_time = self.world.get_snapshot().timestamp.elapsed_seconds
        self.time = 0.0
        self.client.apply_batch_sync([
            carla.command.ApplyVehicleControl(actor.id,
                carla.VehicleControl(throttle=0.35, brake=0.0, gear=1))
            for actor in (self.car, *self.followers.values())], False)
        # Accelerate from rest through wheel controls. Injecting chassis velocity
        # creates wheel-speed mismatch and spurious initial deceleration.

        self.visible_driver: Dict[int, int] = {}
        self.visible_rsu: Dict[int, int] = {}
        self.visible_bus: Dict[int, int] = {}

        self.current_decision = Decision(
            state="normal",
            target_speed_mps=self.target_speed_mps,
            brake=0.0,
            reason="Scenario initialized",
            stopping_margin_m=None,
            conflict_in_s=None,
            observation_age_s=None,
            sources=(),
        )

        if self.study_options.get('site'):
            eye=self.car.get_transform().transform(carla.Location(x=.4,y=-.3,z=1.25))
            target=self.person.get_location()+carla.Location(z=.8)
            hits=self.world.cast_ray(eye,target)
            audit={'native_crosswalk':self.crosswalk_info,'site':self.study_options['site'],
                'pedestrian_start':[self.person.get_location().x,self.person.get_location().y,self.person.get_location().z],
                'driver_to_pedestrian_ray':[{'label':str(h.label),'location':[h.location.x,h.location.y,h.location.z],'distance_from_camera_m':h.location.distance(eye)} for h in hits],
                'camera_to_pedestrian_distance_m':target.distance(eye),
                'occluder_blueprint':self.bus.type_id,'occluder_extent_m':[self.bus.bounding_box.extent.x,self.bus.bounding_box.extent.y,self.bus.bounding_box.extent.z],
                'actors':{n:{'id':a.id,'blueprint':a.type_id,'location':[a.get_location().x,a.get_location().y,a.get_location().z],'actor_alive':a.is_alive,'physics_mass_kg':a.get_physics_control().mass} for n,a in {'A':self.car,'B':self.followers['follower_b'],'C':self.followers['follower_c'],'occluder':self.bus}.items()},
                'evaluator_only':True}
            out=Path(self.study_options['output']);out.mkdir(parents=True,exist_ok=True)
            if self.study_options.get('additional_occluder'):
                extra=self.additional_occluder
                audit['additional_occluder']={'id':extra.id,'blueprint':extra.type_id,
                    'location':[extra.get_location().x,extra.get_location().y,extra.get_location().z],
                    'extent_m':[extra.bounding_box.extent.x,extra.bounding_box.extent.y,extra.bounding_box.extent.z],
                    'distance_to_main_occluder_m':extra.get_location().distance(self.bus.get_location())}
            (out/'placement_audit.json').write_text(json.dumps(audit,indent=2))
        self.save_metadata()
        self.source_camera_recorder=None
        self.six_view_recorder=None
        if self.record_six_views:
            from crossguard.simulation.visual_evidence import SixViewRecorder
            self.six_view_recorder=SixViewRecorder(self.study_options.get('output',self.trial_path),
                self.start_time, self.study_options.get('labels'), self.study_options.get('assets_output'))
        if self.scene_preflight and self.perception_mode=='rgb':
            from crossguard.simulation.visual_evidence import SourceCameraRecorder
            self.source_camera_recorder=SourceCameraRecorder(self.trial_path,self.start_time)

    def set_view(self, name):
        if name not in self.view_poses:
            raise ValueError("Unknown camera view")
        if name == self.active_view:
            return
        if self.record_six_views:
            self.active_view=name
            return
        for stream in self.views.values():
            stream.sensor.stop()
            stream.sensor.destroy()
            self.scenario.remove(stream.sensor)
        parent = self.view_parents[name]
        self.views = {name: Stream(self.world, self.scenario, self.view_poses[name],
                                  parent=parent, size=self.resolution, fov=90.0)}
        self.active_view = name
        # The next controlled tick supplies the new image. Camera selection must
        # not move vehicles or advance messages while the dashboard is paused.

    def configure_communication(self, profile=None, optical_enabled=None, phone_available=None):
        if profile is not None:
            if profile not in ("radio", "cellular", "optical", "hybrid"):
                raise ValueError("Invalid communication profile")
            self.communication_profile = profile
        if optical_enabled is not None:
            self.optical_enabled = bool(optical_enabled)
        if phone_available is not None:
            self.phone_available = bool(phone_available)
        view = self.active_view
        self.reset(self.enabled)
        self.set_view(view)

    def _optical_geometry(self, message, sender_xy, receiver_xy):
        actors = {"hero_car": self.car, "bus": self.bus, **self.followers}
        receiver = actors.get(message.receiver_id)
        sender = actors.get(message.sender_id)
        if receiver is None:
            return None
        key = (message.sender_id, message.receiver_id)
        cached = self._optical_cache.get(key)
        if cached and self.time-cached[0] < 0.1:
            return cached[1]
        if message.sender_id == "rsu" and self.rsu_pose:
            start = self.rsu_pose.location
            emit = (-self.forward.x, -self.forward.y, -0.12)
        elif sender is not None:
            transform = sender.get_transform()
            start = transform.transform(carla.Location(x=-sender.bounding_box.extent.x-0.15, z=1.1))
            axis = transform.get_forward_vector()
            emit = (-axis.x, -axis.y, 0.0)
        else:
            return None
        transform = receiver.get_transform()
        end = transform.transform(carla.Location(x=receiver.bounding_box.extent.x+0.15, z=1.4))
        axis = transform.get_forward_vector()
        # Exclude emitter/receiver housings by placing the endpoints outside bodies.
        hits = self.world.cast_ray(start, end)
        distance = start.distance(end)
        blocked = any(0.25 < start.distance(hit.location) < distance-0.25 for hit in hits)
        geometry = OpticalGeometry(emit, (axis.x, axis.y, 0.12), blocked,
            (start.x,start.y,start.z), (end.x,end.y,end.z), 150.0, 150.0)
        self._optical_cache[key] = (self.time, geometry)
        return geometry

    def _broadcast_braking(self, name, actor, brake):
        if not self.enabled or self.group_mode != "cooperative":
            return
        deceleration = max(0.0, -actor.get_acceleration().dot(self.forward))
        if actor.get_velocity().length() < 0.5 or (brake < 0.2 and deceleration < 2.0):
            return
        if self.time-self._last_braking_send.get(name, -10) < 0.2:
            return
        self._last_braking_send[name] = self.time
        position, velocity = actor.get_location(), actor.get_velocity()
        event = BrakingEvent(f"brake:{name}:{self.frame_id}:{self.trial_number}", name,
            self.time, self.time+1.2, max(deceleration, brake*6.0, 0.1),
            (position.x,position.y), (velocity.x,velocity.y))
        targets = ("follower_b", "follower_c") if name == "hero_car" else (("follower_c",) if name == "follower_b" else ())
        for target in targets:
            location = self.followers[target].get_location()
            self.network.send(event.to_message(target,self.time), self.time,
                event.position, (location.x,location.y))
            self.braking_events.append({"event":"braking_created", "sender_id":name,
                "receiver_id":target, "channel":self.communication_profile, "at_s":self.time,
                "event_id":event.event_id, "deceleration_mps2":event.deceleration_mps2})

    def _offer_phone(self, message):
        event = self.phone_model.offer(message, self.time)
        if event["event"] == "delivered" and self.receiver_mode == "retrofit":
            self.messages.append({**event,"event":"notification_delivered",
                "source_id":message.observation.source_id,"sender_id":message.sender_id,
                "channel":"simulated_phone"})

    def _poll_phone(self, name):
        for message in self.phone_model.poll(self.time, name):
            key = (message.observation.source_id, message.observation.object_id)
            self._phone_ready[name][key] = message.expires_at_s
        self._phone_ready[name] = {key: expiry for key,expiry in self._phone_ready[name].items()
                                  if self.phone_available and self.time <= expiry}

    def _communication_metrics(self):
        summary = self.network.summary(self.time)
        summary["braking_events"] = self.braking_events[-20:]
        summary["braking_event_count"] = len(self.braking_events)
        # Latest meaningful state per recipient, not a fabricated phone notification.
        advisories = {}
        deliveries = {}
        for event in self.phone_model.events:
            if event["event"] == "duplicate":
                continue
            identity = (event["receiver_id"],event["event_id"])
            if event["event"] == "delivered":
                deliveries[identity] = event
            previous = deliveries.get(identity, {})
            advisories[event["receiver_id"]] = {**previous,**event,"state":event["event"],
                "message":"Upstream hazard: prepare to slow",
                "delivered_at_s":previous.get("at_s"),
                "response_due_at_s":previous.get("react_at_s"),
                "reason":event.get("reason", "modeled driver reaction")}
        summary["phone_advisories"] = list(advisories.values())
        summary["phone"] = self.phone_model.summary()
        return summary

    def make_rsu_unit(self):
        """Construct physical Roadside Unit pole, housing, and visibility sensor."""
        # Mount above an existing traffic-light head/arm; never introduce a
        # free-standing pole in a vehicle lane. CARLA light boxes are world poses.
        candidates = []
        for signal in self.world.get_actors().filter("traffic.traffic_light*"):
            for box in signal.get_light_boxes():
                if box.location.z > self.cross.z + 3.0 and box.location.distance(self.cross) < 35.0:
                    along = (box.location - self.cross).dot(self.forward)
                    if along >= 3.0:  # Far side of the crossing, ahead of approaching car.
                        candidates.append((box.location.distance(self.cross), signal, box))
        self.rsu_mount = None
        if candidates:
            _, signal, box = min(candidates, key=lambda item: item[0])
            eye = carla.Location(x=box.location.x, y=box.location.y,
                                 z=box.location.z + box.extent.z + 0.35)
            self.rsu_mount = {"type": "traffic_light", "actor_id": signal.id,
                              "anchor": [box.location.x, box.location.y, box.location.z]}
        else:
            sidewalk = self.map.get_waypoint(self.cross, project_to_road=True,
                                            lane_type=carla.LaneType.Sidewalk)
            if sidewalk is None or sidewalk.transform.location.distance(self.cross) > 35.0:
                raise RuntimeError("No suitable traffic light or sidewalk mount near crossing")
            pole_loc = sidewalk.transform.location
            bp_pole = self.lib.find("static.prop.mesh")
            bp_pole.set_attribute("mass", "0")
            bp_pole.set_attribute("scale", "1")
            bp_pole.set_attribute("mesh_path", "/Game/Carla/Static/Pole/TrafficLight/SM_TrafficLight__VPole_Main.SM_TrafficLight__VPole_Main")
            self.unit.append(self.world.spawn_actor(bp_pole, carla.Transform(pole_loc)))
            eye = carla.Location(x=pole_loc.x, y=pole_loc.y, z=pole_loc.z + 5.2)
            self.rsu_mount = {"type": "sidewalk", "anchor": [pole_loc.x, pole_loc.y, pole_loc.z]}
        self.rsu_pose = look_at(eye, self.cross - self.forward * 3.0)

        # Housing cube
        bp_box = self.lib.find("static.prop.mesh")
        bp_box.set_attribute("mesh_path", "/Engine/BasicShapes/Cube.Cube")
        bp_box.set_attribute("scale", "0.4")
        bp_box.set_attribute("mass", "0")
        self.unit.append(self.world.spawn_actor(bp_box, self.rsu_pose))

        sensor_pose = carla.Transform(self.rsu_pose.location, self.rsu_pose.rotation)
        sensor_pose.location += sensor_pose.get_forward_vector() * 0.3
        self.rsu_seg = Stream(
            self.world,
            self.unit,
            sensor_pose,
            kind=("rgb" if self.perception_mode == "rgb" else "instance_segmentation"),
            size=((640, 360) if self.perception_mode == "rgb" else (480, 270)),
            fov=105.0,
        )

    def detection_preview(self, view):
        """Annotated exact inference image, never boxes pasted onto newer frames."""
        if self.pipeline is None:
            return None
        data = self.pipeline.snapshot()
        source = "view_" + view
        if source not in data.get("preview_images", {}):
            return None
        import cv2
        image = data["preview_images"][source].copy()
        for box in data["detections"].get(source, []):
            x1,y1,x2,y2 = map(int, box["bbox"])
            cv2.rectangle(image, (x1,y1), (x2,y2), (255,255,255), 2)
            cv2.putText(image, f"{box['label']} {box['confidence']:.2f}",
                        (x1,max(20,y1-6)), cv2.FONT_HERSHEY_SIMPLEX, .65, (255,255,255), 2)
        return image, data["source_frames"][source]

    def save_metadata(self):
        self.session['runtime_source_manifest']=save_runtime_sources(self.output)
        (self.output/'session.json').write_text(json.dumps(self.session,indent=2))
        for module,entry in self.session['runtime_source_manifest']['modules'].items():
            filename=Path(entry['file']).name
            if module.startswith('crossguard.'):
                shutil.copyfile(self.output/'runtime_source'/entry['file'],self.output/filename)
        """Save trial configuration and metadata."""
        mode_str = "with_system" if self.enabled else "without_system"
        self.trial_path = self.output / f"trial_{self.trial_number:03d}_{mode_str}"
        self.trial_path.mkdir(exist_ok=True)

        config = {
            **self.session,
            "trial_number": self.trial_number,
            "enabled": self.enabled,
            "receiver_mode": self.receiver_mode,
            "driver_response_delay_s": self.driver_response_delay_s,
            "receiver_cruise_mps":self.receiver.cruise_mps,
            "hero_vehicle_id": self.car.id,
            "bus_id": self.bus.id,
            "target_pedestrian_id": self.person.id,
            "target_speed_kmh": self.target_speed_mps * 3.6,
            "study_options": self.study_options,
            "scenario_definition": self.scenario_definition,
            "crossing_trigger_distance_m": self.crossing_trigger_distance_m,
            "pedestrian_behavior": self.pedestrian_behavior,
            "retreat_after_s": self.retreat_after_s,
            "baseline_behavior": self.baseline_behavior,
            "follower_model": self.follower_model,
            "follower_delay_s": self.follower_delay_s,
            "v2x_latency_s": self.latency_s,
            "v2x_jitter_s": self.jitter_s,
            "v2x_loss": self.loss,
            "v2x_range_m": self.range_m,
            "camera_views": list(self.views.keys()),
            "record_six_views":self.record_six_views,
            "evidence_sampling_interval_s":.1 if self.record_six_views else None,
            "perception_mode": self.perception_mode,
            "crosswalk_valid": self.crosswalk_valid,
            "group_mode": self.group_mode,
            "communication_profile": self.communication_profile,
            "following_gap_m": self.following_gap_m,
            "optical_enabled": self.optical_enabled,
            "phone_available": self.phone_available,
            "communication_assumptions": self.network.summary(self.time)["channels"],
            "phone_reaction_delay_s": self.driver_response_delay_s,
            "controlled_followers": {k: a.id for k, a in self.followers.items()},
            "follower_sensing_assumption": "ideal local predecessor ranging; no follower RGB sensors" if self.follower_model == "ideal"
                else "delayed local control from ideal sampled ranging; remote pedestrian risk uses receiver own state; no calibrated human model",
        }
        (self.trial_path / "config.json").write_text(json.dumps(config, indent=2))
        (self.trial_path / "crosswalk.json").write_text(
            json.dumps(self.crosswalk_info, indent=2)
        )

    def crossing_candidate(self, observation):
        # Only estimated RGB positions/velocities enter this geometric region gate.
        # A sidewalk person moving away is not a crossing conflict; people in
        # the target lane remain relevant even while stationary.
        if observation.label != "person":
            return False
        along = ((observation.position[0]-self.cross.x)*self.forward.x +
                 (observation.position[1]-self.cross.y)*self.forward.y)
        origin = self.car_wp.transform.location
        lateral = ((observation.position[0]-origin.x)*self.right.x +
                   (observation.position[1]-origin.y)*self.right.y)
        lateral_speed = observation.velocity[0]*self.right.x + observation.velocity[1]*self.right.y
        return (abs(along) <= 4.0 and abs(lateral) <= 12.0 and
                (abs(lateral) <= self.car_wp.lane_width/2 + 0.5 or
                 (abs(lateral_speed) >= 0.3 and lateral*lateral_speed < 0)))

    def observe_actor(
        self,
        actor: carla.Actor,
        source_id: str,
        now_s: float,
    ) -> Observation:
        """Construct a contract Observation from an instance-visible actor."""
        loc = actor.get_location()
        vel = actor.get_velocity()
        return Observation(
            object_id=str(actor.id),
            source_id=source_id,
            observed_at_s=round(now_s, 4),
            position=(round(loc.x, 3), round(loc.y, 3)),
            velocity=(round(vel.x, 3), round(vel.y, 3)),
            position_sigma_m=0.3,
            velocity_sigma_mps=0.2,
            confidence=1.0,
            label="person",
            perception_mode=self.perception_mode,
        )

    def _contact_event(self,event,vehicle_id,actor_id):
        return {'vehicle_id':vehicle_id,'actor_id':actor_id,'other_id':event.other_actor.id,
                'type':event.other_actor.type_id,'frame':event.frame,
                'impulse':float(event.normal_impulse.length()),
                'precontact':self.contact_history.measurement(event.frame,actor_id,event.other_actor.id)}

    def tick(self) -> Dict[str, Optional[np.ndarray]]:
        """Advance the simulation world by DT, process V2X, evaluate policy, and return frames."""
        if not self.finished:
            previous=self.world.get_snapshot()
            velocities={}
            for actor in (self.car,*self.followers.values(),self.person,self.bus):
                snapshot=previous.find(actor.id)
                if snapshot is not None:
                    v=snapshot.get_velocity()
                    velocities[actor.id]=(v.x,v.y,v.z)
            self.contact_history.remember(previous.frame,previous.timestamp.elapsed_seconds-self.start_time,velocities)
            self.world.tick()
        snap = self.world.get_snapshot()
        self.time = snap.timestamp.elapsed_seconds - self.start_time

        c = self.car.get_location()
        p = self.person.get_location()
        v = self.car.get_velocity()
        bus_loc = self.bus.get_location()

        # Distance along road forward axis to crosswalk center
        distance_to_crosswalk = (self.cross - c).dot(self.forward)

        # Trigger scripts by actual distance, not wall-clock processing time.
        if self.encounter_trigger is not None and self.crossing_at is None:
            from crossguard.simulation.encounter_timing import predict_encounter
            origin=self.car_wp.transform.location
            a=self.car.get_acceleration()
            prediction=predict_encounter((c.x,c.y),(p.x,p.y),(origin.x,origin.y),
                (self.car_wp.transform.get_forward_vector().x,self.car_wp.transform.get_forward_vector().y),(self.right.x,self.right.y),
                v.dot(self.car_wp.transform.get_forward_vector()),a.dot(self.car_wp.transform.get_forward_vector()),self.target_speed_mps,
                self.study_options['pedestrian_speed_mps'],
                pedestrian_acceleration_mps2=self.study_options.get('encounter_timing',{}).get('pedestrian_acceleration_mps2'))
            event=self.encounter_trigger.update(prediction,self.time,snap.frame,self.car.get_control().brake)
            if event:
                self.crossing_at=self.time
                self.messages.append({**event,'pedestrian_behavior':self.pedestrian_behavior})
        if self.encounter_trigger is None and self.crossing_at is None and distance_to_crosswalk <= self.crossing_trigger_distance_m:
            self.crossing_at = self.time
            self.messages.append({"event": "scenario_trigger", "at_s": self.time,
                "distance_to_crosswalk_m": distance_to_crosswalk,
                "speed_kmh": v.length() * 3.6, "pedestrian_behavior": self.pedestrian_behavior})

        # Every image/calibration comes from this same capture frame.
        frame = snap.frame
        self.frame_id = frame
        for stream in self.views.values():
            stream.read(frame)
        if self.six_view_recorder is not None:
            self.six_view_recorder.capture(self.time,frame,self.views)
        sources = {"driver": self.driver_seg, "bus": self.bus_seg}
        if self.rsu_seg:
            sources["rsu"] = self.rsu_seg
        for stream in sources.values():
            stream.read(frame)
        if self.source_camera_recorder is not None:
            self.source_camera_recorder.capture(self.time,sources,self.person.id,(p.x,p.y,p.z),self.crossing_at)
        active_observations: List[Observation] = []
        source_observations = {}
        if self.perception_mode == "rgb":
            if self.time - self._last_inference_submit_s >= 0.15:
                samples = {name: {"rgb": stream.rgb(), "size": stream.size,
                           "fov": stream.fov, "transform": stream.last_transform,
                           "timestamp_s": stream.last_timestamp - self.start_time,
                           "frame_id": stream.last_frame, "ground_z": self.cross.z}
                           for name, stream in sources.items()}
                if getattr(self, "preview_detection_enabled", False):
                    view_stream = self.views[self.active_view]
                    samples["view_" + self.active_view] = {"rgb": view_stream.rgb(),
                        "size": view_stream.size, "fov": view_stream.fov,
                        "transform": view_stream.last_transform,
                        "timestamp_s": view_stream.last_timestamp - self.start_time,
                        "frame_id": view_stream.last_frame, "ground_z": self.cross.z}
                self.pipeline.submit(samples)
                self._last_inference_submit_s = self.time
            self.pipeline_status = self.pipeline.snapshot()
            if self.pipeline_status["error"]:
                raise RuntimeError("RGB pipeline failed: " + self.pipeline_status["error"])
            source_observations = self.pipeline_status["observations"]
            driver_sees_ped = any(o.label == "person" for o in source_observations.get("driver", []))
        else:
            ped_key = self.person.id & 0xFFFF
            for name, stream in sources.items():
                if ped_key in instance_counts(stream.last):
                    source_observations[name] = [self.observe_actor(self.person, name, self.time)]
            driver_sees_ped = bool(source_observations.get("driver"))
        # Missing observations are an explicit sensor-output fault, never evaluator truth.
        faults=[]
        for index,outage in enumerate(self.study_options.get('channel_outages',[])):
            faults.append(('channel_'+str(index),outage['start_s']<=self.time<outage['end_s'],outage))
        for index,window in enumerate(self.study_options.get('observation_outages',[])):
            active=window['start_s']<=self.time<window['end_s']
            faults.append(('observation_'+str(index),active,window))
            if active:
                source_observations={key:value for key,value in source_observations.items() if key not in window['sources']}
        for key,active,spec in faults:
            if self._fault_states.get(key,False)!=active:
                self.messages.append({'event':'fault_started' if active else 'fault_ended','fault':key,'at_s':self.time,'frame':frame,'specification':spec})
            self._fault_states[key]=active
        driver_sees_ped=any(o.label=='person' for o in source_observations.get('driver',[]))
        active_observations.extend(source_observations.get("driver", []))
        # Evaluation truth is never used to create RGB observations.
        if not self.finished:
            self.minimum_distance = min(self.minimum_distance, c.distance(p))
        for source in ("rsu", "bus"):
            if not self.enabled:
                continue
            for observation in source_observations.get(source, []):
                key = (source, observation.object_id, observation.frame_id, observation.observed_at_s)
                if key in self._sent_observations or self.time - observation.observed_at_s > 1.0:
                    continue
                self._sent_observations.add(key)
                position = self.rsu_pose.location if source == "rsu" else bus_loc
                for recipient in group_recipient_ids(self.group_mode, self.enabled):
                    actor = self.car if recipient == "hero_car" else self.followers[recipient]
                    target = actor.get_location()
                    message = Message(
                        event_id=f"{source}:{observation.object_id}:{observation.frame_id}:{self.trial_number}",
                        sender_id=source, receiver_id=recipient, sent_at_s=self.time,
                        # Phone reaction may outlive a packet; control still filters
                        # pedestrian estimates at <=1s and never refreshes capture age.
                        expires_at_s=observation.observed_at_s + 2.5,
                        observation=observation, channel="v2i" if source == "rsu" else "v2v")
                    self.network.send(message, self.time, (position.x, position.y), (target.x, target.y))

        # 2. V2X Message Reception at Hero Vehicle
        delivered_messages = self.network.receive("hero_car", self.time)
        for msg in delivered_messages:
            key = (msg.observation.source_id, msg.observation.object_id)
            previous = self._received_observations.get(key)
            if previous is None or msg.observation.observed_at_s > previous.observed_at_s:
                self._received_observations[key] = msg.observation
            if self.received_at is None:
                self.received_at = self.time

        self._received_observations = {key: obs for key, obs in self._received_observations.items()
                                       if 0 <= self.time - obs.observed_at_s <= 1.0}
        active_observations.extend(self._received_observations.values())
        active_observations = [o for o in active_observations if self.crossing_candidate(o)]
        # Copy every newly recorded network event once, including expiry/loss.
        self.messages.extend(self.network.events[self._network_log_cursor:])
        self._network_log_cursor = len(self.network.events)

        # 3. Policy Evaluation
        hero_state = VehicleState(
            vehicle_id="hero_car",
            position=(c.x, c.y),
            velocity=(v.x, v.y),
            response_time_s=(self.driver_response_delay_s if self.receiver_mode == "retrofit" else 0.2),
            deceleration_mps2=6.0,
            length_m=4.6,
            width_m=1.9,
        )

        # Only the explicitly cautious baseline uses blind-crossing fallback.
        # Local-vision baseline retains braking for locally detected conflicts.
        # RGB coverage is a fixed-scene assumption, not certified empty-road proof.
        remote_frames = self.pipeline_status.get("source_frames", {})
        rsu_frame = remote_frames.get("rsu", {})
        self._healthy_coverage = (self.perception_mode == "rgb" and self.enabled
                                  and self.pipeline_status.get("completed_batches", 0) >= 4
                                  and 0 <= self.time - rsu_frame.get("captured_at_s", -10) <= 0.5)
        # Occlusion check: pedestrian is on curb behind the stopped bus
        is_occluded = (
            distance_to_crosswalk > 5.0
            and not driver_sees_ped
            and (self.bus.get_location().distance(self.cross) < 15.0)
            and not self._healthy_coverage
            and ((self.enabled and not self.study_options) or self.baseline_behavior == "cautious")
        )

        decision = self.policy.evaluate(
            vehicle=hero_state,
            observations=active_observations,
            now_s=self.time,
            distance_to_crossing_m=max(0.0, distance_to_crosswalk),
            occluded=is_occluded,
            enabled=self.enabled,
        )
        self.current_decision = decision
        local_decision = self.policy.evaluate(
            hero_state, active_observations, self.time,
            max(0.0, distance_to_crosswalk), is_occluded, enabled=False)
        delivered_risks = [
            (message, self.policy.evaluate(
                hero_state, [message.observation], self.time,
                max(0.0, distance_to_crosswalk), False, enabled=True))
            for message in delivered_messages if self.crossing_candidate(message.observation)
        ]
        for message, risk in delivered_risks:
            if risk.conflict_in_s is not None:
                self._offer_phone(message)
        self._poll_phone("hero_car")
        if self.receiver_mode == "retrofit":
            # Local visual response retains its own modeled human delay. Remote
            # awareness is controlled only by the authoritative phone reaction.
            # A human reacts to a locally seen crossing pedestrian even if the
            # constant-velocity conflict estimate clears during reaction time.
            # Only fresh driver observations enter this visual trigger.
            visible_hazards = [obs for obs in source_observations.get("driver", [])
                if obs.label == "person" and obs.confidence >= self.policy.min_confidence
                and 0 <= self.time-obs.observed_at_s <= self.policy.max_observation_age_s
                and self.crossing_candidate(obs)
                and ((obs.position[0]-c.x)*self.forward.x
                     + (obs.position[1]-c.y)*self.forward.y) >= -hero_state.length_m/2]
            local_response, receiver_events = self.receiver.local_visual_command(
                self.time, local_decision,
                max(visible_hazards, key=lambda obs: obs.observed_at_s) if visible_hazards else None)
            ready = self._phone_ready["hero_car"]
            available = [obs for obs in active_observations
                         if obs.source_id != "driver" and (obs.source_id,obs.object_id) in ready]
            phone_decision = self.policy.evaluate(hero_state,available,self.time,
                max(0.0,distance_to_crosswalk),False,enabled=self.enabled)
            decision = (phone_decision if ready and phone_decision.state != "normal"
                        and phone_decision.brake >= local_response.brake else local_response)
        else:
            decision, receiver_events = self.receiver.command(
                self.time, decision, local_decision, delivered_risks, self.enabled)
        self.messages.extend(receiver_events)
        self.current_decision = decision

        # 4. Vehicle & Walker Controls
        speed_kmh = v.length() * 3.6
        speed_mps = v.length()

        braking = decision.state in ("braking", "decelerating") or self.finished
        if braking and self.brake_at is None and not self.finished:
            self.brake_at = self.time

        # Throttle & Brake control
        if decision.state == "braking" or self.finished:
            throttle_val = 0.0
            brake_val = 1.0
        elif decision.brake > 0:
            throttle_val = 0.0
            brake_val = float(np.clip(decision.brake, 0.0, 1.0))
        else:
            throttle_val = float(
                np.clip(0.35 + (decision.target_speed_mps - speed_mps) * 0.25, 0.0, 0.75)
            )
            brake_val = 0.0

        # Steering: fixed spawn-lane centreline, independent of junction branches.
        lane_origin = self.car_wp.transform.location
        lane_forward = self.car_wp.transform.get_forward_vector()
        axis_length = math.hypot(lane_forward.x, lane_forward.y)
        crosswalk_along = ((self.cross.x - lane_origin.x) * lane_forward.x
                           + (self.cross.y - lane_origin.y) * lane_forward.y) / axis_length
        # Model only the straight approach and 20m beyond the marked crossing.
        steer_val, corridor_stop = straight_corridor_control(
            (c.x, c.y), self.car.get_transform().rotation.yaw, speed_mps,
            (lane_origin.x, lane_origin.y), (lane_forward.x, lane_forward.y),
            crosswalk_along + self.corridor_end_reserve_m, lane_width_m=self.car_wp.lane_width,
            vehicle_half_width_m=self.car.bounding_box.extent.y,
            vehicle_half_length_m=self.car.bounding_box.extent.x,
        )
        if corridor_stop:
            self._corridor_stop_trial = self.trial_number
        if getattr(self, "_corridor_stop_trial", None) == self.trial_number:
            throttle_val, brake_val = 0.0, 1.0

        diagnostic=getattr(self,'study_options',{}).get('diagnostic')
        if diagnostic:
            # Physical brake command at a frozen route marker, never a scripted impact.
            trigger=diagnostic['brake_distance_to_crosswalk_m']
            if self.diagnostic_brake_at is None and distance_to_crosswalk <= trigger:
                self.diagnostic_brake_at=self.time
                self.messages.append({'event':'diagnostic_A_brakes','at_s':self.time,
                    'frame':frame,'distance_to_crosswalk_m':distance_to_crosswalk,
                    'speed_mps':speed_mps,'brake':1.})
            if self.diagnostic_brake_at is None:
                throttle_val=float(np.clip(.35+(self.target_speed_mps-speed_mps)*.25,0,.75))
                brake_val=0.
            else:
                throttle_val,brake_val=0.,1.
            if getattr(self,'_corridor_stop_trial',None)==self.trial_number:
                throttle_val,brake_val=0.,1.

        # Pedestrian walk control across crosswalk
        walk_speed = 0.0 if self.finished else pedestrian_motion(self.pedestrian_behavior,
            self.time - self.crossing_at if self.crossing_at is not None else None,
            self.retreat_after_s,self.pedestrian_crossing_duration_s)
        if self.study_options and walk_speed:
            walk_speed *= self.study_options.get('pedestrian_speed_mps',1.4)/1.4

        if brake_val > 0.0 and self.brake_at is None and not self.finished:
            self.brake_at = self.time

        self.pedestrian_submitted_control={'speed_mps':abs(walk_speed),'direction':[-self.right.x if walk_speed>=0 else self.right.x,-self.right.y if walk_speed>=0 else self.right.y,0.]}
        commands = [
            carla.command.ApplyVehicleControl(
                self.car.id,
                carla.VehicleControl(
                    throttle=throttle_val,
                    brake=brake_val,
                    steer=steer_val,
                ),
            ),
            carla.command.ApplyWalkerControl(
                self.person.id,
                carla.WalkerControl(
                    direction=carla.Vector3D(x=-self.right.x if walk_speed >= 0 else self.right.x,
                                            y=-self.right.y if walk_speed >= 0 else self.right.y, z=0.0),
                    speed=abs(walk_speed),
                ),
            ),
        ]
        self._broadcast_braking("hero_car", self.car, brake_val)
        self._control_followers(commands, crosswalk_along, lane_forward)
        self.messages.extend(self.phone_model.events[self._phone_cursor:])
        self._phone_cursor = len(self.phone_model.events)
        responses=self.client.apply_batch_sync(commands, False)
        if self.study_options:
            for response in responses:
                if response.error:self.messages.append({'event':'control_command_failure','at_s':self.time,'actor_id':response.actor_id,'error':response.error})
        if self.study_options:
            from crossguard.simulation.study_measurements import measure_frame
            self.study_rows.append(measure_frame(self,frame,brake_val,throttle_val))

        # 5. Log Telemetry
        warnings_sent = sum(1 for m in self.messages if m.get("event") == "sent")
        warnings_recv = sum(1 for m in self.messages if m.get("event") == "received")
        messages_dropped = sum(1 for m in self.messages if m.get("event") in ("dropped", "out_of_range"))
        messages_expired = sum(1 for m in self.messages if m.get("event") == "expired")

        row = {
            "time_s": round(self.time, 4),
            "car_x": round(c.x, 3),
            "car_y": round(c.y, 3),
            "pedestrian_x": round(p.x, 3),
            "pedestrian_y": round(p.y, 3),
            "speed_kmh": round(speed_kmh, 2),
            "throttle": round(throttle_val, 2),
            "brake": round(brake_val, 2),
            "steer": round(steer_val, 3),
            "decision_state": decision.state,
            "receiver_mode": self.receiver_mode,
            "advisory_state": self.receiver.advisory_state,
            "driver_response_delay_s": self.driver_response_delay_s,
            "reason": decision.reason,
            "stopping_margin_m": (
                round(decision.stopping_margin_m, 2)
                if decision.stopping_margin_m is not None
                else None
            ),
            "conflict_in_s": (
                round(decision.conflict_in_s, 2)
                if decision.conflict_in_s is not None
                else None
            ),
            "observation_age_s": (
                round(decision.observation_age_s, 3)
                if decision.observation_age_s is not None
                else None
            ),
            "warnings_sent": warnings_sent,
            "warnings_received": warnings_recv,
            "messages_dropped": messages_dropped,
            "messages_expired": messages_expired,
            "active_sources": list(decision.sources),
            "perception_mode": self.perception_mode,
            "crosswalk_valid": self.crosswalk_valid,
            "sim_fps": round(1.0 / DT, 1),
            "corridor_guard": getattr(self, "_corridor_stop_trial", None) == self.trial_number,
            "pedestrian_collision": any(
                e["other_id"] == self.person.id for e in self.collisions
            ),
        }
        self.rows.append(row)

        # End when target clears the modeled scene; watchdog catches deadlocks.
        along = (c - self.car_wp.transform.location).dot(lane_forward)
        all_clear = along >= crosswalk_along + 15.0 and all(
            (actor.get_location() - self.car_wp.transform.location).dot(lane_forward) >= crosswalk_along + 15.0
            for actor in self.followers.values())
        blocked_group = bool(self.followers) and getattr(self,"_corridor_stop_trial",None) == self.trial_number and self.car.get_velocity().length() < 0.1
        end=trial_end_reason(self.time,self.crossing_at,all_clear,blocked_group,self.observation_window_s)
        if end is not None:
            self.finished = True
            self.end_reason = end
            # Stop every controlled actor before declaring a terminal group trial.
            self.client.apply_batch_sync([carla.command.ApplyVehicleControl(actor.id,
                carla.VehicleControl(brake=1.0)) for actor in [self.car,*self.followers.values()]],False)
            self.save(self.end_reason)
            if self.six_view_recorder:
                self.six_view_recorder.capture(self.time,frame,self.views,force=True,marker='outcome')

        # Tick on the next call so returned imagery matches this telemetry frame.

        frames = {name: stream.rgb() for name, stream in self.views.items()}
        if self.followers:
            import cv2
            for name, image in frames.items():
                stream = self.views[name]
                inverse = np.linalg.inv(stream.last_transform)
                w, h = stream.size
                focal = w / (2 * math.tan(math.radians(stream.fov) / 2))
                for label, actor in {"A": self.car, "B": self.followers["follower_b"],
                                     "C": self.followers["follower_c"]}.items():
                    loc = actor.get_location()
                    local = inverse @ np.array([loc.x, loc.y, loc.z + 2.0, 1.0])
                    if local[0] > 0:
                        u = int(w/2 + focal*local[1]/local[0])
                        vv = int(h/2 - focal*local[2]/local[0])
                        if 0 <= u < w and 0 <= vv < h:
                            cv2.putText(image, label, (u, vv), cv2.FONT_HERSHEY_SIMPLEX,
                                        0.8, (255,255,255), 2)
            # Labels are evaluator actor identities; they are not detector outputs.
        if self.rsu_pose:
            import cv2
            for name, image in frames.items():
                stream = self.views[name]
                point = np.array([self.rsu_pose.location.x, self.rsu_pose.location.y,
                                  self.rsu_pose.location.z, 1.0])
                local = np.linalg.inv(stream.last_transform) @ point
                if local[0] > 0:
                    w, h = stream.size
                    focal = w / (2 * math.tan(math.radians(stream.fov) / 2))
                    u, v = int(w/2+focal*local[1]/local[0]), int(h/2-focal*local[2]/local[0])
                    if 0 <= u < w and 0 <= v < h:
                        cv2.circle(image, (u,v), 9, (255,255,255), 2)
                        cv2.putText(image, "CrossGuard RSU", (max(5,min(w-165,u+12)),max(20,v-8)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255,255,255), 2)
        return frames

    def _control_followers(self, commands, crosswalk_along, lane_forward):
        origin = self.car_wp.transform.location
        def state(name, actor):
            loc, velocity, acceleration = actor.get_location(), actor.get_velocity(), actor.get_acceleration()
            return AlongLaneState(name, (loc-origin).dot(lane_forward),
                                  max(0.0, velocity.dot(lane_forward)),
                                  self.vehicle_dimensions[actor.id][0]*2,
                                  acceleration.dot(lane_forward))
        leader_actor = self.car
        leader = state("hero_car", self.car)
        result = {}
        for name, actor in self.followers.items():
            incoming = self.network.receive(name, self.time)
            for msg in incoming:
                braking_event = BrakingEvent.from_message(msg,self.time)
                if braking_event:
                    previous = self._braking_received[name].get(braking_event.origin_id)
                    if previous is None or braking_event.observed_at_s > previous.observed_at_s:
                        self._braking_received[name][braking_event.origin_id] = braking_event
                    self.braking_events.append({"event":"braking_received", "at_s":self.time,
                        "sender_id":msg.sender_id,"receiver_id":name,"channel":msg.channel,
                        "event_id":msg.event_id,"hop_count":msg.hop_count})
                    self._offer_phone(msg)
                    if name == "follower_b":
                        relay = relay_braking(msg,"follower_c",name,self.time)
                        if relay:
                            a,b = actor.get_location(),self.followers["follower_c"].get_location()
                            self.network.send(relay,self.time,(a.x,a.y),(b.x,b.y))
                    continue
                key = (msg.observation.source_id, msg.observation.object_id)
                previous = self.follower_observations[name].get(key)
                if previous is None or msg.observation.observed_at_s > previous.observed_at_s:
                    self.follower_observations[name][key] = msg.observation
            fresh = {key: obs for key, obs in self.follower_observations[name].items()
                     if self.enabled and 0 <= self.time-obs.observed_at_s <= 1.0}
            self.follower_observations[name] = fresh
            observations = [o for o in fresh.values() if self.crossing_candidate(o)]
            follower = state(name, actor)
            # Delayed followers must not obtain current predecessor truth through
            # the remote pedestrian risk calculation. Evaluate their own hazard.
            risk_actor = actor if self.follower_model == "delayed" else leader_actor
            risk_vehicle = follower if self.follower_model == "delayed" else leader
            predecessor_state = VehicleState(risk_vehicle.vehicle_id,
                (risk_actor.get_location().x, risk_actor.get_location().y),
                (risk_actor.get_velocity().x, risk_actor.get_velocity().y),
                length_m=risk_vehicle.length_m)
            risk = self.policy.evaluate(predecessor_state, observations, self.time,
                max(0.0, crosswalk_along-risk_vehicle.position_m), False, enabled=self.enabled)
            for msg in incoming:
                if self.crossing_candidate(msg.observation):
                    msg_risk = self.policy.evaluate(predecessor_state,[msg.observation],self.time,
                        max(0.0,crosswalk_along-risk_vehicle.position_m),False,enabled=self.enabled)
                    if msg_risk.conflict_in_s is not None:
                        self._offer_phone(msg)
            self._poll_phone(name)
            remote_braking = self._braking_received[name].get(leader.vehicle_id)
            if self.receiver_mode == "retrofit":
                ready = self._phone_ready[name]
                available = [obs for obs in observations if (obs.source_id,obs.object_id) in ready]
                risk = self.policy.evaluate(predecessor_state,available,self.time,
                    max(0.0,crosswalk_along-risk_vehicle.position_m),False,enabled=self.enabled)
                if (leader.vehicle_id,leader.vehicle_id) not in ready:
                    remote_braking = None
            following=self.study_following.get(name,self.following_policy)
            decision = following.evaluate(follower, leader, risk, remote_braking, self.time)
            loc = actor.get_location()
            follower_origin = self.follower_origins[name]
            origin_offset = (follower_origin-origin).dot(lane_forward)
            steer, guard = straight_corridor_control((loc.x,loc.y), actor.get_transform().rotation.yaw,
                follower.speed_mps, (follower_origin.x,follower_origin.y), (lane_forward.x,lane_forward.y),
                crosswalk_along+self.corridor_end_reserve_m-origin_offset, lane_width_m=self.car_wp.lane_width,
                vehicle_half_width_m=actor.bounding_box.extent.y,
                vehicle_half_length_m=actor.bounding_box.extent.x)
            if guard:
                self.follower_guards.add(name)
            guarded = name in self.follower_guards
            brake = 1.0 if guarded or self.finished else decision.brake
            throttle = 0.0 if brake > 0 else float(np.clip(
                0.35+(decision.target_speed_mps-follower.speed_mps)*0.25,0,0.75))
            commands.append(carla.command.ApplyVehicleControl(actor.id,
                carla.VehicleControl(throttle=throttle,brake=brake,steer=steer)))
            self._broadcast_braking(name, actor, brake)
            result[name] = {"vehicle_id": name, "predecessor_id": leader.vehicle_id,
                "speed_kmh": round(follower.speed_mps*3.6,2), "brake": round(brake,3),
                "acceleration_mps2": follower.acceleration_mps2,
                "throttle": throttle,
                "position_m": follower.position_m,
                "closing_speed_mps": follower.speed_mps-leader.speed_mps,
                "local_pathway": getattr(following,'telemetry',{}),
                "decision": decision.state, "reason": decision.reason,
                "corridor_guard": guarded,
                "gap_m": round(bumper_gap(follower,leader),3),
                "stopping_margin_m": decision.stopping_margin_m,
                "observation_age_s": decision.observation_age_s,
                "sources": list(decision.sources), "received_observations": len(observations),
                "receiver_mode":self.receiver_mode,
                "phone_state":"responding" if self._phone_ready[name] else ("waiting" if self.phone_available else "unavailable"),
                "upstream_braking_age_s": self.time-remote_braking.observed_at_s if remote_braking else None,
                "sensing": "ideal local predecessor ranging" if self.follower_model == "ideal" else "delayed local control; ideal sampled ranging",
                "follower_model": self.follower_model,
                "local_response_delay_s": self.follower_delay_s if self.follower_model == "delayed" else 0.,
                "local_sample_age_s": self.following_policy.local_sample_age_s.get(name) if self.follower_model == "delayed" else 0.}
            leader, leader_actor = follower, actor
        self.group_metrics = result
        if result:
            self.group_rows.append({"time_s": self.time, "vehicles": result})
        self.messages.extend(self.network.events[self._network_log_cursor:])
        self._network_log_cursor = len(self.network.events)

    def metrics(self) -> Dict[str, Any]:
        """Return comprehensive trial metrics and telemetry summary."""
        v = self.car.get_velocity() if self.car and self.car.is_alive else carla.Vector3D()
        warnings_sent = sum(1 for m in self.messages if m.get("event") == "sent")
        warnings_recv = sum(1 for m in self.messages if m.get("event") == "received")
        messages_dropped = sum(
            1 for m in self.messages if m.get("event") in ("dropped", "out_of_range")
        )
        messages_expired = sum(1 for m in self.messages if m.get("event") == "expired")

        last_row = self.rows[-1] if self.rows else {}

        return {
            "enabled": self.enabled,
            "frame_id": self.frame_id,
            "sim_time_s": round(self.time, 4),
            "end_reason": self.end_reason,
            "detector_backend": getattr(self.detector, "backend", "pytorch"),
            "inference_gpu": getattr(self.detector, "adapter", None),
            "device_mount": getattr(self, "rsu_mount", None) if self.enabled else None,
            "detector_count": sum(self.pipeline_status.get("source_detection_counts", {}).values()),
            "source_detection_counts": self.pipeline_status.get("source_detection_counts", {}),
            "source_frames": self.pipeline_status.get("source_frames", {}),
            "inference_latency_ms": self.pipeline_status.get("inference_latency_ms"),
            "pipeline_completed_batches": self.pipeline_status.get("completed_batches", 0),
            "pipeline_dropped_batches": self.pipeline_status.get("dropped_batches", 0),
            "rgb_detections": self.pipeline_status.get("detections", {}),
            "device_visible_entity": bool(self.unit),
            "baseline_behavior": self.baseline_behavior,
            "coverage_assumption_active": self._healthy_coverage,
            "device_location": ([self.rsu_pose.location.x, self.rsu_pose.location.y, self.rsu_pose.location.z] if self.rsu_pose else None),
            "receiver_mode": self.receiver_mode,
            "advisory_state": self.receiver.advisory_state,
            "driver_response_delay_s": self.driver_response_delay_s,
            "time_s": round(self.time, 3),
            "finished": self.finished,
            "speed_kmh": round(v.length() * 3.6, 2),
            "throttle": last_row.get("throttle", 0.0),
            "brake": last_row.get("brake", 0.0),
            "steer": last_row.get("steer", 0.0),
            "decision_state": last_row.get("decision_state", self.current_decision.state),
            "policy_state": self.current_decision.state,
            "reason": last_row.get("reason", self.current_decision.reason),
            "stopping_margin_m": self.current_decision.stopping_margin_m,
            "conflict_in_s": self.current_decision.conflict_in_s,
            "observation_age_s": self.current_decision.observation_age_s,
            "warnings_sent": warnings_sent,
            "warnings_received": warnings_recv,
            "messages_dropped": messages_dropped,
            "messages_expired": messages_expired,
            "active_sources": list(self.current_decision.sources),
            "perception_mode": self.perception_mode,
            "crosswalk_valid": self.crosswalk_valid,
            "sim_fps": round(1.0 / DT, 1),
            "corridor_guard": getattr(self, "_corridor_stop_trial", None) == self.trial_number,
            "pedestrian_collision": any(
                e["other_id"] == self.person.id for e in self.collisions
            ),
            "other_collisions": [
                e for e in self.collisions if e["other_id"] != self.person.id
            ],
            "crossing_type":self.crossing_type,
            "pedestrian_collisions":[e for e in self.collisions if e['other_id']==self.person.id],
            "group_mode": self.group_mode,
            "communication": self._communication_metrics(),
            "group_vehicles": self.group_metrics,
            "group_collisions": self.group_collisions,
            "secondary_collision": any(e["other_id"] in {self.car.id, *(a.id for a in self.followers.values())}
                                       for e in self.group_collisions),
            "braking_started_s": self.brake_at,
            "first_warning_received_s": self.received_at,
            "minimum_distance_m": (
                round(self.minimum_distance, 2)
                if math.isfinite(self.minimum_distance)
                else None
            ),
        }

    def save(self, reason: str):
        """Save telemetry CSV, metrics JSON, messages JSON, and camera view images."""
        if not hasattr(self, "trial_path") or not self.trial_path.exists():
            return
        if self.study_options:
            from crossguard.simulation.study_measurements import save_study_run
            save_study_run(self,reason)

        (self.trial_path / "metrics.json").write_text(
            json.dumps({**self.metrics(), "end_reason": reason}, indent=2)
        )
        (self.trial_path / "messages.json").write_text(
            json.dumps(self.messages, indent=2)
        )
        if self.followers:
            (self.trial_path / "group_telemetry.json").write_text(json.dumps(self.group_rows, indent=2))

        if self.rows:
            with (self.trial_path / "telemetry.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(self.rows[0].keys()))
                writer.writeheader()
                writer.writerows(self.rows)

        # Save view images
        for name, stream in self.views.items():
            rgb = stream.rgb()
            if rgb is not None:
                img_path = self.trial_path / f"{name}.png"
                Image.fromarray(rgb).save(img_path)

    def close(self):
        try:
            self._close_world()
        finally:
            lease = getattr(self,"_lease",None)
            if lease:
                lease.close()
                self._lease = None

    def _close_world(self):
        """Safely destroy scenario actors and restore CARLA settings."""
        if self.pipeline:
            self.pipeline.close()
            self.pipeline = None
        if hasattr(self, "trial_path"):
            self.save("closed" if not self.finished else (self.end_reason or "finished"))

        self.destroy(self.unit)
        self.destroy(self.scenario)

        for controller in self.controllers:
            try:
                controller.stop()
            except RuntimeError:
                pass
        self.destroy(self.background)

        if self.tm:
            try:
                self.tm.set_synchronous_mode(False)
            except RuntimeError as exc:
                print(f"Traffic Manager cleanup notice: {exc}", flush=True)

        if self.world and self.original:
            try:
                self.world.apply_settings(self.original)
            except RuntimeError as exc:
                print(f"World settings cleanup notice: {exc}", flush=True)
