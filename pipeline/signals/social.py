"""
Social / retail-sentiment collector — Tier-3 signal ingestion.

Why: pull crowd-level signals from Reddit (and optionally X/Twitter when a key
is available) and persist them deduped in the ``signals`` table tagged as Tier 3
(SourceTier.SOCIAL).  Social chatter surfaces early information propagation and
retail-sentiment shifts that can move prediction-market prices before institutional
sources have digested them.

Sources
-------
* Reddit  — FREE, no API key required. JSON hot/top feeds per subreddit.
* X       — Requires X_BEARER_TOKEN (v2 recent-search). Stub returns [] if absent.

Usage::

    python -m pipeline.signals.social                              # all subs, 25 each
    python -m pipeline.signals.social --limit-per-sub 15
    python -m pipeline.signals.social --sub geopolitics --sub Economics --limit-per-sub 15
    python -m pipeline.signals.social --min-score 100              # stricter filter
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

import requests

from pipeline.models import Signal, SourceTier
from pipeline.store import get_store


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Subreddits to poll by default — ordered roughly by signal quality for
#: prediction markets (geopolitics/econ first, then retail sentiment).
SUBREDDITS: List[str] = [
    "geopolitics",
    "Economics",
    "politics",
    "wallstreetbets",
    "technology",
    "worldnews",
    "Conservative",
    "neoliberal",
]

_REDDIT_UA = "trading-terminal:research:v1 (by /u/researcher)"
_REDDIT_HEADERS = {"User-Agent": _REDDIT_UA}
_TIMEOUT = 15          # seconds per request
_THROTTLE = 1.0        # seconds between subreddit fetches (Reddit rate-limit)
_BODY_MAX = 300        # chars of selftext to keep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_iso_utc(ts: float) -> str:
    """Convert a Unix epoch float (Reddit created_utc) to UTC ISO-8601."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _truncate(text: Optional[str], max_len: int = _BODY_MAX) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    if not text or text == "self." or text == "[deleted]" or text == "[removed]":
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


# ---------------------------------------------------------------------------
# Reddit collector
# ---------------------------------------------------------------------------

def fetch_reddit_sub(
    sub: str,
    *,
    listing: str = "hot",
    limit: int = 25,
    min_score: int = 50,
) -> List[Signal]:
    """Fetch one subreddit listing and return a list of Signal objects.

    Args:
        sub:       Subreddit name (no r/ prefix).
        listing:   ``"hot"`` or ``"top"``; ``"top"`` also appends ``?t=day``.
        limit:     Max posts to request from Reddit.
        min_score: Discard posts whose score is below this threshold.

    Returns:
        List of Signal objects (not yet stored).
    """
    if listing == "top":
        url = f"https://www.reddit.com/r/{sub}/top.json?limit={limit}&t=day"
    else:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"

    resp = requests.get(url, headers=_REDDIT_HEADERS, timeout=_TIMEOUT)

    if resp.status_code in (429, 403):
        raise requests.HTTPError(
            f"HTTP {resp.status_code} from Reddit on r/{sub} — rate-limited or forbidden",
            response=resp,
        )
    resp.raise_for_status()

    payload = resp.json()
    children = payload.get("data", {}).get("children", [])

    signals: List[Signal] = []
    for child in children:
        post = child.get("data", {})

        # --- score filter ---
        score = post.get("score", 0)
        if score < min_score:
            continue

        post_id = post.get("id") or ""
        if not post_id:
            continue

        title = (post.get("title") or "").strip()
        if not title:
            continue

        permalink = post.get("permalink") or f"/r/{sub}"
        post_url = "https://www.reddit.com" + permalink

        created_utc = post.get("created_utc")
        published_at = _to_iso_utc(float(created_utc)) if created_utc is not None else None

        body = _truncate(post.get("selftext"))

        signals.append(Signal(
            source=f"reddit:r/{sub}",
            title=title[:300],
            source_tier=SourceTier.SOCIAL.value,
            external_id=f"reddit:{post_id}",
            body=body,
            url=post_url,
            published_at=published_at,
            entities=[],
        ))

    return signals


# ---------------------------------------------------------------------------
# X / Twitter (stub)
# ---------------------------------------------------------------------------

def fetch_x_signals(query: str = "") -> List[Signal]:
    """Fetch recent X posts for the watchlist (or a search ``query``).

    Delegates to :mod:`pipeline.signals.x_collector`, which reads a curated
    watchlist of accounts keyless-first (Nitter / reader proxy) and auto-upgrades
    to the official v2 API when ``X_BEARER_TOKEN`` is set. Best-effort: [] on any
    failure, never raises.
    """
    from pipeline.signals.x_collector import fetch_x_watchlist
    return fetch_x_watchlist(query=query or None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # Windows cp1252 console can't print many Unicode chars; reconfigure to
    # utf-8 with replacement so we never crash on a foreign headline.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--limit-per-sub", type=int, default=25,
        help="max posts to request per subreddit (default 25)",
    )
    ap.add_argument(
        "--sub", dest="subs", action="append", metavar="NAME",
        help="subreddit name (repeatable); default: all in SUBREDDITS list",
    )
    ap.add_argument(
        "--min-score", type=int, default=50,
        help="skip Reddit posts with score below this value (default 50)",
    )
    ap.add_argument(
        "--listing", choices=["hot", "top"], default="hot",
        help="Reddit listing to use: 'hot' (default) or 'top' (day)",
    )
    args = ap.parse_args(argv)

    subs_to_run: List[str] = args.subs if args.subs else list(SUBREDDITS)

    store = get_store()

    print(f"Social collector  |  {len(subs_to_run)} sub(s)  |  "
          f"limit {args.limit_per_sub}/sub  |  min-score {args.min_score}")
    print(f"DB: {store.db_path}")
    print("-" * 60)

    total_fetched = 0
    total_new = 0
    subs_ok = 0
    subs_failed = 0

    for i, sub in enumerate(subs_to_run):
        # Throttle between requests to respect Reddit rate limits.
        if i > 0:
            time.sleep(_THROTTLE)

        try:
            signals = fetch_reddit_sub(
                sub,
                listing=args.listing,
                limit=args.limit_per_sub,
                min_score=args.min_score,
            )
            n_new = store.add_signals(signals)
            total_fetched += len(signals)
            total_new += n_new
            subs_ok += 1
            print(f"[r/{sub:<16s}] {len(signals):3d} posts, {n_new:3d} new")
        except Exception as exc:
            subs_failed += 1
            err = str(exc).encode("ascii", errors="replace").decode("ascii")
            print(f"[r/{sub:<16s}] FAILED: {err}")

    # X / Twitter watchlist (best-effort; must not crash the social run)
    try:
        x_signals = fetch_x_signals()
        if x_signals:
            n_new_x = store.add_signals(x_signals)
            total_fetched += len(x_signals)
            total_new += n_new_x
            print(f"[x               ] {len(x_signals):3d} tweets, {n_new_x:3d} new")
    except Exception as exc:
        err = str(exc).encode("ascii", errors="replace").decode("ascii")
        print(f"[x               ] FAILED: {err}")

    print("-" * 60)
    print(f"Subreddits: {subs_ok} ok, {subs_failed} failed")
    print(f"New signals this run : {total_new}")
    print(f"Total signals in DB  : {store.count_signals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
