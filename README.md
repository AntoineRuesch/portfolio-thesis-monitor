# Portfolio Thesis Monitor

**A daily AI-powered pipeline that reads financial news and tells you whether your investment theses are holding — position by position, in CHF.**

Most tools show you what the market did. This one reads every relevant article published in the last 30 days and asks: does this news support or challenge the specific reason you own this position? The output is not a dashboard of price charts — it is a structured analyst memo with a per-position verdict (🟢 ON TRACK / 🟡 MONITOR / 🔴 REASSESS), concrete watch points, and portfolio-level risk flags you can act on.

---

## The Problem

Generic market news is produced at enormous volume and designed for general audiences, not for investors with specific, articulated reasons for owning each position. An article about rising US interest rates means something very different for an unhedged S&P 500 ETF than for a CHF-hedged global bond fund — but a news aggregator treats both equally. Without a system that evaluates each piece of news against each position's thesis, investors end up with information overload that produces no conviction change in either direction.

The deeper problem is that most portfolio monitoring tools track performance, not thesis validity. A position can be down 10% while its thesis is completely intact (a temporary market selloff), or up 5% while the thesis has quietly been invalidated by three macro developments that flew under the radar. Price and thesis status are correlated but distinct, and conflating them leads to poor decisions — selling solid positions during drawdowns and holding deteriorating ones because they look green.

---

## How It Works

```
config/portfolio.yaml  (thesis definitions: bull signals, bear signals, risk factors)
          ↓
[data/]  market.py + fx.py + news.py
          ↓
[analysis/]  thesis_evaluator.py  →  exposure_analyzer.py
          ↓
[report/]  memo_generator.py
          ↓
output/memo_YYYY-MM-DD.md
```

**`data/market.py`** fetches 30-day price history for every position via yfinance — no API key required. It computes 1D / 7D / 30D percentage changes and average volume, and flags positions with insufficient data so the memo can surface data quality issues transparently.

**`data/fx.py`** fetches EUR/CHF and USD/CHF rates via yfinance. Because the entire portfolio is held in CHF, every position analysis anchors to these rates. An unhedged USD position gaining 3% in USD terms may be losing money in CHF terms if the dollar weakened — this module makes that visible.

**`data/news.py`** implements a 4-provider fallback chain (Marketaux → Alpha Vantage → GNews → NewsAPI) that tries each source in order until at least 3 relevant articles are found per position. Each position has a `proxy_tickers` field for well-known US equivalents (e.g. SPY for CSPX.L) because European ETF tickers are often unknown to US-centric news APIs. All articles are normalised to a common schema and tagged with their source and fetch method.

**`analysis/thesis_evaluator.py`** sends each article to Claude (Haiku model) with the position's thesis, bull signals, bear signals, and risk factors as context. Claude returns structured JSON classifying every article as SUPPORTS / NEUTRAL / WEAKENS and SIGNAL / NOISE, with a 2-3 sentence reasoning and a concrete watch point. The `overall_thesis_status` is then derived from the pattern of results: ON TRACK if no weakening signals, MONITOR if 1-2 weakening signals without high confidence, REASSESS if 3+ weakening signals or at least one HIGH confidence WEAKENS.

**`analysis/exposure_analyzer.py`** performs portfolio-level structural analysis that does not depend on news: currency exposure breakdown (USD unhedged / EUR unhedged / CHF hedged), geographic concentration using hardcoded index composition knowledge (MSCI ACWI = ~65% US), ETF overlap detection, portfolio gap identification (missing asset classes), and concentration flags. Position outlooks are then built dynamically by combining thesis evaluation results with the bull/bear signals defined in `portfolio.yaml`.

**`report/memo_generator.py`** assembles all module outputs into a single Markdown file. Every section is generated from live data — nothing is hardcoded in the report layer. Data quality issues (quota errors, keyword-only news sourcing, insufficient market data) are surfaced in a dedicated section at the bottom so the reader always knows the reliability of each output.

---

## Output: Daily Memo Structure

- **Market Snapshot** — prices and % changes for all positions, FX rates with 30D range, all in CHF context
- **Portfolio Risk Overview** — currency exposure breakdown, geographic concentration, ETF overlaps, portfolio gaps, concentration flags with severity
- **Position Analysis** — per-position thesis status (🟢/🟡/🔴), top signal articles in a table, key reasoning quote
- **Position Outlook** — watch points extracted from Claude's `next_to_watch` field, bull and bear scenarios built from live signals + thesis definitions, CHF angle with live FX rate
- **What To Research Next** — numbered diversification suggestions pulled from portfolio gap analysis and high-severity flags
- **Data Quality Notes** — transparent disclosure of which providers were used, which failed or were quota-limited, and any data reliability concerns

---

## Tech Stack

| Component | Tool | Why |
|-----------|------|-----|
| Market data | yfinance | Free, reliable, no quota limits |
| News | Marketaux + Alpha Vantage + GNews + NewsAPI | Multi-provider chain avoids single-point-of-failure on free tier quotas |
| AI reasoning | Claude API (Haiku) | Structured JSON classification, context-aware, cost-efficient at ~$0.001 per article |
| Config | YAML | Human-readable thesis definitions, version-controllable, no code changes needed to update positions |
| Output | Markdown | Readable anywhere, renderable on GitHub, version-controllable as a journal |

---

## Setup

### 1. Install

```bash
cd portfolio-thesis-monitor
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API keys

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```
ANTHROPIC_API_KEY=your_anthropic_key_here
MARKETAUX_API_KEY=your_marketaux_key_here
ALPHA_VANTAGE_API_KEY=your_alpha_vantage_key_here
GNEWS_API_KEY=your_gnews_key_here
NEWS_API_KEY=your_newsapi_key_here
```

All providers are optional except `ANTHROPIC_API_KEY`. The news pipeline activates whichever providers have keys and skips the rest. The `--dry-run` flag bypasses news and Claude entirely if no keys are available.

### 3. Define your portfolio

Edit `config/portfolio.yaml` with your positions and thesis definitions. Each position requires:

```yaml
- id: my_position
  name: "Full ETF Name"
  ticker: "TICKER.EXCHANGE"
  asset_class: etf          # etf | commodity | bond | reit
  region: Europe            # Europe | USA | Global
  currency: EUR             # base currency of the instrument
  hedged: false             # true if CHF-hedged
  proxy_tickers: ["FEZ"]   # US equivalents for news APIs
  search_terms:
    - "relevant keyword"
  thesis: >
    Why you own this position.
  bull_signals:
    - What would confirm the thesis
  bear_signals:
    - What would challenge the thesis
  risk_factors:
    - Known structural risks
```

### 4. Run

```bash
# Full pipeline
python main.py

# Skip news + AI when API quota is exhausted
python main.py --dry-run

# Run individual modules for debugging
python data/market.py
python data/fx.py
python data/news.py
python analysis/thesis_evaluator.py
python analysis/exposure_analyzer.py
python report/memo_generator.py
```

---

## Design Decisions

**YAML for thesis definitions.** Investment theses are living documents — they evolve as macro conditions change, as positions are added or removed, and as conviction levels shift. By keeping thesis definitions in a human-readable YAML file rather than in Python code or a database, they can be edited directly, committed to git (creating a journal of how your thinking evolved), and reviewed without opening a code editor. The separation of config from code also means the analysis modules never need to change when a position changes.

**Claude as the reasoning layer.** Keyword matching fails at the nuance thesis-driven investing requires. The same article about "rising US interest rates" is bearish for an unhedged equity ETF but potentially bullish for a short-duration bond position — context that a word-frequency model cannot resolve. Claude reads the thesis, the bull and bear signals, and the article together, and produces a classification that accounts for that context. The SIGNAL vs NOISE distinction is particularly valuable: it filters out routine market updates that are technically relevant but carry no new information, keeping the watch list focused on events that actually matter.

**Equal weighting assumption.** The system deliberately does not ask for real portfolio weights. Real allocation data is sensitive, changes frequently, and would require either a broker API integration or manual updates to stay accurate. Equal weighting gives a directionally correct risk picture — the structural insights about currency exposure, geographic concentration, and ETF overlaps hold regardless of whether a position is 20% or 30% of the portfolio — while keeping the setup to a single YAML file. The weight assumption is disclosed prominently in every memo so the reader always knows the basis.

**Multi-provider news fallback chain.** Every news API on a free tier has daily quota limits. A system that depends on a single provider will fail silently the moment the quota is hit — returning zero articles while the pipeline appears to succeed, producing an ON TRACK verdict by default rather than acknowledging it has no data. The four-provider chain (Marketaux → Alpha Vantage → GNews → NewsAPI) ensures that quota exhaustion on any single provider immediately triggers the next one. Provider status is logged at startup, quota errors are detected by HTTP status code, and the memo's Data Quality Notes section discloses which fallbacks were used and which providers failed.

---

## Limitations

- No backtesting or historical validation of thesis accuracy — the system cannot tell you whether previous REASSESS verdicts were justified in hindsight
- News coverage depends on free tier quotas; on days when all providers are quota-limited, news sections will be empty and thesis evaluations will default to ON TRACK
- Equal weighting assumption may not reflect real allocation — currency and geographic exposure figures are indicative, not precise
- Not a buy/sell signal generator — all output is for research and reflection purposes only, not financial advice
- ETF holdings overlap is estimated using hardcoded index composition knowledge, not computed from live holdings data
- Claude evaluations are only as good as the news summaries provided — paywalled or poorly summarised articles will produce low-quality classifications

---

## Roadmap

- Automated daily scheduling (cron / GitHub Actions) so the memo is generated without manual runs
- Live ETF holdings data for precise overlap calculation instead of hardcoded index composition
- Thesis accuracy tracking over time — was REASSESS actually followed by underperformance?
- Web UI for thesis management (add/edit/archive positions without editing YAML)

---

## Sample Output

See `output/memo_sample.md` for an example of a generated memo.
