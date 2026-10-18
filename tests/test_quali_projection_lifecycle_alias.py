"""Bounded public-ingestion matrix for the lifecycle/alias closure review."""
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import LiveTimingMessage, QualiSessionStore
from woplanner.quali.service import QualiLiveService


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
A = (20, 30, 40)
B = (25, 26, 39)
PENDING = (21, 31, 38)
EMPTY = (None, None, None)


def sectors(values):
    return {str(i): {"Value": "" if value is None else str(value)}
            for i, value in enumerate(values)}


class Ingestion:
    def __init__(self, *, utc_source=False):
        self.utc_source = utc_source
        self.store = QualiSessionStore()
        self.service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
        self.send({"InPit": False, "NumberOfLaps": 1}, 0, number="1")

    def send(self, patch, source, *, number="31", topic="TimingData", delivery=None,
             utc_source=None):
        payload = {"Lines": {number: patch}} if topic == "TimingData" else patch
        received = NOW + timedelta(seconds=delivery if delivery is not None else source)
        utc_source = self.utc_source if utc_source is None else utc_source
        message = LiveTimingMessage(
            topic, payload, NOW + timedelta(seconds=source) if utc_source and source is not None else None,
            received, session_time_seconds=None if utc_source or source is None else float(source))
        self.raw = self.store.apply(message)
        self.result = self.service.consume(self.raw)
        return self.raw

    def complete(self, boundary=3, source=100, values=A, alias=None, best=True, lap="1:30"):
        # These ownership/projection scenarios model a confirmed participant,
        # including a driver first discovered after an explicit Q2 transition.
        patch = {"InPit": False, "KnockedOut": False, "NumberOfLaps": boundary,
                 "LastLapTime": {"Value": lap}}
        if best:
            patch["BestLapTime"] = {"Value": lap}
            if alias is not None:
                patch["BestLapTime"]["Lap"] = alias
        if values is not None:
            patch["Sectors"] = sectors(values)
        return self.send(patch, source)

    def owner(self):
        return next(d for d in self.raw.drivers if d.driver_number == "31").best_lap_event_key

    def projection(self, s1=25, source=400):
        self.send({"InPit": False, "NumberOfLaps": 1, "Sectors": {"0": {"Value": str(s1)}}},
                  source, number="1")
        assert next(d for d in self.raw.drivers if d.driver_number == "1").timed_lap_eligible
        return next(d for d in self.result.drivers if d.driver_number == "1").projected_lap_s


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("extra_delivery", ["none", "before", "after"])
def test_rejected_boundary_retires_pending_cycle_even_on_ledger_hit(duplicate, extra_delivery):
    h = Ingestion()
    h.complete(alias=2)
    h.send({"Sectors": sectors(EMPTY)}, 110)
    h.send({"Sectors": sectors(PENDING)}, 150)
    patch = {"NumberOfLaps": 4, "LastLapTime": {"Value": ""}}
    if duplicate:
        patch["Sectors"] = sectors(A)
    h.send(patch, 200)
    if extra_delivery == "before":
        h.send({"Sectors": sectors(A)}, 201)
    h.complete(5, 300, values=None, alias=4)
    if extra_delivery == "after":
        h.send({"Sectors": sectors(A)}, 301)
    assert h.raw.completed_lap_sequence[0].event_key == ("Q1", "31", 3, 100.0)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[1].event_key == ("Q1", "31", 5, 300.0)
    assert h.raw.completed_lap_sequence[1].sectors_s == EMPTY
    assert h.owner() == h.raw.completed_lap_sequence[1].event_key
    assert h.projection() is None


@pytest.mark.parametrize("retract", [False, True])
@pytest.mark.parametrize("known_alias", [False, True])
@pytest.mark.parametrize("same_completion", [False, True])
def test_delayed_best_cannot_select_newest_equal_time_after_owner_loss(
        retract, known_alias, same_completion):
    h = Ingestion()
    h.complete(alias=2 if known_alias else None)
    clear = {"BestLapTime": {"Value": ""}}
    if retract:
        clear.update({"NumberOfLaps": 3, "LastLapTime": {"Value": ""}})
    h.send(clear, 101)
    h.complete(4, 200, B, alias=2, best=same_completion)
    if not same_completion:
        h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 201)
    expected = ("Q1", "31", 3, 100.0) if known_alias and not retract else None
    assert h.owner() == expected
    first, second = h.raw.completed_lap_sequence
    assert (first.sectors_s, second.sectors_s) == (A, B)
    assert second.official_lap_number is None
    projection = h.projection(24)
    assert projection == (pytest.approx(108.0) if expected else None)


@pytest.mark.parametrize("parts", [("Q1", "Q2"), ("Q2", "Q3")])
@pytest.mark.parametrize("stale_packet", ["none", "clear", "values", "clear_and_values"])
def test_stale_packet_cannot_anchor_first_new_part_event(parts, stale_packet):
    h = Ingestion()
    h.send({"SessionPart": parts[0]}, 1, topic="SessionInfo")
    h.complete(alias=2)
    h.send({"SessionPart": parts[1]}, 110, topic="SessionInfo")
    if stale_packet != "none":
        values = EMPTY if stale_packet == "clear" else PENDING
        h.send({"Sectors": sectors(values)}, 90, delivery=150)
        if stale_packet == "clear_and_values":
            h.send({"Sectors": sectors(EMPTY)}, 91, delivery=150.1)
    h.send({"Sectors": sectors(PENDING)}, 151)
    h.complete(4, 220, values=None, alias=3)
    first, second = h.raw.completed_lap_sequence
    assert first.event_key == (parts[0], "31", 3, 100.0)
    assert first.sectors_s == A
    assert second.event_key == (parts[1], "31", 4, 220.0)
    assert second.sectors_s == EMPTY
    assert h.owner() == second.event_key
    assert h.projection() is None


@pytest.mark.parametrize("parts", [("Q1", "Q2"), ("Q2", "Q3")])
@pytest.mark.parametrize("stale_values", [False, True])
def test_stale_packet_cannot_retire_genuine_new_part_cycle(parts, stale_values):
    h = Ingestion()
    h.send({"SessionPart": parts[0]}, 1, topic="SessionInfo")
    h.complete(alias=2)
    h.send({"SessionPart": parts[1]}, 110, topic="SessionInfo")
    h.send({"Sectors": sectors(EMPTY)}, 120)
    h.send({"Sectors": {"0": {"Value": "25"}}}, 145)
    h.send({"Sectors": sectors(PENDING if stale_values else EMPTY)}, 90, delivery=150)
    h.send({"Sectors": {"1": {"Value": "26"}}}, 171)
    h.complete(4, 220, values=None, alias=3)
    h.send({"Sectors": {"2": {"Value": "39"}}}, 221)
    assert h.raw.completed_lap_sequence[1].sectors_s == B
    assert h.owner() == (parts[1], "31", 4, 220.0)
    assert h.projection(24) == pytest.approx(86.4)


@pytest.mark.parametrize("path", ["same_packet", "sector_first", "late_trailing", "late_original_source"])
def test_positive_first_association_and_late_enrichment(path):
    h = Ingestion()
    if path in {"sector_first", "late_trailing"}:
        h.send({"NumberOfLaps": 2}, 1)
        h.send({"Sectors": {"0": {"Value": "20"}}}, 25)
        h.send({"Sectors": {"1": {"Value": "30"}}}, 55)
        if path == "sector_first":
            h.send({"Sectors": {"2": {"Value": "40"}}}, 95)
    h.complete(values=A if path == "same_packet" else None)
    key = h.owner()
    if path.startswith("late"):
        h.send({"Position": 1}, 150)
        late = {"2": {"Value": "40"}} if path == "late_trailing" else sectors(A)
        h.send({"Sectors": late}, 151 if path == "late_trailing" else 100, delivery=151)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 152)
    assert h.owner() == key == ("Q1", "31", 3, 100.0)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[0].official_lap_number == 2
    assert h.projection() == pytest.approx(112.5)


@pytest.mark.parametrize("repeated", [A, (20, 31, 39)])
@pytest.mark.parametrize("duplicate_position", ["before", "during", "after"])
def test_separate_observed_cycles_can_repeat_values_without_rebinding_history(repeated, duplicate_position):
    h = Ingestion()
    h.complete(alias=2)
    if duplicate_position == "before":
        h.send({"Sectors": sectors(A)}, 100, delivery=105)
    h.send({"Sectors": sectors(EMPTY)}, 110)
    h.send({"Sectors": {"0": {"Value": str(repeated[0])}}}, 130)
    if duplicate_position == "during":
        h.send({"Sectors": sectors(A)}, 100, delivery=140)
    h.send({"Sectors": {"1": {"Value": str(repeated[1])}}}, 161)
    h.complete(4, 200, values=None, alias=3)
    h.send({"Sectors": {"2": {"Value": str(repeated[2])}}}, 201)
    if duplicate_position == "after":
        h.send({"Sectors": sectors(A)}, 100, delivery=210)
    first, second = h.raw.completed_lap_sequence
    assert first.event_key == ("Q1", "31", 3, 100.0) and first.sectors_s == A
    assert second.event_key == ("Q1", "31", 4, 200.0) and second.sectors_s == repeated
    assert h.owner() == second.event_key
    assert h.projection() == pytest.approx(25 * 90 / repeated[0])


def test_ambiguity_recovers_with_genuinely_new_completion_evidence():
    h = Ingestion()
    h.complete()
    h.send({"NumberOfLaps": 3, "LastLapTime": {"Value": ""},
            "BestLapTime": {"Value": ""}}, 101)
    h.complete(4, 200, B, best=False)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 201)
    assert h.owner() is None
    h.send({"Sectors": sectors(EMPTY)}, 210)
    h.send({"Sectors": {"0": {"Value": "19"}}}, 229)
    h.send({"Sectors": {"1": {"Value": "29"}}}, 258)
    h.complete(5, 300, (19, 29, 40), alias=4, lap="1:28")
    assert h.owner() == ("Q1", "31", 5, 300.0)
    assert h.raw.completed_lap_sequence[-1].sectors_s == (19, 29, 40)
    assert h.projection() == pytest.approx(25 * 88 / 19)


@pytest.mark.parametrize("utc_source", [False, True])
@pytest.mark.parametrize("origin", ["old", "unknown"])
def test_origin_scrutiny_uses_source_not_receive_or_accumulated_clock(utc_source, origin):
    h = Ingestion(utc_source=utc_source)
    h.complete(alias=2)
    h.send({"SessionPart": "Q2"}, 110, topic="SessionInfo")
    h.send({"Position": 1}, 150)
    h.send({"Sectors": sectors(EMPTY)}, 90 if origin == "old" else None, delivery=151)
    h.send({"Sectors": sectors(PENDING)}, 152)
    h.complete(4, 220, None, alias=3)
    assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
    expected_time = (NOW + timedelta(seconds=220)).timestamp() if utc_source else 220.0
    if origin == "unknown" and not utc_source:
        # General reducer time retains its existing receive-time fallback;
        # provenance must not misinterpret that accumulated time as origin.
        expected_time = (NOW + timedelta(seconds=151)).timestamp()
    assert h.owner() == ("Q2", "31", 4, expected_time)
    assert h.projection() is None


@pytest.mark.parametrize("retract", [False, True])
def test_proven_historical_enrichment_survives_retirement_without_new_owner(retract):
    h = Ingestion()
    h.complete(values=(20, 30), alias=2)
    if retract:
        h.send({"NumberOfLaps": 3, "LastLapTime": {"Value": ""},
                "BestLapTime": {"Value": ""}}, 101)
    else:
        h.send({"SessionPart": "Q2"}, 110, topic="SessionInfo")
    h.complete(4, 220, B, alias=3)
    second_key = h.owner()
    h.send({"Sectors": {"2": {"Value": "40"}}}, 100, delivery=230)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[1].sectors_s == B
    assert h.owner() == second_key
    assert h.projection(24) == pytest.approx(86.4)


@pytest.mark.parametrize("reset", [False, True])
def test_provenance_lifetime_is_one_store_session(reset):
    h = Ingestion()
    h.complete(alias=2)
    h.send({"Sectors": sectors(EMPTY)}, 110)
    h.send({"Sectors": sectors(PENDING)}, 150)
    if reset:
        h.store.reset()
        h.service.reset()
        h.send({"InPit": False, "NumberOfLaps": 1}, 0, number="1")
    else:
        h = Ingestion()
    h.complete(alias=2)
    assert len(h.raw.completed_lap_sequence) == 1
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.owner() == ("Q1", "31", 3, 100.0)
    assert h.projection() == pytest.approx(112.5)


def test_sole_equal_time_after_best_clear_is_not_positive_first_association():
    h = Ingestion()
    h.complete(alias=None)
    h.send({"BestLapTime": {"Value": ""}}, 101)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 151)
    assert h.owner() is None
    assert h.raw.completed_lap_sequence[0].official_lap_number is None
    assert h.projection(source=152) is None
    # An original publication coordinate supplies the missing positive link.
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 100, delivery=153)
    assert h.owner() == ("Q1", "31", 3, 100.0)
    assert h.projection() == pytest.approx(112.5)


def test_carried_best_without_owner_is_not_identified_by_a_later_boundary_alone():
    h = Ingestion()
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 99)
    h.complete(best=False)
    assert h.owner() is None
    assert h.raw.completed_lap_sequence[0].official_lap_number is None
    assert h.projection(source=101) is None
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 100, delivery=102)
    assert h.owner() == ("Q1", "31", 3, 100.0)
    assert h.projection() == pytest.approx(112.5)


def test_actual_publication_ledger_hit_does_not_bypass_rejected_boundary():
    h = Ingestion()
    h.complete(values=(20, 30), alias=2)
    h.send({"Sectors": {"2": {"Value": "40"}}}, 200)
    # Several updates can share a publication coordinate. Supplied ingestion
    # order still imposes lifecycle obligations on the explicit boundary.
    h.send({"Sectors": sectors(EMPTY)}, 200, delivery=210)
    h.send({"Sectors": sectors(PENDING)}, 200, delivery=250)
    h.send({"NumberOfLaps": 4, "LastLapTime": {"Value": ""},
            "Sectors": {"2": {"Value": "40"}}}, 200, delivery=300)
    h.complete(5, 400, None, alias=4)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
    assert h.projection() is None


@pytest.mark.parametrize("retract", [False, True])
def test_original_best_publication_can_identify_history_but_not_revive_invalid_event(retract):
    h = Ingestion()
    h.complete()
    clear = {"BestLapTime": {"Value": ""}}
    if retract:
        clear.update({"NumberOfLaps": 3, "LastLapTime": {"Value": ""}})
    h.send(clear, 101)
    h.complete(4, 200, B, best=False)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 100, delivery=201)
    assert h.raw.completed_lap_sequence[-1].official_lap_number is None
    assert h.owner() == (None if retract else ("Q1", "31", 3, 100.0))
    assert h.projection(24) == (None if retract else pytest.approx(108.0))
