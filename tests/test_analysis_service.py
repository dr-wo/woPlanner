import os
from concurrent.futures import Future
from datetime import datetime, timezone

from types import MappingProxyType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from wodata.artifacts import write_model_snapshot, write_weekend_model_config
from wodata.contracts import ModelSnapshot, ParameterEstimate, StrategySearchState
from wodata.live import CompletedLap, CompletedSector, LiveSessionSnapshot
from woplanner.analysis_service import (
    LiveRecalculationResult,
    ModelSummary,
    OfflineAnalysisService,
    _live_analysis_laps,
)
from woplanner.analysis_gui import StrategyPredictionWindow, format_history_hover_text


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
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), laps, MappingProxyType({"1": 1}), None
    )
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
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), laps, MappingProxyType({"1": 1}), None
    )
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
    assert second.status == "no-new-clean-laps"
    assert second.previous_clean_lap_count == 4


def test_replay_strategy_prediction_queues_leader_laps_while_worker_is_busy(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
        state_directory=tmp_path / "replay",
        persistence_mode="temporary",
    )
    busy = Future()
    window._future = busy
    window._future_mode = "strategy"
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

    assert len(window._live_recalculation_queue) == 3
    assert [item[0] for item in window._live_recalculation_queue] == [1, 2, 3]
    assert [
        max(lap.lap_number for lap in item[1].completed_laps)
        for item in window._live_recalculation_queue
    ] == [1, 2, 3]
    _, _, options = window._live_recalculation_queue[0]
    assert options["sample_count"] == window.sample_count.value()
    assert "pending=3" in window.calculation_log.toPlainText()
    window._future = None
    window._future_mode = None
    window._live_recalculation_queue.clear()
    window.close()
    app.processEvents()


def test_live_strategy_prediction_does_not_queue_while_worker_is_busy(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
        persistence_mode="durable",
    )
    window._future = Future()
    window._future_mode = "live-model"
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        (CompletedLap("1", "AAA", "Team", 1, 90.0, 90.0, 1, None),),
        MappingProxyType({"1": 1}),
        None,
    )

    window.consume_live_snapshot(snapshot)

    assert not window._live_recalculation_queue
    assert "not queued" in window.calculation_log.toPlainText()
    window._future = None
    window._future_mode = None
    window.close()
    app.processEvents()


def test_strategy_prediction_log_reports_recalculation_elapsed_time(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=5,
        service=OfflineAnalysisService(tmp_path),
    )
    completed = Future()
    completed.set_result(
        LiveRecalculationResult(
            "no-new-clean-laps", 7, 12, elapsed_seconds=1.234
        )
    )
    window._future = completed
    window._future_mode = "live-model"

    window._poll_future()

    assert "time=1.23s" in window.calculation_log.toPlainText()
    window.close()
    app.processEvents()


def test_replay_recalculation_stays_temporary_until_promoted(tmp_path):
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
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), laps, MappingProxyType({"1": 1}), None
    )
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
    assert first.sector_traffic_filter.isChecked()
    first.result_count.setValue(17)
    first.sample_count.setValue(4321)
    first.enforce_compound_order.setChecked(False)
    first._baseline_values["MEDIUM"] = 0.25
    first._manual_baseline_compounds.add("MEDIUM")
    first._degradation_values["MEDIUM"] = 0.11
    first._manual_degradation_compounds.add("MEDIUM")
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
    assert second._baseline_values["MEDIUM"] == 0.25
    assert second._degradation_values["MEDIUM"] == 0.11
    assert second.strategy_table.rowCount() == 1
    assert second.strategy_table.item(0, 1).text() == "M-H"
    assert second._state_path == state_directory / "strategy_prediction_state.json"
    second.close()
    app.processEvents()


def test_temporary_strategy_state_keeps_settings_but_not_results(tmp_path):
    data_root = tmp_path / "data"
    state_directory = tmp_path / "replay"
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
        "Initial FP baseline"
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
