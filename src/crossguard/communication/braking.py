"""Immutable braking events and reaction-delay phone advisory model.

Phone advisories model an already running application, not public cell broadcast,
background delivery guarantees, or automatic control of a real car.
"""
from dataclasses import dataclass, replace
from crossguard.provenance import register_source
register_source(__name__,__file__)
import math

from crossguard.contracts import Message, Observation


@dataclass(frozen=True)
class BrakingEvent:
    event_id: str
    origin_id: str
    observed_at_s: float
    expires_at_s: float
    deceleration_mps2: float
    position: tuple[float,float]
    velocity: tuple[float,float] = (0.,0.)

    def __post_init__(self):
        if not self.event_id or not self.origin_id:
            raise ValueError('Braking identity must be nonempty')
        values=(self.observed_at_s,self.expires_at_s,self.deceleration_mps2,*self.position,*self.velocity)
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Braking values must be finite')
        if self.expires_at_s < self.observed_at_s or self.deceleration_mps2 <= 0:
            raise ValueError('Invalid braking expiry or deceleration')

    def to_message(self, receiver_id, sent_at_s, sender_id=None, hop_count=0):
        if not receiver_id or not math.isfinite(sent_at_s) or hop_count < 0:
            raise ValueError('Invalid braking message address/time/hop')
        # Deceleration is encoded explicitly in perception_mode for the existing
        # immutable Observation contract; velocity always remains vehicle velocity.
        observation=Observation(object_id=self.origin_id,source_id=self.origin_id,
            observed_at_s=self.observed_at_s,position=self.position,velocity=self.velocity,
            position_sigma_m=0.,velocity_sigma_mps=0.,label='vehicle_braking',
            perception_mode=f'braking:{self.deceleration_mps2:.9g}')
        return Message(self.event_id,sender_id or self.origin_id,receiver_id,sent_at_s,
                       self.expires_at_s,observation,hop_count,'radio')

    @classmethod
    def from_message(cls,message,now_s=None,max_age_s=1.):
        o=message.observation
        if o.label!='vehicle_braking' or not o.perception_mode.startswith('braking:'):
            return None
        if now_s is not None and (not math.isfinite(now_s) or
                now_s > message.expires_at_s or not 0 <= now_s-o.observed_at_s <= max_age_s):
            return None
        try:
            decel=float(o.perception_mode.split(':',1)[1])
            return cls(message.event_id,o.source_id,o.observed_at_s,
                       message.expires_at_s,decel,o.position,o.velocity)
        except (ValueError,TypeError):
            return None


def relay_braking(message, receiver_id, relay_id, now_s, max_hops=1):
    """Relay once without changing original identity, measurement time or expiry."""
    if BrakingEvent.from_message(message,now_s) is None or message.hop_count >= max_hops:
        return None
    return replace(message,receiver_id=receiver_id,sender_id=relay_id,
                   sent_at_s=now_s,hop_count=message.hop_count+1)


class PhoneAdvisoryModel:
    """Optional fresh-target updates preserve the first notification's deadline.

    Refreshing is opt-in and only occurs while the original reaction is pending.
    Original packet timestamps and expiries remain immutable; the latest valid
    delivered packet replaces pending evidence, never the driver's reaction time.
    """
    def __init__(self, available=True, reaction_delay_s=1.0, max_age_s=2.,
                 refresh_pending_by_target=False):
        self.available=bool(available)
        self.reaction_delay_s=float(reaction_delay_s)
        self.max_age_s=float(max_age_s)
        if not isinstance(refresh_pending_by_target, bool):
            raise ValueError('Phone pending refresh must be boolean')
        self.refresh_pending_by_target=refresh_pending_by_target
        if not math.isfinite(self.max_age_s) or self.max_age_s < 0:
            raise ValueError('Phone max age must be finite and nonnegative')
        self._availability={}
        self._pending={}
        self._pending_targets={}
        self._seen={}
        self.events=[]
        self.configure(reaction_delay_s=reaction_delay_s)

    def configure(self, available=None, reaction_delay_s=None, receiver_id=None):
        if available is not None:
            if receiver_id:
                self._availability[receiver_id]=bool(available)
            else:
                self.available=bool(available)
        if reaction_delay_s is not None:
            if not math.isfinite(reaction_delay_s) or reaction_delay_s < 0:
                raise ValueError('Phone reaction delay must be finite and nonnegative')
            self.reaction_delay_s=float(reaction_delay_s)

    def reset(self):
        self._pending.clear()
        self._pending_targets.clear()
        self._seen.clear()
        self.events.clear()

    def offer(self,message,now_s):
        if not math.isfinite(now_s):
            raise ValueError('Phone offer time must be finite')
        identity=(message.receiver_id,message.event_id)
        target=(message.receiver_id,message.observation.source_id,
                message.observation.object_id,message.observation.label)
        self._seen={k:v for k,v in self._seen.items() if v>=now_s}
        state='delivered'
        react_at=now_s+self.reaction_delay_s
        refresh_details={}
        if identity in self._seen:
            state='duplicate'
        elif not self._availability.get(message.receiver_id,self.available):
            state='unavailable'
        elif now_s > message.expires_at_s or not 0 <= now_s-message.observation.observed_at_s <= self.max_age_s:
            state='expired'
        else:
            self._seen[identity]=message.expires_at_s
            pending_identity=self._pending_targets.get(target) if self.refresh_pending_by_target else None
            pending=self._pending.get(pending_identity)
            if pending is not None and now_s < pending[0]:
                deadline,previous=pending
                react_at=deadline
                if message.observation.observed_at_s > previous.observation.observed_at_s:
                    self._pending[pending_identity]=(deadline,message)
                    state='refreshed'
                    refresh_details={'initial_event_id':pending_identity[1],
                                     'replaced_event_id':previous.event_id}
                else:
                    state='older_evidence'
            else:
                self._pending[identity]=(react_at,message)
                if self.refresh_pending_by_target:
                    self._pending_targets[target]=identity
        event=dict(event=state,receiver_id=message.receiver_id,event_id=message.event_id,
                   at_s=now_s,react_at_s=react_at,
                   observed_at_s=message.observation.observed_at_s,
                   channel=message.channel,reason='modeled_active_phone_app',**refresh_details)
        self.events.append(event)
        return event

    def poll(self,now_s,receiver_id=None):
        if not math.isfinite(now_s):
            raise ValueError('Phone poll time must be finite')
        ready=[]
        for identity,(react_at,message) in list(self._pending.items()):
            if receiver_id is not None and identity[0]!=receiver_id:
                continue
            if react_at>now_s:
                continue
            del self._pending[identity]
            target=(message.receiver_id,message.observation.source_id,
                    message.observation.object_id,message.observation.label)
            if self._pending_targets.get(target)==identity:
                del self._pending_targets[target]
            valid=(self._availability.get(identity[0],self.available)
                   and now_s<=message.expires_at_s
                   and 0<=now_s-message.observation.observed_at_s<=self.max_age_s)
            self.events.append(dict(event='reacted' if valid else 'expired_before_reaction',
                receiver_id=identity[0],event_id=message.event_id,initial_event_id=identity[1],
                at_s=now_s,react_at_s=react_at,
                observed_at_s=message.observation.observed_at_s,channel=message.channel))
            if valid:
                ready.append(message)
        return ready

    def summary(self):
        return dict(available=self.available,availability=dict(self._availability),
                    reaction_delay_s=self.reaction_delay_s,pending=len(self._pending),
                    refresh_pending_by_target=self.refresh_pending_by_target,
                    events=self.events[-20:],simulation_only=True)
