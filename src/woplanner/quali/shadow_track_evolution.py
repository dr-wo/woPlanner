"""Deprecated import compatibility for the promoted production policy.

New exports use schema 2; historical saved shadow schema 1 is never reinterpreted.
There is no shadow engine or separate estimator state.
"""
from .track_evolution_policy import (
    TrackEvolutionPolicy as ShadowTrackEvolution,
    POLICY_VERSION, QUALITY_VERSION, THRESHOLDS, quality, positive, tyre_eligible,
)
