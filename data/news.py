"""
data/news.py
------------
Fetch relevant financial news for each portfolio position over the last 30 days.

Multi-provider fallback chain (tried in order until ≥3 articles found):
  1. Marketaux   — primary_ticker → proxy_tickers → keyword_search
  2. Alpha Vantage — ticker → keyword_search
  3. GNews        — keyword_search
  4. NewsAPI      — keyword_search  (last resort; 24h delay on free tier)
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "portfolio.yaml"
ENV_PATH    = Path(__file__).parent.parent / ".env"

load_dotenv(ENV_PATH)

# ---------------------------------------------------------------------------
# Provider endpoints
# ---------------------------------------------------------------------------

MARKETAUX_BASE     = "https://api.marketaux.com/v1/news/all"
ALPHAVANTAGE_BASE  = "https://www.alphavantage.co/query"
GNEWS_BASE         = "https://gnews.io/api/v4/search"
NEWSAPI_BASE       = "https://newsapi.org/v2/everything"

RATE_LIMIT_SLEEP   = 0.5   # seconds between requests to the same provider
MIN_ARTICLES       = 3     # minimum to stop the fallback chain


# ---------------------------------------------------------------------------
# Provider availability — checked once at import time
# ---------------------------------------------------------------------------

def _check_providers() -> dict[str, str | None]:
    """Return dict of provider → api_key (or None if missing)."""
    return {
        "marketaux":     os.getenv("MARKETAUX_API_KEY",    "").strip() or None,
        "alpha_vantage": os.getenv("ALPHA_VANTAGE_API_KEY","").strip() or None,
        "gnews":         os.getenv("GNEWS_API_KEY",        "").strip() or None,
        "newsapi":       os.getenv("NEWS_API_KEY",         "").strip() or None,
    }


PROVIDERS = _check_providers()


def log_provider_status() -> None:
    """Log which providers are active / inactive. Called at startup."""
    for name, key in PROVIDERS.items():
        if key:
            log.info("Provider %-15s ACTIVE", name)
        else:
            log.info("Provider %-15s skipped — API key not found in .env", name)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_positions(config_path: Path = CONFIG_PATH) -> list[dict]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg["portfolio"]["positions"]


def _published_after_iso(days: int = 30) -> str:
    """Return ISO 8601 UTC timestamp for `days` ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _published_after_compact(days: int = 30) -> str:
    """Return compact timestamp YYYYMMDDTHHMM for Alpha Vantage."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y%m%dT%H%M")


# ---------------------------------------------------------------------------
# Quota / rate-limit detection
# ---------------------------------------------------------------------------

_QUOTA_CODES = {402, 429}
_QUOTA_KEYWORDS = ("quota", "rate limit", "exceeded", "limit reached",
                   "upgrade", "payment required")


def _is_quota_error(resp: requests.Response) -> bool:
    if resp.status_code in _QUOTA_CODES:
        return True
    try:
        body = resp.json()
        msg  = str(body).lower()
        return any(kw in msg for kw in _QUOTA_KEYWORDS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _deduplicate(articles: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for a in articles:
        key = (a.get("title") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def _sort_by_date(articles: list[dict]) -> list[dict]:
    def _key(a: dict) -> datetime:
        try:
            return datetime.fromisoformat(
                (a.get("published_at") or "").replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)
    return sorted(articles, key=_key, reverse=True)


def _normalise_published(raw: str) -> str:
    """Best-effort ISO 8601 normalisation."""
    if not raw:
        return ""
    raw = raw.strip()
    # Alpha Vantage: "20240115T103000"
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(raw, fmt).replace(
                tzinfo=timezone.utc
            ).isoformat()
        except ValueError:
            pass
    # Already ISO — just ensure Z suffix becomes +00:00
    return raw.replace("Z", "+00:00")


# ---------------------------------------------------------------------------
# Provider 1 — Marketaux
# ---------------------------------------------------------------------------

def _marketaux_article(item: dict, fetched_via: str) -> dict:
    entities  = item.get("entities", [])
    relevance = max(
        (float(e.get("relevance_score", 0.0)) for e in entities),
        default=0.5,
    )
    return {
        "title":              item.get("title", ""),
        "summary":            item.get("description") or item.get("snippet") or "",
        "url":                item.get("url", ""),
        "published_at":       _normalise_published(item.get("published_at", "")),
        "source":             item.get("source", ""),
        "relevance_score":    round(relevance, 4),
        "source_api":         "marketaux",
        "fetched_via":        fetched_via,
        "data_delay_warning": False,
        "data_delay_note":    None,
    }


def _query_marketaux(
    api_key: str,
    published_after: str,
    *,
    symbols:  str | None = None,
    keywords: list[str] | None = None,
    limit:    int = 50,
) -> list[dict] | None:
    """
    Returns list of raw items, or None if quota/rate-limit hit.
    Returns [] on other errors.
    """
    params: dict = {
        "api_token":       api_key,
        "filter_entities": "true",
        "language":        "en",
        "limit":           limit,
        "published_after": published_after,
    }
    if symbols:
        params["symbols"] = symbols
    elif keywords:
        params["search"] = " OR ".join(keywords[:5])
    else:
        return []

    try:
        resp = requests.get(MARKETAUX_BASE, params=params, timeout=10)
        if _is_quota_error(resp):
            log.warning("Provider marketaux quota exceeded — trying next provider")
            return None   # signals quota hit to caller
        resp.raise_for_status()
        return resp.json().get("data", [])
    except requests.RequestException as exc:
        log.error("Marketaux request failed: %s", exc)
        return []


def _fetch_marketaux(
    pos: dict,
    published_after: str,
) -> tuple[list[dict], str]:
    """
    Full 3-step Marketaux chain for one position.
    Returns (articles, fetched_via).
    """
    api_key = PROVIDERS["marketaux"]
    if not api_key:
        return [], "none"

    ticker        = pos["ticker"]
    proxy_tickers = pos.get("proxy_tickers", [])
    search_terms  = pos.get("search_terms", [])
    pos_id        = pos["id"]

    # Step 1: primary ticker
    log.info("[%s] Marketaux — primary ticker %s …", pos_id, ticker)
    raw = _query_marketaux(api_key, published_after, symbols=ticker)
    time.sleep(RATE_LIMIT_SLEEP)
    if raw is None:
        return [], "none"   # quota hit, skip whole provider
    if len(raw) >= MIN_ARTICLES:
        return [_marketaux_article(i, "primary_ticker") for i in raw], "primary_ticker"

    # Step 2: proxy tickers
    if proxy_tickers:
        symbols_str = ",".join(proxy_tickers)
        log.info("[%s] Marketaux — proxy tickers %s …", pos_id, symbols_str)
        raw2 = _query_marketaux(api_key, published_after, symbols=symbols_str)
        time.sleep(RATE_LIMIT_SLEEP)
        if raw2 is None:
            return [], "none"
        if len(raw2) >= MIN_ARTICLES:
            return [_marketaux_article(i, "proxy_ticker") for i in raw2], "proxy_ticker"
        raw = raw or raw2   # keep best result so far

    # Step 3: keyword search
    if search_terms:
        log.info("[%s] Marketaux — keyword search …", pos_id)
        raw3 = _query_marketaux(api_key, published_after, keywords=search_terms)
        time.sleep(RATE_LIMIT_SLEEP)
        if raw3 is None:
            return [_marketaux_article(i, "keyword_search") for i in raw] if raw else [], "keyword_search"
        if raw3:
            return [_marketaux_article(i, "keyword_search") for i in raw3], "keyword_search"

    # Return whatever we accumulated (may be < MIN_ARTICLES)
    if raw:
        return [_marketaux_article(i, "keyword_search") for i in raw], "keyword_search"
    return [], "none"


# ---------------------------------------------------------------------------
# Provider 2 — Alpha Vantage
# ---------------------------------------------------------------------------

def _alphavantage_article(item: dict, fetched_via: str) -> dict:
    # ticker_sentiment is a list; take max relevance_score
    ts = item.get("ticker_sentiment", [])
    relevance = max(
        (float(t.get("relevance_score", 0.0)) for t in ts),
        default=None,
    ) if ts else None

    # overall_sentiment_score is also available
    if relevance is None:
        relevance = float(item.get("overall_sentiment_score", 0.5) or 0.5)

    source_domain = item.get("source_domain", "") or item.get("source", "")

    return {
        "title":              item.get("title", ""),
        "summary":            item.get("summary", ""),
        "url":                item.get("url", ""),
        "published_at":       _normalise_published(item.get("time_published", "")),
        "source":             source_domain,
        "relevance_score":    round(float(relevance), 4),
        "source_api":         "alpha_vantage",
        "fetched_via":        fetched_via,
        "data_delay_warning": False,
        "data_delay_note":    None,
    }


def _query_alphavantage(
    api_key: str,
    published_after_compact: str,
    *,
    tickers:  str | None = None,
    topics:   str | None = None,
    limit:    int = 50,
) -> list[dict] | None:
    params: dict = {
        "function":   "NEWS_SENTIMENT",
        "apikey":     api_key,
        "limit":      limit,
        "time_from":  published_after_compact,
        "sort":       "LATEST",
    }
    if tickers:
        params["tickers"] = tickers
    if topics:
        params["topics"] = topics

    try:
        resp = requests.get(ALPHAVANTAGE_BASE, params=params, timeout=10)
        if _is_quota_error(resp):
            log.warning("Provider alpha_vantage quota exceeded — trying next provider")
            return None
        resp.raise_for_status()
        body = resp.json()
        # AV returns {"Information": "..."} when rate-limited on free tier
        if "Information" in body or "Note" in body:
            msg = body.get("Information") or body.get("Note", "")
            log.warning("Alpha Vantage: %s", msg[:120])
            return None
        return body.get("feed", [])
    except requests.RequestException as exc:
        log.error("Alpha Vantage request failed: %s", exc)
        return []


def _fetch_alphavantage(
    pos: dict,
    published_after_compact: str,
) -> tuple[list[dict], str]:
    api_key = PROVIDERS["alpha_vantage"]
    if not api_key:
        return [], "none"

    pos_id = pos["id"]
    ticker = pos["ticker"]

    # Step 4: ticker
    log.info("[%s] Alpha Vantage — ticker %s …", pos_id, ticker)
    raw = _query_alphavantage(api_key, published_after_compact, tickers=ticker)
    time.sleep(RATE_LIMIT_SLEEP)
    if raw is None:
        return [], "none"
    if len(raw) >= MIN_ARTICLES:
        return [_alphavantage_article(i, "primary_ticker") for i in raw], "primary_ticker"

    # Step 5: search_terms as topics/keywords
    search_terms = pos.get("search_terms", [])
    if search_terms:
        log.info("[%s] Alpha Vantage — keyword search …", pos_id)
        # AV topics param is limited; pass first keyword as free-text via tickers fallback
        # Best-effort: join up to 3 terms as comma-separated tickers (may yield results)
        kw_query = ",".join(search_terms[:3])
        raw2 = _query_alphavantage(
            api_key, published_after_compact, tickers=kw_query
        )
        time.sleep(RATE_LIMIT_SLEEP)
        if raw2 is None:
            if raw:
                return [_alphavantage_article(i, "keyword_search") for i in raw], "keyword_search"
            return [], "none"
        if raw2:
            return [_alphavantage_article(i, "keyword_search") for i in raw2], "keyword_search"

    if raw:
        return [_alphavantage_article(i, "primary_ticker") for i in raw], "primary_ticker"
    return [], "none"


# ---------------------------------------------------------------------------
# Provider 3 — GNews
# ---------------------------------------------------------------------------

def _gnews_article(item: dict, fetched_via: str) -> dict:
    source = item.get("source", {})
    return {
        "title":              item.get("title", ""),
        "summary":            item.get("description", ""),
        "url":                item.get("url", ""),
        "published_at":       _normalise_published(item.get("publishedAt", "")),
        "source":             source.get("name", "") if isinstance(source, dict) else str(source),
        "relevance_score":    None,
        "source_api":         "gnews",
        "fetched_via":        fetched_via,
        "data_delay_warning": False,
        "data_delay_note":    None,
    }


def _query_gnews(
    api_key: str,
    published_after_iso: str,
    query: str,
    limit: int = 10,
) -> list[dict] | None:
    params: dict = {
        "q":      query,
        "lang":   "en",
        "from":   published_after_iso,
        "max":    limit,
        "token":  api_key,
    }
    try:
        resp = requests.get(GNEWS_BASE, params=params, timeout=10)
        if _is_quota_error(resp):
            log.warning("Provider gnews quota exceeded — trying next provider")
            return None
        resp.raise_for_status()
        return resp.json().get("articles", [])
    except requests.RequestException as exc:
        log.error("GNews request failed: %s", exc)
        return []


def _fetch_gnews(
    pos: dict,
    published_after_iso: str,
) -> tuple[list[dict], str]:
    api_key = PROVIDERS["gnews"]
    if not api_key:
        return [], "none"

    pos_id       = pos["id"]
    search_terms = pos.get("search_terms", [])
    if not search_terms:
        return [], "none"

    # GNews: build a short OR-query from the top 3 search terms
    query = " OR ".join(f'"{t}"' for t in search_terms[:3])
    log.info("[%s] GNews — keyword search …", pos_id)
    raw = _query_gnews(api_key, published_after_iso, query)
    time.sleep(RATE_LIMIT_SLEEP)
    if raw is None:
        return [], "none"
    return [_gnews_article(i, "keyword_search") for i in raw], "keyword_search"


# ---------------------------------------------------------------------------
# Provider 4 — NewsAPI  (last resort)
# ---------------------------------------------------------------------------

def _newsapi_article(item: dict, fetched_via: str) -> dict:
    source = item.get("source", {})
    return {
        "title":              item.get("title", ""),
        "summary":            item.get("description", ""),
        "url":                item.get("url", ""),
        "published_at":       _normalise_published(item.get("publishedAt", "")),
        "source":             source.get("name", "") if isinstance(source, dict) else str(source),
        "relevance_score":    None,
        "source_api":         "newsapi",
        "fetched_via":        fetched_via,
        "data_delay_warning": True,
        "data_delay_note":    "Sourced via NewsAPI free tier — 24h delay",
    }


def _query_newsapi(
    api_key: str,
    published_after_iso: str,
    query: str,
    limit: int = 20,
) -> list[dict] | None:
    params: dict = {
        "q":        query,
        "language": "en",
        "from":     published_after_iso,
        "pageSize": limit,
        "sortBy":   "publishedAt",
        "apiKey":   api_key,
    }
    try:
        resp = requests.get(NEWSAPI_BASE, params=params, timeout=10)
        if _is_quota_error(resp):
            log.warning("Provider newsapi quota exceeded — trying next provider")
            return None
        if resp.status_code == 426:
            log.warning("NewsAPI 426 Upgrade Required — skipping")
            return None
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "error":
            log.warning("NewsAPI error: %s", body.get("message", "unknown"))
            return []
        return body.get("articles", [])
    except requests.RequestException as exc:
        log.error("NewsAPI request failed: %s", exc)
        return []


def _fetch_newsapi(
    pos: dict,
    published_after_iso: str,
) -> tuple[list[dict], str]:
    api_key = PROVIDERS["newsapi"]
    if not api_key:
        return [], "none"

    pos_id       = pos["id"]
    search_terms = pos.get("search_terms", [])
    if not search_terms:
        return [], "none"

    query = " OR ".join(search_terms[:4])
    log.info("[%s] NewsAPI — keyword search (last resort) …", pos_id)
    raw = _query_newsapi(api_key, published_after_iso, query)
    time.sleep(RATE_LIMIT_SLEEP)
    if raw is None:
        return [], "none"
    return [_newsapi_article(i, "keyword_search") for i in raw], "keyword_search"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def fetch_news_for_positions(
    positions:     list[dict] | None = None,
    lookback_days: int = 30,
) -> dict:
    """
    Fetch news for every position using a 4-provider fallback chain.

    Returns dict keyed by position_id → deduplicated, date-sorted articles.
    """
    if positions is None:
        positions = _load_positions()

    published_iso     = _published_after_iso(lookback_days)
    published_compact = _published_after_compact(lookback_days)
    results: dict     = {}

    for pos in positions:
        pos_id   = pos["id"]
        articles: list[dict] = []
        via      = "none"
        provider_used = "none"

        # ── Provider 1: Marketaux ───────────────────────────────────────────
        if PROVIDERS["marketaux"]:
            articles, via = _fetch_marketaux(pos, published_iso)
            if articles:
                provider_used = "marketaux"

        # ── Provider 2: Alpha Vantage ───────────────────────────────────────
        if len(articles) < MIN_ARTICLES and PROVIDERS["alpha_vantage"]:
            av_articles, av_via = _fetch_alphavantage(pos, published_compact)
            if av_articles:
                articles  = av_articles
                via       = av_via
                provider_used = "alpha_vantage"

        # ── Provider 3: GNews ───────────────────────────────────────────────
        if len(articles) < MIN_ARTICLES and PROVIDERS["gnews"]:
            gn_articles, gn_via = _fetch_gnews(pos, published_iso)
            if gn_articles:
                articles  = gn_articles
                via       = gn_via
                provider_used = "gnews"

        # ── Provider 4: NewsAPI (last resort) ───────────────────────────────
        if len(articles) < MIN_ARTICLES and PROVIDERS["newsapi"]:
            na_articles, na_via = _fetch_newsapi(pos, published_iso)
            if na_articles:
                articles  = na_articles
                via       = na_via
                provider_used = "newsapi"

        if not articles:
            log.warning("[%s] No articles found from any provider.", pos_id)

        final = _sort_by_date(_deduplicate(articles))
        results[pos_id] = final

        log.info(
            "[%s] %d article(s) — provider=%s  fetched_via=%s",
            pos_id, len(final), provider_used, via,
        )

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

    SEP  = "=" * 62
    DASH = "─" * 62

    print(f"\n{SEP}")
    print("  PROVIDER STATUS")
    print(SEP)
    log_provider_status()
    active = [k for k, v in PROVIDERS.items() if v]
    print(f"  Active providers : {', '.join(active) if active else 'none'}")

    print(f"\n{SEP}")
    print("  NEWS FETCH — 30-DAY HEADLINES")
    print(SEP)

    news = fetch_news_for_positions()

    for pos_id, articles in news.items():
        print(f"\n{DASH}")
        print(f"  Position : {pos_id}")

        if not articles:
            print("  No articles found from any provider.")
            continue

        a0  = articles[0]
        via = a0.get("fetched_via", "unknown")
        src = a0.get("source_api", "unknown")
        print(f"  Articles : {len(articles)}  |  provider={src}  fetched_via={via}")

        if a0.get("data_delay_warning"):
            print("  ⚠️  Data delay warning — NewsAPI 24h delay applies")

        print("  2 most recent:")
        for a in articles[:2]:
            pub = (a.get("published_at") or "")[:10] or "unknown"
            title = (a.get("title") or "")[:78]
            delay = " [24h delay]" if a.get("data_delay_warning") else ""
            print(f"    [{pub}]{delay} {title}")

    print(f"\n{SEP}\n")
