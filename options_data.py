"""
Options market-data layer (read-only) — yfinance backend.

A thin, swappable wrapper around the *one* free source that reliably gives
equity option chains + quotes + earnings dates without an account: Yahoo via
yfinance. Everything the wheel scanner needs goes through the functions here,
so when you outgrow yfinance (it is unofficial, rate-limited, and occasionally
returns stale rows) you replace THIS file with a Tradier / IBKR / Polygon
backend and nothing downstream changes.

What it deliberately does NOT trust yfinance for:
  * Greeks — yfinance has none. We compute delta + probability-of-profit
    ourselves from IV in wheel_strategy.py (one consistent model beats a
    vendor's mystery greeks).
  * Row quality — Yahoo chains carry stale/junk rows (bid 0, ask 1.73 on a
    deep-OTM strike). get_put_chain() drops anything without a real two-sided
    quote so garbage never reaches the strategy layer.

Honest limits:
  * Quotes are ~15 min delayed. Fine for a once-a-day decision tool; do NOT
    use this to chase intraday fills.
  * Earnings dates from Yahoo are sometimes an estimated *range*; treat the
    earliest as the avoid-by date.
  * No real-time IV rank (needs a 52-wk IV history source). We approximate
    "are premiums rich" with IV / realized-vol — see iv_richness().
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache

import numpy as np
import yfinance as yf

THROTTLE = 0.3  # seconds between Yahoo calls (it rate-limits aggressively)


@dataclass
class PutQuote:
    """One cleaned put-option row, ready for the strategy layer."""
    symbol: str
    expiry: str           # 'YYYY-MM-DD'
    strike: float
    bid: float
    ask: float
    mid: float
    last: float
    volume: int
    open_interest: int
    iv: float             # implied vol as a fraction (0.35 = 35%)
    spread_pct: float     # (ask-bid)/mid — liquidity proxy

    @property
    def collateral(self) -> float:
        """Cash a cash-secured put ties up = strike * 100 (one contract)."""
        return self.strike * 100.0


@lru_cache(maxsize=128)
def _ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol)


def _retry(fn, tries: int = 3, label: str = ""):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001  yfinance raises a zoo of types
            last = exc
            time.sleep(THROTTLE * (i + 1))
    if label:
        print(f"    (data warn: {label} failed: {last})")
    return None


def get_quote(symbol: str) -> float | None:
    """Last/most-recent price, or None if the symbol won't resolve."""
    def _q():
        fi = _ticker(symbol).fast_info
        px = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        return float(px) if px else None
    return _retry(_q, label=f"quote {symbol}")


def get_expirations(symbol: str) -> list[str]:
    """All listed expiry strings ('YYYY-MM-DD'), nearest first."""
    return _retry(lambda: list(_ticker(symbol).options), label=f"expirations {symbol}") or []


def get_put_chain(symbol: str, expiry: str, *, min_oi: int = 50,
                  max_spread_pct: float = 0.20) -> list[PutQuote]:
    """
    Cleaned put chain for one expiry. Drops rows that can't be traded sanely:
      * bid <= 0 or ask <= 0           -> no real two-sided market
      * open interest < min_oi          -> illiquid, you'll be stuck
      * (ask-bid)/mid > max_spread_pct  -> spread eats the edge on entry/exit
    Returns PutQuotes sorted by strike ascending.
    """
    def _chain():
        return _ticker(symbol).option_chain(expiry).puts
    df = _retry(_chain, label=f"chain {symbol} {expiry}")
    if df is None or df.empty:
        return []

    out: list[PutQuote] = []
    for _, r in df.iterrows():
        bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        oi = int(r.get("openInterest") or 0)
        if oi < min_oi or spread_pct > max_spread_pct:
            continue
        vol = r.get("volume")
        out.append(PutQuote(
            symbol=symbol, expiry=expiry, strike=float(r["strike"]),
            bid=bid, ask=ask, mid=mid, last=float(r.get("lastPrice") or 0),
            volume=int(vol) if vol == vol and vol is not None else 0,  # NaN-safe
            open_interest=oi, iv=float(r.get("impliedVolatility") or 0),
            spread_pct=spread_pct,
        ))
    out.sort(key=lambda p: p.strike)
    return out


def get_next_earnings(symbol: str) -> date | None:
    """
    Next earnings date (earliest if Yahoo returns a range), or None if unknown.
    Used to AVOID selling puts across an earnings event unless you mean to —
    earnings gap the stock and blow up the 'high probability' assumption.
    """
    def _cal():
        cal = _ticker(symbol).calendar
        if not cal:
            return None
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not ed:
            return None
        ed = ed if isinstance(ed, list) else [ed]
        dates = [d for d in ed if isinstance(d, date)]
        return min(dates) if dates else None
    return _retry(_cal, label=f"earnings {symbol}")


def realized_vol(symbol: str, lookback_days: int = 30) -> float | None:
    """
    Annualized realized (historical) volatility from daily closes. Used as the
    denominator in iv_richness() — if the options' IV sits well above what the
    stock has actually been doing, premium is 'rich' and selling is favored.
    """
    def _hist():
        h = _ticker(symbol).history(period=f"{max(lookback_days * 2, 40)}d")
        if h is None or h.empty or "Close" not in h:
            return None
        closes = h["Close"].dropna().to_numpy()
        if len(closes) < 10:
            return None
        rets = np.diff(np.log(closes))[-lookback_days:]
        return float(np.std(rets, ddof=1) * np.sqrt(252))
    return _retry(_hist, label=f"hist {symbol}")


def iv_richness(atm_iv: float, symbol: str, lookback_days: int = 30) -> float | None:
    """
    IV / realized-vol ratio. >1.2ish = options pricing in more move than the
    stock has delivered (good for premium SELLERS). <1.0 = premium is cheap,
    selling is poorly paid. A pragmatic stand-in for true IV-rank without a
    historical-IV feed. Returns None if realized vol is unavailable.
    """
    rv = realized_vol(symbol, lookback_days)
    if not rv or rv <= 0 or not atm_iv:
        return None
    return atm_iv / rv


def days_to(expiry: str, ref: date | None = None) -> int:
    """Calendar days from ref (default today) to an expiry string."""
    ref = ref or datetime.now().date()
    exp = datetime.strptime(expiry, "%Y-%m-%d").date()
    return (exp - ref).days
