"""
Official government/regulatory document collector — Tier-1 signal ingestion.

Why: constraint-shaped edges come from official documents with effective dates,
filing deadlines, and rule changes.  These move prediction-market prices before
the crowd prices them in.  This collector targets the richest free official
sources: the Federal Register (rules/notices with effective dates — the gold for
constraint theses) and SEC EDGAR 8-K filings (material events from public
companies).

Usage::

    python -m pipeline.signals.official                            # all sources, 50 items each
    python -m pipeline.signals.official --limit-per-source 20
    python -m pipeline.signals.official --source federal_register  # filter to one source
    python -m pipeline.signals.official --type RULE                # Federal Register type filter
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET

import requests

from pipeline.models import Signal, SourceTier
from pipeline.store import get_store


# ---------------------------------------------------------------------------
# Source registry  (source_name, description)
# ---------------------------------------------------------------------------

SOURCES: List[Tuple[str, str]] = [
    # ---- Federal Register (Tier 1) — richest for constraint edges ----------
    ("federal_register", "Federal Register API -- rules/notices/proposed-rules with effective dates"),
    # ---- SEC EDGAR 8-K filings (Tier 1) — material company events ---------
    ("sec_edgar",        "SEC EDGAR 8-K current-events Atom feed"),
]

_SOURCE_NAMES = [s for s, _ in SOURCES]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS     = {"User-Agent": "Mozilla/5.0 (pipeline-official-collector)"}
# SEC requires a descriptive User-Agent with contact info or it may 403.
_SEC_HEADERS = {"User-Agent": "trading-terminal research (contact: research@example.com)"}
_TIMEOUT     = 15          # seconds per request
_ATOM_NS     = "http://www.w3.org/2005/Atom"
_BODY_MAX    = 500

_FR_API_URL  = "https://www.federalregister.gov/api/v1/documents.json"
_SEC_8K_URL  = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&output=atom"
)

_TIER = SourceTier.OFFICIAL.value   # 1 — every signal from this module is Tier 1


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _truncate(text: Optional[str], max_len: int = _BODY_MAX) -> Optional[str]:
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_iso(date_str: Optional[str]) -> Optional[str]:
    """Parse ISO-8601 or plain YYYY-MM-DD to UTC ISO-8601.  Returns None on failure.

    Handles:
    * bare date        ``2024-01-15``            -> UTC midnight
    * naive datetime   ``2024-01-15T10:30:00``   -> assumed UTC
    * Z suffix         ``2024-01-15T10:30:00Z``
    * offset           ``2024-01-15T10:30:00-05:00``
    """
    if not date_str:
        return None
    try:
        s = date_str.strip()
        if len(s) == 10:          # bare date, e.g. "2024-01-15"
            s += "T00:00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Federal Register API
# ---------------------------------------------------------------------------

_FR_FIELDS = [
    "title", "abstract", "publication_date", "html_url",
    "document_number", "type", "agencies", "effective_on",
]

# Map common long agency names to short abbreviations for the title prefix.
_AGENCY_ABBREVS = {
    "environmental protection agency":           "EPA",
    "department of transportation":              "DOT",
    "department of health and human services":   "HHS",
    "food and drug administration":              "FDA",
    "securities and exchange commission":        "SEC",
    "federal reserve system":                    "Fed",
    "federal communications commission":         "FCC",
    "department of labor":                       "DOL",
    "department of energy":                      "DOE",
    "department of justice":                     "DOJ",
    "department of homeland security":           "DHS",
    "department of the treasury":                "Treasury",
    "commodity futures trading commission":      "CFTC",
    "federal trade commission":                  "FTC",
    "department of commerce":                    "Commerce",
    "department of defense":                     "DOD",
    "department of agriculture":                 "USDA",
    "federal aviation administration":           "FAA",
    "nuclear regulatory commission":             "NRC",
    "federal energy regulatory commission":      "FERC",
    "centers for medicare & medicaid services":  "CMS",
    "internal revenue service":                  "IRS",
    "consumer financial protection bureau":      "CFPB",
    "office of the comptroller of the currency": "OCC",
    "federal deposit insurance corporation":     "FDIC",
    "bureau of alcohol, tobacco, firearms and explosives": "ATF",
    "immigration and customs enforcement":       "ICE",
    "customs and border protection":             "CBP",
}


def _fr_agency_abbrev(agencies: list) -> str:
    """Return a short agency label from the agencies array (first entry used)."""
    if not agencies:
        return ""
    first = agencies[0]
    name: str = first.get("name", "") if isinstance(first, dict) else str(first)
    lower = name.lower().strip()
    for pattern, abbrev in _AGENCY_ABBREVS.items():
        if pattern in lower:
            return abbrev
    # Fallback: first two words, max 20 chars total
    words = name.split()
    return " ".join(words[:2])[:20] if words else name[:20]


def fetch_federal_register(limit: int, doc_type: Optional[str] = None) -> List[Signal]:
    """Fetch the latest Federal Register documents via the JSON API.

    Each result becomes a Signal.  Titles are prefixed with ``[Type|Agency]``
    and suffixed with ``(effective YYYY-MM-DD)`` when an effective date exists —
    effective dates are the gold for constraint theses.
    """
    # requests serialises a list of (key, value) tuples correctly,
    # preserving repeated keys such as fields[].
    param_list: list = [
        ("per_page", min(limit, 1000)),
        ("order", "newest"),
    ]
    for f in _FR_FIELDS:
        param_list.append(("fields[]", f))
    if doc_type:
        param_list.append(("conditions[type][]", doc_type.upper()))

    resp = requests.get(_FR_API_URL, params=param_list, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    signals: List[Signal] = []
    for doc in (data.get("results") or [])[:limit]:
        doc_type_val = (doc.get("type") or "").strip()
        agency_label = _fr_agency_abbrev(doc.get("agencies") or [])

        # Build title prefix:  "[Rule | EPA] ..."
        prefix_parts = [p for p in [doc_type_val, agency_label] if p]
        prefix = f"[{' | '.join(prefix_parts)}] " if prefix_parts else ""

        raw_title = (doc.get("title") or "").strip()
        title = prefix + raw_title

        # Append effective date when present — the constraint-edge gold
        effective_on: Optional[str] = (doc.get("effective_on") or "").strip() or None
        if effective_on:
            title += f" (effective {effective_on})"

        title = title[:300]

        signals.append(Signal(
            source="federal_register",
            title=title,
            source_tier=_TIER,
            external_id=doc.get("document_number") or None,
            body=_truncate((doc.get("abstract") or "").strip() or None),
            url=doc.get("html_url") or None,
            published_at=_parse_iso(doc.get("publication_date")),
            entities=[],
        ))
    return signals


# ---------------------------------------------------------------------------
# SEC EDGAR 8-K Atom feed
# ---------------------------------------------------------------------------

def _atom_tag(local: str) -> str:
    return f"{{{_ATOM_NS}}}{local}"


def fetch_sec_edgar(limit: int) -> List[Signal]:
    """Fetch SEC 8-K current filings via the EDGAR Atom feed.

    SEC will 403 requests without a descriptive User-Agent.  A 403 is logged
    and returns an empty list (does not raise).
    """
    resp = requests.get(_SEC_8K_URL, headers=_SEC_HEADERS, timeout=_TIMEOUT)
    if resp.status_code == 403:
        # Logged here so the outer loop still sees "0 items, 0 new" rather
        # than a failure; the 403 message tells the user why.
        print(f"[sec_edgar           ] NOTE: SEC returned 403 "
              f"(User-Agent blocked or rate limit) -- 0 items fetched")
        return []
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    signals: List[Signal] = []
    for entry in root.findall(_atom_tag("entry"))[:limit]:
        def _txt(local: str, _entry: ET.Element = entry) -> Optional[str]:
            el = _entry.find(_atom_tag(local))
            return (el.text or "").strip() or None if el is not None else None

        # Prefer rel="alternate" href, fall back to first <link> element.
        link: Optional[str] = None
        for link_el in entry.findall(_atom_tag("link")):
            href = (link_el.get("href") or link_el.text or "").strip()
            if href:
                link = href
                break

        ext_id = _txt("id") or link
        date_raw = _txt("updated") or _txt("published")

        title_el = entry.find(_atom_tag("title"))
        title_raw = (title_el.text or "").strip() if title_el is not None else ""

        signals.append(Signal(
            source="sec_edgar",
            title=title_raw or "(no title)",
            source_tier=_TIER,
            external_id=ext_id,
            body=_truncate(_txt("summary")),
            url=link,
            published_at=_parse_iso(date_raw),
            entities=[],
        ))
    return signals


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def fetch_source(
    source: str,
    limit: int,
    doc_type: Optional[str] = None,
) -> List[Signal]:
    """Route a source name to the correct fetcher."""
    if source == "federal_register":
        return fetch_federal_register(limit, doc_type=doc_type)
    if source == "sec_edgar":
        return fetch_sec_edgar(limit)
    raise ValueError(f"Unknown source: {source!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # Windows cp1252 consoles cannot print many characters found in government
    # document titles; reconfigure to utf-8 with replacement so we never crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--limit-per-source", type=int, default=50,
        help="max items to ingest per source (default 50)",
    )
    ap.add_argument(
        "--source", dest="sources", action="append", metavar="NAME",
        help=(
            "filter to specific source names (repeatable); default: all.\n"
            f"Available: {', '.join(_SOURCE_NAMES)}"
        ),
    )
    ap.add_argument(
        "--type", dest="doc_type", metavar="TYPE", default=None,
        help="Federal Register document type filter: RULE, PRORULE, NOTICE (optional)",
    )
    args = ap.parse_args(argv)

    store = get_store()

    sources_to_run = _SOURCE_NAMES
    if args.sources:
        wanted = set(args.sources)
        sources_to_run = [s for s in _SOURCE_NAMES if s in wanted]
        if not sources_to_run:
            print(f"No sources matched {args.sources!r}.  Available: {_SOURCE_NAMES}")
            return 1

    type_label = f"  |  FR type filter: {args.doc_type}" if args.doc_type else ""
    print(
        f"Official collector  |  {len(sources_to_run)} source(s)  |  "
        f"limit {args.limit_per_source}/source{type_label}"
    )
    print(f"DB: {store.db_path}")
    print("-" * 60)

    total_new = 0
    sources_ok = 0
    sources_failed = 0

    for source in sources_to_run:
        try:
            signals = fetch_source(source, args.limit_per_source, doc_type=args.doc_type)
            n_new = store.add_signals(signals)
            total_new += n_new
            sources_ok += 1
            print(f"[{source:20s}] {len(signals):3d} items, {n_new:3d} new")
        except Exception as exc:
            sources_failed += 1
            # Keep error message ASCII-safe for cp1252 consoles.
            err = str(exc).encode("ascii", errors="replace").decode("ascii")
            print(f"[{source:20s}] FAILED: {err}")

    print("-" * 60)
    print(f"Sources: {sources_ok} ok, {sources_failed} failed")
    print(f"New signals this run : {total_new}")
    print(f"Total signals in DB  : {store.count_signals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
