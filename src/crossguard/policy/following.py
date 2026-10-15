"""First controlled-group policy: ideal onboard ranging plus optional early evidence.

Predecessor state is a modeled, ideal local ranging input, not RGB perception.
Remote evidence is evaluated by RiskPolicy and never resets its capture time.
This is a bounded straight-lane experiment, not a production ACC controller.
"""
from dataclasses import dataclass, replace
from crossguard.provenance import register_source
register_source(__name__,__file__)
from collections import deque
import math
from crossguard.contracts import Decision


DEFAULT_FOLLOWING_GAP_M = 8.0


def validate_following_gap(value):
    """Dashboard experiment gap, measured between bumpers in metres."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid following gap; enter a number between 3 and 30 metres")
    if not math.isfinite(value) or not 3.0 <= value <= 30.0:
        raise ValueError("Invalid following gap; enter a number between 3 and 30 metres")
    return float(value)


@dataclass(frozen=True)
class AlongLaneState:
    vehicle_id: str
    position_m: float
    speed_mps: float
    length_m: float
    acceleration_mps2: float = 0.0

    def __post_init__(self):
        if not all(math.isfinite(v) for v in
                   (self.position_m, self.speed_mps, self.length_m, self.acceleration_mps2)):
            raise ValueError("Nonfinite lane state")
        if self.speed_mps < 0 or self.length_m <= 0 or not self.vehicle_id:
            raise ValueError("Invalid lane state")


def bumper_gap(follower, predecessor):
    return predecessor.position_m - follower.position_m - (predecessor.length_m + follower.length_m) / 2


class FollowingPolicy:
    def __init__(self, cruise_mps=40/3.6, response_s=0.2, deceleration_mps2=6.0,
                 headway_s=1.2, buffer_m=2.0, target_gap_m=None):
        if (not all(math.isfinite(v) for v in (cruise_mps, response_s, deceleration_mps2, headway_s, buffer_m))
                or cruise_mps <= 0 or response_s < 0 or deceleration_mps2 <= 0
                or headway_s <= 0 or buffer_m < 0):
            raise ValueError("Invalid following parameters")
        self.cruise = cruise_mps
        self.response = response_s
        self.deceleration = deceleration_mps2
        self.headway = headway_s
        self.buffer = buffer_m
        self.target_gap = None if target_gap_m is None else validate_following_gap(target_gap_m)

    def evaluate(self, follower, predecessor, pedestrian_decision=None, remote_braking=None, now_s=None):
        gap = bumper_gap(follower, predecessor)
        desired = (self.target_gap if self.target_gap is not None
                   else self.buffer + self.headway * follower.speed_mps)
        target = min(self.cruise, max(0.0, predecessor.speed_mps + 0.5 * (gap - desired)))
        sources = ()
        age = None
        anticipated = (pedestrian_decision is not None and
                       pedestrian_decision.conflict_in_s is not None and
                       pedestrian_decision.state != "normal")
        reason = "Ideal local ranging: predecessor headway control"
        deceleration = max(0.0, -predecessor.acceleration_mps2)
        if (remote_braking is not None and now_s is not None
                and remote_braking.origin_id == predecessor.vehicle_id
                and 0 <= now_s-remote_braking.observed_at_s <= 1.0
                and now_s <= remote_braking.expires_at_s):
            deceleration = max(deceleration, remote_braking.deceleration_mps2)
            sources = (remote_braking.origin_id,)
            age = now_s-remote_braking.observed_at_s
            reason = "Delivered upstream braking event: stopping-gap control"
        if anticipated:
            # A fresh prediction permits earlier bounded slowing, not a remote brake command.
            deceleration = max(deceleration, self.deceleration)
            target = min(target, pedestrian_decision.target_speed_mps)
            sources = pedestrian_decision.sources
            age = pedestrian_decision.observation_age_s
            reason = "Delivered pedestrian evidence: anticipate upstream braking"
        available = gap - self.buffer
        if deceleration > 0.1:
            available += predecessor.speed_mps**2 / (2 * deceleration)
            safe = math.sqrt((self.deceleration*self.response)**2 +
                             2*self.deceleration*max(0.0, available)) - self.deceleration*self.response
            target = min(target, safe)
        stopping = follower.speed_mps*self.response + follower.speed_mps**2/(2*self.deceleration)
        margin = available - stopping if deceleration > 0.1 else None
        # Local obstacle protection persists with System OFF and without received messages.
        emergency = gap <= self.buffer or (deceleration > 0.1 and margin <= 0 and follower.speed_mps > 0.1)
        if emergency:
            return Decision("braking", 0.0, 1.0, reason+": predecessor stopping-gap conflict", margin,
                            observation_age_s=age, sources=sources)
        brake = min(1.0, max(0.0, (follower.speed_mps-target)/self.deceleration))
        return Decision("decelerating" if brake > 0.01 else "normal", target, brake,
                        reason, margin, observation_age_s=age, sources=sources)


class DelayedFollowingPolicy:
    """Experimental delay of local control decisions, not a calibrated human model.

    Each follower has its own history. Remote control uses current own state and
    a constant-speed projection of the latest delayed predecessor sample, never
    the current predecessor truth. The caller handles message/phone readiness.
    Geometry containment guards remain external and must be logged separately.
    """
    def __init__(self, policy, delay_s=1.0):
        if isinstance(delay_s, bool) or not isinstance(delay_s, (int, float)) or not math.isfinite(delay_s) or not 0 <= delay_s <= 3:
            raise ValueError("Invalid local follower delay; expected 0 to 3 seconds")
        self.policy = policy
        self.delay_s = float(delay_s)
        self.histories = {}
        self.local_sample_age_s = {}

    def evaluate(self, follower, predecessor, pedestrian_decision=None, remote_braking=None, now_s=None):
        if now_s is None or not math.isfinite(now_s) or now_s < 0:
            raise ValueError("Invalid follower simulation time")
        history = self.histories.setdefault(follower.vehicle_id, deque())
        if history and now_s <= history[-1][0]:
            raise ValueError("Follower simulation time must advance; rebuild policy on trial reset")
        local = self.policy.evaluate(follower, predecessor)
        history.append((now_s, predecessor, local))
        cutoff = now_s - self.delay_s
        while len(history) > 1 and history[1][0] <= cutoff + 1e-9:
            history.popleft()
        mature = history[0][0] <= cutoff + 1e-9
        if mature:
            sampled_at, known_predecessor, local = history[0]
            local = replace(local, reason=f"Delayed local response ({self.delay_s:g}s): " + local.reason)
            self.local_sample_age_s[follower.vehicle_id] = now_s - sampled_at
        else:
            # No mature local decision yet: do not disclose the current hazard.
            sampled_at, known_predecessor, _ = history[0]
            known_predecessor = replace(known_predecessor, acceleration_mps2=0.)
            local = Decision("normal", self.policy.cruise, 0., "Local follower response warming up")
            self.local_sample_age_s[follower.vehicle_id] = None

        projected = replace(known_predecessor,
            position_m=known_predecessor.position_m + known_predecessor.speed_mps * (now_s - sampled_at))
        remote = self.policy.evaluate(follower, projected, pedestrian_decision, remote_braking, now_s)
        if remote.sources and (remote.brake > local.brake or remote.target_speed_mps < local.target_speed_mps):
            return replace(remote, brake=max(remote.brake, local.brake),
                target_speed_mps=min(remote.target_speed_mps, local.target_speed_mps),
                reason="Delivered evidence with delayed local predecessor state: " + remote.reason)
        return local


def group_recipient_ids(mode, enabled):
    if mode not in ("single", "lead-only", "cooperative"):
        raise ValueError("Invalid group mode")
    if not enabled:
        return ()
    return ("hero_car", "follower_b", "follower_c") if mode == "cooperative" else ("hero_car",)


class SampledFollowingPolicy:
    """Local ranging samples mature before decisions enter a reaction queue.

    Current predecessor state is only copied at sensing updates. Neither its
    acceleration nor an emergency can jump the declared sensing/reaction delay.
    Delivered cooperative evidence is a separate, explicitly timed input.
    """
    def __init__(self, policy, update_s=.1, sensing_s=.1, reaction_s=.8, brake_limit=.7):
        for name,value,low,high in [('update_s',update_s,.01,1),('sensing_s',sensing_s,0,1),
                                  ('reaction_s',reaction_s,0,3),('brake_limit',brake_limit,.1,1)]:
            if isinstance(value,bool) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid '+name)
        self.policy=policy
        self.update_s=update_s
        self.sensing_s=sensing_s
        self.reaction_s=reaction_s
        self.brake_limit=brake_limit
        self.samples=deque()
        self.pending=deque()
        self.last_update=None
        self.known=None
        self.local=Decision('normal',policy.cruise,0.,'Local sensing/response warming up')
        self.local_sample_age_s={}
        self.telemetry={}

    def evaluate(self, follower, predecessor, pedestrian_decision=None, remote_braking=None, now_s=None):
        if now_s is None or not math.isfinite(now_s):
            raise ValueError('Simulation time required')
        if self.last_update is None or now_s-self.last_update >= self.update_s-1e-9:
            self.samples.append((now_s,predecessor))
            self.last_update=now_s
        while self.samples and self.samples[0][0]+self.sensing_s <= now_s+1e-9:
            sampled_at,known=self.samples.popleft()
            self.known=(sampled_at,known)
            projected=replace(known,position_m=known.position_m+known.speed_mps*(now_s-sampled_at))
            decision=self.policy.evaluate(follower,projected)
            self.pending.append((now_s+self.reaction_s,decision,sampled_at))
        while self.pending and self.pending[0][0] <= now_s+1e-9:
            _,self.local,sampled_at=self.pending.popleft()
            self.local_sample_age_s[follower.vehicle_id]=now_s-sampled_at
        decision=self.local
        if self.known:
            sampled_at,known=self.known
            projected=replace(known,position_m=known.position_m+known.speed_mps*(now_s-sampled_at))
            remote=self.policy.evaluate(follower,projected,pedestrian_decision,remote_braking,now_s)
            if remote.sources and (remote.brake>decision.brake or remote.target_speed_mps<decision.target_speed_mps):
                decision=remote
        self.telemetry={'latest_sample_s':self.known[0] if self.known else None,
                        'queued_responses':len(self.pending),'update_s':self.update_s,
                        'sensing_s':self.sensing_s,'reaction_s':self.reaction_s,
                        'brake_limit':self.brake_limit}
        return replace(decision,brake=min(self.brake_limit,decision.brake),
                       reason='Sampled local following: '+decision.reason)
