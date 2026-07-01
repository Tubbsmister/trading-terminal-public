"""
Tests for premium-pass dedup (Cycle 8): a repeated/scheduled premium pass must cover NEW
markets instead of re-grinding the same stable top-K (every premium call counts under
subscription rate limits).

Two pieces:
  - triage(exclude_market_ids=...) drops excluded markets from the pool.
  - recently_premium_markets() returns markets that already got a premium (cli/api) thesis
    within the window — local-only theses do NOT count.

Runnable two ways:
  pytest pipeline/tests/test_premium_dedup.py
  python  pipeline/tests/test_premium_dedup.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline import thesis
from pipeline.models import Market
from pipeline.premium_pass import recently_premium_markets


class FakeStore:
    def __init__(self, markets, signals=None, history=None, db_path=None):
        self._markets = markets
        self._signals = signals or {}
        self._history = history or {}
        self.db_path = db_path

    def get_markets(self, active_only=True):
        return list(self._markets)

    def get_signals_for_market(self, market_id, *, limit=20):
        return list(self._signals.get(market_id, []))[:limit]

    def get_price_history(self, market_id, *, limit=50):
        return list(self._history.get(market_id, []))


def _mkt(mid, q="Will the rule take effect by 2026?", price=0.3, liq=500.0, cat="Politics"):
    return Market(id=mid, venue="poly", venue_market_id=mid, question=q, yes_price=price,
                  no_price=round(1 - price, 4), volume=0.0, volume_24h=0.0, liquidity=liq,
                  category=cat, close_time=None, url="", is_active=True, is_resolved=False,
                  extra={}, collected_at="")


def test_triage_excludes_market_ids():
    a, b = _mkt("a"), _mkt("b")
    store = FakeStore([a, b], signals={"a": [{"source_tier": 1}], "b": [{"source_tier": 1}]})
    full = {m.id for m in thesis.triage(store, max_markets=10)}
    assert {"a", "b"} <= full
    pruned = {m.id for m in thesis.triage(store, max_markets=10, exclude_market_ids={"a"})}
    assert "a" not in pruned and "b" in pruned


def _seed_theses(db_path, rows):
    c = sqlite3.connect(db_path)
    try:
        c.execute("CREATE TABLE theses (market_id TEXT, generated_by TEXT, created_at TEXT)")
        c.executemany("INSERT INTO theses VALUES (?,?,?)", rows)
        c.commit()
    finally:
        c.close()


def test_recently_premium_markets_filters_by_backend_and_age():
    now = datetime.now(timezone.utc)
    recent = now.isoformat()
    old = (now - timedelta(days=10)).isoformat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        db = str(Path(d) / "t.db")
        _seed_theses(db, [
            ("m_premium_recent", "cli:claude-sonnet-4-6", recent),   # premium + recent -> skip
            ("m_premium_old", "cli:claude-sonnet-4-6", old),         # premium but old   -> keep
            ("m_local_recent", "local:claude-sonnet-4-6", recent),   # local             -> keep
            ("m_api_recent", "api:claude-sonnet-4-6", recent),       # api premium       -> skip
        ])
        store = FakeStore([], db_path=db)
        got = recently_premium_markets(store, days=3)
    assert got == {"m_premium_recent", "m_api_recent"}


def test_reeval_days_zero_returns_empty():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        db = str(Path(d) / "t.db")
        _seed_theses(db, [("m", "cli:x", datetime.now(timezone.utc).isoformat())])
        assert recently_premium_markets(FakeStore([], db_path=db), days=0) == set()


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  ok   {fn.__name__}")
        except Exception:
            failed += 1; print(f"  FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
