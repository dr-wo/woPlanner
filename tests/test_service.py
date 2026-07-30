from __future__ import annotations

import pandas as pd

from woplanner.model import (
    COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    COMPARISON_MODE_OBSERVED_REAL_DATA,
    COMPARISON_MODE_PURE_SIM,
    PlannedPitStop,
    StintSegment,
    StrategyPlan,
)
from woplanner.normalize import normalize_race_laps
from woplanner.service import (
    RacePlannerService,
    assign_driver_styles,
    default_reference_driver,
    latest_driver_order,
    build_pit_window_data,
    build_reference_delta_data,
    build_track_status_event_data,
    build_tyre_event_data,
    build_lap_delta_guide_data,
    build_modelled_real_strategy_plan,
    build_sector_strategy_comparison,
    add_driver_ranks,
    starting_driver_order,
    estimate_actual_pit_loss_by_lap,
    track_status_type,
)
from woplanner.strategy_model import (
    MissingDriverStrategyModelError,
    MissingStrategyModelError,
    WoStrategyRaceModelAdapter,
)


def test_normalize_race_laps_calculates_cumulative_time():
    raw = pd.DataFrame(
        {
            "Driver": ["AAA", "AAA", "BBB"],
            "Team": ["Team A", "Team A", "Team B"],
            "LapNumber": [1, 2, 1],
            "LapTime": [pd.Timedelta(seconds=90), pd.Timedelta(seconds=91), pd.Timedelta(seconds=92)],
            "Compound": ["MEDIUM", "MEDIUM", "HARD"],
            "Stint": [1, 1, 1],
            "TyreLife": [1, 2, 1],
        }
    )

    session = normalize_race_laps(raw, year=2026, round_number=1, session_name="R")

    aaa = session.laps.loc[session.laps["driver"] == "AAA"]
    assert aaa["cumulative_time_s"].tolist() == [90.0, 181.0]


def test_normalize_race_laps_prefers_absolute_lap_end_time_for_cumulative_order():
    raw = pd.DataFrame(
        {
            "Driver": ["AAA", "BBB"],
            "Team": ["Team A", "Team B"],
            "LapNumber": [2, 2],
            "LapTime": [168.0, 123.0],
            "LapStartTime": [
                pd.Timedelta(seconds=100.0),
                pd.Timedelta(seconds=145.0),
            ],
            "Time": [
                pd.Timedelta(seconds=268.0),
                pd.Timedelta(seconds=276.0),
            ],
            "Position": [1.0, 2.0],
        }
    )

    session = normalize_race_laps(raw, year=2026, round_number=1, session_name="R")

    assert session.laps.set_index("driver")["cumulative_time_s"].to_dict() == {
        "AAA": 168.0,
        "BBB": 176.0,
    }


def test_default_reference_driver_uses_result_winner():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "BBB"],
            "lap": [1, 1],
            "cumulative_time_s": [90.0, 91.0],
            "result_rank": [2.0, 1.0],
        }
    )

    assert default_reference_driver(laps) == "BBB"


def test_reference_delta_calculation_uses_selected_driver():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "BBB", "BBB"],
            "lap": [1, 2, 1, 2],
            "cumulative_time_s": [90.0, 181.0, 91.0, 183.5],
        }
    )

    plot_data = build_reference_delta_data(laps, reference_driver="AAA")
    bbb = plot_data.loc[plot_data["driver"] == "BBB"]

    assert bbb["delta_s"].tolist() == [1.0, 2.5]


def test_starting_and_latest_orders_rank_by_cumulative_time_without_result_rank():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "BBB", "BBB"],
            "lap": [1, 2, 1, 2],
            "cumulative_time_s": [90.0, 181.0, 91.0, 180.5],
        }
    )

    assert starting_driver_order(laps).set_index("driver")["rank"].to_dict() == {
        "AAA": 1,
        "BBB": 2,
    }
    assert latest_driver_order(laps).set_index("driver")["rank"].to_dict() == {
        "BBB": 1,
        "AAA": 2,
    }


def test_starting_order_prefers_session_start_position_over_first_lap_time():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "BBB"],
            "lap": [1, 1],
            "cumulative_time_s": [90.0, 91.0],
            "start_position": [2.0, 1.0],
        }
    )

    assert starting_driver_order(laps).set_index("driver")["rank"].to_dict() == {
        "BBB": 1,
        "AAA": 2,
    }


def test_pit_window_boundary_calculation():
    selected = pd.DataFrame({"lap": [1, 2], "delta_s": [0.0, 1.5]})

    pit_window = build_pit_window_data(
        selected,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    assert pit_window["normal_lower_boundary_s"].tolist() == [-20.0, -18.5]
    assert pit_window["vsc_lower_boundary_s"].tolist() == [-10.0, -8.5]
    assert pit_window["vsc_upper_boundary_s"].tolist() == [10.0, 11.5]
    assert pit_window["normal_upper_boundary_s"].tolist() == [20.0, 21.5]


def test_service_pit_window_uses_current_reference_delta():
    service = RacePlannerService()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team B", "Team B"],
                "LapNumber": [1, 2, 1, 2],
                "LapTime": [90.0, 91.0, 91.0, 92.5],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.build_plot_data("AAA")
    pit_window = service.build_pit_window_data(
        "BBB",
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    assert pit_window["normal_lower_boundary_s"].tolist() == [-19.0, -17.5]
    assert pit_window["vsc_lower_boundary_s"].tolist() == [-9.0, -7.5]
    assert pit_window["vsc_upper_boundary_s"].tolist() == [11.0, 12.5]
    assert pit_window["normal_upper_boundary_s"].tolist() == [21.0, 22.5]


def test_track_status_event_data_classifies_sc_and_vsc_laps():
    laps = pd.DataFrame(
        {
            "lap": [1, 2, 3, 4, 5, 5, 6],
            "track_status": ["1", "4", "6", "7", "VSC", "SC", "SAFETY CAR"],
        }
    )

    status_data = build_track_status_event_data(laps)

    assert track_status_type("4") == "sc"
    assert track_status_type("6") == "vsc"
    assert track_status_type("7") == "vsc"
    assert status_data.to_dict("records") == [
        {"lap": 2, "track_status_type": "sc", "label": "SC"},
        {"lap": 3, "track_status_type": "vsc", "label": "VSC"},
        {"lap": 4, "track_status_type": "vsc", "label": "VSC"},
        {"lap": 5, "track_status_type": "sc", "label": "SC"},
        {"lap": 6, "track_status_type": "sc", "label": "SC"},
    ]


def test_real_race_adjusted_keeps_physical_vsc_residual_before_leader_aligned_band():
    session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["LEA", "LEA", "SIM", "SIM"],
                "Team": ["Team A", "Team A", "Team B", "Team B"],
                "LapNumber": [1, 2, 1, 2],
                "LapTime": [90.0, 90.0, 90.0, 96.0],
                "Sector1Time": [30.0, 30.0, 30.0, 30.0],
                "Sector2Time": [30.0, 30.0, 30.0, 36.0],
                "Sector3Time": [30.0, 30.0, 30.0, 30.0],
                "Compound": ["HARD"] * 4,
                "TyreLife": [1, 2, 1, 2],
                "TrackStatus": ["1", "1", "1", "6"],
                "Sector1TrackStatus": ["1", "1", "1", "1"],
                "Sector2TrackStatus": ["1", "1", "1", "6"],
                "Sector3TrackStatus": ["1", "1", "1", "1"],
                "Position": [1, 1, 2, 2],
            }
        ),
        year=2026,
        round_number=11,
        session_name="R",
    )
    observed = session.laps.loc[session.laps["driver"].eq("SIM")].rename(
        columns={"lap_time_s": "observed_real_lap_time_s"}
    )
    model_laps = pd.DataFrame(
        {
            "lap": [2],
            "driver": ["SIM"],
            "team": ["Team B"],
            "compound": ["HARD"],
            "tyre_age": [1],
            "predicted_clean_lap_time_s": [90.0],
            "pit_loss_s": [0.0],
            "pit_in_loss_s": [0.0],
            "pit_out_loss_s": [0.0],
            "predicted_total_lap_time_s": [90.0],
        }
    )

    comparison = build_sector_strategy_comparison(
        model_laps,
        model_laps,
        observed,
        comparison_mode=COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    )
    sector_2 = comparison.loc[comparison["sector"].eq("S2")].iloc[0]
    event_observed = observed.loc[observed["lap"].eq(2)].iloc[0]

    assert event_observed["track_status_s2"] == "1"
    assert event_observed["physical_track_status_s2"] == "6"
    assert sector_2["race_domain_residual_s"] == 6.0
    assert sector_2["display_residual_s"] == 6.0
    assert sector_2["sim_display_sector_time_s"] == 36.0


def test_service_defaults_pit_loss_from_first_lap_after_stop_status():
    service = RacePlannerService()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA"],
                "Team": ["Team A", "Team A", "Team A"],
                "LapNumber": [1, 2, 3],
                "LapTime": [90.0, 91.0, 92.0],
                "TrackStatus": ["1", "6", "4"],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    assert service.pit_loss_default_for_stop(
        stop_after_lap=1,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    ) == 10.0
    assert service.pit_loss_default_for_stop(
        stop_after_lap=0,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    ) == 20.0


def test_tyre_event_data_marks_stint_changes_but_not_starting_tyres():
    plot_data = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "AAA", "BBB"],
            "lap": [1, 2, 3, 1],
            "delta_s": [0.0, 1.0, 2.0, 0.5],
            "compound": ["MEDIUM", "MEDIUM", "HARD", "SOFT"],
            "stint": [1, 1, 2, 1],
            "tyre_age": [1, 2, 1, 4],
            "pit_out": [True, False, True, True],
        }
    )

    events = build_tyre_event_data(plot_data)

    assert events[["driver", "lap", "compound", "tyre_age_label"]].to_dict("records") == [
        {"driver": "AAA", "lap": 3, "compound": "HARD", "tyre_age_label": "0"},
    ]


def test_tyre_event_data_marks_only_first_simulated_lap():
    plot_data = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "AAA", "AAA", "AAA", "AAA"],
            "lap": [1, 2, 3, 2, 3, 4],
            "delta_s": [0.0, 1.0, 2.0, 20.0, 21.0, 22.0],
            "compound": ["MEDIUM", "MEDIUM", "MEDIUM", "HARD", "HARD", "HARD"],
            "stint": [1, 1, 1, 2, 2, 2],
            "tyre_age": [1, 2, 3, 0, 1, 2],
            "pit_out": [True, False, False, True, False, False],
            "plot_role": ["actual", "actual", "actual", "simulation", "simulation", "simulation"],
            "scenario_id": [pd.NA, pd.NA, pd.NA, "sim-1", "sim-1", "sim-1"],
            "is_simulated": [False, False, False, True, True, True],
        }
    )

    events = build_tyre_event_data(plot_data)

    assert events[["driver", "lap", "compound", "tyre_age_label", "is_simulated"]].to_dict(
        "records"
    ) == [
        {
            "driver": "AAA",
            "lap": 2,
            "compound": "HARD",
            "tyre_age_label": "0",
            "is_simulated": True,
        },
    ]


def test_tyre_event_data_preserves_faded_actual_marker_state():
    plot_data = pd.DataFrame(
        {
            "driver": ["AAA", "AAA"],
            "lap": [1, 2],
            "delta_s": [0.0, 1.0],
            "compound": ["MEDIUM", "HARD"],
            "stint": [1, 2],
            "tyre_age": [1, 0],
            "pit_out": [False, True],
            "plot_role": ["actual", "actual"],
            "scenario_id": [pd.NA, pd.NA],
            "is_simulated": [False, False],
            "is_faded": [False, True],
        }
    )

    events = build_tyre_event_data(plot_data)

    assert events[["driver", "lap", "is_faded"]].to_dict("records") == [
        {"driver": "AAA", "lap": 2, "is_faded": True}
    ]


def test_add_driver_ranks_includes_start_and_latest_tyre_labels():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "BBB", "BBB"],
            "team": ["Team A", "Team A", "Team B", "Team B"],
            "lap": [1, 2, 1, 2],
            "cumulative_time_s": [90.0, 181.0, 91.0, 180.5],
            "compound": ["MEDIUM", "HARD", "SOFT", "SOFT"],
            "tyre_age": [1, 1, 4, 5],
        }
    )
    drivers = pd.DataFrame({"driver": ["AAA", "BBB"], "team": ["Team A", "Team B"]})

    ranked = add_driver_ranks(laps, drivers).set_index("driver")

    assert ranked.loc["AAA", "start_tyre_compound"] == "MEDIUM"
    assert ranked.loc["AAA", "start_tyre_age_label"] == "0"
    assert ranked.loc["AAA", "latest_tyre_compound"] == "HARD"
    assert ranked.loc["AAA", "latest_tyre_age_label"] == "0"
    assert ranked.loc["BBB", "start_tyre_age_label"] == "3"
    assert ranked.loc["BBB", "latest_tyre_age_label"] == "4"


def test_lap_delta_guides_use_reference_lap_time_and_visible_range():
    plot_data = pd.DataFrame({"delta_s": [-250.0, 100.0]})
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "BBB"],
            "lap_time_s": [80.0, 82.0, 90.0],
        }
    )

    guides = build_lap_delta_guide_data(
        plot_data,
        laps,
        reference_driver="AAA",
    )

    assert guides[["lap_delta", "y", "label"]].to_dict("records") == [
        {"lap_delta": -3, "y": -243.0, "label": "-3 laps"},
        {"lap_delta": -2, "y": -162.0, "label": "-2 laps"},
        {"lap_delta": -1, "y": -81.0, "label": "-1 lap"},
        {"lap_delta": 1, "y": 81.0, "label": "+1 lap"},
    ]


def test_team_colour_and_teammate_line_style_assignment():
    drivers = pd.DataFrame(
        {
            "driver": ["HAM", "RUS", "VER"],
            "team": ["Mercedes", "Mercedes", "Red Bull Racing"],
        }
    )

    styled = assign_driver_styles(drivers)

    assert styled.loc[styled["driver"] == "HAM", "colour"].iloc[0] == "#00D7B6"
    assert styled.loc[styled["driver"] == "HAM", "line_style"].iloc[0] == "solid"
    assert styled.loc[styled["driver"] == "RUS", "line_style"].iloc[0] == "dash"
    assert styled.loc[styled["driver"] == "VER", "colour"].iloc[0] == "#4781D7"


def test_strategy_model_adapter_reads_weighted_median_cache(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    prefix = "race_performance_2026_7_R"
    (source / f"{prefix}_summary_baseline_pace.csv").write_text(
        "Team,Driver,P10,Median,P90,SampleCount,WeightSum\nMercedes,HAM,80,81,82,10,5\n"
    )
    (source / f"{prefix}_summary_fuel_rate.csv").write_text(
        "P10,Median,P90,SampleCount,WeightSum\n0.01,0.05,0.09,10,5\n"
    )
    (source / f"{prefix}_summary_track_rate.csv").write_text(
        "P10,Median,P90,SampleCount,WeightSum\n0.00,0.02,0.04,10,5\n"
    )
    (source / f"{prefix}_summary_compound_delta.csv").write_text(
        "Compound,CompoundDeltaReference,P10,Median,P90,SampleCount,WeightSum\n"
        "HARD,HARD,0,0,0,10,5\nMEDIUM,HARD,-0.5,-0.2,0,10,5\n"
    )
    (source / f"{prefix}_summary_team_compound_degradation.csv").write_text(
        "Team,Compound,P10,Median,P90,SampleCount,WeightSum\n"
        "Mercedes,HARD,0.02,0.10,0.20,10,5\n"
        "Mercedes,MEDIUM,0.04,0.15,0.25,10,5\n"
    )
    (source / f"{prefix}_clean_laps.csv").write_text(
        "Driver,Team,LapNumber,TyreAgeLaps,FuelLapNumber\n"
        "HAM,Mercedes,10,3,40\nHAM,Mercedes,20,5,30\n"
    )
    adapter = WoStrategyRaceModelAdapter(data_root=tmp_path / "data", source_cache_dir=source)

    model = adapter.load_model(year=2026, round_number=7, session_name="R")
    hard = model.loc[model["compound"] == "HARD"].iloc[0]

    assert hard["driver"] == "HAM"
    assert hard["baseline_pace_s"] == 81.0
    assert hard["fuel_rate_s_per_lap"] == 0.05
    assert hard["track_rate_s_per_lap"] == 0.02
    assert hard["team_compound_degradation_s_per_lap"] == 0.10
    assert hard["reference_lap"] == 15.0
    assert hard["fuel_reference"] == 35.0
    assert hard["tyre_age_reference"] == 4.0


def test_strategy_model_adapter_reports_missing_cache_files(tmp_path):
    adapter = WoStrategyRaceModelAdapter(data_root=tmp_path / "data", source_cache_dir=tmp_path)

    try:
        adapter.load_model(year=2026, round_number=7, session_name="R")
    except MissingStrategyModelError as exc:
        assert "race_performance_2026_7_R_summary_baseline_pace.csv" in str(exc)
        assert "race_performance_review" in str(exc) or str(tmp_path) in str(exc)
    else:
        raise AssertionError("Expected MissingStrategyModelError")


def test_strategy_model_adapter_can_use_teammate_data_for_missing_driver():
    class InMemoryAdapter(WoStrategyRaceModelAdapter):
        def load_model(self, **kwargs):
            return pd.DataFrame(
                {
                    "driver": ["RUS"],
                    "team": ["Mercedes"],
                    "compound": ["MEDIUM"],
                    "baseline_pace_s": [81.0],
                    "compound_delta_s": [-0.2],
                    "team_compound_degradation_s_per_lap": [0.1],
                    "tyre_age_reference": [0.0],
                    "fuel_total_laps": [70.0],
                    "fuel_rate_s_per_lap": [0.0],
                    "fuel_reference": [0.0],
                    "track_rate_s_per_lap": [0.0],
                    "reference_lap": [1.0],
                }
            )

    adapter = InMemoryAdapter()
    plan = StrategyPlan(
        driver="ANT",
        team="Mercedes",
        start_after_lap=1,
        end_lap=2,
        segments=[
            StintSegment(
                start_lap=2,
                end_lap=2,
                compound="MEDIUM",
                initial_tyre_age=5,
            )
        ],
        pit_stops=[],
    )

    try:
        adapter.predict_strategy_plan(
            year=2026,
            round_number=11,
            session_name="R",
            strategy_plan=plan,
        )
    except MissingDriverStrategyModelError as exc:
        assert exc.driver == "ANT"
        assert exc.team_model_driver == "RUS"
    else:
        raise AssertionError("Expected MissingDriverStrategyModelError")

    predicted = adapter.predict_strategy_plan(
        year=2026,
        round_number=11,
        session_name="R",
        strategy_plan=plan,
        use_team_data=True,
    )

    assert predicted["driver"].tolist() == ["ANT"]
    assert predicted["predicted_clean_lap_time_s"].tolist() == [81.3]


def test_service_creates_simulation_without_mutating_base_session():
    class FakeStrategyModel:
        def predict_future_laps(self, **kwargs):
            laps = list(range(kwargs["start_lap"], kwargs["end_lap"] + 1))
            return pd.DataFrame(
                {
                    "driver": kwargs["driver"],
                    "team": kwargs["team"],
                    "lap": laps,
                    "compound": kwargs["compound"],
                    "tyre_age": [kwargs["initial_tyre_age"] + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [88.0 for _ in laps],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [90.0, 91.0, 92.0, 91.0, 91.0, 91.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM", "HARD", "HARD", "HARD"],
                "Stint": [1, 1, 1, 1, 1, 1],
                "TyreLife": [1, 2, 3, 1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )
    base_laps = service.session.laps.copy(deep=True)

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=5,
        pit_loss_s=20.0,
    )
    plot_data = service.build_plot_data("BBB")

    pd.testing.assert_frame_equal(service.session.laps, base_laps)
    simulation_curve = plot_data.loc[plot_data["scenario_id"] == scenario.scenario_id]
    assert simulation_curve["lap"].tolist() == [1, 2, 3]
    assert simulation_curve["plot_role"].tolist() == ["simulation", "simulation", "simulation"]
    assert simulation_curve["is_simulated"].tolist() == [False, True, True]
    simulated = simulation_curve.loc[simulation_curve["is_simulated"]]
    assert simulated["lap_time_s"].tolist() == [111.0, 92.0]
    assert simulated["tyre_age"].tolist() == [5, 6]
    faded_actual = plot_data.loc[
        (plot_data["driver"] == "AAA")
        & (plot_data["plot_role"] == "actual")
        & (plot_data["is_faded"])
    ]
    assert faded_actual["lap"].tolist() == [1, 2, 3]
    assert plot_data.loc[
        (plot_data["driver"] == "AAA")
        & (plot_data["plot_role"] == "actual")
        & (plot_data["lap"] > 1),
        "is_faded",
    ].tolist() == [True, True]


def test_service_replaces_existing_driver_simulation():
    class FakeStrategyModel:
        def predict_future_laps(self, **kwargs):
            return pd.DataFrame(
                {
                    "driver": kwargs["driver"],
                    "team": kwargs["team"],
                    "lap": [kwargs["start_lap"]],
                    "compound": kwargs["compound"],
                    "tyre_age": [kwargs["initial_tyre_age"]],
                    "predicted_clean_lap_time_s": [90.0],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA"],
                "Team": ["Team A", "Team A"],
                "LapNumber": [1, 2],
                "LapTime": [90.0, 91.0],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    first = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    second = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="MEDIUM",
        initial_tyre_age=2,
        pit_loss_s=18.0,
    )

    assert [scenario.scenario_id for scenario in service.scenarios.active_scenarios()] == [
        first.scenario_id
    ]
    assert first.scenario_id == second.scenario_id
    assert len(second.edits) == 1
    assert second.edits[0].compound == "MEDIUM"
    assert second.edits[0].initial_tyre_age == 2
    assert second.edits[0].pit_loss_s == 18.0


def test_service_preserves_manual_pit_loss_when_model_split_is_available():
    class FakeStrategyModel:
        def pit_loss_split(self, **kwargs):
            return 4.779, 16.742

        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    stop = next(
                        (
                            pit_stop
                            for pit_stop in plan.pit_stops
                            if pit_stop.first_lap_after_stop == lap
                        ),
                        None,
                    )
                    pit_loss = float(stop.pit_loss_s) if stop is not None else 0.0
                    pit_in = float(stop.pit_in_loss_s or 0.0) if stop is not None else 0.0
                    pit_out = float(stop.pit_out_loss_s or 0.0) if stop is not None else 0.0
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": 90.0,
                            "pit_loss_s": pit_loss,
                            "pit_in_loss_s": pit_in,
                            "pit_out_loss_s": pit_out,
                            "predicted_total_lap_time_s": 90.0 + pit_in + pit_out,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA"],
                "Team": ["Team A", "Team A", "Team A"],
                "LapNumber": [1, 2, 3],
                "LapTime": [90.0, 91.0, 92.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM"],
                "Stint": [1, 1, 1],
                "TyreLife": [1, 2, 3],
            }
        ),
        year=2026,
        round_number=10,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=18.0,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    edit = scenario.edits[0]
    assert edit.pit_loss_s == 18.0
    assert round(float(edit.pit_in_loss_s), 6) == round(18.0 * 4.779 / 21.521, 6)
    assert round(float(edit.pit_out_loss_s), 6) == round(18.0 - float(edit.pit_in_loss_s), 6)
    assert edit.pit_loss_source == "manual, woStrategy split ratio"
    comparison = service.scenarios.active_comparisons()[0]
    first_simulated = comparison.comparison_laps.loc[
        comparison.comparison_laps["what_if_pit_loss_s"] > 0.0
    ].iloc[0]
    assert float(first_simulated["what_if_pit_loss_s"]) == 18.0


def test_add_pit_on_observed_stop_replaces_observed_pit_residual():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_in = sum(
                        float(stop.pit_in_loss_s or 0.0)
                        for stop in plan.pit_stops
                        if int(stop.stop_after_lap) == int(lap)
                    )
                    pit_out = sum(
                        float(stop.pit_out_loss_s or 0.0)
                        for stop in plan.pit_stops
                        if int(stop.first_lap_after_stop) == int(lap)
                    )
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": 90.0,
                            "pit_loss_s": pit_in + pit_out,
                            "pit_in_loss_s": pit_in,
                            "pit_out_loss_s": pit_out,
                            "predicted_total_lap_time_s": 90.0 + pit_in + pit_out,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "BBB"],
                "Team": [
                    "Team A",
                    "Team A",
                    "Team A",
                    "Team A",
                    "Team B",
                    "Team B",
                    "Team B",
                    "Team B",
                ],
                "LapNumber": [1, 2, 3, 4, 1, 2, 3, 4],
                "LapTime": [90.0, 102.0, 102.0, 90.0, 90.0, 90.0, 90.0, 90.0],
                "Sector1Time": [30.0, 30.0, 42.0, 30.0, 30.0, 30.0, 30.0, 30.0],
                "Sector2Time": [30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0],
                "Sector3Time": [30.0, 42.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD", "HARD", "HARD", "HARD", "HARD", "HARD"],
                "Stint": [1, 1, 2, 2, 1, 1, 1, 1],
                "TyreLife": [1, 2, 1, 2, 1, 2, 3, 4],
                "PitInTime": [
                    pd.NaT,
                    pd.Timestamp("2026-01-01"),
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                ],
                "PitOutTime": [
                    pd.NaT,
                    pd.NaT,
                    pd.Timestamp("2026-01-01"),
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                    pd.NaT,
                ],
                "TrackStatus": ["1"] * 8,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=2,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=18.0,
        comparison_mode=COMPARISON_MODE_MODELLED_REAL_STRATEGY,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    delta = service.build_strategy_delta_data()
    pit_sectors = delta.loc[
        ((delta["lap"] == 2) & (delta["sector"] == "S3"))
        | ((delta["lap"] == 3) & (delta["sector"] == "S1"))
    ]

    assert pit_sectors["what_if_pit_loss_s"].tolist() == [9.0, 9.0]
    assert pit_sectors["race_domain_residual_s"].tolist() == [0.0, 0.0]
    assert pit_sectors["sim_display_sector_time_s"].tolist() == [39.0, 39.0]


def test_service_adds_multiple_edits_to_one_driver_scenario_and_rebuilds_from_earliest():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"MEDIUM": 100.0, "HARD": 90.0, "SOFT": 85.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 5 + ["BBB"] * 5,
                "Team": ["Team A"] * 5 + ["Team B"] * 5,
                "LapNumber": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5],
                "LapTime": [100.0] * 10,
                "Compound": ["MEDIUM"] * 5 + ["HARD"] * 5,
                "TyreLife": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    first = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    second = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=3,
        compound="SOFT",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    delta = service.build_strategy_delta_data()
    plot_data = service.build_plot_data("BBB")
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == second.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]
    tyre_events = service.build_tyre_event_data()
    simulated_events = tyre_events.loc[
        tyre_events["scenario_id"].astype(str) == second.scenario_id
    ]

    assert first.scenario_id == second.scenario_id
    assert [edit.stop_after_lap for edit in second.edits] == [1, 3]
    assert delta[["lap", "compound", "sim_display_lap_time_s"]].to_dict("records") == [
        {"lap": 2, "compound": "HARD", "sim_display_lap_time_s": 110.0},
        {"lap": 3, "compound": "HARD", "sim_display_lap_time_s": 90.0},
        {"lap": 4, "compound": "SOFT", "sim_display_lap_time_s": 105.0},
        {"lap": 5, "compound": "SOFT", "sim_display_lap_time_s": 85.0},
    ]
    assert simulation[["lap", "cumulative_time_s"]].to_dict("records") == [
        {"lap": 2, "cumulative_time_s": 210.0},
        {"lap": 3, "cumulative_time_s": 300.0},
        {"lap": 4, "cumulative_time_s": 405.0},
        {"lap": 5, "cumulative_time_s": 490.0},
    ]
    assert simulated_events[["lap", "compound", "stop_after_lap"]].to_dict("records") == [
        {"lap": 2, "compound": "HARD", "stop_after_lap": 1.0},
        {"lap": 4, "compound": "SOFT", "stop_after_lap": 3.0},
    ]


def test_service_remove_strategy_edit_rebuilds_then_deletes_empty_scenario():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"MEDIUM": 100.0, "HARD": 90.0, "SOFT": 85.0}
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    clean = base_by_compound[str(segment.compound).upper()]
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 5,
                "Team": ["Team A"] * 5,
                "LapNumber": [1, 2, 3, 4, 5],
                "LapTime": [100.0] * 5,
                "Compound": ["MEDIUM"] * 5,
                "TyreLife": [1, 2, 3, 4, 5],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=3,
        compound="SOFT",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )

    service.remove_strategy_edit(scenario.scenario_id, 1)
    remaining = service.scenarios.active_scenarios()[0]
    delta = service.build_strategy_delta_data()

    assert [edit.stop_after_lap for edit in remaining.edits] == [3]
    assert delta["lap"].tolist() == [4, 5]

    service.remove_strategy_edit(scenario.scenario_id, 3)

    assert service.scenarios.active_scenarios() == []
    assert service.build_strategy_delta_data().empty


def test_service_observed_comparison_uses_actual_future_lap_times():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [90.0 for _ in laps],
                    "pit_loss_s": [pit_losses.get(lap, 0.0) for lap in laps],
                    "predicted_total_lap_time_s": [
                        90.0 + pit_losses.get(lap, 0.0) for lap in laps
                    ],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA"],
                "Team": ["Team A", "Team A", "Team A"],
                "LapNumber": [1, 2, 3],
                "LapTime": [80.0, 100.0, 95.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM"],
                "TyreLife": [1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_OBSERVED_REAL_DATA,
    )
    delta = service.build_strategy_delta_data()

    assert scenario.comparison_mode == COMPARISON_MODE_OBSERVED_REAL_DATA
    assert delta[
        [
            "lap",
            "observed_real_lap_time_s",
            "sim_model_lap_time_s",
            "baseline_model_lap_time_s",
            "model_delta_lap_s",
            "sim_display_lap_time_s",
        ]
    ].to_dict("records") == [
        {
            "lap": 2,
            "observed_real_lap_time_s": 100.0,
            "sim_model_lap_time_s": 110.0,
            "baseline_model_lap_time_s": 90.0,
            "model_delta_lap_s": 20.0,
            "sim_display_lap_time_s": 120.0,
        },
        {
            "lap": 3,
            "observed_real_lap_time_s": 95.0,
            "sim_model_lap_time_s": 90.0,
            "baseline_model_lap_time_s": 90.0,
            "model_delta_lap_s": 0.0,
            "sim_display_lap_time_s": 95.0,
        },
    ]
    assert delta["cumulative_model_delta_s"].tolist() == [20.0, 20.0]


def test_pure_sim_suppresses_green_flag_observed_residual():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            pit_in_losses = {
                pit_stop.stop_after_lap: float(pit_stop.pit_in_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            pit_out_losses = {
                pit_stop.first_lap_after_stop: float(pit_stop.pit_out_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 100.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_in_loss = pit_in_losses.get(lap, 0.0)
                    pit_out_loss = pit_out_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_losses.get(lap, 0.0),
                            "pit_in_loss_s": pit_in_loss,
                            "pit_out_loss_s": pit_out_loss,
                            "predicted_total_lap_time_s": clean + pit_in_loss + pit_out_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 4,
                "Team": ["Team A"] * 4,
                "LapNumber": [1, 2, 3, 4],
                "LapTime": [100.0, 100.0, 150.0, 100.0],
                "Compound": ["MEDIUM"] * 4,
                "TyreLife": [1, 2, 3, 4],
                "TrackStatus": ["1"] * 4,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_PURE_SIM,
    )
    delta = service.build_strategy_delta_data()
    noisy_lap = delta.loc[delta["lap"].eq(3)]

    assert scenario.comparison_mode == COMPARISON_MODE_PURE_SIM
    assert noisy_lap["race_domain_residual_s"].tolist() == [
        16.66666666666667,
        16.66666666666667,
        16.66666666666667,
    ]
    assert noisy_lap["display_residual_s"].tolist() == [0.0, 0.0, 0.0]
    assert noisy_lap["sim_display_sector_time_s"].tolist() == [30.0, 30.0, 30.0]


def test_pure_sim_suppresses_sc_vsc_observed_residual():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": 90.0,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": 90.0 + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 4,
                "Team": ["Team A"] * 4,
                "LapNumber": [1, 2, 3, 4],
                "LapTime": [90.0, 90.0, 150.0, 90.0],
                "Compound": ["MEDIUM"] * 4,
                "TyreLife": [1, 2, 3, 4],
                "TrackStatus": ["1", "1", "6", "1"],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    delta = service.build_strategy_delta_data()
    vsc_lap = delta.loc[delta["lap"].eq(3)]

    assert vsc_lap["race_domain_residual_s"].tolist() == [20.0, 20.0, 20.0]
    assert vsc_lap["display_residual_s"].tolist() == [0.0, 0.0, 0.0]
    assert vsc_lap["sim_display_sector_time_s"].tolist() == [30.0, 30.0, 30.0]


def test_strategy_delta_details_can_be_reference_relative():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            pit_in_losses = {
                pit_stop.stop_after_lap: float(pit_stop.pit_in_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            pit_out_losses = {
                pit_stop.first_lap_after_stop: float(pit_stop.pit_out_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 100.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_in_loss = pit_in_losses.get(lap, 0.0)
                    pit_out_loss = pit_out_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_losses.get(lap, 0.0),
                            "pit_in_loss_s": pit_in_loss,
                            "pit_out_loss_s": pit_out_loss,
                            "predicted_total_lap_time_s": clean + pit_in_loss + pit_out_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 3 + ["BBB"] * 3,
                "Team": ["Team A"] * 3 + ["Team B"] * 3,
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [100.0, 100.0, 100.0, 95.0, 95.0, 95.0],
                "Compound": ["MEDIUM"] * 3 + ["HARD"] * 3,
                "TyreLife": [1, 2, 3, 1, 2, 3],
                "TrackStatus": ["1"] * 6,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    same_driver_delta = service.build_strategy_delta_data()
    reference_delta = service.build_strategy_delta_data(
        reference_driver="BBB",
        reference_mode="actual_pace",
    )

    assert same_driver_delta["cumulative_delta_s"].tail(1).iloc[0] == same_driver_delta[
        "cumulative_model_delta_s"
    ].tail(1).iloc[0]
    assert reference_delta["cumulative_delta_s"].tail(1).iloc[0] == reference_delta[
        "delta_to_reference_s"
    ].tail(1).iloc[0]
    assert [round(value, 6) for value in reference_delta["segment_delta_to_reference_s"].tolist()] == [
        1.666667,
        1.666667,
        11.666667,
        8.333333,
        -1.666667,
        -1.666667,
        -1.666667,
        -1.666667,
        -1.666667,
    ]
    assert reference_delta["cumulative_model_delta_s"].tail(1).iloc[0] != reference_delta[
        "cumulative_delta_s"
    ].tail(1).iloc[0]


def test_pit_after_lap_applies_inlap_s3_and_outlap_s1_losses():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_out_losses = {
                pit_stop.first_lap_after_stop: float(pit_stop.pit_out_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            pit_in_losses = {
                pit_stop.stop_after_lap: float(pit_stop.pit_in_loss_s or 0.0)
                for pit_stop in plan.pit_stops
            }
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_in_loss = pit_in_losses.get(lap, 0.0)
                    pit_out_loss = pit_out_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": 90.0,
                            "pit_loss_s": pit_in_loss + pit_out_loss,
                            "pit_in_loss_s": pit_in_loss,
                            "pit_out_loss_s": pit_out_loss,
                            "predicted_total_lap_time_s": 90.0 + pit_in_loss + pit_out_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA"] * 5 + ["BBB"] * 5,
                "Team": ["Team A"] * 5 + ["Team B"] * 5,
                "LapNumber": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5],
                "LapTime": [100.0] * 10,
                "Compound": ["MEDIUM"] * 5 + ["HARD"] * 5,
                "TyreLife": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5],
                "TrackStatus": ["1"] * 10,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=4,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    delta = service.build_strategy_delta_data(reference_driver="BBB", reference_mode="actual_pace")
    sim = service.build_plot_data("BBB").loc[
        lambda data: (data["scenario_id"] == scenario.scenario_id)
        & data["plot_role"].eq("simulation")
        & data["is_simulated"].fillna(False).astype(bool)
    ]

    assert sim["lap"].min() == 4
    lap4 = delta.loc[delta["lap"].eq(4)]
    lap5 = delta.loc[delta["lap"].eq(5)]
    assert lap4["what_if_pit_in_loss_s"].tolist() == [0.0, 0.0, 10.0]
    assert lap4["what_if_pit_out_loss_s"].tolist() == [0.0, 0.0, 0.0]
    assert lap5["what_if_pit_in_loss_s"].tolist() == [0.0, 0.0, 0.0]
    assert lap5["what_if_pit_out_loss_s"].tolist() == [10.0, 0.0, 0.0]


def test_actual_pit_loss_estimation_uses_track_status_and_warns_on_missing_status():
    warnings = []
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "BBB"],
            "lap": [2, 3, 2],
            "pit_out": [True, True, True],
            "track_status": ["6", pd.NA, "1"],
        }
    )

    actual_pit_loss = estimate_actual_pit_loss_by_lap(
        laps,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
        warnings=warnings,
    )

    assert actual_pit_loss["actual_pit_loss_s"].tolist() == [10.0, 20.0, 20.0]
    assert warnings == [
        "Track status was unavailable for 1 actual pit event(s); normal pit-loss default was used."
    ]


def test_actual_pit_loss_estimation_splits_adjacent_pit_in_and_pit_out_laps():
    laps = pd.DataFrame(
        {
            "driver": ["AAA", "AAA", "AAA"],
            "lap": [40, 41, 42],
            "pit_in": [False, True, False],
            "pit_out": [False, False, True],
            "track_status": ["1", "1", "1"],
        }
    )

    actual_pit_loss = estimate_actual_pit_loss_by_lap(
        laps,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    assert actual_pit_loss["actual_pit_loss_s"].tolist() == [0.0, 10.0, 10.0]


def test_actual_pit_loss_estimation_accumulates_overlapping_pit_out_and_pit_in_lap():
    laps = pd.DataFrame(
        {
            "driver": ["HAD", "HAD", "HAD"],
            "lap": [1, 2, 3],
            "pit_in": [True, True, False],
            "pit_out": [False, True, True],
            "track_status": ["4", "4", "4"],
        }
    )

    actual_pit_loss = estimate_actual_pit_loss_by_lap(
        laps,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )

    assert actual_pit_loss["actual_pit_in_loss_s"].tolist() == [5.0, 5.0, 0.0]
    assert actual_pit_loss["actual_pit_out_loss_s"].tolist() == [0.0, 5.0, 5.0]
    assert actual_pit_loss["actual_pit_loss_s"].tolist() == [5.0, 10.0, 5.0]


def test_modelled_real_strategy_plan_uses_previous_inlap_and_current_outlap_for_observed_stop():
    driver_laps = pd.DataFrame(
        {
            "driver": ["HAD", "HAD", "HAD"],
            "lap": [1, 2, 3],
            "compound": ["HARD", "MEDIUM", "HARD"],
            "tyre_age": [1, 1, 1],
            "pit_out": [False, True, True],
        }
    )
    actual_pit_loss = pd.DataFrame(
        {
            "driver": ["HAD", "HAD", "HAD"],
            "lap": [1, 2, 3],
            "actual_pit_loss_s": [5.0, 10.0, 5.0],
            "actual_pit_in_loss_s": [5.0, 5.0, 0.0],
            "actual_pit_out_loss_s": [0.0, 5.0, 5.0],
        }
    )

    plan = build_modelled_real_strategy_plan(
        driver_laps,
        driver="HAD",
        team="Red Bull Racing",
        stop_after_lap=0,
        default_pit_loss_s=10.0,
        actual_pit_loss_by_lap=actual_pit_loss,
    )

    assert [
        (
            stop.stop_after_lap,
            stop.first_lap_after_stop,
            stop.pit_loss_s,
            stop.pit_in_loss_s,
            stop.pit_out_loss_s,
        )
        for stop in plan.pit_stops
    ] == [
        (1, 2, 10.0, 5.0, 5.0),
        (2, 3, 10.0, 5.0, 5.0),
    ]


def test_unavailable_model_rows_for_other_drivers_do_not_block_simulation():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            if plan.driver == "HUL" and str(segment.compound).upper() == "MEDIUM":
                raise ValueError("No strategy model row for HUL on MEDIUM")
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [90.0 for _ in laps],
                    "pit_loss_s": [pit_losses.get(lap, 0.0) for lap in laps],
                    "predicted_total_lap_time_s": [
                        90.0 + pit_losses.get(lap, 0.0) for lap in laps
                    ],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["ANT", "ANT", "ANT", "HUL", "HUL", "HUL"],
                "Team": ["Mercedes", "Mercedes", "Mercedes", "Sauber", "Sauber", "Sauber"],
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [90.0, 91.0, 92.0, 91.0, 92.0, 93.0],
                "Compound": ["HARD", "HARD", "HARD", "MEDIUM", "MEDIUM", "MEDIUM"],
                "TyreLife": [1, 2, 3, 1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="ANT",
        stop_after_lap=1,
        compound="MEDIUM",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    comparison = service.scenarios.active_comparisons()[0]

    assert scenario.driver == "ANT"
    assert comparison.warnings == []


def test_team_model_fallback_is_saved_and_reused_when_scenario_is_rebuilt():
    class FakeStrategyModel:
        def __init__(self):
            self.use_team_data_values = []

        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            use_team_data = bool(kwargs.get("use_team_data", False))
            self.use_team_data_values.append(use_team_data)
            if plan.driver == "ANT" and not use_team_data:
                raise MissingDriverStrategyModelError(
                    driver="ANT",
                    team="Mercedes",
                    compound=plan.segments[0].compound,
                    team_model_driver="RUS",
                )
            rows = []
            pit_losses = {
                stop.first_lap_after_stop: stop.pit_loss_s for stop in plan.pit_stops
            }
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": 90.0,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": 90.0 + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    fake_model = FakeStrategyModel()
    service.strategy_model = fake_model
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["ANT"] * 5,
                "Team": ["Mercedes"] * 5,
                "LapNumber": [1, 2, 3, 4, 5],
                "LapTime": [90.0, 90.0, 90.0, 90.0, 90.0],
                "Compound": ["MEDIUM"] * 5,
                "TyreLife": [1, 2, 3, 4, 5],
            }
        ),
        year=2026,
        round_number=11,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="ANT",
        stop_after_lap=2,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        use_team_model_data=True,
    )
    service.create_pit_stop_simulation(
        driver="ANT",
        stop_after_lap=3,
        compound="MEDIUM",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )

    rebuilt = service.scenarios.get(scenario.scenario_id)
    comparison = service.scenarios.active_comparisons()[0]
    assert rebuilt is not None
    assert rebuilt.use_team_model_data is True
    assert fake_model.use_team_data_values and all(fake_model.use_team_data_values)
    assert comparison.warnings == [
        "ANT: driver model data was unavailable; using Mercedes team data where needed."
    ]


def test_simulated_overlay_uses_strategy_delta_on_existing_cumulative_delta_plot():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [90.0 for _ in laps],
                    "pit_loss_s": [pit_losses.get(lap, 0.0) for lap in laps],
                    "predicted_total_lap_time_s": [
                        90.0 + pit_losses.get(lap, 0.0) for lap in laps
                    ],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [80.0, 100.0, 95.0, 82.0, 82.0, 82.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM", "HARD", "HARD", "HARD"],
                "TyreLife": [1, 2, 3, 1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_OBSERVED_REAL_DATA,
    )
    plot_data = service.build_plot_data("BBB")
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == scenario.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]

    assert simulation[["lap", "cumulative_time_s", "delta_s"]].to_dict("records") == [
        {"lap": 2, "cumulative_time_s": 200.0, "delta_s": 36.0},
        {"lap": 3, "cumulative_time_s": 295.0, "delta_s": 49.0},
    ]


def test_modelled_real_display_overlay_keeps_future_observed_pit_stop_unless_removed():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 100.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team A", "Team B", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 4, 1, 2, 3, 4],
                "LapTime": [100.0, 100.0, 120.0, 90.0, 90.0, 90.0, 90.0, 90.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD", "HARD", "HARD", "HARD", "HARD", "HARD"],
                "Stint": [1, 1, 2, 2, 1, 1, 1, 1],
                "TyreLife": [1, 2, 1, 2, 1, 2, 3, 4],
                "PitOutTime": [pd.NaT, pd.NaT, pd.Timestamp("2026-01-01"), pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    )
    plot_data = service.build_plot_data("BBB")
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == scenario.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]

    assert simulation[["lap", "cumulative_time_s", "delta_s"]].to_dict("records") == [
        {"lap": 2, "cumulative_time_s": 210.0, "delta_s": 30.0},
        {"lap": 3, "cumulative_time_s": 330.0, "delta_s": 60.0},
        {"lap": 4, "cumulative_time_s": 420.0, "delta_s": 60.0},
    ]


def test_modelled_real_display_overlay_removes_split_future_observed_pit_loss():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 100.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team A", "Team B", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 4, 1, 2, 3, 4],
                "LapTime": [100.0, 110.0, 110.0, 90.0, 90.0, 90.0, 90.0, 90.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD", "HARD", "HARD", "HARD", "HARD", "HARD"],
                "Stint": [1, 1, 2, 2, 1, 1, 1, 1],
                "TyreLife": [1, 2, 1, 2, 1, 2, 3, 4],
                "PitInTime": [pd.NaT, pd.Timestamp("2026-01-01"), pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT],
                "PitOutTime": [pd.NaT, pd.NaT, pd.Timestamp("2026-01-01"), pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT],
                "TrackStatus": ["1"] * 8,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    )
    delta = service.build_strategy_delta_data()
    plot_data = service.build_plot_data("BBB")
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == scenario.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]

    assert delta[["lap", "observed_actual_pit_loss_s", "observed_scaffold_lap_time_s"]].to_dict(
        "records"
    ) == [
        {"lap": 2, "observed_actual_pit_loss_s": 10.0, "observed_scaffold_lap_time_s": 100.0},
        {"lap": 3, "observed_actual_pit_loss_s": 10.0, "observed_scaffold_lap_time_s": 100.0},
        {"lap": 4, "observed_actual_pit_loss_s": 0.0, "observed_scaffold_lap_time_s": 90.0},
    ]
    assert delta[["lap", "baseline_pit_loss_s", "race_domain_residual_s"]].to_dict(
        "records"
    ) == [
        {"lap": 2, "baseline_pit_loss_s": 10.0, "race_domain_residual_s": 0.0},
        {"lap": 3, "baseline_pit_loss_s": 10.0, "race_domain_residual_s": 10.0},
        {"lap": 4, "baseline_pit_loss_s": 0.0, "race_domain_residual_s": 0.0},
    ]
    assert simulation[["lap", "cumulative_time_s", "delta_s"]].to_dict("records") == [
        {"lap": 2, "cumulative_time_s": 210.0, "delta_s": 30.0},
        {"lap": 3, "cumulative_time_s": 320.0, "delta_s": 50.0},
        {"lap": 4, "cumulative_time_s": 410.0, "delta_s": 50.0},
    ]


def test_remove_observed_pit_edit_creates_no_pit_strategy_from_actual_marker_lap():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 100.0}
            for segment in plan.segments:
                clean = base_by_compound[str(segment.compound).upper()]
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team A", "Team B", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 4, 1, 2, 3, 4],
                "LapTime": [100.0, 100.0, 120.0, 90.0, 90.0, 90.0, 90.0, 90.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD", "HARD", "HARD", "HARD", "HARD", "HARD"],
                "Stint": [1, 1, 2, 2, 1, 1, 1, 1],
                "TyreLife": [1, 2, 1, 2, 1, 2, 3, 4],
                "PitOutTime": [pd.NaT, pd.NaT, pd.Timestamp("2026-01-01"), pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT],
                "TrackStatus": ["1"] * 8,
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_remove_observed_pit_simulation(
        driver="AAA",
        first_lap_after_stop=3,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )
    edit = scenario.edits[0]
    delta = service.build_strategy_delta_data()
    plot_data = service.build_plot_data("BBB")
    tyre_events = service.build_tyre_event_data()
    removed_event = tyre_events.loc[
        (tyre_events["driver"] == "AAA") & (pd.to_numeric(tyre_events["lap"]) == 3)
    ].iloc[0]

    assert edit.edit_type == "remove_observed_pit"
    assert edit.stop_after_lap == 2
    assert edit.compound == "MEDIUM"
    assert edit.initial_tyre_age == 3
    assert edit.pit_loss_s == 20.0
    assert delta[["lap", "baseline_pit_loss_s", "what_if_pit_loss_s"]].to_dict("records") == [
        {"lap": 3, "baseline_pit_loss_s": 20.0, "what_if_pit_loss_s": 0.0},
        {"lap": 4, "baseline_pit_loss_s": 0.0, "what_if_pit_loss_s": 0.0},
    ]
    assert delta[["lap", "compound", "tyre_age"]].to_dict("records") == [
        {"lap": 3, "compound": "MEDIUM", "tyre_age": 3},
        {"lap": 4, "compound": "MEDIUM", "tyre_age": 4},
    ]
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == scenario.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]
    assert simulation[["lap", "cumulative_time_s", "delta_s"]].to_dict("records") == [
        {"lap": 3, "cumulative_time_s": 310.0, "delta_s": 40.0},
        {"lap": 4, "cumulative_time_s": 410.0, "delta_s": 50.0},
    ]
    assert bool(removed_event["is_removed_observed_pit"])
    assert removed_event["removed_pit_edit_id"] == edit.edit_id


def test_restore_removed_observed_pit_deletes_edit_and_observed_trajectory_returns():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [100.0 for _ in laps],
                    "pit_loss_s": [0.0 for _ in laps],
                    "predicted_total_lap_time_s": [100.0 for _ in laps],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA"],
                "Team": ["Team A", "Team A", "Team A"],
                "LapNumber": [1, 2, 3],
                "LapTime": [100.0, 100.0, 120.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD"],
                "Stint": [1, 1, 2],
                "TyreLife": [1, 2, 1],
                "PitOutTime": [pd.NaT, pd.NaT, pd.Timestamp("2026-01-01")],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_remove_observed_pit_simulation(
        driver="AAA",
        first_lap_after_stop=3,
        normal_pit_loss_s=20.0,
        sc_vsc_pit_loss_s=10.0,
    )
    service.remove_strategy_edit_by_id(scenario.scenario_id, scenario.edits[0].edit_id)
    plot_data = service.build_plot_data("AAA")

    assert service.scenarios.active_scenarios() == []
    assert not plot_data["is_simulated"].fillna(False).astype(bool).any()


def test_simulated_reference_recalculates_all_driver_deltas():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [90.0 for _ in laps],
                    "pit_loss_s": [pit_losses.get(lap, 0.0) for lap in laps],
                    "predicted_total_lap_time_s": [
                        90.0 + pit_losses.get(lap, 0.0) for lap in laps
                    ],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM", "HARD", "HARD", "HARD"],
                "TyreLife": [1, 2, 3, 1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    plot_data = service.build_plot_data("AAA")
    bbb_actual = plot_data.loc[
        (plot_data["driver"] == "BBB") & (plot_data["plot_role"] == "actual")
    ]

    assert bbb_actual[["lap", "delta_s"]].to_dict("records") == [
        {"lap": 1, "delta_s": 0.0},
        {"lap": 2, "delta_s": -20.0},
        {"lap": 3, "delta_s": -20.0},
    ]


def test_simulated_driver_delta_is_against_selected_reference_not_own_observed_delta():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            segment = plan.segments[0]
            laps = list(range(segment.start_lap, segment.end_lap + 1))
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            return pd.DataFrame(
                {
                    "driver": plan.driver,
                    "team": plan.team,
                    "lap": laps,
                    "compound": segment.compound,
                    "tyre_age": [segment.initial_tyre_age + index for index, _ in enumerate(laps)],
                    "predicted_clean_lap_time_s": [90.0 for _ in laps],
                    "pit_loss_s": [pit_losses.get(lap, 0.0) for lap in laps],
                    "predicted_total_lap_time_s": [
                        90.0 + pit_losses.get(lap, 0.0) for lap in laps
                    ],
                }
            )

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
                "Team": ["Team A", "Team A", "Team A", "Team B", "Team B", "Team B"],
                "LapNumber": [1, 2, 3, 1, 2, 3],
                "LapTime": [100.0, 100.0, 100.0, 95.0, 95.0, 95.0],
                "Compound": ["MEDIUM", "MEDIUM", "MEDIUM", "HARD", "HARD", "HARD"],
                "TyreLife": [1, 2, 3, 1, 2, 3],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    scenario = service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
    )
    plot_data = service.build_plot_data("BBB")
    simulation = plot_data.loc[
        (plot_data["scenario_id"] == scenario.scenario_id)
        & (plot_data["is_simulated"].fillna(False).astype(bool))
    ]

    assert simulation[["lap", "cumulative_time_s", "delta_s"]].to_dict("records") == [
        {"lap": 2, "cumulative_time_s": 220.0, "delta_s": 30.0},
        {"lap": 3, "cumulative_time_s": 320.0, "delta_s": 35.0},
    ]


def test_service_modelled_comparison_predicts_actual_remaining_strategy():
    class FakeStrategyModel:
        def predict_strategy_plan(self, **kwargs):
            plan = kwargs["strategy_plan"]
            rows = []
            pit_losses = {
                pit_stop.first_lap_after_stop: pit_stop.pit_loss_s
                for pit_stop in plan.pit_stops
            }
            base_by_compound = {"HARD": 90.0, "MEDIUM": 92.0}
            for segment in plan.segments:
                for offset, lap in enumerate(range(segment.start_lap, segment.end_lap + 1)):
                    clean = base_by_compound[str(segment.compound).upper()]
                    pit_loss = pit_losses.get(lap, 0.0)
                    rows.append(
                        {
                            "driver": plan.driver,
                            "team": plan.team,
                            "lap": lap,
                            "compound": segment.compound,
                            "tyre_age": segment.initial_tyre_age + offset,
                            "predicted_clean_lap_time_s": clean,
                            "pit_loss_s": pit_loss,
                            "predicted_total_lap_time_s": clean + pit_loss,
                        }
                    )
            return pd.DataFrame(rows)

    service = RacePlannerService()
    service.strategy_model = FakeStrategyModel()
    service.session = normalize_race_laps(
        pd.DataFrame(
            {
                "Driver": ["AAA", "AAA", "AAA", "AAA"],
                "Team": ["Team A", "Team A", "Team A", "Team A"],
                "LapNumber": [1, 2, 3, 4],
                "LapTime": [80.0, 120.0, 96.0, 97.0],
                "Compound": ["MEDIUM", "MEDIUM", "HARD", "HARD"],
                "Stint": [1, 1, 2, 2],
                "TyreLife": [1, 2, 1, 2],
                "PitOutTime": [pd.NaT, pd.NaT, pd.Timestamp("2026-01-01"), pd.NaT],
            }
        ),
        year=2026,
        round_number=1,
        session_name="R",
    )

    service.create_pit_stop_simulation(
        driver="AAA",
        stop_after_lap=1,
        compound="HARD",
        initial_tyre_age=0,
        pit_loss_s=20.0,
        comparison_mode=COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    )
    delta = service.build_strategy_delta_data()

    assert delta[["lap", "what_if_compound", "baseline_compound"]].to_dict("records") == [
        {"lap": 2, "what_if_compound": "HARD", "baseline_compound": "MEDIUM"},
        {"lap": 3, "what_if_compound": "HARD", "baseline_compound": "HARD"},
        {"lap": 4, "what_if_compound": "HARD", "baseline_compound": "HARD"},
    ]
    assert delta["baseline_pit_loss_s"].tolist() == [0.0, 20.0, 0.0]
    assert delta["what_if_pit_loss_s"].tolist() == [20.0, 20.0, 0.0]
    assert delta["lap_delta_s"].tolist() == [18.0, 0.0, 0.0]
    assert delta["cumulative_delta_s"].tolist() == [18.0, 18.0, 18.0]


def test_strategy_model_adapter_predicts_plan_with_pit_losses(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    prefix = "race_performance_2026_7_R"
    (source / f"{prefix}_summary_baseline_pace.csv").write_text(
        "Team,Driver,P10,Median,P90,SampleCount,WeightSum\nMercedes,HAM,80,81,82,10,5\n"
    )
    (source / f"{prefix}_summary_fuel_rate.csv").write_text(
        "P10,Median,P90,SampleCount,WeightSum\n0.01,0.00,0.09,10,5\n"
    )
    (source / f"{prefix}_summary_track_rate.csv").write_text(
        "P10,Median,P90,SampleCount,WeightSum\n0.00,0.00,0.04,10,5\n"
    )
    (source / f"{prefix}_summary_compound_delta.csv").write_text(
        "Compound,CompoundDeltaReference,P10,Median,P90,SampleCount,WeightSum\n"
        "HARD,HARD,0,0,0,10,5\nMEDIUM,HARD,-0.5,-0.2,0,10,5\n"
    )
    (source / f"{prefix}_summary_team_compound_degradation.csv").write_text(
        "Team,Compound,P10,Median,P90,SampleCount,WeightSum\n"
        "Mercedes,HARD,0.02,0.00,0.20,10,5\n"
        "Mercedes,MEDIUM,0.04,0.00,0.25,10,5\n"
    )
    (source / f"{prefix}_sample_parameters.csv").write_text(
        "SampleId,FuelRateSecondsPerLap,TrackRateSecondsPerLap,FuelRef,RaceLapRef,TyreAgeRef\n"
        "0,0.0,0.0,0.0,10.0,0.0\n"
    )
    (source / f"{prefix}_clean_laps.csv").write_text(
        "Driver,Team,LapNumber,TyreAgeLaps,FuelProxyLapsRemaining\n"
        "HAM,Mercedes,10,3,40\nHAM,Mercedes,20,5,30\n"
    )
    adapter = WoStrategyRaceModelAdapter(data_root=tmp_path / "data", source_cache_dir=source)
    plan = StrategyPlan(
        driver="HAM",
        team="Mercedes",
        start_after_lap=1,
        end_lap=3,
        segments=[StintSegment(start_lap=2, end_lap=3, compound="HARD", initial_tyre_age=0)],
        pit_stops=[
            PlannedPitStop(
                stop_after_lap=1,
                first_lap_after_stop=2,
                pit_loss_s=20.0,
                pit_in_loss_s=0.0,
                pit_out_loss_s=20.0,
            )
        ],
    )

    predicted = adapter.predict_strategy_plan(
        year=2026,
        round_number=7,
        session_name="R",
        strategy_plan=plan,
    )

    assert predicted["pit_loss_s"].tolist() == [20.0, 0.0]
    assert predicted["pit_in_loss_s"].tolist() == [0.0, 0.0]
    assert predicted["pit_out_loss_s"].tolist() == [20.0, 0.0]
    assert predicted["predicted_total_lap_time_s"].tolist() == [101.0, 81.0]
