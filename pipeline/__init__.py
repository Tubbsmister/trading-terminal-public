"""
trading-terminal pipeline — a domain-general positive-EV betting engine.

The decision pipeline is identical for every prediction market, regardless of
domain (politics, macro, sports, geopolitics, ...):

    collect markets  ->  estimate P(event)  ->  compare to price (net of costs)
    ->  if edge is real, size by fractional Kelly  ->  execute within guardrails
    ->  log & learn (score predicted vs realized, recalibrate)

Only the *probability estimate* differs by domain; the rest of the machine is
shared. This package builds that shared machine once. It reuses the existing
read-only clients (``polymarket_client``, ``kalshi_client``) and the live
execution / ledger layers (``polymarket_trader``, ``poly_ledger``) rather than
reimplementing them.

Phase 0 (this commit): storage layer + ``Market`` / ``Opportunity`` models +
collectors for Polymarket, Kalshi, and Manifold. No edges, no money — just a
normalized, persisted view of every market across venues.
"""

from __future__ import annotations

__all__ = ["models", "store", "collectors"]
