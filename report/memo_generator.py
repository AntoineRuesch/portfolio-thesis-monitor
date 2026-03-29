"""
report/memo_generator.py
-------------------------
Assembles all module outputs into a clean daily Markdown memo.
"""

import logging
from datetime import date, datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"
OUTPUT_DIR  = Path(__file__).parent.parent / "output"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_EMOJI = {
    "ON TRACK": "🟢",
    "MONITOR":  "🟡",
    "REASSESS": "🔴",
}

_SEVERITY_EMOJI = {
    "HIGH":   "🔴",
    "MEDIUM": "🟡",
    "LOW":    "🟢",
}

_CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _fmt_rate(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "n/a"


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Section builders  (each returns a list of Markdown lines)
# ---------------------------------------------------------------------------

def _section_header(today: date, base_currency: str, n_positions: int = 4) -> list[str]:
    weight_pct = round(100.0 / n_positions) if n_positions else 25
    return [
        "# Portfolio Thesis Monitor",
        f"**Date:** {today.isoformat()}  ",
        f"**Base currency:** {base_currency}  ",
        f"**Weight assumption:** Equal weighting ({weight_pct}% per position)",
        "",
        "---",
        "",
    ]


def _section_market_snapshot(
    positions: list[dict],
    market_data: dict,
) -> list[str]:
    lines = [
        "## Market Snapshot",
        "",
        "| Position | Price | 1D % | 7D % | 30D % | Currency |",
        "|----------|-------|------|------|-------|----------|",
    ]
    for pos in positions:
        ticker = pos["ticker"]
        name   = pos["name"]
        d      = market_data.get(ticker, {})
        if d.get("error") or d.get("insufficient_data") and not d.get("current_price"):
            lines.append(
                f"| {name} | — | — | — | — | {pos.get('currency', '—')} |"
            )
        else:
            price  = f"{d.get('current_price', '—')}"
            c1d    = _fmt_pct(d.get("change_1d_pct"))
            c7d    = _fmt_pct(d.get("change_7d_pct"))
            c30d   = _fmt_pct(d.get("change_30d_pct"))
            cur    = pos.get("currency", "—")
            lines.append(f"| {name} | {price} | {c1d} | {c7d} | {c30d} | {cur} |")

    lines += [""]
    return lines


def _section_fx_rates(fx_data: dict) -> list[str]:
    lines = [
        "## FX Rates (CHF Base)",
        "",
        "| Pair | Rate | 1D % | 7D % | 30D % | 30D Range |",
        "|------|------|------|------|-------|-----------|",
    ]
    for pair, d in fx_data.items():
        if d is None:
            lines.append(f"| {pair} | — | — | — | — | — |")
            continue
        rate   = _fmt_rate(d.get("rate"))
        c1d    = _fmt_pct(d.get("change_1d_pct"))
        c7d    = _fmt_pct(d.get("change_7d_pct"))
        c30d   = _fmt_pct(d.get("change_30d_pct"))
        rng    = d.get("range_30d", {})
        rng_str = f"{_fmt_rate(rng.get('min'))}–{_fmt_rate(rng.get('max'))}"
        display = pair[:3] + "/" + pair[3:]  # e.g. EURCHF → EUR/CHF
        lines.append(f"| {display} | {rate} | {c1d} | {c7d} | {c30d} | {rng_str} |")

    lines += [""]
    return lines


def _section_portfolio_risk(exposure: dict, positions: list[dict] | None = None) -> list[str]:
    lines: list[str] = [
        "---",
        "",
        "## Portfolio Risk Overview",
        "",
        f"**Overall Risk Score:** {exposure.get('overall_risk_score', 'N/A')}",
        "",
    ]

    # Currency exposure
    cx = exposure.get("currency_exposure", {})
    lines += [
        "### Currency Exposure",
        "",
        f"- USD unhedged: **{cx.get('USD_unhedged_pct', 0):.1f}%**",
        f"- EUR unhedged: **{cx.get('EUR_unhedged_pct', 0):.1f}%**",
        f"- CHF hedged:   **{cx.get('CHF_hedged_pct',   0):.1f}%**",
    ]
    if cx.get("commentary"):
        lines.append(f"- ⚠️ {cx['commentary']}")
    lines.append("")

    # Geographic concentration
    geo = exposure.get("geographic_concentration", {})
    geo_parts = [
        f"{k}: {v}%"
        for k, v in geo.items()
        if k != "commentary" and isinstance(v, (int, float))
    ]
    lines += [
        "### Geographic Concentration",
        "",
        "- " + " | ".join(geo_parts),
    ]
    if geo.get("commentary"):
        lines.append(f"- ⚠️ {geo['commentary']}")
    lines.append("")

    # Concentration flags
    flags = exposure.get("concentration_flags", [])
    if flags:
        lines += ["### Concentration Flags", ""]
        for f in flags:
            emoji  = _SEVERITY_EMOJI.get(f.get("severity", ""), "⚠️")
            sev    = f.get("severity", "")
            detail = f.get("detail", "")
            flag   = f.get("flag", "")
            lines.append(f"- {emoji} **{sev}:** {flag} — {detail}")
        lines.append("")

    # Portfolio gaps
    gaps = exposure.get("portfolio_gaps", [])
    if gaps:
        lines += ["### Portfolio Gaps", ""]
        for g in gaps:
            lines.append(f"- 🔍 **{g['gap']}:** {g.get('research_suggestion', '')}")
        lines.append("")

    # ETF overlaps
    overlaps = exposure.get("etf_overlaps", [])
    if overlaps:
        id_to_name = {p["id"]: p["name"] for p in (positions or [])}
        lines += ["### ETF Overlaps", ""]
        for ov in overlaps:
            labels = " ↔ ".join(
                id_to_name.get(pid, pid) for pid in ov.get("positions", [])
            )
            lines.append(
                f"- ⚠️ {labels}: ~{ov.get('estimated_overlap_pct', '?')}% overlap"
                f" — {ov.get('overlap_reason', '')}"
            )
        lines.append("")

    lines += ["---", ""]
    return lines


def _section_position_analysis(
    positions:           list[dict],
    market_data:         dict,
    fx_data:             dict,
    thesis_evaluations:  dict,
    exposure:            dict,
) -> list[str]:
    lines = [
        "## Position Analysis",
        "",
    ]

    outlooks = exposure.get("position_outlooks", {})

    for pos in positions:
        pos_id  = pos["id"]
        ticker  = pos["ticker"]
        name    = pos["name"]

        eval_data = thesis_evaluations.get(pos_id, {})
        summary   = eval_data.get("summary", {})
        evals     = eval_data.get("evaluations", [])
        status    = summary.get("overall_thesis_status", "ON TRACK")
        emoji     = _STATUS_EMOJI.get(status, "⚪")
        outlook   = outlooks.get(pos_id, {})

        # --- Heading ---
        lines.append(f"### {emoji} {name} — {status}")
        lines.append("")

        # --- Market line ---
        mkt = market_data.get(ticker, {})
        if mkt and not mkt.get("error"):
            price = mkt.get("current_price", "—")
            cur   = pos.get("currency", "")
            c1d   = _fmt_pct(mkt.get("change_1d_pct"))
            c7d   = _fmt_pct(mkt.get("change_7d_pct"))
            c30d  = _fmt_pct(mkt.get("change_30d_pct"))
            lines.append(
                f"**Market:** {price} {cur} | 1D: {c1d} | 7D: {c7d} | 30D: {c30d}"
            )
        else:
            lines.append("**Market:** data unavailable")

        # --- CHF angle from outlook (enriched with live FX if unhedged) ---
        chf_angle = outlook.get("chf_angle", "")
        if chf_angle:
            # Append live rate if we can identify the pair
            pair_key = None
            if pos.get("currency", "").upper() == "EUR" and not pos.get("hedged"):
                pair_key = "EURCHF"
            elif pos.get("currency", "").upper() == "USD" and not pos.get("hedged"):
                pair_key = "USDCHF"

            if pair_key and fx_data.get(pair_key):
                fx = fx_data[pair_key]
                live = (
                    f" (live: {_fmt_rate(fx.get('rate'))}, "
                    f"30D: {_fmt_pct(fx.get('change_30d_pct'))})"
                )
                chf_angle = chf_angle.rstrip(".") + live + "."

            lines.append(f"**CHF angle:** {chf_angle}")

        lines.append("")

        # --- Thesis signals table ---
        lines.append("#### Thesis Signals This Period")
        lines.append("")

        clean_evals = [e for e in evals if not e.get("parse_error")]
        signal_evals = sorted(
            [e for e in clean_evals if e.get("signal_type") == "SIGNAL"],
            key=lambda e: _CONFIDENCE_RANK.get(e.get("confidence", "LOW"), 0),
            reverse=True,
        )[:3]  # max 3

        if signal_evals:
            lines += [
                "| Article | Classification | Confidence | Signal/Noise |",
                "|---------|---------------|------------|--------------|",
            ]
            for e in signal_evals:
                title = (e.get("article_title") or "")[:55]
                clf   = e.get("classification", "—")
                conf  = e.get("confidence", "—")
                sn    = e.get("signal_type", "—")
                lines.append(f'| "{title}…" | {clf} | {conf} | {sn} |')
        else:
            lines.append("*No significant signals detected this period.*")

        lines.append("")

        # --- Key reasoning (top signal only) ---
        if signal_evals:
            top = signal_evals[0]
            reasoning = top.get("reasoning", "")
            if reasoning:
                lines += [
                    "#### Key Reasoning",
                    "",
                    f"> **\"{(top.get('article_title') or '')[:70]}\"**  ",
                    f"> {reasoning}",
                    "",
                ]

        # --- Outlook ---
        lines.append("#### Outlook")
        lines.append("")
        watch = outlook.get("watch_points", [])
        if watch:
            for wp in watch:
                lines.append(f"- 📌 **Watch:** {wp}")
        bull = outlook.get("bull_scenario", "")
        bear = outlook.get("bear_scenario", "")
        if bull:
            lines.append(f"- 🐂 **Bull:** {bull}")
        if bear:
            lines.append(f"- 🐻 **Bear:** {bear}")

        lines += ["", "---", ""]

    return lines


def _section_research_next(exposure: dict) -> list[str]:
    lines = [
        "## What To Research Next",
        "",
        "Based on current portfolio composition, these areas are worth "
        "researching to improve diversification:",
        "",
    ]

    items: list[str] = []

    # Pull from portfolio_gaps
    for g in exposure.get("portfolio_gaps", []):
        gap_name = g.get("gap", "")
        suggest  = g.get("research_suggestion", "")
        if suggest:
            items.append(f"**{gap_name}** — {suggest}")

    # Add HIGH severity concentration flags not already covered
    for f in exposure.get("concentration_flags", []):
        if f.get("severity") == "HIGH":
            detail = f.get("detail", "")
            flag   = f.get("flag", "")
            items.append(f"**{flag}** — {detail}")

    # Guarantee at least 3 items (pad with generic guidance if needed)
    fallbacks = [
        "**Portfolio stress test** — model a simultaneous -10% USD/CHF move and "
        "-20% equity drawdown to understand worst-case CHF returns.",
        "**Rebalancing review** — confirm current allocations still match the "
        "intended 25% equal weight.",
        "**Benchmark comparison** — compare 1Y portfolio return vs a 60/40 "
        "CHF-hedged benchmark.",
    ]
    for fb in fallbacks:
        if len(items) >= 3:
            break
        items.append(fb)

    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item}")

    lines += ["", "---", ""]
    return lines


def _section_data_quality(
    news_data:   dict,
    market_data: dict,
    fx_data:     dict,
) -> list[str]:
    notes: list[str] = []

    # News source notes
    fetched_via_counts: dict[str, list[str]] = {}
    for pos_id, articles in news_data.items():
        if articles:
            via = articles[0].get("fetched_via", "")
            if via:
                fetched_via_counts.setdefault(via, []).append(pos_id)

    keyword_positions = fetched_via_counts.get("keyword_search", [])
    if keyword_positions:
        joined = ", ".join(keyword_positions)
        notes.append(
            f"⚠️ News sourced via keyword search for: **{joined}** "
            f"— primary/proxy ticker not recognized by Marketaux."
        )

    no_news_positions = [
        pos_id for pos_id, articles in news_data.items() if not articles
    ]
    if no_news_positions:
        joined = ", ".join(no_news_positions)
        notes.append(
            f"⚠️ **Marketaux returned no results** for: {joined} "
            f"— quota may be exceeded or API key invalid."
        )

    # Market data warnings
    for ticker, d in market_data.items():
        if d.get("insufficient_data"):
            notes.append(
                f"⚠️ Market data for **{ticker}** has fewer than 5 days of history "
                f"— percentage changes may be unreliable."
            )
        if d.get("error"):
            notes.append(
                f"⚠️ Failed to fetch market data for **{ticker}**: {d['error']}"
            )

    # FX warnings
    for pair, d in fx_data.items():
        if d is None:
            notes.append(f"⚠️ FX data unavailable for **{pair}**.")

    # Always include weight assumption note
    notes.append(
        "ℹ️ Equal weighting assumed — actual exposure depends on real allocation."
    )

    if not notes:
        return []

    lines = [
        "## Data Quality Notes",
        "",
    ]
    for note in notes:
        lines.append(f"- {note}")
    lines += ["", "---", ""]
    return lines


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def generate(
    market_data:        dict,
    fx_data:            dict,
    news_data:          dict,
    thesis_evaluations: dict,
    exposure_analysis:  dict,
    output_path:        Path | None = None,
) -> Path:
    """
    Assemble all module outputs into a Markdown memo.

    Parameters
    ----------
    market_data        : from data/market.py
    fx_data            : from data/fx.py
    news_data          : from data/news.py
    thesis_evaluations : from analysis/thesis_evaluator.py
    exposure_analysis  : from analysis/exposure_analyzer.py
    output_path        : override default output/memo_YYYY-MM-DD.md

    Returns
    -------
    Path to the written file.
    """
    cfg       = _load_config()
    positions = cfg["portfolio"]["positions"]
    base_cur  = cfg["portfolio"].get("base_currency", "CHF")

    today = date.today()
    if output_path is None:
        OUTPUT_DIR.mkdir(exist_ok=True)
        output_path = OUTPUT_DIR / f"memo_{today.isoformat()}.md"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # Assemble all sections
    sections: list[list[str]] = [
        _section_header(today, base_cur, len(positions)),
        _section_market_snapshot(positions, market_data),
        _section_fx_rates(fx_data),
        _section_portfolio_risk(exposure_analysis, positions),
        _section_position_analysis(
            positions, market_data, fx_data,
            thesis_evaluations, exposure_analysis,
        ),
        _section_research_next(exposure_analysis),
        _section_data_quality(news_data, market_data, fx_data),
        [
            f"*Generated by Portfolio Thesis Monitor — "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
            "",
        ],
    ]

    memo = "\n".join(line for section in sections for line in section)

    output_path.write_text(memo, encoding="utf-8")
    log.info("Memo written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Standalone test  (full pipeline)
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
    from data.fx import fetch_fx
    from data.news import fetch_news_for_positions
    from analysis.thesis_evaluator import evaluate_all
    from analysis.exposure_analyzer import analyze

    log.info("Fetching market data …")
    market = fetch_positions()

    log.info("Fetching FX rates …")
    fx = fetch_fx()

    log.info("Fetching news …")
    news = fetch_news_for_positions()

    log.info("Evaluating theses via Claude …")
    evaluations = evaluate_all(news)

    log.info("Analysing exposure …")
    exposure = analyze(market=market, evaluations=evaluations)

    log.info("Generating memo …")
    path = generate(
        market_data=market,
        fx_data=fx,
        news_data=news,
        thesis_evaluations=evaluations,
        exposure_analysis=exposure,
    )

    print(f"\n✅  Memo saved to {path}\n")
    print("=" * 72)
    print(path.read_text(encoding="utf-8"))
    print("=" * 72)
