"""
Core data models shared across the whole pipeline.

Two abstractions carry everything:

* ``Market``  — one tradable binary contract, normalized across venues. A
  Polymarket condition, a Kalshi market, and a Manifold market all collapse to
  the same shape: a YES price in [0, 1], volume, liquidity, a resolution
  deadline, and a stable cross-venue ``id``.

* ``Opportunity`` — a *bet we might place*: a (market, side) pair where our
  estimated fair probability differs from the price enough to be worth money,
  once we account for fees, liquidity, and sizing. Phase 0 only defines and
  persists the schema; the edge/estimator/sizing phases populate it.

Everything is plain dataclasses + a couple of enums so the storage layer can
round-trip them through SQLite without an ORM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Optional


# ───────────────────────── venues ─────────────────────────

class Venue(str, Enum):
    POLYMARKET = "poly"
    KALSHI = "kalshi"
    MANIFOLD = "manifold"


def make_market_id(venue: Venue, venue_market_id: str) -> str:
    """Stable, globally-unique market id, e.g. ``poly:0xabc...`` / ``kalshi:FED-25``."""
    return f"{venue.value}:{venue_market_id}"


# ───────────────────────── market ─────────────────────────

@dataclass
class Market:
    """A binary contract normalized across venues.

    ``yes_price`` is the price (in probability units, [0, 1]) to back YES.
    For venues that only quote a single probability (Manifold), ``no_price``
    is ``1 - yes_price``. For order-book venues (Polymarket, Kalshi) we store
    the best YES ask as ``yes_price`` and best NO ask as ``no_price`` when
    available, so YES + NO can exceed 1 by the spread — that gap is what the
    cross-venue/arb logic later keys on.
    """

    id: str                       # make_market_id(venue, venue_market_id)
    venue: str                    # Venue value
    venue_market_id: str          # raw id within the venue (condition id / ticker / id)
    question: str

    yes_price: Optional[float] = None     # [0, 1]
    no_price: Optional[float] = None      # [0, 1]
    volume: Optional[float] = None        # venue's notion of volume (USD or mana)
    volume_24h: Optional[float] = None
    liquidity: Optional[float] = None

    category: Optional[str] = None
    close_time: Optional[str] = None      # ISO-8601 UTC resolution deadline
    url: Optional[str] = None

    is_active: bool = True
    is_resolved: bool = False

    # Free-form venue-specific payload we may need later (token ids, neg_risk,
    # series tickers, raw outcome arrays). Stored as JSON; never queried on.
    extra: Dict[str, Any] = field(default_factory=dict)

    collected_at: Optional[str] = None    # ISO-8601 UTC; set by the store on write

    @property
    def venue_enum(self) -> Venue:
        return Venue(self.venue)

    def days_to_close(self, now_iso: str) -> Optional[float]:
        """Days from ``now_iso`` to ``close_time`` (None if either is missing)."""
        if not self.close_time:
            return None
        try:
            from datetime import datetime
            close = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            return max(0.0, (close - now).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            return None

    # round-trip helpers for the store ----------------------------------

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["extra"] = json.dumps(d.get("extra") or {})
        d["is_active"] = int(self.is_active)
        d["is_resolved"] = int(self.is_resolved)
        return d

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Market":
        data = dict(row)
        raw_extra = data.get("extra")
        data["extra"] = json.loads(raw_extra) if raw_extra else {}
        data["is_active"] = bool(data.get("is_active", 1))
        data["is_resolved"] = bool(data.get("is_resolved", 0))
        # tolerate extra columns the dataclass doesn't know about
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ───────────────────────── opportunity ─────────────────────────

@dataclass
class Position:
    """An ACTIVE trade we hold and must manage — not just open, but watch and exit.

    Carries the original thesis so the monitor can re-test it against new data each
    cycle and recommend HOLD / TRIM / EXIT. ``extra`` holds the thesis text, entry
    rationale, and the resolution deadline / thesis-breaker conditions to watch for.
    """

    market_id: str
    venue: str
    side: str                       # YES / NO
    shares: float                   # contracts held
    avg_price: float                # entry price per contract [0,1]
    cost_basis: float               # avg_price * shares

    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    realized_pnl: Optional[float] = None
    status: str = "open"            # open | closed
    extra: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def unrealized_pnl(self, current_price: float) -> float:
        """Mark-to-market P&L if marked at ``current_price`` (price of our side)."""
        return round((current_price - self.avg_price) * self.shares, 2)

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["extra"] = json.dumps(d.get("extra") or {})
        return d

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Position":
        data = dict(row)
        raw = data.get("extra")
        data["extra"] = json.loads(raw) if raw else {}
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


class SourceTier(int, Enum):
    """The Tier-1..6 hierarchy from the pipeline design. Lower = closer to source,
    higher signal value, less digested by the crowd."""
    OFFICIAL = 1     # gov releases, filings, dockets, central banks, official stats
    NEWS = 2         # Reuters, Bloomberg, AP, FT, WSJ ...
    SOCIAL = 3       # X, Reddit, YouTube, podcasts
    MARKETS = 5      # other prediction markets / financial markets
    ALT = 6          # Google Trends, flights, shipping, weather ...


@dataclass
class Signal:
    """One piece of incoming information (a headline, a filing, a release).

    Signals are venue-agnostic and market-agnostic on arrival; the relevance
    matcher links them to the markets they might move. The LLM estimator then
    reads a market's linked signals and asks the one question that matters:
    *does this change the probability of this event, and by how much?*
    """

    source: str                       # "reuters", "sec", "fed", "x:@handle" ...
    title: str
    source_tier: int = SourceTier.NEWS.value
    external_id: Optional[str] = None     # url/guid for dedup (UNIQUE with source)
    body: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None    # ISO-8601 UTC
    entities: list = field(default_factory=list)   # extracted names/tickers/places
    collected_at: Optional[str] = None
    id: Optional[int] = None              # set by the store on insert

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["entities"] = json.dumps(d.get("entities") or [])
        return d

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Signal":
        data = dict(row)
        raw = data.get("entities")
        data["entities"] = json.loads(raw) if raw else []
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OppStatus(str, Enum):
    CANDIDATE = "candidate"     # found, not yet vetted by guardrails
    APPROVED = "approved"       # passed guardrails, ready/queued to execute
    EXECUTED = "executed"       # order placed
    REJECTED = "rejected"       # failed guardrails or stale
    EXPIRED = "expired"         # market moved / closed before we acted
    SETTLED = "settled"         # market resolved; outcome known (for learning)


@dataclass
class Opportunity:
    """A candidate bet: back ``side`` of ``market_id`` at ``market_price``,
    because our ``fair_prob`` says it's worth more than that.

    ``edge`` is the post-cost expected value per $1 staked. ``recommended_stake``
    is what the sizing engine (fractional Kelly + caps) says to risk. Both are
    filled in by later phases; Phase 0 just defines the schema and lets the
    store persist/query them.
    """

    market_id: str
    venue: str
    question: str
    side: str                       # Side value

    market_price: float             # price to back `side`, [0, 1]
    fair_prob: float                # our estimated P(side wins), [0, 1]
    edge: float                     # post-cost EV per $1 staked
    edge_source: str                # "arb" | "calibration" | "model" | "llm" | "consensus"

    # sizing (populated by the Kelly engine in Phase 1)
    kelly_fraction: Optional[float] = None      # fraction of bankroll (already scaled, e.g. 1/4-Kelly)
    recommended_stake: Optional[float] = None   # USD

    confidence: Optional[float] = None          # estimator's self-reported confidence [0, 1]
    liquidity: Optional[float] = None           # backable depth near `market_price`

    status: str = OppStatus.CANDIDATE.value
    guardrail_notes: Optional[str] = None       # why approved/rejected
    detail: Dict[str, Any] = field(default_factory=dict)  # estimator breakdown, citations

    created_at: Optional[str] = None            # ISO-8601 UTC; set by the store
    updated_at: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["detail"] = json.dumps(d.get("detail") or {})
        return d

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Opportunity":
        data = dict(row)
        raw = data.get("detail")
        data["detail"] = json.loads(raw) if raw else {}
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ───────────────────────── thesis ─────────────────────────

@dataclass
class Thesis:
    """A researched, falsifiable argument for why a market is mispriced.

    Modeled on a real winning trade: bet NO on "Fable 5 returns by July 1" because
    a policy update added ID verification on July 8, so an earlier return required
    the government to drop a rule first — an unlikely path the volatile market
    hadn't priced. The shape that survives scrutiny is always a *concrete checkable
    constraint* (a date, a procedure, a capacity) that bounds the outcome.

    Every thesis is generated, then run past a ruthless critic that tries to kill
    it. Both survivors and kills are stored — the kills are how the engine learns
    what bad reasoning looks like.
    """

    market_id: str
    venue: str
    question: str
    side: str                       # Side value (YES/NO) being argued
    claim: str                      # the thesis in one sentence
    fair_prob: float                # P(backed side wins) per the thesis
    confidence: float

    causal_chain: list = field(default_factory=list)   # ordered reasoning steps
    key_facts: list = field(default_factory=list)       # [{fact, source, checkable}]
    evidence_summary: str = ""
    generated_by: str = ""          # backend/model that generated it

    status: str = "pending"         # pending | survived | killed
    critic_verdict: Optional[str] = None     # survived | killed
    critic_flaws: list = field(default_factory=list)    # why the critic killed it
    critic_by: Optional[str] = None

    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    id: Optional[int] = None

    _JSON_FIELDS = ("causal_chain", "key_facts", "critic_flaws", "detail")

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_JSON_FIELDS", None)
        for f in self._JSON_FIELDS:
            d[f] = json.dumps(d.get(f) or ([] if f != "detail" else {}))
        return d

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Thesis":
        data = dict(row)
        for f in cls._JSON_FIELDS:
            raw = data.get(f)
            data[f] = json.loads(raw) if raw else ([] if f != "detail" else {})
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})
