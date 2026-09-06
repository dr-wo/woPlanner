import os
import json
from concurrent.futures import Future
from datetime import datetime, timezone

from types import MappingProxyType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6 import QtCore, QtWidgets

from wodata.artifacts import write_model_snapshot, write_weekend_model_config
from wodata import get_race_performance_review_root
from wodata.contracts import ModelSnapshot, ParameterEstimate, StrategySearchState
from wodata.live import (
    CompletedLap,
    CompletedSector,
    LiveDriver,
    LiveSessionSnapshot,
    LiveTelemetrySample,
)
from woplanner.analysis.service import (
    LiveRecalculationResult,
    ModelSummary,
    OfflineAnalysisService,
    _live_analysis_laps,
    _live_traffic_gaps,
    _resolve_scheduled_total_laps,
)
from woplanner.analysis.gui import (
    StrategyPredictionWindow, format_history_hover_text,
    tyre_history_source_segments,
)
from woplanner.analysis.worker import (
    AnalysisQueuePolicy,
    ModelSnapshot as PublishedModelSnapshot,
)
from wostrategy.core.race_performance_settings import (
    write_resolved_race_performance_settings,
)


def model_snapshot():
    created = datetime(2026, 7, 30, tzinfo=timezone.utc)
    rows = []
    for parameter, compound, median in (
        ("degradation", "MEDIUM", 0.08),
        ("degradation", "HARD", 0.04),
        ("compound_delta", "MEDIUM", -0.3),
        ("compound_delta", "HARD", 0.0),
    ):
        rows.append(
            ParameterEstimate(
                "a1", "b1", "aggregate", 0, parameter, "FP2", None, compound,
                "HARD", median - 0.01, median, median + 0.01,
                "s/lap" if parameter == "degradation" else "s", "measured",
                20, 4, 100, 0.1, 0, 0, "input", "config", created,
            )
        )
    return ModelSnapshot("a1", "b1", "aggregate", 0, tuple(rows), "input", "config", created)


def test_live_model_config_uses_compatible_race_review_settings(tmp_path):
    write_weekend_model_config(
        {
            "sample_count": 100_000,
            "random_seed": 7,
            "fuel_rate_bounds": [0.0, 0.08],
            "track_rate_bounds": [-0.03, 0.03],
            "default_degradation_bounds": [0.0, 0.2],
            "default_compound_delta_bounds": [-1.0, 1.0],
        },
        year=2026,
        round_number=11,
        data_root=tmp_path,
    )
    write_resolved_race_performance_settings(
        {
            "sample_count": 80_000,
            "default_compound_degradation_bounds": [0.0, 0.5],
            "compound_delta_reference": "HARD",
            "team_variation_fraction": 0.5,
        },
        output_root=get_race_performance_review_root(tmp_path),
        year=2026,
        races=[11],
        session="R",
    )

    config = OfflineAnalysisService(tmp_path).live_model_config(2026, 11)

    assert config["sample_count"] == 80_000
    assert config["default_degradation_bounds"] == [0.0, 0.5]
    assert config["reference_compound"] == "HARD"
    assert config["random_seed"] == 7
    assert config["settings_source"] == "event_latest_invocation"
    assert "team_variation_fraction" not in config


def test_scheduled_total_laps_resolution_prefers_live_then_manual_then_fallback():
    snapshot = LiveSessionSnapshot.empty()
    assert _resolve_scheduled_total_laps(
        snapshot, manual_override=57, current_leader_lap=9
    ) == (57, "manual_override")
    snapshot = LiveSessionSnapshot(
        snapshot.session_info, snapshot.session_status, snapshot.drivers,
        snapshot.completed_laps, snapshot.latest_position_by_driver,
        snapshot.last_message_at_utc, scheduled_total_laps=70,
    )
    assert _resolve_scheduled_total_laps(
        snapshot, manual_override=57, current_leader_lap=9
    ) == (70, "live_lap_count")
    assert _resolve_scheduled_total_laps(
        LiveSessionSnapshot.empty(), manual_override=None, current_leader_lap=9
    ) == (9, "current_leader_lap_fallback")


def snapshot_with_clear_sector_traffic(laps):
    sectors = []
    for lap in laps:
        finish = float(lap.session_time_seconds or lap.lap_number * 100.0)
        for number, code, offset, position in (
            ("98", "AHEAD", -5.0, 1),
            (lap.driver_number, lap.driver_code, 0.0, 2),
            ("99", "BEHIND", 5.0, 3),
        ):
            for index, sector in enumerate(("S1", "S2", "S3"), start=1):
                sectors.append(
                    CompletedSector(
                        number, code, "Team", lap.lap_number, sector,
                        lap.lap_number * 3 - 3 + index, 30.0, 30.0 * index,
                        position, None, session_time_seconds=finish + offset,
                    )
                )
    return LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), tuple(laps),
        MappingProxyType({"98": 1, "1": 2, "99": 3}), None, tuple(sectors),
    )


def write_test_pre_race_prior(data_root, year=2026, round_number=5):
    path = (
        data_root / "wostrategy/weekend_model/schema_v1"
        / f"year={year}" / f"round={round_number}" / "pre_race_prior_v1.json"
    )
    marginal = lambda p10, median, p90, unit: {
        "p10": p10, "median": median, "p90": p90, "unit": unit,
        "approximation": "split_normal_from_marginal_p10_median_p90",
    }
    payload = {
        "schema_version": 1, "year": year, "round_number": round_number,
        "source_analysis_id": "test-pre-race", "source_sessions": ["FP2"],
        "source_created_at": "2026-01-01T00:00:00Z",
        "frozen_at": "2026-01-02T00:00:00Z",
        "construction": "marginal_split_normal_approximation_no_joint_covariance",
        "fuel": marginal(0.01, 0.03, 0.05, "s/lap"),
        "degradation": {
            "HARD": marginal(0.01, 0.04, 0.08, "s/lap"),
            "MEDIUM": marginal(0.04, 0.08, 0.12, "s/lap"),
            "SOFT": marginal(0.10, 0.15, 0.20, "s/lap"),
        },
        "performance": {
            "HARD": marginal(0.2, 0.4, 0.6, "s"),
            "MEDIUM": marginal(-0.1, 0.0, 0.1, "s"),
            "SOFT": marginal(-0.7, -0.5, -0.3, "s"),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_history_hover_readings_use_hovered_compound_as_pace_reference():
    rows = [
        {
            "as_of_leader_lap": 12,
            "source_scope": "aggregate",
            "parameter": parameter,
            "compound": compound,
            "median": value,
        }
        for parameter, compound, value in (
            ("degradation", "SOFT", 0.18),
            ("degradation", "MEDIUM", 0.10),
            ("degradation", "HARD", 0.06),
            ("compound_delta", "SOFT", -0.70),
            ("compound_delta", "MEDIUM", -0.35),
            ("compound_delta", "HARD", 0.0),
        )
    ]

    text = format_history_hover_text(
        rows, leader_lap=12, reference_compound="MEDIUM"
    )

    assert "pace reference: MEDIUM" in text
    assert "SOFT: degradation 0.180 s/lap | pace Δ -0.350 s" in text
    assert "MEDIUM: degradation 0.100 s/lap | pace Δ +0.000 s" in text
    assert "HARD: degradation 0.060 s/lap | pace Δ +0.350 s" in text


def test_strategy_assumptions_derive_uninformed_degradation_from_fixed_ratios():
    service = OfflineAnalysisService()

    def summary(statuses, degradation):
        rows = []
        for compound, value in degradation.items():
            rows.append({
                "parameter": "degradation", "compound": compound,
                "median": value, "usable_lap_count": 10, "session": "R",
                "support_status": statuses.get(compound, "prior_only"),
            })
            rows.append({
                "parameter": "compound_delta", "compound": compound,
                "median": 0.0, "usable_lap_count": 10, "session": "R",
                "support_status": "prior_only",
            })
        return ModelSummary({"analysis_id": "race"}, tuple(rows))

    baseline = {"HARD": .10, "MEDIUM": .20, "SOFT": .30}
    one = service.strategy_assumptions(
        summary(
            {"MEDIUM": "race_informed"},
            {"HARD": .50, "MEDIUM": .18, "SOFT": .60},
        ),
        baseline_degradation=baseline,
        baseline_compound_delta={"HARD": .4, "MEDIUM": 0.0, "SOFT": -.4},
    )
    one = {row["compound"]: row for row in one}
    assert one["MEDIUM"]["degradation"] == .18
    assert one["HARD"]["degradation"] == pytest.approx(.09)
    assert one["SOFT"]["degradation"] == pytest.approx(.27)
    assert one["MEDIUM"]["degradation_source"] == "live_direct"
    assert one["HARD"]["degradation_source"] == "live_derived"
    assert one["SOFT"]["degradation_anchor_compound"] == "MEDIUM"

    two = service.strategy_assumptions(
        summary(
            {"HARD": "race_informed", "MEDIUM": "race_informed"},
            {"HARD": .11, "MEDIUM": .19, "SOFT": .60},
        ),
        baseline_degradation=baseline,
        baseline_compound_delta={"HARD": .4, "MEDIUM": 0.0, "SOFT": -.4},
    )
    two = {row["compound"]: row for row in two}
    assert two["HARD"]["degradation"] == pytest.approx(.11)
    assert two["MEDIUM"]["degradation"] == pytest.approx(.19)
    assert two["SOFT"]["degradation"] == pytest.approx(.285)
    assert two["SOFT"]["degradation_source"] == "live_derived"
    assert two["SOFT"]["degradation_anchor_compound"] == "MEDIUM"


def test_hard_direct_scales_uninformed_compounds_from_initial_relationship():
    rows = []
    for compound, value in (("HARD", .20), ("MEDIUM", .90), ("SOFT", .90)):
        rows.extend((
            {"parameter": "degradation", "compound": compound, "median": value,
             "usable_lap_count": 10, "session": "R",
             "support_status": "race_informed" if compound == "HARD" else "prior_only"},
            {"parameter": "compound_delta", "compound": compound, "median": 0.0,
             "usable_lap_count": 10, "session": "R", "support_status": "prior_only"},
        ))
    assumptions = OfflineAnalysisService().strategy_assumptions(
        ModelSummary({"analysis_id": "race"}, tuple(rows)),
        baseline_degradation={"HARD": .08, "MEDIUM": .10, "SOFT": .18},
        baseline_compound_delta={"HARD": .4, "MEDIUM": 0.0, "SOFT": -.4},
    )
    by_compound = {row["compound"]: row for row in assumptions}
    assert by_compound["HARD"]["degradation"] == pytest.approx(.20)
    assert by_compound["MEDIUM"]["degradation"] == pytest.approx(.25)
    assert by_compound["SOFT"]["degradation"] == pytest.approx(.45)
    assert by_compound["HARD"]["degradation_source"] == "live_direct"
    assert by_compound["MEDIUM"]["degradation_source"] == "live_derived"
    assert by_compound["SOFT"]["degradation_source"] == "live_derived"


def test_live_window_updates_strategy_and_history_with_fixed_degradation_ratios(tmp_path):
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs.save_manual_inputs(
        2026, 5,
        {
            "Deg_HARD": .08, "Deg_MEDIUM": .10, "Deg_SOFT": .18,
            "Perf_HARD_vs_MEDIUM": .4, "Perf_SOFT_vs_MEDIUM": -.5,
        },
    )
    write_model_snapshot(
        model_snapshot(), year=2026, round_number=5, data_root=tmp_path
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=service, auto_save_history=False
    )

    def live_summary(lap, observed, degradation):
        rows = []
        for compound in ("HARD", "MEDIUM", "SOFT"):
            rows.extend((
                {"parameter": "degradation", "compound": compound,
                 "median": degradation[compound], "usable_lap_count": 12,
                 "p10": degradation[compound] - .01,
                 "p90": degradation[compound] + .01,
                 "support_status": "measured", "session": "R"},
                {"parameter": "compound_delta", "compound": compound,
                 "median": {"HARD": .4, "MEDIUM": 0.0, "SOFT": -.5}[compound],
                 "p10": {"HARD": .39, "MEDIUM": -.01, "SOFT": -.51}[compound],
                 "p90": {"HARD": .41, "MEDIUM": .01, "SOFT": -.49}[compound],
                 "usable_lap_count": 12, "support_status": "measured", "session": "R"},
            ))
        return ModelSummary(
            {"analysis_id": f"live-{lap}", "as_of_leader_lap": lap,
             "sampler_quality": {"architecture": "live-retro-v1",
                                 "observed_compounds": list(observed), "ess": 100}},
            tuple(rows),
        )

    window._load_model(
        summary=live_summary(30, (), {"HARD": .9, "MEDIUM": .9, "SOFT": .9}),
        record_history=True,
    )
    assert window._manual_overrides[("degradation", "HARD")] == pytest.approx(.08)
    assert window._manual_overrides[("degradation", "MEDIUM")] == pytest.approx(.10)
    assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(.18)

    window._load_model(
        summary=live_summary(31, ("HARD",),
                             {"HARD": .20, "MEDIUM": .9, "SOFT": .9}),
        record_history=True,
    )
    assert window._manual_overrides[("degradation", "HARD")] == pytest.approx(.20)
    assert window._manual_overrides[("degradation", "MEDIUM")] == pytest.approx(.25)
    assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(.45)

    # Changing the resolved base later must not redefine the session's ratios.
    service.tyre_inputs.save_manual_inputs(
        2026, 5,
        {
            "Deg_HARD": .10, "Deg_MEDIUM": .20, "Deg_SOFT": .30,
            "Perf_HARD_vs_MEDIUM": .4, "Perf_SOFT_vs_MEDIUM": -.5,
        },
    )
    window._load_model(
        summary=live_summary(64, ("HARD", "MEDIUM"),
                             {"HARD": .21, "MEDIUM": .24, "SOFT": .9}),
        record_history=True,
    )
    assert window._manual_overrides[("degradation", "HARD")] == pytest.approx(.21)
    assert window._manual_overrides[("degradation", "MEDIUM")] == pytest.approx(.24)
    assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(.432)

    window._load_model(
        summary=live_summary(65, ("HARD", "MEDIUM", "SOFT"),
                             {"HARD": .22, "MEDIUM": .23, "SOFT": .39}),
        record_history=True,
    )
    assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(.39)
    sources = {
        (int(row["as_of_leader_lap"]), str(row["compound"])):
        row["degradation_source"]
        for row in window._working_plot_rows
        if row["parameter"] == "degradation"
    }
    assert {sources[(30, compound)] for compound in ("HARD", "MEDIUM", "SOFT")} == {"base"}
    assert sources[(31, "HARD")] == "live_direct"
    assert sources[(31, "MEDIUM")] == "live_derived"
    assert sources[(31, "SOFT")] == "live_derived"
    assert sources[(64, "MEDIUM")] == "live_direct"
    assert sources[(64, "SOFT")] == "live_derived"
    assert {sources[(65, compound)] for compound in ("HARD", "MEDIUM", "SOFT")} == {"live_direct"}
    plotted = {
        item.name(): item for item in window.degradation_history_plot.listDataItems()
    }
    assert plotted["SOFT — base"].opts["pen"].style() == QtCore.Qt.PenStyle.DashLine
    assert plotted["SOFT — base"].opts["symbol"] is None
    derived = plotted["SOFT — live-derived"]
    assert derived.opts["pen"].style() == QtCore.Qt.PenStyle.DashLine
    assert derived.opts["symbol"] == "o"
    assert derived.opts["symbolBrush"] is None
    direct = plotted["SOFT — live-direct"]
    assert direct.opts["pen"].style() == QtCore.Qt.PenStyle.SolidLine
    assert direct.opts["symbol"] == "o"
    assert direct.opts["symbolBrush"] is not None
    window.close()
    app.processEvents()


def test_history_hover_clear_hides_label_cursor_and_selection(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )
    plot = window.history_plot
    label = __import__("pyqtgraph").TextItem("hover")
    plot.addItem(label)
    window._history_hover_labels[plot] = label
    window._hovered_history_lap = 4
    for cursor in window._history_cursor_lines.values():
        cursor.show()

    window._clear_history_hover()

    assert all(value is None for value in window._history_hover_labels.values())
    assert all(not cursor.isVisible() for cursor in window._history_cursor_lines.values())
    assert window._hovered_history_lap is None
    window.close()
    app.processEvents()


def test_history_click_retains_selected_lap_when_menu_clears_hover(
    tmp_path, monkeypatch
):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )
    plot = window.history_plot
    applied = []

    class MenuThatClearsHover:
        def __init__(self, parent):
            self.action = object()

        def addAction(self, text):
            assert "lap 9" in text
            return self.action

        def exec(self, position):
            window._clear_history_hover()
            return self.action

    class ClickEvent:
        @staticmethod
        def button():
            return QtCore.Qt.MouseButton.LeftButton

        @staticmethod
        def scenePos():
            return plot.plotItem.vb.sceneBoundingRect().center()

        @staticmethod
        def screenPos():
            return QtCore.QPointF(0, 0)

    monkeypatch.setattr(QtWidgets, "QMenu", MenuThatClearsHover)
    monkeypatch.setattr(window, "_history_mouse_moved", lambda *_: setattr(
        window, "_hovered_history_lap", 9
    ))
    monkeypatch.setattr(window, "_apply_history_lap_to_strategy", applied.append)

    window._history_mouse_clicked(plot, ClickEvent())

    assert applied == [9]
    assert window._hovered_history_lap is None
    window.close()
    app.processEvents()


def test_apply_history_lap_rejects_missing_selection(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )

    assert window._apply_history_lap_to_strategy(None) is False

    window.close()
    app.processEvents()


def test_service_loads_canonical_model_generates_and_persists_top_ten(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    service = OfflineAnalysisService(tmp_path)
    state = StrategySearchState(10, 30, "MEDIUM", 6, selected_driver="44", max_future_stops=2)
    results = service.generate_continuations(
        year=2026, round_number=5, state=state, pit_loss=18.0, k=10
    )
    saved = service.load_saved_strategies(2026, 5, "44")
    assert len(results) == 7
    assert len(saved) == 7
    assert len({result.plan.compounds for result in results}) == len(results)
    assert saved[0]["model_analysis_id"] == "a1"


def test_service_rules_compliant_option_requires_two_dry_compounds(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    service = OfflineAnalysisService(tmp_path)
    state = StrategySearchState(0, 12, "MEDIUM", 0, selected_driver=None, max_future_stops=2)

    results = service.generate_continuations(
        year=2026, round_number=5, state=state, pit_loss=2.0, k=10,
        rules_compliant=True, persist=False,
    )

    assert results
    assert all(len(set(result.plan.compounds)) >= 2 for result in results)


def test_service_enforces_three_pinned_comparisons():
    service = OfflineAnalysisService()
    for value in ("a", "b", "c"):
        service.pin(value)
    try:
        service.pin("d")
    except ValueError as exc:
        assert "maximum of three" in str(exc)
    else:
        raise AssertionError("Expected pin limit")


def test_live_clean_lap_definition_supports_stint_edge_trimming():
    laps = tuple(
        CompletedLap(
            "1", "AAA", "Team", lap, 90.0 + lap * 0.01, 90.0 * lap,
            1, None, "MEDIUM", float(lap), 1, True,
        )
        for lap in range(1, 7)
    )
    snapshot = snapshot_with_clear_sector_traffic(laps)
    selected = _live_analysis_laps(
        snapshot,
        quick_lap_threshold=1.1,
        min_clean_laps=4,
        allowed_compounds=("MEDIUM",),
        exclude_stint_start_laps=1,
        exclude_stint_end_laps=1,
    )
    assert selected["LapNumber"].tolist() == [2, 3, 4, 5]


def test_live_clean_lap_sector_boundary_filter_requires_clear_ahead_and_behind():
    finish_session_time = 1_000.0
    completed_laps = []
    completed_sectors = []
    for number, code, offset, position in (
        ("1", "AHEAD", -5.0, 1),
        ("2", "TARGET", 0.0, 2),
        ("3", "BEHIND", 3.0, 3),
    ):
        completed_laps.append(
            CompletedLap(
                number, code, "Team", 1, 90.0, 90.0, position,
                None, "MEDIUM", 1.0, 1, True, finish_session_time + offset,
            )
        )
        for index, sector in enumerate(("S1", "S2", "S3"), start=1):
            completed_sectors.append(
                CompletedSector(
                    number, code, "Team", 1, sector, index, 30.0,
                    30.0 * index, position, None,
                    "MEDIUM", 1.0, 1, True, finish_session_time + offset,
                )
            )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), tuple(completed_laps),
        MappingProxyType({"1": 1, "2": 2, "3": 3}), None,
        tuple(completed_sectors),
    )

    automatic = _live_traffic_gaps(snapshot)
    target_traffic = automatic.loc[automatic["Driver"].eq("TARGET")].iloc[0]
    assert target_traffic["TrafficMethod"] == "SECTOR_BOUNDARY_FALLBACK"
    assert target_traffic["TrafficStatus"] == "sector_fallback"
    assert target_traffic["DirectTrafficStatus"] == "unknown"
    assert target_traffic["PhysicalTrafficStatus"] == "unknown"
    assert pd.isna(target_traffic["MeanTimeDeltaToDriverAhead"])

    selected = _live_analysis_laps(
        snapshot,
        quick_lap_threshold=1.1,
        min_clean_laps=1,
        use_sector_boundary_traffic=True,
        min_gap_ahead_seconds=4.0,
        min_gap_behind_seconds=2.0,
    )
    assert selected["Driver"].tolist() == ["TARGET"]
    assert selected["MinGapAheadSeconds"].tolist() == [5.0]
    assert selected["MinGapBehindSeconds"].tolist() == [3.0]

    blocked = _live_analysis_laps(
        snapshot,
        quick_lap_threshold=1.1,
        min_clean_laps=1,
        use_sector_boundary_traffic=True,
        min_gap_ahead_seconds=6.0,
        min_gap_behind_seconds=2.0,
    )
    assert blocked.empty


def test_missing_live_telemetry_and_sector_data_is_explicitly_unknown():
    lap = CompletedLap(
        "1", "AAA", "Team", 1, 90.0, 90.0, 1, None,
        "MEDIUM", 1.0, 1, True,
    )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), (lap,), MappingProxyType({}), None
    )
    traffic = _live_traffic_gaps(snapshot)
    assert traffic["TrafficStatus"].tolist() == ["unknown"]
    assert traffic["CoverageFraction"].tolist() == [0.0]
    assert "fallback unavailable" in traffic["TrafficStatusReason"].iloc[0]


def test_live_traffic_uses_shared_circular_physical_evaluator():
    speed = 180.0
    samples = []
    for number, lap, start in (("1", 10, -10.0), ("2", 8, -12.0), ("3", 12, -9.0)):
        for index in range(41):
            time = start + index * 0.5
            samples.append(LiveTelemetrySample(number, time, lap, speed))
    drivers = (
        LiveDriver("1", "TARGET", "Team", "Target", 1, None),
        LiveDriver("2", "LAPPED_AHEAD", "Team", "Ahead", 2, None),
        LiveDriver("3", "BEHIND", "Team", "Behind", 3, None),
    )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", drivers, (), MappingProxyType({}), None,
        telemetry_samples=tuple(samples),
    )

    traffic = _live_traffic_gaps(snapshot)
    target = traffic.loc[traffic["Driver"].eq("TARGET")].iloc[0]
    assert target["TrafficMethod"] == "PHYSICAL_CIRCULAR"
    assert target["TrafficStatus"] == "ok"
    assert target["PhysicalMeanTimeDeltaToDriverAhead"] > 0.0
    assert target["PhysicalMeanTimeDeltaToDriverBehind"] > 0.0
    assert target["DirectMeanTimeDeltaToDriverAhead"] != target["DirectMeanTimeDeltaToDriverAhead"]


def test_live_traffic_exposes_shared_completed_lap_direct_component():
    samples = tuple(
        LiveTelemetrySample(number, start + index * 0.5, 5, 180.0)
        for number, start in (("1", -10.0), ("2", -12.0), ("3", -8.5))
        for index in range(41)
    )
    drivers = (
        LiveDriver("1", "TARGET", "Team", "Target", 1, None),
        LiveDriver("2", "AHEAD", "Team", "Ahead", 2, None),
        LiveDriver("3", "BEHIND", "Team", "Behind", 3, None),
    )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", drivers, (), MappingProxyType({}), None,
        telemetry_samples=samples,
    )

    traffic = _live_traffic_gaps(snapshot)
    target = traffic.loc[traffic["Driver"].eq("TARGET")].iloc[0]

    assert target["DirectTrafficStatus"] == "ok"
    assert target["DirectMeanDistanceToDriverAhead"] == 100.0
    assert target["DirectMeanTimeDeltaToDriverAhead"] == 2.0
    assert target["TrafficMethod"] == "compatibility_min"


def test_finalized_live_traffic_is_identical_for_batch_and_replay_sample_order():
    samples = tuple(
        LiveTelemetrySample(number, start + index * 0.5, lap, 180.0)
        for number, lap, start in (("1", 5, -10.0), ("2", 3, -11.5))
        for index in range(41)
    )
    drivers = (
        LiveDriver("1", "TARGET", "Team", "Target", 1, None),
        LiveDriver("2", "OTHER", "Team", "Other", 2, None),
    )

    def snapshot(telemetry_samples):
        return LiveSessionSnapshot(
            MappingProxyType({}),
            "Started",
            drivers,
            (),
            MappingProxyType({}),
            None,
            telemetry_samples=telemetry_samples,
        )

    batch = _live_traffic_gaps(snapshot(samples)).sort_values(["Driver", "LapNumber"])
    replay = _live_traffic_gaps(snapshot(tuple(reversed(samples)))).sort_values(
        ["Driver", "LapNumber"]
    )
    columns = [
        "Driver",
        "LapNumber",
        "PhysicalMeanTimeDeltaToDriverAhead",
        "PhysicalMeanTimeDeltaToDriverBehind",
        "DirectMeanTimeDeltaToDriverAhead",
        "DirectMeanTimeDeltaToDriverBehind",
        "MeanTimeDeltaToDriverAhead",
        "MeanTimeDeltaToDriverBehind",
        "TrafficMethod",
        "TrafficStatus",
        "DirectTrafficStatus",
        "CoverageFraction",
    ]
    pd.testing.assert_frame_equal(
        batch.loc[:, columns].reset_index(drop=True),
        replay.loc[:, columns].reset_index(drop=True),
    )


def test_service_loads_canonical_and_race_review_tyre_information(tmp_path):
    canonical = tmp_path / "latest_parameters.csv"
    canonical.write_text(
        "parameter,compound,median,usable_lap_count,reference_compound\n"
        "degradation,SOFT,0.20,10,HARD\n"
        "degradation,HARD,0.05,10,HARD\n"
        "compound_delta,SOFT,-0.80,10,HARD\n"
        "compound_delta,HARD,0.00,10,HARD\n",
        encoding="utf-8",
    )
    service = OfflineAnalysisService(tmp_path)
    rows, reference = service.load_tyre_information_files([canonical])
    by_compound = {row["compound"]: row for row in rows}
    assert reference == "HARD"
    assert by_compound["SOFT"]["baseline_pace"] == 0.0
    assert by_compound["HARD"]["baseline_pace"] == 0.8
    assert by_compound["SOFT"]["degradation"] == 0.2

    degradation = tmp_path / "race_performance_2026_11_R_summary_compound_degradation.csv"
    delta = tmp_path / "race_performance_2026_11_R_summary_compound_delta.csv"
    degradation.write_text(
        "Compound,Median\nMEDIUM,0.10\nHARD,0.04\n", encoding="utf-8"
    )
    delta.write_text(
        "Compound,CompoundDeltaReference,Median\nMEDIUM,HARD,-0.30\nHARD,HARD,0.00\n",
        encoding="utf-8",
    )
    rows, reference = service.load_tyre_information_files([degradation, delta])
    by_compound = {row["compound"]: row for row in rows}
    assert reference == "HARD"
    assert by_compound["MEDIUM"]["degradation"] == 0.1
    assert by_compound["HARD"]["baseline_pace"] == 0.3

    combined = tmp_path / "tyre_information.csv"
    combined.write_text(
        "Scope,Team,Compound,ReferenceCompound,"
        "CompoundDeltaMedianSeconds,DegradationMedianSecondsPerLap,"
        "DeltaSampleCount,DegradationSampleCount\n"
        "global,,SOFT,HARD,-0.7,0.2,100,100\n"
        "global,,HARD,HARD,0.0,0.05,100,100\n"
        "team,Mercedes,SOFT,HARD,-0.7,0.21,100,100\n",
        encoding="utf-8",
    )
    rows, reference = service.load_tyre_information_files([combined])
    by_compound = {row["compound"]: row for row in rows}
    assert reference == "HARD"
    assert by_compound["SOFT"]["degradation"] == 0.2
    assert by_compound["HARD"]["baseline_pace"] == 0.7


def test_strategy_model_uses_usable_lap_weighted_session_mean(tmp_path):
    created = datetime(2026, 7, 30, tzinfo=timezone.utc)
    rows = []
    for session, degradation, usable_laps in (
        ("FP1", 0.12, 10),
        ("FP2", 0.06, 30),
    ):
        for parameter, median in (("degradation", degradation), ("compound_delta", 0.0)):
            rows.append(
                ParameterEstimate(
                    "joint", "bank", "aggregate", 0, parameter, session, None, "HARD",
                    "HARD", median, median, median, "s/lap", "measured", usable_laps, 2,
                    50, 0.1, 0, 0, session, "config", created,
                )
            )
    snapshot = ModelSnapshot(
        "joint", "bank", "aggregate", 0, tuple(rows), "input", "config", created,
        contributing_sessions=("FP1", "FP2"),
    )
    write_model_snapshot(snapshot, year=2026, round_number=9, data_root=tmp_path)
    service = OfflineAnalysisService(tmp_path)
    summary = service.load_model(2026, 9)
    model = service.build_strategy_model(summary)
    assert model.degradation["HARD"] == 0.075
    assumptions = service.strategy_assumptions(summary)
    assert assumptions[0]["sessions"] == "FP1+FP2"
    assert assumptions[0]["aggregation"] == "usable-lap weighted mean"


def test_live_recalculation_runs_once_for_new_clean_laps(tmp_path):
    write_test_pre_race_prior(tmp_path)
    write_weekend_model_config(
        {
            "sample_count": 100,
            "random_seed": 42,
            "fuel_rate_bounds": [0.0, 0.1],
            "track_rate_bounds": [-0.05, 0.05],
            "default_degradation_bounds": [0.0, 0.2],
            "default_compound_delta_bounds": [-1.0, 1.0],
            "reference_compound": "HARD",
            "clean_lap_noise_sigma": 0.35,
            "expected_sessions": ["R"],
            "expected_compounds": ["MEDIUM", "HARD"],
        },
        year=2026,
        round_number=5,
        data_root=tmp_path,
    )
    laps = tuple(
        CompletedLap(
            "1", "AAA", "Team", lap, 90.0 + lap * 0.1, 90.0 * lap,
            1, None, "MEDIUM", float(lap), 1, True,
        )
        for lap in range(1, 5)
    )
    snapshot = snapshot_with_clear_sector_traffic(laps)
    service = OfflineAnalysisService(tmp_path)
    first = service.recalculate_from_live_snapshot(
        year=2026, round_number=5, snapshot=snapshot, sample_count=100,
        quick_lap_threshold=1.1, min_clean_laps=3,
    )
    second = service.recalculate_from_live_snapshot(
        year=2026, round_number=5, snapshot=snapshot, sample_count=100,
        quick_lap_threshold=1.1, min_clean_laps=3,
    )
    assert first.status == "updated"
    assert first.elapsed_seconds >= 0.0
    assert first.leader_lap == 4
    assert first.previous_clean_lap_count == 0
    assert dict(first.clean_lap_sources)["R"] == 4
    assert dict(first.sampler_quality)["architecture"] == "live-retro-v1"
    assert second.status == "no-new-clean-laps"
    assert second.previous_clean_lap_count == 4


class _FakeAnalysisWorker:
    def __init__(self, policy=AnalysisQueuePolicy.LATEST_WINS):
        self.policy = policy
        self.requests = []
        self.analysis_running = False
        self.running_input_version = None
        self.pending_input_version = None
        self.current_result_version = None
        self.last_error = None
        self.completed = None
        self.completed_version = None

    @property
    def queue_depth(self):
        return max(len(self.requests) - (1 if self.analysis_running else 0), 0)

    @property
    def oldest_queued_version(self):
        return 2 if self.queue_depth else None

    @property
    def newest_queued_version(self):
        return len(self.requests) if self.queue_depth else None

    def submit(self, **request):
        version = max(
            len(self.requests),
            int(self.running_input_version or 0),
            int(self.pending_input_version or 0),
        ) + 1
        self.requests.append(request)
        if self.analysis_running:
            self.pending_input_version = version
        else:
            self.analysis_running = True
            self.running_input_version = version
        return version

    def poll(self):
        completed, self.completed = self.completed, None
        return completed

    def shutdown(self, **_kwargs):
        self.analysis_running = False


class _FakeTelemetryWorker:
    def __init__(self):
        self.capacity_probes = []

    def set_bounded_replay_mc_pipeline_capacity_probe(self, probe):
        self.capacity_probes.append(probe)

    def poll_analysis_inputs(self, *, max_results=None):
        return ()


def test_replay_window_does_not_throttle_race_state_on_mc_backlog(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    telemetry = _FakeTelemetryWorker()
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
        persistence_mode="temporary",
        telemetry_worker=telemetry,
    )

    assert telemetry.capacity_probes[-1] is None
    window.configure_session_context(
        persistence_mode="durable",
        state_directory=tmp_path / "live",
        auto_save_history=True,
    )
    assert telemetry.capacity_probes[-1] is None

    window.close()
    app.processEvents()


def test_replay_strategy_prediction_queues_every_leader_lap_snapshot(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
        state_directory=tmp_path / "replay",
        persistence_mode="temporary",
    )
    window._analysis_worker.shutdown(wait=False)
    window._analysis_worker = _FakeAnalysisWorker(AnalysisQueuePolicy.FIFO_ALL)
    window.service.recalculate_from_live_snapshot = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("Monte Carlo must not run on the GUI callback path")
    )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        tuple(
            CompletedLap(
                "1", "AAA", "Team", lap, 90.0, 90.0 * lap, 1, None,
                session_time_seconds=90.0 * lap,
            )
            for lap in range(1, 4)
        ),
        MappingProxyType({"1": 1}),
        None,
    )

    window.consume_live_snapshot(snapshot)

    assert len(window._analysis_worker.requests) == 3
    assert [
        max(lap.lap_number for lap in request["snapshot"].completed_laps)
        for request in window._analysis_worker.requests
    ] == [1, 2, 3]
    assert len({id(request["snapshot"]) for request in window._analysis_worker.requests}) == 3
    request = window._analysis_worker.requests[0]
    options = request["options"]
    assert options["sample_count"] == window.sample_count.value()
    assert "requested V1–V3" in window.calculation_log.toPlainText()
    window.close()
    app.processEvents()


def test_strategy_can_start_while_process_analysis_is_running(tmp_path):
    write_test_pre_race_prior(tmp_path)
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )
    window._analysis_worker.shutdown(wait=False)
    worker = _FakeAnalysisWorker()
    worker.analysis_running = True
    worker.running_input_version = 2
    window._analysis_worker = worker

    window._start_search()

    assert window._future is not None
    assert worker.analysis_running
    window.close()
    app.processEvents()


def test_live_strategy_prediction_keeps_only_latest_pending_request(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
        persistence_mode="durable",
    )
    window._analysis_worker.shutdown(wait=False)
    worker = _FakeAnalysisWorker()
    worker.analysis_running = True
    worker.running_input_version = 1
    window._analysis_worker = worker
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        (CompletedLap("1", "AAA", "Team", 1, 90.0, 90.0, 1, None),),
        MappingProxyType({"1": 1}),
        None,
    )

    window.consume_live_snapshot(snapshot)

    assert len(worker.requests) == 1
    assert worker.pending_input_version == 2
    assert "latest pending is V2" in window.calculation_log.toPlainText()
    window.close()
    app.processEvents()


def test_strategy_prediction_log_reports_process_recalculation_elapsed_time(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
    )
    window._analysis_worker.shutdown(wait=False)
    worker = _FakeAnalysisWorker()
    worker.completed = PublishedModelSnapshot(
        2,
        LiveRecalculationResult("no-new-clean-laps", 7, 12, elapsed_seconds=1.234),
        None,
    )
    window._analysis_worker = worker

    window._poll_future()

    assert "time=1.23s" in window.calculation_log.toPlainText()
    window.close()
    app.processEvents()


def test_replay_recalculation_stays_temporary_until_promoted(tmp_path):
    write_test_pre_race_prior(tmp_path)
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    write_weekend_model_config(
        {
            "sample_count": 100, "random_seed": 42,
            "fuel_rate_bounds": [0.0, 0.1], "track_rate_bounds": [-0.05, 0.05],
            "default_degradation_bounds": [0.0, 0.2],
            "default_compound_delta_bounds": [-1.0, 1.0],
            "reference_compound": "HARD", "clean_lap_noise_sigma": 0.35,
            "expected_sessions": ["R"], "expected_compounds": ["MEDIUM", "HARD"],
        },
        year=2026, round_number=5, data_root=tmp_path,
    )
    laps = tuple(
        CompletedLap(
            "1", "AAA", "Team", lap, 90.0 + lap * 0.1, 90.0 * lap,
            1, None, "MEDIUM", float(lap), 1, True,
        )
        for lap in range(1, 5)
    )
    snapshot = snapshot_with_clear_sector_traffic(laps)
    service = OfflineAnalysisService(tmp_path)
    service.activate_temporary_replay_model(2026, 5)
    result = service.recalculate_from_live_snapshot(
        year=2026, round_number=5, snapshot=snapshot, sample_count=100,
        quick_lap_threshold=1.1, min_clean_laps=3, persist=False,
    )

    assert result.status == "updated"
    assert service.load_model(2026, 5).manifest["analysis_id"] == result.analysis_id
    assert OfflineAnalysisService(tmp_path).load_model(2026, 5).manifest["analysis_id"] == "a1"


def test_strategy_prediction_restores_context_local_state(tmp_path):
    data_root = tmp_path / "data"
    state_directory = tmp_path / "replay"
    write_test_pre_race_prior(data_root)
    write_model_snapshot(
        model_snapshot(), year=2026, round_number=5, data_root=data_root
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    first = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(data_root),
        state_directory=state_directory,
    )
    assert first.strategy_tab.isAncestorOf(first.load_tyre_file_button)
    assert first.live_tab.isAncestorOf(first.enforce_compound_order)
    assert first.traffic_source.currentData() == "automatic"
    first.result_count.setValue(17)
    first.sample_count.setValue(4321)
    first.enforce_compound_order.setChecked(False)
    first.service.set_manual_tyre_input(2026, 5, "Perf_HARD_vs_MEDIUM", 0.25)
    first.service.set_manual_tyre_input(2026, 5, "Deg_MEDIUM", 0.11)
    first._refresh_tyre_inputs(apply_to_strategy=True)
    first._last_strategy_rows = [
        {
            "rank": 1,
            "formatted_strategy": "M-H",
            "compounds": ["MEDIUM", "HARD"],
            "pit_laps": [20],
            "remaining_cost": 12.3,
            "delta_to_best": 0.0,
            "current_lap": 0,
            "total_laps": 57,
        }
    ]
    first._save_state()
    first.close()
    app.processEvents()

    second = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(data_root),
        state_directory=state_directory,
    )
    assert second.result_count.value() == 17
    assert second.sample_count.value() == 4321
    assert (
        second._resolved_tyre_inputs.by_parameter()["Perf_HARD_vs_MEDIUM"].selected_value
        == 0.25
    )
    assert second._degradation_values["MEDIUM"] == 0.11
    assert second.strategy_table.rowCount() == 1
    assert second.strategy_table.item(0, 1).text() == "M-H"
    assert second._state_path == state_directory / "strategy_prediction_state.json"
    second.close()
    app.processEvents()


def test_temporary_strategy_state_keeps_settings_but_not_results(tmp_path):
    data_root = tmp_path / "data"
    state_directory = tmp_path / "replay"
    write_test_pre_race_prior(data_root)
    write_model_snapshot(
        model_snapshot(), year=2026, round_number=5, data_root=data_root
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    first = StrategyPredictionWindow(
        year=2026, round_number=5,
        service=OfflineAnalysisService(data_root),
        state_directory=state_directory,
        persistence_mode="temporary",
    )
    first.result_count.setValue(13)
    first._last_strategy_rows = [
        {
            "rank": 1, "formatted_strategy": "M-H",
            "compounds": ["MEDIUM", "HARD"], "pit_laps": [20],
            "remaining_cost": 1.0, "delta_to_best": 0.0,
            "current_lap": 0, "total_laps": 57,
        }
    ]
    first._save_state()
    first.close()
    app.processEvents()


    second = StrategyPredictionWindow(
        year=2026, round_number=5,
        service=OfflineAnalysisService(data_root),
        state_directory=state_directory,
        persistence_mode="temporary",
    )
    assert second.result_count.value() == 13
    assert second.strategy_table.rowCount() == 0
    assert "last_strategy_results" not in __import__("json").loads(
        second._state_path.read_text(encoding="utf-8")
    )
    assert second.strategy_input_table.horizontalHeaderItem(1).text().startswith(
        "Selected pre-race baseline"
    )
    assert second.history_snapshot_combo.count() == 1
    second.history_snapshot_combo.setCurrentIndex(0)
    assert second.history_snapshot_combo.currentData() == "clear"
    assert second.history_table.rowCount() == 0
    assert second.history_plot.listDataItems() == []
    assert second.compound_delta_history_plot.listDataItems() == []
    latest = second.service.load_model(2026, 5)
    replay_update = ModelSummary(
        {**latest.manifest, "as_of_leader_lap": 5}, latest.parameters
    )
    second._record_history_point(replay_update)
    assert {row["as_of_leader_lap"] for row in second._working_plot_rows} == {0, 5}
    assert second.degradation_history_plot.listDataItems()
    assert second.compound_delta_history_plot.listDataItems()
    second._select_history_lap(5)
    assert all(
        cursor.isVisible() and cursor.value() == 5
        for cursor in second._history_cursor_lines.values()
    )
    assert second._apply_history_lap_to_strategy(5)
    assert second.tabs.currentWidget() is second.strategy_tab
    assert set(second._external_tyre_sources.values()) == {"Update history lap 5"}
    second.reset_replay_history()
    assert second._working_plot_rows == []
    assert second.history_plot.listDataItems() == []
    assert second.compound_delta_history_plot.listDataItems() == []
    second.close()
    app.processEvents()


def test_history_assumed_degradation_is_selectable_and_plots_share_x_range(tmp_path):
    write_test_pre_race_prior(tmp_path)
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )
    rows = []
    for compound, degradation, delta, source in (
        ("HARD", .09, 0.0, "relative_degradation_assumption"),
        ("MEDIUM", .18, -.3, "race_evidence"),
        ("SOFT", .27, -.6, "relative_degradation_assumption"),
    ):
        rows.extend(window._plot_rows_from_assumptions([{
            "compound": compound, "degradation": degradation,
            "compound_delta": delta, "compound_delta_available": True,
            "degradation_source": source,
            "degradation_race_informed": source == "race_evidence",
        }], 9))
    for row in rows:
        row.update({"sampler_ess": 10.0, "sample_count": 100,
                    "ess_fraction": .1, "max_weight": .05, "top5_weight": .2})
    # Lap zero extends every chart, while ESS deliberately remains absent there.
    rows.extend(window._plot_rows_from_assumptions([{
        "compound": "MEDIUM", "degradation": .2, "compound_delta": -.3,
        "degradation_source": "relative_degradation_assumption",
    }], 0))
    window._estimated_strategy_inputs["SOFT"] = {
        "compound": "SOFT", "baseline_pace": 0.0, "degradation": .27,
        "sessions": "R", "aggregation": "test",
    }
    window._show_plot_rows(rows)

    ranges = [plot.viewRange()[0] for plot in (
        window.degradation_history_plot,
        window.compound_delta_history_plot,
        window.model_quality_plot,
    )]
    assert all(value == pytest.approx([0, 9]) for value in ranges)
    assert window._apply_history_lap_to_strategy(9)
    assert window._degradation_values["HARD"] == pytest.approx(.09)
    assert window._degradation_values["MEDIUM"] == pytest.approx(.18)
    assert window._degradation_values["SOFT"] == pytest.approx(.27)
    assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(.27)
    window.close()
    app.processEvents()


def test_named_snapshot_writes_durable_bundle(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    service = OfflineAnalysisService(tmp_path)
    destination = service.save_named_snapshot(
        2026, 5, "My replay checkpoint", [{"rank": 1, "strategy": "M-H"}]
    )
    assert destination.name == "snapshot.json"
    assert destination.parent.name == "My_replay_checkpoint"
    assert destination.is_file()
    snapshots = service.list_named_snapshots(2026, 5)
    assert snapshots[0]["name"] == "My replay checkpoint"
    loaded = service.load_named_snapshot(snapshots[0]["path"])
    assert loaded["strategy_results"] == [{"rank": 1, "strategy": "M-H"}]
    history_entries = service.list_analysis_history_entries(2026, 5)
    assert history_entries[0]["analysis_id"] == "a1"
    runs, parameters = service.load_analysis_history_entry(2026, 5, "a1")
    assert len(runs) == 1
    assert parameters
    plot_path = service.save_history_plot(
        2026, 5, "Whole plot", [
            {
                "as_of_leader_lap": 5, "source_scope": "aggregate",
                "parameter": "degradation", "compound": "MEDIUM", "median": 0.08,
            }
        ]
    )
    assert plot_path.is_file()
    plots = service.list_saved_history_plots(2026, 5)
    assert plots[0]["name"] == "Whole plot"
    assert service.load_history_plot(plots[0]["path"])["parameter_history"][0][
        "as_of_leader_lap"
    ] == 5


def test_strategy_search_captures_latest_live_race_state(tmp_path, monkeypatch):
    write_test_pre_race_prior(tmp_path)
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026, round_number=5, service=OfflineAnalysisService(tmp_path)
    )
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started",
        (LiveDriver("1", "AAA", "Team", "A Driver", 1, None),),
        (CompletedLap(
            "1", "AAA", "Team", 12, 88.0, 1056.0, 1, None,
            "SOFT", 5.0, 2, False,
        ),),
        MappingProxyType({"1": 1}), None, scheduled_total_laps=72,
    )
    window._last_seen_leader_lap = 12
    window.consume_live_snapshot(snapshot)
    captured = {}
    pending = Future()

    def submit(_function, **kwargs):
        captured.update(kwargs)
        return pending

    monkeypatch.setattr(window._executor, "submit", submit)
    window._start_search()

    state = captured["state"]
    assert state.current_completed_lap == 12
    assert state.current_compound == "SOFT"
    assert state.current_tyre_age == 4
    assert state.total_laps == 72
    assert window._strategy_request_context == window._current_strategy_context()
    pending.cancel()
    window._future = None
    window.close()
    app.processEvents()


def test_replay_model_config_is_generated_from_existing_resolved_settings(tmp_path, monkeypatch):
    from wostrategy.core.race_performance_settings import ResolvedRacePerformanceSettings
    settings = {
        "sample_count": 5000, "fuel_rate_bounds": [0, .1],
        "track_rate_bounds": [-.05, .05],
        "default_compound_degradation_bounds": [0, .5],
        "default_compound_delta_bounds": [-1, 1],
        "compound_delta_reference": "HARD",
    }
    monkeypatch.setattr(
        "woplanner.analysis.service.resolve_race_performance_settings",
        lambda **_kwargs: ResolvedRacePerformanceSettings(settings, "test", tmp_path / "settings.json"),
    )
    service = OfflineAnalysisService(tmp_path)
    config, source = service.resolve_live_model_config(
        2026, 12, replay_directory=tmp_path / "replay"
    )
    assert config["sample_count"] == 5000
    assert config["random_seed"]
    assert "settings builder" in source
    assert (tmp_path / "replay/replay_model_config.json").is_file()


def test_replay_model_config_fails_before_analysis_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "woplanner.analysis.service.resolve_race_performance_settings",
        lambda **_kwargs: None,
    )
    service = OfflineAnalysisService(tmp_path)
    with pytest.raises(ValueError, match="Run pre_race_analysis"):
        service.resolve_live_model_config(2026, 12, replay_directory=tmp_path / "replay")


def test_tyre_history_segments_transition_each_coordinate_once():
    rows = []
    def add(lap, parameter, compound, value, source):
        row = {"source_scope": "aggregate", "as_of_leader_lap": lap,
               "parameter": parameter, "compound": compound, "median": value}
        row["degradation_source" if parameter == "degradation" else "compound_delta_source"] = source
        rows.append(row)
    add(0, "degradation", "SOFT", .30, "pre_race_baseline")
    add(6, "degradation", "SOFT", .24, "pre_race_estimate")
    add(12, "degradation", "SOFT", .28, "race_evidence")
    add(20, "degradation", "SOFT", .27, "race_evidence")
    add(0, "degradation", "HARD", .10, "pre_race_baseline")
    add(20, "degradation", "HARD", .09, "race_evidence")
    add(0, "compound_delta", "SOFT", -.5, "resolved_pre_race_baseline")
    add(12, "compound_delta", "SOFT", -.4, "race_evidence")
    add(20, "compound_delta", "SOFT", -.35, "race_evidence")
    add(0, "compound_delta", "MEDIUM", -.2, "resolved_pre_race_baseline")
    segments = tyre_history_source_segments(rows)

    assert segments[("degradation", "SOFT")] == (
        {
            "source": "base",
            "points": ((0, .30), (6, .24), (12, .28)),
        },
        {"source": "live_direct", "points": ((12, .28), (20, .27))},
    )
    assert segments[("degradation", "HARD")][0]["points"][-1] == (20, .09)
    assert segments[("compound_delta", "MEDIUM")] == (
        {"source": "base", "points": ((0, -.2), (20, -.2))},
    )


def test_degradation_history_has_base_derived_direct_segments_without_stale_tail():
    rows = [
        {"source_scope": "aggregate", "as_of_leader_lap": 0,
         "parameter": "degradation", "compound": "SOFT", "median": .18,
         "degradation_source": "base"},
        {"source_scope": "aggregate", "as_of_leader_lap": 31,
         "parameter": "degradation", "compound": "SOFT", "median": .45,
         "degradation_source": "live_derived"},
        {"source_scope": "aggregate", "as_of_leader_lap": 40,
         "parameter": "degradation", "compound": "SOFT", "median": .42,
         "degradation_source": "live_derived"},
        {"source_scope": "aggregate", "as_of_leader_lap": 65,
         "parameter": "degradation", "compound": "SOFT", "median": .39,
         "degradation_source": "live_direct", "degradation_race_informed": True},
        {"source_scope": "aggregate", "as_of_leader_lap": 66,
         "parameter": "degradation", "compound": "SOFT", "median": .38,
         "degradation_source": "live_direct", "degradation_race_informed": True},
    ]
    assert tyre_history_source_segments(rows)[("degradation", "SOFT")] == (
        {"source": "base", "points": ((0, .18), (31, .45))},
        {"source": "live_derived", "points": ((31, .45), (40, .42), (65, .39))},
        {"source": "live_direct", "points": ((65, .39), (66, .38))},
    )
    assert "SOFT: degradation 0.450 s/lap [live_derived]" in format_history_hover_text(
        rows, leader_lap=31, reference_compound="MEDIUM"
    )


def test_tyre_history_does_not_fabricate_missing_compound_delta():
    rows = [{"source_scope": "aggregate", "as_of_leader_lap": 10,
             "parameter": "degradation", "compound": "HARD", "median": .1,
             "degradation_source": "race_evidence"}]
    segments = tyre_history_source_segments(rows)
    assert ("compound_delta", "HARD") not in segments
