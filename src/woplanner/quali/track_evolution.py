"""Shared qualifying track-evolution diagnostic view model and renderer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from wostrategy.analysis.pre_quali import (
    aggregate_driver_track_progression, dominant_track_compound,
    fit_driver_track_progression,
)
from wostrategy.analysis.traffic import TrafficCoveragePolicy

from wodata.colours import get_team_colour
from .physical_traffic import QUALIFYING_TRAFFIC_POLICY


@dataclass(frozen=True)
class TrackEvolutionSample:
    part: str
    driver: str
    team: str
    lap_number: int
    lap_time_s: float
    global_push_index: int
    compound: str | None
    stint_start_laps: int | None
    fresh: bool | None
    physical_mean_ahead_gap_s: float | None
    ahead_coverage: float = 0.0
    traffic_status: str = "unknown"
    traffic_method: str | None = None
    traffic_target_samples: int = 0
    traffic_ahead_samples: int = 0
    traffic_evaluator_version: str | None = None
    clean_air_at_completion: bool | None = None
    clean_air_gap_threshold_s: float | None = None
    traffic_circuit_length_m: float | None = None
    tyre_install_index: int | None = None
    first_eligible_push_for_installation: bool = False


@dataclass(frozen=True)
class TrackEvolutionSeries:
    driver: str
    team: str
    compound: str
    samples: tuple[TrackEvolutionSample, ...]
    slope_s_per_push: float


@dataclass(frozen=True)
class TrackEvolutionDiagnostic:
    """Per-driver slopes and their support-weighted signed lap-time aggregate.

    Slopes use seconds per global/session-wide field PUSH; negative improves,
    zero is stable, positive worsens. The aggregate is the canonical supported
    live automatic qualifying forecast rate.
    """

    series: tuple[TrackEvolutionSeries, ...]
    clean_air_gap_s: float
    aggregate_slope_s_per_push: float | None
    weighted_slope_std_s_per_push: float | None
    omitted: tuple[str, ...]
    min_ahead_coverage: float = QUALIFYING_TRAFFIC_POLICY.min_ahead_coverage
    observations: tuple[TrackEvolutionSample, ...] = ()

    @property
    def driver_count(self) -> int:
        return len({series.driver for series in self.series})

    @property
    def lap_count(self) -> int:
        return sum(len(series.samples) for series in self.series)


def build_track_evolution_diagnostic(
    samples: tuple[TrackEvolutionSample, ...], *, clean_air_gap_s: float,
    coverage_policy: TrafficCoveragePolicy = QUALIFYING_TRAFFIC_POLICY,
    require_traffic: bool = True,
) -> TrackEvolutionDiagnostic:
    """Fit one first-eligible PUSH per installation across the whole session.

    The canonical policy supplies require_traffic=False. The explicit legacy
    predicate remains available for historical diagnostics, never live admission.
    """
    first_by_install: dict[tuple[str, int], TrackEvolutionSample] = {}
    omitted: set[str] = set()
    for sample in sorted(samples, key=lambda value: value.global_push_index):
        if (sample.fresh is not True
                or (require_traffic and (sample.traffic_status != "ok"
                    or sample.ahead_coverage < coverage_policy.min_ahead_coverage
                    or sample.physical_mean_ahead_gap_s is None
                    or sample.physical_mean_ahead_gap_s <= clean_air_gap_s))
                or sample.compound in {None, "UNKNOWN"}
                or sample.tyre_install_index is None):
            omitted.add(sample.driver)
            continue
        key = (sample.driver, sample.tyre_install_index)
        first_by_install.setdefault(
            key, replace(sample, first_eligible_push_for_installation=True)
        )
    observations = tuple(sorted(first_by_install.values(),
                                key=lambda value: value.global_push_index))
    grouped: dict[str, list[TrackEvolutionSample]] = {}
    for sample in observations:
        grouped.setdefault(sample.driver, []).append(sample)
    series = []
    for driver, values in grouped.items():
        compound = dominant_track_compound(value.compound for value in values)
        stable = [value for value in values if value.compound == compound]
        if len(stable) < 2 or len({value.global_push_index for value in stable}) < 2:
            omitted.add(driver)
            continue
        try:
            slope = fit_driver_track_progression(
                (value.global_push_index for value in stable),
                (value.lap_time_s for value in stable),
            )
        except (ValueError, np.linalg.LinAlgError):
            omitted.add(driver)
            continue
        series.append(TrackEvolutionSeries(
            driver, stable[0].team, compound, tuple(stable), slope,
        ))
    series.sort(key=lambda value: value.driver)
    aggregate = (
        aggregate_driver_track_progression(
            (value.slope_s_per_push, len(value.samples)) for value in series
        )
        if series else None
    )
    # Population dispersion of driver slopes, using the same fit-support
    # weights as the aggregate. One supported driver has zero disagreement.
    slope_std = (
        float(np.sqrt(sum(len(value.samples) * (value.slope_s_per_push - aggregate) ** 2
                          for value in series) / sum(len(value.samples) for value in series)))
        if aggregate is not None else None
    )
    used = {value.driver for value in series}
    return TrackEvolutionDiagnostic(
        tuple(series), clean_air_gap_s, aggregate, slope_std,
        tuple(sorted(omitted - used)), coverage_policy.min_ahead_coverage,
        observations,
    )


def render_track_evolution_diagnostic(diagnostic: TrackEvolutionDiagnostic, path: Path | None = None) -> bytes:
    """Render the exact diagnostic view model used by the Qt Track Correction view."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from io import BytesIO

    figure = Figure(figsize=(12, 6), dpi=120)
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    markers = ("o", "s")
    team_counts: dict[str, int] = {}
    for series in diagnostic.series:
        ordinal = team_counts.get(series.team, 0)
        team_counts[series.team] = ordinal + 1
        colour = get_team_colour(series.team) or "#666666"
        x = np.array([sample.global_push_index for sample in series.samples])
        y = np.array([sample.lap_time_s for sample in series.samples])
        axis.scatter(x, y, color=colour, marker=markers[ordinal % len(markers)], s=52, zorder=3)
        line_x = np.array([x.min(), x.max()])
        axis.plot(line_x, series.slope_s_per_push * line_x + np.mean(y - series.slope_s_per_push * x),
                  color=colour, linestyle="-" if ordinal % 2 == 0 else "--",
                  label=f"{series.driver} {series.slope_s_per_push:+.4f} s/push")
    aggregate = "—" if diagnostic.aggregate_slope_s_per_push is None else f"{diagnostic.aggregate_slope_s_per_push:+.4f} s/push"
    slope_std = "—" if diagnostic.weighted_slope_std_s_per_push is None else f"{diagnostic.weighted_slope_std_s_per_push:.4f} s/push"
    axis.set_title(f"Track Evolution — {diagnostic.driver_count} drivers / {diagnostic.lap_count} laps used\n"
                   f"Aggregate {aggregate} · driver spread σ {slope_std}; traffic is auxiliary")
    axis.set_xlabel("Global/session-wide PUSH-lap index")
    axis.set_ylabel("Completed FAST/PUSH lap time (s)")
    axis.grid(alpha=.25)
    if diagnostic.series:
        axis.legend(loc="upper center", bbox_to_anchor=(.5, -.14), ncol=3, frameon=False)
    figure.tight_layout()
    output = BytesIO()
    figure.savefig(output, format="png", bbox_inches="tight")
    rendered = output.getvalue()
    if path is not None:
        path.write_bytes(rendered)
    return rendered
