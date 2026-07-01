"""
Fast position-watcher — catch a breaking move in minutes, not the next cycle.

The Fable-5 post-mortem: the full 30-min cycle didn't run for 19 hours (the PC
slept), the Anthropic/Commerce announcement broke in that gap, and the NO position
collapsed 0.40 -> 0.12 before the monitor ever re-ran. The monitor's *reasoning*
was fine; it just wasn't looking often enough.

This module is the cheap, high-frequency watcher that fills that gap. It does the
opposite of the heavy cycle: NO collect / scan / thesis, NO LLM by default — just
poll the live price of each OPEN position and fire a deterministic trip-wire when
our side moves against us. The market's own price IS the fastest signal: the crowd
prices a breaking announcement long before it reaches an RSS feed, so a sharp
adverse move is reason enough to escalate immediately.

On a trip it ESCALATES to the full LLM monitor review for just that position
(``monitor.review_position``) — the same HOLD/TRIM/EXIT logic the 30-min cycle
runs, but now, on demand, instead of hours later. It is **alert-only**: it never
places or cancels an order (that stays a human/approved action). It writes the
alert to an ``alerts`` log and, if ``ALERT_WEBHOOK_URL`` is set, posts it there.

Run it on a tight cadence, independent of the main scheduler::

    python -m pipeline.fast_watch                 # one pass
    python -m pipeline.fast_watch --loop 120      # every 2 min until Ctrl-C
    python -m pipeline.fast_watch --abs 0.06 --rel 0.12
    python -m pipeline.fast_watch --force         # escalate every position now
    python -m pipeline.fast_watch --no-escalate   # price trip-wire only, no LLM
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

from . import monitor
from .store import Store, get_store

REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTS_LOG = REPO_ROOT / "alerts.log"

# Defaults: fire if our side drops >= 8 cents OR >= 15% relative since the baseline.
DEFAULT_ABS = float(os.environ.get("FASTWATCH_ABS", "0.08"))
DEFAULT_REL = float(os.environ.get("FASTWATCH_REL", "0.15"))


# ───────────────────────── pure trip-wire logic (unit-tested) ─────────────────────────

# A relative-only trip still needs at least this much ABSOLUTE movement. Near
# resolution (price ~0 or ~1) a 1-cent dust move is a huge % but meaningless — this
# floor stops the watcher spamming EXIT alerts as a settled position trickles to 0.
MIN_REL_ABS = float(os.environ.get("FASTWATCH_MIN_REL_ABS", "0.02"))


def price_moved_against(cur_price: Optional[float], baseline_price: Optional[float],
                        *, abs_thresh: float = DEFAULT_ABS,
                        rel_thresh: float = DEFAULT_REL,
                        min_rel_abs: float = MIN_REL_ABS) -> Tuple[bool, str]:
    """Did the price of the side we HOLD fall enough to warrant an immediate re-check?

    We hold ``side``; its price falling = our position losing value. Trip when the
    drop from ``baseline_price`` to ``cur_price`` clears the absolute (cents)
    threshold, OR the relative (%) threshold *with* at least ``min_rel_abs`` of
    absolute movement (so tail-price dust doesn't fire). A rise never trips.
    Returns (tripped, human-reason). Missing prices never trip (can't judge).
    """
    if cur_price is None or baseline_price is None or baseline_price <= 0:
        return False, "no price to compare"
    drop = baseline_price - cur_price          # positive = adverse
    if drop <= 0:
        return False, f"favorable/flat ({baseline_price:.3f}->{cur_price:.3f})"
    rel = drop / baseline_price
    move = f"{drop:.3f} ({rel*100:.0f}%) {baseline_price:.3f}->{cur_price:.3f}"
    if drop >= abs_thresh:
        return True, f"our side fell {move}"
    if rel >= rel_thresh and drop >= min_rel_abs:
        return True, f"our side fell {move}"
    if rel >= rel_thresh:
        return False, f"move {move} is {rel*100:.0f}% but < {min_rel_abs:.3f} abs (tail dust)"
    return False, (f"move {move} below {abs_thresh:.3f}/{rel_thresh*100:.0f}% threshold")


def _baseline_price(store: Store, position) -> Tuple[Optional[float], str]:
    """The price to compare against: the most recent monitor review's price if we
    have one (catches a fresh spike since we last looked), else the entry price
    (catches a slow bleed we've never reviewed)."""
    revs = store.get_position_reviews(position.id, limit=1)
    if revs and revs[0].get("current_price") is not None:
        return float(revs[0]["current_price"]), "last review"
    return position.avg_price, "entry"


# ───────────────────────── alert sink ─────────────────────────

def emit_alert(text: str) -> None:
    """Print, append to alerts.log, and POST to ALERT_WEBHOOK_URL if configured."""
    stamp = monitor_now()
    line = f"{stamp}  {text}"
    print(f"  !! ALERT  {text}")
    try:
        with open(ALERTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    hook = os.environ.get("ALERT_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"text": line}, timeout=8)
        except requests.RequestException:
            pass


def monitor_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────── one pass ─────────────────────────

def check_once(store: Optional[Store] = None, *, abs_thresh: float = DEFAULT_ABS,
               rel_thresh: float = DEFAULT_REL, escalate: bool = True,
               do_web: bool = True, force: bool = False, verbose: bool = True) -> list:
    """One fast sweep of all open positions. Returns the list of tripped alerts."""
    store = store or get_store()
    positions = store.get_open_positions()
    if verbose:
        print(f"fast_watch: {len(positions)} open position(s) "
              f"| trip if drop >= {abs_thresh:.3f} or {rel_thresh*100:.0f}%"
              + (" | FORCE" if force else ""))
    if not positions:
        return []

    live = monitor.live_positions()          # ground-truth on-chain, fetched once
    alerts = []
    for p in positions:
        # cheap: current price of the side we hold (no LLM)
        side_price, market = monitor._current_side_price(p, store)
        # prefer the ground-truth live price if we matched the position on-chain
        rec = monitor._match_live(p, live)
        if rec and rec.get("cur_price"):
            side_price = rec["cur_price"]
        baseline, base_src = _baseline_price(store, p)
        tripped, reason = price_moved_against(side_price, baseline,
                                              abs_thresh=abs_thresh, rel_thresh=rel_thresh)
        q = (p.extra or {}).get("question", p.market_id)
        if verbose:
            state = "TRIP" if tripped else "ok"
            print(f"  [{state}] {p.side} {q[:48]}  price={side_price} "
                  f"base={baseline}({base_src})  {reason}")

        if not (tripped or force):
            continue

        # ESCALATE: run the full LLM monitor review NOW, for just this position.
        verdict = None
        if escalate:
            try:
                verdict = monitor.review_position(p, store, do_web=do_web, live=live)
            except Exception as exc:  # never let one position kill the sweep
                print(f"    (escalation review failed: {exc})")

        rec_txt = (verdict or {}).get("recommendation", "REVIEW")
        alert = {
            "position_id": p.id, "side": p.side, "question": q,
            "price": side_price, "baseline": baseline, "reason": reason,
            "recommendation": rec_txt,
            "llm_reason": (verdict or {}).get("reason"),
            "threat": (verdict or {}).get("threat"),
        }
        alerts.append(alert)
        msg = (f"{p.side} '{q[:60]}' price-move: {reason}"
               f" -> monitor says {rec_txt}"
               + (f" ({alert['llm_reason'][:100]})" if alert.get("llm_reason") else ""))
        emit_alert(msg)
    return alerts


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="Fast position-watcher (price trip-wire)")
    ap.add_argument("--abs", type=float, default=DEFAULT_ABS,
                    help=f"absolute adverse move (cents) to trip (default {DEFAULT_ABS})")
    ap.add_argument("--rel", type=float, default=DEFAULT_REL,
                    help=f"relative adverse move (fraction) to trip (default {DEFAULT_REL})")
    ap.add_argument("--no-escalate", action="store_true",
                    help="price trip-wire only; do NOT run the LLM monitor review")
    ap.add_argument("--no-web", action="store_true",
                    help="on escalation, skip web search (signals-only review)")
    ap.add_argument("--force", action="store_true",
                    help="escalate EVERY open position now, regardless of price move")
    ap.add_argument("--loop", type=float, default=None, metavar="SECONDS",
                    help="repeat forever every SECONDS (e.g. 120); default: one pass")
    args = ap.parse_args(argv)

    store = get_store()

    def one():
        return check_once(store, abs_thresh=args.abs, rel_thresh=args.rel,
                          escalate=not args.no_escalate, do_web=not args.no_web,
                          force=args.force)

    if not args.loop:
        one()
        return 0

    print(f"fast_watch loop: every {args.loop:g}s (Ctrl-C to stop)")
    try:
        while True:
            one()
            time.sleep(max(15.0, args.loop))
    except KeyboardInterrupt:
        print("\nfast_watch stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
