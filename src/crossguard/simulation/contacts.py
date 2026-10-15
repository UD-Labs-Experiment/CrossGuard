"""Strictly pre-contact snapshots; absence is recorded rather than invented."""
from collections import deque
import math

class ContactHistory:
    def __init__(self):
        self.frames = deque(maxlen=600)

    def remember(self, frame, time_s, velocities):
        self.frames.append((frame,time_s,dict(velocities)))

    def measurement(self, frame, actor_id, other_id):
        for previous, time_s, velocities in reversed(self.frames):
            if previous < frame:
                a,b = velocities.get(actor_id),velocities.get(other_id)
                return {'frame':previous,'time_s':time_s,
                    'actor_speed_mps':math.dist(a,(0,0,0)) if a else None,
                    'other_speed_mps':math.dist(b,(0,0,0)) if b else None,
                    'relative_speed_mps':math.dist(a,b) if a and b else None,
                    'actor_velocity_mps':a,'other_velocity_mps':b,
                    'missing':a is None or b is None}
        return {'missing':True,'reason':'No strictly pre-contact snapshot'}
