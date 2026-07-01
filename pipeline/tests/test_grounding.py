"""
Tests for the deterministic fact-grounding gate (Cycle 5).

Invariant: a thesis whose CHECKABLE key_facts assert a specific date / $ amount / % that
does NOT appear in the evidence corpus (evidence + market question + rules) is flagged
ungrounded — that is the generator hallucinating a fabricated constraint. A thesis whose
specific facts DO appear in the evidence passes, including dates written in a different
format ("July 15" vs "15 July").

Runnable two ways:
  pytest pipeline/tests/test_grounding.py
  python  pipeline/tests/test_grounding.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline import thesis
from pipeline.models import Market, Thesis
from pipeline.research import Evidence


def _mkt(q="Will the rule take effect?", rules=None):
    extra = {"rules_primary": rules} if rules else {}
    return Market(
        id="m", venue="poly", venue_market_id="0x", question=q,
        yes_price=0.3, no_price=0.7, volume=0.0, volume_24h=0.0, liquidity=500.0,
        category=None, close_time=None, url="", is_active=True, is_resolved=False,
        extra=extra, collected_at="",
    )


def _evidence(*titles):
    return Evidence(signals=[{"source": "feed", "title": t, "published_at": "2026-06-29"}
                             for t in titles])


def _thesis(*facts):
    return Thesis(
        market_id="m", venue="poly", question="q", side="YES", claim="c",
        fair_prob=0.5, confidence=0.5, causal_chain=[],
        key_facts=[{"fact": f, "source": "s", "tier": 2, "checkable": True} for f in facts],
    )


def test_fabricated_date_is_flagged():
    ev = _evidence("FDIC Sunshine Act Meetings", "Education Dept FLAS survey")
    th = _thesis("Senate Banking Committee scheduled markup on July 15, 2026")
    bad = thesis.check_grounding(th, _mkt(), ev)
    assert bad, "a fabricated date absent from evidence must be flagged"


def test_grounded_date_passes():
    ev = _evidence("Senate sets CLARITY Act markup for July 15, 2026 in committee")
    th = _thesis("Senate Banking Committee scheduled markup on July 15, 2026")
    assert thesis.check_grounding(th, _mkt(), ev) == []


def test_date_format_difference_still_grounded():
    """'July 15' in the fact vs '15 July' in evidence — month+day fallback should match."""
    ev = _evidence("Committee markup happens 15 July 2026 per the schedule")
    th = _thesis("markup on July 15, 2026")
    assert thesis.check_grounding(th, _mkt(), ev) == []


def test_date_from_market_question_is_grounded():
    """A date that lives in the market question/rules (not a feed) is legit context."""
    ev = _evidence("Some loosely related headline")
    th = _thesis("Market resolves on signing by Dec 31, 2026")
    mkt = _mkt(q="Will the CLARITY Act be signed by Dec 31, 2026?")
    assert thesis.check_grounding(th, mkt, ev) == []


def test_fabricated_dollar_amount_flagged():
    ev = _evidence("Company reports quarterly earnings beat")
    th = _thesis("The deal is valued at $4.2B according to the filing")
    assert thesis.check_grounding(th, _mkt(), ev)


def test_non_checkable_fact_is_skipped():
    ev = _evidence("nothing relevant")
    th = Thesis(market_id="m", venue="poly", question="q", side="YES", claim="c",
                fair_prob=0.5, confidence=0.5, causal_chain=[],
                key_facts=[{"fact": "Pundits expect movement around July 15, 2026",
                            "source": "s", "tier": 3, "checkable": False}])
    assert thesis.check_grounding(th, _mkt(), ev) == []


def test_thesis_with_no_specific_tokens_passes():
    ev = _evidence("general background")
    th = _thesis("The incumbent is widely expected to remain in power")
    assert thesis.check_grounding(th, _mkt(), ev) == []


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
