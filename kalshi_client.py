"""
Kalshi API Client - No Authentication Required for Public Endpoints
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import time


class KalshiClient:
    """Client for Kalshi's public API endpoints."""

    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, base_url: Optional[str] = None):
        # Override via constructor or KALSHI_BASE_URL env var if elections subdomain
        # is geo-limited from your VPN exit (try https://trading-api.kalshi.com/trade-api/v2).
        import os
        self.BASE_URL = base_url or os.environ.get("KALSHI_BASE_URL", self.DEFAULT_BASE_URL)
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json"
        })
    
    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make API request with error handling."""
        url = f"{self.BASE_URL}{endpoint}"
        
        try:
            response = self.session.request(method, url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API Error: {e}")
            return {}
    
    # ==================== MARKETS ====================
    
    def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: Optional[str] = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        tickers: Optional[List[str]] = None
    ) -> Dict:
        """
        Get list of markets.
        
        Args:
            limit: Results per page (1-1000)
            status: 'open', 'closed', 'settled', or None for all
            series_ticker: Filter by series
            event_ticker: Filter by event
            tickers: List of specific market tickers
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        
        return self._request("GET", "/markets", params)
    
    def get_market(self, ticker: str) -> Dict:
        """Get details for a specific market."""
        return self._request("GET", f"/markets/{ticker}")
    
    # ==================== TRADES ====================
    
    def get_trades(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        ticker: Optional[str] = None,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None
    ) -> Dict:
        """
        Get recent trades across all markets or for a specific market.
        
        Args:
            limit: Results per page (1-1000)
            ticker: Filter by specific market ticker
            min_ts: Filter trades after this Unix timestamp
            max_ts: Filter trades before this Unix timestamp
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        
        return self._request("GET", "/markets/trades", params)
    
    def get_all_recent_trades(self, hours: int = 24, max_trades: int = 5000) -> List[Dict]:
        """
        Fetch all trades from the last N hours with pagination.
        """
        min_ts = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
        all_trades = []
        cursor = None
        
        while len(all_trades) < max_trades:
            result = self.get_trades(limit=1000, cursor=cursor, min_ts=min_ts)
            trades = result.get("trades", [])
            
            if not trades:
                break
            
            all_trades.extend(trades)
            cursor = result.get("cursor")
            
            if not cursor:
                break
            
            time.sleep(0.1)  # Rate limiting
        
        return all_trades[:max_trades]
    
    # ==================== ORDERBOOK ====================
    
    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        """
        Get current orderbook for a market.
        
        Args:
            ticker: Market ticker
            depth: Number of price levels to return
        """
        params = {"depth": depth}
        return self._request("GET", f"/markets/{ticker}/orderbook", params)
    
    # ==================== CANDLESTICKS ====================
    
    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        period_interval: int = 60,  # 1=1min, 60=1hr, 1440=1day
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None
    ) -> Dict:
        """
        Get OHLC candlestick data for a market.
        
        Args:
            series_ticker: The series ticker (e.g., 'KXHIGHNY')
            market_ticker: The market ticker
            period_interval: 1 (1min), 60 (1hr), or 1440 (1day)
            start_ts: Start Unix timestamp
            end_ts: End Unix timestamp
        """
        params = {"period_interval": period_interval}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        
        return self._request(
            "GET", 
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params
        )
    
    def get_price_history(
        self,
        series_ticker: str,
        market_ticker: str,
        days: int = 30,
        period_interval: int = 1440,  # 1440 = daily; 60 = hourly
    ) -> List[float]:
        """
        Convenience wrapper: returns a list of YES close prices in [0, 1]
        for the last `days` of history. Used as input to the Markov engine.
        """
        end_ts = int(datetime.utcnow().timestamp())
        start_ts = end_ts - days * 86400
        data = self.get_candlesticks(
            series_ticker=series_ticker,
            market_ticker=market_ticker,
            period_interval=period_interval,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        candles = data.get("candlesticks", []) or []
        prices: List[float] = []
        for c in candles:
            # Kalshi exposes 'price' (mid/last) and 'yes_bid'/'yes_ask' OHLC bundles.
            # Prefer the executed price close; fall back to the yes_bid/ask midpoint.
            close = None
            price_field = c.get("price")
            if isinstance(price_field, dict):
                close = price_field.get("close")
            if close is None:
                yb = (c.get("yes_bid") or {}).get("close")
                ya = (c.get("yes_ask") or {}).get("close")
                if yb is not None and ya is not None:
                    close = (yb + ya) / 2
                elif yb is not None:
                    close = yb
                elif ya is not None:
                    close = ya
            if close is not None:
                prices.append(close / 100.0)  # cents -> [0,1]
        return prices

    def get_batch_candlesticks(
        self,
        tickers: List[str],
        period_interval: int = 60,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None
    ) -> Dict:
        """
        Get candlesticks for multiple markets at once (up to 100).
        """
        params = {
            "tickers": ",".join(tickers[:100]),
            "period_interval": period_interval
        }
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        
        return self._request("GET", "/markets/candlesticks", params)
    
    # ==================== SERIES & EVENTS ====================
    
    def get_series(self, series_ticker: str) -> Dict:
        """Get information about a series."""
        return self._request("GET", f"/series/{series_ticker}")
    
    def get_series_list(self, limit: int = 100) -> Dict:
        """Get list of all series."""
        return self._request("GET", "/series", {"limit": limit})
    
    def get_events(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: Optional[str] = "open",
        series_ticker: Optional[str] = None,
        with_nested_markets: bool = False
    ) -> Dict:
        """
        Get list of events.
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        
        return self._request("GET", "/events", params)
    
    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> Dict:
        """Get details for a specific event."""
        params = {}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", f"/events/{event_ticker}", params)
    
    # ==================== EXCHANGE STATUS ====================
    
    def get_exchange_status(self) -> Dict:
        """Get current exchange status."""
        return self._request("GET", "/exchange/status")


# Quick test
if __name__ == "__main__":
    client = KalshiClient()
    
    # Test: Get exchange status
    status = client.get_exchange_status()
    print("Exchange Status:", status)
    
    # Test: Get some open markets
    markets = client.get_markets(limit=5)
    print(f"\nFound {len(markets.get('markets', []))} markets")
    for m in markets.get("markets", [])[:3]:
        print(f"  - {m.get('ticker')}: {m.get('title')[:50]}...")
