import pytest

from woplanner.gui import expanded_size_for_aspect, symmetrically_expanded_ranges


def test_expanded_size_for_4k_aspect_expands_a_tall_source() -> None:
    width, height = expanded_size_for_aspect(1200, 800, 3840, 2160)

    assert (width, height) == (1424, 801)
    assert width >= 1200 and height >= 800
    assert width * 2160 == height * 3840


def test_expanded_size_for_4k_aspect_expands_a_wide_source() -> None:
    width, height = expanded_size_for_aspect(1800, 900, 3840, 2160)

    assert (width, height) == (1808, 1017)
    assert width >= 1800 and height >= 900
    assert width * 2160 == height * 3840


def test_ranges_expand_equally_on_both_sides() -> None:
    x_range, y_range = symmetrically_expanded_ranges(
        (10.0, 20.0),
        (-5.0, 5.0),
        (1000.0, 500.0),
        (1500.0, 500.0),
    )

    assert x_range == pytest.approx((7.5, 22.5))
    assert y_range == pytest.approx((-5.0, 5.0))


def test_ranges_never_contract_when_height_grows_more() -> None:
    x_range, y_range = symmetrically_expanded_ranges(
        (0.0, 8.0),
        (20.0, 30.0),
        (800.0, 600.0),
        (800.0, 900.0),
    )

    assert x_range == pytest.approx((0.0, 8.0))
    assert y_range == pytest.approx((17.5, 32.5))
