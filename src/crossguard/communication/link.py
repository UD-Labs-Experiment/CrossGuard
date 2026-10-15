"""Simulated V2X Communication Network for cooperative safety.

SI units and simulation seconds.

Features:
- Latency modeling (base delay + deterministic bounded jitter).
- Sorted queue for out-of-order jitter delivery and chronological reception.
- Distance-based communication range boundary.
- Deterministic seeded packet loss.
- Hop-limit enforcement.
- Message expiry enforcement.
- Duplicate event delivery rejection.
- Preservation of original observation timestamps and source IDs.
- Comprehensive event logging: 'sent', 'dropped', 'out_of_range', 'received',
  'expired', 'duplicate'.

DISCLAIMER:
This is a high-level simulation model of latency, jitter, loss, and range
boundaries for evaluation of cooperative collision avoidance policies.
It makes no physical radio propagation, MAC layer (e.g. CSMA/CA), or
standards-compliance (e.g. IEEE 802.11p / DSRC, 3GPP C-V2X PC5 PHY) claims.
"""

from collections import defaultdict
from dataclasses import dataclass
import heapq
import math
from crossguard.provenance import register_source
register_source(__name__,__file__)
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from crossguard.contracts import Message, Observation


@dataclass(frozen=True)
class NetworkEvent:
    event: str
    at_s: float
    sender_id: str
    receiver_id: str
    event_id: str
    channel: str
    observed_at_s: float
    expires_at_s: float
    source_id: str
    sender_position: Tuple[float, float]
    receiver_position: Tuple[float, float]
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "event": self.event,
            "at_s": round(self.at_s, 6),
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "event_id": self.event_id,
            "channel": self.channel,
            "observed_at_s": round(self.observed_at_s, 6),
            "expires_at_s": round(self.expires_at_s, 6),
            "source_id": self.source_id,
            "sender_position": (round(self.sender_position[0], 3), round(self.sender_position[1], 3)),
            "receiver_position": (round(self.receiver_position[0], 3), round(self.receiver_position[1], 3)),
        }
        if self.reason:
            d["reason"] = self.reason
        return d


class SimulatedNetwork:
    """Deterministic simulated communication link and network dispatcher."""

    def __init__(
        self,
        latency_s: float = 0.15,
        jitter_s: float = 0.0,
        loss: float = 0.0,
        range_m: float = 150.0,
        seed: int = 7,
        max_hops: int = 1,
    ):
        values = (latency_s, jitter_s, loss, range_m, max_hops)
        if not all(math.isfinite(x) for x in values):
            raise ValueError("Network parameters must be finite")
        if latency_s < 0:
            raise ValueError("latency_s must be nonnegative")
        if jitter_s < 0:
            raise ValueError("jitter_s must be nonnegative")
        if not (0.0 <= loss <= 1.0):
            raise ValueError("loss must be between 0.0 and 1.0")
        if range_m < 0:
            raise ValueError("range_m must be nonnegative")
        if max_hops < 0:
            raise ValueError("max_hops must be nonnegative")

        self.latency_s = float(latency_s)
        self.jitter_s = float(jitter_s)
        self.loss = float(loss)
        self.range_m = float(range_m)
        self.seed = int(seed)
        self.max_hops = int(max_hops)

        self._random = random.Random(self.seed)
        self.events: List[Dict[str, Any]] = []
        # Per-receiver sorted queue (min-heap): entries are (delivery_time, counter, message, sender_pos, receiver_pos)
        self._queues: Dict[str, List[Tuple[float, int, Message, Tuple[float, float], Tuple[float, float]]]] = defaultdict(list)
        self._counter = 0
        # Delivered event IDs per receiver to reject duplicate event delivery
        self._delivered_event_ids: Set[Tuple[str, str]] = set()

    def reset(self, seed: Optional[int] = None):
        """Reset network state and queues."""
        if seed is not None:
            self.seed = int(seed)
        self._random = random.Random(self.seed)
        self.events.clear()
        self._queues.clear()
        self._counter = 0
        self._delivered_event_ids.clear()

    def _record_event(
        self,
        event: str,
        at_s: float,
        message: Message,
        sender_pos: Tuple[float, float],
        receiver_pos: Tuple[float, float],
        reason: str = "",
    ) -> Dict[str, Any]:
        evt = NetworkEvent(
            event=event,
            at_s=at_s,
            sender_id=message.sender_id,
            receiver_id=message.receiver_id,
            event_id=message.event_id,
            channel=message.channel,
            observed_at_s=message.observation.observed_at_s,
            expires_at_s=message.expires_at_s,
            source_id=message.observation.source_id,
            sender_position=sender_pos,
            receiver_position=receiver_pos,
            reason=reason,
        ).to_dict()
        self.events.append(evt)
        return evt

    def send(
        self,
        message: Message,
        now_s: float,
        sender_position: Tuple[float, float],
        receiver_position: Tuple[float, float],
    ) -> Dict[str, Any]:
        """Send a message across the simulated network.

        Returns an event dictionary representing the dispatch outcome
        ('sent', 'dropped', 'out_of_range', 'expired').
        """
        if not isinstance(message, Message):
            raise TypeError("message must be an instance of crossguard.contracts.Message")
        if not message.receiver_id:
            raise ValueError("Message receiver_id must be specified")

        positions = (*sender_position, *receiver_position)
        if not all(math.isfinite(x) for x in positions) or not math.isfinite(now_s):
            raise ValueError("Timestamps and positions must be finite numbers")

        sender_pos = (float(sender_position[0]), float(sender_position[1]))
        receiver_pos = (float(receiver_position[0]), float(receiver_position[1]))

        # Hop limit enforcement
        if message.hop_count > self.max_hops:
            return self._record_event(
                "dropped", now_s, message, sender_pos, receiver_pos, reason="hop_limit_exceeded"
            )

        # Expiry enforcement at send time
        if now_s > message.expires_at_s:
            return self._record_event(
                "expired", now_s, message, sender_pos, receiver_pos, reason="expired_before_send"
            )

        # Communication range check
        dist = math.hypot(sender_pos[0] - receiver_pos[0], sender_pos[1] - receiver_pos[1])
        if dist > self.range_m:
            return self._record_event(
                "out_of_range", now_s, message, sender_pos, receiver_pos,
                reason=f"distance_{dist:.1f}m_exceeds_range_{self.range_m:.1f}m",
            )

        # Packet loss fault injection
        if self.loss > 0.0 and self._random.random() < self.loss:
            return self._record_event(
                "dropped", now_s, message, sender_pos, receiver_pos, reason="packet_loss"
            )

        # Latency and jitter scheduling
        if self.jitter_s > 0.0:
            jitter = self._random.uniform(-self.jitter_s, self.jitter_s)
        else:
            jitter = 0.0
        delivery_time = max(now_s, now_s + self.latency_s + jitter)

        self._counter += 1
        entry = (delivery_time, self._counter, message, sender_pos, receiver_pos)
        heapq.heappush(self._queues[message.receiver_id], entry)

        return self._record_event(
            "sent", now_s, message, sender_pos, receiver_pos, reason="queued"
        )

    def receive(self, receiver_id: str, now_s: float) -> List[Message]:
        """Retrieve all delivered messages for a given receiver at simulation time now_s.

        Delivers messages in chronological delivery order. Enforces expiry at delivery
        time and rejects duplicate event delivery.
        """
        if not isinstance(receiver_id, str) or not receiver_id:
            raise ValueError("receiver_id must be a non-empty string")
        if not math.isfinite(now_s):
            raise ValueError("now_s must be a finite number")

        delivered: List[Message] = []
        queue = self._queues[receiver_id]

        while queue and queue[0][0] <= now_s + 1e-9:
            delivery_time, _, message, sender_pos, receiver_pos = heapq.heappop(queue)

            # Expiry check at delivery time
            if now_s > message.expires_at_s:
                self._record_event(
                    "expired", now_s, message, sender_pos, receiver_pos, reason="expired_in_transit"
                )
                continue

            # Duplicate event delivery rejection
            dedup_key = (receiver_id, message.event_id)
            if dedup_key in self._delivered_event_ids:
                self._record_event(
                    "duplicate", now_s, message, sender_pos, receiver_pos, reason="duplicate_event_rejected"
                )
                continue

            self._delivered_event_ids.add(dedup_key)
            received_event = self._record_event(
                "received", now_s, message, sender_pos, receiver_pos, reason="delivered"
            )
            received_event["scheduled_delivery_at_s"] = delivery_time
            delivered.append(message)

        return delivered


# Compatibility alias
SimulatedLink = SimulatedNetwork
