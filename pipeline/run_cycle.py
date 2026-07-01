"""
Full pipeline cycle runner — one pass of: collect -> news -> relevance -> scan -> thesis.

Run from repo root:

    python -m pipeline.run_cycle
    python -m pipeline.run_cycle --no-llm --no-thesis --market-limit 30
    python -m pipeline.run_cycle --no-web

The scheduler (scripts/schedule_pipeline.ps1) repeats this at a fixed interval.
This script runs exactly ONE cycle per invocation; there is no internal loop.

Kill-switch: if a file named STOP exists in the repo root, steps 4 (scan) and
5 (thesis) are skipped entirely and the cycle logs HALTED.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Repo root + STOP file
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
STOP = REPO_ROOT / "STOP"
LOG_PATH = REPO_ROOT / "pipeline_cycle.log"

BANKROLL = float(
    os.environ.get("PIPELINE_BANKROLL", os.environ.get("POLY_BANKROLL", "1000"))
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed(t0: datetime) -> float:
    return (_utcnow() - t0).total_seconds()


def _log(msg: str) -> None:
    """Write msg to stdout and append it to pipeline_cycle.log."""
    print(msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError as e:
        print(f"  [warn] could not write to {LOG_PATH}: {e}")


def _step_log(step: str, detail: str, elapsed: float) -> None:
    _log(f"  [{step}] {detail}  ({elapsed:.1f}s)")


# ---------------------------------------------------------------------------
# Step runners — each returns a short summary string and raises on hard error
# ---------------------------------------------------------------------------

def _step_collect(market_limit: int) -> str:
    from pipeline import collect
    rc = collect.main(["--limit", str(market_limit)])
    if rc and rc != 0:
        raise RuntimeError(f"collect.main returned {rc}")
    return f"limit={market_limit}"


def _step_news() -> str:
    from pipeline.signals import news
    rc = news.main(["--limit-per-feed", "25"])
    if rc and rc != 0:
        raise RuntimeError(f"news.main returned {rc}")
    return "limit-per-feed=25"


def _step_x() -> str:
    """Best-effort X watchlist ingestion (Tier-3 early-warning). Keyless routes are
    flaky by design, so this never hard-fails: it returns a count and swallows
    route errors inside the collector."""
    from pipeline.signals.x_collector import fetch_x_watchlist
    from pipeline.store import get_store
    signals = fetch_x_watchlist()
    n_new = get_store().add_signals(signals)
    return f"posts={len(signals)} new={n_new}"


def _step_relevance() -> str:
    from pipeline.signals import relevance
    # relevance.main() takes no argv parameter; call run() directly instead
    n_links = relevance.run(hours=72)
    return f"hours=72 links={n_links}"


def _step_scan(bankroll: float, no_llm: bool, state) -> dict:
    from pipeline.scan import run_scan
    from pipeline.guardrails import GuardrailConfig
    from pipeline.store import get_store

    store = get_store()
    cfg = GuardrailConfig()
    res = run_scan(
        store,
        bankroll=bankroll,
        cfg=cfg,
        persist=True,
        enable_llm=not no_llm,
        state=state,   # shared across scan + thesis so the exposure cap is cycle-wide
    )
    if res is None:
        raise RuntimeError("run_scan returned None")
    return res


def _step_thesis(bankroll: float, thesis_markets: int, no_web: bool, state) -> dict:
    from pipeline import thesis as thesis_mod
    from pipeline.sizing import KellySizer
    from pipeline.guardrails import apply, GuardrailConfig
    from pipeline.store import get_store

    store = get_store()
    thesis_opps = thesis_mod.run_engine(
        store,
        max_markets=thesis_markets,
        do_web=not no_web,
    )
    if thesis_opps is None:
        thesis_opps = []

    # Math floor: shrink the (LLM-estimated) thesis fair-prob toward price by a
    # deterministic per-source trust factor before sizing. This is the hallucination
    # locus — a thesis can't manufacture conviction the floor doesn't grant.
    from pipeline import edge_calibration
    edge_calibration.calibrate(thesis_opps)

    # Size + guardrail + persist the resulting Opportunity rows
    sizer = KellySizer(
        bankroll=bankroll,
        multiplier=0.25,
        max_fraction=0.05,
        min_stake=1.0,
    )
    for o in thesis_opps:
        r = sizer.size(o.market_price, o.fair_prob)
        o.recommended_stake = r.stake
        o.kelly_fraction = r.fraction

    cfg = GuardrailConfig()
    # Reuse the SAME state the scan step used: combined scan+thesis exposure must
    # respect ONE cycle-wide cap, not a fresh 50% budget per step.
    approved, rejected = apply(thesis_opps, state, cfg)

    # Persist approved + rejected thesis opportunities
    for o in approved + rejected:
        try:
            store.add_opportunity(o)
        except Exception as e:
            print(f"  [thesis] warn: could not persist opportunity: {e}")

    return {
        "total": len(thesis_opps),
        "approved": len(approved),
        "rejected": len(rejected),
    }


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def _run_once(args: argparse.Namespace) -> int:
    cycle_start = _utcnow()
    errors = 0
    markets_ok = 0
    signals_ok = 0
    scan_bets = 0
    thesis_bets = 0

    _log(f"\n{'='*60}")
    _log(f"cycle start  {_iso(cycle_start)}  "
         f"bankroll=${BANKROLL:,.0f}  market-limit={args.market_limit}")

    # ---- Step 1: collect markets ----------------------------------------
    t0 = _utcnow()
    try:
        _step_collect(args.market_limit)
        markets_ok = args.market_limit
        _step_log("collect", f"markets requested={args.market_limit}", _elapsed(t0))
    except Exception as exc:
        errors += 1
        _step_log("collect", f"FAILED: {exc!s:.200}", _elapsed(t0))

    # ---- Step 2: ingest news signals ------------------------------------
    t0 = _utcnow()
    try:
        _step_news()
        signals_ok = 1
        _step_log("news", "signals ingested", _elapsed(t0))
    except Exception as exc:
        errors += 1
        _step_log("news", f"FAILED: {exc!s:.200}", _elapsed(t0))

    # ---- Step 2b: ingest X watchlist (Tier-3 early-warning) --------------
    if not args.no_x:
        t0 = _utcnow()
        try:
            detail = _step_x()
            _step_log("x", detail, _elapsed(t0))
        except Exception as exc:
            errors += 1
            _step_log("x", f"FAILED: {exc!s:.200}", _elapsed(t0))

    # ---- Step 3: match relevance ----------------------------------------
    t0 = _utcnow()
    try:
        _step_relevance()
        _step_log("relevance", "links updated", _elapsed(t0))
    except Exception as exc:
        errors += 1
        _step_log("relevance", f"FAILED: {exc!s:.200}", _elapsed(t0))

    # ---- Kill-switch: STOP gates the BETTING steps (scan/thesis) only.
    # Monitoring open trades is read-only and ALWAYS runs — you want exit alerts
    # on positions you already hold even when new betting is halted.
    if STOP.exists():
        _log("HALTED: STOP file present — skipping scan and thesis (monitor still runs)")
    else:
        # One portfolio state shared across scan + thesis so the total-exposure cap
        # is enforced cycle-wide (not a fresh 50% budget for each step).
        from pipeline.guardrails import PortfolioState
        cycle_state = PortfolioState(bankroll=BANKROLL)

        # ---- Step 4: scan (Phase-1 + LLM estimator) ---------------------
        t0 = _utcnow()
        try:
            res = _step_scan(BANKROLL, args.no_llm, cycle_state)
            scan_bets = len(res.get("approved") or [])
            _step_log("scan", f"markets={res.get('markets', '?')} approved={scan_bets} "
                      f"rejected={len(res.get('rejected') or [])}", _elapsed(t0))
        except Exception as exc:
            errors += 1
            _step_log("scan", f"FAILED: {exc!s:.200}", _elapsed(t0))

        # ---- Step 5: thesis engine --------------------------------------
        if not args.no_thesis:
            t0 = _utcnow()
            try:
                tr = _step_thesis(BANKROLL, args.thesis_markets, args.no_web, cycle_state)
                thesis_bets = tr.get("approved", 0)
                _step_log("thesis", f"total={tr.get('total', 0)} approved={thesis_bets} "
                          f"rejected={tr.get('rejected', 0)}", _elapsed(t0))
            except Exception as exc:
                errors += 1
                _step_log("thesis", f"FAILED: {exc!s:.200}", _elapsed(t0))
        else:
            _log("  [thesis] skipped (--no-thesis)")

        # ---- Step 5b: thesis entry tracker (wait for price to come to the thesis) ----
        if not args.no_entry:
            t0 = _utcnow()
            try:
                from pipeline.entry_tracker import sync_watches, run_watches
                from pipeline.store import get_store
                store = get_store()
                n_new = sync_watches(store)
                fired = run_watches(store, verbose=False)
                _step_log("entry", f"watches+{n_new} fired={len(fired)}", _elapsed(t0))
            except Exception as exc:
                errors += 1
                _step_log("entry", f"FAILED: {exc!s:.200}", _elapsed(t0))

    # ---- Step 6: monitor active trades (always; read-only HOLD/TRIM/EXIT) ----
    monitor_exits = 0
    if not args.no_monitor:
        t0 = _utcnow()
        try:
            from pipeline.monitor import run_monitor
            from pipeline.store import get_store
            revs = run_monitor(get_store(), do_web=not args.no_web, verbose=True)
            monitor_exits = sum(1 for r in revs if r.get("recommendation") in ("EXIT", "TRIM"))
            _step_log("monitor", f"positions={len(revs)} "
                      f"exit/trim_alerts={monitor_exits}", _elapsed(t0))
        except Exception as exc:
            errors += 1
            _step_log("monitor", f"FAILED: {exc!s:.200}", _elapsed(t0))

    _write_summary(cycle_start, markets_ok, signals_ok, scan_bets, thesis_bets,
                   errors, monitor_exits)
    return 0 if errors == 0 else 1


def _write_summary(
    cycle_start: datetime,
    markets_ok: int,
    signals_ok: int,
    scan_bets: int,
    thesis_bets: int,
    errors: int,
    monitor_exits: int = 0,
) -> None:
    dur = _elapsed(cycle_start)
    tag = "PARTIAL" if errors else "done"
    summary = (
        f"{_iso(cycle_start)} cycle {tag} | "
        f"markets={markets_ok}ok signals=+{signals_ok} "
        f"scan_bets={scan_bets} thesis_bets={thesis_bets} "
        f"pos_alerts={monitor_exits} "
        f"errors={errors} [dur {dur:.1f}s]"
    )
    _log(summary)
    _log(f"{'='*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    # Windows console encoding fix
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--market-limit", type=int, default=300, metavar="N",
        help="max markets to collect per venue (default 300)",
    )
    ap.add_argument(
        "--thesis-markets", type=int, default=12, metavar="N",
        help="max markets to run through the thesis engine (default 12)",
    )
    ap.add_argument(
        "--no-llm", action="store_true",
        help="skip the LLM estimator in the scan step",
    )
    ap.add_argument(
        "--no-web", action="store_true",
        help="skip web search in the thesis engine",
    )
    ap.add_argument(
        "--no-thesis", action="store_true",
        help="skip the thesis engine entirely",
    )
    ap.add_argument(
        "--no-monitor", action="store_true",
        help="skip the active-trade monitor step",
    )
    ap.add_argument(
        "--no-entry", action="store_true",
        help="skip the thesis entry tracker (price-triggered entries)",
    )
    ap.add_argument(
        "--no-x", action="store_true",
        help="skip the X watchlist ingestion step",
    )
    ap.add_argument(
        "--once", action="store_true",
        help="(no-op; the scheduler repeats this; one cycle is the default)",
    )
    args = ap.parse_args(argv)

    return _run_once(args)


if __name__ == "__main__":
    sys.exit(main())
