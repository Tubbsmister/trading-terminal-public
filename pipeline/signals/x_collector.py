"""
X / Twitter watchlist collector — fast Tier-3 early-warning ingestion.

Why this exists: the Fable-5 episode proved the pipeline's fatal latency gap.
The US-Commerce rescission + Anthropic "restoring July 1" announcement broke on
**X first**, hours before it reached the BBC RSS feed the monitor eventually saw —
and by then the NO position had already collapsed. X is where policy/company
announcements land first, so a targeted watchlist of the accounts that actually
move our markets is the earliest signal we can get.

Design goals:
  * **Keyless-first.** X's free API tier is write-only (no search/read). So we read
    a curated WATCHLIST of PUBLIC accounts via best-effort keyless routes (Nitter
    RSS, a reader proxy, the syndication endpoint), trying each in turn and
    degrading to [] — NEVER raising — when a route is down. Free routes are flaky
    by nature; this layer is opportunistic early-warning, not a guarantee.
  * **Upgrades cleanly.** The moment ``X_BEARER_TOKEN`` is set, we use the official
    v2 recent-search API (Basic tier+) instead — same Signal output, far more
    reliable. Nothing else in the pipeline changes.
  * **Targeted, not a firehose.** A watchlist keeps us well under any quota and
    keeps the signal-to-noise high. X is Tier-3 (social) — an early trip-wire that
    triggers a fuller check, never a fact that places/exits a bet on its own.

Every tweet becomes a ``Signal(source="x:@handle", source_tier=SOCIAL)`` deduped
in the store by tweet id; the relevance matcher links it to markets as usual.

Usage::

    python -m pipeline.signals.x_collector                    # default watchlist
    python -m pipeline.signals.x_collector --handle WhiteHouse --handle AnthropicAI
    python -m pipeline.signals.x_collector --query "Anthropic Fable export ban"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests

from pipeline.models import Signal, SourceTier
from pipeline.store import get_store


# ---------------------------------------------------------------------------
# Configuration (all overridable via env, comma-separated)
# ---------------------------------------------------------------------------

def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [x.strip().lstrip("@") for x in raw.split(",") if x.strip()]


#: Accounts whose posts actually move our markets. Curated, not a firehose.
#: Override with X_WATCHLIST="WhiteHouse,AnthropicAI,federalreserve".
DEFAULT_WATCHLIST: List[str] = [
    "WhiteHouse",       # US executive announcements (the Fable rescission origin)
    "AnthropicAI",      # the company behind the Fable/Mythos markets
    "federalreserve",   # FOMC / rate decisions
    "SECGov",           # filings / enforcement
    "Reuters",          # fast breaking wire (verification of the above)
]
WATCHLIST: List[str] = _env_list("X_WATCHLIST", DEFAULT_WATCHLIST)

#: Public Nitter instances (keyless RSS mirror of X). These come and go; override
#: with X_NITTER_INSTANCES="https://nitter.foo,https://nitter.bar". Empty list is
#: fine — the collector just falls through to the next route.
DEFAULT_NITTER: List[str] = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]
NITTER_INSTANCES: List[str] = _env_list("X_NITTER_INSTANCES", DEFAULT_NITTER)

#: Reader-proxy prefix that renders a JS page to plain text, keyless. Best-effort
#: last resort; set X_READER_PROXY="" to disable.
READER_PROXY = os.getenv("X_READER_PROXY", "https://r.jina.ai/")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) pipeline-x/1.0"}
_TIMEOUT = 12
_MAX_PER_HANDLE = 12
_BODY_MAX = 400


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)            # strip any HTML
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    for parser in (_parse_rfc822, _parse_iso):
        out = parser(raw)
        if out:
            return out
    return None


def _parse_rfc822(raw: str) -> Optional[str]:
    try:
        return _iso(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError):
        return None


def _parse_iso(raw: str) -> Optional[str]:
    try:
        s = raw.strip().replace("Z", "+00:00")
        if "+" not in s[10:] and not s.endswith("+00:00"):
            s += "+00:00"
        return _iso(datetime.fromisoformat(s))
    except ValueError:
        return None


def _mk_signal(handle: str, text: str, *, tweet_id: Optional[str],
               url: Optional[str], published_at: Optional[str]) -> Optional[Signal]:
    text = _clean(text)
    if not text:
        return None
    ext = f"x:{tweet_id}" if tweet_id else None
    return Signal(
        source=f"x:@{handle}",
        title=text[:300],
        source_tier=SourceTier.SOCIAL.value,
        external_id=ext,                       # None -> store falls back to content hash
        body=text[:_BODY_MAX] if len(text) > 300 else None,
        url=url,
        published_at=published_at,
        entities=[],
    )


# ---------------------------------------------------------------------------
# Route 1 (preferred): official X v2 API — only when a token is present
# ---------------------------------------------------------------------------

def _fetch_official(handles: List[str], query: Optional[str],
                    token: str) -> List[Signal]:
    """X v2 recent-search (Basic tier+). One request covers the whole watchlist via
    an OR of ``from:`` filters, so it is quota-cheap. Best-effort: [] on any error."""
    if query:
        q = query
    else:
        q = "(" + " OR ".join(f"from:{h}" for h in handles) + ") -is:retweet"
    params = {
        "query": q,
        "max_results": max(10, min(100, len(handles) * _MAX_PER_HANDLE)),
        "tweet.fields": "created_at,author_id,text",
        "expansions": "author_id",
        "user.fields": "username",
    }
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {token}", **_UA},
            params=params, timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"    (X official API HTTP {r.status_code}: {r.text[:120]})")
            return []
        payload = r.json()
        # Parse INSIDE the try: a malformed 200 (missing keys, non-dict) must
        # degrade to [], never raise — this module's hard "never raises" contract.
        users = (payload.get("includes", {}) or {}).get("users", []) or []
        id_to_handle = {u.get("id"): u.get("username", "")
                        for u in users if isinstance(u, dict) and u.get("id")}
        out: List[Signal] = []
        for tw in (payload.get("data", []) or []):
            if not isinstance(tw, dict):
                continue
            handle = id_to_handle.get(tw.get("author_id"), "") or "x"
            sig = _mk_signal(
                handle, tw.get("text", ""), tweet_id=tw.get("id"),
                url=f"https://x.com/{handle}/status/{tw.get('id')}",
                published_at=_parse_date(tw.get("created_at")),
            )
            if sig:
                out.append(sig)
        return out
    except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
        print(f"    (X official API error: {exc})")
        return []


# ---------------------------------------------------------------------------
# Route 2 (keyless): Nitter RSS mirror
# ---------------------------------------------------------------------------

def parse_nitter_rss(xml_bytes: bytes, handle: str) -> List[Signal]:
    """Parse a Nitter ``/<handle>/rss`` feed into Signals. Pure/­testable."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    items = (channel.findall("item") if channel is not None else root.findall("item"))
    out: List[Signal] = []
    for it in items[:_MAX_PER_HANDLE]:
        def txt(tag: str) -> Optional[str]:
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else None
        link = txt("link")
        # Nitter guids/links look like https://nitter.x/<handle>/status/<id>#m
        tweet_id = None
        m = re.search(r"/status/(\d+)", (txt("guid") or link or ""))
        if m:
            tweet_id = m.group(1)
        # rewrite the link back to canonical x.com
        url = None
        if tweet_id:
            url = f"https://x.com/{handle}/status/{tweet_id}"
        elif link:
            url = link
        sig = _mk_signal(
            handle, txt("title") or txt("description") or "",
            tweet_id=tweet_id, url=url, published_at=_parse_date(txt("pubDate")),
        )
        if sig:
            out.append(sig)
    return out


def _fetch_nitter(handle: str) -> List[Signal]:
    for base in NITTER_INSTANCES:
        try:
            r = requests.get(f"{base.rstrip('/')}/{handle}/rss",
                             headers=_UA, timeout=_TIMEOUT)
            if r.status_code == 200 and r.content:
                sigs = parse_nitter_rss(r.content, handle)
                if sigs:
                    return sigs
        except requests.RequestException:
            continue
    return []


# ---------------------------------------------------------------------------
# Route 3 (keyless last resort): reader proxy on the public profile page
# ---------------------------------------------------------------------------

def _fetch_reader(handle: str) -> List[Signal]:
    if not READER_PROXY:
        return []
    try:
        r = requests.get(f"{READER_PROXY}https://x.com/{handle}",
                         headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200 or not r.text:
            return []
    except requests.RequestException:
        return []
    # The proxy returns rendered text; keep non-trivial lines as coarse "posts".
    seen, out = set(), []
    for line in (l.strip() for l in r.text.splitlines()):
        if len(line) < 40 or line.startswith(("http", "![", "[", "#", "|")):
            continue
        if line in seen:
            continue
        seen.add(line)
        sig = _mk_signal(handle, line, tweet_id=None,
                         url=f"https://x.com/{handle}", published_at=None)
        if sig:
            out.append(sig)
        if len(out) >= _MAX_PER_HANDLE:
            break
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_x_watchlist(handles: Optional[List[str]] = None,
                      query: Optional[str] = None) -> List[Signal]:
    """Collect recent posts for a watchlist of X accounts (or a search ``query``).

    Prefers the official v2 API when ``X_BEARER_TOKEN`` is set; otherwise walks the
    keyless routes per handle (Nitter -> reader proxy). Best-effort throughout —
    a dead route yields fewer signals, never an exception.
    """
    handles = handles or WATCHLIST
    token = os.getenv("X_BEARER_TOKEN")
    if token:
        return _fetch_official(handles, query, token)

    if query:
        # No keyless full-search exists; the watchlist routes are per-account only.
        print("    (X: keyless mode cannot search a free-text query — set "
              "X_BEARER_TOKEN for search; falling back to the account watchlist)")

    out: List[Signal] = []
    for h in handles:
        sigs = _fetch_nitter(h) or _fetch_reader(h)
        out.extend(sigs)
    return out


def fetch_x_signals(query: str = "") -> List[Signal]:
    """Back-compat shim for pipeline.signals.social — delegates here."""
    return fetch_x_watchlist(query=query or None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--handle", dest="handles", action="append", metavar="NAME",
                    help="X account to poll (repeatable); default: the watchlist")
    ap.add_argument("--query", default=None,
                    help="free-text search (needs X_BEARER_TOKEN; keyless can't search)")
    args = ap.parse_args(argv)

    store = get_store()
    mode = "official API" if os.getenv("X_BEARER_TOKEN") else "keyless (best-effort)"
    handles = args.handles or WATCHLIST
    print(f"X collector [{mode}] | watchlist: {', '.join('@'+h for h in handles)}")
    print(f"DB: {store.db_path}")
    print("-" * 60)

    signals = fetch_x_watchlist(handles=handles, query=args.query)
    n_new = store.add_signals(signals)
    print(f"Fetched {len(signals)} post(s), {n_new} new.")
    if not signals:
        print("No posts (keyless routes may be down — add X_BEARER_TOKEN for a "
              "reliable feed).")
    else:
        for s in signals[:8]:
            print(f"  [{s.source}] {s.title[:80]}")
    print(f"Total signals in DB: {store.count_signals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
