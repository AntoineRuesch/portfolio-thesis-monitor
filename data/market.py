"""
data/market.py
--------------
Fetch 30-day market data for all positions defined in config/portfolio.yaml.
"""

import logging
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"


def _load_positions(config_path: Path = CONFIG_PATH) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg["portfolio"]["positions"]


def _pct_change(series: pd.Series, n_days: int) -> float | None:
    """Return % change between the last close and the close n_days ago."""
    if len(series) < 2:
        return None
    # Find the value n_days back (or the oldest available if shorter)
    idx = max(0, len(series) - 1 - n_days)
    past_price = series.iloc[idx]
    current_price = series.iloc[-1]
    if past_price == 0:
        return None
    return round((current_price - past_price) / past_price * 100, 2)


def fetch_positions(
    positions: list[dict] | None = None,
    lookback_days: int = 30,
) -> dict:
    """
    Fetch market data for a list of position dicts.

    Parameters
    ----------
    positions   : list of position dicts from portfolio.yaml.
                  If None, loaded automatically from config.
    lookback_days : ignored (period is always "1mo"); kept for API consistency.

    Returns
    -------
    dict keyed by ticker symbol, e.g. {"EXSA.DE": {...}, ...}
    """
    if positions is None:
        positions = _load_positions()

    results: dict = {}

    for pos in positions:
        ticker_symbol: str = pos["ticker"]
        pos_id: str = pos["id"]
        name: str = pos["name"]

        log.info("Fetching %s (%s) …", ticker_symbol, pos_id)

        try:
            ticker = yf.Ticker(ticker_symbol)
            hist: pd.DataFrame = ticker.history(period="1mo", interval="1d")

            if hist.empty:
                log.warning("%s — no data returned by yfinance.", ticker_symbol)
                results[ticker_symbol] = {
                    "position_id": pos_id,
                    "name": name,
                    "insufficient_data": True,
                    "error": "No data returned",
                }
                continue

            # Extract average volume before slicing to Close-only
            try:
                avg_volume = int(hist["Volume"].mean()) if "Volume" in hist.columns else None
            except Exception:
                avg_volume = None

            # Keep only Date + Close; normalise index to plain dates
            hist = hist[["Close"]].copy()
            hist.index = pd.to_datetime(hist.index).normalize()
            hist.index.name = "Date"

            close = hist["Close"]
            n = len(close)
            insufficient = n < 5

            if insufficient:
                log.warning(
                    "%s — only %d day(s) of history returned (< 5).", ticker_symbol, n
                )

            current_price = round(float(close.iloc[-1]), 4)

            results[ticker_symbol] = {
                "position_id": pos_id,
                "name": name,
                "current_price": current_price,
                "currency": pos.get("currency", ""),
                "change_1d_pct": _pct_change(close, 1),
                "change_7d_pct": _pct_change(close, 7),
                "change_30d_pct": _pct_change(close, 30),
                "avg_volume_30d": avg_volume,
                "history": hist,
                "insufficient_data": insufficient,
            }

        except Exception as exc:
            log.error("Failed to fetch %s: %s", ticker_symbol, exc)
            results[ticker_symbol] = {
                "position_id": pos_id,
                "name": name,
                "insufficient_data": True,
                "error": str(exc),
            }

    return results


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("\n" + "=" * 60)
    print("  MARKET DATA — 30-DAY SNAPSHOT")
    print("=" * 60)

    data = fetch_positions()

    for ticker, d in data.items():
        print(f"\n{'─' * 60}")
        print(f"  {d['name']}")
        print(f"  Ticker : {ticker}  |  ID : {d['position_id']}")

        if d.get("error"):
            print(f"  ERROR  : {d['error']}")
            continue

        if d.get("insufficient_data"):
            print("  WARNING: insufficient data (< 5 days)")

        print(f"  Price  : {d['current_price']} {d['currency']}")
        def _fmt(v: float | None) -> str:
            return f"{v:+.2f}" if v is not None else "n/a"
        print(
            f"  Change : 1d {_fmt(d['change_1d_pct'])}%  "
            f"7d {_fmt(d['change_7d_pct'])}%  "
            f"30d {_fmt(d['change_30d_pct'])}%"
        )
        if d["avg_volume_30d"]:
            print(f"  Avg vol (30d) : {d['avg_volume_30d']:,}")
        else:
            print("  Avg vol (30d) : n/a")
        print(f"  History rows  : {len(d['history'])}")
        print(d["history"].tail(3).to_string())

    print("\n" + "=" * 60 + "\n")
