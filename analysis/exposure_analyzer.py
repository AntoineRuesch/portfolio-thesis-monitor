"""
analysis/exposure_analyzer.py
------------------------------
Portfolio-level risk analysis: currency exposure, geographic concentration,
ETF overlaps, portfolio gaps, concentration flags, and position outlooks
driven by live thesis evaluation data.
"""

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"

# ---------------------------------------------------------------------------
# Hardcoded structural knowledge about the underlying indices
# ---------------------------------------------------------------------------

# Fraction of each position_id that is ultimately US-equity exposure
_US_EQUITY_FRACTION: dict[str, float] = {
    "sp500":                  1.00,   # 100% US
    "global_acwi_chf_hedged": 0.65,   # MSCI ACWI ≈ 65% US equities
    "europe_stoxx600":        0.00,   # STOXX 600 → 0% US
    "gold":                   0.00,   # commodity, not equity
}

# Confidence ordering used when sorting evaluations
_CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config(config_path: Path = CONFIG_PATH) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _positions_list(cfg: dict) -> list[dict]:
    return cfg["portfolio"]["positions"]


# ---------------------------------------------------------------------------
# Currency exposure
# ---------------------------------------------------------------------------

def _currency_exposure(positions: list[dict], weight: float) -> dict:
    """
    Compute portfolio currency exposure assuming equal weighting.

    hedged: true  → counts as CHF
    hedged: false + currency USD → USD unhedged
    hedged: false + currency EUR → EUR unhedged
    """
    usd_pct = 0.0
    eur_pct = 0.0
    chf_pct = 0.0

    for pos in positions:
        if pos.get("hedged"):
            chf_pct += weight
        elif pos.get("currency", "").upper() == "USD":
            usd_pct += weight
        elif pos.get("currency", "").upper() == "EUR":
            eur_pct += weight
        else:
            chf_pct += weight  # default to CHF if unknown

    impact_10pct_drop = round(usd_pct * 0.10, 1)

    commentary = (
        f"{usd_pct:.0f}% of portfolio exposed to USD/CHF movements unhedged "
        f"(S&P 500 + Gold). "
        f"A 10% USD/CHF depreciation would impact portfolio value by "
        f"approximately {impact_10pct_drop:.0f}%."
    )
    if eur_pct > 0:
        commentary += (
            f" {eur_pct:.0f}% is EUR-denominated (STOXX 600) and also unhedged — "
            f"EUR/CHF movements directly affect returns on that leg."
        )

    return {
        "USD_unhedged_pct": round(usd_pct, 1),
        "EUR_unhedged_pct": round(eur_pct, 1),
        "CHF_hedged_pct":   round(chf_pct, 1),
        "commentary": commentary,
    }


# ---------------------------------------------------------------------------
# Geographic concentration
# ---------------------------------------------------------------------------

def _geographic_concentration(positions: list[dict], weight: float) -> dict:
    """
    Break down portfolio by geography using hardcoded index composition knowledge.
    Returns percentage points allocated to each region.
    """
    buckets: dict[str, float] = {}

    for pos in positions:
        pos_id   = pos["id"]
        region   = pos.get("region", "Global")
        us_frac  = _US_EQUITY_FRACTION.get(pos_id, 0.0)

        if pos.get("asset_class") == "commodity":
            buckets["Commodity (Gold)"] = buckets.get("Commodity (Gold)", 0.0) + weight
        elif region == "USA" or us_frac == 1.0:
            buckets["USA"] = buckets.get("USA", 0.0) + weight
        elif region == "Europe":
            buckets["Europe"] = buckets.get("Europe", 0.0) + weight
        elif region == "Global":
            # Split ACWI: 65% US, 35% rest-of-world
            us_share    = weight * us_frac
            other_share = weight * (1.0 - us_frac)
            buckets["USA"]       = buckets.get("USA", 0.0) + us_share
            buckets["Global_EM"] = buckets.get("Global_EM", 0.0) + other_share
        else:
            buckets[region] = buckets.get(region, 0.0) + weight

    buckets = {k: round(v, 1) for k, v in buckets.items()}

    usa_pct = buckets.get("USA", 0.0)
    commentary = (
        f"Portfolio is US-heavy at {usa_pct:.0f}% (direct S&P 500 + ~65% US weight "
        f"inside MSCI ACWI). Consider whether this aligns with your diversification "
        f"objective given the USD/CHF unhedged exposure on the S&P 500 and Gold legs."
    )

    result = dict(buckets)
    result["commentary"] = commentary
    return result


# ---------------------------------------------------------------------------
# ETF overlaps
# ---------------------------------------------------------------------------

def _etf_overlaps(positions: list[dict]) -> list[dict]:
    pos_ids  = {p["id"] for p in positions}
    overlaps = []

    if "sp500" in pos_ids and "global_acwi_chf_hedged" in pos_ids:
        overlaps.append({
            "positions": ["sp500", "global_acwi_chf_hedged"],
            "overlap_reason": (
                "MSCI ACWI contains ~65% US equities, creating meaningful overlap "
                "with the direct S&P 500 holding. Both positions will move in the "
                "same direction during US equity drawdowns."
            ),
            "estimated_overlap_pct": 65,
        })

    if "europe_stoxx600" in pos_ids and "global_acwi_chf_hedged" in pos_ids:
        overlaps.append({
            "positions": ["europe_stoxx600", "global_acwi_chf_hedged"],
            "overlap_reason": (
                "MSCI ACWI contains ~15% European equities. Minor overlap with "
                "the dedicated STOXX 600 position, not material enough to flag "
                "as a concentration concern."
            ),
            "estimated_overlap_pct": 15,
        })

    return overlaps


# ---------------------------------------------------------------------------
# Portfolio gaps
# ---------------------------------------------------------------------------

def _portfolio_gaps(positions: list[dict]) -> list[dict]:
    asset_classes = {p.get("asset_class", "") for p in positions}
    gaps          = []

    # Fixed income
    if "bond" not in asset_classes:
        gaps.append({
            "gap": "Fixed income / bonds",
            "reason": (
                "No bond exposure. Portfolio is 100% equity + commodity, "
                "increasing drawdown risk in risk-off scenarios where correlations "
                "between equity positions spike."
            ),
            "research_suggestion": (
                "A CHF-hedged global bond ETF (e.g. iShares Global Aggregate Bond "
                "CHF Hedged) could reduce overall portfolio volatility and provide "
                "a return buffer during equity selloffs."
            ),
        })

    # REITs
    if "reit" not in asset_classes:
        gaps.append({
            "gap": "Real estate / REITs",
            "reason": "No real estate exposure in the current portfolio.",
            "research_suggestion": (
                "A global REIT ETF hedged to CHF could add income, inflation "
                "protection, and low correlation to pure equity beta."
            ),
        })

    # EM pure play — only flag if EM solely via ACWI
    has_dedicated_em = any(
        p["id"] != "global_acwi_chf_hedged" and "Global" in p.get("region", "")
        for p in positions
    )
    if not has_dedicated_em and any(p["id"] == "global_acwi_chf_hedged" for p in positions):
        gaps.append({
            "gap": "Emerging markets pure play",
            "reason": (
                "EM exposure only via MSCI ACWI (~15% weight within a 25% position "
                "→ ~3.75% effective EM exposure). Very limited direct access to "
                "India, Brazil, and Southeast Asia growth."
            ),
            "research_suggestion": (
                "A dedicated EM ETF (e.g. iShares MSCI EM IMI) would improve "
                "diversification beyond developed markets and allow independent "
                "sizing of the EM bet."
            ),
        })

    # Inflation protection
    has_gold = any(p.get("asset_class") == "commodity" for p in positions)
    has_tips  = any("tip" in p.get("name", "").lower() for p in positions)
    if not has_tips:
        note = (
            "Gold provides a partial inflation hedge but offers no TIPS or "
            "broad commodity basket exposure. "
        ) if has_gold else (
            "No inflation protection assets in the portfolio. "
        )
        gaps.append({
            "gap": "Inflation protection (TIPS / commodity basket)",
            "reason": note + "Rising real yields remain a risk to the portfolio.",
            "research_suggestion": (
                "A TIPS ETF or broad commodity basket ETF (e.g. iShares Diversified "
                "Commodity Swap) could complement the gold position for broader "
                "inflation hedging."
            ),
        })

    return gaps


# ---------------------------------------------------------------------------
# Concentration flags
# ---------------------------------------------------------------------------

def _concentration_flags(
    currency_exp: dict,
    geo_conc:     dict,
    positions:    list[dict],
) -> list[dict]:
    flags         = []
    usd           = currency_exp.get("USD_unhedged_pct", 0.0)
    eur           = currency_exp.get("EUR_unhedged_pct", 0.0)
    asset_classes = {p.get("asset_class", "") for p in positions}

    if usd >= 40:
        flags.append({
            "flag":     "HIGH USD UNHEDGED EXPOSURE",
            "severity": "HIGH" if usd >= 50 else "MEDIUM",
            "detail": (
                f"S&P 500 + Gold combine for ~{usd:.0f}% USD exposure with no CHF hedge. "
                f"Significant sensitivity to USD/CHF depreciation. A 10% USD/CHF drop "
                f"costs the portfolio approximately {usd * 0.10:.1f} percentage points."
            ),
        })

    if eur >= 20:
        flags.append({
            "flag":     "EUR UNHEDGED EXPOSURE",
            "severity": "MEDIUM",
            "detail": (
                f"{eur:.0f}% of portfolio in EUR with no hedge. EUR/CHF weakness "
                f"directly reduces the CHF-denominated return of STOXX 600."
            ),
        })

    if "bond" not in asset_classes:
        flags.append({
            "flag":     "ZERO FIXED INCOME",
            "severity": "MEDIUM",
            "detail": (
                "100% risk assets (equity + commodity). No buffer in equity drawdown "
                "scenarios. Portfolio max drawdown will be higher than a balanced allocation."
            ),
        })

    for region, pct in geo_conc.items():
        if region == "commentary":
            continue
        if pct >= 60:
            flags.append({
                "flag":     f"GEOGRAPHIC CONCENTRATION — {region.upper()}",
                "severity": "HIGH",
                "detail":   f"{pct:.0f}% of portfolio allocated to {region}. High single-region risk.",
            })
        elif pct >= 45:
            flags.append({
                "flag":     f"ELEVATED {region.upper()} WEIGHT",
                "severity": "MEDIUM",
                "detail":   f"{pct:.0f}% in {region}. Consider whether this is intentional.",
            })

    return flags


# ---------------------------------------------------------------------------
# Overall risk score
# ---------------------------------------------------------------------------

def _risk_score(
    currency_exp: dict,
    geo_conc:     dict,
    flags:        list[dict],
) -> tuple[str, str]:
    usd       = currency_exp.get("USD_unhedged_pct", 0.0)
    max_geo   = max((v for k, v in geo_conc.items() if k != "commentary"), default=0.0)
    n_flags   = len(flags)
    high_flags = sum(1 for f in flags if f.get("severity") == "HIGH")

    if usd > 50 or max_geo > 60 or high_flags >= 2:
        score = "HIGH"
        commentary = (
            "Portfolio carries elevated risk: high USD/CHF unhedged exposure and "
            "geographic concentration in US equities. Suitable only if comfortable "
            "with currency volatility and equity-only drawdowns."
        )
    elif usd >= 30 or max_geo >= 40 or n_flags >= 2:
        score = "MODERATE"
        commentary = (
            "Portfolio is reasonably diversified geographically but carries meaningful "
            "USD/CHF unhedged exposure and lacks fixed income as a stabilizer. "
            "Suitable for long-term growth but vulnerable to simultaneous USD weakness "
            "and equity drawdowns."
        )
    else:
        score = "LOW"
        commentary = (
            "Portfolio is well-diversified with limited currency risk and broad geographic "
            "spread. Conservative risk profile suitable for most long-term investors."
        )

    return score, commentary


# ---------------------------------------------------------------------------
# Position outlooks  (dynamic — driven by evaluations)
# ---------------------------------------------------------------------------

def _build_position_outlook(pos: dict, eval_data: dict) -> dict:
    summary       = eval_data.get("summary", {})
    evaluations   = eval_data.get("evaluations", [])
    thesis_status = summary.get("overall_thesis_status", "ON TRACK")

    clean = [e for e in evaluations if not e.get("parse_error")]

    def _sort_confidence(e: dict) -> int:
        return _CONFIDENCE_RANK.get(e.get("confidence", "LOW"), 0)

    weakens_evals  = sorted(
        [e for e in clean if e.get("classification") == "WEAKENS"],
        key=_sort_confidence, reverse=True,
    )
    supports_evals = sorted(
        [e for e in clean if e.get("classification") == "SUPPORTS"],
        key=_sort_confidence, reverse=True,
    )
    signal_evals   = [e for e in clean if e.get("signal_type") == "SIGNAL"]

    # key_signals_this_period
    n_signals = len(signal_evals)
    n_weakens = len(weakens_evals)
    n_supports = len(supports_evals)
    high_w = [e for e in weakens_evals if e.get("confidence") == "HIGH"]

    if n_signals == 0:
        key_signals = "No actionable signals detected this period — mostly noise."
    else:
        parts = []
        if high_w:
            parts.append(f"{len(high_w)} HIGH confidence WEAKENS article(s) detected")
        if n_weakens:
            parts.append(f"{n_weakens} WEAKENS article(s) total")
        if n_supports:
            parts.append(f"{n_supports} SUPPORTS article(s)")
        parts.append(f"{n_signals} signal(s) out of {len(clean)} article(s) evaluated")
        key_signals = ". ".join(parts) + "."

    # watch_points — from next_to_watch of SIGNAL articles
    watch_points: list[str] = []
    seen: set[str] = set()
    for e in signal_evals:
        w = (e.get("next_to_watch") or "").strip()
        if w and w not in seen:
            watch_points.append(w)
            seen.add(w)
    if not watch_points:
        bull = pos.get("bull_signals", [])
        if bull:
            watch_points.append(f"Monitor for confirmation of: {bull[0]}")

    # bull_scenario
    bull_signals = pos.get("bull_signals", [])
    top_supports_title = supports_evals[0]["article_title"][:60] if supports_evals else None
    if top_supports_title:
        b1 = bull_signals[0].lower() if bull_signals else "macro tailwinds"
        b2 = bull_signals[1].lower() if len(bull_signals) > 1 else "macro tailwinds persist"
        bull_scenario = (
            f"Recent supportive signal: '{top_supports_title}'. "
            f"If {b1} and {b2}, thesis strengthens and position is well-positioned."
        )
    else:
        triggers = ", ".join(bull_signals[:2]) if bull_signals else "macro tailwinds"
        bull_scenario = (
            f"No recent supportive signals detected. Thesis remains intact if "
            f"{triggers} materialise over the coming weeks."
        )

    # bear_scenario
    bear_signals_list = pos.get("bear_signals", [])
    top_weakens_title = weakens_evals[0]["article_title"][:60] if weakens_evals else None
    if top_weakens_title:
        reasoning_snippet = (weakens_evals[0].get("reasoning") or "")[:120].rstrip()
        bear_trigger = bear_signals_list[0].lower() if bear_signals_list else "bear conditions"
        bear_scenario = (
            f"Watch: '{top_weakens_title}' — {reasoning_snippet}. "
            f"If {bear_trigger} persist, thesis comes under pressure."
        )
    else:
        trigger = bear_signals_list[0] if bear_signals_list else "adverse macro conditions"
        bear_scenario = f"No active bear signals this period. Key risk to monitor: {trigger}."

    # chf_angle — prefer extracted chf_impact from signal evaluations
    chf_impacts = [
        e.get("chf_impact", "")
        for e in signal_evals
        if e.get("chf_impact") and str(e.get("chf_impact")).lower() != "null"
    ]
    if chf_impacts:
        chf_angle = chf_impacts[0]
    elif pos.get("hedged"):
        chf_angle = (
            "CHF-hedged position — currency movements are largely neutralised. "
            "The hedge cost (basis risk) is the primary FX consideration."
        )
    else:
        currency  = pos.get("currency", "USD")
        chf_angle = (
            f"Unhedged {currency} position — {currency}/CHF movements directly "
            f"impact CHF-denominated returns. Monitor {currency}/CHF rate closely."
        )

    return {
        "thesis_status":           thesis_status,
        "key_signals_this_period": key_signals,
        "watch_points":            watch_points,
        "bull_scenario":           bull_scenario,
        "bear_scenario":           bear_scenario,
        "chf_angle":               chf_angle,
    }


def _build_all_outlooks(positions: list[dict], evaluations: dict) -> dict:
    return {
        pos["id"]: _build_position_outlook(
            pos, evaluations.get(pos["id"], {"summary": {}, "evaluations": []})
        )
        for pos in positions
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def analyze(
    market:      dict | None = None,
    evaluations: dict | None = None,
) -> dict:
    """
    Run full portfolio exposure analysis.

    Parameters
    ----------
    market      : output from data/market.py fetch_positions()
                  (reserved for future price-weighted calculations)
    evaluations : output from analysis/thesis_evaluator.py evaluate_all()

    Returns
    -------
    Nested dict covering currency, geography, overlaps, gaps, flags,
    risk score, and per-position outlooks.
    """
    if evaluations is None:
        evaluations = {}

    cfg       = _load_config()
    positions = _positions_list(cfg)
    n         = len(positions)
    weight    = round(100.0 / n, 2) if n else 25.0

    currency_exp  = _currency_exposure(positions, weight)
    geo_conc      = _geographic_concentration(positions, weight)
    overlaps      = _etf_overlaps(positions)
    gaps          = _portfolio_gaps(positions)
    flags         = _concentration_flags(currency_exp, geo_conc, positions)
    risk_score, risk_commentary = _risk_score(currency_exp, geo_conc, flags)
    outlooks      = _build_all_outlooks(positions, evaluations)

    return {
        "weight_assumption": (
            f"Equal weighting assumed ({weight:.0f}% per position). "
            "Actual exposure depends on real allocation."
        ),
        "currency_exposure":        currency_exp,
        "geographic_concentration": geo_conc,
        "etf_overlaps":             overlaps,
        "portfolio_gaps":           gaps,
        "concentration_flags":      flags,
        "overall_risk_score":       risk_score,
        "overall_risk_commentary":  risk_commentary,
        "position_outlooks":        outlooks,
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from data.market import fetch_positions
    from data.news import fetch_news_for_positions
    from analysis.thesis_evaluator import evaluate_all

    log.info("Fetching market data …")
    market_data = fetch_positions()

    log.info("Fetching news …")
    news_data = fetch_news_for_positions()

    log.info("Running thesis evaluations …")
    eval_data = evaluate_all(news_data)

    log.info("Running exposure analysis …")
    report = analyze(market=market_data, evaluations=eval_data)

    SEP  = "=" * 64
    DASH = "─" * 64

    print(f"\n{SEP}")
    print("  PORTFOLIO EXPOSURE ANALYSIS")
    print(SEP)
    print(f"\n  {report['weight_assumption']}")

    print(f"\n{DASH}")
    print("  CURRENCY EXPOSURE")
    print(DASH)
    cx = report["currency_exposure"]
    print(f"  USD unhedged : {cx['USD_unhedged_pct']}%")
    print(f"  EUR unhedged : {cx['EUR_unhedged_pct']}%")
    print(f"  CHF hedged   : {cx['CHF_hedged_pct']}%")
    print(f"\n  {cx['commentary']}")

    print(f"\n{DASH}")
    print("  GEOGRAPHIC CONCENTRATION")
    print(DASH)
    geo = report["geographic_concentration"]
    for k, v in geo.items():
        if k != "commentary":
            print(f"  {k:<22} {v}%")
    print(f"\n  {geo['commentary']}")

    print(f"\n{DASH}")
    print("  ETF OVERLAPS")
    print(DASH)
    for ov in report["etf_overlaps"]:
        print(f"  {' + '.join(ov['positions'])}  →  {ov['estimated_overlap_pct']}% overlap")
        print(f"    {ov['overlap_reason']}")

    print(f"\n{DASH}")
    print("  PORTFOLIO GAPS")
    print(DASH)
    for g in report["portfolio_gaps"]:
        print(f"\n  ▸ {g['gap']}")
        print(f"    Why     : {g['reason']}")
        print(f"    Suggest : {g['research_suggestion']}")

    print(f"\n{DASH}")
    print("  CONCENTRATION FLAGS")
    print(DASH)
    for f in report["concentration_flags"]:
        print(f"  [{f['severity']}] {f['flag']}")
        print(f"    {f['detail']}")

    print(f"\n{DASH}")
    print(f"  OVERALL RISK SCORE : {report['overall_risk_score']}")
    print(DASH)
    print(f"  {report['overall_risk_commentary']}")

    print(f"\n{DASH}")
    print("  POSITION OUTLOOKS")
    print(DASH)
    for pos_id, outlook in report["position_outlooks"].items():
        print(f"\n  ── {pos_id.upper()} ──")
        print(f"  Status  : {outlook['thesis_status']}")
        print(f"  Signals : {outlook['key_signals_this_period']}")
        if outlook["watch_points"]:
            print("  Watch   :")
            for wp in outlook["watch_points"]:
                print(f"    • {wp}")
        print(f"  Bull    : {outlook['bull_scenario']}")
        print(f"  Bear    : {outlook['bear_scenario']}")
        print(f"  CHF     : {outlook['chf_angle']}")

    print(f"\n{SEP}\n")
