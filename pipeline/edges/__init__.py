"""
Edge estimators — produce ``Opportunity`` candidates from collected markets.

Each estimator answers the pipeline's one question in its own way: *is the true
probability different enough from the price to be worth money?*

Phase 1 (free, no LLM):
  * ``arb``         — within-market riskless arb + cross-venue review candidates

Phase 3 adds LLM estimators (news/official-data synthesis) behind the same
interface: market(s) in, ``Opportunity`` candidates out. The legacy longshot
``calibration`` estimator was removed (it lost ~77% in paper/live — see IMPROVEMENTS.md);
the engine is now purely thesis + arb.
"""

from __future__ import annotations

from . import arb

__all__ = ["arb"]
