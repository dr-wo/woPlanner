from __future__ import annotations

import pandas as pd

from woplanner.normalize import normalize_race_laps
from woplanner.service import (
    RacePlannerService,
    assign_driver_styles,
    default_reference_driver,
    latest_driver_order,
    build_pit_window_data,
    build_reference_delta_data,
    build_tyre_event_data,
    build_lap_delta_guide_data,
    add_driver_ranks,
    starting_driver_order,
)
from woplanner.strategy_model import MissingStrategyModelError, WoStrategyRaceModelAdapter


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

    assert styled.loc[styled["driver"] == "HAM", "colour"].iloc[0] == "#27F4D2"
    assert styled.loc[styled["driver"] == "HAM", "line_style"].iloc[0] == "solid"
    assert styled.loc[styled["driver"] == "RUS", "line_style"].iloc[0] == "dash"
    assert styled.loc[styled["driver"] == "VER", "colour"].iloc[0] == "#3671C6"


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
    assert simulated["lap_time_s"].tolist() == [108.0, 88.0]
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
        second.scenario_id
    ]
    assert first.scenario_id != second.scenario_id
