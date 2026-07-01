"""
Manifold collector — a new lightweight read-only client.

Manifold is play-money (mana), so we don't *trade* there, but its prices are a
useful independent probability signal for the cross-venue consensus estimator
(a third opinion alongside Polymarket and Kalshi). Public API, no auth.

Docs: https://docs.manifold.markets/api  (GET /v0/search-markets)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import requests

from .base import Collector
from ..models import Market, Venue, make_market_id

API_URL = "https://api.manifold.markets/v0/search-markets"


def _ms_to_iso(ms) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return None


class ManifoldCollector(Collector):
    """Pull open BINARY markets from Manifold, most-liquid first."""

    venue = Venue.MANIFOLD.value

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def collect(self, *, limit: int = 500) -> List[Market]:
        markets: List[Market] = []
        offset = 0
        page = 200
        while len(markets) < limit:
            params = {
                "term": "",
                "filter": "open",
                "sort": "liquidity",
                "contractType": "BINARY",
                "limit": min(page, limit - len(markets)),
                "offset": offset,
            }
            try:
                r = self.session.get(API_URL, params=params, timeout=self.timeout)
                r.raise_for_status()
                batch = r.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                print(f"Manifold API error: {e}")
                break
            if not batch:
                break
            for m in batch:
                norm = self._normalize(m)
                if norm is not None:
                    markets.append(norm)
            offset += len(batch)
            if len(batch) < page:
                break
        return markets

    def _normalize(self, m: dict) -> Optional[Market]:
        mid = m.get("id")
        question = m.get("question")
        prob = m.get("probability")
        if not mid or not question or prob is None:
            return None
        try:
            yes_price = max(0.0, min(1.0, float(prob)))
        except (TypeError, ValueError):
            return None

        return Market(
            id=make_market_id(Venue.MANIFOLD, mid),
            venue=self.venue,
            venue_market_id=mid,
            question=question,
            yes_price=yes_price,
            no_price=round(1.0 - yes_price, 6),
            volume=_safe_float(m.get("volume")),
            volume_24h=_safe_float(m.get("volume24Hours")),
            liquidity=_safe_float(m.get("totalLiquidity")),
            category=None,
            close_time=_ms_to_iso(m.get("closeTime")),
            url=m.get("url"),
            is_active=not bool(m.get("isResolved", False)),
            is_resolved=bool(m.get("isResolved", False)),
            extra={"creator": m.get("creatorUsername"), "mechanism": m.get("mechanism")},
        )


def _safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
