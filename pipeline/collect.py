"""
Collection entry point — pull all markets across venues into the local DB.

    python -m pipeline.collect                      # all venues, default limits
    python -m pipeline.collect --venue kalshi       # one venue
    python -m pipeline.collect --limit 1000         # per-venue cap
    python -m pipeline.collect --no-snapshot        # don't append price history

This is the first stage of the pipeline. It is read-only against the venues and
writes only to ``pipeline.db``. Run it on a schedule (cron / the daemon) to keep
the market view fresh and to accumulate cross-venue price history over time.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List

from .collectors import ALL_COLLECTORS
from .models import Market
from .store import Store, get_store


def collect_venue(name: str, *, limit: int, store: Store, snapshot: bool) -> int:
    collector_cls = ALL_COLLECTORS[name]
    collector = collector_cls()
    t0 = time.time()
    try:
        markets: List[Market] = collector.collect(limit=limit)
    except Exception as e:  # one venue failing must not abort the others
        print(f"  [{name}] collection FAILED: {e}")
        return 0
    n = store.upsert_markets(markets, snapshot=snapshot)
    dt = time.time() - t0
    priced = sum(1 for m in markets if m.yes_price is not None)
    print(f"  [{name}] {n} markets ({priced} priced) in {dt:.1f}s")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--venue", choices=list(ALL_COLLECTORS), action="append",
                    help="venue(s) to collect; repeatable. Default: all.")
    ap.add_argument("--limit", type=int, default=500,
                    help="max markets per venue (default 500)")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="don't append a price-history snapshot row")
    args = ap.parse_args(argv)

    venues = args.venue or list(ALL_COLLECTORS)
    store = get_store()
    print(f"Collecting from: {', '.join(venues)}  (limit {args.limit}/venue)")
    print(f"DB: {store.db_path}")

    total = 0
    for v in venues:
        total += collect_venue(v, limit=args.limit, store=store,
                               snapshot=not args.no_snapshot)

    counts = store.count_markets(active_only=True)
    print(f"\nDone. {total} markets upserted this run.")
    print("Active markets in DB by venue:")
    for venue, n in sorted(counts.items()):
        print(f"  {venue:10s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
