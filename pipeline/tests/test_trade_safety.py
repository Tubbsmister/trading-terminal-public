"""
Adversarial test suite for pipeline.trade_safety — pre-trade safety gate.

PURPOSE: try to sneak bad orders past the gate and assert every one is caught.
Each negative test asserts:
  1. verify_order(...).safe is False
  2. the expected Check.name is among .failures()

This suite is written BEFORE the implementation; it functions as the adversarial
spec.  If the implementation exists and any attack PASSES the gate, that is a
critical bug — this file must not be modified to paper over it.

Runnable two ways:
  pytest pipeline/tests/test_trade_safety.py     (standard test runner)
  python  pipeline/tests/test_trade_safety.py    (plain script, no pytest required)
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

# Ensure the repo root is on sys.path so `pipeline` is importable when this
# file is executed directly (python pipeline/tests/test_trade_safety.py).
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    import pytest  # noqa: F401  (presence checked, not used directly)
    _HAS_PYTEST = True
except ImportError:
    _HAS_PYTEST = False

from pipeline.trade_safety import (
    IntendedOrder,
    SafetyConfig,
    verify_order,
    require_safe,
    TradeSafetyError,
)


# ─── fixtures ────────────────────────────────────────────────────────────────

def sample_market() -> dict:
    """
    A healthy, live Polymarket Gamma market dict.

    Outcome index alignment:
      [0] YES → token YESTOKEN111 → price 0.21
      [1] NO  → token NOTOKEN222  → price 0.79
    """
    return {
        "conditionId": "0x178b...",
        "slug": "fable-5-july-1",
        "closed": False,
        "active": True,
        "umaResolutionStatus": "",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["YESTOKEN111","NOTOKEN222"]',
        "outcomePrices": '["0.21","0.79"]',
    }


def valid_no_sell_order() -> IntendedOrder:
    """
    A correct SELL of 90 NO contracts at 0.785 (≈ 0.79 market price, 0.6% below).
    notional = 0.785 × 90 = 70.65
    """
    return IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="SELL",
        token_id="NOTOKEN222",
        limit_price=0.785,
        size=90,
        notional=70.65,
        note="valid NO SELL",
    )


def _valid_no_held() -> dict:
    """Held position that satisfies the valid_no_sell_order (131.1 NO contracts)."""
    return {
        "size": 131.1,
        "avg": 0.54,
        "side": "NO",
        "token_id": "NOTOKEN222",
        "outcome": "No",
    }


# ─── assertion helpers ───────────────────────────────────────────────────────

def _failure_names(report) -> list:
    """Extract the .name attribute from each critical failure in a SafetyReport."""
    return [c.name for c in report.failures()]


def _assert_fails_with(report, check_name: str) -> None:
    """Assert report.safe is False AND check_name is in the list of failures."""
    assert report.safe is False, (
        f"GATE BYPASS DETECTED — expected safe=False but got safe=True. "
        f"Attack that should have triggered '{check_name}' slipped through!"
    )
    names = _failure_names(report)
    assert check_name in names, (
        f"Expected check '{check_name}' in failures {names} — "
        f"gate may have rejected for the wrong reason or failed to record the check."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL TESTS — each one tries to sneak a bad order past the gate
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Attack 1: token is for the WRONG side (THE critical check) ──────────────

def test_wrong_token_for_side():
    """THE big one: side=NO but token_id points to the YES token.

    Sending a NO order with the YES token executes the exact opposite trade on
    Polymarket's CLOB.  The gate must catch any mismatch between side and token.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="YESTOKEN111",   # ← WRONG: should be NOTOKEN222
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: NO side with YES token",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "token_matches_side")


# ─── Attack 2: mirror — side=YES but token is the NO token ───────────────────

def test_yes_meant_no():
    """side=YES but token_id=NOTOKEN222 — the mirror of attack 1.

    Internally consistent enough to look plausible, but would buy the wrong
    binary outcome on exchange.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="NOTOKEN222",   # ← WRONG: should be YESTOKEN111
        limit_price=0.21,
        size=100,
        notional=21.0,
        note="attack: YES side with NO token",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "token_matches_side")


# ─── Attack 3: order destined for a different market ─────────────────────────

def test_wrong_market():
    """condition_id in the order doesn't match the Gamma market dict we fetched.

    A stale order or a dict look-up bug could pair an order for market A with
    the live feed for market B.  The gate must cross-check identity.
    """
    order = IntendedOrder(
        market_id="poly:0xDEADBEEF",
        condition_id="0xDEADBEEF",   # ← does not match sample_market's 0x178b...
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: wrong market condition_id",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "market_identity")


# ─── Attack 3b: slug mismatch as an additional identity vector ───────────────

def test_slug_mismatch():
    """slug in the order doesn't match market['slug'] — another identity check."""
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="totally-different-market",   # ← mismatch
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: slug mismatch",
    )
    report = verify_order(order, market=sample_market())
    # Either market_identity or a dedicated slug check must fire.
    names = _failure_names(report)
    assert report.safe is False, (
        f"GATE BYPASS: slug mismatch should be caught but got safe=True"
    )
    assert any(n in names for n in ("market_identity", "slug_matches")), (
        f"Expected market_identity or slug_matches in failures {names}"
    )


# ─── Attack 4a: trading on a closed market ───────────────────────────────────

def test_resolved_market_closed_flag():
    """market['closed']=True — contract is settled, trading must be blocked."""
    market = sample_market()
    market["closed"] = True
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: closed market",
    )
    report = verify_order(order, market=market)
    _assert_fails_with(report, "not_resolved")


# ─── Attack 4b: umaResolutionStatus marks the market as resolved ─────────────

def test_resolved_market_uma_status():
    """umaResolutionStatus='resolved' — UMA oracle settled; gate must block trading."""
    market = sample_market()
    market["umaResolutionStatus"] = "resolved"
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: umaResolutionStatus=resolved",
    )
    report = verify_order(order, market=market)
    _assert_fails_with(report, "not_resolved")


# ─── Attack 4c: inactive market ──────────────────────────────────────────────

def test_inactive_market():
    """market['active']=False — paused or delisted; trading must be blocked."""
    market = sample_market()
    market["active"] = False
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: inactive market",
    )
    report = verify_order(order, market=market)
    # Must fail not_resolved or a dedicated active_market check.
    names = _failure_names(report)
    assert report.safe is False, (
        f"GATE BYPASS: inactive market (active=False) should be caught"
    )
    assert any(n in names for n in ("not_resolved", "market_active")), (
        f"Expected not_resolved or market_active in failures {names}"
    )


# ─── Attack 5a: limit_price above 1.0 (impossible on binary market) ──────────

def test_price_above_max():
    """limit_price=1.5 is above max_price=0.99 — price_bounds must fire.

    A price >1 on a binary market means the data source is corrupted or the
    order was built from stale/wrong data.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=1.5,   # ← impossible
        size=90,
        notional=135.0,
        note="attack: price > 1",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "price_bounds")


# ─── Attack 5b: limit_price at zero ──────────────────────────────────────────

def test_price_zero():
    """limit_price=0.0 is below min_price=0.01 — price_bounds must fire.

    Submitting at price=0 would either be a free-money exploit or a zero-fill;
    neither is intended behavior.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.0,   # ← free money attack
        size=90,
        notional=0.0,
        note="attack: price = 0",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "price_bounds")


# ─── Attack 5c: limit_price exactly at the boundary (should PASS) ────────────

def test_price_at_min_boundary_passes():
    """limit_price=0.01 is exactly at min_price — boundary must be inclusive (>=)."""
    # This is an edge-case positive test to prevent an off-by-one exclusion.
    # Market NO price is 0.79; 0.01 is wildly off, so price_sanity will fire.
    # We only want to confirm price_bounds does NOT fire for 0.01.
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.01,
        size=90,
        notional=0.9,
        note="boundary: price at min",
    )
    report = verify_order(order, market=sample_market())
    names = _failure_names(report)
    assert "price_bounds" not in names, (
        f"price_bounds should NOT fire at limit_price=0.01 (min_price=0.01), "
        f"but got failures: {names}"
    )


# ─── Attack 6: fat-finger price (huge slippage from market price) ─────────────

def test_fat_finger_price():
    """NO order at 0.40 when NO market price is 0.79 — ~49% slippage.

    |0.40 - 0.79| / 0.79 ≈ 0.494, which exceeds max_slippage=0.10.
    Even though buying NO at 0.40 is technically a favourable fill (better
    than market), such a large deviation signals wrong-market confusion or
    corrupted data and must be blocked.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="NOTOKEN222",
        limit_price=0.40,   # ← 39 cents away from market price of 0.79
        size=90,
        notional=36.0,
        note="attack: fat-finger price",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "price_sanity")


# ─── Attack 6b: fat-finger on the high side (paying too much) ────────────────

def test_fat_finger_price_high():
    """YES order at 0.50 when YES market price is 0.21 — overpaying by 138%.

    |0.50 - 0.21| / 0.21 ≈ 1.38 >> 0.10.
    Clearly corrupted: would pay 50c for a token the market values at 21c.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.50,   # ← market YES is 0.21
        size=100,
        notional=50.0,
        note="attack: fat-finger high YES",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "price_sanity")


# ─── Attack 7: oversell (more contracts than we hold) ─────────────────────────

def test_oversell():
    """SELL 200 contracts when we only hold 131.1 — attempts a naked short.

    This would open an uncovered short, which is not allowed by SafetyConfig
    when require_position_for_sell=True.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="SELL",
        token_id="NOTOKEN222",
        limit_price=0.785,
        size=200,          # ← 200 > held 131.1
        notional=157.0,
        note="attack: oversell",
    )
    held = {"size": 131.1, "avg": 0.54, "side": "NO", "token_id": "NOTOKEN222", "outcome": "No"}
    report = verify_order(order, market=sample_market(), held=held)
    _assert_fails_with(report, "sell_coverage")


# ─── Attack 8: selling with no position at all ────────────────────────────────

def test_sell_no_position():
    """SELL with held=None — completely naked short, must be blocked."""
    report = verify_order(valid_no_sell_order(), market=sample_market(), held=None)
    _assert_fails_with(report, "sell_coverage")


# ─── Attack 9: held is the wrong side ────────────────────────────────────────

def test_sell_wrong_side_held():
    """SELL NO but the held position is in YES — can't deliver what you don't own."""
    order = valid_no_sell_order()
    held = {
        "size": 200,
        "avg": 0.21,
        "side": "YES",
        "token_id": "YESTOKEN111",
        "outcome": "Yes",
    }
    report = verify_order(order, market=sample_market(), held=held)
    _assert_fails_with(report, "sell_coverage")


# ─── Attack 9b: held is the right side but token_id is wrong ──────────────────

def test_sell_wrong_token_in_held():
    """Held side='NO' but held token_id points to the YES token — data corruption."""
    order = valid_no_sell_order()
    held = {
        "size": 200,
        "avg": 0.54,
        "side": "NO",
        "token_id": "YESTOKEN111",   # ← wrong token despite correct side string
        "outcome": "No",
    }
    report = verify_order(order, market=sample_market(), held=held)
    # Must fail sell_coverage (or a dedicated token consistency check).
    names = _failure_names(report)
    assert report.safe is False, (
        f"GATE BYPASS: held.token_id mismatch should be caught"
    )
    assert any(n in names for n in ("sell_coverage", "held_token_mismatch")), (
        f"Expected sell_coverage or held_token_mismatch in failures {names}"
    )


# ─── Attack 10: notional is wildly inconsistent with price × size ─────────────

def test_notional_mismatch():
    """notional=999 but limit_price × size = 0.785 × 90 = 70.65.

    A bug in the order builder could stamp a stale notional from a different
    trade.  The gate must detect that the stated notional doesn't match the
    arithmetic of the order.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="SELL",
        token_id="NOTOKEN222",
        limit_price=0.785,
        size=90,
        notional=999.0,   # ← should be 70.65; off by factor of ~14×
        note="attack: notional mismatch",
    )
    held = _valid_no_held()
    report = verify_order(order, market=sample_market(), held=held)
    _assert_fails_with(report, "notional_consistency")


# ─── Attack 10b: notional understated (but still wrong) ──────────────────────

def test_notional_mismatch_understated():
    """notional=1.0 but price × size = 70.65 — understated by ~70×.

    An attacker could try to use a tiny reported notional to slip past a
    notional_cap check while placing a large real order.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="SELL",
        token_id="NOTOKEN222",
        limit_price=0.785,
        size=90,
        notional=1.0,   # ← massively understated
        note="attack: understated notional",
    )
    held = _valid_no_held()
    report = verify_order(order, market=sample_market(), held=held)
    _assert_fails_with(report, "notional_consistency")


# ─── Attack 11: notional over the per-order cap ──────────────────────────────

def test_notional_over_cap():
    """Structurally valid BUY of YES, but notional=600.18 exceeds the $500 cap.

    Order arithmetic is internally consistent (0.21 × 2858 = 600.18) so the
    notional_consistency check passes; the notional_cap check must catch the
    excessive size.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.21,
        size=2858,          # 0.21 × 2858 = 600.18 — consistent notional
        notional=600.18,    # ← 600.18 > max_order_notional=500
        note="attack: over notional cap",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "notional_cap")


# ─── Attack 11b: cap bypass via split notional reporting ─────────────────────

def test_notional_cap_uses_computed_not_stated():
    """Attempt to bypass notional_cap by reporting a low notional but trading large.

    The gate should compute notional from price × size independently and apply
    the cap to the computed value, not just the stated notional.
    """
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.21,
        size=2858,       # 0.21 × 2858 = 600.18 — real notional exceeds cap
        notional=499.0,  # ← lies about notional to try to pass cap check
        note="attack: understate notional to bypass cap",
    )
    report = verify_order(order, market=sample_market())
    # Either notional_consistency (499 ≠ 600.18) or notional_cap must fire.
    names = _failure_names(report)
    assert report.safe is False, (
        f"GATE BYPASS: low stated notional on oversized order should be caught"
    )
    assert any(n in names for n in ("notional_consistency", "notional_cap")), (
        f"Expected notional_consistency or notional_cap in failures {names}"
    )


# ─── Attack 12: unrecognised action string ────────────────────────────────────

def test_bad_action():
    """action='HODL' is not a valid trade action — must be BUY or SELL."""
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="HODL",   # ← not BUY or SELL
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: bad action",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "action_valid")


# ─── Attack 12b: empty action string ─────────────────────────────────────────

def test_empty_action():
    """action='' — empty string should also be rejected."""
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="",
        token_id="NOTOKEN222",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack: empty action",
    )
    report = verify_order(order, market=sample_market())
    _assert_fails_with(report, "action_valid")


# ═══════════════════════════════════════════════════════════════════════════════
# POSITIVE TESTS — valid orders that MUST pass the gate
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Positive 13: the reference valid NO SELL ────────────────────────────────

def test_valid_no_sell_passes():
    """The reference valid NO SELL (90 contracts at 0.785, held=131.1) must be approved."""
    report = verify_order(
        valid_no_sell_order(),
        market=sample_market(),
        held=_valid_no_held(),
    )
    assert report.safe is True, (
        f"Expected safe=True for a valid NO SELL but gate rejected it.\n"
        f"Failures: {_failure_names(report)}\n"
        f"Summary: {report.summary()}"
    )


# ─── Positive 14: valid YES BUY ──────────────────────────────────────────────

def test_valid_yes_buy_passes():
    """A correct BUY of YES at just above market price (2.4% slippage) must be approved."""
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.215,    # just above YES market price of 0.21 → ~2.4% slippage
        size=100,
        notional=21.5,        # 0.215 × 100 = 21.5 ✓
        note="valid YES BUY",
    )
    report = verify_order(order, market=sample_market())
    assert report.safe is True, (
        f"Expected safe=True for a valid YES BUY but gate rejected it.\n"
        f"Failures: {_failure_names(report)}\n"
        f"Summary: {report.summary()}"
    )


# ─── Positive 14b: SELL size exactly equals held size (boundary) ─────────────

def test_sell_exactly_full_position_passes():
    """SELL size == held size exactly — this is a full close, must be allowed."""
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="SELL",
        token_id="NOTOKEN222",
        limit_price=0.785,
        size=131,             # close but not over (held=131.1 via int boundary)
        notional=102.935,     # 0.785 × 131 = 102.935
        note="valid: sell full position",
    )
    held = {"size": 131.1, "avg": 0.54, "side": "NO", "token_id": "NOTOKEN222", "outcome": "No"}
    report = verify_order(order, market=sample_market(), held=held)
    assert report.safe is True, (
        f"Expected safe=True selling within held position (131 <= 131.1) "
        f"but got failures: {_failure_names(report)}\n"
        f"Summary: {report.summary()}"
    )


# ─── Positive/exception test 15: require_safe contract ───────────────────────

def test_require_safe_raises_on_unsafe():
    """require_safe must raise TradeSafetyError when the order is not safe."""
    bad_order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="YESTOKEN111",   # ← wrong token (attack 1 again)
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack for require_safe test",
    )
    raised = False
    try:
        require_safe(bad_order, market=sample_market())
    except TradeSafetyError:
        raised = True
    except Exception as e:
        raise AssertionError(
            f"require_safe raised {type(e).__name__} instead of TradeSafetyError: {e}"
        ) from e
    assert raised, "require_safe must raise TradeSafetyError on an unsafe order"


def test_require_safe_returns_report_on_valid():
    """require_safe returns the SafetyReport (safe=True) when the order is valid."""
    result = require_safe(
        valid_no_sell_order(),
        market=sample_market(),
        held=_valid_no_held(),
    )
    assert result is not None, (
        "require_safe should return the SafetyReport (not None) on a valid order"
    )
    assert result.safe is True, (
        f"require_safe returned a report but safe=False — unexpected failures: "
        f"{_failure_names(result)}"
    )


# ─── Custom SafetyConfig: tighter cap ────────────────────────────────────────

def test_custom_cfg_tighter_cap():
    """A valid $70 order must be blocked when the caller supplies a $50 cap."""
    cfg = SafetyConfig(max_order_notional=50.0)
    report = verify_order(
        valid_no_sell_order(),   # notional=70.65
        market=sample_market(),
        held=_valid_no_held(),
        cfg=cfg,
    )
    _assert_fails_with(report, "notional_cap")


def test_custom_cfg_tighter_slippage():
    """Slippage is ABSOLUTE (price points), per spec. A YES BUY 0.03 above the
    0.21 market price must be blocked when max_slippage is tightened to 0.01."""
    cfg = SafetyConfig(max_slippage=0.01)
    order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="YES",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.24,    # 0.03 absolute above YES market price 0.21 > 0.01 cap
        size=100,
        notional=24.0,
        note="custom tight slippage",
    )
    report = verify_order(order, market=sample_market(), cfg=cfg)
    _assert_fails_with(report, "price_sanity")


# ─── SafetyReport API contract ────────────────────────────────────────────────

def test_report_summary_is_string():
    """SafetyReport.summary() must return a non-empty string."""
    report = verify_order(valid_no_sell_order(), market=sample_market(), held=_valid_no_held())
    s = report.summary()
    assert isinstance(s, str) and len(s) > 0, (
        f"summary() must return a non-empty string; got {s!r}"
    )


def test_report_failures_is_list():
    """SafetyReport.failures() must return a list (possibly empty for safe orders)."""
    report = verify_order(valid_no_sell_order(), market=sample_market(), held=_valid_no_held())
    f = report.failures()
    assert isinstance(f, list), f"failures() must return a list; got {type(f)}"


def test_failed_report_has_nonempty_failures():
    """An unsafe order must have at least one entry in failures()."""
    bad_order = IntendedOrder(
        market_id="poly:0x178b...",
        condition_id="0x178b...",
        slug="fable-5-july-1",
        side="NO",
        action="BUY",
        token_id="YESTOKEN111",
        limit_price=0.79,
        size=90,
        notional=71.1,
        note="attack for failures() contract test",
    )
    report = verify_order(bad_order, market=sample_market())
    assert report.safe is False
    assert len(report.failures()) > 0, (
        "An unsafe order must produce at least one critical failure in failures()"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# __main__ runner — no pytest dependency
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _tests = [
        # ── Adversarial attacks ──────────────────────────────────────────────
        test_wrong_token_for_side,
        test_yes_meant_no,
        test_wrong_market,
        test_slug_mismatch,
        test_resolved_market_closed_flag,
        test_resolved_market_uma_status,
        test_inactive_market,
        test_price_above_max,
        test_price_zero,
        test_price_at_min_boundary_passes,
        test_fat_finger_price,
        test_fat_finger_price_high,
        test_oversell,
        test_sell_no_position,
        test_sell_wrong_side_held,
        test_sell_wrong_token_in_held,
        test_notional_mismatch,
        test_notional_mismatch_understated,
        test_notional_over_cap,
        test_notional_cap_uses_computed_not_stated,
        test_bad_action,
        test_empty_action,
        # ── Positive tests ───────────────────────────────────────────────────
        test_valid_no_sell_passes,
        test_valid_yes_buy_passes,
        test_sell_exactly_full_position_passes,
        # ── Exception contract ───────────────────────────────────────────────
        test_require_safe_raises_on_unsafe,
        test_require_safe_returns_report_on_valid,
        # ── Custom config ────────────────────────────────────────────────────
        test_custom_cfg_tighter_cap,
        test_custom_cfg_tighter_slippage,
        # ── API contract ─────────────────────────────────────────────────────
        test_report_summary_is_string,
        test_report_failures_is_list,
        test_failed_report_has_nonempty_failures,
    ]

    _failed = 0
    _total = len(_tests)
    print(f"Running {_total} adversarial safety-gate tests...\n")
    for _fn in _tests:
        try:
            _fn()
            print(f"  PASS  {_fn.__name__}")
        except Exception as _e:
            print(f"  FAIL  {_fn.__name__} — {_e}")
            traceback.print_exc()
            _failed += 1

    _passed = _total - _failed
    print(f"\n{'='*60}")
    print(f"Results: {_passed}/{_total} passed, {_failed} failed")
    if _failed:
        print("CRITICAL: some attacks were not caught by the safety gate!")
    else:
        print("All attacks caught; all valid orders approved.")
    sys.exit(1 if _failed else 0)
