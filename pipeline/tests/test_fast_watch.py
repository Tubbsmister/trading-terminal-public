"""Tests for the fast position-watcher trip-wire."""

from __future__ import annotations

from pipeline.fast_watch import price_moved_against


def test_adverse_move_trips_on_absolute():
    tripped, why = price_moved_against(0.12, 0.40, abs_thresh=0.08, rel_thresh=0.99)
    assert tripped
    assert "fell" in why


def test_adverse_move_trips_on_relative():
    # 0.50 -> 0.44 is 6 cents (below 0.08 abs) but 12% (>= 10% rel) -> trips on rel
    tripped, _ = price_moved_against(0.44, 0.50, abs_thresh=0.08, rel_thresh=0.10)
    assert tripped


def test_small_move_does_not_trip():
    tripped, _ = price_moved_against(0.49, 0.50, abs_thresh=0.08, rel_thresh=0.15)
    assert not tripped


def test_favorable_move_never_trips():
    # our side rose (0.40 -> 0.55) -> we're winning, no alert
    tripped, why = price_moved_against(0.55, 0.40, abs_thresh=0.08, rel_thresh=0.15)
    assert not tripped
    assert "favorable" in why


def test_missing_prices_never_trip():
    assert price_moved_against(None, 0.4)[0] is False
    assert price_moved_against(0.4, None)[0] is False
    assert price_moved_against(0.4, 0.0)[0] is False       # baseline <= 0 guarded


def test_fable_case_would_have_tripped():
    """The real Fable collapse (0.415 last review -> 0.12) must trip decisively."""
    tripped, _ = price_moved_against(0.12, 0.415)
    assert tripped


def test_tail_dust_does_not_trip():
    """Near resolution, 0.009 -> 0.002 is 84% but 0.7c abs — must NOT fire (dust)."""
    tripped, why = price_moved_against(0.002, 0.009)
    assert not tripped
    assert "dust" in why


def test_relative_trip_needs_min_abs_floor():
    # same 15% relative, but tiny absolute below the 0.02 floor -> no trip
    assert price_moved_against(0.085, 0.10, abs_thresh=0.5, rel_thresh=0.15,
                               min_rel_abs=0.02)[0] is False
    # bump the absolute over the floor -> trips
    assert price_moved_against(0.40, 0.50, abs_thresh=0.5, rel_thresh=0.15,
                               min_rel_abs=0.02)[0] is True
