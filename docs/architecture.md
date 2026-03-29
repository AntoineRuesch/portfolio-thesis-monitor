# Architecture & Design Documentation

## Data Flow

The system is a linear pipeline. Each stage produces a typed dict that the next stage consumes. No stage writes to disk except the final report generator. All inter-stage communication is in-memory, which means the full pipeline can run as a single Python process with no message queue, database, or intermediate files.

```
config/portfolio.yaml
  └─ Loaded once at startup by each module that needs it.
     Provides: positions, fx_pairs, lookback_days, thesis definitions.

data/market.py  →  market_data: dict[ticker, PositionData]
  └─ yfinance fetch: 30-day OHLCV per ticker.
     Computes: current_price, change_1d/7d/30d_pct, avg_volume_30d, history DataFrame.
     Flags: insufficient_data (< 5 rows), error (fetch failed).

data/fx.py  →  fx_data: dict[pair, FXData]
  └─ yfinance fetch: EUR/CHF and USD/CHF 30-day history.
     Computes: rate, change_1d/7d/30d_pct, range_30d {min, max}.

data/news.py  →  news_data: dict[position_id, list[Article]]
  └─ Multi-provider fallback chain (see below).
     Each article normalised to common schema.
     Deduplicated by title, sorted by published_at descending.

analysis/thesis_evaluator.py  →  evaluations: dict[position_id, EvalResult]
  └─ One Claude API call per article.
     Each call returns: classification, confidence, signal_type,
     reasoning, chf_impact, next_to_watch.
     Aggregates to per-position summary with overall_thesis_status.

analysis/exposure_analyzer.py  →  exposure: dict
  └─ Pure computation, no API calls.
     Uses hardcoded index composition knowledge + portfolio.yaml.
     Produces: currency_exposure, geographic_concentration,
     etf_overlaps, portfolio_gaps, concentration_flags,
     overall_risk_score, position_outlooks.

report/memo_generator.py  →  output/memo_YYYY-MM-DD.md
  └─ Assembles all dicts into structured Markdown.
     Every section is data-driven; nothing hardcoded in the template.
     Writes to output/ directory. Overwrites same-day file on re-run.
```

---

## Claude Prompt (thesis_evaluator.py)

The following prompt is injected once per article, with the six variables filled from `portfolio.yaml` and the article dict:

```
You are a financial analyst assistant evaluating whether a piece of
news is relevant to a specific investment thesis.

POSITION: {position_name}
THESIS: {thesis}
BULL SIGNALS TO WATCH: {bull_signals}
BEAR SIGNALS TO WATCH: {bear_signals}
RISK FACTORS: {risk_factors}
BASE CURRENCY: CHF

NEWS ARTICLE:
Title: {title}
Summary: {summary}
Published: {published_at}

Evaluate this news against the thesis and respond ONLY with valid JSON,
no other text, no markdown backticks:
{
  "classification": "SUPPORTS" | "NEUTRAL" | "WEAKENS",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "signal_type": "SIGNAL" | "NOISE",
  "reasoning": "<2-3 sentences max explaining why>",
  "chf_impact": "<one sentence on CHF currency angle if relevant, else null>",
  "next_to_watch": "<one concrete thing to monitor as a result>"
}

Rules:
- SUPPORTS: news clearly strengthens the investment case
- WEAKENS: news clearly challenges or invalidates the thesis
- NEUTRAL: news is tangentially related but doesnt move the needle
- SIGNAL: meaningful new information worth tracking
- NOISE: routine, already priced in, or too vague to matter
- Keep reasoning factual, no investment advice
- chf_impact is mandatory for unhedged positions
```

**Model:** `claude-haiku-4-5-20251001`
**max_tokens:** 500
**Temperature:** default (not overridden — determinism not critical for classification tasks)

The response is parsed with `json.loads()`. If parsing fails (Claude occasionally wraps output in markdown fences despite the instruction), the code strips ` ```json ` / ` ``` ` wrappers and retries. If the second parse also fails, the article is stored with `parse_error: true` and excluded from all counts and status calculations.

---

## Classification Logic

### SUPPORTS / NEUTRAL / WEAKENS

These labels represent the article's relationship to the **investment thesis** — not to the price action of the underlying asset.

| Label | Meaning | Example |
|-------|---------|---------|
| `SUPPORTS` | News clearly strengthens the reason to own the position | ECB cuts rates → bullish for STOXX 600 thesis built on rate-cut recovery |
| `WEAKENS` | News clearly challenges or invalidates the thesis | Eurozone enters recession → directly contradicts recovery thesis |
| `NEUTRAL` | News is related but does not move the conviction needle | Routine ECB meeting recap with no policy change |

The key distinction: a position can be **down in price** while its thesis `SUPPORTS` signal fires (e.g. a temporary market selloff), and can be **up in price** while a `WEAKENS` signal fires (e.g. the position rallied on unrelated factors while a thesis pillar quietly deteriorated). Price and thesis validity are tracked separately by design.

### SIGNAL / NOISE

This second dimension filters out articles that are technically relevant but carry no actionable new information.

| Label | Meaning | Example |
|-------|---------|---------|
| `SIGNAL` | Meaningful new information worth tracking — changes the picture | Goldman issues an EM downgrade that directly contradicts a bull signal |
| `NOISE` | Routine, already priced in, or too vague to matter | "Markets were mixed today amid ongoing uncertainty" |

`SIGNAL` articles populate the thesis signals table in the memo. `NOISE` articles are still counted in the totals (for transparency) but do not generate watch points or influence the bull/bear scenario text.

### Confidence

`HIGH / MEDIUM / LOW` reflects Claude's certainty that the classification is correct given the information in the article. A `HIGH` confidence `WEAKENS` is weighted more heavily in the `overall_thesis_status` logic than a `LOW` confidence one.

---

## overall_thesis_status Decision Rules

```python
weakens      = [e for e in evaluations if classification == "WEAKENS" and not parse_error]
high_weakens = [e for e in weakens     if confidence == "HIGH"]

if high_weakens or len(weakens) >= 3:
    return "REASSESS"
elif len(weakens) in (1, 2):
    return "MONITOR"
else:
    return "ON TRACK"
```

**Rationale for thresholds:**

- **REASSESS** triggers on a single `HIGH` confidence `WEAKENS` because a high-confidence signal that directly challenges the thesis is material regardless of how many supporting articles exist. Volume of SUPPORTS does not cancel out a high-conviction bear signal.
- **REASSESS** also triggers on 3+ `WEAKENS` (any confidence) because a pattern of weakening signals — even individually uncertain ones — constitutes a structural shift worth reassessing.
- **MONITOR** is the intermediate state: something is happening, but it is not yet clear whether the thesis is broken or the signal is temporary. The investor should increase attention without acting.
- **ON TRACK** simply means no `WEAKENS` signals were detected in the articles reviewed. It does not mean the thesis is confirmed — only that no evidence against it surfaced in this period's news.

**Important limitation:** `ON TRACK` is the default when no news is available (zero articles fetched). The memo's Data Quality Notes section discloses this case so the reader does not confuse "no news" with "all clear."

---

## News Provider Fallback Chain

The chain is attempted in order for each position until `MIN_ARTICLES = 3` are found. Providers without an API key in `.env` are skipped silently at startup.

```
For each position:

  [1] Marketaux — primary_ticker
      GET /v1/news/all?symbols={ticker}
      → if ≥ 3 results: DONE (fetched_via = "primary_ticker")

  [2] Marketaux — proxy_tickers
      GET /v1/news/all?symbols={proxy1},{proxy2}
      → if ≥ 3 results: DONE (fetched_via = "proxy_ticker")
      Note: European ETF tickers (EXSA.DE, CSPX.L) are often unknown
      to US-centric news APIs. proxy_tickers provides US equivalents
      (FEZ, SPY) that have identical news coverage.

  [3] Marketaux — keyword_search
      GET /v1/news/all?search={term1} OR {term2} OR {term3}
      → if ≥ 3 results: DONE (fetched_via = "keyword_search")

  If Marketaux returns a 402 or 429 on any step:
      log "Provider marketaux quota exceeded — trying next provider"
      skip ALL remaining Marketaux steps immediately

  [4] Alpha Vantage — ticker
      GET /query?function=NEWS_SENTIMENT&tickers={ticker}
      → if ≥ 3 results: DONE

  [5] Alpha Vantage — keyword_search
      GET /query?function=NEWS_SENTIMENT&tickers={keywords}
      → if ≥ 3 results: DONE

  If Alpha Vantage returns "Information" or "Note" key in response body:
      log rate-limit warning, skip provider

  [6] GNews — keyword_search
      GET /api/v4/search?q="{term1}" OR "{term2}" OR "{term3}"
      → if ≥ 3 results: DONE

  [7] NewsAPI — keyword_search  [LAST RESORT]
      GET /v2/everything?q={terms}
      All articles tagged: data_delay_warning=true
                           data_delay_note="Sourced via NewsAPI free tier — 24h delay"
      → whatever results are available: DONE
```

**Quota error detection** checks both HTTP status codes (`402`, `429`) and response body keywords (`quota`, `rate limit`, `exceeded`, `upgrade`, `payment required`). This handles providers that return `200 OK` with an error JSON body — a common pattern on free tier APIs.

**Article normalisation** maps each provider's schema to a common dict:

```python
{
  "title":              str,
  "summary":            str,
  "url":                str,
  "published_at":       str,   # ISO 8601, e.g. "2024-01-15T10:30:00+00:00"
  "source":             str,
  "relevance_score":    float | None,
  "source_api":         "marketaux" | "alpha_vantage" | "gnews" | "newsapi",
  "fetched_via":        "primary_ticker" | "proxy_ticker" | "keyword_search",
  "data_delay_warning": bool,
  "data_delay_note":    str | None
}
```

Deduplication uses `title.strip().lower()` as the key. Sorting is by `published_at` descending (most recent first).

---

## Currency Exposure Calculation

Currency exposure is computed from `portfolio.yaml` fields, assuming equal weighting:

```
weight = 100% / number_of_positions

for each position:
    if hedged == true:      CHF_hedged   += weight
    elif currency == "USD": USD_unhedged += weight
    elif currency == "EUR": EUR_unhedged += weight
    else:                   CHF_hedged   += weight  # default
```

**10% stress test** is computed as `USD_unhedged * 0.10` to show the portfolio-level CHF impact of a 10% USD depreciation. This is reported in the currency commentary and used to calibrate the `HIGH USD UNHEDGED EXPOSURE` concentration flag.

---

## Geographic Concentration Calculation

Uses hardcoded index composition knowledge embedded in `exposure_analyzer.py`:

```python
_US_EQUITY_FRACTION = {
    "sp500":                  1.00,   # 100% US equities
    "global_acwi_chf_hedged": 0.65,   # MSCI ACWI ≈ 65% US
    "europe_stoxx600":        0.00,   # STOXX 600 → 0% US
    "gold":                   0.00,   # commodity
}
```

For Global positions (ACWI), the weight is split: `weight * us_fraction → USA`, `weight * (1 - us_fraction) → Global_EM`. Commodity positions are tracked as a separate bucket.

**Update requirement:** if index composition changes materially (e.g. MSCI rebalancing shifts US weight to 60%), `_US_EQUITY_FRACTION` must be updated manually. This is a known limitation of the static approach.

---

## Overall Risk Score Thresholds

```python
usd_pct    = currency_exposure["USD_unhedged_pct"]
max_geo    = max(geographic_concentration.values())  # excluding commentary key
high_flags = count(flags where severity == "HIGH")

if usd_pct > 50 or max_geo > 60 or high_flags >= 2:
    score = "HIGH"
elif usd_pct >= 30 or max_geo >= 40 or high_flags >= 1:
    score = "MODERATE"
else:
    score = "LOW"
```

The thresholds are intentionally conservative for a CHF-based investor: 50% USD unhedged is flagged as HIGH because a 10% USD/CHF move (historically common) represents a 5% direct portfolio impact before any equity performance is considered.
