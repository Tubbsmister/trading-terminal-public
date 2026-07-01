"""
Benchmark snapshot — the measurement baseline for the self-improvement loop.

Read-only. Captures the metrics that tell us whether the system is getting better:
signal coverage by tier, thesis survival rate + critic-score quality, which data tiers
produce surviving theses, opportunity edge distribution, calibration (Brier vs price),
and open-position monitor state. Each run writes a timestamped JSON to benchmarks/ and
prints a summary; the loop compares snapshots across cycles to judge improvements.

    python -m pipeline.benchmark            # snapshot + print + save
    python -m pipeline.benchmark --no-save  # print only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .store import get_store


def _conn(store):
    c = sqlite3.connect(str(store.db_path))
    c.row_factory = sqlite3.Row
    return c


def snapshot() -> dict:
    store = get_store()
    snap: dict = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    snap["markets_active"] = store.count_markets(active_only=True)

    with _conn(store) as c:
        # signals by tier
        snap["signals_by_tier"] = {
            str(r["source_tier"]): r["n"]
            for r in c.execute("SELECT source_tier, COUNT(*) n FROM signals GROUP BY source_tier")
        }
        snap["signals_total"] = sum(snap["signals_by_tier"].values())

        # theses: status + survival rate + avg composite + winning tiers
        theses = c.execute("SELECT status, critic_verdict, detail FROM theses").fetchall()
        st = Counter(t["status"] for t in theses)
        survived = st.get("survived", 0)
        decided = survived + st.get("killed", 0)
        # hallucination rate: theses killed by the deterministic grounding gate (fabricated
        # dates/amounts not in evidence) before the critic ever ran.
        ungrounded_kills = sum(1 for t in theses if t["critic_verdict"] == "ungrounded")
        # which CRITICAL gates fail most — shows where theses die (e.g. fabricated facts
        # vs. weak causal chains vs. resolution misreads), so we can target the bottleneck.
        gate_fails: Counter = Counter()
        comps, tier_use = [], Counter()
        for t in theses:
            try:
                d = json.loads(t["detail"]) if t["detail"] else {}
            except json.JSONDecodeError:
                d = {}
            if isinstance(d.get("composite_score"), (int, float)):
                comps.append(d["composite_score"])
            for gate, ok in (d.get("gates") or {}).items():
                if ok is False:
                    gate_fails[gate] += 1
            if t["status"] == "survived":
                for tier in (d.get("which_tiers_used") or []):
                    tier_use[str(tier)] += 1
        snap["theses"] = {
            "total": len(theses),
            "by_status": dict(st),
            "survival_rate": round(survived / decided, 3) if decided else None,
            "avg_composite_score": round(sum(comps) / len(comps), 2) if comps else None,
            "winning_tiers": dict(tier_use),  # tiers that produced surviving theses
            "ungrounded_kills": ungrounded_kills,  # hallucinations caught pre-critic
            "gate_fails": dict(gate_fails.most_common()),  # which critical gates kill theses
        }

        # opportunities by source (latest-ish; append-log so this is cumulative)
        snap["opportunities_by_source"] = {
            r["edge_source"]: r["n"]
            for r in c.execute("SELECT edge_source, COUNT(*) n FROM opportunities "
                               "WHERE status='approved' GROUP BY edge_source")
        }

        # calibration: resolved theses' realized outcome vs estimate (if any resolutions)
        try:
            res = c.execute("SELECT COUNT(*) n FROM resolutions").fetchone()
            snap["resolutions_recorded"] = res["n"] if res else 0
        except sqlite3.OperationalError:
            snap["resolutions_recorded"] = 0

        # open positions + last monitor recommendations  (defined below the triage block)
        pass

    # triage preview — which markets the thesis engine will actually research this
    # cycle. The constraint-thesis generator only finds edges in constraint-shaped
    # markets, so this shows whether triage surfaces those vs. game-outcome markets
    # (the diagnosed failure mode: 0 theses because triage fed it only sports).
    try:
        from . import thesis as _thesis
        triaged = _thesis.triage(store, max_markets=20)
        snap["triage_preview"] = {
            "n": len(triaged),
            "by_category": dict(Counter((m.category or "uncategorized") for m in triaged)),
            "game_outcome": sum(1 for m in triaged
                                if _thesis._category_multiplier(m) < 1.0),
        }
    except Exception as e:  # never let the measurement break the benchmark
        snap["triage_preview"] = {"error": str(e)[:160]}

    with _conn(store) as c:
        # open positions + last monitor recommendations
        try:
            snap["open_positions"] = c.execute(
                "SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
            snap["last_monitor_recs"] = [
                dict(r) for r in c.execute(
                    "SELECT recommendation, COUNT(*) n FROM position_reviews GROUP BY recommendation")
            ]
        except sqlite3.OperationalError:
            snap["open_positions"] = 0
            snap["last_monitor_recs"] = []

    return snap


def _print(snap: dict) -> None:
    print(f"=== BENCHMARK {snap['ts']} ===")
    print(f"  active markets : {sum(snap['markets_active'].values())} {snap['markets_active']}")
    print(f"  signals        : {snap['signals_total']} by tier {snap['signals_by_tier']}")
    th = snap["theses"]
    print(f"  theses         : {th['total']} total | {th['by_status']}")
    print(f"     survival rate: {th['survival_rate']}  avg score: {th['avg_composite_score']}/10")
    print(f"     winning tiers: {th['winning_tiers'] or '(none yet)'}  "
          f"ungrounded kills: {th.get('ungrounded_kills', 0)}")
    if th.get("gate_fails"):
        print(f"     gate fails   : {th['gate_fails']}")
    tp = snap.get("triage_preview", {})
    if "error" not in tp:
        print(f"  triage preview : {tp.get('n', 0)} markets "
              f"({tp.get('game_outcome', 0)} game-outcome) {tp.get('by_category', {})}")
    print(f"  paper bets     : {snap['opportunities_by_source'] or '(none)'}")
    print(f"  resolutions    : {snap['resolutions_recorded']}  | open positions: {snap['open_positions']}")
    if snap["last_monitor_recs"]:
        print(f"  monitor recs   : {snap['last_monitor_recs']}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Benchmark snapshot for the improvement loop")
    ap.add_argument("--no-save", action="store_true", help="print only, don't write JSON")
    args = ap.parse_args(argv)

    snap = snapshot()
    _print(snap)
    if not args.no_save:
        out_dir = Path(__file__).resolve().parent.parent / "benchmarks"
        out_dir.mkdir(exist_ok=True)
        fname = out_dir / f"bench_{snap['ts'].replace(':', '').replace('-', '')}.json"
        fname.write_text(json.dumps(snap, indent=2))
        print(f"\n  saved -> {fname.relative_to(out_dir.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
