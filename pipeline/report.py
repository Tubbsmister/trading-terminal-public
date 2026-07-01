"""
Read-only CLI dashboard for the prediction-market pipeline DB.

Why this exists: after running ``python -m pipeline.collect`` and
``python -m pipeline.scan``, it can be hard to remember what is in the DB.
This command gives a human a quick, at-a-glance picture of:

* how many markets we are tracking (and from where)
* what opportunities the edge engine found and which ones cleared guardrails
* current paper exposure vs. the configured bankroll
* cross-venue pairs that need a human review before any bet

This is strictly read-only — it never modifies the DB.

Usage::

    python -m pipeline.report
    python -m pipeline.report --top 20
    python -m pipeline.report --venue kalshi
"""

from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from .store import get_store


# ───────────────────────── helpers ─────────────────────────

def _trunc(text: str, width: int) -> str:
    """Truncate *text* to *width* characters, adding '...' if clipped."""
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "  n/a"
    return f"{p:.3f}"


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


# ───────────────────────── sections ─────────────────────────

def _section_header(store, venue_filter: Optional[str]) -> None:
    """Print DB path, market counts, and freshness timestamp."""
    print(f"DB : {store.db_path}")

    # total row count (all statuses)
    try:
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        total_row = conn.execute("SELECT COUNT(*) AS n FROM markets").fetchone()
        total_markets = total_row["n"] if total_row else 0
        latest_row = conn.execute(
            "SELECT MAX(collected_at) AS ts FROM markets"
        ).fetchone()
        latest_ts = latest_row["ts"] if latest_row else None
        conn.close()
    except Exception:
        total_markets = 0
        latest_ts = None

    active_by_venue: Dict[str, int] = store.count_markets(active_only=True)

    print(f"Markets (total rows): {total_markets}")
    print(f"Active & unresolved : {sum(active_by_venue.values())}")
    for venue, n in sorted(active_by_venue.items()):
        marker = " <--" if venue_filter and venue == venue_filter else ""
        print(f"  {venue:<12} {n:>5}{marker}")
    if latest_ts:
        print(f"Last collected_at   : {latest_ts}")
    else:
        print("Last collected_at   : (no data)")


def _section_opp_summary(store, bankroll: float) -> Optional[float]:
    """Print opportunity counts and exposure; return approved stake total."""
    try:
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row

        # counts by status
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status ORDER BY status"
        ).fetchall()

        # counts by edge_source
        source_rows = conn.execute(
            "SELECT edge_source, COUNT(*) AS n FROM opportunities GROUP BY edge_source ORDER BY n DESC"
        ).fetchall()

        # approved exposure
        exp_row = conn.execute(
            "SELECT SUM(COALESCE(recommended_stake, 0)) AS total "
            "FROM opportunities WHERE status = 'approved'"
        ).fetchone()
        conn.close()
    except Exception as exc:
        print(f"  (could not query opportunities: {exc})")
        return None

    if not status_rows:
        print("  No opportunities in DB yet. Run: python -m pipeline.scan")
        return None

    print()
    print("  By status:")
    for r in status_rows:
        print(f"    {r['status']:<12}  {r['n']:>5}")

    print()
    print("  By edge source:")
    for r in source_rows:
        print(f"    {r['edge_source']:<18}  {r['n']:>5}")

    approved_stake = float(exp_row["total"] or 0) if exp_row else 0.0
    pct = _safe_div(approved_stake * 100.0, bankroll)
    print()
    print(
        f"  Paper exposure (approved): ${approved_stake:>9,.2f}  "
        f"({pct:.1f}% of ${bankroll:,.0f} bankroll)"
    )
    return approved_stake


def _section_top_bets(store, top_n: int, venue_filter: Optional[str]) -> None:
    """Print a table of the top N approved opportunities sorted by edge."""
    from .store import get_store  # already imported at top, but keep self-contained
    from .models import Opportunity

    opps = store.get_opportunities(status="approved", limit=top_n)
    if venue_filter:
        opps = [o for o in opps if o.venue == venue_filter]
    if not opps:
        print("  (no approved opportunities)")
        return

    # header row
    print(
        f"  {'#':>3}  {'edge':>6}  {'EV/$':>6}  {'stake':>8}  "
        f"{'price':>6}  {'fair':>6}  {'side':>3}  "
        f"{'source':<13}  question"
    )
    print("  " + "-" * 108)
    for i, o in enumerate(opps, 1):
        # EV/$ = fair_prob / market_price - 1
        ev_per_dollar = _safe_div(o.fair_prob, o.market_price) - 1.0
        stake_str = (
            f"${o.recommended_stake:>7,.2f}" if o.recommended_stake is not None else "    n/a"
        )
        q = _trunc(o.question, 50)
        print(
            f"  {i:>3}  {o.edge:6.3f}  {ev_per_dollar:6.3f}  {stake_str}  "
            f"{_fmt_price(o.market_price)}  {_fmt_price(o.fair_prob)}  "
            f"{o.side:>3}  {o.edge_source:<13}  {q}"
        )


def _section_xvenue(store, limit: int = 30) -> None:
    """List cross-venue arb review candidates."""
    try:
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT edge, confidence, detail FROM opportunities "
            "WHERE edge_source = 'arb_xvenue' AND status = 'candidate' "
            "ORDER BY edge DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        print(f"  (could not query cross-venue candidates: {exc})")
        return

    if not rows:
        print("  (none)")
        return

    import json as _json

    for r in rows:
        try:
            d: Dict[str, Any] = _json.loads(r["detail"]) if r["detail"] else {}
        except Exception:
            d = {}

        poly = d.get("poly", {})
        kalshi = d.get("kalshi", {})
        poly_yes = poly.get("yes", float("nan"))
        kalshi_yes = kalshi.get("yes", float("nan"))
        poly_q = _trunc(str(poly.get("q", "(no question)")), 60)
        kalshi_q = _trunc(str(kalshi.get("q", "(no question)")), 60)
        conf = r["confidence"]

        print(
            f"  gap {r['edge']:.3f}"
            + (f"  sim {conf:.2f}" if conf is not None else "")
        )
        try:
            print(f"    poly   {poly_yes:.3f}  {poly_q}")
        except (TypeError, ValueError):
            print(f"    poly   n/a   {poly_q}")
        try:
            print(f"    kalshi {kalshi_yes:.3f}  {kalshi_q}")
        except (TypeError, ValueError):
            print(f"    kalshi n/a   {kalshi_q}")


# ───────────────────────── main ─────────────────────────

def main(argv=None) -> int:
    """Entry point; returns 0 on success."""
    # Windows consoles default to cp1252; market questions carry accented chars.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="Read-only dashboard for the pipeline SQLite DB.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--top", type=int, default=15, metavar="N",
        help="max approved bets to display (default 15)",
    )
    ap.add_argument(
        "--venue", default=None,
        help="filter markets/bets to a single venue (e.g. poly, kalshi, manifold)",
    )
    args = ap.parse_args(argv)

    bankroll = float(os.environ.get("POLY_BANKROLL", 1000.0))

    store = get_store()

    # check the DB exists and has data
    try:
        counts = store.count_markets(active_only=True)
    except Exception as exc:
        print(f"Could not open DB: {exc}")
        print("No data found. Run: python -m pipeline.collect")
        return 1

    if not counts:
        print(f"DB: {store.db_path}")
        print("No data found. Run: python -m pipeline.collect")
        return 0

    # ── 1. Header ────────────────────────────────────────────────────────
    print("=" * 70)
    print("PIPELINE DASHBOARD")
    print("=" * 70)
    _section_header(store, args.venue)

    # ── 2. Opportunities summary ─────────────────────────────────────────
    print()
    print("=" * 70)
    print("OPPORTUNITIES SUMMARY")
    print("=" * 70)
    _section_opp_summary(store, bankroll)

    # ── 3. Top approved paper bets ───────────────────────────────────────
    print()
    print("=" * 70)
    print(f"TOP {args.top} APPROVED PAPER BETS (sorted by edge)")
    print("=" * 70)
    _section_top_bets(store, args.top, args.venue)

    # ── 4. Cross-venue review candidates ────────────────────────────────
    print()
    print("=" * 70)
    print("CROSS-VENUE REVIEW CANDIDATES  (arb_xvenue / do NOT auto-bet)")
    print("=" * 70)
    _section_xvenue(store)

    # ── 5. Footer ────────────────────────────────────────────────────────
    print()
    print("-" * 70)
    print(
        "READ-ONLY view. To refresh: python -m pipeline.collect  "
        "| To rescan: python -m pipeline.scan"
    )
    print("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
