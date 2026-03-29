"""
data/fx.py
----------
Fetch EUR/CHF and USD/CHF exchange rates over 30 days via yfinance.
"""

import logging
from pathlib import Path

import yaml
import yfinance as yf

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"


def _load_fx_pairs(config_path: Path = CONFIG_PATH) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg["portfolio"]["fx_pairs"]


def _pct_change(series, n_days: int) -> float | None:
    if len(series) < 2:
        return None
    idx = max(0, len(series) - 1 - n_days)
    past = series.iloc[idx]
    current = series.iloc[-1]
    if past == 0:
        return None
    return round((current - past) / past * 100, 4)


def fetch_fx(
    pairs: list[dict] | None = None,
    lookback_days: int = 30,
) -> dict:
    """
    Fetch FX rate history for CHF pairs.

    Parameters
    ----------
    pairs         : list of fx_pair dicts from portfolio.yaml
                    (each has 'pair' and 'ticker' keys).
                    If None, loaded automatically from config.
    lookback_days : ignored (period is always "1mo"); kept for API consistency.

    Returns
    -------
    dict keyed by pair name, e.g. {"EURCHF": {...}, "USDCHF": {...}}
    """
    if pairs is None:
        pairs = _load_fx_pairs()

    results: dict = {}

    for entry in pairs:
        pair: str = entry["pair"]
        ticker_symbol: str = entry["ticker"]

        log.info("Fetching %s (%s) …", pair, ticker_symbol)

        try:
            hist = yf.Ticker(ticker_symbol).history(period="1mo", interval="1d")

            if hist.empty:
                log.error("%s — no data returned by yfinance.", pair)
                results[pair] = None
                continue

            close = hist["Close"]

            results[pair] = {
                "rate": round(float(close.iloc[-1]), 6),
                "change_1d_pct": _pct_change(close, 1),
                "change_7d_pct": _pct_change(close, 7),
                "change_30d_pct": _pct_change(close, 30),
                "range_30d": {
                    "min": round(float(close.min()), 6),
                    "max": round(float(close.max()), 6),
                },
            }

        except Exception as exc:
            log.error("Failed to fetch %s (%s): %s", pair, ticker_symbol, exc)
            results[pair] = None

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

    print("\n" + "=" * 50)
    print("  FX RATES — 30-DAY SNAPSHOT (base: CHF)")
    print("=" * 50)

    data = fetch_fx()

    for pair, d in data.items():
        print(f"\n  {pair}")
        if d is None:
            print("    ERROR: could not fetch data")
            continue
        print(f"    Rate     : {d['rate']}")
        print(
            f"    Change   : 1d {d['change_1d_pct']:+.4f}%  "
            f"7d {d['change_7d_pct']:+.4f}%  "
            f"30d {d['change_30d_pct']:+.4f}%"
        )
        print(f"    30d range: {d['range_30d']['min']} – {d['range_30d']['max']}")

    print("\n" + "=" * 50 + "\n")
