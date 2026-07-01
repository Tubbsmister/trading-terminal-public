"""Polymarket collector — wraps the existing read-only ``PolymarketClient``."""

from __future__ import annotations

import json
from typing import List, Optional

from .. import rootpath  # noqa: F401  (puts repo root on sys.path)
from polymarket_client import PolymarketClient

from .base import Collector
from ..models import Market, Venue, make_market_id


def _coerce_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class PolymarketCollector(Collector):
    """Pull active binary markets from Polymarket Gamma, ordered by 24h volume.

    Geo-blocked in the US — assumes a non-US VPN exit is up (same assumption the
    rest of the Polymarket tooling makes). Returns only two-outcome (YES/NO)
    markets; multi-outcome events are skipped in Phase 0.
    """

    venue = Venue.POLYMARKET.value

    def __init__(self, client: Optional[PolymarketClient] = None):
        self.client = client or PolymarketClient()

    def collect(self, *, limit: int = 500) -> List[Market]:
        markets: List[Market] = []
        page = 100  # Gamma caps a single /markets response at ~100 rows
        offset = 0
        # Bound by raw rows fetched (not normalized count) so a flood of multi-outcome
        # markets that all get filtered can't spin the loop forever; but keep paging
        # until we have `limit` BINARY markets or the API runs dry.
        max_pages = max(5, (limit // page) * 4)
        pages = 0
        while len(markets) < limit and pages < max_pages:
            batch = self.client.get_markets(
                limit=page,
                offset=offset,
                active=True,
                closed=False,
                order="volume24hr",
            )
            pages += 1
            if not batch:
                break
            for m in batch:
                norm = self._normalize(m)
                if norm is not None:
                    markets.append(norm)
            offset += len(batch)
        return markets[:limit]

    def _normalize(self, m: dict) -> Optional[Market]:
        question = m.get("question") or m.get("title")
        if not question:
            return None

        # Only binary (two-outcome) markets in Phase 0.
        mapping = PolymarketClient._outcome_clob_ids(m)
        if len(mapping) != 2:
            return None

        vmid = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if not vmid:
            return None

        yes_price = PolymarketClient.get_current_yes_price(m)
        no_price = None
        raw_prices = m.get("outcomePrices")
        if isinstance(raw_prices, str):
            try:
                arr = json.loads(raw_prices)
                if isinstance(arr, list) and len(arr) >= 2:
                    no_price = _coerce_float(arr[1])
            except (ValueError, TypeError):
                pass
        if no_price is None and yes_price is not None:
            no_price = round(1.0 - yes_price, 6)

        slug = m.get("slug")
        yes_tok, no_tok = self.client.get_yes_no_token_ids(m)

        return Market(
            id=make_market_id(Venue.POLYMARKET, vmid),
            venue=self.venue,
            venue_market_id=vmid,
            question=question,
            yes_price=yes_price,
            no_price=no_price,
            volume=_coerce_float(m.get("volume")),
            volume_24h=_coerce_float(m.get("volume24hr")),
            liquidity=_coerce_float(m.get("liquidityNum") or m.get("liquidity")),
            category=m.get("category"),
            close_time=m.get("endDate") or m.get("endDateIso"),
            url=f"https://polymarket.com/event/{slug}" if slug else None,
            is_active=bool(m.get("active", True)) and not bool(m.get("closed", False)),
            is_resolved=bool(m.get("closed", False)),
            extra={
                "condition_id": m.get("conditionId"),
                "slug": slug,
                "neg_risk": bool(m.get("negRisk", False)),
                "yes_token_id": yes_tok,
                "no_token_id": no_tok,
                "outcomes": list(mapping.keys()),
            },
        )
