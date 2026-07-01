"""
Resolution-scoring loop — the "learn" phase of the trading pipeline.

Why this exists: we log paper bets as Opportunities, but a bet is only
evidence about model quality once we know who *won*.  This module closes
that feedback loop:

  1.  For every approved/executed Opportunity, look up the underlying
      market on its native venue (Polymarket Gamma or Kalshi) and check
      whether it has resolved.
  2.  Cache each resolution into a local ``resolutions`` SQLite table
      (market_id, resolved_yes, resolved_at, source).
  3.  For all resolved bets, compute realized paper P&L and Brier scores
      (model probability vs. market-price baseline), then print a
      scoreboard grouped by estimator (edge_source).

Running more often is harmless: already-resolved markets are not
re-fetched; unresolved ones are polled again each time.

Usage:
    python -m pipeline.learn              # refresh from venues, then score
    python -m pipeline.learn --no-refresh # score from cached data only
    python -m pipeline.learn --limit 200  # cap opportunities considered
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple

import requests

from .models import Opportunity, OppStatus
from .store import get_store, utcnow_iso


# ---------------------------------------------------------------------------
# resolutions table (lives alongside the existing pipeline tables in the
# same DB file; created here so we never need to touch store.py)
# ---------------------------------------------------------------------------

_CREATE_RESOLUTIONS = """
CREATE TABLE IF NOT EXISTS resolutions (
    market_id    TEXT PRIMARY KEY,
    resolved_yes INTEGER NOT NULL,
    resolved_at  TEXT NOT NULL,
    source       TEXT NOT NULL
);
"""

# Kalshi statuses that mean "definitively settled"
_KALSHI_SETTLED = {"settled", "finalized", "determined"}

GAMMA_URL = "https://gamma-api.polymarket.com"
# CLOB is keyed natively by condition_id (our venue_market_id is the 0x… condition_id).
# Gamma's /markets/{id} path expects a NUMERIC id and 422s on a condition_id, which is
# why poly resolution detection silently failed for every market before this.
CLOB_URL = "https://clob.polymarket.com"


def _init_resolutions_table(db_path: str) -> None:
    """Create the ``resolutions`` table if it does not already exist."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_CREATE_RESOLUTIONS)
        conn.commit()
    finally:
        conn.close()


def _fetch_cached_resolution(
    db_path: str, market_id: str
) -> Optional[Tuple[bool, str, str]]:
    """Return ``(resolved_yes, resolved_at, source)`` if cached, else ``None``."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT resolved_yes, resolved_at, source FROM resolutions WHERE market_id = ?",
            (market_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (bool(row[0]), row[1], row[2])


def _upsert_resolution(
    db_path: str,
    market_id: str,
    resolved_yes: bool,
    resolved_at: str,
    source: str,
) -> None:
    """Insert or update one resolution record."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            INSERT INTO resolutions (market_id, resolved_yes, resolved_at, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                resolved_yes = excluded.resolved_yes,
                resolved_at  = excluded.resolved_at,
                source       = excluded.source
            """,
            (market_id, int(resolved_yes), resolved_at, source),
        )
        conn.commit()
    finally:
        conn.close()


def _all_resolutions(db_path: str) -> Dict[str, bool]:
    """Return every cached resolution as ``{market_id: resolved_yes}``."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT market_id, resolved_yes FROM resolutions"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: bool(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# venue resolution detection
# ---------------------------------------------------------------------------

def _coerce_json_list(x) -> list:
    """Parse a JSON-encoded list or return an already-list value."""
    if isinstance(x, list):
        return x
    if not x:
        return []
    try:
        parsed = json.loads(x)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _detect_poly_resolution(
    condition_id: str,
    session: requests.Session,
    yes_token_id: str = "",
) -> Optional[bool]:
    """
    Query the Polymarket CLOB for a single market by its condition ID.

    Returns ``True`` if YES won, ``False`` if NO won, ``None`` if the market
    has not resolved yet (or if the API call fails — we never raise).

    The CLOB ``/markets/{condition_id}`` endpoint is keyed natively by the 0x…
    condition_id (unlike Gamma's numeric-id path, which 422s on a condition_id —
    the bug that silently broke detection for every poly market). The response
    carries ``closed`` and a ``tokens`` list, each token with ``token_id``,
    ``outcome``, ``price`` and ``winner``.

    YES side: prefer the token whose ``token_id`` matches the market's stored
    ``yes_token_id`` (exact, order-independent); fall back to ``tokens[0]``, which
    is the YES side by Polymarket convention (verified token_id-matched). A token
    wins iff ``winner`` is true, or (fallback) its settled ``price`` is ~1.0.
    """
    if not condition_id:
        return None
    try:
        r = session.get(f"{CLOB_URL}/markets/{condition_id}", timeout=15)
        if r.status_code != 200:
            return None
        m = r.json()
    except Exception:
        return None

    if not bool(m.get("closed")):
        return None  # not settled yet

    tokens = m.get("tokens") or []
    if not tokens:
        return None

    # Find the YES token: match by stored yes_token_id, else fall back to index 0.
    yes_tok = None
    if yes_token_id:
        yes_tok = next((t for t in tokens
                        if str(t.get("token_id")) == str(yes_token_id)), None)
    if yes_tok is None:
        yes_tok = tokens[0]

    # winner flag is authoritative; price is the settled fallback (1.0 = won).
    w = yes_tok.get("winner")
    if isinstance(w, bool):
        return w
    try:
        p = float(yes_tok.get("price"))
    except (TypeError, ValueError):
        return None
    if p >= 0.99:
        return True
    if p <= 0.01:
        return False
    return None  # settled but ambiguous (dispute / mid price) — wait


def _detect_kalshi_resolution(venue_market_id: str) -> Optional[bool]:
    """
    Query Kalshi for a single market by its ticker.

    Returns ``True`` if YES, ``False`` if NO, ``None`` if not resolved or error.
    Kalshi 429s are handled at the call site via sleep throttling.
    """
    if not venue_market_id:
        return None
    try:
        from pipeline import rootpath  # noqa: F401 — puts repo root on sys.path
        from kalshi_client import KalshiClient

        client = KalshiClient()
        data = client.get_market(venue_market_id)
    except Exception:
        return None

    if not data:
        return None

    inner: dict = data.get("market") or {}
    status = str(inner.get("status") or "").lower()

    if status not in _KALSHI_SETTLED:
        return None  # still active or indeterminate

    result = str(inner.get("result") or "").lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None  # settled but result field absent/empty


def detect_resolution(
    opp: Opportunity,
    venue_market_id: str,
    session: requests.Session,
    yes_token_id: str = "",
) -> Optional[bool]:
    """
    Top-level dispatcher: check resolution for any venue.

    Returns ``True`` (YES won), ``False`` (NO won), or ``None`` (unresolved /
    unknown / manifold).  Never raises. ``yes_token_id`` (poly only) lets the
    detector match the YES outcome by token id instead of relying on order.
    """
    try:
        if opp.venue == "poly":
            return _detect_poly_resolution(venue_market_id, session, yes_token_id)
        if opp.venue == "kalshi":
            return _detect_kalshi_resolution(venue_market_id)
        # manifold: we don't trade there, skip resolution scoring
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# refresh loop
# ---------------------------------------------------------------------------

def refresh_resolutions(limit: int = 1000) -> Dict[str, int]:
    """
    Iterate over approved/executed Opportunities and update ``resolutions``.

    Only markets not yet in the table are queried.  API calls are throttled
    at ~0.3 s per market to stay polite (Kalshi especially rate-limits hard).

    Returns a dict with counts: checked, newly_resolved, already_cached, errors.
    """
    store = get_store()
    db_path = str(store.db_path)
    _init_resolutions_table(db_path)

    opps: List[Opportunity] = []
    for status in (OppStatus.APPROVED.value, OppStatus.EXECUTED.value):
        opps.extend(store.get_opportunities(status=status, limit=limit))

    # Deduplicate by market_id — many opps may share a market
    unique: Dict[str, object] = {}
    for opp in opps:
        if opp.market_id not in unique:
            unique[opp.market_id] = opp

    # Also resolve markets referenced by THESES (incl. killed theses that never became
    # opportunities) so the thesis Brier scorer has resolution data. A SimpleNamespace
    # stand-in is enough — detect_resolution() only reads .venue.
    try:
        with sqlite3.connect(db_path) as tc:
            tc.row_factory = sqlite3.Row
            for r in tc.execute("SELECT DISTINCT market_id, venue FROM theses"):
                mid = r["market_id"]
                if mid and mid not in unique:
                    unique[mid] = SimpleNamespace(market_id=mid, venue=r["venue"])
    except sqlite3.OperationalError:
        pass

    if not unique:
        return {"checked": 0, "newly_resolved": 0, "already_cached": 0, "errors": 0}

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    counts: Dict[str, int] = {
        "checked": 0,
        "newly_resolved": 0,
        "already_cached": 0,
        "errors": 0,
    }

    for market_id, opp in unique.items():
        if _fetch_cached_resolution(db_path, market_id) is not None:
            counts["already_cached"] += 1
            continue

        counts["checked"] += 1

        # Retrieve the Market record to get the raw venue_market_id
        market = store.get_market(market_id)
        if market is None:
            counts["errors"] += 1
            continue

        yes_token_id = str((market.extra or {}).get("yes_token_id") or "")
        resolved = detect_resolution(opp, market.venue_market_id, session, yes_token_id)

        if resolved is not None:
            _upsert_resolution(
                db_path,
                market_id,
                resolved,
                utcnow_iso(),
                opp.venue,
            )
            counts["newly_resolved"] += 1

        time.sleep(0.3)  # polite throttle

    return counts


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

@dataclass
class BetResult:
    """Scored outcome for a single resolved Opportunity."""

    opp: Opportunity
    resolved_yes: bool
    won: bool           # did our bet win?
    pnl: float          # realized paper P&L in USD
    model_brier: float  # (P_model(backed side) - outcome)^2
    price_brier: float  # (P_market(backed side) - outcome)^2  [baseline]


def score_opportunity(opp: Opportunity, resolved_yes: bool) -> BetResult:
    """
    Compute P&L and Brier scores for a single resolved opportunity.

    * Side YES: won when market resolved YES.
    * Side NO : won when market resolved NO.
    * Side BOTH / arb: treated as always winning (both legs are risk-locked);
      Brier is set to zero for these since the signal is about price gaps,
      not probability estimation.

    P&L formula for a win: profit = stake * (1/price - 1)  (bought shares at
    ``market_price``, each paying $1 at resolution).
    P&L for a loss: profit = -stake.
    """
    side = (opp.side or "YES").upper()
    stake = opp.recommended_stake or 0.0
    price = max(opp.market_price, 1e-6)

    if side == "BOTH":
        # Riskless arb pair — treat as win, Brier not meaningful
        won = True
        outcome = 1.0
        model_brier = 0.0
        price_brier = 0.0
    elif side == "NO":
        won = not resolved_yes
        outcome = 1.0 if won else 0.0
        # P(NO) = 1 - fair_prob  vs  resolved outcome (1 if NO-bet won)
        model_brier = ((1.0 - opp.fair_prob) - outcome) ** 2
        price_brier = ((1.0 - opp.market_price) - outcome) ** 2
    else:  # YES (default)
        won = resolved_yes
        outcome = 1.0 if won else 0.0
        model_brier = (opp.fair_prob - outcome) ** 2
        price_brier = (opp.market_price - outcome) ** 2

    if won:
        pnl = stake * (1.0 / price - 1.0)
    else:
        pnl = -stake

    return BetResult(
        opp=opp,
        resolved_yes=resolved_yes,
        won=won,
        pnl=pnl,
        model_brier=model_brier,
        price_brier=price_brier,
    )


def compute_scoreboard(limit: int = 1000) -> List[BetResult]:
    """
    Join approved/executed Opportunities with cached resolutions and score them.

    Returns only the subset of opportunities whose market has resolved.
    """
    store = get_store()
    db_path = str(store.db_path)
    _init_resolutions_table(db_path)

    opps: List[Opportunity] = []
    for status in (OppStatus.APPROVED.value, OppStatus.EXECUTED.value):
        opps.extend(store.get_opportunities(status=status, limit=limit))

    resolutions = _all_resolutions(db_path)

    results: List[BetResult] = []
    for opp in opps:
        if opp.market_id in resolutions:
            results.append(score_opportunity(opp, resolutions[opp.market_id]))

    return results


# ---------------------------------------------------------------------------
# scoreboard printer
# ---------------------------------------------------------------------------

def _group_row(label: str, group: List[BetResult]) -> str:
    n = len(group)
    wins = sum(1 for r in group if r.won)
    win_pct = 100.0 * wins / n if n else 0.0
    total_pnl = sum(r.pnl for r in group)
    total_stake = sum(r.opp.recommended_stake or 0.0 for r in group)
    roi = 100.0 * total_pnl / total_stake if total_stake > 0 else 0.0
    avg_mb = sum(r.model_brier for r in group) / n if n else 0.0
    avg_pb = sum(r.price_brier for r in group) / n if n else 0.0
    return (
        f"{label:<15} {n:>5}   {wins:>4}   {win_pct:>5.1f}%"
        f"  {total_pnl:>+9.2f}  {roi:>7.1f}%"
        f"  {avg_mb:>9.5f}  {avg_pb:>9.5f}"
    )


def print_scoreboard(results: List[BetResult]) -> None:
    """Print the calibration scoreboard to stdout using plain ASCII."""
    if not results:
        print("  No resolved opportunities to score yet.")
        return

    by_source: Dict[str, List[BetResult]] = defaultdict(list)
    for r in results:
        by_source[r.opp.edge_source or "unknown"].append(r)

    hdr = (
        f"{'source':<15} {'n':>5}   {'wins':>4}   {'win%':>6}"
        f"  {'paper_pnl':>9}  {'roi%':>7}"
        f"  {'brier_mdl':>9}  {'brier_mkt':>9}"
    )
    sep = "-" * len(hdr)
    print(sep)
    print(hdr)
    print(sep)

    for source in sorted(by_source):
        print(_group_row(source, by_source[source]))

    if len(by_source) > 1:
        print(sep)
        print(_group_row("TOTAL", results))

    print(sep)


# ---------------------------------------------------------------------------
# thesis scoring — is the thesis engine's probability estimate better than price?
# ---------------------------------------------------------------------------

def score_theses(db_path: str) -> List[dict]:
    """Score every thesis whose market has resolved.

    The core question this answers: is the thesis engine's P(YES) estimate **better
    calibrated than the market price** (lower Brier)? That is the edge test — if the
    estimate doesn't beat the price, there's no information edge and nothing should go
    live. We score BOTH the generator's fair_prob and the critic-adjusted estimate, and
    separately track how SURVIVED theses (the ones we'd actually bet) would have done.
    """
    res = _all_resolutions(db_path)
    out: List[dict] = []
    try:
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT market_id, side, fair_prob, status, critic_verdict, detail "
                "FROM theses"
            ).fetchall()
    except sqlite3.OperationalError:
        return out

    for r in rows:
        mid = r["market_id"]
        if mid not in res:
            continue
        outcome = 1.0 if res[mid] else 0.0
        d = json.loads(r["detail"]) if r["detail"] else {}
        gen_yes = r["fair_prob"]
        adj_yes = d.get("adjusted_fair_prob")
        price = d.get("market_price_at_thesis")
        side = (r["side"] or "YES").upper()
        rec: dict = {
            "market_id": mid, "side": side, "status": r["status"],
            "verdict": r["critic_verdict"], "resolved_yes": bool(res[mid]),
            "gen_brier": (gen_yes - outcome) ** 2 if gen_yes is not None else None,
            "adj_brier": (adj_yes - outcome) ** 2 if isinstance(adj_yes, (int, float)) else None,
            "mkt_brier": (price - outcome) ** 2 if isinstance(price, (int, float)) else None,
        }
        # how a bet on this thesis would have done (only meaningful if we have the price)
        if isinstance(price, (int, float)):
            side_price = price if side == "YES" else round(1.0 - price, 6)
            won = (outcome == 1.0) if side == "YES" else (outcome == 0.0)
            rec["bet_won"] = won
            rec["bet_pnl_per_$1"] = (1.0 / max(side_price, 1e-6) - 1.0) if won else -1.0
        out.append(rec)
    return out


def _avg(vals: List[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if isinstance(v, (int, float))]
    return sum(xs) / len(xs) if xs else None


def print_thesis_scoreboard(records: List[dict]) -> None:
    if not records:
        print("  No resolved theses to score yet (constraint markets are mostly long-dated).")
        return
    n = len(records)
    gen_b = _avg([r.get("gen_brier") for r in records])
    adj_b = _avg([r.get("adj_brier") for r in records])
    mkt_b = _avg([r.get("mkt_brier") for r in records])
    print(f"  resolved theses: {n}")
    print(f"  generator  Brier : {gen_b:.5f}" if gen_b is not None else "  generator  Brier : n/a")
    print(f"  critic-adj Brier : {adj_b:.5f}" if adj_b is not None else "  critic-adj Brier : n/a")
    print(f"  market     Brier : {mkt_b:.5f}" if mkt_b is not None else "  market     Brier : n/a")
    if gen_b is not None and mkt_b is not None:
        verdict = "BEATS market (edge!)" if gen_b < mkt_b else "worse than market (no edge yet)"
        print(f"  --> generator estimate {verdict}")
    survived = [r for r in records if r["status"] == "survived" and "bet_won" in r]
    if survived:
        wins = sum(1 for r in survived if r["bet_won"])
        pnl = sum(r["bet_pnl_per_$1"] for r in survived)
        print(f"  survived-thesis bets: {len(survived)} | wins {wins} "
              f"({100*wins/len(survived):.0f}%) | P&L ${pnl:+.2f} per $1 staked")
    else:
        print("  survived-thesis bets: 0 resolved yet (none have cleared the critic + resolved)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    # Windows consoles default to cp1252; market questions contain accented
    # chars.  Reconfigure to UTF-8 so they print (or get replaced) safely.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip live venue lookups; score from cached resolutions only",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max opportunities to consider (default 1000)",
    )
    args = ap.parse_args(argv)

    store = get_store()
    print(f"pipeline/learn  DB: {store.db_path}")

    if not args.no_refresh:
        print("\nRefreshing resolutions from venues ...")
        counts = refresh_resolutions(limit=args.limit)
        print(
            f"  markets checked: {counts['checked']}  |  "
            f"newly resolved: {counts['newly_resolved']}  |  "
            f"already cached: {counts['already_cached']}  |  "
            f"errors: {counts['errors']}"
        )
    else:
        print("\n  (--no-refresh: skipping live venue lookups)")

    print("\nScoring resolved paper bets ...")
    results = compute_scoreboard(limit=args.limit)
    n_resolved = len(results)
    print(f"  resolved bets found: {n_resolved}\n")

    print("=== Calibration Scoreboard (paper P&L) ===")
    print(
        "  brier_mdl = model Brier score (lower = better calibrated)\n"
        "  brier_mkt = market-price Brier baseline (lower = market was better)\n"
    )
    print_scoreboard(results)

    print("\n=== Thesis Scoreboard (information edge: estimate vs market price) ===")
    print(
        "  The edge test: is the thesis engine's P(YES) better calibrated than the price?\n"
        "  generator/critic Brier < market Brier => real edge. Must beat it before going live.\n"
    )
    print_thesis_scoreboard(score_theses(str(store.db_path)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
