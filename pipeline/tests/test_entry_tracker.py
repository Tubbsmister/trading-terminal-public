"""
Tests for the thesis entry tracker's edge math (the core trigger logic).

The invariant — and the whole point of the tracker: a sound thesis with a THIN edge at the
current price must NOT fire, but the SAME thesis must fire when the price swings in our favor
(the crowd over-weights the side we bet against). Validated on the real Fable-5 shape: NO
thesis (fair P(YES)=0.05), thin at the normal price, screaming buy when YES spiked to ~0.70.

Runnable two ways:
  pytest pipeline/tests/test_entry_tracker.py
  python  pipeline/tests/test_entry_tracker.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline.entry_tracker import entry_edge
from pipeline.thesis import MIN_EDGE


def test_fable_scenario_thin_then_fires():
    """NO thesis, fair P(YES)=0.05 (NO worth ~0.95)."""
    # Normal price: YES 0.07 -> NO 0.93. ~2c gross edge, eaten by cost -> below the bar.
    edge_normal, price_normal = entry_edge("NO", 0.05, yes_price=0.07)
    assert price_normal == 0.93
    assert edge_normal < MIN_EDGE, f"thin edge should not fire, got {edge_normal}"

    # YES spikes to 0.70 -> NO collapses to 0.30. Huge edge vs the 0.95 fair value -> fires.
    edge_spike, price_spike = entry_edge("NO", 0.05, yes_price=0.70)
    assert price_spike == 0.30
    assert edge_spike >= MIN_EDGE, f"favorable swing should fire, got {edge_spike}"
    assert edge_spike > edge_normal


def test_yes_side_symmetric():
    """YES thesis, fair P(YES)=0.80. Cheap when price dips."""
    edge_fair, _ = entry_edge("YES", 0.80, yes_price=0.78)   # ~fairly priced -> thin
    edge_dip, _ = entry_edge("YES", 0.80, yes_price=0.55)    # price dipped -> our buy is cheap
    assert edge_dip > edge_fair
    assert edge_dip >= MIN_EDGE
    assert edge_fair < MIN_EDGE


def test_uses_explicit_no_price_when_given():
    # if a real no_price is supplied (not just 1-yes), it is used for the NO side
    edge, price = entry_edge("NO", 0.05, yes_price=0.65, no_price=0.30)
    assert price == 0.30


def test_degenerate_price_returns_negative_edge():
    edge, _ = entry_edge("YES", 0.5, yes_price=0.0)
    assert edge < 0


def test_edge_monotonic_in_price_for_no_side():
    """For a NO bet, a lower our-side price (cheaper) is always a better edge."""
    e_hi, _ = entry_edge("NO", 0.05, yes_price=0.10)  # NO=0.90 (expensive)
    e_lo, _ = entry_edge("NO", 0.05, yes_price=0.50)  # NO=0.50 (cheap)
    assert e_lo > e_hi


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
