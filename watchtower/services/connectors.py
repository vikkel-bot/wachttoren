from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from watchtower.domain import MarketSnapshot, NewsEvent, utc_now
from watchtower.services.crypto_market import CryptoMarketAdapter
from watchtower.services.resolver import AssetResolver

log = logging.getLogger("watchtower.connectors")

BBC_RSS_FEEDS = {
    "bbc-business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "bbc-technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
}


def test_mode_enabled() -> bool:
    return os.getenv("TEST_MODE", "false").strip().lower() == "true"

YFINANCE_TICKER_ALIASES = {
    "GOLD": "GLD",
    "SILVER": "SLV",
    "COPPER": "CPER",
    "WTI": "USO",
    "BRENT": "BNO",
    "NATGAS": "UNG",
    "ASML": "ASML.AS",
    "INGA": "INGA.AS",
}


POSITIVE_WORDS = {
    "beat",
    "beats",
    "breakout",
    "growth",
    "higher",
    "profit",
    "raise",
    "raises",
    "record",
    "surge",
    "upgraded",
}
NEGATIVE_WORDS = {
    "downgrade",
    "downgraded",
    "fall",
    "falls",
    "fraud",
    "loss",
    "miss",
    "misses",
    "probe",
    "recall",
    "risk",
    "slump",
    "warning",
}


class RateLimitError(ValueError):
    """Raised when a connector-side request budget is exhausted."""


def naive_sentiment(text: str) -> float:
    words = set(re.findall(r"[a-z]+", text.lower()))
    positive = len(words & POSITIVE_WORDS)
    negative = len(words & NEGATIVE_WORDS)
    if positive == negative:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / 4.0))


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return utc_now()


def _load_dotenv() -> None:
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _stable_event_id(prefix: str, *parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _clamp(value: float, minimum: float = -1.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _parse_alpha_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return utc_now()


def _parse_eodhd_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return utc_now()


def _alpha_time_param(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")


class AlphaVantageNewsConnector:
    """News + sentiment via Alpha Vantage NEWS_SENTIMENT."""

    BASE_URL = "https://www.alphavantage.co/query"
    CRYPTO_TICKERS = {
        "BTC-EUR": "CRYPTO:BTC",
        "BTC": "CRYPTO:BTC",
        "ETH-EUR": "CRYPTO:ETH",
        "ETH-BTC": "CRYPTO:ETH",
        "ETH": "CRYPTO:ETH",
        "SOL-EUR": "CRYPTO:SOL",
        "SOL": "CRYPTO:SOL",
    }

    def __init__(
        self,
        api_key: str | None = None,
        rate_file: str | Path | None = None,
        daily_limit: int = 25,
        fetch_json: Callable[[str, dict[str, str] | None], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _load_dotenv()
        self.api_key = api_key or os.getenv("ALPHAVANTAGE_API_KEY")
        data_dir = Path(os.getenv("WATCHTOWER_DATA_DIR", "data"))
        self.rate_file = Path(rate_file) if rate_file else data_dir / "alphavantage_requests.json"
        self.daily_limit = daily_limit
        self._fetch_json = fetch_json or self._urlopen_json
        self._sleep = sleeper

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.available else "ALPHAVANTAGE_API_KEY is not set"

    def map_ticker(self, asset: str) -> str:
        normalized = asset.strip().upper().replace("/", "-").replace("_", "-")
        return self.CRYPTO_TICKERS.get(normalized, normalized)

    def fetch(
        self,
        asset: str,
        limit: int = 50,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[NewsEvent]:
        if not self.available:
            raise ValueError(self.unavailable_reason)

        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": self.map_ticker(asset),
            "apikey": self.api_key or "",
            "limit": str(max(1, min(limit, 1000))),
        }
        if from_dt:
            params["time_from"] = _alpha_time_param(from_dt) or ""
        if to_dt:
            params["time_to"] = _alpha_time_param(to_dt) or ""
        if from_dt or to_dt:
            params["sort"] = "EARLIEST"

        payload = self._request(params)
        return self._map_events(asset, payload)

    def fetch_historical(
        self,
        asset: str,
        from_dt: datetime,
        to_dt: datetime,
        limit: int = 200,
    ) -> list[NewsEvent]:
        events: list[NewsEvent] = []
        cursor = from_dt
        while cursor < to_dt and len(events) < limit:
            end = min(cursor + timedelta(days=7), to_dt)
            remaining = limit - len(events)
            events.extend(self.fetch(asset, limit=remaining, from_dt=cursor, to_dt=end))
            cursor = end
            if cursor < to_dt and len(events) < limit:
                self._sleep(2.0)
        return events[:limit]

    def _request(self, params: dict[str, str]) -> dict[str, Any]:
        self._check_and_increment_rate_limit()
        query = urllib.parse.urlencode(params)
        payload = self._fetch_json(f"{self.BASE_URL}?{query}", None)
        if not isinstance(payload, dict):
            raise ValueError("Alpha Vantage response must be a JSON object")
        note = str(payload.get("Note") or payload.get("Information") or "")
        if "rate limit" in note.lower() or "standard api rate limit" in note.lower():
            raise RateLimitError(note)
        if payload.get("Error Message"):
            raise ValueError(str(payload["Error Message"]))
        return payload

    def _check_and_increment_rate_limit(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        state = {"date": today, "count": 0}
        if self.rate_file.exists():
            try:
                loaded = json.loads(self.rate_file.read_text(encoding="utf-8"))
                if loaded.get("date") == today:
                    state = {"date": today, "count": int(loaded.get("count", 0))}
            except (OSError, ValueError, TypeError):
                state = {"date": today, "count": 0}

        if state["count"] >= self.daily_limit:
            raise RateLimitError(
                f"Alpha Vantage daily request limit reached ({self.daily_limit}/day). "
                f"Counter file: {self.rate_file}"
            )

        self.rate_file.parent.mkdir(parents=True, exist_ok=True)
        state["count"] += 1
        self.rate_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _map_events(self, asset: str, payload: dict[str, Any]) -> list[NewsEvent]:
        feed = payload.get("feed", [])
        if not isinstance(feed, list):
            return []

        events: list[NewsEvent] = []
        mapped_ticker = self.map_ticker(asset)
        for item in feed:
            if not isinstance(item, dict):
                continue
            ticker_sentiment = self._ticker_sentiment(item.get("ticker_sentiment"), mapped_ticker)
            sentiment = _clamp(_safe_float(item.get("overall_sentiment_score"), 0.0))
            relevance = _clamp(_safe_float(ticker_sentiment.get("relevance_score"), 0.5), 0.0, 1.0)
            ticker_score = _clamp(_safe_float(ticker_sentiment.get("ticker_sentiment_score"), sentiment))
            label = str(item.get("overall_sentiment_label") or "Neutral")
            title = str(item.get("title") or "Untitled Alpha Vantage news")
            url = str(item.get("url") or "")
            published_at = _parse_alpha_datetime(item.get("time_published"))
            events.append(
                NewsEvent(
                    id=_stable_event_id("av", mapped_ticker, url, published_at.isoformat(), title),
                    asset=asset.upper(),
                    headline=title,
                    summary=str(item.get("summary") or ""),
                    source=str(item.get("source") or "Alpha Vantage"),
                    url=url or None,
                    published_at=published_at,
                    sentiment=sentiment,
                    novelty=0.55,
                    relevance=relevance,
                    tags=["alpha_vantage", "sentiment", label],
                    metadata={
                        "connector": "alphavantage-news",
                        "overall_sentiment_label": label,
                        "ticker_sentiment_score": ticker_score,
                        "ticker": mapped_ticker,
                    },
                )
            )
        return events

    def _ticker_sentiment(self, values: Any, mapped_ticker: str) -> dict[str, Any]:
        if not isinstance(values, list):
            return {}
        for item in values:
            if isinstance(item, dict) and item.get("ticker") == mapped_ticker:
                return item
        return values[0] if values and isinstance(values[0], dict) else {}

    def _urlopen_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        request = urllib.request.Request(url, headers=headers or {"User-Agent": "watchtower/0.1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


class EODHDNewsConnector:
    """Historical financial news via EODHD Financial News API."""

    BASE_URL = "https://eodhd.com/api/news"
    TICKERS = {
        "BTC-EUR": "BTC-USD.CC",
        "BTC": "BTC-USD.CC",
        "ETH-EUR": "ETH-USD.CC",
        "ETH-BTC": "ETH-USD.CC",
        "ETH": "ETH-USD.CC",
        "SOL-EUR": "SOL-USD.CC",
        "SOL": "SOL-USD.CC",
        "AAPL": "AAPL.US",
        "ASML": "ASML.AS",
    }

    def __init__(
        self,
        api_key: str | None = None,
        rate_file: str | Path | None = None,
        daily_limit: int = 100,
        fetch_json: Callable[[str, dict[str, str] | None], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _load_dotenv()
        self.api_key = api_key or os.getenv("EODHD_API_KEY")
        data_dir = Path(os.getenv("WATCHTOWER_DATA_DIR", "data"))
        self.rate_file = Path(rate_file) if rate_file else data_dir / "eodhd_requests.json"
        self.daily_limit = daily_limit
        self._fetch_json = fetch_json or self._urlopen_json
        self._sleep = sleeper

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.available else "EODHD_API_KEY is not set"

    def map_ticker(self, asset: str) -> str:
        normalized = asset.strip().upper().replace("/", "-").replace("_", "-")
        if normalized in self.TICKERS:
            return self.TICKERS[normalized]
        if "." in normalized:
            return normalized
        return f"{normalized}.US"

    def fetch(self, asset: str, limit: int = 50) -> list[NewsEvent]:
        return self.fetch_historical(asset, None, None, max_articles=limit)

    def fetch_historical(
        self,
        asset: str,
        from_dt: datetime | None,
        to_dt: datetime | None,
        max_articles: int = 500,
    ) -> list[NewsEvent]:
        if not self.available:
            raise ValueError(self.unavailable_reason)

        events: list[NewsEvent] = []
        offset = 0
        while len(events) < max_articles:
            page_limit = max(1, min(50, max_articles - len(events)))
            params = {
                "s": self.map_ticker(asset),
                "api_token": self.api_key or "",
                "limit": str(page_limit),
                "offset": str(offset),
                "fmt": "json",
            }
            if from_dt:
                params["from"] = self._date_param(from_dt)
            if to_dt:
                params["to"] = self._date_param(to_dt)

            payload = self._request(params)
            mapped = self._map_events(asset, payload)
            events.extend(mapped)
            if len(mapped) < page_limit or len(events) >= max_articles:
                break
            offset += page_limit
            self._sleep(1.0)
        return events[:max_articles]

    def _request(self, params: dict[str, str]) -> Any:
        self._check_and_increment_rate_limit()
        query = urllib.parse.urlencode(params)
        payload = self._fetch_json(f"{self.BASE_URL}?{query}", {"User-Agent": "watchtower/0.1"})
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or payload.get("errors") or "")
            if message:
                raise ValueError(message)
        if not isinstance(payload, (list, dict)):
            raise ValueError("EODHD news response must be a JSON list or object")
        return payload

    def _check_and_increment_rate_limit(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        state = {"date": today, "count": 0}
        if self.rate_file.exists():
            try:
                loaded = json.loads(self.rate_file.read_text(encoding="utf-8"))
                if loaded.get("date") == today:
                    state = {"date": today, "count": int(loaded.get("count", 0))}
            except (OSError, ValueError, TypeError):
                state = {"date": today, "count": 0}

        if state["count"] >= self.daily_limit:
            raise RateLimitError(
                f"EODHD daily request limit reached ({self.daily_limit}/day). "
                f"Counter file: {self.rate_file}"
            )

        self.rate_file.parent.mkdir(parents=True, exist_ok=True)
        state["count"] += 1
        self.rate_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _map_events(self, asset: str, payload: Any) -> list[NewsEvent]:
        articles: Any = payload
        if isinstance(payload, dict):
            articles = payload.get("data") or payload.get("news") or payload.get("items") or []
        if not isinstance(articles, list):
            return []

        events: list[NewsEvent] = []
        ticker = self.map_ticker(asset)
        for item in articles:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Untitled EODHD news")
            url = str(item.get("link") or item.get("url") or "")
            content = str(item.get("content") or item.get("summary") or "")
            summary = content[:500] if len(content) > 500 else content
            published_at = _parse_eodhd_datetime(str(item.get("date") or ""))
            symbols = self._symbols(item.get("symbols"))
            events.append(
                NewsEvent(
                    id=_stable_event_id("eodhd", ticker, url, published_at.isoformat(), title),
                    asset=asset.upper(),
                    headline=title,
                    summary=summary,
                    source=str(item.get("source") or "EODHD"),
                    url=url or None,
                    published_at=published_at,
                    sentiment=0.0,
                    novelty=0.5,
                    relevance=0.8 if ticker in symbols else 0.5,
                    tags=["eodhd", *symbols],
                    metadata={"connector": "eodhd-news", "ticker": ticker, "symbols": symbols},
                )
            )
        return events

    def _symbols(self, value: Any) -> list[str]:
        if isinstance(value, list):
            symbols = []
            for item in value:
                if isinstance(item, dict):
                    symbol = item.get("code") or item.get("symbol") or item.get("ticker")
                    if symbol:
                        symbols.append(str(symbol).upper())
                else:
                    symbols.append(str(item).upper())
            return symbols
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        return []

    def _date_param(self, value: datetime) -> str:
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date().isoformat()

    def _urlopen_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        request = urllib.request.Request(url, headers=headers or {"User-Agent": "watchtower/0.1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


class YahooFinanceMarketConnector:
    """Read-only market snapshots via yfinance daily OHLCV."""

    def __init__(
        self,
        fetch_history: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self._fetch_history = fetch_history or self._yfinance_history

    def map_ticker(self, asset: str) -> str:
        normalized = asset.strip().upper().replace("/", "-").replace("_", "-")
        if normalized in YFINANCE_TICKER_ALIASES:
            return YFINANCE_TICKER_ALIASES[normalized]
        return normalized

    def fetch(self, asset: str, period: str = "5d", interval: str = "1d") -> MarketSnapshot:
        ticker = self.map_ticker(asset)
        df = self._fetch_history(ticker, period, interval)
        if df is None or getattr(df, "empty", True):
            raise ValueError(f"no yfinance data for {asset} ({ticker})")

        rows = []
        for _idx, row in df.iterrows():
            close = _safe_float(row.get("Close"), 0.0)
            if close <= 0:
                continue
            rows.append(row)
        if not rows:
            raise ValueError(f"no valid yfinance close prices for {asset} ({ticker})")

        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else rows[-1]
        open_price = _safe_float(last.get("Open"), _safe_float(last.get("Close"), 0.0))
        high = _safe_float(last.get("High"), open_price)
        low = _safe_float(last.get("Low"), open_price)
        close = _safe_float(last.get("Close"), open_price)
        volume = _safe_float(last.get("Volume"), 0.0)
        prev_close = _safe_float(prev.get("Close"), close)
        change_1d_pct = ((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
        change_open_pct = ((close - open_price) / open_price * 100.0) if open_price > 0 else change_1d_pct
        previous_volumes = [_safe_float(row.get("Volume"), 0.0) for row in rows[:-1]]
        avg_volume = sum(previous_volumes) / len(previous_volumes) if previous_volumes else volume
        volume_zscore = ((volume - avg_volume) / avg_volume) if avg_volume > 0 else 0.0
        volatility_zscore = ((high - low) / close) if close > 0 else 0.0
        return MarketSnapshot(
            asset=asset.upper(),
            price=close,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            change_15m_pct=0.0,
            change_1h_pct=round(change_open_pct, 4),
            change_1d_pct=round(change_1d_pct, 4),
            volume_zscore=round(volume_zscore, 4),
            volatility_zscore=round(volatility_zscore, 4),
            trend_1h=round(_clamp(change_open_pct / 5.0), 4),
            trend_1d=round(_clamp(change_1d_pct / 5.0), 4),
        )

    def _yfinance_history(self, ticker: str, period: str, interval: str) -> Any:
        import yfinance as yf

        return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)


class ConnectorRegistry:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        _load_dotenv()
        self.resolver = resolver or AssetResolver()
        self.crypto_market = CryptoMarketAdapter()
        self.alpha_vantage_news = AlphaVantageNewsConnector()
        self.eodhd_news = EODHDNewsConnector()
        self.yfinance_market = YahooFinanceMarketConnector()

    def list_connectors(self) -> list[dict]:
        connectors = [
            {
                "name": "rss",
                "type": "news",
                "requires": ["feed_url"],
                "description": "Fetches public RSS/Atom-like feeds through urllib.",
            },
            {
                "name": "bbc-business",
                "type": "news",
                "requires": [],
                "description": "BBC Business public RSS feed for production news refresh.",
            },
            {
                "name": "bbc-technology",
                "type": "news",
                "requires": [],
                "description": "BBC Technology public RSS feed for production news refresh.",
            },
            {
                "name": "alphavantage-news",
                "type": "news",
                "requires": ["ALPHAVANTAGE_API_KEY"],
                "available": self.alpha_vantage_news.available,
                "unavailable_reason": self.alpha_vantage_news.unavailable_reason,
                "description": "Alpha Vantage historical market news with sentiment scoring.",
            },
            {
                "name": "eodhd-news",
                "type": "news",
                "requires": ["EODHD_API_KEY"],
                "available": self.eodhd_news.available,
                "unavailable_reason": self.eodhd_news.unavailable_reason,
                "description": "EODHD historical financial news with offset pagination.",
            },
            {
                "name": "bitvavo-public",
                "type": "market",
                "requires": [],
                "description": "Public Bitvavo crypto ticker and 1h candles for BTC-EUR, ETH-EUR, ETH-BTC and other pairs.",
            },
            {
                "name": "yfinance-market",
                "type": "market",
                "requires": [],
                "description": "Real daily OHLCV snapshots via yfinance for equities, ETFs and mapped commodities.",
            },
        ]
        if test_mode_enabled():
            connectors[0:0] = [
                {
                    "name": "mock-news",
                    "type": "news",
                    "requires": [],
                    "description": "Deterministic demo news events for local testing.",
                },
                {
                    "name": "mock-regional-news",
                    "type": "news",
                    "requires": [],
                    "description": "Region-aware demo news events for exchange pipeline testing.",
                },
            ]
            connectors.insert(-2, {
                "name": "mock-market",
                "type": "market",
                "requires": [],
                "description": "Deterministic demo market snapshot for local testing.",
            })
            connectors.insert(-2, {
                "name": "mock-regional-market",
                "type": "market",
                "requires": [],
                "description": "Region-aware demo market snapshot for exchange pipeline testing.",
            })
        return connectors

    def fetch_news(
        self,
        connector: str,
        asset: str,
        limit: int = 10,
        feed_url: str | None = None,
        exchange: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[NewsEvent]:
        connector = connector.lower()
        resolved_asset = self.resolver.resolve(asset)
        if connector in {"mock-news", "mock-regional-news"}:
            if not test_mode_enabled():
                raise ValueError("Mock news connectors are only available when TEST_MODE=true")
            return self._mock_news(resolved_asset, limit, exchange=exchange)
        if connector == "rss":
            if not feed_url:
                raise ValueError("feed_url is required for rss connector")
            return self._rss_news(resolved_asset, feed_url, limit)
        if connector in BBC_RSS_FEEDS:
            return self._rss_news(resolved_asset, BBC_RSS_FEEDS[connector], limit, source_name=connector)
        if connector == "alphavantage-news":
            return self.alpha_vantage_news.fetch(resolved_asset, limit=limit, from_dt=from_dt, to_dt=to_dt)
        if connector == "eodhd-news":
            return self.eodhd_news.fetch_historical(resolved_asset, from_dt, to_dt, max_articles=limit)
        raise ValueError(f"Unknown news connector: {connector}")

    def fetch_historical_news(
        self,
        connector: str,
        asset: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int = 200,
    ) -> list[NewsEvent]:
        connector = connector.lower()
        resolved_asset = self.resolver.resolve(asset)
        if connector == "alphavantage-news":
            start = from_dt or (to_dt or utc_now()) - timedelta(days=7)
            end = to_dt or utc_now()
            return self.alpha_vantage_news.fetch_historical(resolved_asset, start, end, limit=limit)
        if connector == "eodhd-news":
            return self.eodhd_news.fetch_historical(resolved_asset, from_dt, to_dt, max_articles=limit)
        raise ValueError(f"Unknown historical news connector: {connector}")

    def fetch_market(self, connector: str, asset: str, exchange: str | None = None) -> MarketSnapshot:
        connector = connector.lower()
        resolved_asset = self.resolver.resolve(asset)
        if connector in {"bitvavo-public", "crypto-public"}:
            return self.crypto_market.fetch_snapshot(resolved_asset)
        if connector in {"yfinance-market", "yahoo-finance"}:
            return self.yfinance_market.fetch(resolved_asset)
        if connector not in {"mock-market", "mock-regional-market"}:
            raise ValueError(f"Unknown market connector: {connector}")
        if not test_mode_enabled():
            raise ValueError("Mock market connectors are only available when TEST_MODE=true")
        profile = self._market_profile(exchange)
        return MarketSnapshot(
            asset=resolved_asset,
            price=profile["price"],
            change_15m_pct=profile["change_15m_pct"],
            change_1h_pct=profile["change_1h_pct"],
            change_1d_pct=profile["change_1d_pct"],
            volume_zscore=profile["volume_zscore"],
            volatility_zscore=profile["volatility_zscore"],
            trend_1h=profile["trend_1h"],
            trend_1d=profile["trend_1d"],
            sector_change_1d_pct=profile["sector_change_1d_pct"],
            benchmark_change_1d_pct=profile["benchmark_change_1d_pct"],
            resistance_distance_pct=profile["resistance_distance_pct"],
        )

    def _mock_news(self, asset: str, limit: int, exchange: str | None = None) -> list[NewsEvent]:
        exchange_tag = exchange.upper() if exchange else "GLOBAL"
        examples = [
            (
                f"{asset} beats expectations on {exchange_tag} watch",
                f"Fresh {exchange_tag} report suggests improving momentum and stronger forward guidance.",
                0.72,
                ["earnings", "guidance", exchange_tag.lower()],
            ),
            (
                f"{asset} faces local volatility as traders wait for confirmation",
                "The initial move is mixed while market participants watch volume, currency and sector strength.",
                0.08,
                ["market_context", exchange_tag.lower()],
            ),
            (
                f"Analysts flag downside risk for {asset} in regional trading",
                "A cautious note points to margin pressure, currency sensitivity and slower near-term demand.",
                -0.45,
                ["analyst", "risk", exchange_tag.lower()],
            ),
        ]
        events = []
        for headline, summary, sentiment, tags in examples[: max(1, min(limit, len(examples)))]:
            events.append(
                NewsEvent(
                    id=f"evt_{uuid4().hex[:12]}",
                    asset=asset,
                    headline=headline,
                    summary=summary,
                    source="mock-news",
                    sentiment=sentiment,
                    novelty=0.72,
                    relevance=0.82,
                    tags=tags,
                )
            )
        return events

    def _market_profile(self, exchange: str | None) -> dict[str, float]:
        exchange_code = (exchange or "GLOBAL").upper()
        defaults = {
            "price": 100.0,
            "change_15m_pct": 0.25,
            "change_1h_pct": 0.75,
            "change_1d_pct": 1.4,
            "volume_zscore": 1.8,
            "volatility_zscore": 0.9,
            "trend_1h": 0.42,
            "trend_1d": 0.35,
            "sector_change_1d_pct": 0.6,
            "benchmark_change_1d_pct": 0.45,
            "resistance_distance_pct": 2.0,
        }
        overrides = {
            "NASDAQ": {"volume_zscore": 2.4, "trend_1h": 0.62, "trend_1d": 0.48, "benchmark_change_1d_pct": 0.75},
            "AEX": {"volume_zscore": 1.7, "trend_1h": 0.38, "sector_change_1d_pct": 0.85, "benchmark_change_1d_pct": 0.35},
            "JPX": {"change_1d_pct": 1.8, "volatility_zscore": 1.35, "benchmark_change_1d_pct": 0.9},
            "HKEX": {"change_1d_pct": 1.7, "volatility_zscore": 1.55, "benchmark_change_1d_pct": 0.7},
            "SSE": {"change_1d_pct": 1.25, "volatility_zscore": 1.2, "benchmark_change_1d_pct": 0.55},
            "JSE": {"volume_zscore": 1.35, "volatility_zscore": 1.45, "benchmark_change_1d_pct": 0.25},
            "NGX": {"volume_zscore": 1.1, "volatility_zscore": 1.8, "benchmark_change_1d_pct": 0.35},
            "EGX": {"volume_zscore": 1.2, "volatility_zscore": 1.7, "benchmark_change_1d_pct": 0.4},
            "NSE_KE": {"volume_zscore": 1.15, "volatility_zscore": 1.55, "benchmark_change_1d_pct": 0.2},
            "CSE_MA": {"volume_zscore": 1.1, "volatility_zscore": 1.35, "benchmark_change_1d_pct": 0.15},
            "COMEX": {"volume_zscore": 2.1, "volatility_zscore": 1.25, "trend_1h": 0.5, "benchmark_change_1d_pct": 0.45},
            "NYMEX": {"volume_zscore": 2.0, "volatility_zscore": 1.85, "trend_1h": 0.35, "benchmark_change_1d_pct": 0.2},
            "ICE": {"volume_zscore": 1.9, "volatility_zscore": 1.65, "trend_1h": 0.32, "benchmark_change_1d_pct": 0.25},
            "LME": {"volume_zscore": 1.7, "volatility_zscore": 1.35, "trend_1h": 0.28, "benchmark_change_1d_pct": 0.15},
            "SHFE": {"volume_zscore": 1.8, "volatility_zscore": 1.45, "trend_1h": 0.3, "benchmark_change_1d_pct": 0.35},
        }
        return {**defaults, **overrides.get(exchange_code, {})}

    def _rss_news(self, asset: str, feed_url: str, limit: int, source_name: str | None = None) -> list[NewsEvent]:
        request = urllib.request.Request(feed_url, headers={"User-Agent": "watchtower/0.1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_xml = response.read()

        root = ET.fromstring(raw_xml)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        events: list[NewsEvent] = []
        for item in items[: max(1, min(limit, 50))]:
            title = self._xml_text(item, "title")
            summary = self._xml_text(item, "description") or self._xml_text(item, "{http://www.w3.org/2005/Atom}summary")
            link = self._xml_text(item, "link")
            if not link:
                link_node = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
            published_at = parse_datetime(
                self._xml_text(item, "pubDate")
                or self._xml_text(item, "published")
                or self._xml_text(item, "{http://www.w3.org/2005/Atom}published")
            )
            text = f"{title} {summary}"
            relevance = 0.85 if asset.upper() in text.upper() else 0.45
            source = source_name or urllib.parse.urlparse(feed_url).netloc or "rss"
            events.append(
                NewsEvent(
                    id=_stable_event_id("rss", source, link, published_at.isoformat(), title),
                    asset=asset,
                    headline=title or "Untitled feed item",
                    summary=summary,
                    source=source,
                    url=link or None,
                    published_at=published_at,
                    sentiment=naive_sentiment(text),
                    novelty=0.55,
                    relevance=relevance,
                    tags=["rss"],
                )
            )
        return events

    def _xml_text(self, item: ET.Element, name: str) -> str:
        node = item.find(name)
        if node is None or node.text is None:
            return ""
        return node.text.strip()
