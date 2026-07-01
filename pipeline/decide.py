"""
Decision layer (Opus) — the final gate before a bet is real.

This is the top of the model-routing pyramid. Cheap filters and Sonnet have
already done the bulk work and produced a short list of sized, guardrail-passed
opportunities. Opus never reads the raw firehose; it reads the *synopsis* of each
candidate — our estimated probability, the rationale, the decisive signals, the
price, the proposed stake — and makes the judgment call a human pro would make:

    approve / veto, and at what fraction of the proposed size, and what's the
    single biggest risk that could make this bet wrong.

It can only ever REDUCE risk: it vetoes or downsizes, never upsizes past what the
sizer + guardrails already allowed. No ANTHROPIC_API_KEY -> pass-through (the bets
that cleared guardrails stand), clearly labeled, so the pipeline still runs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv

from . import llm_backend
from .models import Opportunity, OppStatus
from .store import Store, get_store

load_dotenv()

MODEL_OPUS = "claude-opus-4-8"

_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "size_multiplier": {"type": "number",
                            "description": "0.0 (veto) to 1.0 (full proposed size). Scale down if the edge is shaky."},
        "key_risk": {"type": "string", "description": "the single biggest reason this bet could be wrong, <=160 chars"},
        "reasoning": {"type": "string", "description": "one sentence justifying the decision"},
    },
    "required": ["approve", "size_multiplier", "key_risk", "reasoning"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the final risk officer for an automated prediction-market trader. "
    "Lower layers have already estimated a probability and proposed a position "
    "size that passed mechanical guardrails. Your job is judgment: approve, veto, "
    "or downsize each proposed bet. You can only REDUCE size (0.0 vetoes, 1.0 keeps "
    "the full proposed stake) — never increase it.\n\n"
    "Veto or heavily downsize when: the rationale is thin or circular; the 'edge' "
    "looks like a model artifact rather than real mispricing; the information is "
    "plausibly already priced in; the resolution criteria are ambiguous; or the "
    "evidence doesn't actually pin down THIS market's outcome. Keep full size only "
    "for bets with a clear, specific, evidence-backed reason the market is wrong. "
    "When in doubt, downsize. A skipped bet costs nothing; a confidently wrong "
    "bet at full size is how the bankroll dies."
)


@dataclass
class Decision:
    approve: bool
    size_multiplier: float
    key_risk: str
    reasoning: str


def _synopsis(opp: Opportunity) -> str:
    d = opp.detail or {}
    parts = [
        f"MARKET: {opp.question}",
        f"Venue: {opp.venue} | side: {opp.side} | price: {opp.market_price:.3f}",
        f"Our estimated fair P({opp.side}): {opp.fair_prob:.3f}  "
        f"(edge {opp.edge:+.3f} EV/$, confidence {opp.confidence})",
        f"Estimator: {opp.edge_source}",
        f"Proposed stake: ${opp.recommended_stake:.2f}" if opp.recommended_stake else "",
    ]
    if d.get("rationale"):
        parts.append(f"Rationale: {d['rationale']}")
    if d.get("decisive_signals"):
        parts.append("Decisive signals: " + "; ".join(d["decisive_signals"][:4]))
    if d.get("note"):
        parts.append(f"Note: {d['note']}")
    return "\n".join(p for p in parts if p)


def decide_one(opp: Opportunity) -> Optional[Decision]:
    """Ask Opus to rule on one proposed bet (API or CLI backend). None if unavailable."""
    user = _synopsis(opp) + "\n\nApprove, veto, or downsize this bet."
    dd = llm_backend.complete_json(
        MODEL_OPUS, _SYSTEM, user, _SCHEMA, max_tokens=800,
        required=["approve", "size_multiplier", "key_risk", "reasoning"],
    )
    if dd is None:
        return None
    try:
        return Decision(
            approve=bool(dd["approve"]),
            size_multiplier=max(0.0, min(1.0, float(dd["size_multiplier"]))),
            key_risk=str(dd.get("key_risk", ""))[:160],
            reasoning=str(dd.get("reasoning", ""))[:240],
        )
    except (ValueError, TypeError):
        return None


def review(opps: List[Opportunity], *, verbose: bool = True,
           max_review: int = 15) -> List[Opportunity]:
    """Run the Opus gate over guardrail-approved opportunities.

    Mutates each opp: applies the size multiplier to recommended_stake, records the
    decision in detail/guardrail_notes, and sets status. Returns the survivors
    (approved, possibly downsized). No backend -> pass-through unchanged.

    Cost control: only the ``max_review`` highest-stake bets get an Opus call
    (the biggest risks deserve the scrutiny); smaller bets that already cleared
    guardrails pass through. Prevents an Opus call per weak longshot.
    """
    if not llm_backend.available():
        if verbose:
            print(f"  (Opus decision layer skipped: {llm_backend.describe()} — "
                  "guardrail-approved bets pass through unreviewed)")
        return opps

    ranked = sorted(opps, key=lambda o: o.recommended_stake or 0, reverse=True)
    to_review = set(id(o) for o in ranked[:max_review])
    survivors: List[Opportunity] = []
    for opp in opps:
        if id(opp) not in to_review:
            survivors.append(opp)  # below the review cutoff — passes on guardrails
            continue
        dec = decide_one(opp)
        if dec is None:
            # Backend IS available (checked above) but this call failed mid-run.
            # Fail CLOSED: a bet that should have been Opus-reviewed but wasn't does
            # not go through. (Distinct from the no-backend pass-through above.)
            opp.status = OppStatus.REJECTED.value
            opp.guardrail_notes = "opus review FAILED mid-run — rejected (fail-closed)"
            continue
        opp.detail = {**(opp.detail or {}), "opus": {
            "approve": dec.approve, "size_multiplier": dec.size_multiplier,
            "key_risk": dec.key_risk, "reasoning": dec.reasoning}}
        if dec.approve and dec.size_multiplier > 0:
            if opp.recommended_stake:
                opp.recommended_stake = round(opp.recommended_stake * dec.size_multiplier, 2)
            opp.guardrail_notes = f"opus ok x{dec.size_multiplier:.2f}: {dec.reasoning}"
            survivors.append(opp)
        else:
            opp.status = OppStatus.REJECTED.value
            opp.guardrail_notes = f"opus VETO: {dec.reasoning}"
    if verbose:
        print(f"  Opus reviewed {len(opps)} -> {len(survivors)} survive")
    return survivors


if __name__ == "__main__":
    # Dry-run the synopsis builder on current approved opportunities (no API call).
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    store = get_store()
    approved = store.get_opportunities(status=OppStatus.APPROVED.value, limit=3)
    print(f"Synopsis preview for {len(approved)} approved opps "
          f"(no API call; key {'set' if os.getenv('ANTHROPIC_API_KEY') else 'NOT set'}):\n")
    for o in approved:
        print(_synopsis(o))
        print("-" * 60)
