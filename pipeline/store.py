"""
SQLite storage layer for the pipeline.

Why SQLite: a single-file DB with zero setup that comfortably handles our scale
(thousands of live markets, time-series price snapshots, an opportunity log).
It replaces the scattered flat CSV/JSON files for *pipeline* state — the
existing money-scripts keep their own ledgers untouched.

Four tables:

* ``markets``           — latest normalized state per market (upsert by id)
* ``market_snapshots``  — append-only price/volume observations (our own
                          cross-venue price history, built as we poll)
* ``opportunities``     — candidate/approved/executed bets (the audit trail)
* ``positions``         — open exposure per market (used from Phase 2 on)

The store is deliberately thin: dicts in, dicts/dataclasses out. No ORM.
Concurrency: WAL mode so a collector can write while the dashboard reads.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import Market, Opportunity, OppStatus, Signal, Thesis, Position


DEFAULT_DB_PATH = Path(
    os.environ.get(
        "PIPELINE_DB",
        str(Path(__file__).resolve().parent.parent / "pipeline.db"),
    )
)


def utcnow_iso() -> str:
    """Current UTC time as ISO-8601, e.g. ``2026-06-29T14:03:11+00:00``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id              TEXT PRIMARY KEY,
    venue           TEXT NOT NULL,
    venue_market_id TEXT NOT NULL,
    question        TEXT NOT NULL,
    yes_price       REAL,
    no_price        REAL,
    volume          REAL,
    volume_24h      REAL,
    liquidity       REAL,
    category        TEXT,
    close_time      TEXT,
    url             TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_resolved     INTEGER NOT NULL DEFAULT 0,
    extra           TEXT,
    collected_at    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_venue   ON markets(venue);
CREATE INDEX IF NOT EXISTS idx_markets_active  ON markets(is_active);
CREATE INDEX IF NOT EXISTS idx_markets_close   ON markets(close_time);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    yes_price  REAL,
    no_price   REAL,
    volume     REAL,
    liquidity  REAL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);
CREATE INDEX IF NOT EXISTS idx_snap_market_ts ON market_snapshots(market_id, ts);

CREATE TABLE IF NOT EXISTS opportunities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id        TEXT NOT NULL,
    venue            TEXT NOT NULL,
    question         TEXT NOT NULL,
    side             TEXT NOT NULL,
    market_price     REAL NOT NULL,
    fair_prob        REAL NOT NULL,
    edge             REAL NOT NULL,
    edge_source      TEXT NOT NULL,
    kelly_fraction   REAL,
    recommended_stake REAL,
    confidence       REAL,
    liquidity        REAL,
    status           TEXT NOT NULL,
    guardrail_notes  TEXT,
    detail           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opp_market ON opportunities(market_id);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    venue         TEXT NOT NULL,
    side          TEXT NOT NULL,
    shares        REAL NOT NULL,
    avg_price     REAL NOT NULL,
    cost_basis    REAL NOT NULL,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT,
    realized_pnl  REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    extra         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_pos_market ON positions(market_id);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    source_tier  INTEGER NOT NULL,
    external_id  TEXT,
    title        TEXT NOT NULL,
    body         TEXT,
    url          TEXT,
    published_at TEXT,
    entities     TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_sig_published ON signals(published_at);
CREATE INDEX IF NOT EXISTS idx_sig_tier      ON signals(source_tier);

-- relevance edges: which signal might move which market, and how strongly
CREATE TABLE IF NOT EXISTS signal_market_links (
    signal_id     INTEGER NOT NULL,
    market_id     TEXT NOT NULL,
    relevance     REAL NOT NULL,
    matched_terms TEXT,
    linked_at     TEXT NOT NULL,
    PRIMARY KEY (signal_id, market_id)
);
CREATE INDEX IF NOT EXISTS idx_link_market ON signal_market_links(market_id);

CREATE TABLE IF NOT EXISTS theses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    venue           TEXT NOT NULL,
    question        TEXT NOT NULL,
    side            TEXT NOT NULL,
    claim           TEXT NOT NULL,
    fair_prob       REAL NOT NULL,
    confidence      REAL NOT NULL,
    causal_chain    TEXT,
    key_facts       TEXT,
    evidence_summary TEXT,
    generated_by    TEXT,
    status          TEXT NOT NULL,
    critic_verdict  TEXT,
    critic_flaws    TEXT,
    critic_by       TEXT,
    detail          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status);
CREATE INDEX IF NOT EXISTS idx_theses_market ON theses(market_id);

-- monitor verdicts over time for each active position (audit of how the call evolved)
CREATE TABLE IF NOT EXISTS position_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    current_price   REAL,
    unrealized_pnl  REAL,
    updated_fair    REAL,
    edge            REAL,
    recommendation  TEXT,
    thesis_intact   INTEGER,
    threat          TEXT,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_posrev_pos ON position_reviews(position_id, ts);
"""


class Store:
    """Thin SQLite wrapper. Open one per process; it's cheap to reopen."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ───────────────────────── markets ─────────────────────────

    def upsert_market(self, market: Market, *, snapshot: bool = True) -> None:
        """Insert or update a market's latest state, preserving ``first_seen_at``.

        If ``snapshot`` is True, also append a price observation so we
        accumulate our own cross-venue price history over time.
        """
        now = utcnow_iso()
        market.collected_at = now
        row = market.to_row()
        cols = [
            "id", "venue", "venue_market_id", "question", "yes_price", "no_price",
            "volume", "volume_24h", "liquidity", "category", "close_time", "url",
            "is_active", "is_resolved", "extra", "collected_at",
        ]
        placeholders = ", ".join("?" for _ in cols)
        update_set = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        with self._conn() as conn:
            conn.execute(
                f"""
                INSERT INTO markets ({", ".join(cols)}, first_seen_at)
                VALUES ({placeholders}, ?)
                ON CONFLICT(id) DO UPDATE SET {update_set}
                """,
                [row[c] for c in cols] + [now],
            )
            # Don't pollute price history with all-null rows (e.g. venue outage).
            if snapshot and (market.yes_price is not None or market.no_price is not None):
                conn.execute(
                    """INSERT INTO market_snapshots
                       (market_id, ts, yes_price, no_price, volume, liquidity)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (market.id, now, market.yes_price, market.no_price,
                     market.volume_24h if market.volume_24h is not None else market.volume,
                     market.liquidity),
                )

    def upsert_markets(self, markets: Iterable[Market], *, snapshot: bool = True) -> int:
        n = 0
        for m in markets:
            self.upsert_market(m, snapshot=snapshot)
            n += 1
        return n

    def get_market(self, market_id: str) -> Optional[Market]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        return Market.from_row(dict(r)) if r else None

    def get_markets(
        self,
        *,
        venue: Optional[str] = None,
        active_only: bool = True,
        limit: Optional[int] = None,
    ) -> List[Market]:
        q = "SELECT * FROM markets WHERE 1=1"
        params: List[Any] = []
        if venue:
            q += " AND venue = ?"
            params.append(venue)
        if active_only:
            q += " AND is_active = 1 AND is_resolved = 0"
        q += " ORDER BY COALESCE(volume_24h, volume, 0) DESC"
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [Market.from_row(dict(r)) for r in rows]

    def count_markets(self, *, active_only: bool = True) -> Dict[str, int]:
        q = "SELECT venue, COUNT(*) AS n FROM markets"
        if active_only:
            q += " WHERE is_active = 1 AND is_resolved = 0"
        q += " GROUP BY venue"
        with self._conn() as conn:
            rows = conn.execute(q).fetchall()
        return {r["venue"]: r["n"] for r in rows}

    # ───────────────────────── opportunities ─────────────────────────

    def add_opportunity(self, opp: Opportunity) -> int:
        now = utcnow_iso()
        opp.created_at = opp.created_at or now
        opp.updated_at = now
        row = opp.to_row()
        cols = [
            "market_id", "venue", "question", "side", "market_price", "fair_prob",
            "edge", "edge_source", "kelly_fraction", "recommended_stake", "confidence",
            "liquidity", "status", "guardrail_notes", "detail", "created_at", "updated_at",
        ]
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
            return int(cur.lastrowid)

    def set_opportunity_status(
        self, opp_id: int, status: OppStatus | str, notes: Optional[str] = None
    ) -> None:
        status_val = status.value if isinstance(status, OppStatus) else status
        with self._conn() as conn:
            conn.execute(
                "UPDATE opportunities SET status = ?, guardrail_notes = COALESCE(?, guardrail_notes), "
                "updated_at = ? WHERE id = ?",
                (status_val, notes, utcnow_iso(), opp_id),
            )

    def get_opportunities(
        self, *, status: Optional[str] = None, limit: int = 100
    ) -> List[Opportunity]:
        q = "SELECT * FROM opportunities"
        params: List[Any] = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY edge DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [Opportunity.from_row(dict(r)) for r in rows]

    # ───────────────────────── signals ─────────────────────────

    def add_signal(self, signal: Signal) -> Optional[int]:
        """Insert a signal, deduped on (source, external_id). Returns the row id,
        or None if it was a duplicate that already existed.

        Dedup hardening: SQLite treats NULLs as distinct in a UNIQUE constraint, so a
        signal with no external_id would never dedup and would re-insert every run.
        Fall back to a stable content hash so feeds without GUIDs still dedup.
        """
        now = utcnow_iso()
        signal.collected_at = signal.collected_at or now
        if not signal.external_id:
            import hashlib
            basis = f"{signal.source}|{signal.title}|{signal.published_at or ''}"
            signal.external_id = "h:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        row = signal.to_row()
        cols = ["source", "source_tier", "external_id", "title", "body", "url",
                "published_at", "entities", "collected_at"]
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO signals ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def add_signals(self, signals: Iterable[Signal]) -> int:
        """Bulk insert; returns count of NEW (non-duplicate) rows."""
        return sum(1 for s in signals if self.add_signal(s) is not None)

    def get_recent_signals(
        self, *, hours: float = 48, source_tier: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Signal]:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        q = ("SELECT * FROM signals WHERE COALESCE(published_at, collected_at) >= ?")
        params: List[Any] = [cutoff]
        if source_tier is not None:
            q += " AND source_tier = ?"
            params.append(source_tier)
        q += " ORDER BY COALESCE(published_at, collected_at) DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [Signal.from_row(dict(r)) for r in rows]

    def link_signal_market(self, signal_id: int, market_id: str,
                           relevance: float, matched_terms: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signal_market_links "
                "(signal_id, market_id, relevance, matched_terms, linked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (signal_id, market_id, relevance, matched_terms, utcnow_iso()),
            )

    def clear_links_for_signal(self, signal_id: int) -> None:
        """Remove all relevance links for one signal, so a re-run can rewrite only the
        currently-qualifying ones (idempotent re-linking — purges links that no longer
        meet the relevance rule instead of leaving INSERT-OR-REPLACE orphans)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM signal_market_links WHERE signal_id = ?", (signal_id,))

    def get_signals_for_market(self, market_id: str, *, limit: int = 20) -> List[dict]:
        """Signals linked to a market, most-relevant first, with the join payload."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.*, l.relevance, l.matched_terms FROM signals s "
                "JOIN signal_market_links l ON l.signal_id = s.id "
                "WHERE l.market_id = ? ORDER BY l.relevance DESC, "
                "COALESCE(s.published_at, s.collected_at) DESC LIMIT ?",
                (market_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_signals(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])

    # ───────────────────────── theses ─────────────────────────

    def add_thesis(self, thesis: Thesis) -> int:
        now = utcnow_iso()
        thesis.created_at = thesis.created_at or now
        row = thesis.to_row()
        cols = ["market_id", "venue", "question", "side", "claim", "fair_prob",
                "confidence", "causal_chain", "key_facts", "evidence_summary",
                "generated_by", "status", "critic_verdict", "critic_flaws",
                "critic_by", "detail", "created_at"]
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO theses ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
            return int(cur.lastrowid)

    def get_theses(self, *, status: Optional[str] = None, limit: int = 100) -> List[Thesis]:
        q = "SELECT * FROM theses"
        params: List[Any] = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [Thesis.from_row(dict(r)) for r in rows]

    def count_theses(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) n FROM theses GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ───────────────────────── positions (active trades) ─────────────────────────

    def add_position(self, position: Position) -> int:
        position.opened_at = position.opened_at or utcnow_iso()
        if not position.cost_basis:
            position.cost_basis = round(position.avg_price * position.shares, 2)
        row = position.to_row()
        cols = ["market_id", "venue", "side", "shares", "avg_price", "cost_basis",
                "opened_at", "closed_at", "realized_pnl", "status", "extra"]
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO positions ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
            return int(cur.lastrowid)

    def get_open_positions(self) -> List[Position]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at DESC"
            ).fetchall()
        return [Position.from_row(dict(r)) for r in rows]

    def close_position(self, position_id: int, realized_pnl: Optional[float] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE positions SET status='closed', closed_at=?, realized_pnl=? WHERE id=?",
                (utcnow_iso(), realized_pnl, position_id),
            )

    def add_position_review(self, position_id: int, *, current_price=None,
                            unrealized_pnl=None, updated_fair=None, edge=None,
                            recommendation=None, thesis_intact=None, threat=None,
                            reason=None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO position_reviews
                   (position_id, ts, current_price, unrealized_pnl, updated_fair, edge,
                    recommendation, thesis_intact, threat, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (position_id, utcnow_iso(), current_price, unrealized_pnl, updated_fair,
                 edge, recommendation,
                 None if thesis_intact is None else int(bool(thesis_intact)),
                 threat, reason),
            )
            return int(cur.lastrowid)

    def get_position_reviews(self, position_id: int, limit: int = 20) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM position_reviews WHERE position_id=? ORDER BY ts DESC LIMIT ?",
                (position_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ───────────────────────── snapshots ─────────────────────────

    def get_price_history(self, market_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, yes_price, no_price, volume, liquidity FROM market_snapshots "
                "WHERE market_id = ? ORDER BY ts DESC LIMIT ?",
                (market_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]


_default_store: Optional[Store] = None


def get_store() -> Store:
    """Process-wide default store (lazy singleton)."""
    global _default_store
    if _default_store is None:
        _default_store = Store()
    return _default_store
