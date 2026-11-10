"""The dwell rule, held to the owner's own examples (2026-11-10) and to the two bounds it promises."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arrt_player.dwell import CEILING_SECONDS, FLOOR_SECONDS, DwellLimits, DwellLimitsInvalid, dwell_seconds, limits_problem

THIRTY_SIXTY = DwellLimits(minimum=30, maximum=60)


def test_a_long_clip_is_cut_at_the_maximum() -> None:
    """The owner's first example: a two-minute clip, minimum 30, maximum 60, moves on after 60 s."""
    assert dwell_seconds(120, THIRTY_SIXTY) == 60


def test_a_short_clip_plays_whole_times_until_it_reaches_the_minimum() -> None:
    """The owner's second example: an 18 s clip plays twice, 36 s, to get above 30."""
    assert dwell_seconds(18, THIRTY_SIXTY) == 36


def test_a_second_play_that_would_pass_the_maximum_is_cut_partway() -> None:
    """25 s needs two plays to reach 30, and 50 is past a maximum of 40: cut at 40, as a long clip is."""
    assert dwell_seconds(25, DwellLimits(minimum=30, maximum=40)) == 40


def test_a_clip_exactly_the_minimum_plays_once() -> None:
    assert dwell_seconds(30, THIRTY_SIXTY) == 30


def test_a_clip_that_divides_the_minimum_plays_exactly_enough_times() -> None:
    """10 s into 30 is three plays, not four: the count is the smallest that reaches the minimum."""
    assert dwell_seconds(10, THIRTY_SIXTY) == 30


@pytest.mark.parametrize("duration", [None, 0.0, -1.0, math.nan, math.inf])
def test_a_still_or_a_length_mpv_cannot_give_shows_for_the_minimum(duration: float | None) -> None:
    """mpv reports 0.0 for an image (measured, mpv 0.40); none, negative or not finite is no length at all."""
    assert dwell_seconds(duration, DwellLimits(minimum=12, maximum=60)) == 12


@given(
    duration=st.floats(min_value=0.05, max_value=7200, allow_nan=False),
    minimum=st.floats(min_value=FLOOR_SECONDS, max_value=CEILING_SECONDS),
    spread=st.floats(min_value=0, max_value=CEILING_SECONDS),
)
def test_a_clip_never_passes_the_maximum_and_never_stops_short_unless_cut(duration: float, minimum: float, spread: float) -> None:
    maximum = min(minimum + spread, CEILING_SECONDS)
    limits = DwellLimits(minimum=minimum, maximum=maximum)
    shown = dwell_seconds(duration, limits)
    assert shown <= maximum
    if shown < maximum:
        # Not cut, so it ended on a whole play and reached the minimum.
        assert shown >= minimum - 1e-9
        assert math.isclose(shown / duration, round(shown / duration), abs_tol=1e-6)


@pytest.mark.parametrize(
    ("minimum", "maximum", "says"),
    [
        (FLOOR_SECONDS - 1, 60, "at least"),
        (30, CEILING_SECONDS + 1, "at most"),
        (61, 60, "not be more than"),
        (math.nan, 60, "numbers of seconds"),
        (30, math.inf, "numbers of seconds"),
    ],
)
def test_limits_the_page_must_refuse_are_refused_with_a_reason(minimum: float, maximum: float, says: str) -> None:
    problem = limits_problem(minimum, maximum)
    assert problem is not None
    assert says in problem
    with pytest.raises(DwellLimitsInvalid, match=says):
        DwellLimits(minimum=minimum, maximum=maximum)


def test_equal_limits_and_the_extremes_are_accepted() -> None:
    assert limits_problem(30, 30) is None
    assert limits_problem(FLOOR_SECONDS, CEILING_SECONDS) is None
