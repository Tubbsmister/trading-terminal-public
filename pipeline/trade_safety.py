"""
pipeline/trade_safety.py -- LAST LINE OF DEFENSE before a trade hits the wire.

Philosophy
----------
This module is deliberately *dumb*, *deterministic*, and *dependency-free*.
No network calls. No LLM. No exotic libraries. Only the Python standard library
and the json module. The point is that this gate must be *obviously correct*,
auditable in minutes, and runnable in any environment -- including incident
post-mortems.

Every check here is a question we would ask ourselves before pressing "confirm"
on a trading UI: Is this the right market? Is this the right side? Is the price
reasonable? Can I even sell what I am claiming to sell?

Fail-closed: any check we cannot definitively PASS is marked FAIL. If the
market dict is missing outcomePrices we drop to a non-critical warning rather
than a hard block (we cannot verify, but we will not silently continue without
noting it).

Correctness and clarity are the only goals.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TradeSafetyError(Exception):
    """Raised by require_safe() when one or more critical checks fail."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class IntendedOrder:
    market_id: str        # "poly:0x<conditionId>"
    condition_id: str     # 0x... hex
    slug: str
    side: str             # "YES" or "NO" -- the outcome we want exposure to
    action: str           # "BUY" (open/increase) or "SELL" (close/reduce)
    token_id: str         # the CLOB ERC1155 token id we will actually trade
    limit_price: float    # price per share, must be in (0,1)
    size: float           # number of shares
    notional: float       # USD: limit_price*size
    note: str = ""


@dataclass
class SafetyConfig:
    max_order_notional: float = 500.0   # hard cap per order, USD
    max_slippage: float = 0.10          # limit price must be within this of current mkt price
    min_price: float = 0.01
    max_price: float = 0.99
    require_position_for_sell: bool = True


@dataclass
class Check:
    name: str
    passed: bool
    critical: bool
    detail: str


@dataclass
class SafetyReport:
    order: IntendedOrder
    checks: List[Check] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        """True iff every CRITICAL check passed."""
        return all(c.passed for c in self.checks if c.critical)

    def failures(self) -> List[Check]:
        """Return critical checks that failed."""
        return [c for c in self.checks if c.critical and not c.passed]

    def summary(self) -> str:
        """
        Multi-line human-readable report.
        Critical checks: [PASS] / [FAIL].
        Non-critical:    (PASS) / (WARN).
        Ends with SAFE / UNSAFE verdict and lists critical failures.
        """
        lines = [
            f"Safety report: {self.order.action} {self.order.size} shares  "
            f"market={self.order.market_id}  side={self.order.side}  "
            f"@ {self.order.limit_price:.4f}  notional=${self.order.notional:.2f}",
            "-" * 72,
        ]
        for c in self.checks:
            if c.critical:
                tag = "[PASS]" if c.passed else "[FAIL]"
            else:
                tag = "(PASS)" if c.passed else "(WARN)"
            lines.append(f"  {tag}  {c.name}: {c.detail}")
        lines.append("-" * 72)
        if self.safe:
            lines.append("SAFE -- order may proceed")
        else:
            lines.append("UNSAFE -- DO NOT TRADE")
            lines.append("  CRITICAL FAILURES:")
            for c in self.failures():
                lines.append(f"    * {c.name}: {c.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_list(x) -> list:
    """Return x as a Python list.  Handles JSON-encoded string arrays or already-list."""
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            parsed = json.loads(x)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _abbrev(s: str, keep: int = 8) -> str:
    """Shorten a long hex string for display: show first/last `keep` chars."""
    if not isinstance(s, str):
        return repr(s)
    if len(s) <= keep * 2 + 3:
        return s
    return f"{s[:keep]}...{s[-keep:]}"


def _normalize_side(side: str) -> Optional[str]:
    """
    Normalize side string to canonical "YES" or "NO".
    Accepts: yes/YES/y/Y -> "YES",  no/NO/n/N -> "NO".
    Returns None for anything else.
    """
    s = side.strip().upper()
    if s in ("YES", "Y"):
        return "YES"
    if s in ("NO", "N"):
        return "NO"
    return None


def _find_outcome_index(outcomes: list, canonical_side: str) -> Optional[int]:
    """
    Return the index in `outcomes` that corresponds to `canonical_side` ("YES" or "NO").

    Standard binary markets use "Yes"/"No"; the comparison is case-insensitive.
    Non-standard outcomes (e.g. "Up"/"Down") will not match YES/NO and return None.
    """
    for i, outcome in enumerate(outcomes):
        if isinstance(outcome, str) and outcome.strip().upper() == canonical_side:
            return i
    return None


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

def verify_order(
    order: IntendedOrder,
    *,
    market: dict,
    held: Optional[dict] = None,
    cfg: Optional[SafetyConfig] = None,
) -> SafetyReport:
    """
    Run all safety checks against the order and market data.

    Returns a SafetyReport with every check result recorded.
    Never raises; call require_safe() if you want a raising variant.

    Parameters
    ----------
    order   : the trade we intend to place
    market  : Polymarket Gamma market dict (conditionId, outcomes, clobTokenIds, ...)
    held    : live on-chain position for this token (needed for SELL coverage check)
    cfg     : override default SafetyConfig thresholds
    """
    if cfg is None:
        cfg = SafetyConfig()

    report = SafetyReport(order=order)

    # ------------------------------------------------------------------
    # Pre-parse market arrays once (several checks read them)
    # ------------------------------------------------------------------
    outcomes: list = _coerce_list(market.get("outcomes", []))
    token_ids: list = _coerce_list(market.get("clobTokenIds", []))
    prices_raw: list = _coerce_list(market.get("outcomePrices", []))

    outcome_prices: List[Optional[float]] = []
    for raw in prices_raw:
        try:
            outcome_prices.append(float(raw))
        except (TypeError, ValueError):
            outcome_prices.append(None)

    # ---- helper: append one check -------------------------------
    def _add(name: str, passed: bool, critical: bool, detail: str) -> None:
        report.checks.append(Check(name=name, passed=passed,
                                   critical=critical, detail=detail))

    # ------------------------------------------------------------------
    # CHECK: action_valid
    # ------------------------------------------------------------------
    action_ok = order.action in ("BUY", "SELL")
    _add(
        "action_valid", action_ok, True,
        (f"action='{order.action}' is valid"
         if action_ok else
         f"action='{order.action}' must be 'BUY' or 'SELL'"),
    )

    # ------------------------------------------------------------------
    # CHECK: token_matches_side  -- THE most critical check
    #
    # Map the declared side ("YES"/"NO") to its outcome index, then assert
    # that the token_id we are actually sending matches that index's entry
    # in clobTokenIds.  A mismatch means we are about to bet the WRONG SIDE.
    # ------------------------------------------------------------------
    norm_side = _normalize_side(order.side)
    side_idx: Optional[int] = None  # set below; used by price_sanity too

    if norm_side is None:
        _add(
            "token_matches_side", False, True,
            f"side='{order.side}' is not recognized; expected 'YES' or 'NO'",
        )
    else:
        side_idx = _find_outcome_index(outcomes, norm_side)
        if side_idx is None:
            _add(
                "token_matches_side", False, True,
                (f"Cannot map side='{order.side}' to any outcome in {outcomes!r}. "
                 f"If this market uses non-YES/NO outcomes (e.g. Up/Down) the "
                 f"order's side must match the exact outcome name."),
            )
        else:
            expected_token = (token_ids[side_idx]
                              if side_idx < len(token_ids) else None)
            if expected_token is None:
                _add(
                    "token_matches_side", False, True,
                    (f"Outcome index {side_idx} for side='{order.side}' has no "
                     f"corresponding clobTokenId (clobTokenIds={token_ids!r})"),
                )
            elif expected_token == order.token_id:
                _add(
                    "token_matches_side", True, True,
                    (f"token_id matches side {norm_side}: "
                     f"expected {_abbrev(expected_token)}, "
                     f"got {_abbrev(order.token_id)}"),
                )
            else:
                _add(
                    "token_matches_side", False, True,
                    (f"token id does not match side {norm_side} for this market: "
                     f"expected {_abbrev(expected_token)}, "
                     f"got {_abbrev(order.token_id)}"),
                )

    # ------------------------------------------------------------------
    # CHECK: market_identity
    # order.condition_id == market["conditionId"]  (case-insensitive)
    # order.market_id ends with conditionId
    # order.slug == market["slug"]  (when slug provided)
    # ------------------------------------------------------------------
    cond_id_mkt = market.get("conditionId", "")
    cond_match = order.condition_id.lower() == cond_id_mkt.lower()
    mid_match = order.market_id.lower().endswith(cond_id_mkt.lower())
    slug_mkt = market.get("slug", "")
    slug_match = (not order.slug) or (order.slug == slug_mkt)

    identity_ok = cond_match and mid_match and slug_match
    if identity_ok:
        id_detail = (
            f"conditionId={_abbrev(cond_id_mkt)}, "
            f"market_id suffix matches, slug={slug_mkt!r} matches"
        )
    else:
        parts = []
        if not cond_match:
            parts.append(
                f"condition_id mismatch: order={_abbrev(order.condition_id)!r} "
                f"vs market={_abbrev(cond_id_mkt)!r}"
            )
        if not mid_match:
            parts.append(
                f"market_id '{order.market_id}' does not end with "
                f"conditionId '{_abbrev(cond_id_mkt)}'"
            )
        if not slug_match:
            parts.append(
                f"slug mismatch: order={order.slug!r} vs market={slug_mkt!r}"
            )
        id_detail = "; ".join(parts)

    _add("market_identity", identity_ok, True, id_detail)

    # ------------------------------------------------------------------
    # CHECK: not_resolved
    # Market must not be closed and must not have a resolved UMA status.
    # ------------------------------------------------------------------
    is_closed = bool(market.get("closed", False))
    uma_status = str(market.get("umaResolutionStatus", "") or "").strip().lower()
    market_is_settled = is_closed or (uma_status == "resolved")
    _add(
        "not_resolved", not market_is_settled, True,
        ("market is open (closed=False, umaResolutionStatus not 'resolved')"
         if not market_is_settled else
         f"market is CLOSED/RESOLVED: closed={is_closed}, "
         f"umaResolutionStatus={market.get('umaResolutionStatus', '')!r}"),
    )

    # ------------------------------------------------------------------
    # CHECK: market_active  (extra critical check -- obvious value)
    # active=False markets should not be traded even if not yet resolved.
    # ------------------------------------------------------------------
    # Only check if the key is present (some market dicts may omit it).
    if "active" in market:
        is_active = bool(market["active"])
        _add(
            "market_active", is_active, True,
            ("market.active=True" if is_active
             else "market.active=False -- market is not currently active"),
        )

    # ------------------------------------------------------------------
    # CHECK: price_bounds
    # ------------------------------------------------------------------
    price = order.limit_price
    in_strict = 0 < price < 1
    in_cfg = cfg.min_price <= price <= cfg.max_price
    price_bounds_ok = in_strict and in_cfg
    _add(
        "price_bounds", price_bounds_ok, True,
        (f"limit_price={price:.6f} within (0,1) and "
         f"[{cfg.min_price},{cfg.max_price}]"
         if price_bounds_ok else
         f"limit_price={price:.6f} violates bounds: "
         f"must be strictly in (0,1) and within "
         f"[{cfg.min_price},{cfg.max_price}]"),
    )

    # ------------------------------------------------------------------
    # CHECK: price_sanity  (fat-finger guard)
    # Compare limit_price to current market price for THIS side.
    # Non-critical if outcomePrices is missing or side_idx unknown.
    # ------------------------------------------------------------------
    if (side_idx is not None
            and side_idx < len(outcome_prices)
            and outcome_prices[side_idx] is not None):
        cur = outcome_prices[side_idx]
        slippage = abs(price - cur)
        sane = slippage <= cfg.max_slippage
        _add(
            "price_sanity", sane, True,
            (f"limit_price={price:.4f} vs market_price={cur:.4f}: "
             f"slippage={slippage:.4f} "
             f"{'<=' if sane else '>'} max_slippage={cfg.max_slippage:.4f}"),
        )
    else:
        _add(
            "price_sanity", True, False,
            "outcomePrices unavailable or side_idx unknown -- "
            "cannot verify price against market; proceeding unverified",
        )

    # ------------------------------------------------------------------
    # CHECK: size_positive
    # ------------------------------------------------------------------
    size_ok = order.size > 0
    _add(
        "size_positive", size_ok, True,
        (f"size={order.size} > 0"
         if size_ok else
         f"size={order.size} must be > 0"),
    )

    # ------------------------------------------------------------------
    # CHECK: notional_consistency
    # The caller-supplied notional must match price*size within tolerance.
    # Tolerance = 0.01 + 0.1% of notional to allow for float rounding.
    # ------------------------------------------------------------------
    derived = order.limit_price * order.size
    tol = 0.01 + 0.001 * order.notional
    diff = abs(order.notional - derived)
    notional_ok = diff <= tol
    _add(
        "notional_consistency", notional_ok, True,
        (f"notional={order.notional:.4f} vs price*size={derived:.4f}: "
         f"diff={diff:.6f} {'<=' if notional_ok else '>'} tol={tol:.6f}"),
    )

    # ------------------------------------------------------------------
    # CHECK: notional_cap
    # ------------------------------------------------------------------
    cap_ok = order.notional <= cfg.max_order_notional
    _add(
        "notional_cap", cap_ok, True,
        (f"notional=${order.notional:.2f} <= cap=${cfg.max_order_notional:.2f}"
         if cap_ok else
         f"notional=${order.notional:.2f} EXCEEDS cap=${cfg.max_order_notional:.2f}"),
    )

    # ------------------------------------------------------------------
    # CHECK: sell_coverage  (only for SELL orders)
    # We must hold at least as many shares as we are trying to sell,
    # and the held position must be on the same side we are selling.
    # ------------------------------------------------------------------
    if order.action == "SELL" and cfg.require_position_for_sell:
        if held is None:
            _add(
                "sell_coverage", False, True,
                "SELL with no confirmed on-chain position (held=None)",
            )
        else:
            held_size = float(held.get("size", 0))
            size_covered = held_size >= order.size - 1e-6

            # Normalize held side/outcome -- the dict may use either key
            held_side_raw = str(held.get("side", "") or "")
            held_outcome_raw = str(held.get("outcome", "") or "")
            held_norm = (_normalize_side(held_side_raw)
                         or _normalize_side(held_outcome_raw))
            side_covered = (held_norm is not None and held_norm == norm_side)

            # Defense in depth: the held position's TOKEN must also match the order
            # token. A corrupted/mismatched held dict (right side label, wrong token)
            # must not let a SELL through — token_matches_side validates the order's
            # token against the market; this validates the held lot we're reducing.
            held_token = str(held.get("token_id", "") or "")
            token_covered = (not held_token) or (held_token == order.token_id)

            sell_ok = size_covered and side_covered and token_covered
            if sell_ok:
                sell_detail = (
                    f"held {held_size} shares of {held_side_raw!r} "
                    f"(outcome={held_outcome_raw!r}, token {_abbrev(held_token)}), "
                    f"selling {order.size} shares of {norm_side}: covered"
                )
            else:
                parts = []
                if not size_covered:
                    parts.append(
                        f"held size {held_size} < order size {order.size}"
                    )
                if not side_covered:
                    parts.append(
                        f"held side/outcome '{held_side_raw}'/'{held_outcome_raw}' "
                        f"does not match order side '{norm_side}'"
                    )
                if not token_covered:
                    parts.append(
                        f"held token_id {_abbrev(held_token)} != order token_id "
                        f"{_abbrev(order.token_id)}"
                    )
                sell_detail = "; ".join(parts)

            _add("sell_coverage", sell_ok, True, sell_detail)

    return report


def require_safe(
    order: IntendedOrder,
    *,
    market: dict,
    held: Optional[dict] = None,
    cfg: Optional[SafetyConfig] = None,
) -> SafetyReport:
    """
    Run verify_order and raise TradeSafetyError if any critical check fails.

    This is the entry point for production code paths.  Call this immediately
    before submitting an order to the exchange.  If it returns without raising,
    every critical safety invariant has been verified and you have a SafetyReport
    to attach to your trade log.

    Raises
    ------
    TradeSafetyError
        Contains the full multi-line summary so operators can see exactly what
        failed without digging into logs.
    """
    report = verify_order(order, market=market, held=held, cfg=cfg)
    if not report.safe:
        raise TradeSafetyError(report.summary())
    return report


# ---------------------------------------------------------------------------
# Demo / self-test
# Run with:  python -m pipeline.trade_safety
# Must show SAFE for the correct order and UNSAFE for the wrong-token order.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    # Realistic (but fictional) market dict as Polymarket Gamma returns it.
    # Outcomes and token ids arrive as JSON-encoded strings in the real API.
    COND_ID = "0xabc123def456abc123def456abc123def456abc123def456abc123def456abc1"

    SAMPLE_MARKET = {
        "conditionId": COND_ID,
        "slug": "will-fed-cut-rates-in-july-2026",
        "closed": False,
        "active": True,
        "umaResolutionStatus": "",
        # JSON-encoded string arrays -- must be json.loads'd by _coerce_list
        "outcomes":      '["Yes", "No"]',
        "clobTokenIds":  '["111111111111111111111111111111", "999999999999999999999999999999"]',
        "outcomePrices": '["0.72", "0.28"]',
    }

    YES_TOKEN = "111111111111111111111111111111"   # index 0 -> "Yes"
    NO_TOKEN  = "999999999999999999999999999999"   # index 1 -> "No"

    # ----------------------------------------------------------------
    # Case 1: SAFE order -- BUY YES with the correct YES token
    # limit_price=0.74, current YES price=0.72, slippage=0.02 < 0.10
    # ----------------------------------------------------------------
    safe_order = IntendedOrder(
        market_id=f"poly:{COND_ID}",
        condition_id=COND_ID,
        slug="will-fed-cut-rates-in-july-2026",
        side="YES",
        action="BUY",
        token_id=YES_TOKEN,       # correct token for YES
        limit_price=0.74,
        size=50.0,
        notional=37.0,            # 0.74 * 50 = 37.0 -- consistent
        note="Demo: safe order",
    )

    # ----------------------------------------------------------------
    # Case 2: WRONG order -- side=NO but token_id is the YES token.
    # This is the classic "betting the wrong side" mistake.
    # The gate MUST catch this on token_matches_side.
    # ----------------------------------------------------------------
    wrong_order = IntendedOrder(
        market_id=f"poly:{COND_ID}",
        condition_id=COND_ID,
        slug="will-fed-cut-rates-in-july-2026",
        side="NO",
        action="BUY",
        token_id=YES_TOKEN,       # WRONG: YES token for a NO order
        limit_price=0.28,
        size=50.0,
        notional=14.0,            # 0.28 * 50 = 14.0 -- consistent
        note="Demo: deliberately wrong token id to prove gate catches it",
    )

    sep = "=" * 72

    print(sep)
    print("CASE 1: Correct order -- BUY YES with the correct YES token")
    print(sep)
    report1 = verify_order(safe_order, market=SAMPLE_MARKET)
    print(report1.summary())

    print()
    print(sep)
    print("CASE 2: Wrong order -- side=NO but token_id is the YES token")
    print(sep)
    report2 = verify_order(wrong_order, market=SAMPLE_MARKET)
    print(report2.summary())

    print()
    print(sep)
    print("ASSERTION CHECK")
    print(sep)
    assert report1.safe, f"DEMO BROKEN: safe_order should be SAFE but got failures: {report1.failures()}"
    assert not report2.safe, "DEMO BROKEN: wrong_order should be UNSAFE"
    fail_names = {c.name for c in report2.failures()}
    assert "token_matches_side" in fail_names, (
        f"DEMO BROKEN: token_matches_side should be in failures, got {fail_names}"
    )
    print("  [OK] safe_order  -> SAFE")
    print("  [OK] wrong_order -> UNSAFE, token_matches_side FAILED")
    print("  Gate is working correctly.")
