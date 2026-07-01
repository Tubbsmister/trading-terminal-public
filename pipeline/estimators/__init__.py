"""
LLM probability estimators — the Phase 3 "information edge".

Where the Phase 1 estimators (calibration, arb) use only prices, these read the
fresh *information* linked to a market and estimate the true probability from it —
catching public data the market hasn't digested yet. Model routing keeps it
affordable: a cheap pre-filter selects the few markets worth analyzing, Sonnet
does the per-market estimate, and Opus (in pipeline/decide.py) makes the final
call on the handful that show an edge.
"""

from __future__ import annotations

from . import llm

__all__ = ["llm"]
