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
