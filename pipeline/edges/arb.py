"""
Arbitrage estimators.

Two very different animals share this file:

1. ``within_market_arb`` — RISKLESS. On a single venue, if you can buy YES and NO
   for less than $1 combined (after fees), you lock a guaranteed profit no matter
   how the market resolves. No probability estimate, no basis risk, no matching.
   Rare on liquid markets but trivially correct, so it's auto-approvable.

2. ``cross_venue_candidates`` — NOT riskless, NOT auto-bet. The same *event* priced
   differently on Polymarket vs Kalshi looks like free money, but only if the two
   contracts resolve on identical criteria. Mismatched resolution rules ("basis
   risk") are the #1 way this trade loses. Reliable matching + rule verification is
   an LLM job (Phase 3). For now we surface high-confidence text matches with a
   price gap as *review candidates* (status stays CANDIDATE) so a human — or, later,
   Sonnet — can verify the rules before anything is staked.
"""

from __future__ import annotations

import re
from typing import Dict, List

from ..models import Market, Opportunity, OppStatus, Venue

# Kalshi per-contract fee ≈ 0.07·p·(1-p); Polymarket taker ≈ 2% of cost.
def _kalshi_fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


# ───────────────────── within-market riskless arb ─────────────────────

def within_market_arb(markets: List[Market]) -> List[Opportunity]:
    """Find single-venue YES+NO < $1 (post-fee) locks.

    Requires REAL executable two-sided ask quotes on both sides — only then is the
    profit actually lockable. We currently have those only for Kalshi (``yes_ask``/
    ``no_ask`` in ``extra``). Polymarket's ``yes_price``/``no_price`` are mid/last
    quotes (and for some markets don't even sum to ~1), so they are NOT arbitrageable
    and must not be used here — doing so invents phantom arb from price noise.
    (Real Poly arb would need a CLOB order-book fetch — a later refinement.)
    """
    out: List[Opportunity] = []
    for m in markets:
        extra = m.extra or {}
        yes_ask = extra.get("yes_ask")
        no_ask = extra.get("no_ask")
        if yes_ask is None or no_ask is None:
            continue  # no executable two-sided quote -> not lockable, skip

        pair_cost = yes_ask + no_ask
        if pair_cost <= 0 or pair_cost >= 1.0:
            continue

        fee = _kalshi_fee(yes_ask) + _kalshi_fee(no_ask) if m.venue == Venue.KALSHI.value else 0.0
        profit_per_pair = 1.0 - pair_cost - fee
        if profit_per_pair <= 0:
            continue
        edge = profit_per_pair / pair_cost  # guaranteed return per $1 staked

        out.append(Opportunity(
            market_id=m.id,
            venue=m.venue,
            question=m.question,
            side="BOTH",
            market_price=round(pair_cost, 6),
            fair_prob=1.0,                  # pays $1 with certainty
            edge=round(edge, 6),
            edge_source="arb",
            confidence=0.99,
            liquidity=m.liquidity,
            detail={"yes_ask": yes_ask, "no_ask": no_ask,
                    "pair_cost": round(pair_cost, 6), "fee": round(fee, 6),
                    "kind": "within_market_riskless"},
        ))
    return out


# ───────────────────── cross-venue review candidates ─────────────────────

_STOP = {"will", "the", "be", "in", "on", "of", "to", "a", "an", "by", "at", "for",
         "before", "after", "than", "or", "and", "is", "are", "this", "that", "20"}


def _tokens(q: str) -> set:
    words = re.findall(r"[a-z0-9]+", q.lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def _similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cross_venue_candidates(
    markets: List[Market], *, min_gap: float = 0.05, min_sim: float = 0.6
) -> List[Opportunity]:
    """Surface Poly↔Kalshi markets that look like the same event but disagree on
    price by > ``min_gap``. Returned as CANDIDATE (review-only) — never auto-bet."""
    poly = [m for m in markets if m.venue == Venue.POLYMARKET.value and m.yes_price is not None]
    kalshi = [m for m in markets if m.venue == Venue.KALSHI.value and m.yes_price is not None]
    poly_tok: Dict[str, set] = {m.id: _tokens(m.question) for m in poly}
    kalshi_tok: Dict[str, set] = {m.id: _tokens(m.question) for m in kalshi}

    out: List[Opportunity] = []
    for pm in poly:
        pt = poly_tok[pm.id]
        best = None
        best_sim = min_sim
        for km in kalshi:
            sim = _similarity(pt, kalshi_tok[km.id])
            if sim >= best_sim:
                best_sim, best = sim, km
        if best is None:
            continue
        gap = abs(pm.yes_price - best.yes_price)
        if gap < min_gap:
            continue
        # cheaper venue to buy YES = the one with the lower yes price
        cheap, dear = (pm, best) if pm.yes_price < best.yes_price else (best, pm)
        out.append(Opportunity(
            market_id=cheap.id,
            venue=cheap.venue,
            question=cheap.question,
            side="YES",
            market_price=round(cheap.yes_price, 6),
            fair_prob=round(dear.yes_price, 6),   # other venue's price as the "fair" ref
            edge=round(gap, 6),
            edge_source="arb_xvenue",
            confidence=round(best_sim, 3),
            liquidity=cheap.liquidity,
            status=OppStatus.CANDIDATE.value,      # review-only
            guardrail_notes="UNVERIFIED resolution rules — basis risk; do not auto-bet",
            detail={
                "match_similarity": round(best_sim, 3),
                "poly": {"id": pm.id, "yes": pm.yes_price, "q": pm.question},
                "kalshi": {"id": best.id, "yes": best.yes_price, "q": best.question},
                "price_gap": round(gap, 6),
            },
        ))
    return out
