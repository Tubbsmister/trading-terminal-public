"""
Kalshi collector — wraps the existing read-only ``KalshiClient``.

Important quirk (see the kalshi-api-quirks memory): the bulk ``GET /markets``
feed is flooded with auto-generated zero-volume MVE parlay markets and reports
prices as "0.00". Enumerating it returns no usable liquid binaries. The right
approach is the **events** endpoint with nested markets, which returns clean
real markets with populated live prices. Prices arrive as decimal *string*
fields (``yes_ask_dollars`` = "0.1300"), already in [0, 1] — no cents math.
"""

from __future__ import annotations

import time
from typing import List, Optional

from .. import rootpath  # noqa: F401
from kalshi_client import KalshiClient

from .base import Collector
from ..models import Market, Venue, make_market_id

THROTTLE = 0.4  # Kalshi 429s on rapid paging


def _dollars(x) -> Optional[float]:
    """Parse a Kalshi ``*_dollars`` string ("0.4900") to a float in [0, 1]."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return max(0.0, min(1.0, v))


def _fp(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class KalshiCollector(Collector):
    """Pull open Kalshi markets via the events endpoint (clean, priced, no MVE)."""

    venue = Venue.KALSHI.value

    def __init__(self, client: Optional[KalshiClient] = None):
        self.client = client or KalshiClient()

    def collect(self, *, limit: int = 500) -> List[Market]:
        markets: List[Market] = []
        cursor: Optional[str] = None
        while len(markets) < limit:
            resp = self.client.get_events(
                limit=100, cursor=cursor, status="open", with_nested_markets=True
            )
            events = (resp or {}).get("events", [])
            if not events:
                break
            for ev in events:
                category = ev.get("category")
                event_ticker = ev.get("event_ticker") or ev.get("ticker")
                for m in ev.get("markets", []):
                    norm = self._normalize(m, category=category, event_ticker=event_ticker)
                    if norm is not None:
                        markets.append(norm)
            cursor = (resp or {}).get("cursor")
            if not cursor:
                break
            time.sleep(THROTTLE)
        return markets[:limit]

    def _normalize(self, m: dict, *, category, event_ticker) -> Optional[Market]:
        ticker = m.get("ticker")
        if not ticker or "MVE" in ticker:  # defensive: skip parlay flood
            return None
        title = m.get("title") or m.get("yes_sub_title") or m.get("subtitle")
        if not title:
            return None

        yes_bid = _dollars(m.get("yes_bid_dollars"))
        yes_ask = _dollars(m.get("yes_ask_dollars"))
        no_bid = _dollars(m.get("no_bid_dollars"))
        no_ask = _dollars(m.get("no_ask_dollars"))
        last = _dollars(m.get("last_price_dollars"))

        if yes_bid is not None and yes_ask is not None:
            yes_price = round((yes_bid + yes_ask) / 2.0, 6)
        else:
            yes_price = last
        if no_bid is not None and no_ask is not None:
            no_price = round((no_bid + no_ask) / 2.0, 6)
        elif yes_price is not None:
            no_price = round(1.0 - yes_price, 6)
        else:
            no_price = None

        status = m.get("status")
        return Market(
            id=make_market_id(Venue.KALSHI, ticker),
            venue=self.venue,
            venue_market_id=ticker,
            question=title,
            yes_price=yes_price,
            no_price=no_price,
            volume=_fp(m.get("volume_fp")),
            volume_24h=_fp(m.get("volume_24h_fp")),
            liquidity=_dollars(m.get("liquidity_dollars")),
            category=category,
            close_time=m.get("close_time"),
            url=f"https://kalshi.com/markets/{event_ticker}" if event_ticker else None,
            is_active=status in ("active", "open"),
            is_resolved=status in ("settled", "finalized", "determined"),
            extra={
                "event_ticker": event_ticker,
                # asks are what arb/execution actually pay — keep them explicit
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "open_interest": _fp(m.get("open_interest_fp")),
                "rules_primary": m.get("rules_primary"),
            },
        )
