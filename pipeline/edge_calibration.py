"""
Deterministic edge calibration — the "math floor" the estimators cannot overrule.

Two corrections, both pure arithmetic, applied to a batch of candidate
Opportunities *after* the estimators produce them and *before* the sizer turns
them into stakes. Neither correction can ever INCREASE a bet's edge or size — by
construction they only ever move an estimate toward the market price (less
conviction) or shave the edge down. The model proposes; this floor disposes.

Borrowed from forven's two disciplines (see forven/_analysis/):

  1. MATH-FLOOR-OVER-LLM (forven hypothesis_verdict.py:535).  An LLM/curve gives
     us a fair probability and a self-reported confidence, then bets get sized on
     that number. A hallucinating estimator can assert fair=0.95 on a 0.30 market
     and manufacture a huge edge. The floor: shrink the estimated probability
     *toward the market price* (the crowd's prior) by a deterministic TRUST factor
     that the estimator does not control. Trust is capped per source — the LLM can
     report confidence 1.0 and still only earn the thesis ceiling. The estimate is
     an upper bound on conviction; we bet a deterministic fraction of it.

         shrunk_fair = price + trust * (fair - price)          # trust in [0,1]

     trust = SOURCE_TRUST[source] * confidence. Since a candidate always has
     fair > price (positive EV), shrinking toward price only ever lowers the edge.

  2. BEST-OF-N DEFLATION (forven Deflated Sharpe, strategies/backtest.py).  We scan
     thousands of markets and bet the most-mispriced. The maximum of N noisy edge
     estimates is inflated by selection alone: even with zero true edge, the best
     of N looks good. The expected maximum of N iid noise terms with scale sigma is
     ~ sigma * sqrt(2 ln N), so we subtract that selection floor from every edge:

         deflated_edge = edge - SCAN_NOISE[source] * K * sqrt(2 ln N)

     The more markets we sift to find a bet, the higher the bar it must clear.

Arb is exempt from both: its edge is mechanical (a riskless price relationship),
not an estimate, so there is no overconfidence or selection bias to correct.

Escape hatch: set PIPELINE_EDGE_CALIBRATION=0 to disable entirely (pass-through),
since this sits in the path of real-money sizing.
"""

from __future__ import annotations

import math
import os
from typing import Iterable, List, Optional

from .models import Opportunity

# ---- per-source knobs (env-overridable for tuning without code edits) --------

# Trust ceiling per estimator: the most of a claimed edge we'll ever act on,
# before multiplying by the estimator's own confidence. Conservative on the
# estimators most prone to hallucination (llm/thesis), generous on the
# statistically-grounded curve, and 1.0 (untouched) on mechanical arb.
SOURCE_TRUST = {
    "arb": 1.0,
    "calibration": 0.75,
    "model": 0.70,
    "consensus": 0.70,
    "thesis": 0.60,
    "llm": 0.60,
}
DEFAULT_TRUST = 0.60

# Estimation-noise scale (in EV/$ units) used for the best-of-N selection haircut.
# Larger = noisier estimator = bigger selection-bias discount.
SCAN_NOISE = {
    "arb": 0.0,
    "calibration": 0.03,
    "model": 0.04,
    "consensus": 0.04,
    "thesis": 0.05,
    "llm": 0.05,
}
DEFAULT_NOISE = 0.04

# Global multiplier on the selection haircut. 1.0 = the textbook sqrt(2 ln N) term.
DEFLATE_K = float(os.environ.get("PIPELINE_DEFLATE_K", "1.0"))

# Confidence assumed when an estimator doesn't report one. Deliberately low — an
# estimator that won't state its confidence doesn't earn full trust.
DEFAULT_CONFIDENCE = 0.50


def enabled() -> bool:
    """Whether calibration is active. Default ON; PIPELINE_EDGE_CALIBRATION=0 disables."""
    return os.environ.get("PIPELINE_EDGE_CALIBRATION", "1").strip() not in ("0", "false", "False", "")


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def effective_trust(source: Optional[str], confidence: Optional[float]) -> float:
    """Deterministic trust in [0,1] = per-source ceiling * the estimator's confidence.

    The estimator controls only its confidence; the ceiling caps how much that
    confidence can ever buy, so a model asserting confidence 1.0 still can't get
    full credit on a hallucination-prone source.
    """
    ceiling = SOURCE_TRUST.get(source or "", DEFAULT_TRUST)
    conf = confidence if isinstance(confidence, (int, float)) else DEFAULT_CONFIDENCE
    return _clamp01(ceiling * _clamp01(float(conf)))


def shrink_fair(price: float, fair: float, trust: float) -> float:
    """Move the estimated fair probability toward the market price by (1-trust)."""
    return _clamp01(price + trust * (fair - price))


def selection_haircut(source: Optional[str], n_candidates: int) -> float:
    """Best-of-N selection-bias floor to subtract from an edge (EV/$).

    Zero for a single candidate (no selection happened) and for mechanical arb.
    """
    if n_candidates is None or n_candidates <= 1:
        return 0.0
    noise = SCAN_NOISE.get(source or "", DEFAULT_NOISE)
    if noise <= 0.0:
        return 0.0
    return noise * DEFLATE_K * math.sqrt(2.0 * math.log(n_candidates))


def calibrate_one(opp: Opportunity, n_candidates: int) -> Opportunity:
    """Apply the math floor + selection deflation to ONE opportunity, in place.

    Mutates ``fair_prob`` (shrunk toward price) and ``edge`` (shrink drop +
    selection haircut subtracted), recording the raw values and every term under
    ``detail['calibration']`` for audit. Arb is passed through untouched.
    """
    source = opp.edge_source or ""
    if source == "arb":
        return opp  # mechanical edge — nothing to correct

    price = opp.market_price
    raw_fair = opp.fair_prob
    raw_edge = opp.edge

    trust = effective_trust(source, opp.confidence)
    eff_fair = shrink_fair(price, raw_fair, trust)

    # EV/$ drop from shrinking the probability toward price:
    #   ev(price,fair) - ev(price,eff_fair) = (fair - eff_fair)/price
    # This preserves whatever cost buffer the estimator already baked into edge.
    shrink_drop = ((raw_fair - eff_fair) / price) if price > 0 else 0.0
    haircut = selection_haircut(source, n_candidates)
    eff_edge = raw_edge - shrink_drop - haircut

    opp.fair_prob = round(eff_fair, 6)
    opp.edge = round(eff_edge, 6)
    opp.detail = {
        **(opp.detail or {}),
        "calibration": {
            "raw_fair_prob": round(raw_fair, 6),
            "raw_edge": round(raw_edge, 6),
            "trust": round(trust, 4),
            "shrink_drop": round(shrink_drop, 6),
            "selection_haircut": round(haircut, 6),
            "n_candidates": int(n_candidates),
        },
    }
    return opp


def calibrate(opps: List[Opportunity]) -> List[Opportunity]:
    """Apply the math floor + best-of-N deflation across a batch of candidates.

    N for the selection haircut is the number of NON-arb candidates in the batch
    (the pool we're selecting the best edges from). Returns the same list (mutated
    in place) so callers can keep their reference. No-op when disabled.
    """
    if not opps or not enabled():
        return opps
    n = sum(1 for o in opps if (o.edge_source or "") != "arb")
    for o in opps:
        calibrate_one(o, n)
    return opps


__all__ = [
    "calibrate", "calibrate_one", "effective_trust", "shrink_fair",
    "selection_haircut", "enabled",
]
