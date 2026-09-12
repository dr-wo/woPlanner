"""Helpers for interpreting live timing session metadata."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping


from wodata.live.session_identity import (
    SessionIdentity, parse_session_identity, is_qualifying_session,
    is_sprint_qualifying_session, _positive_int,
)

def resolve_session_identity(
    session_info: Mapping[str, object],
    *,
    fallback_year: int | None = None,
    event_loader: Callable[[int, str], object] | None = None,
) -> SessionIdentity:
    identity = parse_session_identity(session_info, fallback_year=fallback_year)
    if identity.round_number is not None or identity.year is None or not identity.event_name:
        return identity
    try:
        if event_loader is None:
            import fastf1

            def load_event(year: int, name: str):
                return fastf1.get_event(year, name, exact_match=True)

            event_loader = load_event
        event = event_loader(identity.year, identity.event_name)
        round_number = _positive_int(event.get("RoundNumber"))
    except (AttributeError, ImportError, KeyError, TypeError, ValueError, OSError):
        return identity
    return replace(identity, round_number=round_number)
