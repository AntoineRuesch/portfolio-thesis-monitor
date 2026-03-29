"""
Portfolio Thesis Monitor
------------------------
Single entry point that runs the entire pipeline end-to-end and
generates the daily Markdown memo.

Usage:
    python main.py            # full run
    python main.py --dry-run  # skip news + Claude, market/FX only
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Logging — module-level noise suppressed, our progress lines go to stdout
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,          # suppress noisy library logs by default
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("main")

CONFIG_PATH = Path(__file__).parent / "config" / "portfolio.yaml"

_STATUS_EMOJI = {"ON TRACK": "🟢", "MONITOR": "🟡", "REASSESS": "🔴"}


# ---------------------------------------------------------------------------
# Progress printer
# ---------------------------------------------------------------------------

def _step(n: int, total: int, label: str, *, done: bool = False,
          detail: str = "", error: str = "") -> None:
    prefix = f"  [{n}/{total}] {label}"
    if error:
        print(f"{prefix:<48} ✗  ERROR: {error}", flush=True)
    elif done:
        suffix = f"  ✓  {detail}" if detail else "  ✓"
        print(f"{prefix:<48}{suffix}", flush=True)
    else:
        print(f"{prefix:<48}", end="", flush=True)


def _ok(detail: str = "") -> None:
    suffix = f"  ✓  {detail}" if detail else "  ✓"
    print(suffix, flush=True)


def _fail(msg: str) -> None:
    print(f"  ✗  ERROR: {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Pipeline steps — each returns (result, error_msg | None)
# ---------------------------------------------------------------------------

def _run_market(positions: list[dict]) -> tuple[dict, str | None]:
    try:
        from data.market import fetch_positions
        return fetch_positions(positions), None
    except Exception as exc:
        return {}, str(exc)


def _run_fx(pairs: list[dict]) -> tuple[dict, str | None]:
    try:
        from data.fx import fetch_fx
        return fetch_fx(pairs), None
    except Exception as exc:
        return {}, str(exc)


def _run_news(positions: list[dict], lookback_days: int, dry_run: bool) -> tuple[dict, str | None]:
    if dry_run:
        return {pos["id"]: [] for pos in positions}, None
    try:
        from data.news import fetch_news_for_positions
        return fetch_news_for_positions(positions, lookback_days), None
    except Exception as exc:
        return {pos["id"]: [] for pos in positions}, str(exc)


def _run_thesis(news: dict, dry_run: bool) -> tuple[dict, str | None]:
    if dry_run:
        return {}, None
    try:
        from analysis.thesis_evaluator import evaluate_all
        return evaluate_all(news), None
    except Exception as exc:
        return {}, str(exc)


def _run_exposure(market: dict, evaluations: dict) -> tuple[dict, str | None]:
    try:
        from analysis.exposure_analyzer import analyze
        return analyze(market=market, evaluations=evaluations), None
    except Exception as exc:
        return {}, str(exc)


def _run_memo(
    market: dict,
    fx: dict,
    news: dict,
    evaluations: dict,
    exposure: dict,
    output_path: Path,
) -> tuple[Path | None, str | None]:
    try:
        from report.memo_generator import generate
        path = generate(
            market_data=market,
            fx_data=fx,
            news_data=news,
            thesis_evaluations=evaluations,
            exposure_analysis=exposure,
            output_path=output_path,
        )
        return path, None
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(
    memo_path:   Path | None,
    evaluations: dict,
    exposure:    dict,
    dry_run:     bool,
) -> None:
    bar = "=" * 42
    print(f"\n  {bar}")
    print("   Portfolio Thesis Monitor — Done")
    print(f"  {bar}")

    if memo_path:
        print(f"   Memo saved : {memo_path}")
    else:
        print("   Memo       : generation failed")

    if dry_run:
        print("   Mode       : DRY RUN (news + Claude skipped)")

    # Position statuses
    if evaluations:
        print("\n   Position Status:")
        for pos_id, data in evaluations.items():
            status = data.get("summary", {}).get("overall_thesis_status", "ON TRACK")
            emoji  = _STATUS_EMOJI.get(status, "⚪")
            name   = data.get("position_name", pos_id)
            print(f"   {emoji}  {name} — {status}")
    else:
        print("\n   Position Status: n/a (dry run or evaluation failed)")

    # Overall risk
    risk = exposure.get("overall_risk_score", "n/a")
    print(f"\n   Overall Portfolio Risk: {risk}")
    print(f"  {bar}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> int:
    TOTAL = 7
    today = date.today()

    output_path = Path("output") / f"memo_{today.isoformat()}.md"
    Path("output").mkdir(exist_ok=True)

    print()
    print("  Portfolio Thesis Monitor")
    print(f"  {date.today().isoformat()}  {'[DRY RUN]' if dry_run else ''}")
    print("  " + "─" * 40)

    errors: list[str] = []

    # ── Step 1: Config ────────────────────────────────────────────────────
    _step(1, TOTAL, "Loading portfolio config...")
    try:
        cfg           = _load_config()
        positions     = cfg["portfolio"]["positions"]
        fx_pairs      = cfg["portfolio"]["fx_pairs"]
        lookback      = cfg["portfolio"].get("lookback_days", 30)
        news_lookback = cfg["portfolio"].get("news_lookback_days", 30)
        _ok(f"{len(positions)} positions loaded")
    except Exception as exc:
        _fail(str(exc))
        print("\n  Fatal: cannot continue without config.\n")
        return 1

    # ── Step 2: Market data ───────────────────────────────────────────────
    _step(2, TOTAL, "Fetching market data...")
    market, err = _run_market(positions)
    if err:
        _fail(err)
        errors.append(f"Market data: {err}")
    else:
        ok_count = sum(1 for d in market.values() if not d.get("error"))
        _ok(f"{ok_count}/{len(positions)} tickers fetched")

    # ── Step 3: FX rates ──────────────────────────────────────────────────
    _step(3, TOTAL, "Fetching FX rates...")
    fx, err = _run_fx(fx_pairs)
    if err:
        _fail(err)
        errors.append(f"FX rates: {err}")
    else:
        ok_count = sum(1 for v in fx.values() if v is not None)
        _ok(f"{ok_count}/{len(fx_pairs)} pairs fetched")

    # ── Step 4: News ──────────────────────────────────────────────────────
    label = "Fetching news... [skipped — dry run]" if dry_run else "Fetching news..."
    _step(4, TOTAL, label)
    news, err = _run_news(positions, news_lookback, dry_run)
    if err:
        _fail(err)
        errors.append(f"News: {err}")
    elif dry_run:
        print()   # newline after the inline label
    else:
        total_articles = sum(len(v) for v in news.values())
        positions_with = sum(1 for v in news.values() if v)
        _ok(f"{total_articles} articles across {positions_with}/{len(positions)} positions")

    # ── Step 5: Thesis evaluation ─────────────────────────────────────────
    label = "Evaluating thesis... [skipped — dry run]" if dry_run else "Evaluating thesis..."
    _step(5, TOTAL, label)
    evaluations, err = _run_thesis(news, dry_run)
    if err:
        _fail(err)
        errors.append(f"Thesis evaluation: {err}")
    elif dry_run:
        print()
    else:
        total_evals = sum(
            len(d.get("evaluations", [])) for d in evaluations.values()
        )
        _ok(f"{total_evals} evaluations completed")

    # ── Step 6: Exposure analysis ─────────────────────────────────────────
    _step(6, TOTAL, "Analyzing portfolio exposure...")
    exposure, err = _run_exposure(market, evaluations)
    if err:
        _fail(err)
        errors.append(f"Exposure analysis: {err}")
    else:
        risk = exposure.get("overall_risk_score", "n/a")
        _ok(f"Risk score: {risk}")

    # ── Step 7: Generate memo ─────────────────────────────────────────────
    _step(7, TOTAL, "Generating memo...")
    memo_path, err = _run_memo(market, fx, news, evaluations, exposure, output_path)
    if err:
        _fail(err)
        errors.append(f"Memo generation: {err}")
    else:
        _ok(f"Saved to {memo_path}")

    # ── Summary ───────────────────────────────────────────────────────────
    _print_summary(memo_path, evaluations, exposure, dry_run)

    return 0 if not errors else 2   # 2 = partial success


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Portfolio Thesis Monitor — daily memo generator"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip news fetch and Claude evaluation; generate memo with market + FX data only.",
    )
    args = parser.parse_args()

    try:
        code = main(dry_run=args.dry_run)
        sys.exit(code)
    except KeyboardInterrupt:
        print("\n  Interrupted.\n")
        sys.exit(0)
