"""
Signals — the Phase 3 information layer.

Collectors ingest information events (news, official releases, social) into the
``signals`` table; the relevance matcher links each signal to the markets it might
move; the LLM estimator then reads a market's linked signals and estimates the
true probability of the event — the "public data not yet priced in" edge.

    collectors (news/official/...)  ->  signals table
    relevance matcher               ->  signal_market_links
    llm estimator (Sonnet)          ->  per-market probability  ->  Opportunity
    decision layer (Opus)           ->  final go/size on top candidates
"""

from __future__ import annotations

__all__ = ["news", "relevance"]
