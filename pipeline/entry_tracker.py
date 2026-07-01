"""
Thesis entry tracker — wait for the market to come to your thesis.

The thesis engine finds SOUND theses, but many sit on markets that are fairly priced
RIGHT NOW (thin edge), so nothing is bet. That doesn't make the thesis useless: prices
move, and when the crowd temporarily over-weights the side your thesis bets AGAINST, your
side gets cheap — the optimal entry.

  Fable-5 example: thesis = NO (it won't return by the deadline; fair P(YES) ~0.05, so NO
  is worth ~0.95). Normally NO trades ~0.93 — a thin ~2c edge, not worth it. But when the
  market SPIKED to ~0.70 YES, NO collapsed to ~0.30 — a ~0.65 edge against fair value, a
  screaming buy. An entry tracker would have fired exactly there.

This module registers a WATCH for every surviving thesis and, each cycle, recomputes the
edge at the CURRENT price. When the price has moved enough that the bet clears the edge
threshold, it fires an entry ``Opportunity`` (edge_source="thesis_entry") into the normal
sizing / guardrail / paper-tracking path, and marks the watch triggered. It is thesis-driven
limit-order discipline: pick the price your thesis says is a steal, then wait for it.

    python -m pipeline.entry_tracker            # sync watches + check prices, report
    python -m pipeline.entry_tracker --no-sync  # only re-check existing watches

Price granularity = the collect cadence (markets are refreshed each cycle), so sustained
favorable moves are caught; sub-cycle spikes would need live polling (future).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import List, Optional

from .guardrails import GuardrailConfig, PortfolioState, apply
from .models import Opportunity
from .sizing import KellySizer, ev_per_dollar
from .store import Store, get_store, utcnow_iso
from .thesis import COST_BUFFER, MIN_EDGE

_CREATE_WATCHES = """
CREATE TABLE IF NOT EXISTS thesis_watches (
    thesis_id    INTEGER PRIMARY KEY,
    market_id    TEXT NOT NULL,
    venue        TEXT,
    side         TEXT,
    fair_prob    REAL,
    question     TEXT,
    created_at   TEXT,
    status       TEXT DEFAULT 'watching',   -- watching | triggered | expired
    best_edge    REAL,
    best_price   REAL,
    trigger_edge REAL,
    trigger_price REAL,
    triggered_at TEXT
);
"""


def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute(_CREATE_WATCHES)
    return c


# ───────────────────────── edge math (pure, testable) ─────────────────────────

def entry_edge(side: str, fair_prob: float, yes_price: float,
               no_price: Optional[float] = None, venue: str = "poly") -> tuple:
    """Post-cost EV/$ for entering ``side`` at the current price, given our fair P(YES).

    Returns (edge, our_side_price). This is the SAME calculation the thesis engine uses to
    decide bettability — here it's recomputed every cycle as the price moves, so a thesis
    that was un-bettable at generation can become bettable on a favorable swing.
    """
    side = (side or "YES").upper()
    cost = COST_BUFFER.get(venue, 0.02)
    if side == "YES":
        price, fair = yes_price, fair_prob
    else:
        price = no_price if no_price is not None else round(1.0 - yes_price, 6)
        fair = 1.0 - fair_prob
    if not price or price <= 0:
        return -1.0, price
    return ev_per_dollar(price, fair) - cost, price


# ───────────────────────── watch lifecycle ─────────────────────────

def sync_watches(store: Optional[Store] = None) -> int:
    """Register a watch for every SURVIVED thesis not already watched. Idempotent.
    Returns the number of new watches created."""
    store = store or get_store()
    db_path = str(store.db_path)
    new = 0
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT id, market_id, venue, side, fair_prob, question, detail "
            "FROM theses WHERE status='survived'"
        ).fetchall()
        for r in rows:
            exists = c.execute(
                "SELECT 1 FROM thesis_watches WHERE thesis_id=?", (r["id"],)
            ).fetchone()
            if exists:
                continue
            d = json.loads(r["detail"]) if r["detail"] else {}
            # the critic-adjusted estimate is the system's final P(YES); fall back to gen.
            fair = d.get("adjusted_fair_prob")
            if not isinstance(fair, (int, float)):
                fair = r["fair_prob"]
            c.execute(
                "INSERT INTO thesis_watches (thesis_id, market_id, venue, side, fair_prob, "
                "question, created_at, status) VALUES (?,?,?,?,?,?,?,'watching')",
                (r["id"], r["market_id"], r["venue"], (r["side"] or "YES").upper(),
                 float(fair), r["question"], utcnow_iso()),
            )
            new += 1
    return new


def run_watches(store: Optional[Store] = None, *, verbose: bool = True) -> List[Opportunity]:
    """Re-check every active watch at the current price; fire entries that clear the edge
    threshold. Returns the list of fired entry Opportunities (sized + guardrailed)."""
    store = store or get_store()
    db_path = str(store.db_path)
    fired: List[Opportunity] = []
    sizer = KellySizer(bankroll=_bankroll(), multiplier=0.25, max_fraction=0.05, min_stake=1.0)
    cfg = GuardrailConfig()
    state = PortfolioState(bankroll=_bankroll())

    with _conn(db_path) as c:
        watches = c.execute(
            "SELECT * FROM thesis_watches WHERE status='watching'"
        ).fetchall()

        for w in watches:
            market = store.get_market(w["market_id"])
            if market is None or market.is_resolved or market.yes_price is None:
                c.execute("UPDATE thesis_watches SET status='expired' WHERE thesis_id=?",
                          (w["thesis_id"],))
                continue

            edge, side_price = entry_edge(
                w["side"], w["fair_prob"], market.yes_price, market.no_price, w["venue"])

            # track the best (highest) edge seen so we can see how close watches have come
            best_edge = w["best_edge"]
            if best_edge is None or edge > best_edge:
                c.execute(
                    "UPDATE thesis_watches SET best_edge=?, best_price=? WHERE thesis_id=?",
                    (round(edge, 6), round(side_price, 6), w["thesis_id"]))

            if verbose:
                print(f"  [watch] {w['side']:>3} edge {edge:+.3f} (need >={MIN_EDGE:.2f}) "
                      f"@ price {side_price:.3f} | {w['question'][:48]}")

            if edge >= MIN_EDGE:
                opp = _fire(market, w, edge, side_price, sizer, cfg, state, store)
                if opp is not None:
                    fired.append(opp)
                    c.execute(
                        "UPDATE thesis_watches SET status='triggered', trigger_edge=?, "
                        "trigger_price=?, triggered_at=? WHERE thesis_id=?",
                        (round(edge, 6), round(side_price, 6), utcnow_iso(), w["thesis_id"]))
                    if verbose:
                        print(f"    >>> ENTRY FIRED: buy {w['side']} @ {side_price:.3f} "
                              f"edge {edge:+.3f} stake ${opp.recommended_stake or 0:.2f}")
    return fired


def _fire(market, w, edge, side_price, sizer, cfg, state, store) -> Optional[Opportunity]:
    """Build, size, guardrail and persist an entry opportunity for a triggered watch."""
    fair_yes = w["fair_prob"]
    fair_side = fair_yes if w["side"] == "YES" else (1.0 - fair_yes)
    opp = Opportunity(
        market_id=w["market_id"], venue=w["venue"], question=w["question"],
        side=w["side"], market_price=round(side_price, 6), fair_prob=round(fair_side, 6),
        edge=round(edge, 6), edge_source="thesis_entry", confidence=0.5,
        liquidity=market.liquidity,
        detail={"thesis_id": w["thesis_id"], "trigger": "price moved to thesis target",
                "fair_yes": fair_yes},
    )
    r = sizer.size(opp.market_price, opp.fair_prob)
    opp.recommended_stake = r.stake
    opp.kelly_fraction = r.fraction
    approved, rejected = apply([opp], state, cfg)
    final = (approved or rejected)[0]
    try:
        store.add_opportunity(final)
    except Exception as e:  # noqa: BLE001 — never crash the cycle on persistence
        print(f"    [entry] warn: could not persist opportunity: {e}")
    return final if final in approved else None


def _bankroll() -> float:
    import os
    return float(os.environ.get("PIPELINE_BANKROLL", os.environ.get("POLY_BANKROLL", "1000")))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Thesis entry tracker: wait for the price to come to you")
    ap.add_argument("--no-sync", action="store_true", help="don't register new watches first")
    args = ap.parse_args(argv)

    store = get_store()
    if not args.no_sync:
        n = sync_watches(store)
        print(f"synced watches (+{n} new) from surviving theses")
    fired = run_watches(store)
    print(f"\nentry signals fired this run: {len(fired)}")
    for o in fired:
        print(f"  BUY {o.side} @ {o.market_price:.3f} (edge {o.edge:+.3f}, "
              f"${o.recommended_stake or 0:.2f}) | {o.question[:55]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
