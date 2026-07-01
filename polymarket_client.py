"""
Polymarket public API client (read-only — no auth required).

Two endpoints we use:

    Gamma:   https://gamma-api.polymarket.com   (markets metadata, listings)
    CLOB:    https://clob.polymarket.com        (orderbook + price history)

This client only fetches *public* data. Placing orders on Polymarket requires
EIP-712 signed messages with a Polygon wallet — that's a separate concern and
not needed for the edge scanner. (Geo-blocked in the US; using NordVPN to a
non-US exit is on the user.)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests


GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


class PolymarketClient:
    """Read-only client for Polymarket Gamma + CLOB APIs."""

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def _get(self, base: str, path: str, params: Optional[Dict] = None) -> Any:
        url = f"{base}{path}"
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"Polymarket API error ({url}): {e}")
            return None

    # ==================== MARKETS ====================

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> List[Dict]:
        """
        List markets from the Gamma API.

        Defaults: active markets ordered by 24h volume descending.
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        data = self._get(GAMMA_URL, "/markets", params)
        if data is None:
            return []
        # Gamma returns either a list or {"data": [...]}; handle both.
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_high_volume_markets(
        self, min_volume_24h: float = 10_000, top_n: int = 50
    ) -> List[Dict]:
        """Top-N markets above a 24h volume floor."""
        markets = self.get_markets(limit=200, active=True, closed=False)
        filt = [
            m for m in markets
            if float(m.get("volume24hr") or 0) >= min_volume_24h
        ]
        filt.sort(key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
        return filt[:top_n]

    # ==================== PRICES ====================

    @staticmethod
    def _outcome_clob_ids(market: Dict) -> Dict[str, str]:
        """
        Map outcome label -> CLOB token id.
        Gamma returns these as stringified JSON arrays in `outcomes` and
        `clobTokenIds`.
        """
        import json

        outcomes = market.get("outcomes")
        token_ids = market.get("clobTokenIds")

        def _coerce(x: Any) -> List[str]:
            if isinstance(x, list):
                return [str(v) for v in x]
            if isinstance(x, str):
                try:
                    return [str(v) for v in json.loads(x)]
                except Exception:
                    return []
            return []

        outs = _coerce(outcomes)
        ids = _coerce(token_ids)
        return dict(zip(outs, ids))

    def get_yes_token_id(self, market: Dict) -> Optional[str]:
        """Return the YES (or 'Yes') outcome's CLOB token id, if available."""
        mapping = self._outcome_clob_ids(market)
        for key in ("Yes", "YES", "yes"):
            if key in mapping:
                return mapping[key]
        # Two-outcome market with unconventional labels: pick the first.
        if len(mapping) == 2:
            return next(iter(mapping.values()))
        return None

    def get_yes_no_token_ids(self, market: Dict) -> tuple[Optional[str], Optional[str]]:
        """Return (yes_token_id, no_token_id) for a two-outcome market."""
        mapping = self._outcome_clob_ids(market)
        yes_tok = None
        no_tok = None
        for k, v in mapping.items():
            kl = k.lower()
            if kl in ("yes", "y") or "yes" in kl:
                yes_tok = v
            elif kl in ("no", "n") or "no" in kl:
                no_tok = v
        if (yes_tok is None or no_tok is None) and len(mapping) == 2:
            # Fall back to position order: outcome 0 = YES, outcome 1 = NO
            items = list(mapping.values())
            yes_tok = yes_tok or items[0]
            no_tok = no_tok or items[1]
        return yes_tok, no_tok

    def get_price_history(
        self,
        token_id: str,
        days: int = 30,
        interval: Optional[str] = None,  # "1m","1w","1d","6h","1h","max"
        fidelity_minutes: int = 60,
    ) -> List[float]:
        """
        Return a list of YES prices in [0, 1] from the CLOB price-history endpoint.

        Two ways to request:
          - pass `interval` (rolling window, e.g. "1d", "1w", "max")
          - or let it compute startTs/endTs from `days`
        `fidelity_minutes` is the bucket size in minutes.
        """
        params: Dict[str, Any] = {
            "market": token_id,
            "fidelity": fidelity_minutes,
        }
        if interval is not None:
            params["interval"] = interval
        else:
            end_ts = int(datetime.utcnow().timestamp())
            start_ts = end_ts - days * 86400
            params["startTs"] = start_ts
            params["endTs"] = end_ts
        data = self._get(CLOB_URL, "/prices-history", params)
        if not data:
            return []
        history = data.get("history", []) if isinstance(data, dict) else []
        prices: List[float] = []
        for pt in history:
            p = pt.get("p")
            if p is None:
                continue
            try:
                pf = float(p)
            except (TypeError, ValueError):
                continue
            # Clip to [0,1] just in case of bad ticks
            prices.append(max(0.0, min(1.0, pf)))
        return prices

    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Top-of-book bids/asks for a CLOB token id."""
        return self._get(CLOB_URL, f"/book", {"token_id": token_id})

    # ==================== HELPERS ====================

    @staticmethod
    def get_current_yes_price(market: Dict) -> Optional[float]:
        """Best-effort current YES price as a float in [0, 1]."""
        for field in ("lastTradePrice", "outcomePrices"):
            v = market.get(field)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                # outcomePrices is a JSON-encoded array like '["0.62","0.38"]'
                import json
                try:
                    arr = json.loads(v)
                    if isinstance(arr, list) and arr:
                        return float(arr[0])
                except Exception:
                    try:
                        return float(v)
                    except ValueError:
                        pass
            if isinstance(v, list) and v:
                try:
                    return float(v[0])
                except (TypeError, ValueError):
                    continue
        return None


if __name__ == "__main__":
    client = PolymarketClient()
    print("Fetching top Polymarket markets...")
    markets = client.get_high_volume_markets(min_volume_24h=5_000, top_n=5)
    for m in markets:
        title = m.get("question") or m.get("title") or "?"
        vol = m.get("volume24hr")
        price = PolymarketClient.get_current_yes_price(m)
        print(f"- {title[:60]}  vol24={vol}  yes={price}")
        tok = client.get_yes_token_id(m)
        if tok:
            hist = client.get_price_history(tok, interval="1w", fidelity_minutes=360)
            print(f"  history points: {len(hist)}  last={hist[-1] if hist else None}")
            time.sleep(0.2)
