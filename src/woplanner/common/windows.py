"""Shared presentation contract for current-event pit-loss windows.

This module only resolves already-authoritative operational inputs into visual
descriptors.  It never estimates pit loss and never persists a derived value.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowDescriptor:
    key: str
    value_s: float
    label: str
    colour: str
    line_style: str
    source: str


WINDOW_STYLES = {
    "pit": ("#5f5f5f", "dash"),
    "sc": ("#f4b400", "dashdot"),
    "vsc": ("#9b59b6", "dot"),
    "sc_vsc": ("#4682b4", "dashdot"),
}


def resolve_effective_windows(
    *,
    green_pit_loss_s: float | None,
    sc_pit_loss_s: float | None,
    vsc_pit_loss_s: float | None,
    source: str = "event-bound UI value",
) -> tuple[WindowDescriptor, ...]:
    """Resolve GREEN, SC and VSC independently from current-event inputs.

    Missing states remain absent, including when another state's value exists.
    Legacy combined evidence is not accepted by this resolver.
    """
    resolved: list[WindowDescriptor] = []
    for key, value, title in (
        ("pit", green_pit_loss_s, "Pit"),
        ("sc", sc_pit_loss_s, "SC"),
        ("vsc", vsc_pit_loss_s, "VSC"),
    ):
        if value is not None:
            resolved.append(_descriptor(key, value, title, source))
    return tuple(resolved)


def _descriptor(key: str, value: float, title: str, source: str) -> WindowDescriptor:
    value = float(value)
    colour, line_style = WINDOW_STYLES[key]
    return WindowDescriptor(
        key=key,
        value_s=value,
        label=f"{title} ±{value:.1f} s",
        colour=colour,
        line_style=line_style,
        source=source,
    )


__all__ = ["WINDOW_STYLES", "WindowDescriptor", "resolve_effective_windows"]
