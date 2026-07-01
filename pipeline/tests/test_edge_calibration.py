"""
Tests for pipeline.edge_calibration — the deterministic "math floor": per-source
trust shrinkage toward price (anti-hallucination) + best-of-N selection deflation.

The invariant under test everywhere: calibration can only REDUCE conviction/edge,
never increase it, and arb is always exempt.

Runnable two ways:
  pytest pipeline/tests/test_edge_calibration.py
  python  pipeline/tests/test_edge_calibration.py
"""

from __future__ import annotations

import math
import os
import sys
import traceback
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline import edge_calibration as ec
from pipeline.models import Opportunity
from pipeline.sizing import ev_per_dollar


def _opp(source="llm", price=0.30, fair=0.60, conf=0.8, edge=None) -> Opportunity:
    """A candidate where, by construction, fair > price (positive EV)."""
    if edge is None:
        edge = ev_per_dollar(price, fair)  # pre-cost EV/$ for a clean numeric check
    return Opportunity(
        market_id="m1", venue="poly", question="q?", side="YES",
        market_price=price, fair_prob=fair, edge=edge,
        edge_source=source, confidence=conf,
    )


# ─── effective_trust ─────────────────────────────────────────────────────────

def test_trust_capped_by_source_ceiling():
    """An LLM screaming confidence 1.0 still only earns the 0.60 llm ceiling."""
    assert abs(ec.effective_trust("llm", 1.0) - 0.60) < 1e-9
    # calibration ceiling 0.75 * conf 0.5 = 0.375
    assert abs(ec.effective_trust("calibration", 0.5) - 0.375) < 1e-9


def test_trust_defaults_and_clamps():
    # missing confidence -> DEFAULT_CONFIDENCE (0.5); llm ceiling 0.6 * 0.5 = 0.3
    assert abs(ec.effective_trust("llm", None) - 0.30) < 1e-9
    # unknown source -> DEFAULT_TRUST; confidence >1 clamped to 1
    assert abs(ec.effective_trust("mystery", 5.0) - ec.DEFAULT_TRUST) < 1e-9
    # result always in [0,1]
    assert 0.0 <= ec.effective_trust("llm", -3.0) <= 1.0


# ─── shrink_fair ─────────────────────────────────────────────────────────────

def test_shrink_fair_endpoints_and_midpoint():
    # trust 1.0 -> unchanged; trust 0.0 -> collapses to price; 0.5 -> midpoint
    assert abs(ec.shrink_fair(0.30, 0.60, 1.0) - 0.60) < 1e-9
    assert abs(ec.shrink_fair(0.30, 0.60, 0.0) - 0.30) < 1e-9
    assert abs(ec.shrink_fair(0.30, 0.60, 0.5) - 0.45) < 1e-9


def test_shrink_fair_never_crosses_price():
    """Shrunk fair stays within [price, raw_fair] for a positive-EV candidate."""
    for trust in (0.0, 0.25, 0.6, 1.0):
        s = ec.shrink_fair(0.30, 0.60, trust)
        assert 0.30 - 1e-12 <= s <= 0.60 + 1e-12, (trust, s)


# ─── selection_haircut ───────────────────────────────────────────────────────

def test_haircut_zero_for_singleton_and_arb():
    assert ec.selection_haircut("llm", 1) == 0.0
    assert ec.selection_haircut("llm", 0) == 0.0
    assert ec.selection_haircut("arb", 500) == 0.0   # arb noise is 0


def test_haircut_matches_formula_and_grows_with_n():
    expected = ec.SCAN_NOISE["llm"] * ec.DEFLATE_K * math.sqrt(2.0 * math.log(50))
    assert abs(ec.selection_haircut("llm", 50) - expected) < 1e-12
    # more candidates screened -> bigger discount
    assert ec.selection_haircut("llm", 1000) > ec.selection_haircut("llm", 10)


# ─── calibrate_one ───────────────────────────────────────────────────────────

def test_calibrate_one_only_reduces_edge_and_shrinks_fair():
    o = _opp(source="llm", price=0.30, fair=0.60, conf=0.8)
    raw_edge, raw_fair = o.edge, o.fair_prob
    ec.calibrate_one(o, n_candidates=20)
    assert o.edge < raw_edge, "edge must only ever shrink"
    assert o.market_price <= o.fair_prob < raw_fair, "fair must move toward price"
    cal = o.detail["calibration"]
    assert cal["raw_fair_prob"] == round(raw_fair, 6)
    assert cal["raw_edge"] == round(raw_edge, 6)
    assert cal["selection_haircut"] > 0


def test_calibrate_one_numeric_coherence():
    """edge after = raw_edge - (raw_fair-eff_fair)/price - haircut, exactly."""
    o = _opp(source="thesis", price=0.40, fair=0.70, conf=0.5)
    raw_edge, raw_fair, price = o.edge, o.fair_prob, o.market_price
    trust = ec.effective_trust("thesis", 0.5)
    eff_fair = ec.shrink_fair(price, raw_fair, trust)
    haircut = ec.selection_haircut("thesis", 8)
    expected_edge = raw_edge - (raw_fair - eff_fair) / price - haircut
    ec.calibrate_one(o, n_candidates=8)
    assert abs(o.fair_prob - round(eff_fair, 6)) < 1e-9
    assert abs(o.edge - round(expected_edge, 6)) < 1e-9


def test_lower_confidence_means_more_shrink():
    hi = _opp(source="llm", price=0.30, fair=0.60, conf=0.9)
    lo = _opp(source="llm", price=0.30, fair=0.60, conf=0.2)
    ec.calibrate_one(hi, 10)
    ec.calibrate_one(lo, 10)
    assert lo.fair_prob < hi.fair_prob, "less confident -> shrunk harder toward price"
    assert lo.edge < hi.edge


def test_arb_is_exempt():
    o = _opp(source="arb", price=0.50, fair=0.55, conf=0.9)
    raw_edge, raw_fair = o.edge, o.fair_prob
    ec.calibrate_one(o, n_candidates=100)
    assert o.edge == raw_edge and o.fair_prob == raw_fair
    assert "calibration" not in (o.detail or {})


# ─── calibrate (batch) ───────────────────────────────────────────────────────

def test_calibrate_batch_excludes_arb_from_n_and_skips_arb():
    arb_o = _opp(source="arb", price=0.50, fair=0.55, conf=0.9)
    a = _opp(source="llm", price=0.30, fair=0.60, conf=0.8)
    b = _opp(source="calibration", price=0.20, fair=0.40, conf=0.5)
    ec.calibrate([arb_o, a, b])
    # n_candidates recorded should be 2 (the two non-arb), not 3
    assert a.detail["calibration"]["n_candidates"] == 2
    assert b.detail["calibration"]["n_candidates"] == 2
    # arb untouched
    assert arb_o.edge == _opp(source="arb", price=0.50, fair=0.55).edge


def test_calibrate_disabled_is_passthrough():
    prev = os.environ.get("PIPELINE_EDGE_CALIBRATION")
    os.environ["PIPELINE_EDGE_CALIBRATION"] = "0"
    try:
        o = _opp(source="llm", price=0.30, fair=0.60, conf=0.8)
        raw_edge, raw_fair = o.edge, o.fair_prob
        ec.calibrate([o])
        assert o.edge == raw_edge and o.fair_prob == raw_fair
        assert "calibration" not in (o.detail or {})
    finally:
        if prev is None:
            os.environ.pop("PIPELINE_EDGE_CALIBRATION", None)
        else:
            os.environ["PIPELINE_EDGE_CALIBRATION"] = prev


def test_calibrate_empty_and_idempotent_reference():
    assert ec.calibrate([]) == []
    o = _opp()
    out = ec.calibrate([o])
    assert out[0] is o, "returns the same list/objects (mutated in place)"


# ─── __main__ runner (pytest not required) ──────────────────────────────────

if __name__ == "__main__":
    _tests = [
        test_trust_capped_by_source_ceiling,
        test_trust_defaults_and_clamps,
        test_shrink_fair_endpoints_and_midpoint,
        test_shrink_fair_never_crosses_price,
        test_haircut_zero_for_singleton_and_arb,
        test_haircut_matches_formula_and_grows_with_n,
        test_calibrate_one_only_reduces_edge_and_shrinks_fair,
        test_calibrate_one_numeric_coherence,
        test_lower_confidence_means_more_shrink,
        test_arb_is_exempt,
        test_calibrate_batch_excludes_arb_from_n_and_skips_arb,
        test_calibrate_disabled_is_passthrough,
        test_calibrate_empty_and_idempotent_reference,
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
