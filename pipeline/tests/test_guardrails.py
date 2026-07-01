"""
Tests for pipeline.guardrails — evaluate, apply, kill_switch_engaged.

Runnable two ways:
  pytest pipeline/tests/test_guardrails.py     (standard test runner)
  python  pipeline/tests/test_guardrails.py    (plain script, no pytest required)
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure the repo root is in sys.path so `pipeline` is importable when this
# file is executed directly: python pipeline/tests/test_guardrails.py
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    import pytest  # noqa: F401  (not used directly; just checked for availability)
    _HAS_PYTEST = True
except ImportError:
    _HAS_PYTEST = False

from pipeline import guardrails
from pipeline.guardrails import (
    GuardrailConfig,
    PortfolioState,
    Verdict,
    evaluate,
    apply,
    kill_switch_engaged,
)
from pipeline.models import Opportunity, OppStatus


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_opp(**overrides) -> Opportunity:
    """Build a default clean Opportunity, overriding any fields via keyword args."""
    defaults = dict(
        market_id="poly:test",
        venue="poly",
        question="Will X happen?",
        side="YES",
        market_price=0.5,
        fair_prob=0.6,
        edge=0.2,
        edge_source="calibration",
        recommended_stake=20.0,
        liquidity=100.0,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def _make_state(**overrides) -> PortfolioState:
    """Build a default empty PortfolioState with bankroll=1000."""
    defaults = dict(bankroll=1000.0)
    defaults.update(overrides)
    return PortfolioState(**defaults)


# ─── evaluate: approval ─────────────────────────────────────────────────────

def test_evaluate_approves_clean():
    """A well-formed opp with positive edge and empty portfolio should be approved."""
    opp = _make_opp()
    state = _make_state()
    v = evaluate(opp, state)
    assert v.approved, f"Expected approval but got rejection: {v.notes}"


# ─── evaluate: per-opportunity rejections ───────────────────────────────────

def test_evaluate_rejects_edge_below_min():
    """Edge 0.01 is below min_edge=0.03 → rejected."""
    opp = _make_opp(edge=0.01)
    v = evaluate(opp, _make_state())
    assert not v.approved, "Should reject edge below min"
    assert "edge" in v.notes.lower(), f"Expected 'edge' in notes, got: {v.notes}"


def test_evaluate_rejects_price_out_of_bounds():
    """Price 0.99 exceeds max_price=0.97 → rejected."""
    opp = _make_opp(market_price=0.99)
    v = evaluate(opp, _make_state())
    assert not v.approved, "Should reject price above max_price"
    assert "price" in v.notes.lower(), f"Expected 'price' in notes, got: {v.notes}"


def test_evaluate_rejects_liquidity_below_min():
    """Liquidity $10 is below min_liquidity=$25 → rejected."""
    opp = _make_opp(liquidity=10.0)
    v = evaluate(opp, _make_state())
    assert not v.approved, "Should reject liquidity below min"
    assert "liquidity" in v.notes.lower(), f"Expected 'liquidity' in notes, got: {v.notes}"


def test_evaluate_rejects_stake_below_min():
    """Stake $0.50 is below min_stake=$1.00 → rejected."""
    opp = _make_opp(recommended_stake=0.5)
    v = evaluate(opp, _make_state())
    assert not v.approved, "Should reject stake below min_stake"
    assert "stake" in v.notes.lower(), f"Expected 'stake' in notes, got: {v.notes}"


def test_evaluate_rejects_position_over_cap():
    """stake=$100 on bankroll=$1000 → 10% > 5% per-market cap → rejected."""
    opp = _make_opp(recommended_stake=100.0)
    v = evaluate(opp, _make_state())
    assert not v.approved, "Should reject position exceeding per-market cap"
    assert ("position" in v.notes.lower() or "cap" in v.notes.lower()), (
        f"Expected 'position' or 'cap' in notes, got: {v.notes}"
    )


def test_evaluate_rejects_total_exposure_over_cap():
    """open_exposure=$480 + stake=$50 = $530 → 53% > 50% total cap → rejected."""
    opp = _make_opp(recommended_stake=50.0)
    state = _make_state(open_exposure=480.0)
    v = evaluate(opp, state)
    assert not v.approved, "Should reject when total exposure would exceed cap"
    assert ("exposure" in v.notes.lower() or "cap" in v.notes.lower()), (
        f"Expected 'exposure' or 'cap' in notes, got: {v.notes}"
    )


# ─── arb bypass ─────────────────────────────────────────────────────────────

def test_arb_bypasses_directional_min_edge():
    """edge_source='arb' uses arb_min_edge=0.005 instead of min_edge=0.03.

    An arb opp with edge=0.01 (above 0.005 but below 0.03) should be approved.
    """
    opp = _make_opp(edge=0.01, edge_source="arb")
    v = evaluate(opp, _make_state())
    assert v.approved, (
        f"Arb opp should bypass directional min_edge=0.03, got rejection: {v.notes}"
    )
    assert "arb" in v.notes.lower(), (
        f"Expected 'arb' in approval notes, got: {v.notes}"
    )


# ─── kill switch ────────────────────────────────────────────────────────────

def test_kill_switch_stop_file():
    """STOP file present → kill_switch_engaged returns a reason; evaluate rejects with HALT.

    We monkeypatch guardrails.STOP_FILE to a temp file so we never create a
    real STOP file in the repo root (which would halt the live trader).
    """
    orig_stop = guardrails.STOP_FILE
    fd, tmppath = tempfile.mkstemp()
    try:
        os.close(fd)
        # The temp file already exists on disk — that's our stand-in STOP file.
        guardrails.STOP_FILE = Path(tmppath)

        cfg = GuardrailConfig()
        state = _make_state()

        reason = kill_switch_engaged(state, cfg)
        assert reason is not None, (
            "kill_switch_engaged should return a reason string when STOP file exists"
        )

        opp = _make_opp()
        v = evaluate(opp, state, cfg)
        assert not v.approved, "evaluate should reject when STOP file exists"
        assert "HALT" in v.notes, f"Expected 'HALT' in verdict notes, got: {v.notes}"
    finally:
        guardrails.STOP_FILE = orig_stop
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def test_kill_switch_daily_loss():
    """Daily loss >= 10% of bankroll → halt even without a STOP file.

    bankroll=$1000, realized_pnl_today=-$150 → 15% loss > 10% limit.
    """
    cfg = GuardrailConfig()
    state = _make_state(bankroll=1000.0, realized_pnl_today=-150.0)

    reason = kill_switch_engaged(state, cfg)
    assert reason is not None, (
        "Should halt when daily loss (15%) exceeds limit (10%)"
    )

    opp = _make_opp()
    v = evaluate(opp, state, cfg)
    assert not v.approved, "evaluate should reject when daily loss limit is breached"
    assert "HALT" in v.notes, f"Expected 'HALT' in verdict notes, got: {v.notes}"


# ─── apply() ────────────────────────────────────────────────────────────────

def test_apply_exposure_accumulation_and_cap():
    """apply() accumulates open_exposure and stops approving once the total cap is hit.

    Setup: bankroll=$1000, max_position_fraction=30%, max_total_exposure=50%.
    Three opps each with stake=$200 (20% of bankroll — under the 30% per-market cap).
      Opp 1: approved (total 20%)
      Opp 2: approved (total 40%)
      Opp 3: rejected (total would be 60% > 50%)
    Approved opps must carry status=OppStatus.APPROVED.value;
    rejected must carry status=OppStatus.REJECTED.value.
    """
    cfg = GuardrailConfig(
        max_position_fraction=0.30,       # allow up to 30% per single market
        max_total_exposure_fraction=0.50,  # hard cap at 50% total
        min_edge=0.03,
        min_stake=1.0,
        # liquidity=None on opps skips the liquidity check regardless of this value
        min_liquidity=0.0,
    )
    state = PortfolioState(bankroll=1000.0)

    opp1 = _make_opp(market_id="poly:m1", recommended_stake=200.0, liquidity=None)
    opp2 = _make_opp(market_id="poly:m2", recommended_stake=200.0, liquidity=None)
    opp3 = _make_opp(market_id="poly:m3", recommended_stake=200.0, liquidity=None)

    approved, rejected = apply([opp1, opp2, opp3], state, cfg)

    assert len(approved) == 2, (
        f"Expected 2 approved opps, got {len(approved)}"
    )
    assert len(rejected) == 1, (
        f"Expected 1 rejected opp, got {len(rejected)}"
    )

    # open_exposure should reflect the two approved stakes ($200 + $200 = $400)
    assert abs(state.open_exposure - 400.0) < 1e-9, (
        f"Expected open_exposure=$400 after two approvals, got ${state.open_exposure}"
    )

    # Status fields must be mutated correctly
    for opp in approved:
        assert opp.status == OppStatus.APPROVED.value, (
            f"Approved opp has wrong status: {opp.status!r} "
            f"(expected {OppStatus.APPROVED.value!r})"
        )
    for opp in rejected:
        assert opp.status == OppStatus.REJECTED.value, (
            f"Rejected opp has wrong status: {opp.status!r} "
            f"(expected {OppStatus.REJECTED.value!r})"
        )

    # The rejection note for the third opp should explain why (exposure cap)
    rej_notes = rejected[0].guardrail_notes or ""
    assert ("exposure" in rej_notes.lower() or "cap" in rej_notes.lower()), (
        f"Expected 'exposure' or 'cap' in rejection notes, got: {rej_notes!r}"
    )


# ─── __main__ runner (pytest not required) ──────────────────────────────────

if __name__ == "__main__":
    _tests = [
        test_evaluate_approves_clean,
        test_evaluate_rejects_edge_below_min,
        test_evaluate_rejects_price_out_of_bounds,
        test_evaluate_rejects_liquidity_below_min,
        test_evaluate_rejects_stake_below_min,
        test_evaluate_rejects_position_over_cap,
        test_evaluate_rejects_total_exposure_over_cap,
        test_arb_bypasses_directional_min_edge,
        test_kill_switch_stop_file,
        test_kill_switch_daily_loss,
        test_apply_exposure_accumulation_and_cap,
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
