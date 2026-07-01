"""
Financial-market signal collector — Tier 5.

Snapshots yields, vol, FX, commodities, and sector ETFs into the ``signals``
table so downstream geopolitical or macro theses can read corroborating
evidence like "defense ETF +3%, oil +5%, VIX +12%".

Usage::

    python -m pipeline.signals.markets_financial                      # all tickers
    python -m pipeline.signals.markets_financial --symbol ^VIX        # one symbol
    python -m pipeline.signals.markets_financial --symbol ^VIX --symbol GC=F
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import options_data as od
from pipeline.models import Signal, SourceTier
from pipeline.store import get_store


# ---------------------------------------------------------------------------
# Ticker registry  (yfinance symbol, human label)
# ---------------------------------------------------------------------------

TICKERS: List[Tuple[str, str]] = [
    ("^TNX",       "US 10Y Yield"),
    ("^VIX",       "VIX"),
    ("DX-Y.NYB",   "USD Index"),
    ("GC=F",       "Gold"),
    ("CL=F",       "WTI Oil"),
    ("NG=F",       "Nat Gas"),
    ("BTC-USD",    "Bitcoin"),
    ("XLE",        "Energy ETF (XLE)"),
    ("XLF",        "Financials ETF (XLF)"),
    ("ITA",        "Defense ETF (ITA)"),
    ("JETS",       "Airlines ETF (JETS)"),
    ("SMH",        "Semis ETF (SMH)"),
]


# ---------------------------------------------------------------------------
# Per-symbol fetch
# ---------------------------------------------------------------------------

def _fetch_signal(symbol: str, label: str) -> Optional[Signal]:
    """Return a Signal for the latest close + 1d pct change, or None on failure."""

    def _history():
        h = od._ticker(symbol).history(period="5d")
        return h

    hist = od._retry(_history, label=f"history {symbol}")
    if hist is None or hist.empty or "Close" not in hist:
        return None

    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return None

    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])

    # Guard against degenerate data (zeros, NaN that slipped through)
    import math
    if not math.isfinite(last) or not math.isfinite(prev) or prev == 0.0:
        return None

    pct = (last / prev - 1.0) * 100.0

    # 5-day range for the body
    lo_5 = float(closes.min())
    hi_5 = float(closes.max())
    last_date = hist.index[-1]
    try:
        # DatetimeIndex — grab date portion
        last_date_str = last_date.date().isoformat()
    except AttributeError:
        last_date_str = str(last_date)[:10]

    title = f"{label}: {last:.2f} ({pct:+.1f}% 1d)"
    body = f"5d range: {lo_5:.2f} - {hi_5:.2f} | last bar: {last_date_str}"
    external_id = f"{symbol}:{date.today().isoformat()}"
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return Signal(
        source=f"market:{symbol}",
        title=title,
        source_tier=SourceTier.MARKETS.value,
        external_id=external_id,
        body=body,
        published_at=published_at,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # Windows cp1252 console: reconfigure to ASCII-safe UTF-8 output
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--symbol", dest="symbols", action="append", metavar="SYM",
        help="filter to specific ticker symbols (repeatable); default: all",
    )
    args = ap.parse_args(argv)

    # Resolve ticker list
    tickers_to_run = TICKERS
    if args.symbols:
        wanted = set(args.symbols)
        tickers_to_run = [(s, l) for s, l in TICKERS if s in wanted]
        if not tickers_to_run:
            known = [s for s, _ in TICKERS]
            print(f"No symbols matched {args.symbols!r}.  Available: {known}")
            return 1

    store = get_store()

    print(f"Financial-market collector  |  {len(tickers_to_run)} symbol(s)")
    print(f"DB: {store.db_path}")
    print("-" * 60)

    signals: List[Signal] = []
    ok_symbols: List[str] = []
    failed_symbols: List[str] = []

    for i, (symbol, label) in enumerate(tickers_to_run):
        if i > 0:
            time.sleep(od.THROTTLE)
        try:
            sig = _fetch_signal(symbol, label)
            if sig is None:
                print(f"  [{symbol:12s}] SKIP  (no valid price data)")
                failed_symbols.append(symbol)
            else:
                signals.append(sig)
                ok_symbols.append(symbol)
                # Print ASCII-safe: encode/decode to strip non-ASCII
                safe_title = sig.title.encode("ascii", errors="replace").decode("ascii")
                print(f"  [{symbol:12s}] {safe_title}")
        except Exception as exc:
            err = str(exc).encode("ascii", errors="replace").decode("ascii")
            print(f"  [{symbol:12s}] ERROR: {err}")
            failed_symbols.append(symbol)

    print("-" * 60)

    total_new = 0
    if signals:
        total_new = store.add_signals(signals)

    print(f"Symbols fetched : {len(ok_symbols)} ok, {len(failed_symbols)} failed/skipped")
    if failed_symbols:
        print(f"Failed/skipped  : {', '.join(failed_symbols)}")
    print(f"New signals     : {total_new}")
    print(f"Total in DB     : {store.count_signals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
