"""
pipeline/execute.py -- SAFE Polymarket order executor.

Default behavior: PREVIEW ONLY.  No order is ever placed without --execute.
Every live order path is routed through trade_safety.require_safe() first.

Functions
---------
build_order(*, slug, condition_id, side, action, limit_price, size, note)
    -> (IntendedOrder, market_dict)
preview(order, market, *, cfg=None)  -> SafetyReport    (never places)
execute(order, market, *, trader=None, cfg=None) -> dict  (places if safe)

CLI
---
python -m pipeline.execute --slug SLUG --side NO --action SELL --price 0.785 --size 90
python -m pipeline.execute --condition-id 0x... --side YES --action BUY  --price 0.05  --size 20 --execute
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Windows console UTF-8 (best-effort -- safe to ignore if it fails)
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

GAMMA_URL = "https://gamma-api.polymarket.com"


# ===========================================================================
# Market resolution helpers
# ===========================================================================

def _coerce_list(x) -> list:
    """Parse JSON-encoded list or return list as-is."""
    if isinstance(x, list):
        return [str(v) for v in x]
    if isinstance(x, str):
        try:
            parsed = json.loads(x)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _resolve_token_id(market: dict, side: str) -> str:
    """
    Return the CLOB token_id for the requested side ("YES" or "NO").

    The Gamma market dict stores outcomes and token ids as parallel arrays
    (JSON-encoded strings or Python lists).  outcomes[i] aligns with
    clobTokenIds[i].  Side matching is case-insensitive.

    Raises ValueError if the side cannot be resolved.
    """
    outcomes = _coerce_list(market.get("outcomes", []))
    token_ids = _coerce_list(market.get("clobTokenIds", []))
    if not outcomes or not token_ids:
        raise ValueError(
            f"market {market.get('conditionId', '?')} is missing outcomes or "
            f"clobTokenIds (outcomes={outcomes!r}, tokenIds={token_ids!r})"
        )

    # Primary: direct case-insensitive match on outcome label
    for i, label in enumerate(outcomes):
        if isinstance(label, str) and label.strip().upper() == side.upper():
            if i < len(token_ids):
                return token_ids[i]

    # Fallback: positional convention (index 0 = YES, index 1 = NO)
    if side.upper() == "YES":
        return token_ids[0]
    if side.upper() == "NO" and len(token_ids) > 1:
        return token_ids[1]

    raise ValueError(
        f"Cannot resolve token_id for side={side!r} from "
        f"outcomes={outcomes!r}, clobTokenIds={token_ids!r}"
    )


def _side_price_from_market(market: dict, side: str) -> Optional[float]:
    """Current market price for the given side, from outcomePrices in the Gamma dict."""
    outcomes = _coerce_list(market.get("outcomes", []))
    prices_raw = _coerce_list(market.get("outcomePrices", []))
    if not prices_raw:
        return None

    # Try label-matched index first
    for i, label in enumerate(outcomes):
        if isinstance(label, str) and label.strip().upper() == side.upper():
            if i < len(prices_raw):
                try:
                    return float(prices_raw[i])
                except (TypeError, ValueError):
                    pass

    # Positional fallback
    idx = 0 if side.upper() == "YES" else 1
    if idx < len(prices_raw):
        try:
            return float(prices_raw[idx])
        except (TypeError, ValueError):
            pass

    return None


def _pick_best_market(
    markets: list,
    *,
    side_hint: Optional[str] = None,
    price_hint: Optional[float] = None,
) -> Optional[dict]:
    """
    From a list of markets (inside an event), pick the most relevant one.

    When side_hint and price_hint are supplied (e.g. from build_order), we use
    price proximity as the primary selection criterion: the market whose current
    outcomePrices[side] is closest to limit_price is the one the caller is
    actually trying to trade.  This disambiguates events that have multiple
    open markets at very different price levels (e.g. June-29 NO at 0.987 vs
    July-1 NO at 0.785 -- a --price 0.785 order clearly targets July-1).

    When no price hint is given, fall back to soonest-future-end-date order.

    Priority order (price hint given):
      1. Filter out closed markets.
      2. Sort by |current_side_price - price_hint| ascending.
      3. Among ties, earlier end date wins.

    Priority order (no price hint):
      1. Filter out closed markets.
      2. Sort by end date ascending (soonest, most pressing first).
      3. Undated active markets after dated ones.
    """
    if not markets:
        return None
    if len(markets) == 1:
        return markets[0]

    active = [m for m in markets if not bool(m.get("closed"))]
    if not active:
        return markets[0]   # all closed -- caller will fail on not_resolved check

    # ---- price-proximity mode (preferred when hints are available) ---------
    if side_hint is not None and price_hint is not None:
        def _price_dist(m: dict) -> float:
            p = _side_price_from_market(m, side_hint)
            if p is None:
                return float("inf")
            return abs(p - price_hint)

        return min(active, key=_price_dist)

    # ---- end-date mode -----------------------------------------------------
    now = datetime.now(timezone.utc)
    dated: list[tuple[datetime, dict]] = []
    undated: list[dict] = []

    for m in active:
        end_str = (m.get("endDateIso") or m.get("endDate") or "").strip()
        if end_str:
            try:
                ed = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=timezone.utc)
                dated.append((ed, m))
            except (ValueError, TypeError):
                undated.append(m)
        else:
            undated.append(m)

    # Prefer markets whose end date is still in the future (soonest first)
    future = [(ed, m) for ed, m in dated if ed > now]
    if future:
        future.sort(key=lambda t: t[0])
        return future[0][1]
    if dated:
        dated.sort(key=lambda t: t[0])
        return dated[0][1]
    if undated:
        return undated[0]
    return active[0]


def _gamma_get(session: requests.Session, path: str, params: dict) -> Optional[object]:
    """GET from Gamma API; return parsed JSON or None on error."""
    try:
        r = session.get(f"{GAMMA_URL}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  (gamma request {path} {params} failed: {exc})")
        return None


def fetch_gamma_market(
    *,
    slug: Optional[str] = None,
    condition_id: Optional[str] = None,
    side_hint: Optional[str] = None,
    price_hint: Optional[float] = None,
) -> dict:
    """
    Fetch the Gamma market dict for a slug or conditionId.

    Resolution order:
      1. Direct market lookup by condition_id (fastest, unambiguous).
      2. Direct market lookup by slug (works when slug is a market slug).
      3. Event lookup by slug (slug may be an event slug containing N markets).
         When the event has multiple markets, _pick_best_market() selects the
         most relevant one -- price proximity to price_hint (when supplied) is
         used first, then soonest future end date as fallback.

    Raises ValueError if the market cannot be resolved.
    """
    if not slug and not condition_id:
        raise ValueError("fetch_gamma_market: supply slug or condition_id.")

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    def _extract_list(data) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or []
        return []

    # ---- 1. Lookup by conditionId ------------------------------------------
    if condition_id:
        data = _gamma_get(session, "/markets", {"condition_ids": condition_id})
        items = _extract_list(data)
        for m in items:
            if str(m.get("conditionId", "")).lower() == condition_id.lower():
                return m
        # Some versions of the Gamma API use singular key
        data2 = _gamma_get(session, "/markets", {"conditionId": condition_id})
        for m in _extract_list(data2):
            if str(m.get("conditionId", "")).lower() == condition_id.lower():
                return m

    # ---- 2. Lookup by market slug ------------------------------------------
    if slug:
        data = _gamma_get(session, "/markets", {"slug": slug})
        items = _extract_list(data)
        if items:
            if condition_id:
                for m in items:
                    if str(m.get("conditionId", "")).lower() == condition_id.lower():
                        return m
            else:
                return items[0]

    # ---- 3. Event slug lookup ----------------------------------------------
    if slug:
        data = _gamma_get(session, "/events", {"slug": slug})
        if isinstance(data, dict):
            # Events endpoint may return a single dict or a list
            evs = [data]
        elif isinstance(data, list):
            evs = data
        else:
            evs = []

        markets_in_event: list = []
        for ev in evs:
            if isinstance(ev, dict):
                markets_in_event.extend(ev.get("markets") or [])

        if markets_in_event:
            if condition_id:
                # User supplied a conditionId -- find the exact market
                for m in markets_in_event:
                    if str(m.get("conditionId", "")).lower() == condition_id.lower():
                        return m
                avail = [m.get("conditionId") for m in markets_in_event]
                raise ValueError(
                    f"conditionId {condition_id!r} not found in event {slug!r}. "
                    f"Available conditionIds: {avail}"
                )
            else:
                # No conditionId given -- pick the most relevant market
                chosen = _pick_best_market(
                    markets_in_event,
                    side_hint=side_hint,
                    price_hint=price_hint,
                )
                if chosen is not None:
                    return chosen

    raise ValueError(
        f"Could not resolve Gamma market for slug={slug!r}, "
        f"condition_id={condition_id!r}"
    )


# ===========================================================================
# Core pipeline functions
# ===========================================================================

def build_order(
    *,
    slug: Optional[str] = None,
    condition_id: Optional[str] = None,
    side: str,
    action: str,
    limit_price: float,
    size: float,
    note: str = "",
) -> Tuple["IntendedOrder", dict]:
    """
    Fetch the market and build an IntendedOrder ready for the safety gate.

    Parameters
    ----------
    slug          : Polymarket market or event slug.
    condition_id  : conditionId (0x...); preferred over slug when both given.
    side          : "YES" or "NO"
    action        : "BUY" or "SELL"
    limit_price   : limit price per share, e.g. 0.785
    size          : number of shares, e.g. 90.0
    note          : free-text annotation (stored in the ledger)

    Returns
    -------
    (IntendedOrder, market_dict)
        The order is ready for preview() or execute(); the market_dict is the
        raw Gamma response needed by verify_order() / require_safe().
    """
    from pipeline.trade_safety import IntendedOrder

    side = side.upper()
    action = action.upper()
    if side not in ("YES", "NO"):
        raise ValueError(f"--side must be YES or NO, got {side!r}")
    if action not in ("BUY", "SELL"):
        raise ValueError(f"--action must be BUY or SELL, got {action!r}")

    market = fetch_gamma_market(
        slug=slug,
        condition_id=condition_id,
        side_hint=side,
        price_hint=limit_price,
    )

    token_id = _resolve_token_id(market, side)
    cond_id = str(market.get("conditionId") or condition_id or "")
    market_slug = str(market.get("slug") or slug or "")

    # market_id must end with conditionId for trade_safety.market_identity check
    market_id = f"poly:{cond_id}"

    # Re-derive notional in Python -- never trust caller-supplied notional
    notional = round(limit_price * size, 4)

    order = IntendedOrder(
        market_id=market_id,
        condition_id=cond_id,
        slug=market_slug,
        side=side,
        action=action,
        token_id=token_id,
        limit_price=limit_price,
        size=size,
        notional=notional,
        note=note,
    )
    return order, market


def preview(
    order: "IntendedOrder",
    market: dict,
    *,
    cfg: Optional["SafetyConfig"] = None,
) -> "SafetyReport":
    """
    Print a full order ticket and run the safety gate.  NEVER places an order.

    For SELL orders, attempts to fetch live on-chain position via
    monitor.live_positions() so the sell_coverage check has real data.
    If positions cannot be fetched, the check will be marked FAIL in the
    report -- which is the correct conservative behaviour.

    Returns the SafetyReport (caller can inspect .safe and .failures()).
    """
    from pipeline.trade_safety import SafetyConfig, verify_order

    cfg = cfg or SafetyConfig()

    # ------- fetch held position for SELL orders ----------------------------
    held: Optional[dict] = None
    if order.action == "SELL":
        try:
            from pipeline.monitor import live_positions
            positions = live_positions()
            key = (order.condition_id.lower(), order.side.lower())
            if key in positions:
                held = dict(positions[key])
                # trade_safety.sell_coverage inspects held["outcome"] or held["side"]
                # live_positions() doesn't set these; inject the known side here.
                held.setdefault("outcome", order.side)
                held.setdefault("side", order.side)
            elif order.token_id in positions:
                held = dict(positions[order.token_id])
                held.setdefault("outcome", order.side)
                held.setdefault("side", order.side)
        except Exception as exc:  # noqa: BLE001
            print(f"  (live positions fetch failed: {exc})")

    # ------- run safety checks (no-raise) -----------------------------------
    report = verify_order(order, market=market, held=held, cfg=cfg)

    # ------- current market price for the side we are trading ---------------
    cur_price = _side_price_from_market(market, order.side)

    # ------- print full ticket ----------------------------------------------
    SEP = "-" * 60
    DSEP = "=" * 60
    print()
    print(DSEP)
    print(f"  ORDER TICKET  [{order.action}  {order.side}]")
    print(DSEP)
    print(f"  question : {market.get('question', '(unknown)')}")
    print(f"  cond id  : {order.condition_id}")
    print(f"  slug     : {order.slug}")
    print(f"  side     : {order.side}   action: {order.action}")
    print(f"  token_id : ...{order.token_id[-8:]}")
    print(f"  price    : {order.limit_price:.4f}")
    print(f"  size     : {order.size:.4f}  shares")
    print(f"  notional : ${order.notional:.4f}")
    if cur_price is not None:
        print(f"  mkt price: {cur_price:.4f}  ({order.side})")
    if order.action == "SELL":
        if held is not None:
            print(f"  held     : {held.get('size', '?')}  shares  "
                  f"(avg {held.get('avg', '?')}  cur_price={held.get('cur_price', '?')})")
        else:
            print(f"  held     : (not fetched -- live position unavailable)")
    if order.note:
        print(f"  note     : {order.note}")
    print(SEP)
    print(report.summary())
    print(DSEP)

    return report


def execute(
    order: "IntendedOrder",
    market: dict,
    *,
    trader=None,
    cfg: Optional["SafetyConfig"] = None,
    marketable: bool = False,
) -> dict:
    """
    Place the order live.

    Safety gate runs FIRST (require_safe).  If any critical check fails,
    TradeSafetyError is raised and NO order is submitted.  This is the only
    path to the CLOB and it cannot be bypassed.

    Parameters
    ----------
    order   : IntendedOrder from build_order()
    market  : Gamma market dict from build_order() / fetch_gamma_market()
    trader  : Optional pre-built PolymarketTrader(mode="live").  Built lazily
              if omitted; raises RuntimeError if credentials are absent.
    cfg     : SafetyConfig overrides (e.g. custom max_order_notional).

    Returns
    -------
    dict with keys: placed (bool), order_id (str), status (str),
                    skipped_reason (str), response (dict | None)
    """
    from pipeline.trade_safety import SafetyConfig, require_safe

    cfg = cfg or SafetyConfig()

    # ------- fetch held position for SELL coverage check --------------------
    held: Optional[dict] = None
    if order.action == "SELL":
        try:
            from pipeline.monitor import live_positions
            positions = live_positions()
            key = (order.condition_id.lower(), order.side.lower())
            if key in positions:
                held = dict(positions[key])
                held.setdefault("outcome", order.side)
                held.setdefault("side", order.side)
            elif order.token_id in positions:
                held = dict(positions[order.token_id])
                held.setdefault("outcome", order.side)
                held.setdefault("side", order.side)
        except Exception as exc:  # noqa: BLE001
            print(f"  (live positions fetch failed: {exc})")

    # ------- SAFETY GATE -- always first, always required ------------------
    # require_safe() raises TradeSafetyError if any critical check fails.
    # If it returns, every invariant is verified and we have the report on file.
    report = require_safe(order, market=market, held=held, cfg=cfg)  # noqa: F841

    # ------- build trader if not supplied -----------------------------------
    if trader is None:
        try:
            from polymarket_trader import PolymarketTrader
            # marketable=True -> allow taking liquidity (fills now); else maker-only.
            trader = PolymarketTrader(mode="live", post_only=not marketable)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot build live trader (check POLY_* env vars): {exc}"
            ) from exc

    # ------- resolve CLOB side constant ------------------------------------
    try:
        from py_clob_client_v2.order_builder.constants import BUY, SELL as SELL_CONST
    except ImportError:
        try:
            from py_clob_client_v2.order_builder.constants import BUY
            SELL_CONST = "SELL"   # fallback string
        except ImportError as exc:
            raise RuntimeError(
                f"py_clob_client_v2 not installed or incompatible: {exc}"
            ) from exc

    clob_side = BUY if order.action == "BUY" else SELL_CONST

    # ------- place on CLOB -------------------------------------------------
    from py_clob_client_v2.clob_types import OrderArgs, OrderType

    placed = False
    order_id = ""
    status = ""
    skipped_reason = ""
    response: Optional[dict] = None

    try:
        args = OrderArgs(
            token_id=order.token_id,
            price=order.limit_price,
            size=order.size,
            side=clob_side,
        )
        signed = trader.client.create_order(args)
        resp = trader.client.post_order(signed, OrderType.GTC, post_only=not marketable)
        response = resp if isinstance(resp, dict) else {"raw": str(resp)}

        ok = response.get("success")
        err = response.get("errorMsg") or response.get("error")
        status = str(response.get("status") or "").lower()
        good_status = status in ("live", "matched", "delayed", "open")

        if ok is False or (err and str(err).strip()):
            placed = False
            skipped_reason = f"rejected: {err or response}"
        elif ok is True or good_status:
            placed = True
        else:
            placed = False
            skipped_reason = f"ambiguous response (treat as NOT placed): {response}"

        order_id = (str(response.get("orderID") or response.get("order_id") or ""))
    except Exception as exc:  # noqa: BLE001
        skipped_reason = f"CLOB post error: {repr(exc)[:200]}"
        placed = False

    # ------- ledger (best-effort -- never let this fail the trade) ----------
    try:
        import poly_ledger
        question = str(market.get("question") or "")
        cur_price = _side_price_from_market(market, order.side)
        ledger_status = (status or "live") if placed else f"failed:{skipped_reason[:40]}"
        row = {
            "ts": int(time.time()),
            "run_id": "execute",
            "mode": "live",
            "signal": "manual",
            "market_id": order.market_id,
            "slug": order.slug,
            "title": question[:80],
            "yes_no": order.side,
            "token_id": order.token_id,
            "limit_price": f"{order.limit_price:.4f}",
            "size": f"{order.size:.4f}",
            "notional": f"{order.notional:.4f}",
            "status": ledger_status,
            "order_id": order_id,
            "fair": "",
            "market_price": f"{cur_price:.4f}" if cur_price is not None else "",
            "edge_cents": "",
            "event_key": order.note,
        }
        poly_ledger.log_orders([row])
        print(f"  ledger: logged {order.action} {order.side} row "
              f"(order_id={order_id or 'n/a'}).")
    except Exception as exc:  # noqa: BLE001
        print(f"  (ledger logging failed -- non-fatal: {exc})")

    return {
        "placed": placed,
        "order_id": order_id,
        "status": status,
        "skipped_reason": skipped_reason,
        "response": response,
    }


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None) -> int:
    # Re-apply UTF-8 reconfiguration here too (called as __main__)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        prog="python -m pipeline.execute",
        description=(
            "SAFE Polymarket order executor.\n"
            "Default: PREVIEW ONLY.  Pass --execute to actually place.\n"
            "Every live order requires --execute AND all safety checks passing."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--slug", default=None,
                    help="market or event slug")
    ap.add_argument("--condition-id", dest="condition_id", default=None,
                    help="market conditionId (0x...); preferred when both slug and "
                         "condition-id are given -- disambiguates multi-market events")
    ap.add_argument("--side", required=True, choices=["YES", "NO"],
                    help="outcome side to trade")
    ap.add_argument("--action", required=True, choices=["BUY", "SELL"],
                    help="order action")
    ap.add_argument("--price", dest="price", type=float, required=True,
                    help="limit price per share, e.g. 0.785")
    ap.add_argument("--size", type=float, required=True,
                    help="number of shares, e.g. 90")
    ap.add_argument("--note", default="", help="free-text annotation for the ledger")
    ap.add_argument("--execute", action="store_true",
                    help="place the order (default: preview only, nothing is sent)")
    ap.add_argument("--marketable", action="store_true",
                    help="allow taking liquidity (fills now) instead of maker-only/post-only; "
                         "use for an immediate fill, e.g. locking profit on a SELL")
    ap.add_argument("--max-notional", dest="max_notional", type=float, default=None,
                    help="override SafetyConfig.max_order_notional (USD cap per order)")

    args = ap.parse_args(argv)

    if not args.slug and not args.condition_id:
        ap.error("supply at least one of --slug or --condition-id")

    from pipeline.trade_safety import SafetyConfig, TradeSafetyError

    cfg = SafetyConfig()
    if args.max_notional is not None:
        cfg.max_order_notional = args.max_notional

    # ------- build order ----------------------------------------------------
    try:
        order, market = build_order(
            slug=args.slug,
            condition_id=args.condition_id,
            side=args.side,
            action=args.action,
            limit_price=args.price,
            size=args.size,
            note=args.note,
        )
    except Exception as exc:
        print(f"ERROR: could not build order: {exc}")
        return 1

    # ------- preview (always runs) -----------------------------------------
    report = preview(order, market, cfg=cfg)

    if not args.execute:
        print()
        print("  (preview only -- pass --execute to place)")
        return 0

    # ------- execute path: gate must be clear ------------------------------
    if not report.safe:
        print()
        print("  REFUSING TO PLACE: safety checks failed.")
        for c in report.failures():
            print(f"    [FAIL] {c.name}: {c.detail}")
        print()
        print("  Fix the failures above, then re-run with --execute.")
        return 2

    print()
    print("  Safety gate passed -- placing order...")
    try:
        result = execute(order, market, cfg=cfg, marketable=args.marketable)
    except TradeSafetyError as exc:
        # require_safe() inside execute() caught something preview missed
        # (e.g. market changed between preview and execute)
        print(f"  SAFETY GATE (execute): {exc}")
        return 2
    except RuntimeError as exc:
        # Trader build failed (missing creds / geo-block)
        print(f"  ERROR (trader): {exc}")
        print("  Order was NOT placed.  Check POLY_* environment variables.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  UNEXPECTED ERROR: {exc}")
        return 1

    if result["placed"]:
        print(f"  ORDER PLACED")
        print(f"    order_id : {result['order_id']}")
        print(f"    status   : {result['status']}")
        return 0
    else:
        print(f"  ORDER NOT PLACED: {result['skipped_reason']}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
