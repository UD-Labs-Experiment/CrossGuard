"""Application-level channel models, not implementations of physical protocols.

Optical geometry is evaluated at transmission time. A CARLA caller can supply
orientations and an occlusion test; without geometry, optical fails closed.
"""
from collections import Counter
from crossguard.provenance import register_source
register_source(__name__,__file__)
from dataclasses import dataclass, replace
import math

from crossguard.communication.link import SimulatedNetwork
from crossguard.contracts import Message


@dataclass(frozen=True)
class ChannelConfig:
    latency_s: float
    jitter_s: float
    loss: float
    range_m: float


@dataclass(frozen=True)
class OpticalGeometry:
    emitter_direction: tuple[float, float, float]
    receiver_direction: tuple[float, float, float]
    blocked: bool = False
    emitter_position: tuple[float, float, float] | None = None
    receiver_position: tuple[float, float, float] | None = None
    emitter_fov_deg: float = 120.0
    receiver_fov_deg: float = 120.0


DEFAULT_CONFIGS = {
    'radio': ChannelConfig(.05, .015, .01, 300.),
    'cellular': ChannelConfig(.30, .10, .03, 10000.),
    'optical': ChannelConfig(.01, .003, .01, 120.),
}
LABELS = {'radio': 'PC5-style radio (modeled)', 'cellular': 'Cellular/app (modeled)',
          'optical': 'VLC/LiFi optical (modeled)'}


class ChannelNetwork:
    """Redundant channel delivery with original-event deduplication per receiver.

    geometry(message, sender_xy, receiver_xy) returns OpticalGeometry.
    profiles select one channel or all three (hybrid). Configurations are
    explicit experimental assumptions, not certified real-world performance.
    """
    def __init__(self, profile='radio', seed=7, configs=None, geometry=None,
                 max_hops=2, optical_enabled=True, outages=None):
        self.seed = seed
        self.geometry = geometry
        self.optical_enabled = bool(optical_enabled)
        self._configs = dict(DEFAULT_CONFIGS)
        if configs and set(configs) - set(DEFAULT_CONFIGS):
            raise ValueError('Unknown channel configuration')
        self._configs.update(configs or {})
        self._networks = {key: SimulatedNetwork(latency_s=c.latency_s,
            jitter_s=c.jitter_s, loss=c.loss, range_m=c.range_m,
            seed=seed + i * 1009, max_hops=max_hops)
            for i, (key, c) in enumerate(self._configs.items())}
        self.profile = 'radio'
        self.configure(profile=profile)
        self.events = []
        self._seen = {}
        self._links = {}
        self.outages = list(outages or [])
        for item in self.outages:
            if item['channel'] not in DEFAULT_CONFIGS or not 0 <= item['start_s'] < item['end_s']:
                raise ValueError('Invalid scheduled channel outage')

    def outage(self, channel, now_s):
        return next((item for item in self.outages if item['channel']==channel
                     and item['start_s'] <= now_s < item['end_s']), None)

    def configure(self, profile=None, optical_enabled=None):
        if profile is not None:
            if profile not in ('radio', 'cellular', 'optical', 'hybrid'):
                raise ValueError('Unknown communication profile')
            self.profile = profile
        if optical_enabled is not None:
            self.optical_enabled = bool(optical_enabled)

    def reset(self, seed=None):
        if seed is not None:
            self.seed = seed
        for i, network in enumerate(self._networks.values()):
            network.reset(self.seed + i * 1009)
        self.events.clear()
        self._seen.clear()
        self._links.clear()

    def _append(self, event):
        event = dict(event)
        self.events.append(event)
        key = (event['sender_id'], event['receiver_id'], event['channel'])
        a, b = event['sender_position'], event['receiver_position']
        self._links[key] = dict(sender_id=key[0], receiver_id=key[1], channel=key[2],
            state=event['event'], reason=event.get('reason', ''),
            distance_m=round(math.dist(a, b), 2),
            latency_ms=round(self._configs[key[2]].latency_s * 1000, 1),
            age_s=max(0., event['at_s'] - event['observed_at_s']))
        return event

    @staticmethod
    def _in_cone(direction, ray, fov):
        if not math.isfinite(fov) or not 0 < fov <= 360:
            raise ValueError('Optical FOV must be in (0, 360]')
        if not all(math.isfinite(v) for v in (*direction, *ray)):
            raise ValueError('Optical geometry must be finite')
        norm = math.sqrt(sum(v*v for v in direction)) * math.sqrt(sum(v*v for v in ray))
        if norm <= 1e-9:
            return False
        return sum(a*b for a,b in zip(direction,ray)) / norm >= math.cos(math.radians(fov/2)) - 1e-9

    def _optical_reason(self, message, sender, receiver):
        if not self.optical_enabled:
            return 'optical_disabled'
        if self.geometry is None:
            return 'optical_geometry_unavailable'
        g = self.geometry(message, sender, receiver)
        if g is None:
            return 'optical_geometry_unavailable'
        if g.blocked:
            return 'line_of_sight_blocked'
        a = g.emitter_position or (*sender, 0.)
        b = g.receiver_position or (*receiver, 0.)
        ray = tuple(y-x for x,y in zip(a,b))
        if math.dist(a,b) > self._configs['optical'].range_m:
            return 'optical_out_of_range'
        if not self._in_cone(g.emitter_direction, ray, g.emitter_fov_deg):
            return 'outside_emitter_fov'
        if not self._in_cone(g.receiver_direction, tuple(-x for x in ray), g.receiver_fov_deg):
            return 'outside_receiver_fov'
        return ''

    def send(self, message, now_s, sender_position, receiver_position):
        if not isinstance(message,Message):
            raise TypeError('message must be an instance of Message')
        if not message.receiver_id:
            raise ValueError('Message receiver_id must be specified')
        if message.hop_count < 0:
            raise ValueError('Message hop_count must be nonnegative')
        keys = ('radio', 'cellular', 'optical') if self.profile == 'hybrid' else (self.profile,)
        outcomes = []
        for key in keys:
            adapted = replace(message, channel=key)
            network = self._networks[key]
            outage=self.outage(key,now_s)
            if outage:
                outcomes.append(self._append(network._record_event('blocked',now_s,adapted,
                    sender_position,receiver_position,'scheduled_outage')))
                continue
            if message.hop_count > network.max_hops or now_s > message.expires_at_s:
                outcomes.append(self._append(network.send(adapted,now_s,sender_position,receiver_position)))
                continue
            if key == 'optical':
                # Validate timestamps and hop/expiry before geometry evaluation.
                if not all(math.isfinite(v) for v in (*sender_position,*receiver_position,now_s)):
                    raise ValueError('Timestamps and positions must be finite')
                reason = self._optical_reason(adapted, sender_position, receiver_position)
                if reason:
                    outcome = network._record_event('blocked', now_s, adapted,
                        sender_position, receiver_position, reason)
                    outcomes.append(self._append(outcome))
                    continue
            outcomes.append(self._append(network.send(adapted,now_s,sender_position,receiver_position)))
        # Preserve old single-dispatch dictionary interface; full outcomes in events.
        return next((e for e in outcomes if e['event']=='sent'), outcomes[0])

    def receive(self, receiver_id, now_s):
        if not math.isfinite(now_s):
            raise ValueError('now_s must be finite')
        candidates = []
        for network in self._networks.values():
            offset = len(network.events)
            delivered = network.receive(receiver_id, now_s)
            messages = {m.event_id:m for m in delivered}
            for event in network.events[offset:]:
                if event['event']=='received':
                    if self.outage(event['channel'],now_s):
                        self._append(dict(event,event='blocked',reason='scheduled_outage_in_flight'))
                    else:
                        candidates.append((event,messages[event['event_id']]))
                else:
                    self._append(event)
        candidates.sort(key=lambda pair: (pair[0]['scheduled_delivery_at_s'],
                                           pair[1].channel))
        # Keep identities until expiry. This bounds memory across long runs.
        self._seen = {k:v for k,v in self._seen.items() if v >= now_s}
        result = []
        for event, message in candidates:
            identity=(receiver_id,message.event_id)
            if identity in self._seen:
                event=dict(event,event='duplicate',reason='redundant_original_event_rejected')
            else:
                self._seen[identity]=message.expires_at_s
                result.append(message)
            self._append(event)
        return result

    def summary(self, now_s=0.):
        channels=[]
        for key,network in self._networks.items():
            counts=Counter(e['event'] for e in self.events if e['channel']==key)
            channels.append(dict(id=key,label=LABELS[key],sent=counts['sent'],
                delivered=counts['received'],dropped=sum(counts[k] for k in ('dropped','expired','out_of_range')),
                blocked=counts['blocked'],duplicates=counts['duplicate'],
                pending=sum(len(q) for q in network._queues.values()),
                active=self.profile=='hybrid' or self.profile==key,
                latency_ms=self._configs[key].latency_s*1000,
                loss=self._configs[key].loss,range_m=self._configs[key].range_m))
        return dict(profile=self.profile,channels=channels,links=list(self._links.values()),
                    optical_enabled=self.optical_enabled,simulation_only=True)
