"""Receive-clock deadlines and bounded, non-blocking telemetry diagnostics."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal
import math


NORMAL_TRAFFIC_DELAY_S = 2.0
HARD_TRAFFIC_DELAY_S = 4.0
MAX_RECEIPT_IDENTITIES = 131072
MAX_DEDUP_STREAMS = 256
MAX_COMPLETION_RECORDS = 1024
# Acquisition timestamps are parsed through datetime/timedelta, whose source
# resolution is one microsecond. Canonicalise only explicit source domains;
# coordinate-less legacy samples retain their original identity contract.
ACQUISITION_QUANTUM_S = Decimal("0.000001")


@dataclass
class PendingTraffic:
    key: tuple
    driver: str
    lap_number: int
    sample_index: int
    completed_at: float
    clock_kind: str
    normal_done: bool = False
    last_revision: int = -1
    acquisition_bounds: tuple[float, float] | None = None
    acquisition_coordinate: str | None = None

    @property
    def normal_deadline(self):
        return self.completed_at + NORMAL_TRAFFIC_DELAY_S

    @property
    def hard_deadline(self):
        return self.completed_at + HARD_TRAFFIC_DELAY_S


class TelemetryDelayMonitor:
    """Count distinct late driver-laps only after a real observation arrives.

    First receipts are retained for ten minutes (or the configured window),
    with explicit cardinality caps. Captures without trustworthy receipts do
    not enter the measurement ledger or produce a delivery warning.
    """

    def __init__(self, *, threshold=3, window_s=600.):
        if int(threshold) < 1 or not math.isfinite(window_s) or window_s <= 0:
            raise ValueError("Telemetry warning threshold/window must be positive")
        self.threshold = int(threshold)
        self.window_s = float(window_s)
        self.completions = OrderedDict()
        self.first_receipts = OrderedDict()
        self._receipt_anchors = {}
        self._receipt_expiry_order = deque()
        self._receipt_ids_by_driver = {}
        self._associated_receipts = set()
        self._coordinate_offset_s = None
        self._new_completions = set()
        # Explicit invalidation differs from routine completion retention.
        # Exact recent identities permit a corrected completion of the same
        # lap; bounded overflow suppresses measurements for older lap numbers.
        self._revoked_completions = OrderedDict()
        self._forgotten_revocation_lap_floor = {}
        self._revocation_stream_capacity_exceeded = False
        # Once an identity is forgotten, an older acquisition timestamp can
        # no longer prove first arrival. Conservatively suppress measurements
        # in that range instead of calling a retransmission new late evidence.
        self._forgotten_acquisition_floor = {}
        self._dedup_stream_capacity_exceeded = False
        self.late_laps = OrderedDict()
        self.now = None
        self.trustworthy = False
        self.observed_late_samples = 0
        self.maximum_delay_s = None

    def advance(self, now):
        if now is None:
            return
        self.now = max(now, self.now if self.now is not None else now)
        floor = self.now - self.window_s
        while self._receipt_expiry_order and self._receipt_expiry_order[0][0] < floor:
            anchor, identity = self._receipt_expiry_order.popleft()
            if self._receipt_anchors.get(identity) == anchor:
                self._discard_receipt(identity)
        while self.late_laps and next(iter(self.late_laps.values())) < floor:
            self.late_laps.popitem(last=False)
        while self.completions and next(iter(self.completions.values())) < floor:
            key, _ = self.completions.popitem(last=False)
            self._new_completions.discard(key)
        if not self.first_receipts and not self.completions:
            self._forgotten_acquisition_floor.clear()
            self._dedup_stream_capacity_exceeded = False

    def _discard_receipt(self, identity):
        if identity not in self.first_receipts:
            return
        del self.first_receipts[identity]
        self._receipt_anchors.pop(identity, None)
        self._associated_receipts.discard(identity)
        kind, number, stamp = identity[:3]
        coordinate = identity[3] if len(identity) > 3 else None
        identities = self._receipt_ids_by_driver[number]
        identities.discard(identity)
        if not identities:
            del self._receipt_ids_by_driver[number]
        stream = ((kind, number, coordinate) if coordinate is not None else (kind, number))
        if stream in self._forgotten_acquisition_floor:
            self._forgotten_acquisition_floor[stream] = max(
                stamp, self._forgotten_acquisition_floor[stream])
        elif len(self._forgotten_acquisition_floor) < MAX_DEDUP_STREAMS:
            self._forgotten_acquisition_floor[stream] = stamp
        else:
            self._dedup_stream_capacity_exceeded = True

    def _associate_early_receipts(self, adapter):
        # Run once per new completion, against that driver's bounded receipt
        # index. An on-time receipt must outlive its completion's warning
        # window even though it arrived before that completion.
        for key in self._new_completions:
            bounds = adapter.completion_bounds(key) if hasattr(adapter, "completion_bounds") else None
            if bounds is None:
                continue
            completed_at = self.completions.get(key)
            if completed_at is None:
                continue
            for identity in self._receipt_ids_by_driver.get(key[1], ()):
                if identity in self._associated_receipts:
                    continue
                stamp = identity[2]
                coordinate = identity[3] if len(identity) > 3 else None
                if coordinate is not None:
                    expected = (adapter.completion_coordinate(key)
                                if hasattr(adapter, "completion_coordinate") else None)
                    if expected is not None:
                        stamp = self._convert_acquisition(stamp, coordinate, expected)
                        if stamp is None:
                            continue
                    elif adapter.completed_lap_for_sample(key[1], stamp, coordinate) != key:
                        continue
                if not bounds[0] < stamp <= bounds[1]:
                    continue
                self._associated_receipts.add(identity)
                anchor = max(completed_at, self.now, self._receipt_anchors[identity])
                if anchor > self._receipt_anchors[identity]:
                    self._receipt_anchors[identity] = anchor
                    self._receipt_expiry_order.append((anchor, identity))
        self._new_completions.clear()
        self._compact_expiry_index()

    def _compact_expiry_index(self):
        if len(self._receipt_expiry_order) > 2 * MAX_RECEIPT_IDENTITIES:
            self._receipt_expiry_order = deque(
                (anchor, item) for anchor, item in self._receipt_expiry_order
                if self._receipt_anchors.get(item) == anchor)

    def _convert_acquisition(self, stamp, coordinate, target):
        if coordinate == target:
            return self._canonical_acquisition(stamp, coordinate)
        offset = self._coordinate_offset_s
        if offset is None:
            return None
        if coordinate == "utc" and target == "session":
            converted = Decimal(str(stamp)) - Decimal(str(offset))
            return float(converted.quantize(ACQUISITION_QUANTUM_S))
        if coordinate == "session" and target == "utc":
            converted = Decimal(str(stamp)) + Decimal(str(offset))
            return float(converted.quantize(ACQUISITION_QUANTUM_S))
        return None

    @staticmethod
    def _canonical_acquisition(stamp, coordinate):
        # This is a deterministic precision representation, not a nearest
        # neighbour search. Samples one source tick apart remain distinct.
        # Large UTC subtraction happens in decimal above, before conversion
        # back to the canonical float key; binary cancellation never owns
        # observation identity.
        return round(stamp, 6) if coordinate in {"utc", "session"} else stamp

    def _receipt_alias(self, identity):
        if len(identity) < 4 or identity[3] not in {"utc", "session"}:
            return None
        other = "session" if identity[3] == "utc" else "utc"
        stamp = self._convert_acquisition(identity[2], identity[3], other)
        return (*identity[:2], stamp, other) if stamp is not None else None

    def _is_revoked(self, key):
        if key in self._revoked_completions:
            return True
        stream = key[:2]
        return (self._revocation_stream_capacity_exceeded or key[2] <=
                self._forgotten_revocation_lap_floor.get(stream, -math.inf))

    def revoke(self, key):
        """Withdraw an explicit completion while preserving receipt identities.

        Tombstones survive warning expiry, so a retransmission cannot obtain a
        newly measured delay. Acquisition timestamps from different domains
        never order this ledger: only the driver's lap counter is used when
        cardinality forces conservative suppression of old identities.
        """
        if key is None:
            return
        self.completions.pop(key, None)
        self._new_completions.discard(key)
        self.late_laps.pop(key, None)
        self._revoked_completions.setdefault(key, None)
        while len(self._revoked_completions) > MAX_COMPLETION_RECORDS:
            expired, _ = self._revoked_completions.popitem(last=False)
            stream, lap_number = expired[:2], expired[2]
            if stream in self._forgotten_revocation_lap_floor:
                self._forgotten_revocation_lap_floor[stream] = max(
                    lap_number, self._forgotten_revocation_lap_floor[stream])
            elif len(self._forgotten_revocation_lap_floor) < MAX_DEDUP_STREAMS:
                self._forgotten_revocation_lap_floor[stream] = lap_number
            else:
                self._revocation_stream_capacity_exceeded = True

    def completed(self, key, now, trusted):
        if not trusted or now is None or self._is_revoked(key):
            return
        self.trustworthy = True
        self.advance(now)
        if key not in self.completions:
            self.completions[key] = now
            self._new_completions.add(key)
        while len(self.completions) > MAX_COMPLETION_RECORDS:
            expired, _ = self.completions.popitem(last=False)
            self._new_completions.discard(expired)

    def observe(self, raw, adapter):
        if not getattr(raw, "receive_time_trusted", False):
            self.trustworthy = False
            return
        self.trustworthy = True
        now = raw.traffic_clock_time_s
        if now is None:
            self.trustworthy = False
            return
        self.advance(now)
        offset = getattr(raw, "speed_coordinate_offset_s", None)
        self._coordinate_offset_s = offset if offset is not None and math.isfinite(offset) else None
        self._associate_early_receipts(adapter)
        samples = [("position", item.driver_number, item.session_time_s,
                    getattr(item, "acquisition_coordinate", None))
                   for item in raw.position_observations]
        samples.extend(("speed", item.driver_number, item.session_time_s,
                        getattr(item, "acquisition_coordinate", None))
                       for item in getattr(raw, "speed_observations", ()))
        for kind, number, stamp, coordinate in samples:
            stamp = self._canonical_acquisition(stamp, coordinate)
            identity = ((kind, number, stamp, coordinate) if coordinate is not None
                        else (kind, number, stamp))
            stream = ((kind, number, coordinate) if coordinate is not None else (kind, number))
            if identity in self.first_receipts:
                continue
            alias = self._receipt_alias(identity)
            known_alias = alias is not None and alias in self.first_receipts
            forgotten_alias = (alias is not None and alias[2] <=
                               self._forgotten_acquisition_floor.get(
                                   (*alias[:2], alias[3]), -math.inf))
            forgotten = (self._dedup_stream_capacity_exceeded or forgotten_alias or stamp <=
                         self._forgotten_acquisition_floor.get(stream, -math.inf))
            # Keep a proven alias with the original first-arrival metadata.
            # This remains a duplicate if a later mapping becomes unavailable.
            self.first_receipts[identity] = self.first_receipts[alias] if known_alias else now
            # Retention anchors follow processing progress, keeping the expiry
            # queue ordered even when a newly proven alias arrived much earlier.
            anchor = max(self.now, self._receipt_anchors[alias]) if known_alias else self.now
            self._receipt_anchors[identity] = anchor
            self._receipt_expiry_order.append((anchor, identity))
            if known_alias and alias in self._associated_receipts:
                self._associated_receipts.add(identity)
            self._receipt_ids_by_driver.setdefault(number, set()).add(identity)
            while len(self.first_receipts) > MAX_RECEIPT_IDENTITIES:
                self._discard_receipt(next(iter(self.first_receipts)))
            # At most two expiry entries exist per retained identity. Compact
            # stale entries after cardinality eviction as well as time expiry.
            self._compact_expiry_index()
            if forgotten or known_alias:
                continue
            key = (adapter.completed_lap_for_sample(number, stamp, coordinate)
                   if coordinate is not None else adapter.completed_lap_for_sample(number, stamp))
            if alias is not None:
                alias_key = adapter.completed_lap_for_sample(number, alias[2], alias[3])
                owners = {owner for owner in (key, alias_key) if owner is not None}
                # A bridge can establish ownership, but two independently
                # different owners make this measurement ambiguous.
                key = next(iter(owners)) if len(owners) == 1 else None
            completed_at = self.completions.get(key)
            if completed_at is None:
                continue
            delay = now - completed_at
            if delay <= NORMAL_TRAFFIC_DELAY_S:
                continue
            self.observed_late_samples += 1
            self.maximum_delay_s = max(delay, self.maximum_delay_s or delay)
            # The same driver-lap never refreshes the warning window.
            self.late_laps.setdefault(key, now)
            while len(self.late_laps) > MAX_COMPLETION_RECORDS:
                self.late_laps.popitem(last=False)

    @property
    def count(self):
        return len(self.late_laps) if self.trustworthy else 0

    @property
    def warning(self):
        if self.count < self.threshold:
            return ""
        minutes = self.window_s / 60.
        return (f"⚠ Track telemetry delayed: {self.count} laps exceeded 2s in the "
                f"last {minutes:g} min. Track Correction may update late.")
