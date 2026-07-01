"""
Collectors: pull markets from each venue and normalize them to ``Market``.

Every collector exposes the same interface (``Collector.collect() -> list[Market]``)
so the orchestration loop can fan out across venues without knowing venue
details. Polymarket and Kalshi collectors wrap the existing read-only clients
(``polymarket_client``, ``kalshi_client``); Manifold is a new lightweight client.
"""

from __future__ import annotations

from .base import Collector
from .polymarket import PolymarketCollector
from .kalshi import KalshiCollector
from .manifold import ManifoldCollector

ALL_COLLECTORS = {
    "poly": PolymarketCollector,
    "kalshi": KalshiCollector,
    "manifold": ManifoldCollector,
}

__all__ = [
    "Collector",
    "PolymarketCollector",
    "KalshiCollector",
    "ManifoldCollector",
    "ALL_COLLECTORS",
]
