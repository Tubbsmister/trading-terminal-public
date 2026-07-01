"""
Tests for pipeline.sizing — full_kelly_fraction, ev_per_dollar, KellySizer.size.

Runnable two ways:
  pytest pipeline/tests/test_sizing.py     (standard test runner)
  python  pipeline/tests/test_sizing.py    (plain script, no pytest required)
"""

from __future__ import annotations

import math
import sys
import traceback
from pathlib import Path

# Ensure the repo root is in sys.path so `pipeline` is importable when this
# file is executed directly: python pipeline/tests/test_sizing.py
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    import pytest  # noqa: F401  (not used directly; just checked for availability)
    _HAS_PYTEST = True
except ImportError:
    _HAS_PYTEST = False

from pipeline.sizing import full_kelly_fraction, ev_per_dollar, KellySizer


# ─── full_kelly_fraction ────────────────────────────────────────────────────

def test_full_kelly_known_values():
    """Verify the formula f* = (q - price) / (1 - price) for two known cases."""
    # price=0.5, fair=0.6 → (0.6-0.5)/(1-0.5) = 0.1/0.5 = 0.20
    result = full_kelly_fraction(0.5, 0.6)
    assert abs(result - 0.20) < 1e-9, f"Expected 0.20, got {result}"

    # price=0.10, fair=0.15 → 0.05/0.90 = 0.05556...
    expected = 0.05 / 0.90
    result = full_kelly_fraction(0.10, 0.15)
    assert abs(result - expected) < 1e-9, f"Expected {expected}, got {result}"


def test_full_kelly_no_edge():
    """Zero or negative edge → 0.0 (floored)."""
    # exact zero edge
    assert full_kelly_fraction(0.5, 0.5) == 0.0, "zero edge should return 0"
    # negative edge (fair < price) → floored to 0
    assert full_kelly_fraction(0.5, 0.45) == 0.0, "negative edge should return 0"
    # zero edge at a different price
    assert full_kelly_fraction(0.6, 0.6) == 0.0, "zero edge at 0.6 should return 0"


def test_full_kelly_invalid_price():
    """Price outside the open interval (0, 1) → 0.0."""
    assert full_kelly_fraction(0.0, 0.5) == 0.0,  "price=0.0 is invalid"
    assert full_kelly_fraction(1.0, 0.5) == 0.0,  "price=1.0 is invalid"
    assert full_kelly_fraction(-0.1, 0.5) == 0.0, "negative price is invalid"


# ─── ev_per_dollar ──────────────────────────────────────────────────────────

def test_ev_per_dollar():
    """ev = q/price - 1 with special case for price=0."""
    # 0.6/0.5 - 1 = 0.2
    assert abs(ev_per_dollar(0.5, 0.6) - 0.2) < 1e-9

    # 0.15/0.10 - 1 = 0.5
    assert abs(ev_per_dollar(0.1, 0.15) - 0.5) < 1e-9

    # price=0 → guard returns 0 rather than dividing by zero
    assert ev_per_dollar(0.0, 0.5) == 0.0


# ─── KellySizer.size ────────────────────────────────────────────────────────

def test_kelly_sizer_quarter_kelly():
    """Quarter-Kelly with bankroll=1000, price=0.5, fair=0.6.

    full Kelly = 0.20; scaled = 0.20 * 0.25 = 0.05 — exactly at the max_fraction
    cap.  The implementation uses a strict `>` test (frac > max_fraction), so
    when frac == max_fraction nothing is clamped and capped_by stays None.
    """
    s = KellySizer(bankroll=1000.0, multiplier=0.25, max_fraction=0.05)
    r = s.size(0.5, 0.6)

    assert abs(r.full_fraction - 0.20) < 1e-6,  f"full_fraction: {r.full_fraction}"
    assert abs(r.fraction - 0.05)      < 1e-6,  f"fraction: {r.fraction}"
    assert abs(r.stake - 50.0)         < 1e-6,  f"stake: {r.stake}"
    assert abs(r.contracts - 100.0)    < 1e-6,  f"contracts: {r.contracts}"
    # frac == max_fraction exactly (not strictly greater), so no capping occurred
    assert r.capped_by is None, (
        f"Expected capped_by=None when frac==max_fraction, got {r.capped_by!r}"
    )


def test_kelly_sizer_max_fraction_cap():
    """When scaled Kelly strictly exceeds max_fraction, stake is capped.

    price=0.3, fair=0.7: full ≈ 0.5714; scaled ≈ 0.1429 > 0.05 → capped.
    """
    s = KellySizer(bankroll=1000.0, multiplier=0.25, max_fraction=0.05)
    r = s.size(0.3, 0.7)

    assert abs(r.fraction - 0.05) < 1e-6,  f"fraction: {r.fraction}"
    assert abs(r.stake - 50.0)    < 1e-6,  f"stake: {r.stake}"
    assert r.capped_by == "max_fraction", f"Expected capped_by='max_fraction', got {r.capped_by!r}"


def test_kelly_sizer_min_stake_floor():
    """A tiny edge that yields sub-$1 stake is bumped to min_stake.

    bankroll=100, price=0.5, fair=0.501:
      full = 0.001/0.5 = 0.002; scaled = 0.002*0.25 = 0.0005
      stake = 0.0005*100 = $0.05 < min_stake=$1.0 → bumped to $1.0
    """
    s = KellySizer(bankroll=100.0, multiplier=0.25, max_fraction=0.05, min_stake=1.0)
    r = s.size(0.5, 0.501)

    assert r.stake == 1.0,             f"stake should be min_stake=1.0, got {r.stake}"
    assert r.capped_by == "min_stake", f"Expected capped_by='min_stake', got {r.capped_by!r}"
    assert abs(r.fraction - 0.01) < 1e-6,  f"fraction: {r.fraction}"   # 1.0/100
    assert abs(r.contracts - 2.0) < 1e-6,  f"contracts: {r.contracts}" # 1.0/0.5


def test_kelly_sizer_no_edge():
    """Zero edge → zero stake, zero fraction, zero contracts, no cap label."""
    s = KellySizer(bankroll=1000.0)
    r = s.size(0.5, 0.5)

    assert r.stake     == 0.0,  f"stake: {r.stake}"
    assert r.fraction  == 0.0,  f"fraction: {r.fraction}"
    assert r.contracts == 0.0,  f"contracts: {r.contracts}"
    assert r.capped_by is None, f"capped_by: {r.capped_by}"


# ─── __main__ runner (pytest not required) ──────────────────────────────────

if __name__ == "__main__":
    _tests = [
        test_full_kelly_known_values,
        test_full_kelly_no_edge,
        test_full_kelly_invalid_price,
        test_ev_per_dollar,
        test_kelly_sizer_quarter_kelly,
        test_kelly_sizer_max_fraction_cap,
        test_kelly_sizer_min_stake_floor,
        test_kelly_sizer_no_edge,
    ]
    _failed = 0
    for _fn in _tests:
        try:
            _fn()
            print(f"PASS {_fn.__name__}")
        except Exception as _e:
            print(f"FAIL {_fn.__name__} {_e}")
            traceback.print_exc()
            _failed += 1
    sys.exit(1 if _failed else 0)
