"""
Premium thesis pass — frontier reasoning on the top constraint markets.

The 24/7 loop runs the thesis engine on the FREE local model (Qwen). That model
grounds facts well now but builds gappy causal chains (the #1 critic kill). This pass
re-runs generation + critique on the few best-triaged markets using the **Claude Code
subscription** (``claude -p``): Sonnet as the generator, Opus as the critic — much
stronger at gap-free causal chains and reading exact resolution criteria, with NO API
credits spent.

Deliberate + infrequent by design (subscription rate limits): a handful of markets,
run on demand or a slow cron — not the 30-minute loop.

    python -m pipeline.premium_pass                 # top 5 markets, web on
    python -m pipeline.premium_pass --max-markets 3
    python -m pipeline.premium_pass --no-web

Requires the ``claude`` CLI on PATH (the Claude Code subscription). Falls back with a
clear message if it's missing.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from . import llm_backend
from .store import get_store
from . import thesis as thesis_mod


def recently_premium_markets(store, days: float) -> set:
    """market_ids that already got a PREMIUM (cli/api backend) thesis within ``days`` — so a
    repeated/scheduled premium run covers NEW markets instead of re-grinding the same top-K.
    Premium theses are tagged in generated_by as 'cli:...' or 'api:...' (local is 'local:...')."""
    if days <= 0:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out: set = set()
    conn = sqlite3.connect(str(store.db_path))
    try:
        for mid, created in conn.execute(
            "SELECT market_id, created_at FROM theses "
            "WHERE generated_by LIKE 'cli:%' OR generated_by LIKE 'api:%'"
        ):
            if created and str(created) >= cutoff:
                out.add(mid)
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()   # explicit close so temp DBs (tests) aren't left locked on Windows
    return out


def run_premium(max_markets: int = 5, do_web: bool = True, reeval_days: float = 3.0) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    if not shutil.which("claude"):
        print("premium pass needs the `claude` CLI (Claude Code subscription) on PATH.")
        print("Install Claude Code, or set PIPELINE_THESIS_BACKEND=api with an ANTHROPIC_API_KEY.")
        return 2

    store = get_store()
    print(f"=== PREMIUM thesis pass ===  generator={thesis_mod.GEN_MODEL} (sonnet) | "
          f"critic={thesis_mod.CRITIC_MODEL} (opus)  via Claude Code subscription")
    exclude = recently_premium_markets(store, reeval_days)
    print(f"  triaging top {max_markets} constraint markets (skip {len(exclude)} premium-"
          f"evaluated in last {reeval_days:g}d), web={'on' if do_web else 'off'} ...\n")

    # backend="cli" forces Sonnet-generate + Opus-critique through the subscription,
    # regardless of the env-configured (free/local) default the 24/7 loop uses.
    opps = thesis_mod.run_engine(
        store, max_markets=max_markets, do_web=do_web, verbose=True, backend="cli",
        exclude_market_ids=exclude,
    )

    print(f"\n=== survivors: {len(opps)} bettable thesis(es) ===")
    for o in opps:
        print(f"  {o.side} {o.market_price:.3f} -> fair {o.fair_prob:.3f} "
              f"(edge {o.edge:+.3f}) | {o.question[:60]}")
        print(f"      {str(o.detail.get('claim', ''))[:110]}")
    print(f"\nThesis tally in DB: {store.count_theses()}  "
          f"(run `python -m pipeline.learn` to Brier-score them as markets resolve)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Premium (Sonnet+Opus via subscription) thesis pass")
    ap.add_argument("--max-markets", type=int, default=5,
                    help="how many top-triaged markets to run premium (keep small; rate limits)")
    ap.add_argument("--no-web", action="store_true", help="signals only, skip web search")
    ap.add_argument("--reeval-days", type=float, default=3.0,
                    help="skip markets premium-evaluated within this many days (0 = re-eval all)")
    args = ap.parse_args(argv)
    return run_premium(max_markets=args.max_markets, do_web=not args.no_web,
                       reeval_days=args.reeval_days)


if __name__ == "__main__":
    sys.exit(main())
