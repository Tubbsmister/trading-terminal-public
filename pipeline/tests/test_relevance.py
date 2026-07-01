"""
Tests for the relevance precision gate (Cycle 6).

Invariant: a signal links to a market only with genuine topical corroboration — at least
two shared NON-date tokens. Calendar coincidence (shared years/months) and single-generic-
word overlap ("act" between "Sunshine Act" and "CLARITY Act") must NOT create a link, since
that spurious evidence is what the thesis generator hallucinates from.

Runnable two ways:
  pytest pipeline/tests/test_relevance.py
  python  pipeline/tests/test_relevance.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline.signals import relevance as rel


def _shared(sig_text, mkt_text):
    st, sc = rel._tokens(sig_text), rel._cap_tokens(sig_text)
    mt, mc = rel._tokens(mkt_text), rel._cap_tokens(mkt_text)
    score, shared = rel._score_pair(st, sc, mt, mc)
    return score, shared


def test_dates_are_not_tokens():
    toks = rel._tokens("Committee markup on July 15, 2026 and June 30")
    assert "2026" not in toks and "july" not in toks and "june" not in toks and "15" not in toks
    assert "committee" in toks and "markup" in toks


def test_single_generic_token_does_not_link():
    """'Sunshine Act' vs 'CLARITY Act' share only 'act' — must NOT qualify (the bug)."""
    score, shared = _shared("FDIC Sunshine Act Meetings Notice",
                            "CLARITY Act signed into law in 2026?")
    assert not rel.link_qualifies(score, shared, 0.12), \
        f"single-token 'act' overlap should not link; shared={shared}"


def test_date_only_overlap_does_not_link():
    """'Lebanon by June 30' vs 'Johannesburg violence June 30' share only date tokens."""
    score, shared = _shared("Fear grows in Johannesburg market violence June 30",
                            "Will Lebanon recognize Israel by June 30?")
    assert not rel.link_qualifies(score, shared, 0.12), \
        f"date-only overlap should not link; shared={shared}"


def test_two_distinctive_tokens_link():
    """'Putin ... president' shares two topical tokens with the Putin market — links."""
    score, shared = _shared("Putin makes rare admission, Russian president under pressure",
                            "Putin out as President of Russia by December 31, 2026?")
    assert len(shared) >= 2
    assert rel.link_qualifies(score, shared, 0.12), f"shared={shared} score={score}"


def test_lebanon_israel_links():
    score, shared = _shared("Lebanon Latest: Israel strikes as Hezbollah responds",
                            "Will Lebanon recognize Israel by June 30?")
    assert rel.link_qualifies(score, shared, 0.12), f"shared={shared}"


def test_min_shared_is_configurable():
    score, shared = _shared("Israel launches new assault in southern region",
                            "Will Lebanon recognize Israel by June 30?")
    # single shared token 'israel' -> blocked at default 2, allowed at min_shared=1
    assert not rel.link_qualifies(score, shared, 0.12, min_shared=2)
    assert rel.link_qualifies(score, shared, 0.12, min_shared=1)


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
