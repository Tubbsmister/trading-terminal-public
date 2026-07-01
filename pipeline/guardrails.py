"""
Guardrails — the risk layer that stands between "we found an edge" and "we placed
a bet". Because the end state is full-auto execution, this is not optional and it
runs on *every* opportunity before it can be approved.

Two scopes of check:

* per-opportunity (does this single bet clear our quality/safety bars?)
* portfolio    (does adding it keep total/per-market exposure inside limits?)

Plus a global kill-switch: a ``STOP`` file in the repo root (the same convention
the existing money-scripts use) or a tripped daily-loss limit halts everything.

Phase 1 only *records* approve/reject decisions (paper-trade). Phase 2 wires the
same checks in front of live execution unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .models import Opportunity, OppStatus

STOP_FILE = Path(__file__).resolve().parent.parent / "STOP"


@dataclass
class GuardrailConfig:
    # per-opportunity quality bars
    min_edge: float = 0.03              # require >=3% post-cost EV per $1
    min_liquidity: float = 25.0         # USD of backable depth near our price
    min_price: float = 0.03             # avoid sub-3c dust (fees/resolution risk dominate)
    max_price: float = 0.97             # and the mirror image on the expensive side
    min_stake: float = 1.0
    require_open: bool = True           # market must not be resolved/closed

    # portfolio limits (fractions of bankroll)
    max_position_fraction: float = 0.05     # per market
    max_total_exposure_fraction: float = 0.50   # across all open positions

    # circuit breaker
    max_daily_loss_fraction: float = 0.10   # halt new bets if down >10% on the day

    # arb is near-riskless; let it bypass the directional min_edge bar
    arb_min_edge: float = 0.005


@dataclass
class PortfolioState:
    """Snapshot the guardrails need to reason about exposure / drawdown."""
    bankroll: float
    open_exposure: float = 0.0          # USD currently at risk across markets
    open_by_market: dict = field(default_factory=dict)  # market_id -> USD
    realized_pnl_today: float = 0.0


@dataclass
class Verdict:
    approved: bool
    notes: str


def kill_switch_engaged(state: PortfolioState, cfg: GuardrailConfig) -> Optional[str]:
    """Return a reason string if all new betting must halt, else None."""
    if STOP_FILE.exists():
        return "STOP file present"
    if state.bankroll > 0 and state.realized_pnl_today < 0:
        loss_frac = -state.realized_pnl_today / state.bankroll
        if loss_frac >= cfg.max_daily_loss_fraction:
            return (f"daily loss {loss_frac:.1%} >= limit "
                    f"{cfg.max_daily_loss_fraction:.0%}")
    return None


def evaluate(
    opp: Opportunity,
    state: PortfolioState,
    cfg: Optional[GuardrailConfig] = None,
) -> Verdict:
    """Approve or reject a single sized opportunity given current portfolio state.

    Expects ``opp.recommended_stake`` to already be set by the sizer.
    """
    cfg = cfg or GuardrailConfig()
    is_arb = opp.edge_source == "arb"

    halt = kill_switch_engaged(state, cfg)
    if halt:
        return Verdict(False, f"HALT: {halt}")

    # ---- per-opportunity quality bars ----
    min_edge = cfg.arb_min_edge if is_arb else cfg.min_edge
    if opp.edge < min_edge:
        return Verdict(False, f"edge {opp.edge:.3f} < min {min_edge:.3f}")
    if not (cfg.min_price <= opp.market_price <= cfg.max_price):
        return Verdict(False, f"price {opp.market_price:.3f} outside "
                              f"[{cfg.min_price},{cfg.max_price}]")
    if opp.liquidity is not None and opp.liquidity < cfg.min_liquidity:
        return Verdict(False, f"liquidity {opp.liquidity:.0f} < min {cfg.min_liquidity:.0f}")

    stake = opp.recommended_stake or 0.0
    if stake < cfg.min_stake:
        return Verdict(False, f"stake {stake:.2f} < min {cfg.min_stake:.2f}")

    # ---- portfolio limits ----
    if state.bankroll <= 0:
        return Verdict(False, "bankroll <= 0")
    pos_frac = stake / state.bankroll
    if pos_frac > cfg.max_position_fraction + 1e-9:
        return Verdict(False, f"position {pos_frac:.1%} > cap "
                              f"{cfg.max_position_fraction:.0%}")
    existing = state.open_by_market.get(opp.market_id, 0.0)
    if (existing + stake) / state.bankroll > cfg.max_position_fraction + 1e-9:
        return Verdict(False, f"market exposure would exceed "
                              f"{cfg.max_position_fraction:.0%} cap")
    new_total = (state.open_exposure + stake) / state.bankroll
    if new_total > cfg.max_total_exposure_fraction + 1e-9:
        return Verdict(False, f"total exposure {new_total:.0%} > cap "
                              f"{cfg.max_total_exposure_fraction:.0%}")

    return Verdict(True, "ok" + (" (arb)" if is_arb else ""))


def apply(
    opps: List[Opportunity],
    state: PortfolioState,
    cfg: Optional[GuardrailConfig] = None,
) -> Tuple[List[Opportunity], List[Opportunity]]:
    """Run guardrails over a ranked list, mutating status + notes and updating the
    running exposure as bets are approved. Returns (approved, rejected).

    Opportunities are evaluated in the order given (caller should pre-sort by edge
    so the best bets claim the exposure budget first).
    """
    cfg = cfg or GuardrailConfig()
    approved: List[Opportunity] = []
    rejected: List[Opportunity] = []
    for opp in opps:
        v = evaluate(opp, state, cfg)
        opp.guardrail_notes = v.notes
        if v.approved:
            opp.status = OppStatus.APPROVED.value
            stake = opp.recommended_stake or 0.0
            state.open_exposure += stake
            state.open_by_market[opp.market_id] = (
                state.open_by_market.get(opp.market_id, 0.0) + stake
            )
            approved.append(opp)
        else:
            opp.status = OppStatus.REJECTED.value
            rejected.append(opp)
    return approved, rejected
