"""
Position sizing — fractional Kelly for binary prediction-market contracts.

You always *buy a side* (YES or NO) of a binary market at a cost ``price`` per
contract (price in [0,1] = the probability the market assigns). The contract
pays $1 if that side wins. Given our estimated win probability ``q`` for that
side, the growth-optimal (full-Kelly) fraction of bankroll to stake is

    f* = (q - price) / (1 - price)

Derivation: a contract bought at ``p`` is a bet at net odds b = (1-p)/p with win
prob q. Kelly f* = (q·b - (1-q)) / b, which simplifies to (q - p)/(1 - p).

Full Kelly is growth-optimal but brutal under estimation error: if our ``q`` is
even slightly wrong (and with LLM/curve-estimated probabilities it will be), full
Kelly massively over-bets and invites ruinous drawdowns. So we stake a *fraction*
of full Kelly — quarter-Kelly by default — which keeps ~95% of the long-run growth
rate with a fraction of the variance. We also hard-cap the per-bet fraction and
enforce a minimum stake (sub-$1 bets aren't worth the fees/effort).

Expected value, for reporting, is per $1 staked: ev = q/price - 1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def full_kelly_fraction(price: float, fair_prob: float) -> float:
    """Growth-optimal bankroll fraction to back a side costing ``price`` whose
    true win probability we estimate at ``fair_prob``. Zero if no edge.
    """
    if not (0.0 < price < 1.0):
        return 0.0
    f = (fair_prob - price) / (1.0 - price)
    return max(0.0, f)


def ev_per_dollar(price: float, fair_prob: float) -> float:
    """Expected profit per $1 staked, before fees (q/price - 1)."""
    if price <= 0.0:
        return 0.0
    return fair_prob / price - 1.0


@dataclass
class SizeResult:
    stake: float            # USD to risk
    fraction: float         # fraction of bankroll (after multiplier + caps)
    full_fraction: float    # raw full-Kelly fraction, pre-scaling
    contracts: float        # stake / price
    ev_per_dollar: float    # expected profit per $1 staked (pre-fee)
    capped_by: Optional[str] = None  # which limit bound the size, if any


@dataclass
class KellySizer:
    """Turns an (estimated prob, price) into a concrete stake.

    ``multiplier`` is the Kelly fraction (0.25 = quarter-Kelly, the default and
    recommended setting for real money). ``max_fraction`` caps any single bet's
    share of bankroll regardless of what Kelly says. ``min_stake`` drops bets too
    small to bother with.
    """

    bankroll: float
    multiplier: float = 0.25
    max_fraction: float = 0.05      # never risk >5% of bankroll on one market
    min_stake: float = 1.0

    @classmethod
    def from_env(cls, bankroll_env: str = "POLY_BANKROLL", default: float = 1000.0,
                 **kw) -> "KellySizer":
        bankroll = float(os.environ.get(bankroll_env, default))
        return cls(bankroll=bankroll, **kw)

    def size(self, price: float, fair_prob: float) -> SizeResult:
        full = full_kelly_fraction(price, fair_prob)
        frac = full * self.multiplier
        capped_by = None
        if frac > self.max_fraction:
            frac = self.max_fraction
            capped_by = "max_fraction"
        stake = frac * self.bankroll
        if 0.0 < stake < self.min_stake:
            # round up to the floor only if there's genuinely an edge to bet
            stake = self.min_stake
            frac = stake / self.bankroll if self.bankroll else 0.0
            capped_by = "min_stake"
        contracts = stake / price if price > 0 else 0.0
        return SizeResult(
            stake=round(stake, 2),
            fraction=round(frac, 6),
            full_fraction=round(full, 6),
            contracts=round(contracts, 2),
            ev_per_dollar=round(ev_per_dollar(price, fair_prob), 6),
            capped_by=capped_by,
        )


if __name__ == "__main__":
    # quick self-test / sanity table
    s = KellySizer(bankroll=1000.0, multiplier=0.25)
    print(f"bankroll ${s.bankroll:.0f}  quarter-Kelly  cap {s.max_fraction:.0%}/market\n")
    print(f"{'price':>6} {'fair':>6} {'fullK':>8} {'frac':>8} {'stake':>8} {'EV/$':>7} {'cap':>12}")
    for price, fair in [(0.50, 0.60), (0.10, 0.15), (0.90, 0.95),
                        (0.05, 0.08), (0.50, 0.50), (0.50, 0.45), (0.30, 0.55)]:
        r = s.size(price, fair)
        print(f"{price:6.2f} {fair:6.2f} {r.full_fraction:8.4f} {r.fraction:8.4f} "
              f"{r.stake:8.2f} {r.ev_per_dollar:+7.3f} {str(r.capped_by or ''):>12}")
