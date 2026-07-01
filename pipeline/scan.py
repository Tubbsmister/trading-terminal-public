"""
Scan entry point — the Phase 1 engine loop (paper-trade).

    python -m pipeline.scan                     # scan DB, size, guardrail, report
    python -m pipeline.scan --bankroll 500
    python -m pipeline.scan --min-edge 0.05
    python -m pipeline.scan --no-persist        # don't write opportunities to DB

Pipeline:  load active markets  ->  estimators (arb + thesis/LLM)  ->  quarter-Kelly
sizing  ->  guardrails  ->  persist opportunities  ->  print what we WOULD bet.

This is paper-trade only: it never places an order. Phase 2 adds an ``--execute``
path behind the same guardrails. Run ``python -m pipeline.collect`` first to
populate / refresh the market DB.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import decide, edge_calibration
from .edges import arb
from .estimators import llm
from .guardrails import GuardrailConfig, PortfolioState, apply, kill_switch_engaged
from .models import Opportunity, OppStatus
from .sizing import KellySizer
from .store import Store, get_store


def _size_all(opps: List[Opportunity], sizer: KellySizer) -> None:
    """Fill recommended_stake / kelly_fraction on each opportunity in place."""
    for o in opps:
        r = sizer.size(o.market_price, o.fair_prob)
        o.recommended_stake = r.stake
        o.kelly_fraction = r.fraction
        o.detail = {**(o.detail or {}), "size": {
            "full_kelly": r.full_fraction, "fraction": r.fraction,
            "contracts": r.contracts, "ev_per_dollar": r.ev_per_dollar,
            "capped_by": r.capped_by,
        }}


def run_scan(
    store: Store,
    *,
    bankroll: float,
    cfg: GuardrailConfig,
    persist: bool = True,
    enable_llm: bool = True,
    state: Optional[PortfolioState] = None,
) -> dict:
    markets = store.get_markets(active_only=True)
    sizer = KellySizer(bankroll=bankroll, multiplier=0.25,
                       max_fraction=cfg.max_position_fraction, min_stake=cfg.min_stake)

    # ---- estimators -> candidate opportunities ----
    auto: List[Opportunity] = []
    auto += arb.within_market_arb(markets)
    # Phase 3: information edge. No-op without ANTHROPIC_API_KEY.
    if enable_llm:
        auto += llm.LLMEstimator(store).scan()

    xvenue = arb.cross_venue_candidates(markets)

    # ---- math floor: shrink overconfident estimates toward price + deflate the
    # best-of-N selection bias, BEFORE sizing so the conservative number flows into
    # stake, the min-edge gate, and the ranking alike. Arb is exempt (mechanical).
    edge_calibration.calibrate(auto)

    # ---- size, sort best-first, guardrail ----
    _size_all(auto, sizer)
    auto.sort(key=lambda o: o.edge, reverse=True)

    # Caller may pass a shared state so scan + thesis (in one cycle) enforce ONE
    # combined exposure cap instead of each getting a fresh 50% budget.
    if state is None:
        state = PortfolioState(bankroll=bankroll)
    halt = kill_switch_engaged(state, cfg)
    if halt:
        print(f"!! KILL-SWITCH: {halt} — no bets approved this run.\n")
        approved, rejected = [], auto
        for o in auto:
            o.status = OppStatus.REJECTED.value
            o.guardrail_notes = f"HALT: {halt}"
    else:
        approved, rejected = apply(auto, state, cfg)
        # Phase 3: Opus decision gate over guardrail-approved bets (downsize/veto).
        # No-op without a key. review() mutates status; vetoed ones become REJECTED.
        if approved:
            decide.review(approved)
            vetoed = [o for o in approved if o.status != OppStatus.APPROVED.value]
            approved = [o for o in approved if o.status == OppStatus.APPROVED.value]
            rejected += vetoed
            # exposure freed by vetoes/downsizes is fine to leave conservative

    if persist:
        for o in approved + rejected + xvenue:
            store.add_opportunity(o)

    return {
        "markets": len(markets),
        "approved": approved,
        "rejected": rejected,
        "review": xvenue,
        "exposure": sum(o.recommended_stake or 0 for o in approved),
        "bankroll": bankroll,
        "state": state,   # shared portfolio state, for the caller to reuse (cycle-wide cap)
    }


def _print_report(res: dict) -> None:
    approved, rejected, review = res["approved"], res["rejected"], res["review"]
    bankroll = res["bankroll"]
    print(f"\nScanned {res['markets']} active markets | bankroll ${bankroll:,.0f} "
          f"| quarter-Kelly\n")

    print(f"== PAPER BETS (would place {len(approved)}) ==")
    if not approved:
        print("  (none cleared guardrails this run)")
    else:
        print(f"  {'edge':>6} {'EV/$':>6} {'stake':>8} {'price':>6} {'fair':>6} "
              f"{'src':>11}  market")
        for o in approved:
            print(f"  {o.edge:6.3f} {(o.detail.get('size',{}).get('ev_per_dollar',0)):6.3f} "
                  f"${o.recommended_stake:7.2f} {o.market_price:6.3f} {o.fair_prob:6.3f} "
                  f"{o.side:>3}/{o.edge_source:<7} {o.question[:52]}")
        total = sum(o.recommended_stake or 0 for o in approved)
        print(f"\n  total staked ${total:,.2f} "
              f"({100*res['exposure']/bankroll:.1f}% of bankroll)")

    # rejection reasons, summarized
    from collections import Counter
    reasons = Counter()
    for o in rejected:
        key = (o.guardrail_notes or "").split(" <")[0].split(" >")[0]
        # bucket by leading phrase
        reasons[(o.guardrail_notes or "?").split(" ")[0]] += 1
    if rejected:
        print(f"\n-- rejected {len(rejected)} --  " +
              ", ".join(f"{k}:{v}" for k, v in reasons.most_common(6)))

    if review:
        print(f"\n== CROSS-VENUE REVIEW CANDIDATES ({len(review)}) -- verify rules, do NOT auto-bet ==")
        for o in review[:10]:
            d = o.detail
            print(f"  gap {o.edge:.3f}  sim {o.confidence:.2f}")
            print(f"     poly  {d['poly']['yes']:.3f}  {d['poly']['q'][:60]}")
            print(f"     kalshi {d['kalshi']['yes']:.3f}  {d['kalshi']['q'][:60]}")


def main(argv=None) -> int:
    # Windows consoles default to cp1252; market titles carry accented chars.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--bankroll", type=float,
                    default=float(os.environ.get("POLY_BANKROLL", 1000.0)))
    ap.add_argument("--min-edge", type=float, default=None,
                    help="override directional min-edge guardrail")
    ap.add_argument("--max-position", type=float, default=None,
                    help="override per-market max fraction of bankroll")
    ap.add_argument("--no-persist", action="store_true",
                    help="don't write opportunities to the DB")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the Phase 3 LLM estimator (saves API spend)")
    ap.add_argument("--execute", action="store_true",
                    help="(Phase 2) place real orders — not yet implemented")
    args = ap.parse_args(argv)

    if args.execute:
        print("--execute is Phase 2 (guardrailed live execution) and not wired yet. "
              "Phase 1 is paper-trade only.")
        return 2

    cfg = GuardrailConfig()
    if args.min_edge is not None:
        cfg.min_edge = args.min_edge
    if args.max_position is not None:
        cfg.max_position_fraction = args.max_position

    store = get_store()
    res = run_scan(store, bankroll=args.bankroll, cfg=cfg, persist=not args.no_persist,
                   enable_llm=not args.no_llm)
    _print_report(res)
    if not args.no_persist:
        print(f"\nPersisted {len(res['approved'])+len(res['rejected'])+len(res['review'])} "
              f"opportunities to {store.db_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
