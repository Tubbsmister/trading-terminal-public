# trading-terminal (public showcase)

A research pipeline for finding **positive-expected-value bets on prediction
markets** (Polymarket, Kalshi, Manifold). It scans markets across venues, gathers
evidence from primary/official and news sources, and uses an adversarial two-model
LLM process to form and stress-test falsifiable trade theses before sizing them.

> This is a **sanitized public snapshot** of a larger private project — the core
> engine only. Live-execution wallet tooling, deployment specifics, and internal
> performance journals are intentionally omitted. No credentials are included;
> everything sensitive is read from environment variables at runtime.

## How it works

```
collect ─▶ signals ─▶ relevance ─▶ triage ─▶ research ─▶ generate ─▶ red-team ─▶ size/guardrail
 (venues)  (news/X)    (link to      (rank    (evidence)  (thesis)    (critic)     (Kelly + caps)
                        markets)     markets)
```

1. **Collect** — normalize active binary markets from each venue into one schema.
2. **Signals** — ingest primary/official releases (SEC, Fed, BLS, Federal Register)
   and news/social feeds as tiered `Signal`s.
3. **Relevance** — link each signal to the markets it might move (term-overlap with
   rarity weighting and a corroboration threshold).
4. **Triage** — rank markets by tier-weighted signal mass and category affinity, so
   constraint-rich markets (policy/econ/regulatory) outrank game-outcome markets.
5. **Thesis engine** — a *generator* forms a concrete, falsifiable thesis (the
   strongest rest on a checkable constraint the crowd overlooked — a deadline, a
   required procedure, a rule's effective date). A deterministic grounding gate
   rejects fabricated dates/amounts, then a *critic* red-teams the thesis against
   five hard gates and five scored dimensions. Only survivors become sized bets.
6. **Sizing & guardrails** — quarter-Kelly with fractional caps and exposure limits.
7. **Monitor & fast-watch** — re-tests open positions against fresh evidence and
   fires a fast price-move trip-wire so a broken thesis is caught in minutes.

## Layout

| Path | What |
|------|------|
| `pipeline/collectors/` | per-venue market collectors |
| `pipeline/signals/` | news / official / social (incl. X watchlist) ingestion |
| `pipeline/thesis.py` | the generator + adversarial critic engine |
| `pipeline/research.py` | evidence gathering (signals + web) |
| `pipeline/sizing.py`, `guardrails.py` | Kelly sizing + risk caps |
| `pipeline/monitor.py`, `fast_watch.py` | open-position risk management |
| `pipeline/llm_backend.py` | pluggable LLM backend (local / API / CLI) |
| `pipeline/store.py` | SQLite storage layer |
| `pipeline/tests/` | unit tests |

## Design notes

- **Backend-agnostic LLM layer.** The same code runs on a free local model, the
  Anthropic API, or the Claude CLI — selected at runtime, degrading gracefully.
- **Math-floor-over-LLM.** Wherever a check can be deterministic (fact grounding,
  price trip-wires), it is — the LLM reasons, but doesn't get to hallucinate past
  a hard gate.
- **Adversarial by construction.** A thesis must survive a critic whose default is
  to kill it; a position must survive continuous re-evaluation for a thesis-breaker.
- **Auditable.** Every thesis records its evidence, causal chain, and the critic's
  scorecard, so any bet can be explained after the fact.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in venue/LLM config
python -m pipeline.run_cycle  # one collect -> ... -> thesis pass
python -m pytest pipeline/tests/ -q
```

## Disclaimer

For research and educational purposes. Nothing here is financial advice. Prediction
markets carry real risk of loss.
