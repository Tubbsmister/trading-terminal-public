"""
Tests for pipeline.thesis triage — the tier-weighted, constraint-aware market picker.

The invariant under test: a constraint-shaped market grounded in a high-tier official
signal must outrank a game-outcome (sports) market flooded with cheap Tier-2 news,
because the constraint-thesis generator can only find edges in the former. This is the
fix for the diagnosed "0 theses every cycle" failure (triage fed the generator only
World Cup markets it correctly rejected).

Runnable two ways:
  pytest pipeline/tests/test_triage.py
  python  pipeline/tests/test_triage.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline import thesis
from pipeline.models import Market


class FakeStore:
    """Minimal store stub: serves markets, per-market signals, and price history."""

    def __init__(self, markets, signals=None, history=None):
        self._markets = markets
        self._signals = signals or {}      # market_id -> list[signal dict]
        self._history = history or {}      # market_id -> list[{yes_price}]

    def get_markets(self, active_only=True):
        return list(self._markets)

    def get_signals_for_market(self, market_id, *, limit=20):
        return list(self._signals.get(market_id, []))[:limit]

    def get_price_history(self, market_id, *, limit=50):
        return list(self._history.get(market_id, []))[:limit]


def _mkt(mid, q, *, category=None, price=0.30, liq=500.0):
    return Market(
        id=mid, venue="poly", venue_market_id=mid, question=q,
        yes_price=price, no_price=round(1 - price, 4), volume=0.0, volume_24h=0.0,
        liquidity=liq, category=category, close_time=None, url="",
        is_active=True, is_resolved=False, extra={}, collected_at="",
    )


def _sig(tier):
    return {"source_tier": tier, "source": f"t{tier}", "title": "x"}


def test_tier1_constraint_market_outranks_sports_flood():
    """One Tier-1 official signal on an Economics market beats six Tier-2 signals on a
    World Cup market — the tier weighting + category affinity working together."""
    econ = _mkt("econ", "Will the CFPB rule take effect before Q3?", category="Economics")
    cup = _mkt("cup", "Will France win the 2026 FIFA World Cup?", category=None)
    store = FakeStore(
        markets=[cup, econ],
        signals={"econ": [_sig(1)], "cup": [_sig(2)] * 6},
    )
    triaged = thesis.triage(store, max_markets=10)
    assert triaged[0].id == "econ", \
        f"expected constraint market first, got {[m.id for m in triaged]}"


def test_uncategorised_sports_is_demoted_by_keyword():
    """A category=None Poly market that is plainly a game-outcome bet gets the penalty."""
    cup = _mkt("cup", "Will France win the 2026 FIFA World Cup?", category=None)
    assert thesis._category_multiplier(cup) == thesis._GAME_OUTCOME_PENALTY
    game = _mkt("g", "Will Sweden win on 2026-06-30?", category=None)
    assert thesis._category_multiplier(game) == thesis._GAME_OUTCOME_PENALTY


def test_explicit_sports_category_demoted_constraint_category_lifted():
    sports = _mkt("s", "Some match", category="Sports")
    elections = _mkt("e", "Will X win the primary?", category="Elections")
    assert thesis._category_multiplier(sports) < 1.0 < thesis._category_multiplier(elections)


def test_election_market_not_falsely_penalised_by_keyword():
    """'win the ... election' must NOT trip the game-outcome regex (no sports noun)."""
    m = _mkt("p", "Will Trump win the 2024 election?", category=None)
    assert thesis._category_multiplier(m) == 1.0


def test_tier_weight_monotonic():
    """Higher-tier official signals carry more triage weight than news."""
    assert thesis._signal_tier_weight([_sig(1)]) > thesis._signal_tier_weight([_sig(2)])
    assert thesis._signal_tier_weight([_sig(2)]) > thesis._signal_tier_weight([_sig(3)])


def test_illiquid_or_extreme_market_skipped():
    illiquid = _mkt("i", "q", category="Politics", liq=1.0)
    extreme = _mkt("x", "q", category="Politics", price=0.99)
    store = FakeStore(markets=[illiquid, extreme], signals={"i": [_sig(1)], "x": [_sig(1)]})
    assert thesis.score_market(store, illiquid) is None
    assert thesis.score_market(store, extreme) is None


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
