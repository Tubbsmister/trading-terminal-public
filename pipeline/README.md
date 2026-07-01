# pipeline — domain-general positive-EV betting engine

A single engine that scans **every** prediction market across venues, estimates the
true probability of each event, compares it to the market price, and — where a real
edge exists — sizes a bet by fractional Kelly and (eventually) executes it within hard
guardrails. The decision pipeline is identical for every market; only the *probability
estimate* differs by domain. We build the shared machine once and bolt on data
collectors / estimators incrementally.

```
collect markets  →  estimate P(event)  →  edge vs price (net of fees/liquidity)
  →  fractional Kelly sizing + caps  →  guardrailed execution  →  log & learn
```

It **reuses** the existing money-scripts rather than replacing them: read-only clients
(`polymarket_client`, `kalshi_client`), the live execution layer (`polymarket_trader`),
the ledger (`poly_ledger`), and the calibrated probability model (`longshot_model`).

## Model routing (keeps "scan everything" affordable, ~$50–300/mo)

| Layer | Model | Job |
|---|---|---|
| Tier 0 | none (pure math) | dedup, calibration prior, cross-venue arb — kills ~95%, $0 |
| Triage | Haiku | extract resolution criteria; "worth deep analysis?" |
| Synthesis | Sonnet | per-market probability estimate from news/web → structured `{P, confidence, citation}` |
| Decision | Opus 4.8 | sees only structured synopses for top candidates; final go/size call |

Every model boundary passes **structured JSON, not prose** — that's what keeps Opus cheap.

## Phase status

- [x] **Phase 0 — Foundation**: SQLite store, `Market`/`Opportunity` models,
      collectors for Polymarket + Kalshi + Manifold. Normalized cross-venue view +
      price-history snapshots.
- [x] **Phase 1 — Free EV**: within-market arb + calibration-prior mispricings →
      `Opportunity` rows; quarter-Kelly sizing; guardrails; paper-trade scan; tests.
      *Finding: the free statistical edge is weak (barely beats raw price out-of-sample)
      and real arb is rare — confirms the information edge (Phase 3) is the real prize.*
- [~] **Phase 3 — Information edge (LLM)** *(in progress)*: signals store + news/RSS
      collector + relevance matcher (no-LLM pre-filter); **Sonnet** per-market
      probability estimator; **Opus** decision/veto layer. Built and degrading cleanly;
      **needs `ANTHROPIC_API_KEY` to run live.**
- [x] **Phase 4 — Learning loop** *(core)*: `learn.py` — resolution tracking +
      Brier/P&L scoring per estimator (accumulates as paper bets resolve).
- [ ] **Phase 2 — Auto execution**: flip guardrailed execution live (Poly + new Kalshi
      exec); bankroll + daily-loss kill-switch; VPN-connectivity watchdog. *Deferred
      until an estimator is proven profitable on paper.*

## Layout

```
pipeline/
  models.py          Market, Opportunity, Signal, Venue, Side, OppStatus, SourceTier
  store.py           SQLite store (markets, snapshots, opportunities, positions, signals, links)
  collect.py         CLI: pull all venues → DB        (python -m pipeline.collect)
  scan.py            CLI: estimators → size → guardrails → paper bets  (python -m pipeline.scan)
  report.py          CLI: read-only dashboard         (python -m pipeline.report)
  learn.py           CLI: resolution + Brier/P&L scoring (python -m pipeline.learn)
  decide.py          Opus decision/veto layer (final gate before execution)
  sizing.py          fractional-Kelly position sizing
  guardrails.py      risk layer (caps, kill-switch, daily-loss limit)
  rootpath.py        makes repo-root modules importable from the package
  collectors/        polymarket.py · kalshi.py (events endpoint) · manifold.py
  edges/             calibration.py (longshot curve/model) · arb.py (within-market + x-venue)
  estimators/        llm.py — Sonnet per-market probability estimator (Phase 3)
  signals/           news.py (RSS/official collector) · relevance.py (signal→market matcher)
  tests/             test_sizing.py · test_guardrails.py (19 tests)
```

### Phase 3 information-edge flow
```
python -m pipeline.signals.news        # ingest headlines/releases → signals table
python -m pipeline.signals.relevance   # link signals → markets (cheap, no LLM)
# then the LLM estimator (needs ANTHROPIC_API_KEY) runs inside the scan:
#   select markets with fresh relevant signals → Sonnet estimate → Opus decide
```

## Run

```bash
python -m pipeline.collect                 # all venues → pipeline.db
python -m pipeline.collect --venue kalshi --limit 1000
python -m pipeline.collect --no-snapshot   # skip price-history append
```

`pipeline.db` (SQLite, gitignored) is the single source of truth for pipeline state.
Polymarket is US-geoblocked — collection assumes a non-US VPN exit is up.
