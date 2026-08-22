from woplanner.common.session_info import (
    is_qualifying_session,
    parse_session_identity,
    resolve_session_identity,
)


def test_session_identity_uses_received_event_metadata():
    identity = parse_session_identity(
        {
            "Meeting": {"Name": "British Grand Prix", "Number": 12},
            "Name": "Qualifying",
            "StartDate": "2026-07-04T15:00:00Z",
        }
    )

    assert identity.year == 2026
    assert identity.round_number == 12
    assert identity.event_name == "British Grand Prix"
    assert identity.session_name == "Qualifying"
    assert identity.is_qualifying is True


def test_session_identity_resolves_missing_round_through_fastf1_event():
    calls = []

    def load_event(year, name):
        calls.append((year, name))
        return {"RoundNumber": 7}

    identity = resolve_session_identity(
        {
            "Meeting": {"Name": "Monaco Grand Prix"},
            "Name": "Race",
            "StartDate": "2026-06-07T13:00:00Z",
        },
        event_loader=load_event,
    )

    assert calls == [(2026, "Monaco Grand Prix")]
    assert identity.round_number == 7
    assert identity.is_qualifying is False


def test_qualifying_session_aliases_are_recognised():
    assert is_qualifying_session("Qualifying") is True
    assert is_qualifying_session("Sprint Qualifying") is True
    assert is_qualifying_session("Sprint Shootout") is True
    assert is_qualifying_session("Race") is False
    assert is_qualifying_session("") is None
