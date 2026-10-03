"""Explainable time-to-track-position mapping for the live track map."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class TrackPosition:
    x: float
    y: float
    progress: float
    elapsed_s: float


class ReferenceLapMapper:
    """Project XY onto one real reference lap and move in its time domain."""

    def __init__(self, points) -> None:
        rows = tuple(points)
        if len(rows) < 3:
            raise ValueError("A reference lap needs at least three points")
        self.x = np.asarray([float(point.x) for point in rows], dtype="float64")
        self.y = np.asarray([float(point.y) for point in rows], dtype="float64")
        elapsed = np.asarray([float(point.elapsed_s) for point in rows], dtype="float64")
        valid = np.isfinite(self.x) & np.isfinite(self.y) & np.isfinite(elapsed)
        self.x = self.x[valid]
        self.y = self.y[valid]
        elapsed = elapsed[valid]
        if len(elapsed) < 3 or np.any(np.diff(elapsed) <= 0.0):
            raise ValueError("Reference lap times must be finite and strictly increasing")
        self.elapsed = elapsed - elapsed[0]
        self.lap_time_s = float(self.elapsed[-1])
        if self.lap_time_s <= 0.0:
            raise ValueError("Reference lap duration must be positive")
        segment = np.hypot(np.diff(self.x), np.diff(self.y))
        cumulative = np.concatenate(([0.0], np.cumsum(segment)))
        if cumulative[-1] <= 0.0:
            raise ValueError("Reference lap length must be positive")
        self.progress = cumulative / cumulative[-1]

    def project(self, x: float, y: float) -> TrackPosition:
        """Return the nearest point on the sampled polyline."""
        px = float(x)
        py = float(y)
        dx = np.diff(self.x)
        dy = np.diff(self.y)
        length_sq = dx * dx + dy * dy
        fraction = np.divide(
            (px - self.x[:-1]) * dx + (py - self.y[:-1]) * dy,
            length_sq,
            out=np.zeros_like(length_sq),
            where=length_sq > 0.0,
        )
        fraction = np.clip(fraction, 0.0, 1.0)
        projected_x = self.x[:-1] + fraction * dx
        projected_y = self.y[:-1] + fraction * dy
        distance_sq = (px - projected_x) ** 2 + (py - projected_y) ** 2
        index = int(np.argmin(distance_sq))
        local = float(fraction[index])
        progress = float(
            self.progress[index]
            + local * (self.progress[index + 1] - self.progress[index])
        )
        elapsed_s = float(
            self.elapsed[index]
            + local * (self.elapsed[index + 1] - self.elapsed[index])
        )
        return TrackPosition(
            float(projected_x[index]), float(projected_y[index]), progress, elapsed_s
        )

    def at_elapsed(self, elapsed_s: float) -> TrackPosition:
        wrapped = float(elapsed_s) % self.lap_time_s
        x = float(np.interp(wrapped, self.elapsed, self.x))
        y = float(np.interp(wrapped, self.elapsed, self.y))
        progress = float(np.interp(wrapped, self.elapsed, self.progress))
        return TrackPosition(x, y, progress, wrapped)

    def offset_from_xy(self, x: float, y: float, seconds: float) -> TrackPosition:
        projected = self.project(x, y)
        return self.at_elapsed(projected.elapsed_s + float(seconds))

    def interval_from_xy(
        self, x: float, y: float, seconds: float, *, samples: int = 81
    ) -> tuple[TrackPosition, ...]:
        seconds = abs(float(seconds))
        if not math.isfinite(seconds):
            return ()
        projected = self.project(x, y)
        return tuple(
            self.at_elapsed(value)
            for value in np.linspace(
                projected.elapsed_s - seconds,
                projected.elapsed_s + seconds,
                max(int(samples), 3),
            )
        )


__all__ = ["ReferenceLapMapper", "TrackPosition"]
