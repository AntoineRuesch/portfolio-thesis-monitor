"""
analysis/thesis_evaluator.py
-----------------------------
Evaluate each news article against its position's investment thesis
using the Claude API. Returns structured JSON assessments.
"""

import json
import logging
import os
import time
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"
ENV_PATH    = Path(__file__).parent.parent / ".env"

load_dotenv(ENV_PATH, override=True)

MODEL      = "claude-haiku-4-5-20251001"
MAX_TOKENS = 500


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(config_path: Path = CONFIG_PATH) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _positions_by_id(cfg: dict) -> dict[str, dict]:
    return {p["id"]: p for p in cfg["portfolio"]["positions"]}


# ---------------------------------------------------------------------------
# Thesis status logic
# ---------------------------------------------------------------------------

def _overall_status(evaluations: list[dict]) -> str:
    weakens = [
        e for e in evaluations
        if e.get("classification") == "WEAKENS" and not e.get("parse_error")
    ]
    high_confidence_weakens = [e for e in weakens if e.get("confidence") == "HIGH"]

    if high_confidence_weakens or len(weakens) >= 3:
        return "REASSESS"
    if len(weakens) in (1, 2):
        return "MONITOR"
    return "ON TRACK"


# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You are a financial analyst assistant evaluating whether a piece of \
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
{{
  "classification": "SUPPORTS" | "NEUTRAL" | "WEAKENS",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "signal_type": "SIGNAL" | "NOISE",
  "reasoning": "<2-3 sentences max explaining why>",
  "chf_impact": "<one sentence on CHF currency angle if relevant, else null>",
  "next_to_watch": "<one concrete thing to monitor as a result>"
}}

Rules:
- SUPPORTS: news clearly strengthens the investment case
- WEAKENS: news clearly challenges or invalidates the thesis
- NEUTRAL: news is tangentially related but doesnt move the needle
- SIGNAL: meaningful new information worth tracking
- NOISE: routine, already priced in, or too vague to matter
- Keep reasoning factual, no investment advice
- chf_impact is mandatory for unhedged positions\
"""


def _build_prompt(pos: dict, article: dict) -> str:
    return PROMPT_TEMPLATE.format(
        position_name=pos["name"],
        thesis=pos["thesis"].strip(),
        bull_signals=", ".join(pos.get("bull_signals", [])),
        bear_signals=", ".join(pos.get("bear_signals", [])),
        risk_factors=", ".join(pos.get("risk_factors", [])),
        title=article["title"],
        summary=article.get("summary") or "",
        published_at=article.get("published_at", ""),
    )


# ---------------------------------------------------------------------------
# Single-article evaluation
# ---------------------------------------------------------------------------

def _evaluate_article(
    client: anthropic.Anthropic,
    pos: dict,
    article: dict,
    article_index: int,
    total: int,
) -> dict:
    """Call Claude and return a merged result dict."""
    base = {
        "article_title": article["title"],
        "article_url":   article.get("url", ""),
        "published_at":  article.get("published_at", ""),
        "source":        article.get("source", ""),
        "fetched_via":   article.get("fetched_via", ""),
    }

    log.info(
        "  [%s] article %d/%d — %s",
        pos["id"], article_index, total, article["title"][:60],
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": _build_prompt(pos, article)}],
        )
        raw_text = response.content[0].text.strip()

        # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        try:
            parsed = json.loads(raw_text)
            return {**base, **parsed, "parse_error": False}
        except json.JSONDecodeError:
            log.warning("JSON parse failed for article: %s", article["title"][:60])
            return {**base, "parse_error": True, "raw_response": raw_text}

    except Exception as exc:
        log.error("Claude API error on article '%s': %s", article["title"][:60], exc)
        return {**base, "parse_error": True, "raw_response": str(exc)}


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def evaluate_all(
    news: dict,
    positions: list[dict] | None = None,
) -> dict:
    """
    Evaluate every news article against its position thesis via Claude.

    Parameters
    ----------
    news      : output from data/news.py fetch_news_for_positions()
    positions : optional override; loaded from config if None

    Returns
    -------
    dict keyed by position_id with evaluations + summary
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    cfg          = _load_config()
    pos_by_id    = _positions_by_id(cfg)
    client       = anthropic.Anthropic(api_key=api_key)
    results: dict = {}

    for pos_id, articles in news.items():
        pos = pos_by_id.get(pos_id)
        if not pos:
            log.warning("Position '%s' not found in portfolio.yaml — skipping.", pos_id)
            continue

        # Filter out articles with no usable content
        valid_articles = [
            a for a in articles
            if (a.get("title") or "").strip() and (a.get("summary") or a.get("title"))
        ]

        log.info("Evaluating %s (%d articles) …", pos_id, len(valid_articles))

        evaluations: list[dict] = []
        for i, article in enumerate(valid_articles, start=1):
            evaluation = _evaluate_article(client, pos, article, i, len(valid_articles))
            evaluations.append(evaluation)
            time.sleep(0.3)

        # Build summary counts
        clean = [e for e in evaluations if not e.get("parse_error")]
        supports = sum(1 for e in clean if e.get("classification") == "SUPPORTS")
        neutral  = sum(1 for e in clean if e.get("classification") == "NEUTRAL")
        weakens  = sum(1 for e in clean if e.get("classification") == "WEAKENS")
        signals  = sum(1 for e in clean if e.get("signal_type") == "SIGNAL")
        noise    = sum(1 for e in clean if e.get("signal_type") == "NOISE")

        results[pos_id] = {
            "position_name": pos["name"],
            "evaluations":   evaluations,
            "summary": {
                "total_articles":        len(evaluations),
                "supports":              supports,
                "neutral":               neutral,
                "weakens":               weakens,
                "signals":               signals,
                "noise":                 noise,
                "overall_thesis_status": _overall_status(evaluations),
            },
        }

    return results


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

    from data.news import fetch_news_for_positions

    log.info("Fetching news …")
    news_data = fetch_news_for_positions()

    log.info("Running thesis evaluations …")
    results = evaluate_all(news_data)

    print("\n" + "=" * 60)
    print("  THESIS EVALUATION RESULTS")
    print("=" * 60)

    for pos_id, data in results.items():
        s = data["summary"]
        print(f"\n{'─' * 60}")
        print(f"  {data['position_name']}")
        print(f"  Status  : {s['overall_thesis_status']}")
        print(
            f"  Counts  : {s['supports']} SUPPORTS  "
            f"{s['neutral']} NEUTRAL  "
            f"{s['weakens']} WEAKENS  "
            f"({s['signals']} signals / {s['noise']} noise)"
        )

        # First SIGNAL article with reasoning
        first_signal = next(
            (e for e in data["evaluations"]
             if e.get("signal_type") == "SIGNAL" and not e.get("parse_error")),
            None,
        )
        # Show all WEAKENS with confidence so status logic is transparent
        weakens_evals = [
            e for e in data["evaluations"]
            if e.get("classification") == "WEAKENS" and not e.get("parse_error")
        ]
        if weakens_evals:
            print(f"\n  WEAKENS articles:")
            for e in weakens_evals:
                print(f"    [{e['published_at'][:10]}] ({e['confidence']}) {e['article_title'][:65]}")

        if first_signal:
            print(f"\n  Top signal:")
            print(f"    [{first_signal['published_at'][:10]}] {first_signal['article_title'][:70]}")
            print(f"    → {first_signal['classification']} ({first_signal['confidence']})")
            print(f"    {first_signal.get('reasoning', '')}")

    print("\n" + "=" * 60 + "\n")
