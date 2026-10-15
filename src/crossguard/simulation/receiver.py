"""Simulator-independent receiver/driver model. No phone or audio device I/O."""
from dataclasses import replace
from crossguard.provenance import register_source
register_source(__name__,__file__)
import math

from crossguard.contracts import Decision


class ReceiverController:
    def __init__(self, mode="autonomous", driver_response_delay_s=1.0, cruise_mps=13.9):
        if mode not in ("autonomous", "retrofit"):
            raise ValueError("receiver_mode must be autonomous or retrofit")
        if not math.isfinite(driver_response_delay_s) or driver_response_delay_s < 0:
            raise ValueError("Driver response delay must be finite and nonnegative")
        self.mode = mode
        if isinstance(cruise_mps,bool) or not isinstance(cruise_mps,(int,float)) or not math.isfinite(cruise_mps) or cruise_mps<=0:
            raise ValueError('Cruise speed must be finite and positive')
        self.cruise_mps=float(cruise_mps)
        self.driver_response_delay_s = driver_response_delay_s
        self.advisory_state = "clear"
        self.pending_since = None
        self.notification = None
        self.notification_at = None
        self.notification_responded = False
        self.seen = set()
        self.visual_seen_at = None
        self.visual_observation = None
        self.visual_responded = False

    def local_visual_command(self, now_s, local, visible_observation=None):
        """Model a driver's remembered crossing hazard independently of the phone.

        The caller restricts the observation to an approaching/in-lane pedestrian
        at the crossing. Once seen, a missed camera frame or a changed TTC cannot
        restart reaction time or erase the driver's intent to stop. A new trial
        constructs a new receiver and clears this stop intention.
        """
        events = []
        if self.mode != "retrofit":
            return local, events
        valid = (visible_observation is not None
                 and visible_observation.source_id in ("driver", "driver_camera")
                 and visible_observation.label == "person"
                 and visible_observation.confidence >= 0.25
                 and 0 <= now_s-visible_observation.observed_at_s <= 1.0)
        if valid and self.visual_seen_at is None:
            self.visual_seen_at = now_s
            self.visual_observation = visible_observation
            events.append({"event": "local_hazard_seen", "channel": "local_driver",
                           "at_s": now_s, "source_id": visible_observation.source_id,
                           "object_id": visible_observation.object_id,
                           "observed_at_s": visible_observation.observed_at_s,
                           "response_due_at_s": now_s+self.driver_response_delay_s})
        if self.visual_seen_at is None:
            return self.command(now_s, local, local, (), enabled=False)
        elapsed = now_s-self.visual_seen_at
        if elapsed < self.driver_response_delay_s:
            self.advisory_state = "waiting_driver"
            return replace(local, state="normal", brake=0.0, target_speed_mps=self.cruise_mps,
                           reason="Simulated driver: pedestrian seen; visual response pending"), events
        if not self.visual_responded:
            self.visual_responded = True
            events.append({"event": "local_driver_response", "channel": "local_driver",
                           "at_s": now_s, "driver_response_delay_s": elapsed,
                           "observed_at_s": self.visual_observation.observed_at_s,
                           "source_id": self.visual_observation.source_id,
                           "object_id": self.visual_observation.object_id})
        self.advisory_state = "driver_responding"
        return replace(local, state="braking", brake=1.0, target_speed_mps=0.0,
                       reason="Simulated driver: stop for locally seen crossing pedestrian",
                       sources=(self.visual_observation.source_id,),
                       observation_age_s=now_s-self.visual_observation.observed_at_s), events

    def command(self, now_s, cooperative, local, delivered_risks=(), enabled=True):
        """Return applied modeled driver decision and new notification events.

        delivered_risks contains (Message, Decision) evaluated from each actual
        delivered observation. This excludes packet drops and mere send events.
        """
        events = []
        if self.mode == "autonomous":
            self.advisory_state = "autonomous"
            return cooperative if enabled else local, events
        risk = cooperative if enabled else local
        if enabled:
            for message, decision in delivered_risks:
                if message.event_id in self.seen:
                    continue
                self.seen.add(message.event_id)
                if decision.conflict_in_s is None or self.notification is not None:
                    continue
                self.notification = {"event_id": message.event_id,
                                     "source_id": message.observation.source_id,
                                     "observed_at_s": message.observation.observed_at_s,
                                     "receiver_id": message.receiver_id,
                                     "channel": "simulated_audio"}
                self.notification_at = now_s
                self.notification_responded = False
                for event in ("notification_created", "notification_delivered"):
                    events.append({**self.notification, "event": event, "at_s": now_s})
        requires_response = risk.state != "normal" or self.notification is not None
        # With unobstructed clearance and no current conflict, do not perpetually
        # latch a stale audio warning. Occlusion still produces local caution.
        if risk.state == "normal":
            if self.notification is not None:
                events.append({**self.notification, "event": "notification_cleared", "at_s": now_s})
            self.pending_since = None
            self.notification = None
            self.notification_at = None
            self.advisory_state = "clear"
            return local, events
        if requires_response and self.pending_since is None:
            self.pending_since = now_s
        if now_s - self.pending_since < self.driver_response_delay_s:
            self.advisory_state = "waiting_driver"
            return replace(local, state="normal", brake=0.0,
                           target_speed_mps=self.cruise_mps,
                           reason="Simulated driver response pending"), events
        audio_ready = (self.notification_at is not None and
                       now_s - self.notification_at >= self.driver_response_delay_s)
        if audio_ready and not self.notification_responded:
            events.append({**self.notification, "event": "driver_response", "at_s": now_s,
                           "driver_response_delay_s": now_s - self.notification_at})
            self.notification_responded = True
        if self.advisory_state != "driver_responding" and self.notification is None:
            events.append({"channel": "local_driver",
                           "event": "local_driver_response",
                           "at_s": now_s,
                           "driver_response_delay_s": now_s - self.pending_since})
        self.advisory_state = "driver_responding"
        applied = risk if audio_ready else local
        return replace(applied, reason="Simulated driver: " + applied.reason), events
