"""
Signal-to-market relevance matching -- the cheap keyword/entity pre-filter.

WHY THIS EXISTS
---------------
The pipeline collects thousands of signals (news, official releases) and tracks
~1400 active prediction markets. Before spending LLM tokens estimating probability
updates, we need a fast pre-filter that answers: for each fresh signal, which
markets might it actually move?

This module does that without any LLM calls:

1. Build an inverted token index over all active markets (token -> set of market
   IDs). This avoids the naive O(signals x markets) blowup -- only candidate
   pairs that share at least one significant token are ever scored.
2. For each recent signal, look up only the markets that share at least one token.
3. Score each candidate pair with the OVERLAP COEFFICIENT rather than Jaccard.
   Overlap coefficient = |A ∩ B| / min(|A|, |B|). This is preferred here because
   signal texts (title + body) are much longer than a short market question.
   Jaccard would systematically under-score good matches because the large pool
   of signal-only tokens inflates the union denominator. Overlap coefficient only
   asks "how much of the SMALLER set (the question) is covered by the signal?"
4. Boost for shared tokens that appear CAPITALIZED in both the original signal
   text and the original question. These are proxy proper nouns (people, places,
   orgs) -- the highest-signal tokens for prediction markets.
5. Persist the top-N relevance edges via store.link_signal_market so the LLM
   estimator can cheaply read which fresh signals are relevant to each market.

SCORING FORMULA
---------------
  raw       = |A ∩ B| / min(|A|, |B|)           # overlap coefficient
  cap_boost = min(0.30, 0.15 * n_shared_caps)    # proper-noun bonus
  score     = min(1.0, raw + cap_boost)
  keep if   |A ∩ B| >= min_shared (default 2) AND score >= min_relevance (default 0.12)

PRECISION GATE (why ≥2 shared tokens)
-------------------------------------
Date/number tokens are dropped before matching (a shared "2026"/"June" is calendar
coincidence, not topic). And a single shared word — even a rare one — is too weak:
keyword overlap can't distinguish a generic-but-rare token ("act" shared between
"Sunshine Act" and "CLARITY Act") from a distinctive one, so we require topical
CORROBORATION of ≥2 shared non-date tokens. This is what keeps spurious evidence (e.g.
unrelated FDIC notices) from reaching the thesis generator, which hallucinates from it.

Run: python -m pipeline.signals.relevance [--hours H] [--min-relevance F]
                                          [--max-links-per-signal N] [--tier T]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from pipeline.store import get_store
from pipeline.models import Market, Signal


# ---------------------------------------------------------------------------
# Tokenizer
# Adapted from pipeline.edges.arb._tokens; kept as a local copy to avoid
# importing private names across module boundaries.
# ---------------------------------------------------------------------------

_STOP: Set[str] = {
    "will", "the", "be", "in", "on", "of", "to", "a", "an", "by", "at",
    "for", "before", "after", "than", "or", "and", "is", "are", "this",
    "that", "20", "if", "its", "it", "as", "was", "has", "have", "had",
    "not", "no", "do", "does", "did", "from", "with", "which", "who",
    "what", "when", "how", "any", "all", "been", "more", "up", "out",
    "about", "into", "during", "through", "between", "under", "over",
    "per", "vs", "then", "can", "may", "could", "would", "should",
    "new", "us", "their", "them", "they", "we", "you", "he", "she",
}


# Month names + pure numbers are excluded from relevance tokens: a shared "2026" or
# "june" is calendar coincidence, not topical relevance (it linked "Lebanon by June 30"
# to "Johannesburg market violence" via {30, june}). Dates carry zero topical signal here.
_MONTHS: Set[str] = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december",
}


def _is_date_num(w: str) -> bool:
    return w.isdigit() or w in _MONTHS


def _tokens(text: str) -> Set[str]:
    """Lowercase, split on non-alphanumeric, drop stopwords, 1-char tokens, and pure
    date/number tokens (months, years, bare integers — calendar coincidence, not topic)."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP and len(w) > 1 and not _is_date_num(w)}


def _cap_tokens(text: str) -> Set[str]:
    """Return the lowercased form of tokens that begin with a capital in ``text``.

    Matches two patterns:
    - Mixed-case words starting with a capital (Powell, Germany, Federal)
    - All-caps abbreviations of 2+ chars (GDP, ECB, FED, NATO)

    These serve as proxy proper nouns for the entity-matching boost.
    """
    words = re.findall(r"[A-Z]{2,}|[A-Z][a-z0-9]+", text)
    return {w.lower() for w in words if len(w) > 1} - _STOP


# ---------------------------------------------------------------------------
# Inverted index builder
# ---------------------------------------------------------------------------

def _build_inverted_index(
    markets: List[Market],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]], Dict[str, Set[str]], Dict[str, Market]]:
    """Build an inverted token index and per-market token/cap caches.

    Returns:
        inverted   : token -> set of market IDs
        mkt_tokens : market_id -> token set (question + category)
        mkt_caps   : market_id -> capitalized-token set
        mkt_by_id  : market_id -> Market object
    """
    inverted: Dict[str, Set[str]] = defaultdict(set)
    mkt_tokens: Dict[str, Set[str]] = {}
    mkt_caps: Dict[str, Set[str]] = {}
    mkt_by_id: Dict[str, Market] = {}

    for m in markets:
        if not m.question:
            continue
        mtext = m.question + (" " + m.category if m.category else "")
        toks = _tokens(mtext)
        caps = _cap_tokens(mtext)
        mkt_tokens[m.id] = toks
        mkt_caps[m.id] = caps
        mkt_by_id[m.id] = m
        for tok in toks:
            inverted[tok].add(m.id)

    return inverted, mkt_tokens, mkt_caps, mkt_by_id


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# A link requires at least this many shared (non-date) tokens. A single shared word —
# even a rare one — is too weak: keyword overlap can't tell a generic-but-rare token
# ("act" in "Sunshine Act" vs "CLARITY Act") from a distinctive one, so we demand topical
# CORROBORATION (≥2 shared terms). This is the precision lever that stops the generator
# being fed spurious evidence (e.g. FDIC notices on a crypto-bill market) it hallucinates
# from. Measured: drops links 36k→5.2k (−85%) while 942 markets keep ≥1 link.
MIN_SHARED_TOKENS = 2


def link_qualifies(score: float, shared: Set[str], min_relevance: float,
                   min_shared: int = MIN_SHARED_TOKENS) -> bool:
    """A (signal, market) pair becomes a link only with enough topical corroboration."""
    return len(shared) >= min_shared and score >= min_relevance


def _score_pair(
    sig_toks: Set[str],
    sig_caps: Set[str],
    mkt_toks: Set[str],
    mkt_caps: Set[str],
) -> Tuple[float, Set[str]]:
    """Compute the relevance score for one (signal, market) candidate pair.

    Returns (score in [0, 1], set of shared tokens).
    """
    shared = sig_toks & mkt_toks
    if not shared:
        return 0.0, set()

    # Overlap coefficient: asks "does the signal cover the question's topic?"
    raw = len(shared) / min(len(sig_toks), len(mkt_toks))

    # Proper-noun boost: tokens capitalized in BOTH original texts
    shared_caps = (sig_caps & mkt_caps) & shared
    cap_boost = min(0.30, 0.15 * len(shared_caps))

    score = min(1.0, raw + cap_boost)
    return score, shared


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(
    hours: float = 48,
    min_relevance: float = 0.12,
    max_links: int = 8,
    tier: Optional[int] = None,
    min_shared: int = MIN_SHARED_TOKENS,
) -> int:
    """Run the relevance matcher. Returns the total number of links written."""
    store = get_store()

    signals = store.get_recent_signals(hours=hours, source_tier=tier, limit=1000)
    if not signals:
        print("No recent signals -- run python -m pipeline.signals.news first")
        return 0

    markets = store.get_markets(active_only=True)
    if not markets:
        print("No active markets in the store.")
        return 0

    print(f"Loaded {len(signals)} signals and {len(markets)} active markets.")

    inverted, mkt_tokens, mkt_caps, mkt_by_id = _build_inverted_index(markets)

    # Score signals against candidates
    total_links = 0
    total_comparisons = 0
    signals_processed = 0
    # (score, market_question, signal_title) for the top-10 printout
    top_matches: List[Tuple[float, str, str]] = []

    for sig in signals:
        if sig.id is None:
            continue

        sig_text = sig.title + (" " + sig.body if sig.body else "")
        sig_toks = _tokens(sig_text)
        sig_caps = _cap_tokens(sig_text)

        if not sig_toks:
            signals_processed += 1
            continue

        # Candidate markets: union of inverted-index entries for signal tokens
        candidates: Set[str] = set()
        for tok in sig_toks:
            if tok in inverted:
                candidates |= inverted[tok]

        # Score each candidate
        scored: List[Tuple[float, str, Set[str]]] = []
        for mid in candidates:
            if mid not in mkt_tokens:
                continue
            total_comparisons += 1
            score, shared = _score_pair(
                sig_toks, sig_caps,
                mkt_tokens[mid], mkt_caps[mid],
            )
            if link_qualifies(score, shared, min_relevance, min_shared):
                scored.append((score, mid, shared))

        # Keep top N by score
        scored.sort(key=lambda x: x[0], reverse=True)
        top_n = scored[:max_links]

        # Idempotent re-linking: drop this signal's existing links first, so a re-run
        # (e.g. after this rule change) PURGES links that no longer qualify rather than
        # leaving stale INSERT-OR-REPLACE orphans the generator would still read.
        store.clear_links_for_signal(sig.id)
        for score, mid, shared in top_n:
            # Truncate matched_terms to first 6 shared tokens (alphabetical)
            matched_terms = ", ".join(sorted(shared)[:6])
            store.link_signal_market(sig.id, mid, round(score, 4), matched_terms)
            total_links += 1
            mkt = mkt_by_id.get(mid)
            top_matches.append((score, mkt.question if mkt else mid, sig.title))

        signals_processed += 1

    # Summary output
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  Signals processed    : {signals_processed}")
    print(f"  Candidate comparisons: {total_comparisons}")
    print(f"  Links written        : {total_links}")
    print(sep)

    if top_matches:
        top_matches.sort(key=lambda x: x[0], reverse=True)
        show = top_matches[:10]
        print(f"\nTop {len(show)} (market, signal) matches by relevance:")
        print(f"  {'Score':>6}  {'Market question':45}  {'Signal title':45}")
        print("  " + "-" * 100)
        for score, mq, st in show:
            print(f"  {score:6.3f}  {mq[:45]:<45}  {st[:45]:<45}")

    return total_links


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Match recent signals to active prediction markets via keyword/entity "
            "overlap. Writes relevance edges to signal_market_links so the LLM "
            "estimator only runs on markets with fresh, relevant signals."
        )
    )
    parser.add_argument(
        "--hours", type=float, default=48,
        help="Look back this many hours for recent signals (default: 48)",
    )
    parser.add_argument(
        "--min-relevance", type=float, default=0.12, dest="min_relevance",
        help="Minimum relevance score to write a link (default: 0.12)",
    )
    parser.add_argument(
        "--max-links-per-signal", type=int, default=8, dest="max_links",
        help="Maximum number of markets to link per signal (default: 8)",
    )
    parser.add_argument(
        "--tier", type=int, default=None,
        help="Only process signals with this source_tier (default: all tiers)",
    )
    parser.add_argument(
        "--min-shared", type=int, default=MIN_SHARED_TOKENS, dest="min_shared",
        help=f"Minimum shared non-date tokens to write a link (default: {MIN_SHARED_TOKENS})",
    )
    args = parser.parse_args()

    run(
        hours=args.hours,
        min_relevance=args.min_relevance,
        max_links=args.max_links,
        tier=args.tier,
        min_shared=args.min_shared,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
