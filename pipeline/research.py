"""
Research layer — the local model's "senses".

A local LLM has no built-in web access, so the pipeline gathers the evidence and
hands it over. For a target market we assemble:

  * the fresh signals already linked to it (the RSS/official firehose), and
  * live web-search hits (DuckDuckGo, keyless) for the market's specific question.

The model is the reasoning brain; this module is what it reads. Keeping research
here (not inside the model) makes every thesis *auditable* — you can see exactly
which facts produced a bet. Everything is best-effort: a failed search degrades to
"less evidence", never a crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .models import Market
from .store import Store


@dataclass
class Evidence:
    signals: List[dict] = field(default_factory=list)     # linked store signals
    web: List[dict] = field(default_factory=list)         # [{title, body, url}]
    queries: List[str] = field(default_factory=list)

    @property
    def is_thin(self) -> bool:
        return not self.signals and not self.web

    def as_text(self, max_items: int = 12) -> str:
        """Format the evidence into a prompt block the model can reason over."""
        lines: List[str] = []
        if self.signals:
            lines.append("LINKED SIGNALS (from monitored feeds):")
            for s in self.signals[:max_items]:
                date = s.get("published_at") or s.get("collected_at") or "?"
                lines.append(f"  - ({date}) [{s.get('source')}] {s.get('title')}"
                             + (f" :: {s['body'][:200]}" if s.get("body") else ""))
        if self.web:
            lines.append("\nWEB SEARCH RESULTS:")
            for w in self.web[:max_items]:
                lines.append(f"  - {w.get('title')}: {w.get('body','')[:220]}"
                             f"  <{w.get('url','')}>")
        return "\n".join(lines) if lines else "(no evidence found)"


def web_search(query: str, max_results: int = 5) -> List[dict]:
    """DuckDuckGo text search (no API key). Returns [] on any failure."""
    try:
        from ddgs import DDGS
        # timeout so a hung search can never stall the 24/7 engine
        with DDGS(timeout=10) as ddgs:
            hits = ddgs.text(query, max_results=max_results)
        return [{"title": h.get("title", ""), "body": h.get("body", ""),
                 "url": h.get("href", "")} for h in (hits or [])]
    except Exception as exc:  # noqa: BLE001 — search is best-effort
        print(f"    (web_search failed for {query!r}: {exc})")
        return []


_STOP = {"will", "the", "be", "by", "on", "in", "of", "to", "a", "an", "2026", "2027"}


def _search_queries(market: Market) -> List[str]:
    """Build a couple of focused search queries from the market question."""
    q = market.question.strip().rstrip("?")
    queries = [q]
    # a second query keyed on the salient nouns, to catch fresher coverage
    words = [w for w in re.findall(r"[A-Za-z0-9']+", q) if w.lower() not in _STOP]
    salient = [w for w in words if w[0].isupper()] or words
    if salient:
        queries.append(" ".join(salient[:6]) + " news latest")
    return queries[:2]


def gather_evidence(
    market: Market, store: Store, *, max_web: int = 5, do_web: bool = True,
) -> Evidence:
    """Collect signals + web hits for a market into one auditable bundle."""
    signals = store.get_signals_for_market(market.id, limit=10)
    web: List[dict] = []
    queries: List[str] = []
    if do_web:
        seen = set()
        for query in _search_queries(market):
            queries.append(query)
            for hit in web_search(query, max_results=max_web):
                key = hit.get("url") or hit.get("title")
                if key and key not in seen:
                    seen.add(key)
                    web.append(hit)
    return Evidence(signals=signals, web=web, queries=queries)
