"""
News and official-release RSS/Atom collector — Phase 3 signal ingestion.

Why: pull fresh headlines and official releases from free public feeds, normalize
them to ``Signal`` objects, and persist to the DB deduped.  This is the primary
Tier-1 (official) and Tier-2 (news) ingestion for the "information edge":
signals that may move prediction-market prices before the crowd has fully priced
them in.  A later relevance matcher links each signal to the markets it might
move; the LLM estimator then estimates updated probabilities.

Usage::

    python -m pipeline.signals.news                         # all feeds, 50 items each
    python -m pipeline.signals.news --limit-per-feed 20
    python -m pipeline.signals.news --feed sec --feed fed   # filter to specific sources
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET

import requests

from pipeline.models import Signal, SourceTier
from pipeline.store import get_store


# ---------------------------------------------------------------------------
# Feed registry  (source_name, url, tier)
# ---------------------------------------------------------------------------

FEEDS: List[Tuple[str, str, int]] = [
    # ---- Official (Tier 1) ------------------------------------------------
    ("sec",
     "https://www.sec.gov/news/pressreleases.rss",
     SourceTier.OFFICIAL.value),
    ("fed",
     "https://www.federalreserve.gov/feeds/press_all.xml",
     SourceTier.OFFICIAL.value),
    ("bls",
     "https://www.bls.gov/feed/bls_latest.rss",
     SourceTier.OFFICIAL.value),
    ("whitehouse",
     "https://www.whitehouse.gov/feed/",
     SourceTier.OFFICIAL.value),
    # ---- News (Tier 2) ----------------------------------------------------
    ("npr",
     "https://feeds.npr.org/1001/rss.xml",
     SourceTier.NEWS.value),
    ("bbc",
     "https://feeds.bbci.co.uk/news/world/rss.xml",
     SourceTier.NEWS.value),
    ("guardian",
     "https://www.theguardian.com/world/rss",
     SourceTier.NEWS.value),
    ("aljazeera",
     "https://www.aljazeera.com/xml/rss/all.xml",
     SourceTier.NEWS.value),
    # ---- Google News topic RSS (Tier 2) -----------------------------------
    ("gnews-sec",
     "https://news.google.com/rss/search?q=when:24h+SEC&hl=en-US&gl=US&ceid=US:en",
     SourceTier.NEWS.value),
    ("gnews-fed",
     "https://news.google.com/rss/search?q=when:24h+Federal+Reserve&hl=en-US&gl=US&ceid=US:en",
     SourceTier.NEWS.value),
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (pipeline-news-collector)"}
_TIMEOUT = 15   # seconds per request
_ATOM_NS = "http://www.w3.org/2005/Atom"
_BODY_MAX = 500


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _strip_html(text: Optional[str]) -> Optional[str]:
    """Remove HTML tags from a string; return None if the result is empty."""
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", "", text).strip()
    return cleaned or None


def _truncate(text: Optional[str], max_len: int = _BODY_MAX) -> Optional[str]:
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_rfc822(date_str: Optional[str]) -> Optional[str]:
    """Parse RFC-822 / RSS pubDate to UTC ISO-8601.  Returns None on any failure."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def _parse_iso(date_str: Optional[str]) -> Optional[str]:
    """Parse ISO-8601 / Atom updated|published to UTC ISO-8601.
    Handles trailing 'Z' and naive datetimes (assumed UTC).
    Returns None on any failure.
    """
    if not date_str:
        return None
    try:
        s = date_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # naive datetime (no tz info) -> treat as UTC
        if "+" not in s and not s.endswith("+00:00"):
            s += "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# XML parsers
# ---------------------------------------------------------------------------

def _atom_tag(local: str) -> str:
    return f"{{{_ATOM_NS}}}{local}"


def _parse_rss(root: ET.Element) -> List[dict]:
    """Parse an RSS 2.0 (or RSS 1.0/RDF) root; return list of entry dicts."""
    entries: List[dict] = []
    # RSS 2.0: items live inside <channel>; RSS 1.0 RDF: directly under root
    channel = root.find("channel")
    items_parent = channel if channel is not None else root
    for item in items_parent.findall("item"):
        def txt(tag: str) -> Optional[str]:
            el = item.find(tag)
            return (el.text or "").strip() or None if el is not None else None

        # guid preferred as stable external_id; fall back to link
        guid = txt("guid")
        link = txt("link")
        # Some RSS parsers put the link as tail text after a self-closing element
        if link is None:
            link_el = item.find("link")
            if link_el is not None:
                link = (link_el.tail or "").strip() or None

        ext_id = guid or link

        # try both <description> and <content:encoded>
        body_raw = txt("description")
        if body_raw is None:
            # content:encoded uses a namespace; try a loose search
            for child in item:
                if child.tag.endswith("}encoded"):
                    body_raw = (child.text or "").strip() or None
                    break

        entries.append({
            "external_id":  ext_id,
            "title":        txt("title") or "",
            "url":          link,
            "body":         body_raw,
            "published_at": _parse_rfc822(txt("pubDate")),
        })
    return entries


def _parse_atom(root: ET.Element) -> List[dict]:
    """Parse an Atom feed root; return list of entry dicts."""
    entries: List[dict] = []
    for entry in root.findall(_atom_tag("entry")):
        def txt(local: str) -> Optional[str]:
            el = entry.find(_atom_tag(local))
            return (el.text or "").strip() or None if el is not None else None

        # <link rel="alternate" href="..."/> or first <link>
        link: Optional[str] = None
        for link_el in entry.findall(_atom_tag("link")):
            rel = link_el.get("rel", "alternate")
            href = link_el.get("href") or link_el.text or ""
            if rel == "alternate":
                link = href.strip() or None
                break
        if link is None:
            link_el = entry.find(_atom_tag("link"))
            if link_el is not None:
                link = (link_el.get("href") or link_el.text or "").strip() or None

        ext_id = txt("id") or link
        date_raw = txt("updated") or txt("published")
        body_raw = txt("summary") or txt("content")

        # <title type="html"> needs tag stripping
        title_el = entry.find(_atom_tag("title"))
        title_raw = (title_el.text or "").strip() if title_el is not None else ""

        entries.append({
            "external_id":  ext_id,
            "title":        title_raw,
            "url":          link,
            "body":         body_raw,
            "published_at": _parse_iso(date_raw),
        })
    return entries


def _parse_feed_xml(content: bytes) -> List[dict]:
    """Auto-detect RSS vs Atom from the root element and parse accordingly."""
    root = ET.fromstring(content)
    tag = root.tag
    # Atom: root tag is {http://www.w3.org/2005/Atom}feed  or  feed
    if tag == _atom_tag("feed") or tag == "feed":
        return _parse_atom(root)
    # RSS 2.0: <rss ...>  or  RSS 1.0: <rdf:RDF ...>  -> RSS parser handles both
    return _parse_rss(root)


# ---------------------------------------------------------------------------
# Per-feed fetch
# ---------------------------------------------------------------------------

def fetch_feed(
    source: str,
    url: str,
    tier: int,
    limit: int,
) -> List[Signal]:
    """Fetch one feed URL and return a list of ``Signal`` objects (not yet stored)."""
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    entries = _parse_feed_xml(resp.content)[:limit]

    signals: List[Signal] = []
    for e in entries:
        raw_title = e.get("title") or ""
        title = (_strip_html(raw_title) or "(no title)")[:300]

        body = _truncate(_strip_html(e.get("body")), _BODY_MAX)
        ext_id: Optional[str] = (e.get("external_id") or "").strip() or e.get("url") or None

        signals.append(Signal(
            source=source,
            title=title,
            source_tier=tier,
            external_id=ext_id,
            body=body,
            url=e.get("url"),
            published_at=e.get("published_at"),
            entities=[],
        ))
    return signals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # Windows cp1252 console can't print many headline chars; reconfigure to
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
        "--limit-per-feed", type=int, default=50,
        help="max items to ingest per feed (default 50)",
    )
    ap.add_argument(
        "--feed", dest="feeds", action="append", metavar="NAME",
        help="filter to specific source names (repeatable); default: all",
    )
    args = ap.parse_args(argv)

    store = get_store()

    feeds_to_run = FEEDS
    if args.feeds:
        wanted = set(args.feeds)
        feeds_to_run = [(s, u, t) for s, u, t in FEEDS if s in wanted]
        if not feeds_to_run:
            known = [s for s, _, _ in FEEDS]
            print(f"No feeds matched {args.feeds!r}.  Available: {known}")
            return 1

    print(f"News collector  |  {len(feeds_to_run)} feed(s)  |  "
          f"limit {args.limit_per_feed}/feed")
    print(f"DB: {store.db_path}")
    print("-" * 60)

    total_new = 0
    feeds_ok = 0
    feeds_failed = 0

    for source, url, tier in feeds_to_run:
        try:
            signals = fetch_feed(source, url, tier, args.limit_per_feed)
            n_new = store.add_signals(signals)
            total_new += n_new
            feeds_ok += 1
            print(f"[{source:12s}] {len(signals):3d} items, {n_new:3d} new")
        except Exception as exc:
            feeds_failed += 1
            # keep error message ASCII-safe for cp1252 consoles
            err = str(exc).encode("ascii", errors="replace").decode("ascii")
            print(f"[{source:12s}] FAILED: {err}")

    print("-" * 60)
    print(f"Feeds: {feeds_ok} ok, {feeds_failed} failed")
    print(f"New signals this run : {total_new}")
    print(f"Total signals in DB  : {store.count_signals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
